# Parse gcode commands
#
# Copyright (C) 2016-2021  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
# Test-only excerpts from the captured K2 Plus gcode.py; methods are unmodified.
# Capture: build/file-checks/live-gcode.py, SHA256 fa0d037602a58d40d8694422ade76613961f2a1b6a71f61668b76a59c34680d5
import re
import shlex
import warnings

class CommandWarning(Warning):
    @classmethod
    def warn(cls, message: str, stacklevel: int = 2):
        warnings.warn(message, cls, stacklevel=stacklevel)

class CommandError(Exception):
    pass

class GCodeCommand:
    error = CommandError
    warning = CommandWarning.warn
    def __init__(self, gcode, command, commandline, params, need_ack):
        self._command = command
        self._commandline = commandline
        self._params = params
        self._need_ack = need_ack
        # Method wrappers
        self.respond_info = gcode.respond_info
        self.respond_raw = gcode.respond_raw
        self.check_cancel_running = gcode.check_cancel_running
    def get_commandline(self):
        return self._commandline
    class sentinel: pass
    def get(self, name, default=sentinel, parser=str, minval=None, maxval=None,
            above=None, below=None):
        value = self._params.get(name)
        if value is None:
            if default is self.sentinel:
                raise self.error("""{"code":"key251", "msg":"Error on '%s': missing %s", "values":["%s",%s"]}"""
                                 % (self._commandline, name, self._commandline, name))
            return default
        try:
            value = parser(value)
        except:
            return self.warning(
                             """{"code":"key171", "msg": "Unable to parse '%s' as a  %s", "values": ["%s", "%s"]}""" 
                             % (self._commandline, value, self._commandline, value))
        if minval is not None and value < minval:
            return self.warning("""{"code":"key252","msg":"Error on '%s': %s must have minimum of %s","values":["%s","%s","%s"]}"""
                             % (self._commandline, name, minval, self._commandline, name, minval))
        if maxval is not None and value > maxval:
            return self.warning("""{"code":"key253", "msg":"Error on '%s': %s must have maximumof %s", "values":["%s","%s","%s"]}"""
                             % (self._commandline, name, maxval, self._commandline, name, maxval))
        if above is not None and value <= above:
            raise self.error("""{"code":"key254", "msg":"Error on '%s': %s must be above %s", "values":["%s","%s","%s"]}"""
                             % (self._commandline, name, above, self._commandline, name, above))
        if below is not None and value >= below:
            raise self.error("""{"code":"key255", "msg":"Error on '%s': %s must be below %s", "values":["%s","%s","%s"]}"""
                             % (self._commandline, name, below, self._commandline, name, below))
        return value
    def get_float(self, name, default=sentinel, minval=None, maxval=None,
                  above=None, below=None):
        return self.get(name, default, parser=float, minval=minval,
                        maxval=maxval, above=above, below=below)

class GCodeDispatch:
    error = CommandError
    def is_traditional_gcode(self, cmd):
        # A "traditional" g-code command is a letter and followed by a number
        try:
            cmd = cmd.upper().split()[0]
            val = float(cmd[1:])
            return cmd[0].isupper() and cmd[1].isdigit()
        except:
            return False
    def register_command(self, cmd, func, when_not_ready=False, desc=None):
        if func is None:
            old_cmd = self.ready_gcode_handlers.get(cmd)
            if cmd in self.ready_gcode_handlers:
                del self.ready_gcode_handlers[cmd]
            if cmd in self.base_gcode_handlers:
                del self.base_gcode_handlers[cmd]
            return old_cmd
        if cmd in self.ready_gcode_handlers:
            raise self.printer.config_error(
                """{"code":"key57", "msg":"gcode command %s already registered", "values": ["%s"]}""" % (cmd, cmd))
        if not self.is_traditional_gcode(cmd):
            origfunc = func
            func = lambda params: origfunc(self._get_extended_params(params))
        self.ready_gcode_handlers[cmd] = func
        if when_not_ready:
            self.base_gcode_handlers[cmd] = func
        if desc is not None:
            self.gcode_help[cmd] = desc
    extended_r = re.compile(
        r'^\s*(?:N[0-9]+\s*)?'
        r'(?P<cmd>[a-zA-Z_][a-zA-Z0-9_]*)(?:\s+|$)'
        r'(?P<args>[^#*;]*?)'
        r'\s*(?:[#*;].*)?$')
    extended_r1 = re.compile(
        r'^\s*(?:N[0-9]+\s*)?'
        r'(?P<cmd>[a-zA-Z_][a-zA-Z0-9_]+)(?:\s+|$)'
        r'(?P<args>.*?)'
        r'\s*$')
    def _get_extended_params(self, gcmd):
        if gcmd.get_commandline().startswith("SDCARD_PRINT_FILE") | gcmd.get_commandline().startswith("BED_MESH_CALIBRATE"):
            # Support filename contain '#'
            m = self.extended_r1.match(gcmd.get_commandline())
        else:
            m = self.extended_r.match(gcmd.get_commandline())
        if m is None:
            raise self.error("""{"code":"key513", "msg": "Malformed command '%s'", "values": ["%s"]}""" % (gcmd.get_commandline(), gcmd.get_commandline()))
        eargs = m.group('args')
        try:
            eparams = [earg.split('=', 1) for earg in shlex.split(eargs)]
            eparams = { k.upper(): v for k, v in eparams }
            gcmd._params.clear()
            gcmd._params.update(eparams)
            return gcmd
        except ValueError as e:
            raise self.error("""{"code":"key514", "msg": "Malformed command args '%s'", "values": ["%s"]}""" % (gcmd.get_commandline(), str(e)))

