""" 
Diematic daemon.

(C) 2022 by Ignacio Hernández-Ros
Based on a previous work from Germain Masse 2019

See and respect licensing terms

"""
import logging
import json
import yaml
import os
import signal
import time
import threading
import argparse
import sys

from lockfile import pidlockfile
from boiler import Boiler 
from pymodbus.client.sync import ModbusSerialClient as ModbusClient
from influxdb import InfluxDBClient
from influxdb.exceptions import InfluxDBClientError
from daemon import DaemonContext
from daemon import pidfile
from aiohttp import web
from typing import Any

import paho.mqtt.client as mqtt
import asyncio
import concurrent.futures
import ssl

from webserver import DiematicWebRequestHandler
from filelck import FileLock, FileLockException

"""

For InfluxDB backend

influx -precision rfc3339

CREATE DATABASE "diematic"
GRANT ALL ON "diematic" TO "diematic"
CREATE RETENTION POLICY "one_day" ON "diematic" DURATION 24h REPLICATION 1 DEFAULT
CREATE RETENTION POLICY "five_weeks" ON "diematic" DURATION 5w REPLICATION 1
CREATE RETENTION POLICY "five_years" ON "diematic" DURATION 260w REPLICATION 1

CREATE CONTINUOUS QUERY "cq_month" ON "diematic" BEGIN
  SELECT mean(/temperature/) AS "mean_1h", mean(/pressure/) AS "mean_1h", max(/temperature/) AS "max_1h", max(/pressure/) AS "max_1h"
  INTO "five_weeks".:MEASUREMENT
  FROM "one_day"."diematic"
  GROUP BY time(1h),*
END

CREATE CONTINUOUS QUERY "cq_year" ON "diematic" BEGIN
  SELECT mean(/^mean_.*temperature/) AS "mean_24h", mean(/^mean_.*pressure/) AS "mean_24h", max(/^max_.*temperature/) AS "max_24h", max(/^max_.*pressure/) AS "max_24h"
  INTO "five_years".:MEASUREMENT
  FROM "five_weeks"."diematic"
  GROUP BY time(24h),*
END

DROP CONTINUOUS QUERY cq_month ON diematic
DROP CONTINUOUS QUERY cq_year ON diematic

"""

# --------------------------------------------------------------------------- #
# configure the client logging
# --------------------------------------------------------------------------- #
FORMAT = ('%(asctime)-15s %(threadName)-15s '
          '%(levelname)-8s %(module)-15s:%(lineno)-8s %(message)s')
logging.basicConfig(format=FORMAT, level=logging.ERROR)
log = logging.getLogger()

DEFAULT_LOGGING = 'error'

DEFAULT_MODBUS_TIMEOUT = 10
DEFAULT_MODBUS_BAUDRATE = 9600
DEFAULT_MODBUS_UNIT = 10
DEFAULT_MODBUS_DEVICE = None

class DaemonRunnerError(Exception):
    """ Abstract base class for errors from DaemonRunner. """

class DaemonRunnerInvalidActionError(DaemonRunnerError, ValueError):
    """ Raised when specified action for DaemonRunner is invalid. """

class DaemonRunnerStartFailureError(DaemonRunnerError, RuntimeError):
    """ Raised when failure starting DaemonRunner. """


class DaemonRunnerStopFailureError(DaemonRunnerError, RuntimeError):
    """ Raised when failure stopping DaemonRunner. """

class DaemonRunnerReloadFailureError(DaemonRunnerError, RuntimeError):
    """ Raised when failure reloading DaemonRunner. """

class DiematicModbusError(RuntimeError):
    """ Happens when modbus communication is error """

