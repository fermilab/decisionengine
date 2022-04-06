#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2017 Fermi Research Alliance, LLC
# SPDX-License-Identifier: Apache-2.0

"""
Main loop for Decision Engine.
The following environment variable points to decision engine configuration file:
``DECISION_ENGINE_CONFIG_FILE``
if this environment variable is not defined the ``DE-Config.py`` file from the ``../tests/etc/` directory will be used.
"""

import argparse
import contextlib
import copy
import enum
import json
import logging
import os
import re
import signal
import socketserver
import sys
import xmlrpc.server

from threading import Event

import cherrypy
import pandas as pd
import redis
import structlog
import tabulate

from kombu import Exchange

import decisionengine.framework.dataspace.datablock as datablock
import decisionengine.framework.dataspace.dataspace as dataspace
import decisionengine.framework.modules.de_logger as de_logger
import decisionengine.framework.taskmanager.ProcessingState as ProcessingState
import decisionengine.framework.taskmanager.TaskManager as TaskManager

from decisionengine.framework.config import ChannelConfigHandler, policies, ValidConfig
from decisionengine.framework.dataspace.maintain import Reaper
from decisionengine.framework.engine.ChannelWorkers import ChannelWorker, ChannelWorkers
from decisionengine.framework.engine.SourceWorkers import SourceWorkers
from decisionengine.framework.modules.logging_configDict import DELOGGER_CHANNEL_NAME, LOGGERNAME
from decisionengine.framework.taskmanager.module_graph import source_products, validated_workflow
from decisionengine.framework.util.countdown import Countdown
from decisionengine.framework.util.metrics import display_metrics, Gauge, Histogram
from decisionengine.framework.util.redis_stats import redis_stats

DEFAULT_WEBSERVER_PORT = 8000

# DecisionEngine metrics
STATUS_HISTOGRAM = Histogram("de_client_status_duration_seconds", "Time to run de-client --status")
PRINT_PRODUCT_HISTOGRAM = Histogram("de_client_print_product_duration_seconds", "Time to run de-client --print-product")
START_CHANNEL_HISTOGRAM = Histogram(
    "de_client_start_channel_duration_seconds", "Time to run de-client --start-channel", ["channel_name"]
)
RM_CHANNEL_HISTOGRAM = Histogram(
    "de_client_rm_channel_duration_seconds", "Time to run de-client --stop-channel", ["channel_name"]
)
QUERY_TOOL_HISTOGRAM = Histogram("de_client_query_duration_seconds", "Time to run de-client --query", ["product"])
METRICS_HISTOGRAM = Histogram("de_client_metrics_duration_seconds", "Time to run de-client --status")
WORKERS_COUNT = Gauge("de_workers_total", "Number of workers started by the Decision Engine")


class StopState(enum.Enum):
    NotFound = 1
    Clean = 2
    Terminated = 3


def _channel_preamble(name):
    header = f"Channel: {name}"
    rule = "=" * len(header)
    return "\n" + rule + "\n" + header + "\n" + rule + "\n\n"


def _verify_redis_url(broker_url):
    m = re.search(r"(?P<backend>\w+)://.*", broker_url)
    if m is None:
        raise RuntimeError(
            f"Unsupported broker URL format '{broker_url}'\nSee https://docs.celeryproject.org/projects/kombu/en/stable/userguide/connections.html#urls"
        )

    backend = m.group("backend")
    if backend != "redis":
        raise RuntimeError(f"Unsupported data-broker backend '{backend}'; only 'redis' is currently supported.")


def _verify_redis_server(broker_url):
    _verify_redis_url(broker_url)
    r = redis.Redis.from_url(broker_url)
    try:
        r.ping()
    except Exception:
        raise RuntimeError(f"A server with broker URL {broker_url} is not responding.")


class RequestHandler(xmlrpc.server.SimpleXMLRPCRequestHandler):
    rpc_paths = ("/RPC2",)


