""" 
Diematic daemon.

(C) 2022 by Ignacio Hernández-Ros
Based on a previous work from Germain Masse 2019

See and respect licensing terms

"""
import logging
import yaml
import os
import signal
import time
import threading
import argparse
import sys
import concurrent.futures

from lockfile import pidlockfile
from boiler import Boiler 
from pymodbus.client.sync import ModbusSerialClient as ModbusClient
from influxdb import InfluxDBClient
from influxdb.exceptions import InfluxDBClientError
from daemon import DaemonContext
from daemon import pidfile
from http.server import ThreadingHTTPServer
from webserver import MakeDiematicWebRequestHandler

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

        self.first_run = True

        self.connection_semaphore = threading.Semaphore()

        return

    def _get_context(self):
        """ Returns or create and retuen the self.context that is used by the surrounding daemon """
        try:
            return self.context
        except AttributeError:
            self.context = DaemonContext(
                pidfile=pidlockfile.PIDLockFile('/var/run/diematic/diematicd.pid'),
                working_directory="/etc/diematic"
                )

            self.context.signal_map = {
                signal.SIGTERM: self._terminate_daemon_process,
                signal.SIGHUP: self._terminate_daemon_process,
                signal.SIGUSR1: self._reload_configuration,
                }
            self.context.app = self

            self.pidfile_timeout = 3
            # self._open_streams_from_app_stream_paths()
            return self.context

    def _get_executor(self):
        """ create the executor pool or return if already created """
        try:
            return self.executor
        except AttributeError:
            self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            return self.executor

    def _value_writer(self):
        """ consumes a job writing to the boiler """
        write = self.MyBoiler.next_write()
        tryCount = 0
        while not write is None and 'name' in write:
            paramName = write['name']
            winfo = self.MyBoiler.prepare_write(write)
            address = winfo['address']
            newvalue = winfo['value']
            log.info("Pending write {register} address {address} newvalue {newvalue}".format(register=paramName, address=address, newvalue=newvalue))
            if tryCount > 5:
                # too many attemnts to write a value
                self.MyBoiler.write_error(paramName, "write operation failed, too many attempts to write parameter {parameterName} value {wvalue} in address {address}".format(parameterName=paramName, wvalue=newvalue, address=address))
                return
            try:
                self.connection_semaphore.acquire()
                log.info("Connection adquired")
                try:
                    with ModbusClient(method='rtu', port=self.MODBUS_DEVICE, timeout=self.MODBUS_TIMEOUT, baudrate=self.MODBUS_BAUDRATE) as client:
                        client.connect()
                        try:
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
                                self.MyBoiler.write_error(paramName, "write operation success, but read value differs write value {wvalue} read value {rvalue}".format(wvalue=newvalue, rvalue=receivedvalue))
                            else:
                                self.MyBoiler.write_ok(paramName)
                        finally:
                            log.info("Closing connection")
                            client.close()
                    write = self.MyBoiler.next_write()
                finally:
                    log.info("Releasing semaphore")
                    self.connection_semaphore.release()
            except DiematicModbusError as error:
                tryCount += 1
                self.MyBoiler.write_error(paramName, "write operation failed, {errormessage}".format(errormessage=error))
                log.info("Repeat in one second")
                time.sleep(1)
                pass

    def run(self):
        self._reload_configuration(None,None)

        if self.args.server == 'both' or self.args.server == 'web':
            self.startWebServer()
        
        if self.args.server == 'both' or self.args.server == 'loop':
            while(True):
                self.do_main_program()
                time.sleep(60) # a minute

    def startWebServer(self):
        self._create_boiler()
        self.webServer = ThreadingHTTPServer((self.args.hostname, self.args.port), MakeDiematicWebRequestHandler(self))
        x = threading.Thread(target=self.startWebServerInThread)
        x.setDaemon(True)
        x.start()
        if self.args.server == 'web':
            x.join()

    def startWebServerInThread(self):
        self.webServer.serve_forever()

    def check_pending_writes(self):
        self._get_executor().submit(self._value_writer)

    def _create_boiler(self):
        if self.shall_create_boiler:
            self.MyBoiler = Boiler(index=self.cfg['registers'])
            self.shall_create_boiler = False

    def do_main_program(self):
        self._create_boiler()
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
        self.connection_semaphore.acquire()
        try:
            with ModbusClient(method='rtu', port=self.MODBUS_DEVICE, timeout=self.MODBUS_TIMEOUT, baudrate=self.MODBUS_BAUDRATE) as client:
                client.connect()
                try:
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
                finally:
                    client.close()
        finally:
            self.connection_semaphore.release()

        #parsing registers to push data in Object attributes
        self.MyBoiler.browse_registers()
        log.info("Dumping values\n" + self.MyBoiler.dump())


        #pushing data to influxdb
        if self.args.backend and self.args.backend == 'influxdb':
            timestamp = int(time.time() * 1000) #milliseconds
            influx_json_body = [
            {
                "measurement": "diematic",
                "tags": {
                    "host": "raspberrypi",
                },
                "timestamp": timestamp,
                "fields": self.MyBoiler.fetch_data() 
            }
            ]

            influx_client = InfluxDBClient(self.cfg['influxdb']['host'], self.cfg['influxdb']['port'], self.cfg['influxdb']['user'], self.cfg['influxdb']['password'], self.cfg['influxdb']['database'])

            log.debug("Write points: {0}".format(influx_json_body))
            try:
                influx_client.write_points(influx_json_body, time_precision='ms')
            except InfluxDBClientError as e:
                log.error(e)

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
        self.do_main_program()

    def _readregister(self):
        """ Read the content of the indicated register and shows the
            value. Does not need to exist in the yaml file
        """
        address = self.args.address
        format = self.args.format
        self._reload_configuration(None,None)
        self._create_boiler()
        tryCount = 0
        self.connection_semaphore.acquire()
        try:
            while tryCount < 5:
                registers = []
                tryCount += 1
                try:
                    with ModbusClient(method='rtu', port=self.MODBUS_DEVICE, timeout=self.MODBUS_TIMEOUT, baudrate=self.MODBUS_BAUDRATE) as client:
                        client.connect()
                        try:
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
                        finally:
                            client.close()
                except DiematicModbusError:
                    time.sleep(1)
                    pass
        finally:
            self.connection_semaphore.release()        

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
        """ Send a SIGUSR1 to the running process so it is forced to reload configuration file.
            """
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
        """ Reload the configuration from the configuration file
        _signal and _stack are required because this function is a signal handler
        """
        if self.args.device:
            self.MODBUS_DEVICE = self.args.device

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

    def _restart(self):
        """ Stop, then start. """
        self._stop()
        self._start()

    def _test(self):
        """
          For testing purposes only, not documented
        """
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

    action_funcs = {
            'start': _start,
            'stop': _stop,
            'restart': _restart,
            'status': _status,
            'reload': _reload,
            'runonce': _runonce,
            'readregister': _readregister
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
    parser = argparse.ArgumentParser()
    parser.add_argument(dest='action', choices=['status', 'start', 'stop', 'restart', 'reload', 'runonce', 'readregister'], default="runonce", help="action to take", type=ActionType)
    parser.add_argument("-b", "--backend", choices=['none', 'influxdb'], default='influxdb', help="select data backend (default is influxdb)")
    parser.add_argument("-d", "--device", help="define modbus device")
    parser.add_argument("-f", "--foreground", help="Run in the foreground do not detach process", action="store_true")
    parser.add_argument("-l", "--logging", choices=['critical', 'error', 'warning', 'info', 'debug'], help="define logging level (default is critical)")
    parser.add_argument("-c", "--config", default='/etc/diematic/diematic.yaml', help="alternate configuration file")
    parser.add_argument("-w", "--hostname", default="0.0.0.0", help="web server host name, defaults to 0.0.0.0")
    parser.add_argument("-p", "--port", default=8080, help="web server port, defaults to 8080", type=int)
    parser.add_argument("-s", "--server", choices=['loop','web','both'], default='both', help="servers to start")
    parser.add_argument("-a", "--address", default=0, help="register address to read whe action is readregister", type=int)
    parser.add_argument("-t", "--format", default='Raw', help="value format to apply for register read, default is Raw", choices=['Raw', 'DiematicOneDecimal', 'DiematicModeFlag', 'ErrorCode', 'DiematicCircType', 'DiematicProgram', 'bit0', 'bit1', 'bit2', 'bit3', 'bit4', 'bit5', 'bit6', 'bit7', 'bit8', 'bit9', 'bitA', 'bitB', 'bitC', 'bitD', 'bitE', 'bitF'])

    if len(argv) < 1:
        _usage_exit(parser, argv)

    app.args = parser.parse_args(argv)

    app.action = app.args.action
    if app.args.action == 'runonce':
        if not ('-b' in argv or 'backend' in argv):
            app.args.backend = 'none'
        if not ('-l' in argv or 'loggin' in argv):
            app.args.logging = 'info'

    if app.args.action == 'readregister':
        app.args.backend = 'none'

    # self.action = str(argv[1])
    if app.action not in app.action_funcs:
        _usage_exit(parser, argv)

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