class DiematicApp:
    """ Application container """

    start_message = "started with pid {pid:d}"

    def __init__(self):
        # --------------------------------------------------------------------------- #
        # set configuration variables (command line prevails on configuration file)
        # --------------------------------------------------------------------------- #
        self.MODBUS_TIMEOUT = None
        self.MODBUS_BAUDRATE = None
        self.MODBUS_UNIT = None
        self.MODBUS_DEVICE = None
        self.connection_lock = None

        self.first_run = True

        return

    def _get_context(self):
        """ Returns or create and return the self.context that is used by the surrounding daemon """
        try:
            return self.context
        except AttributeError:
            self.context = DaemonContext(
                pidfile=pidlockfile.PIDLockFile('/run/diematic/diematicd.pid'),
                working_directory="/etc/diematic"
                )

            self.context.signal_map = {
                signal.SIGTERM: self._terminate_daemon_process,
                signal.SIGHUP: self._terminate_daemon_process,
                signal.SIGUSR1: self._reload_configuration,
                }
            self.context.app = self

            self.pidfile_timeout = 3
            return self.context

    def _get_executor(self):
        """ create the executor pool or return if already created """
        try:
            return self.executor
        except AttributeError:
            self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            return self.executor

    def _value_writer(self) -> None:
        """ consumes a job writing to the boiler """
        write = self.MyBoiler.next_write()
        while not write is None and 'name' in write:
            paramName = write['name']
            winfo = self.MyBoiler.prepare_write(write)
            address = winfo['address']
            newvalue = winfo['value']
            log.info("Pending write {register} address {address} newvalue {newvalue}".format(register=paramName, address=address, newvalue=newvalue))
            self._internal_value_writer(paramName, address, newvalue)
            write = self.MyBoiler.next_write()

    def _internal_value_writer(self, paramName: str, address: int, newvalue: int) -> None:
        """ Writes a value to a register
        
        :param paramName: the parameter name, used only to prepare error message if needed
        :param address: the address to write to
        :param newvalue: the value to write, two bytes 
        """
        try_count = 0
        while try_count < 6:
            try:
                with FileLock(self.connection_lock):
                    try:
                        client = ModbusClient(method='rtu', port=self.MODBUS_DEVICE, timeout=self.MODBUS_TIMEOUT, baudrate=self.MODBUS_BAUDRATE)
                        with client:
                            log.info("Going to write")
                            rr = client.write_registers(address, newvalue, unit=self.MODBUS_UNIT)
                            if rr.isError():
                                log.error(rr.message)
                                raise DiematicModbusError(rr.message)
                            rr = client.read_holding_registers(address, unit=self.MODBUS_UNIT)
                            if rr.isError():
                                log.error(rr.message)
                                raise DiematicModbusError(rr.message)
                            receivedvalue = rr.registers[0]
                            if not receivedvalue == newvalue:
                                errormessage = f"write operation success, but read value differs write value {newvalue} read value {receivedvalue} address {address}"
                                self.MyBoiler.write_error(paramName, errormessage)
                                log.error(errormessage)
                            else:
                                self.MyBoiler.write_ok(paramName)
                                log.info(f'Wite value {newvalue} at address {address} success')
                            return
                    except DiematicModbusError as error:
                        try_count += 1
                        self.MyBoiler.write_error(paramName, "write operation failed, {errormessage}".format(errormessage=error))
                        log.info("Repeat in one second")
                        time.sleep(1)
                        pass
            except FileLockException:
                try_count += 1
                log.info("Can't lock the serial port")
                pass

        self.MyBoiler.write_error(paramName, "write operation failed, too many attempts to write parameter {parameterName} value {wvalue} in address {address}".format(parameterName=paramName, wvalue=newvalue, address=address))


    def run(self):
        self._reload_configuration(None,None)

        if self.args.server == 'both' or self.args.server == 'loop':
            self.start_main_program()

        if self.args.server == 'both' or self.args.server == 'web':
            self.startWebServer()

        if self.args.server == 'both':
            loop = asyncio.get_event_loop()
            loop.run_forever()


    def start_main_program(self):
        x = threading.Thread(target=self.main_program_loop)
        x.setDaemon(True)
        x.start()
        if self.args.server == 'loop':
            x.join()

    def main_program_loop(self) -> None:
        while True:
            try:
                self.do_main_program()
            except Exception as ex:
                log.error('Exception inside do_main_program {err}'.format(err=ex))
            time.sleep(60) # a minute

    def startWebServer(self):
        self._create_boiler()

        handler = DiematicWebRequestHandler(self.MyBoiler)

        loop = asyncio.get_event_loop()
        self.webServer = web.Application()
        self.webServer["mainapp"] = self
        self.webServer.add_routes(handler.routes)

        runner = web.AppRunner(self.webServer)
        loop.run_until_complete(runner.setup())

        # argument preference is:
        # cli takes preference over config file.
        http = self.cfg.get('http', None)
        hostname = self.args.hostname if self.hostname_explicit else self.args.hostname if http is None else http.get('hostname', self.args.hostname)
        port = self.args.port if self.port_explicit else self.args.port if http is None else http.get('port', self.args.port)
        site = web.TCPSite(runner, hostname, port)
        loop.run_until_complete(site.start())
        if self.args.server == 'web':
            loop.run_forever()

    def check_pending_writes(self):
        self._get_executor().submit(self._value_writer)

    def _create_boiler(self):
        if self.shall_create_boiler:
            self.MyBoiler = Boiler(uuid=self.cfg['boiler']['uuid'], index=self.cfg['registers'])
            self.shall_create_boiler = False

    def do_main_program(self):
        self._create_boiler()
        self.shall_browse_registers = True
        loop = True
        while loop:
            try:
                self.run_sync_client()
                loop = False
            except DiematicModbusError:
                time.sleep(1)
                pass
        return

    def run_sync_client(self):
        #enabling modbus communication
        if self.first_run:
            log.info("Connection parameters: device={port!r} timeout={timeout!r} baudrate={baudrate!r}".format(port=self.MODBUS_DEVICE, timeout=self.MODBUS_TIMEOUT, baudrate=self.MODBUS_BAUDRATE))
            self.first_run = False
        try:
            with FileLock(self.connection_lock):
                client = ModbusClient(method='rtu', port=self.MODBUS_DEVICE, timeout=self.MODBUS_TIMEOUT, baudrate=self.MODBUS_BAUDRATE)
                with client:
                    self.MyBoiler.registers = []
                    id_stop = -1

                    for mbrange in self.cfg['modbus']['register_ranges']:
                        id_start = mbrange[0]
                        self.MyBoiler.registers.extend([None] * (id_start-id_stop-1))
                        id_stop = mbrange[1]

                        log.debug("Attempt to read registers from {} to {}".format(id_start, id_stop))
                        rr = client.read_holding_registers(count=(id_stop-id_start+1), address=id_start, unit=self.MODBUS_UNIT)
                        if rr.isError():
                            log.error(rr.message)
                            raise DiematicModbusError(rr.message)
                            # MyBoiler.registers.extend([None] * (id_stop-id_start+1))
                        else:
                            self.MyBoiler.registers.extend(rr.registers)
        except FileLockException:
            log.warning("Can't adquire the lock on the serial port")
            return

        #parsing registers to push data in Object attributes
        self.MyBoiler.browse_registers()
        data = self.MyBoiler.fetch_data()
        log.info("Values read")
        log.debug("Dumping values\n" + self.MyBoiler.dump())

        #pushing data to influxdb
        if self.args.backend and (self.args.backend == 'influxdb' or (self.args.backend == 'configured' and 'influxdb' in self.cfg)):
            timestamp = int(time.time() * 1000) #milliseconds
            influx_json_body = [{
                "measurement": "diematic",
                "tags": {
                    "host": "raspberrypi",
                },
                "timestamp": timestamp,
                "fields": data
            }]

            influxcfg = self.cfg.get('influxdb', None)
            influx_host = self.args.influxdb_host if self.influxdb_host_explicit else influxcfg.get('host', None) if influxcfg is not None else None
            influx_port = self.args.influxdb_port if self.influxdb_port_explicit else influxcfg.get('port', None) if influxcfg is not None else None
            influx_user = self.args.influxdb_user if self.influxdb_user_explicit else influxcfg.get('user', None) if influxcfg is not None else None
            influx_password = self.args.influxdb_password if self.influxdb_password_explicit else influxcfg.get('password', None) if influxcfg is not None else None
            influx_database = self.args.influxdb_database if self.influxdb_database_explicit else influxcfg.get('database', None) if influxcfg is not None else None

            if influx_host is not None and influx_port is not None and influx_user is not None and influx_password is not None and influx_database is not None:
                influx_client = InfluxDBClient(influx_host, influx_port, influx_user, influx_password, influx_database)

                log.debug("Write points: {0}".format(influx_json_body))
                try:
                    influx_client.write_points(influx_json_body, time_precision='ms')
                    log.info("Values written to influxdb")
                except InfluxDBClientError as e:
                    log.error(e)
                except Exception as e:
                    log.error(e)
            else:
                log.error(f'influxdb backend is missconfigured, please review configuration file and/or arguments')
        
        if self.args.backend and (self.args.backend == 'mqtt' or self.args.backend == 'configured'):
            try:
                if self.ha_discovery and self.shall_run_discovery:
                    self.home_assistant_discovery(data)
                    self.shall_run_discovery = False
                    log.info('Sending discovery info')
                    time.sleep(0.3)
                mqtt_json_body = json.dumps(data, indent=2)
                self.mqttc.publish(self.mqtt_topic, mqtt_json_body).wait_for_publish()
                log.info('Values published to mqtt')
            except RuntimeError as e:
                log.error('Can\'t publish due to error:', e)

    def _mqtt_device_keys(self) -> tuple[str, str]:
        """
        Returns a tuple that contains device prefix and uuid
        """
        prefix = self.args.mqtt_ha_discovery_prefix 
        if not self.mqtt_ha_discovery_prefix_explicit and 'mqtt' in self.cfg and 'discovery' in self.cfg['mqtt'] and 'prefix' in self.cfg['mqtt']['discovery']:
            confkey = self.cfg['mqtt']['discovery']
            prefix = confkey.get('prefix','homeassistant')
        uuid = self.cfg['boiler']['uuid'].replace('-','')
        return [prefix, uuid]

    def _mqtt_topic_header(self, component: str, object_id: str) -> str:
        prefix, uuid = self._mqtt_device_keys()
        return f'{prefix}/{component}/{uuid}/{object_id}'

    def home_assistant_discovery(self, data: dict[str, Any]) -> None:
        # --------------------------------------------------------------------------- #
        # send home assistant discovery information
        # --------------------------------------------------------------------------- #
        log.debug('preparing discovery for sensors')
        prefix, uuid = self._mqtt_device_keys()
        device_name = self.cfg['boiler'].get('name', 'Boiler')
        model = data.get('boiler_model', 'Unknown')
        sw_version = str(data.get('software_version', 0))
        retain = False
        if self.mqtt_retain_explicit:
            retain = self.args.mqtt_retain
        elif 'mqtt' in self.cfg and 'retain' in self.cfg['mqtt']:
            retain = self.cfg['mqtt'].get('retain')
        subtopic = self.mqtt_topic.split('/').pop()
        for register in self.MyBoiler.index:
            if not 'component' in register:
                continue
            component = register['component']
            if not 'name' in register: 
                continue
            object_id = register['name']
            if not 'entity_category' in register:
                log.error(f'Preparing discovery of {object_id} entity_category is missing')
                continue
            entity_category = register['entity_category']
            if not 'icon' in register:
                log.error(f'Preparing discovery of {object_id} icon is missing')
                continue
            icon = register['icon']
            unit = register.get('unit', None)
            if unit == 'CelsiusTemperature':
                unit = '°C'
            state_class = register.get('state_class', None)
            device_class = register.get('device_class', None)
            min = register.get('min', None)
            max = register.get('max', None)
            step = register.get('step', None)
            value_template = register.get('value_template', None)
            command_template = register.get('command_template', None)
            options = register.get('options', None)
            suggested_display_precision = register.get('suggested_display_precision', None)
            self.ha_discover(
                prefix, uuid, model, sw_version, retain, subtopic, device_name,
                component=component, object_id=object_id, device_class=device_class, 
                entity_category=entity_category, icon=icon, state_class=state_class, 
                unit=unit, min=min, max=max, step=step, value_template=value_template, 
                command_template=command_template, options=options,
                suggested_display_precision=suggested_display_precision
            )

    def ha_discover(self, prefix:str, uuid:str, model: str, sw_version: str, retain: bool, subtopic: str, device_name: str,
        component: str, object_id: str, device_class: str, entity_category: str, icon: str, state_class: str, unit: str,
        min: float = None, max: float = None, step: float = None, value_template: str = None, command_template: str = None,
        options: list[str] = None, suggested_display_precision: int = None
    ):
        entity_name = self.MyBoiler.get_register_field(object_id, 'desc')
        topic_head = f'{prefix}/{component}/{uuid}/{object_id}'
        topic = f'{topic_head}/config'
        config = {
            "config_topic": topic,
            "availability": [
                {
                    "topic": "zigbee2mqtt/bridge/state",
                    "value_template": "{{ value_json.state }}"
                },
                {
                    "topic": self.mqtt_topic_available
                # }, # single device availability, does this really exists?
                # {
                #     "topic": f"{topic_head}/availability"
                }
            ],
            "device": {
                "identifiers": [f"{uuid}"],
                "manufacturer": "De Dietrich",
                "model": model,
                "name": device_name,
                "sw_version": sw_version
            },
            # "json_attributes_topic": f"{topic_head}/attributes",
            "name": f"{entity_name}",
            "retain": retain,
            "state_topic": self.mqtt_topic,
            "value_template": f"{{{{ value_json.{object_id} }}}}",
            "unique_id": f"{uuid}_{object_id}",
            "entity_category": f"{entity_category}",
            "icon": f"{icon}",
            "object_id": f"{subtopic}_{object_id}",
            "qos": 1,
        }
        if device_class is not None:
            config['device_class'] = device_class
        if state_class is not None:
            config['state_class'] = state_class
        if unit is not None:
            config['unit_of_measurement'] = unit
        if min is not None:
            config["min"] = min
        if max is not None:
            config["max"] = max
        if step is not None:
            config["step"] = step
        if value_template is not None:
            config['value_template'] = value_template
        if command_template is not None:
            config['command_template'] = command_template
        if options is not None:
            config['options'] = options
        if suggested_display_precision is not None:
            config['suggested_display_precision'] = suggested_display_precision

        # if component == 'sensor':
        #     config["state_class"] = f"{state_class}"
        if component == 'number' or component == 'select':
            self.command_topic(topic_head, object_id, config)
        if component == 'sensor':
            config['platform'] = 'sensor'
        
        config_str = json.dumps(config, indent=2)
        self.mqttc.publish(topic, config_str).wait_for_publish()
        log.info(f'Entity {object_id} discovered via mqtt')

    def command_topic(self, topic_head: str, object_id: str, config: dict[str, Any]):
        command_topic = f"{topic_head}/set/{object_id}"
        config["command_topic"] = command_topic
        # subscribe to this topic
        self.mqttc.subscribe(command_topic, 2)

    def home_assistant_attributes(self) -> None:
        for register in self.MyBoiler.index:
            if not 'component' in register:
                continue
            component = register['component']
            if not 'name' in register: 
                continue
            object_id = register['name']
            self.ha_attributes(component=component, object_id=object_id)

    def ha_attributes(self, component: str, object_id: str):
        topic_header = self._mqtt_topic_header(component, object_id)
        topic_attributes = f'{topic_header}/attributes'
        self.mqttc.publish(topic_attributes, '{}').wait_for_publish()

    def read_config_file(self):
        # --------------------------------------------------------------------------- #
        # retrieve config from diematic.yaml
        # --------------------------------------------------------------------------- #
        if os.path.isabs(self.args.config):
            config_file = self.args.config
        else:
            main_base = os.path.dirname(__file__)
            config_file = os.path.join(main_base, self.args.config)
        if not os.path.exists(config_file):
            errmsg = "Configuration file not found {file!r}".format(file=config_file)
            raise FileNotFoundError(errmsg)
        with open(config_file) as f:
            # use safe_load instead load
            self.cfg = yaml.safe_load(f)

    def toJSON(self):
        """Return the configuration file in json format"""
        return self.cfg["registers"]

    def set_logging_level(self):
        # --------------------------------------------------------------------------- #
        # set logging level
        # --------------------------------------------------------------------------- #
        new_logging_level = DEFAULT_LOGGING
        if self.args.logging:
            new_logging_level = self.args.logging
        elif 'logging' in self.cfg:
            new_logging_level = self.cfg['logging']
        numeric_level = getattr(logging, new_logging_level.upper())
        if not isinstance(numeric_level, int):
            raise ValueError('Invalid log level: %s' % new_logging_level)
        log.setLevel(numeric_level)

    def _open_streams_from_app_stream_paths(self):
        """ Open the `daemon_context` streams from the paths specified.

            :param app: The application instance.

            Open the `daemon_context` standard streams (`stdin`,
            `stdout`, `stderr`) as stream objects of the appropriate
            types, from each of the corresponding filesystem paths
            from the `app`.
            """
        self.stdin_path = "/dev/null"
        self.stdout_path = os.path.join(os.sep, "var", "log", "diematic", "diematic.out") # Can also be /dev/null 
        self.stderr_path =  os.path.join(os.sep, "var", "log", "diematic", "diematic.err") # Can also be /dev/null

        self._get_context().stdin = open(self.stdin_path, 'rt')
        self._get_context().stdout = open(self.stdout_path, 'w+t')
        self._get_context().stderr = open(self.stderr_path, 'w+t')

    def _start(self):
        """ Open the daemon context and run the application.

            :return: ``None``.
            :raises DaemonRunnerStartFailureError: If the PID file cannot
                be locked by this process.
            """
        if is_pidfile_stale(self._get_context().pidfile):
            self._get_context().pidfile.break_lock()
        elif self.args.server == 'both' and is_process_already_running(self._get_context().pidfile):
            error = DaemonRunnerStartFailureError(
                "Process already running {pid:d}".format(pid=self._get_context().pidfile.read_pid()))
            raise error

        if self.args.server == 'both' and not self.args.foreground:
            try:
                self._open_streams_from_app_stream_paths()
                self._get_context().open()
            except pidlockfile.AlreadyLocked as exc:
                error = DaemonRunnerStartFailureError(
                        "PID file {pidfile.path!r} already locked".format(
                            pidfile=self._get_context().pidfile))
                raise error from exc

            pid = os.getpid()
            message = self.start_message.format(pid=pid)
            emit_message(message, sys.stdout)

        self.run()
    
    def _runonce(self):
        self._reload_configuration(None,None)
        if self.args.server == 'web':
            self.startWebServer()
        else:
            self.do_main_program()

    def _writeregister(self):
        """ Write a value to a register. This method is used for testing purposes only"""
        pass

    def _readregister(self):
        """ Read the content of the indicated register and shows the
            value. Does not need to exist in the yaml file
        """
        address = self.args.address
        format = self.args.format
        self._reload_configuration(None,None)
        self._create_boiler()
        tryCount = 0
        try:
            with FileLock(self.connection_lock):
                while tryCount < 5:
                    registers = []
                    tryCount += 1
                    try:
                        client = ModbusClient(method='rtu', port=self.MODBUS_DEVICE, timeout=self.MODBUS_TIMEOUT, baudrate=self.MODBUS_BAUDRATE)
                        with client:
                            registers.extend([None] * (-1))
                            log.debug("Attempt to read register {}".format(address))
                            rr = client.read_holding_registers(count=1, address=address, unit=self.MODBUS_UNIT)
                            if rr.isError():
                                log.error(rr.message)
                                raise DiematicModbusError(rr.message)
                            else:
                                registers.extend(rr.registers)
                                # format output
                                if format == 'Raw':
                                    emit_message("Register {} value {}".format(address, registers[0]), sys.stdout)
                                elif format == 'DiematicOneDecimal':
                                    emit_message("Register {} value {}".format(address, self.MyBoiler._decode_decimal(registers[0], 1)), sys.stdout)
                                elif format == 'DiematicModeFlag':
                                    emit_message("Register {} value {}".format(address, self.MyBoiler._decode_modeflag(registers[0])), sys.stdout)
                                elif format == 'ErrorCode':
                                    emit_message("Register {} value {}".format(address, self.MyBoiler._decode_errorcode(registers[0])), sys.stdout)
                                elif format == 'DiematicCircType':
                                    emit_message("Register {} value {}".format(address, self.MyBoiler._decode_circtype(registers[0])), sys.stdout)
                                elif format == 'DiematicProgram':
                                    emit_message("Register {} value {}".format(address, self.MyBoiler._decode_program(registers[0])), sys.stdout)
                                elif format == 'Model':
                                    emit_message("Register {} value {}".format(address, self.MyBoiler._decode_model(registers[0])), sys.stdout)
                                elif format == 'bit0':
                                    emit_message("Register {} value {}".format(address, (registers[0] & 0x0001) >> 0), sys.stdout)
                                elif format == 'bit1':
                                    emit_message("Register {} value {}".format(address, (registers[0] & 0x0002) >> 1), sys.stdout)
                                elif format == 'bit2':
                                    emit_message("Register {} value {}".format(address, (registers[0] & 0x0004) >> 2), sys.stdout)
                                elif format == 'bit3':
                                    emit_message("Register {} value {}".format(address, (registers[0] & 0x0008) >> 3), sys.stdout)
                                elif format == 'bit4':
                                    emit_message("Register {} value {}".format(address, (registers[0] & 0x0010) >> 4), sys.stdout)
                                elif format == 'bit5':
                                    emit_message("Register {} value {}".format(address, (registers[0] & 0x0020) >> 5), sys.stdout)
                                elif format == 'bit6':
                                    emit_message("Register {} value {}".format(address, (registers[0] & 0x0040) >> 6), sys.stdout)
                                elif format == 'bit7':
                                    emit_message("Register {} value {}".format(address, (registers[0] & 0x0080) >> 7), sys.stdout)
                                elif format == 'bit8':
                                    emit_message("Register {} value {}".format(address, (registers[0] & 0x0100) >> 8), sys.stdout)
                                elif format == 'bit9':
                                    emit_message("Register {} value {}".format(address, (registers[0] & 0x0200) >> 9), sys.stdout)
                                elif format == 'bitA':
                                    emit_message("Register {} value {}".format(address, (registers[0] & 0x0400) >> 10), sys.stdout)
                                elif format == 'bitB':
                                    emit_message("Register {} value {}".format(address, (registers[0] & 0x0800) >> 11), sys.stdout)
                                elif format == 'bitC':
                                    emit_message("Register {} value {}".format(address, (registers[0] & 0x1000) >> 12), sys.stdout)
                                elif format == 'bitD':
                                    emit_message("Register {} value {}".format(address, (registers[0] & 0x2000) >> 13), sys.stdout)
                                elif format == 'bitE':
                                    emit_message("Register {} value {}".format(address, (registers[0] & 0x4000) >> 14), sys.stdout)
                                elif format == 'bitF':
                                    emit_message("Register {} value {}".format(address, (registers[0] & 0x8000) >> 15), sys.stdout)
                                tryCount = 99 # exit while
                    except DiematicModbusError:
                        time.sleep(1)
                        pass
        except FileLockException:
            log.warning("Can't adquire the lock file on the serial port")
            pass

    def _terminate_daemon_process(self, _signal, _stack):
        """ Terminate the daemon process specified in the current PID file.
            _signal and _stack are required because this function is a signal handler althrough they are not used

            :return: ``None``.
            :raises DaemonRunnerStopFailureError: If terminating the daemon
                fails with an OS error.
            """
        pid = self._get_context().pidfile.read_pid()
        ownpid = os.getpid()
        if pid == ownpid:
            sys.exit(2)
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError as exc:
            error = DaemonRunnerStopFailureError(
                    "Failed to terminate {pid:d}: {exc}".format(
                        pid=pid, exc=exc))
            raise error from exc

    def _stop(self):
        """ Exit the daemon process specified in the current PID file.

            :return: ``None``.
            :raises DaemonRunnerStopFailureError: If the PID file is not
                already locked.
            """
        if not self._get_context().pidfile.is_locked():
            error = DaemonRunnerStopFailureError(
                    "PID file {pidfile.path!r} not locked".format(
                        pidfile=self._get_context().pidfile))
            raise error

        if is_pidfile_stale(self._get_context().pidfile):
            self._get_context().pidfile.break_lock()
        else:
            self._terminate_daemon_process(None, None)

    def _status(self):
        if is_process_already_running(self._get_context().pidfile):
            message = "Process is running on pid {pid:d}".format(pid=self._get_context().pidfile.read_pid())
            emit_message(message, sys.stdout)
        elif is_pidfile_stale(self._get_context().pidfile):
            message = "Process is NOT running but a pid file exists"
            emit_message(message, sys.stdout)
        else:
            message = "Process is NOT running"
            emit_message(message, sys.stdout)

    def _reload(self):
        """ Send a SIGUSR1 to the running process so it is forced to reload configuration file."""
        if not self._get_context().pidfile.is_locked():
            error = DaemonRunnerReloadFailureError(
                "PID file {pidfile.path!r} not locked".format(
                    pidfile=self._get_context().pidfile))
            raise error
        if is_pidfile_stale(self._get_context().pidfile):
            self._get_context().pidfile.break_lock()
        else:
            pid = self._get_context().pidfile.read_pid()
            os.kill(pid, signal.SIGUSR1)

    def _reload_configuration(self, _signal, _stack):
        """ Reload the configuration from the configuration file."""
        if self.args.device:
            self.MODBUS_DEVICE = self.args.device
            self.connection_lock = self.MODBUS_DEVICE[self.MODBUS_DEVICE.rindex('/')+1:]

        self.read_config_file()
        self.set_logging_level()
        if 'modbus' in self.cfg:
            if isinstance(self.cfg['modbus']['timeout'], int):
                self.MODBUS_TIMEOUT = self.cfg['modbus']['timeout']
            if isinstance(self.cfg['modbus']['baudrate'], int):
                self.MODBUS_BAUDRATE = self.cfg['modbus']['baudrate']
            if isinstance(self.cfg['modbus']['unit'], int):
                self.MODBUS_UNIT = self.cfg['modbus']['unit']
            if isinstance(self.cfg['modbus']['device'], str):
                self.MODBUS_DEVICE = self.cfg['modbus']['device']
                self.connection_lock = self.MODBUS_DEVICE[self.MODBUS_DEVICE.rindex('/')+1:]
        if self.MODBUS_TIMEOUT is None:
            self.MODBUS_TIMEOUT = DEFAULT_MODBUS_TIMEOUT
        if self.MODBUS_BAUDRATE is None:
            self.MODBUS_BAUDRATE = DEFAULT_MODBUS_BAUDRATE
        if self.MODBUS_UNIT is None:
            self.MODBUS_UNIT = DEFAULT_MODBUS_UNIT

        # --------------------------------------------------------------------------- #
        # check mandatory configuration variables
        # --------------------------------------------------------------------------- #
        if self.MODBUS_DEVICE is None:
            raise ValueError('Modbus device not set')
        self.shall_create_boiler = True
        self.mqtt_started = False

        mqttk = self.cfg.get('mqtt', None)
        self.ha_discovery = self.args.mqtt_ha_discovery if self.mqtt_ha_discovery_explicit else mqttk.get('discovery', False) if mqttk is not None else False
        self.shall_run_discovery = True if self.ha_discovery is not None else False
        self.mqtt_topic = self.args.mqtt_topic if self.mqtt_topic_explicit else mqttk.get('topic', 'diematic2mqtt/boiler') if mqttk is not None else 'diematic2mqtt/boiler'
        self.mqtt_topic_available = f'{self.mqtt_topic}/availability'

        self.mqtt_client()
        self.mqtt_connect()

    def _restart(self):
        """ Stop, then start. """
        self._stop()
        self._start()

    def mqtt_client(self):
        if not self.mqtt_started and ('mqtt_broker' in self.args or 'mqtt' in self.cfg):
            self.mqtt_started = True
            try:
                if self.mqtt_connected:
                    self.mqttc.disconnect()    
                self.mqtt_connected = False
                client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv5)
                self.mqttc = client
                client.on_connect = self.on_mqtt_connect
                client.on_disconnect = self.on_mqtt_disconnect
                client.on_message = self.on_mqtt_message
                client.will_set(self.mqtt_topic_available, 'offline', 1)
            except Exception as e:
                log.error('mqtt found in configuration file but connection raised the following error:',e)

    def mqtt_connect(self):
        if not self.mqtt_connecting:
            self.mqtt_connecting = True
            try:
                mqttk = self.cfg.get('mqtt', None)
                tls = self.mqtt_tls_explicit or (mqttk is not None and 'tls' in mqttk and mqttk.get('tls') is True)
                if tls:
                    self.mqttc.tls_set(tls_version=ssl.PROTOCOL_TLSv1_2)
                auth = 'mqtt_user' in self.args or (mqttk is not None and 'user' in mqttk)
                if auth:
                    user = self.args.mqtt_user if self.mqtt_user_explicit else mqttk.get('user', None) if mqttk is not None else None
                    password = self.args.mqtt_password if self.mqtt_password_explicit else mqttk.get('password', None) if mqttk is not None else None
                    if user is not None and password is not None:
                        self.mqttc.username_pw_set(user, password)
                broker = self.args.mqtt_broker if self.mqtt_broker_explicit else (mqttk.get('broker', None) if mqtt is not None else None)
                port = self.args.mqtt_port if self.mqtt_port_explicit else mqttk.get('port', 8883 if tls else 1883) if mqttk is not None else 8883 if tls else 1883
                connection = self.mqttc.connect(broker, port, 60, clean_start=True) if broker is not None and port is not None else 'Connection parameters are missing'
                if connection == mqtt.MQTTErrorCode.MQTT_ERR_SUCCESS:
                    self.mqttc.loop_start()
                    self.mqttc.publish(self.mqtt_topic_available, 'online').wait_for_publish()
                else:
                    log.error(f'Can\'t connect to mqtt broker, error code is {connection}')
            except Exception as e:
                log.error('mqtt found in configuration file but connection raised the following error:',e)

    def on_mqtt_connect(self, client, userdata, flags, rc, properties):
        self.mqtt_connected = True
        self.mqtt_connecting = False
        log.info('MQTT Connected successfully')

    def on_mqtt_disconnect(self, client, userdata, flags, rc, properties):
        self.mqtt_connected = False
        log.info('MQTT Disconncted!')

    def on_mqtt_message(self, client, userdata, msg: mqtt.MQTTMessage):
        log.info(f'Message: userdata:{userdata} topic:{msg.topic} payload:{str(msg.payload)} retained:{msg.retain}')
        topic_parts = msg.topic.split('/')
        if len(topic_parts) > 5 and topic_parts[4] == 'set':
            varname = topic_parts[5]
            value = self.parse_payload(msg.payload)
            if value is not None:
                def callback():
                    # This is generating a process lock
                    # self.MyBoiler.browse_registers()
                    # data = self.MyBoiler.fetch_data()
                    # mqtt_json_body = json.dumps(data, indent=2)
                    # self.mqttc.publish(self.mqtt_topic, mqtt_json_body).wait_for_publish()
                    return
                self.MyBoiler.set_write_pending(varname, value, callback)
                self.check_pending_writes()

    def parse_payload(self, payload):
        # json.loads(payload.decode('utf8'))
        if type(payload) is bytes:
            decoded = payload.decode('utf8')
            if len(decoded) > 0:
                return json.loads(decoded)
            return None
        log.error(f'unkown value type for payload {payload}')
        return None

    def _test(self):
        """ For testing purposes only, not documented."""
        self._reload_configuration(None,None)
        self._create_boiler()
        testValues = (0x28, 0x8001, 0xc8)
        for testValue in testValues:
            test1 = self.MyBoiler._decode_decimal(testValue, 1)
            test2 = self.MyBoiler._encode_decimal(test1, 1)
            if testValue == test2:
                emit_message('test PASS')
            else:
                emit_message('test FAIL')

    """Action functions dictionary."""
    action_funcs = {
        'start': _start,
        'stop': _stop,
        'restart': _restart,
        'status': _status,
        'reload': _reload,
        'runonce': _runonce,
        'readregister': _readregister,
        'writeregister': _writeregister,
    }

    def _get_action_func(self):
        """ Get the function for the specified action.

            :return: The function object corresponding to the specified
                action.
            :raises DaemonRunnerInvalidActionError: if the action is
               unknown.

            The action is specified by the `action` attribute, which is set
            during `parse_args`.
            """
        try:
            func = self.action_funcs[self.action]
        except KeyError:
            error = DaemonRunnerInvalidActionError(
                    "Unknown action: {action!r}".format(
                        action=self.action))
            raise error
        return func

    def do_action(self):
        """ Perform the requested action.

            :return: ``None``.

            The action is specified by the `action` attribute, which is set
            during `parse_args`.
            """
        func = self._get_action_func()
        try:
            func(self)
        except DaemonRunnerError as e:
            log.error("{action!r} error: {errmsg}".format(action=self.action, errmsg=e))