class DecisionEngine(socketserver.ThreadingMixIn, xmlrpc.server.SimpleXMLRPCServer):
    def __init__(self, global_config, channel_config_loader, server_address):
        xmlrpc.server.SimpleXMLRPCServer.__init__(
            self, server_address, logRequests=False, requestHandler=RequestHandler
        )
        signal.signal(signal.SIGHUP, self.handle_sighup)
        self.channel_config_loader = channel_config_loader
        self.global_config = global_config
        self.dataspace = dataspace.DataSpace(self.global_config)
        self.reaper = Reaper(self.global_config)
        self.startup_complete = Event()
        self.logger = structlog.getLogger(LOGGERNAME)
        self.logger = self.logger.bind(module=__name__.split(".")[-1], channel=DELOGGER_CHANNEL_NAME)
        self.logger.debug(f"DecisionEngine starting on {server_address}")

        exchange_name = self.global_config.get("exchange_name", "hepcloud_topic_exchange")
        self.logger.debug(f"Creating topic exchange {exchange_name}")
        self.exchange = Exchange(exchange_name, "topic")
        self.broker_url = self.global_config.get("broker_url", "redis://localhost:6379/0")
        _verify_redis_server(self.broker_url)

        self.source_workers = SourceWorkers(self.exchange, self.broker_url, self.logger)
        self.channel_workers = ChannelWorkers()

        self.register_function(self.rpc_metrics, name="metrics")

        self.logger.info(f"DecisionEngine __init__ complete {server_address} with {self.broker_url}")

    def get_logger(self):
        return self.logger

    def _dispatch(self, method, params):
        try:
            # methods allowed to be executed by rpc have 'rpc_' pre-pended
            func = getattr(self, "rpc_" + method)
        except AttributeError:
            raise Exception(f'method "{method}" is not supported')
        return func(*params)

    def service_actions(self):
        # Overrides the base class service_actions, taking sources
        # offline whenever the client task managers have gone offline.
        with self.channel_workers.access() as workers:
            for channel_name, worker in workers.items():
                tm = worker.task_manager
                if tm.state.probably_running():
                    continue

                self.source_workers.detach_channel(tm.name, tm.routing_keys)

    def block_while(self, state, timeout=None):
        self.logger.debug(f"Waiting for {state} or timeout={timeout} on channel_workers.")
        workers = self.channel_workers.unguarded_access()
        if not workers:
            self.logger.info("No active channels to wait on.")
            return "No active channels."
        countdown = Countdown(wait_up_to=timeout)
        for tm in workers.values():
            if tm.is_alive():
                self.logger.debug(f"Waiting for {tm.task_manager.name} to exit {state} state.")
                with countdown:
                    tm.wait_while(state, countdown.time_left)
        return f"No channels in {state} state."

    def _dataframe_to_table(self, df):
        return f"{tabulate.tabulate(df, headers='keys', tablefmt='psql')}\n"

    def _dataframe_to_vertical_tables(self, df):
        txt = ""
        for i in range(len(df)):
            txt += f"Row {i}\n"
            txt += f"{tabulate.tabulate(df.T.iloc[:, [i]], tablefmt='psql')}\n"
        return txt

    def _dataframe_to_column_names(self, df):
        columns = df.columns.values.reshape([len(df.columns), 1])
        return f"{tabulate.tabulate(columns, headers=['columns'], tablefmt='psql')}\n"

    def _dataframe_to_json(self, df):
        return f"{json.dumps(json.loads(df.to_json()), indent=4)}\n"

    def _dataframe_to_csv(self, df):
        return f"{df.to_csv()}\n"

    def rpc_ping(self):
        return "pong"

    def rpc_block_while(self, state_str, timeout=None):
        allowed_state = None
        try:
            allowed_state = ProcessingState.State[state_str]
        except Exception:
            return f"{state_str} is not a valid channel state."
        return self.block_while(allowed_state, timeout)

    def rpc_show_config(self, channel):
        """
        Show the configuration for a channel.

        :type channel: string
        """
        txt = ""
        channels = self.channel_config_loader.get_channels()
        if channel == "all":
            for ch in channels:
                txt += _channel_preamble(ch)
                txt += self.channel_config_loader.print_channel_config(ch)
            return txt

        if channel not in channels:
            return f"There is no active channel named {channel}."

        txt += _channel_preamble(channel)
        txt += self.channel_config_loader.print_channel_config(channel)
        return txt

    def rpc_show_de_config(self):
        return self.global_config.dump()

    @PRINT_PRODUCT_HISTOGRAM.time()
    def rpc_print_product(self, product, columns=None, query=None, types=False, format=None):
        if not isinstance(product, str):
            raise ValueError(f"Requested product should be a string not {type(product)}")

        found = False
        txt = f"Product {product}: "
        with self.channel_workers.access() as workers:
            for ch, worker in workers.items():
                if not worker.is_alive():
                    txt += f"Channel {ch} is in not active\n"
                    self.logger.debug(f"Channel:{ch} is in not active when running rpc_print_product")
                    continue

                produces = worker.get_produces()
                r = [x for x in list(produces.items()) if product in x[1]]
                if not r:
                    continue
                found = True
                txt += f" Found in channel {ch}\n"
                self.logger.debug(f"Found channel:{ch} active when running rpc_print_product")
                tm = self.dataspace.get_taskmanager(ch)
                self.logger.debug(f"rpc_print_product - channel:{ch} taskmanager:{tm}")
                try:
                    data_block = datablock.DataBlock(
                        self.dataspace, ch, taskmanager_id=tm["taskmanager_id"], sequence_id=tm["sequence_id"]
                    )
                    data_block.generation_id -= 1
                    df = data_block[product]
                    dfj = df.to_json()
                    self.logger.debug(f"rpc_print_product - channel:{ch} task manager:{tm} datablock:{dfj}")
                    df = pd.read_json(dfj)
                    dataframe_formatter = self._dataframe_to_table
                    if format == "vertical":
                        dataframe_formatter = self._dataframe_to_vertical_tables
                    if format == "column-names":
                        dataframe_formatter = self._dataframe_to_column_names
                    if format == "json":
                        dataframe_formatter = self._dataframe_to_json
                    if types:
                        for column in df.columns:
                            df.insert(
                                df.columns.get_loc(column) + 1,
                                f"{column}.type",
                                df[column].transform(lambda x: type(x).__name__),
                            )
                    column_names = []
                    if columns:
                        column_names = columns.split(",")
                    if query:
                        if column_names:
                            txt += dataframe_formatter(df.loc[:, column_names].query(query))
                        else:
                            txt += dataframe_formatter(df.query(query))

                    else:
                        if column_names:
                            txt += dataframe_formatter(df.loc[:, column_names])
                        else:
                            txt += dataframe_formatter(df)
                except Exception as e:  # pragma: no cover
                    txt += f"\t\t{e}\n"
        if not found:
            txt += "Not produced by any module\n"
        return txt[:-1]

    def rpc_print_products(self):
        with self.channel_workers.access() as workers:
            channel_keys = workers.keys()
            if not channel_keys:
                return "No channels are currently active.\n"

            width = max(len(x) for x in channel_keys) + 1
            txt = ""
            for ch, worker in workers.items():
                if not worker.is_alive():
                    txt += f"Channel {ch} is in ERROR state\n"
                    continue

                txt += f"channel: {ch:<{width}}, id = {worker.task_manager.id:<{width}}, state = {worker.get_state_name():<10} \n"
                tm = self.dataspace.get_taskmanager(ch)
                data_block = datablock.DataBlock(
                    self.dataspace, ch, taskmanager_id=tm["taskmanager_id"], sequence_id=tm["sequence_id"]
                )
                data_block.generation_id -= 1
                channel_config = self.channel_config_loader.get_channels()[ch]
                produces = worker.get_produces()
                # FIXME: See comment below re. printing product dependencies of the logic engine.
                for i in ("sources", "transforms"):
                    txt += f"\t{i}:\n"
                    modules = channel_config.get(i, {})
                    for mod_name in modules.keys():
                        txt += f"\t\t{mod_name}\n"
                        for product in produces[mod_name]:
                            try:
                                df = data_block[product]
                                df = pd.read_json(df.to_json())
                                txt += f"{tabulate.tabulate(df, headers='keys', tablefmt='psql')}\n"
                            except Exception as e:  # pragma: no cover
                                txt += f"\t\t\t{e}\n"
        return txt[:-1]

    @STATUS_HISTOGRAM.time()
    def rpc_status(self):
        workers = self.source_workers.unguarded_access()
        source_keys = workers.keys()
        if not source_keys:
            return "No sources or channels are currently active.\n" + self.reaper_status()

        txt = ""
        width = max(len(x) for x in source_keys)
        queue_width = max(len(x.queue.name) for x in workers.values())
        for source, worker in workers.items():
            state = worker.state.get().name
            queue = worker.queue.name
            txt += f"source: {source:<{width}}, queue id = {queue:<{queue_width}}, state = {state}\n"

        txt += "\n"

        workers = self.channel_workers.unguarded_access()
        channel_keys = workers.keys()
        if not channel_keys:
            txt += "No channels are currently active.\n" + self.reaper_status()
            return txt

        width = max(len(x) for x in channel_keys)
        for ch, worker in workers.items():
            txt += f"channel: {ch:<{width}}, id = {worker.task_manager.id:<{width}}, state = {worker.get_state_name():<10}\n"
        return txt + self.reaper_status()

    def rpc_queue_status(self):
        status = redis_stats(self.broker_url, self.exchange.name)
        return f"\n{tabulate.tabulate(status, headers=['Source name', 'Queue name', 'Unconsumed messages'])}"

    def rpc_product_dependencies(self):
        workers = self.source_workers.unguarded_access()
        if not workers:
            return "No sources or channels are currently active.\n" + self.reaper_status()

        txt = "\nsources\n"
        for source, worker in sorted(workers.items()):
            txt += f"\t{source}:\n"
            produces = worker.module_instance._produces.keys()
            txt += f"\t\tproduces: {list(produces)}\n"
        txt += "\n"

        workers = self.channel_workers.unguarded_access()
        if not workers:
            txt += "No channels are currently active.\n" + self.reaper_status()
            return txt

        for ch, worker in sorted(workers.items()):
            txt += f"channel: {ch}\n"
            produces = worker.get_produces()
            consumes = worker.get_consumes()
            channel_config = self.channel_config_loader.get_channels()[ch]

            # FIXME: Not sure what we should do about printing the logic engine facts/rules.  Some options:
            #        1. Omit entirely (default as 'produces' and 'consumes' are both empty)
            #        2. Display which data products are used in the logic-engine expressions
            txt += "\ttransforms:\n"
            for mod_name in sorted(channel_config.get("transforms", {}).keys()):
                txt += f"\t\t{mod_name}\n"
                txt += f"\t\t\tconsumes: {consumes[mod_name]}\n"
                txt += f"\t\t\tproduces: {produces[mod_name]}\n"

            txt += "\tpublishers:\n"
            for mod_name in sorted(channel_config.get("publishers", {}).keys()):
                txt += f"\t\t{mod_name}\n"
                txt += f"\t\t\tconsumes: {consumes[mod_name]}\n"

        return txt

    def rpc_stop(self):
        self.shutdown()
        self.stop_channels()
        self.reaper_stop()
        self.dataspace.close()

        if not self.global_config.get("no_webserver"):
            cherrypy.engine.exit()

        de_logger.stop_queue_logger()
        return "OK"

    def start_channel(self, channel_name, channel_config):
        channel_config = copy.deepcopy(channel_config)
        with START_CHANNEL_HISTOGRAM.labels(channel_name).time():
            # NB: Possibly override channel name
            channel_name = channel_config.get("channel_name", channel_name)
            source_configs = channel_config.pop("sources")
            src_workers = self.source_workers.update(channel_name, source_configs)
            module_workers = validated_workflow(channel_name, src_workers, channel_config, self.logger)

            queue_info = [(worker.queue.name, worker.key) for worker in src_workers.values()]
            self.logger.debug(f"Building TaskManger for {channel_name}")
            task_manager = TaskManager.TaskManager(
                channel_name,
                module_workers,
                dataspace.DataSpace(self.global_config),
                source_products(src_workers),
                self.exchange,
                self.broker_url,
                queue_info,
            )
            self.logger.debug(f"Building Worker for {channel_name}")
            worker = ChannelWorker(task_manager, self.global_config["logger"])
            WORKERS_COUNT.inc()
            with self.channel_workers.access() as workers:
                workers[channel_name] = worker

            # The channel must be started first so it can listen for the messages from the sources.
            self.logger.debug(f"Trying to start {channel_name}")
            worker.start()
            self.logger.info(f"Channel {channel_name} started")

            worker.wait_while(ProcessingState.State.BOOT)

            # Start any sources that are not yet alive.
            for key, src_worker in src_workers.items():
                if src_worker.is_alive():
                    continue
                if src_worker.exitcode == 0:  # pragma: no cover
                    # This can happen if the source's acquire method runs only once (e.g. when testing)
                    # and the first process completes before the next channel can use it.
                    raise RuntimeError(
                        f"The {key} source has already completed and cannot be used by channel {channel_name}."
                    )

                src_worker.start()
                self.logger.debug(f"Started process {src_worker.pid} for source {key}")

            worker.wait_while(ProcessingState.State.ACTIVE)

    def start_channels(self):
        self.channel_config_loader.load_all_channels()

        if not self.channel_config_loader.get_channels():
            self.logger.info(
                "No channel configurations available in " + f"{self.channel_config_loader.channel_config_dir}"
            )
        else:
            self.logger.debug(f"Found channels: {self.channel_config_loader.get_channels().items()}")

        # FIXME: Should figure out a way to load the channels in parallel.  Unfortunately, there are data races that
        #        occur when doing that (observed with Python 3.10).
        for name, config in self.channel_config_loader.get_channels().items():
            try:
                self.start_channel(name, config)
            except Exception as e:
                self.logger.exception(f"Channel {name} failed to start: {e}")

    def rpc_start_channel(self, channel_name):
        with self.channel_workers.access() as workers:
            if channel_name in workers:
                return f"ERROR, channel {channel_name} is running"

        success, result = self.channel_config_loader.load_channel(channel_name)
        if not success:
            return result
        self.start_channel(channel_name, result)
        return "OK"

    def rpc_start_channels(self):
        self.start_channels()
        return "OK"

    def rpc_stop_channel(self, channel):
        return self.rpc_rm_channel(channel, None)

    def rpc_kill_channel(self, channel, timeout=None):
        if timeout is None:
            timeout = self.global_config.get("shutdown_timeout", 10)
        return self.rpc_rm_channel(channel, timeout)

    def rpc_rm_channel(self, channel, maybe_timeout):
        rc = self.rm_channel(channel, maybe_timeout)
        if rc == StopState.NotFound:
            return f"No channel found with the name {channel}."
        elif rc == StopState.Terminated:
            if maybe_timeout == 0:
                return f"Channel {channel} has been killed."
            # Would be better to use something like the inflect
            # module, but that introduces another dependency.
            suffix = "s" if maybe_timeout > 1 else ""
            return f"Channel {channel} has been killed due to shutdown timeout ({maybe_timeout} second{suffix})."
        assert rc == StopState.Clean
        WORKERS_COUNT.dec()
        return f"Channel {channel} stopped cleanly."

    def rm_channel(self, channel, maybe_timeout):
        with RM_CHANNEL_HISTOGRAM.labels(channel).time():
            rc = None
            with self.channel_workers.access() as workers:
                worker = workers.get(channel)
                if worker is None:
                    return StopState.NotFound
                sources_to_prune = worker.task_manager.routing_keys
                self.logger.debug(f"Trying to stop {channel}")
                rc = self.stop_worker(worker, maybe_timeout)
                del workers[channel]
                self.logger.debug(f"Channel {channel} removed ({rc})")
                self.source_workers.prune(channel, sources_to_prune)
            return rc

    def stop_worker(self, worker, timeout):
        if worker.is_alive():
            self.logger.debug("Trying to take worker offline")
            worker.task_manager.take_offline()
            worker.join(timeout)
        if worker.exitcode is None:
            worker.terminate()
            return StopState.Terminated
        else:
            return StopState.Clean

    def stop_channels(self):
        timeout = self.global_config.get("shutdown_timeout", 10)
        with self.channel_workers.access() as workers:
            countdown = Countdown(wait_up_to=timeout)
            for worker in workers.values():
                with countdown:
                    self.stop_worker(worker, countdown.time_left)
            workers.clear()
        self.source_workers.remove_all(countdown.time_left)

    def rpc_stop_channels(self):
        self.stop_channels()
        return "All channels stopped."

    def handle_sighup(self, signum, frame):
        self.reaper_stop()
        self.stop_channels()
        self.start_channels()
        self.reaper_start(delay=self.global_config["dataspace"].get("reaper_start_delay_seconds", 1818))

    def rpc_get_log_level(self):
        engineloglevel = self.get_logger().getEffectiveLevel()
        return logging.getLevelName(engineloglevel)

    def rpc_get_channel_log_level(self, channel):
        with self.channel_workers.access() as workers:
            worker = workers.get(channel)
            if worker is None:
                return f"No channel found with the name {channel}."

            if not worker.is_alive():
                return f"Channel {channel} is in ERROR state."
            return logging.getLevelName(worker.task_manager.get_loglevel())

    def rpc_set_channel_log_level(self, channel, log_level):
        """Assumes log_level is a string corresponding to the supported logging-module levels."""
        with self.channel_workers.access() as workers:
            worker = workers.get(channel)
            if worker is None:
                return f"No channel found with the name {channel}."

            if not worker.is_alive():
                return f"Channel {channel} is in ERROR state."

            log_level_code = getattr(logging, log_level)
            if worker.task_manager.get_loglevel() == log_level_code:
                return f"Nothing to do. Current log level is : {log_level}"
            worker.task_manager.set_loglevel_value(log_level)
        return f"Log level changed to : {log_level}"

    def rpc_reaper_start(self, delay=0):
        """
        Start the reaper process after 'delay' seconds.
        Default 0 seconds delay.
        :type delay: int
        """
        self.reaper_start(delay)
        return "OK"

    def reaper_start(self, delay):
        self.reaper.start(delay)

    def rpc_reaper_stop(self):
        self.reaper_stop()
        return "OK"

    def reaper_stop(self):
        self.reaper.stop()

    def rpc_reaper_status(self):
        interval = self.reaper.retention_interval
        state = self.reaper.state.get()
        return f"reaper:\n\tstate: {state}\n\tretention_interval: {interval}"

    def reaper_status(self):
        state = self.reaper.state.get()
        return f"\nreaper: state = {state.name}\n"

    def rpc_query_tool(self, product, format=None, start_time=None):
        with QUERY_TOOL_HISTOGRAM.labels(product).time():
            found = False
            result = pd.DataFrame()
            txt = f"Product {product}: "

            with self.channel_workers.access() as workers:
                for ch, worker in workers.items():
                    if not worker.is_alive():
                        txt += f"Channel {ch} is in not active\n"
                        continue

                    produces = worker.get_produces()
                    r = [x for x in list(produces.items()) if product in x[1]]
                    if not r:
                        continue
                    found = True
                    txt += f" Found in channel {ch}\n"

                    if start_time:
                        tms = self.dataspace.get_taskmanagers(ch, start_time=start_time)
                    else:
                        tms = [self.dataspace.get_taskmanager(ch)]
                    for tm in tms:
                        try:
                            data_block = datablock.DataBlock(
                                self.dataspace, ch, taskmanager_id=tm["taskmanager_id"], sequence_id=tm["sequence_id"]
                            )
                            products = data_block.get_dataproducts(product)
                            for p in products:
                                df = p["value"]
                                if df.shape[0] > 0:
                                    df["channel"] = [tm["name"]] * df.shape[0]
                                    df["taskmanager_id"] = [p["taskmanager_id"]] * df.shape[0]
                                    df["generation_id"] = [p["generation_id"]] * df.shape[0]
                                    result = result.append(df)
                        except Exception as e:  # pragma: no cover
                            txt += f"\t\t{e}\n"

            if found:
                dataframe_formatter = self._dataframe_to_table
                if format == "csv":
                    dataframe_formatter = self._dataframe_to_csv
                if format == "json":
                    dataframe_formatter = self._dataframe_to_json
                result = result.reset_index(drop=True)
                txt += dataframe_formatter(result)
            else:
                txt += "Not produced by any module\n"
            return txt

    def start_webserver(self):
        """
        Start CherryPy webserver using configured port.  If port is not configured
        use default webserver port.
        """
        _socket_host = "0.0.0.0"
        if self.global_config.get("webserver") and isinstance(self.global_config.get("webserver"), dict):
            _port = self.global_config["webserver"].get("port", DEFAULT_WEBSERVER_PORT)
        else:  # pragma: no cover
            # unit tests use a random port
            _port = DEFAULT_WEBSERVER_PORT

        with contextlib.suppress(Exception):
            self.logger.debug(f"Trying to start metrics server on {_socket_host}:{_port}")

        cherrypy.config.update(
            {"server.socket_port": _port, "server.socket_host": _socket_host, "server.shutdown_timeout": 1}
        )
        cherrypy.engine.signals.subscribe()
        cherrypy.tree.mount(self)
        # we know for sure the cherrypy logger is working, so use that too
        cherrypy.log(f"Trying to start metrics server on {_socket_host}:{_port}")
        cherrypy.engine.start()
        with contextlib.suppress(Exception):
            self.logger.debug("Started CherryPy server")

    @cherrypy.expose
    def metrics(self):
        return self.rpc_metrics()

    @METRICS_HISTOGRAM.time()
    def rpc_metrics(self):
        """
        Display collected metrics
        """
        try:
            return display_metrics()
        except Exception as e:  # pragma: no cover
            self.logger.error(e)