def ActionType(value):
    try:
        DiematicApp.action_funcs[value]
    except KeyError:
        error = DaemonRunnerInvalidActionError(
                "Unknown action: {action!r}".format(action=value))
        raise error
    return value

def _usage_exit(parser):
    """ Emit a usage message, then exit.

        :param argv: The command-line arguments used to invoke the
            program, as a sequence of strings.
        :return: ``None``.
        """
    emit_message(parser.usage)
    # progname = os.path.basename(argv[0])
    usage_exit_code = 2
    # action_usage = "|".join(self.action_funcs.keys())
    # message = "usage: {progname} {usage}".format(
    #         progname=progname, usage=action_usage)
    # emit_message(message)
    sys.exit(usage_exit_code)

def parse_args(app, argv=None):
    """ Parse command-line arguments.

        :param argv: The command-line arguments used to invoke the
            program, as a sequence of strings.

        :return: ``None``.

        The parser expects the first argument as the program name, the
        second argument as the action to perform.

        If the parser fails to parse the arguments, emit a usage
        message and exit the program.
        """
    if argv is None:
        argv = sys.argv[1:]

    # --------------------------------------------------------------------------- #
    # retrieve command line arguments
    # --------------------------------------------------------------------------- #
    parser = argparse.ArgumentParser(
        description="Send data from Diematic boiler to web, influx database or mqtt broker",
        epilog="Developed by Ignacio Hernández-Ros and distributed under the MIT license",
        usage='%(prog)s [options]'
    )
    parser.add_argument(dest='action', choices=['status', 'start', 'stop', 'restart', 'reload', 'runonce', 'readregister', 'writeregister'], default="runonce", help="action to take", type=ActionType)
    parser.add_argument("-b", "--backend", choices=['none', 'configured', 'influxdb', 'mqtt'], default='configured', help="select data backend (default is any configured in the configuration file)")
    parser.add_argument("-d", "--device", help="define modbus device")
    parser.add_argument("-f", "--foreground", help="Run in the foreground do not detach process", action="store_true")
    parser.add_argument("-l", "--logging", choices=['critical', 'error', 'warning', 'info', 'debug'], help="define logging level (default is critical)")
    parser.add_argument("-c", "--config", default='/etc/diematic/diematic.yaml', help="alternate configuration file")
    parser.add_argument("-w", "--hostname", default="0.0.0.0", help="web server host name, defaults to 0.0.0.0")
    parser.add_argument("-p", "--port", default=8080, help="web server port, defaults to 8080", type=int)
    parser.add_argument("-s", "--server", choices=['loop','web','both'], default='both', help="servers to start")
    parser.add_argument("-a", "--address", default=0, help="register address to read whe action is readregister", type=int)
    parser.add_argument("-t", "--format", default='Raw', help="value format to apply for register read, default is Raw", choices=['Raw', 'DiematicOneDecimal', 'DiematicModeFlag', 'ErrorCode', 'DiematicCircType', 'DiematicProgram', 'Model', 'bit0', 'bit1', 'bit2', 'bit3', 'bit4', 'bit5', 'bit6', 'bit7', 'bit8', 'bit9', 'bitA', 'bitB', 'bitC', 'bitD', 'bitE', 'bitF'])
    parser.add_argument("--influxdb-host", help="InfluxDB host name", type=str)
    parser.add_argument("--influxdb-port", help="InfluxDB port", type=str)
    parser.add_argument("--influxdb-user", help="InfluxDB user name", type=str)
    parser.add_argument("--influxdb-password", help="InfluxDB user password", type=str)
    parser.add_argument("--influxdb-database", help="InfluxDB database", type=str)
    parser.add_argument("--mqtt-broker", help="MQTT Broker server, hostname or ip address", type=str)
    parser.add_argument("--mqtt-port", help="MQTT Broker server, port", type=str)
    parser.add_argument("--mqtt-tls", help="Use tls to connect to mqtt broker", action='store_true')
    parser.add_argument("--mqtt-user", help="MQTT user name", type=str)
    parser.add_argument("--mqtt-password", help="MQTT user password", type=str)
    parser.add_argument("--mqtt-topic", help="Topic where the values will be published in mqtt broker", default="diematic2mqtt/boiler", type=str)
    parser.add_argument("--mqtt-ha-discovery", help="if set, the service will publish Home Assistant Discovery topics", action='store_true')
    parser.add_argument("--mqtt-ha-discovery-prefix", help="Home assistant topic prefix", default="homeassistant", type=str)
    parser.add_argument("--mqtt-retain", help="set this parameter to retain messages in the broker, default is false", action='store_true')

    if len(argv) < 1:
        _usage_exit(parser)

    app.args = parser.parse_args(argv)

    app.hostname_explicit = '--hostname' in argv or "-w" in argv
    app.port_explicit = '--port' in argv or "-p" in argv

    app.mqtt_broker_explicit = '--mqtt-broker' in argv
    app.mqtt_tls_explicit = '--mqtt-tls' in argv
    app.mqtt_port_explicit = '--mqtt-port' in argv
    app.mqtt_ha_discovery_explicit = '--mqtt-ha-discovery' in argv
    app.mqtt_ha_discovery_prefix_explicit = '--mqtt-ha-discovery-prefix' in argv
    app.mqtt_retain_explicit = '--mqtt-retain' in argv
    app.mqtt_topic_explicit = '--mqtt-topic' in argv
    app.mqtt_user_explicit = '--mqtt-user' in argv
    app.mqtt_password_explicit = '--mqtt-password' in argv

    app.influxdb_host_explicit = '--influxdb-host' in argv
    app.influxdb_port_explicit = '--influxdb-port' in argv
    app.influxdb_user_explicit = '--influxdb-user' in argv
    app.influxdb_password_explicit = '--influxdb-password' in argv
    app.influxdb_database_explicit = '--influxdb-database' in argv

    app.mqtt_connected = False
    app.mqtt_connecting = False

    app.action = app.args.action
    if app.args.action == 'runonce':
        if not ('-b' in argv or '--backend' in argv):
            app.args.backend = 'none'
        if not ('-l' in argv or '--loggin' in argv):
            app.args.logging = 'info'

    if app.args.action == 'readregister' or app.args.action == 'writeregister':
        app.args.backend = 'none'

    if app.action not in app.action_funcs:
        _usage_exit(parser)

def emit_message(message, stream=None):
    """ Emit a message to the specified stream (default `sys.stderr`). """
    if stream is None:
        stream = sys.stderr
    stream.write("{message}\n".format(message=message))
    stream.flush()

def make_pidlockfile(path, acquire_timeout):
    """ Make a PIDLockFile instance with the given filesystem path. """
    if not isinstance(path, str):
        error = ValueError("Not a filesystem path: {path!r}".format(
                path=path))
        raise error
    if not os.path.isabs(path):
        error = ValueError("Not an absolute path: {path!r}".format(
                path=path))
        raise error
    lockfile = pidfile.TimeoutPIDLockFile(path, acquire_timeout)

    return lockfile

def is_pidfile_stale(pidfile):
    """ Determine whether a PID file is stale.

        :return: ``True`` if the PID file is stale; otherwise ``False``.

        The PID file is “stale” if its contents are valid but do not
        match the PID of a currently-running process.
        """
    result = False

    pidfile_pid = pidfile.read_pid()
    if pidfile_pid is not None:
        try:
            os.kill(pidfile_pid, signal.SIG_DFL)
        except ProcessLookupError:
            # The specified PID does not exist.
            result = True

    return result

def is_process_already_running(pidfile):
    """ Determine if the process indicated by the pidfile is already
        running.
        :return: ``True`` if the process pointed to by the pid file is running
        """
    result = False
    pidfile_pid = pidfile.read_pid()
    if pidfile_pid is not None:
        try:
            os.kill(pidfile_pid, signal.SIG_DFL)
            result = True
        except ProcessLookupError:
            # The specified PID does not exist.
            pass

    return result

if __name__ == '__main__':
    app = DiematicApp()
    parse_args(app)
    app.do_action()