def parse_program_options(args=None):
    """If args is a list, it will be used instead of sys.argv"""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--port",
        default=8888,
        type=int,
        choices=range(1, 65535),
        metavar="<port number>",
        help="Default port number is 8888; allowed values are in the half-open interval [1, 65535).",
    )
    parser.add_argument(
        "--config",
        default=policies.GLOBAL_CONFIG_FILENAME,
        metavar="<filename>",
        help="Configuration file for initializing server; default behavior is to choose "
        + f"'{policies.GLOBAL_CONFIG_FILENAME}' located in the CONFIG_PATH directory.",
    )
    parser.add_argument(
        "--no-webserver",
        action="store_true",
        help="Run the decision engine without the accompanying webserver. "
        + "Note that if this option is given, metrics collection will not work.",
    )
    return parser.parse_args(args)


def _check_metrics_env(options):
    if options.no_webserver:
        return
    try:
        assert "PROMETHEUS_MULTIPROC_DIR" in os.environ
    except AssertionError:
        msg = (
            "If running with metrics (webserver), PROMETHEUS_MULTIPROC_DIR"
            " must be set.  If you wish to run the decision engine without"
            " the metrics webserver, please pass the --no-webserver option."
        )
        print(msg, file=sys.stderr)
        raise OSError(msg)


def _get_global_config(config_file, options):
    global_config = None
    try:
        global_config = ValidConfig.ValidConfig(config_file)
    except Exception as msg:  # pragma: no cover
        sys.exit(f"Failed to load configuration {config_file}\n{msg}")

    global_config.update(
        # Use Jsonnet-supported schema (i.e. not a tuple)
        {"server_address": ["localhost", options.port]}
    )

    if options.no_webserver:
        global_config.update({"no_webserver": True})

    return global_config


def _get_de_conf_manager(global_config_dir, channel_config_dir, options):
    config_file = os.path.join(global_config_dir, options.config)
    if not os.path.isfile(config_file):  # pragma: no cover
        raise Exception(f"Config file '{config_file}' not found")

    global_config = _get_global_config(config_file, options)
    conf_manager = ChannelConfigHandler.ChannelConfigHandler(global_config, channel_config_dir)

    return (global_config, conf_manager)


def _create_de_server(global_config, channel_config_loader):
    """Create the DE server with the passed global configuration and config manager"""
    server_address = tuple(global_config.get("server_address"))
    return DecisionEngine(global_config, channel_config_loader, server_address)


def _start_de_server(server):
    """Start the DE server and listen forever"""
    try:
        server.get_logger().info("running _start_de_server")

        server.get_logger().debug("running _start_de_server: step reaper_start")
        server.reaper_start(delay=server.global_config["dataspace"].get("reaper_start_delay_seconds", 1818))

        server.get_logger().debug("running _start_de_server: step start_channels")
        server.start_channels()

        if not server.global_config.get("no_webserver"):
            # cherrypy for metrics
            server.get_logger().debug("running _start_de_server: step start_webserver (metrics)")
            server.start_webserver()

        server.get_logger().debug("running _start_de_server: step startup_complete")
        server.startup_complete.set()

        server.get_logger().debug("running _start_de_server: step serve_forever")
        server.serve_forever(
            poll_interval=1
        )  # Once per second is sufficient, given the amount of work done in the service actions.

        server.get_logger().debug("done with _start_de_server")
    except Exception as __e:  # pragma: no cover
        msg = f"""Server Address: {server.global_config.get('server_address')}
              Fatal Error: {__e}"""
        print(msg, file=sys.stderr)

        with contextlib.suppress(Exception):
            server.get_logger().error(msg)

        raise __e
    finally:
        r = redis.Redis.from_url(server.broker_url)
        with contextlib.suppress(Exception):
            r.flushdb()


def main(args=None):
    """
    If args is None, sys.argv will be used instead
    If args is a list, it will be used instead of sys.argv (for unit testing)
    """
    options = parse_program_options(args)
    _check_metrics_env(options)
    global_config_dir = policies.global_config_dir()
    global_config, channel_config_loader = _get_de_conf_manager(
        global_config_dir, policies.channel_config_dir(), options
    )
    try:
        server = _create_de_server(global_config, channel_config_loader)
        _start_de_server(server)

    except Exception as e:  # pragma: no cover
        msg = f"""Config Dir: {global_config_dir}
              Fatal Error: {e}"""
        print(msg, file=sys.stderr)
        sys.exit(msg)


if __name__ == "__main__":
    if os.geteuid() == 0:
        raise RuntimeError("You cannot run this as root")

    main()
