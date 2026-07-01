#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# flatpack2 v2.9.4 - 2026
#
# Copyright (c) 2026 mti@mti.sk
# Coded by Claude Sonnet 4.6
#
# MIT License - see LICENSE file for details
# https://github.com/mti-sk/flatpack2
"""
flatpack2 v2.9.4 - CLI + Web-GUI controller for Eltek Flatpack2 PSU via CAN bus
Session state: charger implemented but untested on hardware (first priority next session).
See README.md for full documentation, API reference and known issues.

Supports Waveshare USB-CAN-A adapter (STM32, CH341, native binary protocol).
Autodetects adapter by USB VID:PID 1a86:7523 (CH341 chip).

Protocol confirmed by hardware testing:
  LOGIN TX : 0x05004804  every 1 second (keepalive)
  STATUS RX: (arb & 0xFFFFFF00) == 0x05014000
  SET TX   : 0x05FF4004  (broadcast - only this works)
  ALERT TX : 0x0501BFFC

Features:
  - Virtual PTY terminal (connect via: screen /tmp/flatpack2.pty)
  - Daemonization with PID file
  - CAN watchdog with auto-reconnect
  - Waveshare adapter autodetection
  - Per-PSU startup configuration
  - Log rotation
  - systemd support (SIGTERM/SIGHUP)
  - Web-GUI dashboard (Flask, SSE, mobile-first)
"""

import sys
import os
import time
import struct
import logging
import logging.handlers
import threading
import configparser
import signal
import argparse
import glob
import pty
import tty
import termios
import select
import fcntl
import array

try:
    import serial
except ImportError:
    print("ERROR: pyserial not installed. Run: pip install pyserial")
    sys.exit(1)

try:
    from flask import Flask, Response, jsonify, request, stream_with_context
    import json as _json
except ImportError:
    print("ERROR: flask not installed. Run: pip install flask")
    sys.exit(1)

import collections
import datetime

# ---------------------------------------------------------------------------
# Version
# ---------------------------------------------------------------------------
VERSION = "2.9.4"

# ---------------------------------------------------------------------------
# Flatpack2 hardware limits (48V family)
# ---------------------------------------------------------------------------
# Voltage range and OVP ceiling are fixed by the 48V Flatpack2 platform and do
# NOT depend on the power variant (only the internal power/current modules
# differ between the 1800W / 2000W / 3000W HE variants).
PSU_V_MIN = 43.5
PSU_V_MAX = 57.6
PSU_V_NOM = 48.0          # nominal voltage used to derive I_MAX from power_rating

# Power-dependent limits - set from [psu] power_rating at config load time
# (see load_config()). Defaults below correspond to the 2000W HE variant and
# are only used until a config is actually loaded.
PSU_POWER_VARIANTS = (1800.0, 2000.0, 3000.0)
PSU_P_MAX = 2000.0
PSU_I_MAX = round(PSU_P_MAX / PSU_V_NOM, 1)   # 41.7 A

# Standby mode: low-power idle (safe voltage, minimum current)
STANDBY_VOLTAGE = 48.0   # V
STANDBY_CURRENT = 0.1    # A (PSU minimum)

# ---------------------------------------------------------------------------
# Protocol constants (confirmed by hardware testing)
# ---------------------------------------------------------------------------
LOGIN_ARB      = 0x05004804   # XX=0x04 (ID*4, ID=1)
LOGIN_INTERVAL = 1.0          # seconds - PSU timeout is 15s

SET_ARB        = 0x05FF4004   # broadcast - only address that works

STATUS_MASK    = 0xFFFFFF00
STATUS_BASE    = 0x05014000   # (arb & STATUS_MASK) == STATUS_BASE

ALERT_ARB      = 0x0501BFFC
LOGIN_REQ_ARB  = 0x05014400

STATUS_CV      = 0x04
STATUS_CC      = 0x08
STATUS_ALARM   = 0x0C
STATUS_WALKIN  = 0x10
STATUS_NAMES   = {
    STATUS_CV:    "CV (Constant Voltage)",
    STATUS_CC:    "CC (Current Limit)",
    STATUS_ALARM: "ALARM",
    STATUS_WALKIN:"Walk-in (ramping)",
}

ALERT_BYTE1 = [
    "OVS Lock Out", "Mod Fail Primary", "Mod Fail Secondary",
    "High Mains", "Low Mains", "High Temp", "Low Temp", "Current Limit"
]
ALERT_BYTE2 = [
    "Internal Voltage", "Module Fail", "Mod Fail Secondary",
    "Fan 1 Speed Low", "Fan 2 Speed Low", "Sub Mod1 Fail",
    "Fan 3 Speed Low", "Inner Volt"
]

# Waveshare USB-CAN-A identification
WAVESHARE_USB_VID = "1a86"
WAVESHARE_USB_PID = "7523"

CANUSB_SPEED = {
    1000000: 0x01, 800000: 0x02, 500000: 0x03, 400000: 0x04,
    250000:  0x05, 200000: 0x06, 125000: 0x07, 100000: 0x08,
    50000:   0x09, 20000:  0x0A, 10000:  0x0B, 5000:   0x0C,
}

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
log = logging.getLogger("flatpack2")

def setup_logging(logfile, loglevel, max_bytes=10485760, backup_count=5):
    level = getattr(logging, loglevel.upper(), logging.INFO)
    log.setLevel(level)

    # Rotating file handler
    try:
        os.makedirs(os.path.dirname(logfile), exist_ok=True) if os.path.dirname(logfile) else None
        fh = logging.handlers.RotatingFileHandler(
            logfile, maxBytes=max_bytes, backupCount=backup_count)
        fh.setLevel(level)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(threadName)s: %(message)s"))
        log.addHandler(fh)
    except (OSError, PermissionError) as e:
        print("[flatpack2] WARNING: Cannot open log file {}: {}".format(logfile, e))

    # Console handler (suppressed in daemon mode)
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(level)
    ch.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    ch.set_name("console")
    log.addHandler(ch)

    return log

# ---------------------------------------------------------------------------
# Config loader with validation
# ---------------------------------------------------------------------------
def load_config(path="flatpack2.conf"):
    cfg = configparser.ConfigParser()
    # Defaults
    cfg.read_dict({
        "can": {
            "channel": "/dev/ttyUSB0",
            "bitrate": "125000",
            "serial_baudrate": "2000000",
            "autodetect": "true",
        },
        "logging": {
            "logfile": "flatpack2.log",
            "loglevel": "INFO",
            "max_bytes": "10485760",
            "backup_count": "5",
        },
        "watchdog": {
            "enabled": "true",
            "timeout": "30",
        },
        "daemon": {
            "enabled": "false",
            "pidfile": "/var/run/flatpack2.pid",
            "user": "",
            "group": "",
        },
        "terminal": {
            "type": "pty",
            "pty_symlink": "/tmp/flatpack2.pty",
        },
        "webgui": {
            "enabled": "true",
            "host": "0.0.0.0",
            "port": "8080",
            "log_access": "true",
        },
        "psu": {
            "ovp_voltage": "60.0",
            "discovery_timeout": "10",
            "power_rating": "2000",
        },
    })

    if os.path.exists(path):
        cfg.read(path)
        print("[flatpack2] Config loaded from {}".format(path))
    else:
        print("[flatpack2] Config '{}' not found - using defaults".format(path))

    _apply_power_rating(cfg)
    _validate_config(cfg)
    return cfg

def _apply_power_rating(cfg):
    """
    Read [psu] power_rating and derive PSU_P_MAX / PSU_I_MAX from it.
    Must run before _validate_config() and before anything else that checks
    PSU_I_MAX / PSU_P_MAX (cmd_set, charger limits, HTML limits, help text).
    Voltage range (PSU_V_MIN/PSU_V_MAX) is NOT affected - it is fixed by the
    48V Flatpack2 platform regardless of the power variant.
    """
    global PSU_P_MAX, PSU_I_MAX

    try:
        power_rating = cfg.getfloat("psu", "power_rating")
    except (ValueError, configparser.Error):
        print("[flatpack2] WARNING: invalid psu.power_rating - using default 2000W")
        power_rating = 2000.0

    if power_rating not in PSU_POWER_VARIANTS:
        print("[flatpack2] WARNING: psu.power_rating {:.0f}W is not a known "
              "Flatpack2 variant {} - using it anyway, but limits may be "
              "incorrect".format(power_rating, tuple(int(p) for p in PSU_POWER_VARIANTS)))

    PSU_P_MAX = power_rating
    PSU_I_MAX = round(power_rating / PSU_V_NOM, 1)
    print("[flatpack2] PSU power rating: {:.0f}W -> I_MAX={:.1f}A "
          "(V range unchanged: {:.1f}-{:.1f}V)".format(
              PSU_P_MAX, PSU_I_MAX, PSU_V_MIN, PSU_V_MAX))

def _validate_config(cfg):
    errors = []

    # Bitrate
    bitrate = cfg.getint("can", "bitrate")
    if bitrate not in CANUSB_SPEED:
        errors.append("can.bitrate {} not supported. Valid: {}".format(
            bitrate, sorted(CANUSB_SPEED.keys())))

    # Serial baudrate
    serial_baud = cfg.getint("can", "serial_baudrate")
    if serial_baud not in (9600, 19200, 38400, 115200, 1228800, 2000000):
        errors.append("can.serial_baudrate {} unusual (expected 2000000)".format(serial_baud))

    # OVP voltage
    ovp = cfg.getfloat("psu", "ovp_voltage")
    if ovp < PSU_V_MIN or ovp > 65.0:
        errors.append("psu.ovp_voltage {:.1f}V out of range ({:.1f}-65.0V)".format(ovp, PSU_V_MIN))

    # Discovery timeout
    dt = cfg.getfloat("psu", "discovery_timeout")
    if dt < 1 or dt > 120:
        errors.append("psu.discovery_timeout {:.0f}s out of range (1-120s)".format(dt))

    # Watchdog timeout
    wt = cfg.getfloat("watchdog", "timeout")
    if wt < 5:
        errors.append("watchdog.timeout {:.0f}s too short (min 5s)".format(wt))

    # PSU sections
    for section in cfg.sections():
        if not section.upper().startswith("PSU_"):
            continue
        try:
            v = cfg.getfloat(section, "voltage") if cfg.has_option(section, "voltage") else None
            i = cfg.getfloat(section, "current") if cfg.has_option(section, "current") else None
            if v is not None and not (PSU_V_MIN <= v <= PSU_V_MAX):
                errors.append("[{}] voltage {:.2f}V out of range ({}-{}V)".format(
                    section, v, PSU_V_MIN, PSU_V_MAX))
            if i is not None and not (0 < i <= PSU_I_MAX):
                errors.append("[{}] current {:.1f}A out of range (0-{}A)".format(
                    section, i, PSU_I_MAX))
            if v is not None and i is not None and v * i > PSU_P_MAX:
                errors.append("[{}] {:.0f}W exceeds max {:.0f}W".format(
                    section, v * i, PSU_P_MAX))
            psu_ovp = cfg.getfloat("psu", "ovp_voltage")
            if v is not None and v >= psu_ovp:
                errors.append("[{}] voltage {:.2f}V >= ovp_voltage {:.2f}V".format(
                    section, v, psu_ovp))
        except (ValueError, configparser.NoOptionError) as e:
            errors.append("[{}] invalid value: {}".format(section, e))

    if errors:
        print("[flatpack2] Config warnings:")
        for e in errors:
            print("  WARNING: {}".format(e))

def get_psu_configs(cfg):
    """Return dict: serial_hex (or None) -> {voltage, current, apply_on_start, section}"""
    configs = {}
    for section in cfg.sections():
        if not section.upper().startswith("PSU_"):
            continue
        try:
            idx = int(section.split("_", 1)[1])
        except (ValueError, IndexError):
            continue
        entry = {
            "index":          idx,
            "serial":         cfg.get(section, "serial", fallback="").strip().upper() or None,
            "voltage":        cfg.getfloat(section, "voltage", fallback=None),
            "current":        cfg.getfloat(section, "current", fallback=None),
            "apply_on_start": cfg.getboolean(section, "apply_on_start", fallback=False),
            "section":        section,
        }
        configs[idx] = entry
    return configs

# ---------------------------------------------------------------------------
# Waveshare adapter autodetection
# ---------------------------------------------------------------------------
def find_waveshare_ports():
    """Find Waveshare USB-CAN-A adapters by USB VID:PID (CH341: 1a86:7523)."""
    found = []
    for tty_path in sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*")):
        dev_name = os.path.basename(tty_path)
        # Check via sysfs
        for sysfs in glob.glob("/sys/bus/usb/devices/*/{}".format(dev_name)):
            vid_path = os.path.join(os.path.dirname(sysfs), "..", "idVendor")
            pid_path = os.path.join(os.path.dirname(sysfs), "..", "idProduct")
            try:
                vid = open(os.path.normpath(vid_path)).read().strip()
                pid = open(os.path.normpath(pid_path)).read().strip()
                if vid == WAVESHARE_USB_VID and pid == WAVESHARE_USB_PID:
                    if tty_path not in found:
                        found.append(tty_path)
                        log.info("Waveshare adapter found: {} (VID:{} PID:{})".format(
                            tty_path, vid, pid))
            except (OSError, IOError):
                continue
        # Fallback: check via /sys/class/tty
        sysfs_path = "/sys/class/tty/{}/device/..".format(dev_name)
        if os.path.exists(sysfs_path):
            for vid_file in glob.glob(os.path.join(sysfs_path, "../idVendor")):
                try:
                    vid = open(os.path.normpath(vid_file)).read().strip()
                    pid = open(os.path.normpath(
                        vid_file.replace("idVendor", "idProduct"))).read().strip()
                    if vid == WAVESHARE_USB_VID and pid == WAVESHARE_USB_PID:
                        if tty_path not in found:
                            found.append(tty_path)
                except (OSError, IOError):
                    continue
    return found

def detect_adapter(cfg):
    """
    Try config port first, then autodetect.
    Returns port string or None.
    """
    config_port = cfg.get("can", "channel")
    autodetect  = cfg.getboolean("can", "autodetect")

    # Try configured port first
    if os.path.exists(config_port):
        log.info("Using configured port: {}".format(config_port))
        return config_port
    else:
        log.warning("Configured port {} not found".format(config_port))

    if not autodetect:
        return None

    # Autodetect by USB VID:PID
    log.info("Autodetecting Waveshare USB-CAN-A adapter...")
    ports = find_waveshare_ports()
    if ports:
        log.info("Autodetected: {}".format(ports[0]))
        print("[flatpack2] Autodetected adapter: {}".format(ports[0]))
        if len(ports) > 1:
            print("[flatpack2] WARNING: Multiple adapters found: {}".format(ports))
            print("[flatpack2] Using first: {}".format(ports[0]))
        return ports[0]

    # Last resort: try any ttyUSB
    for port in sorted(glob.glob("/dev/ttyUSB*")):
        log.info("Trying fallback port: {}".format(port))
        return port

    return None

# ---------------------------------------------------------------------------
# PSU data model
# ---------------------------------------------------------------------------
class PSUState:
    def __init__(self, serial_bytes, psu_id):
        self.serial       = serial_bytes
        self.psu_id       = psu_id
        self.vout         = 0.0
        self.iout         = 0.0
        self.vin          = 0.0
        self.temp_in      = 0
        self.temp_out     = 0
        self.status       = 0
        self.last_seen    = time.time()
        self.last_login   = 0.0
        self.last_status  = 0.0
        self.warnings     = []
        self.alarms       = []
        self.set_voltage  = None
        self.set_current  = None
        self.last_set_voltage = None   # last successfully sent voltage (for reconnect restore)
        self.last_set_current = None   # last successfully sent current (for reconnect restore)
        self.start_applied= False  # apply_on_start already done

    @property
    def serial_hex(self):
        return self.serial.hex().upper()

    def status_name(self):
        return STATUS_NAMES.get(self.status, "UNKNOWN(0x{:02X})".format(self.status))

    def is_stable(self):
        """True if we have received at least one status packet recently and status is CV or CC."""
        return (self.last_status > 0 and
                self.status in (STATUS_CV, STATUS_CC) and
                time.time() - self.last_seen < 5.0)

    def __str__(self):
        lines = [
            "  ID      : {}".format(self.psu_id),
            "  Serial  : {}".format(self.serial_hex),
            "  Vout    : {:.2f} V".format(self.vout),
            "  Iout    : {:.1f} A".format(self.iout),
            "  Vin     : {:.0f} Vrms".format(self.vin),
            "  Temp in : {} C".format(self.temp_in),
            "  Temp out: {} C".format(self.temp_out),
            "  Status  : {}".format(self.status_name()),
        ]
        if self.set_voltage is not None:
            lines.append("  Set V   : {:.2f} V".format(self.set_voltage))
        if self.set_current is not None:
            lines.append("  Set I   : {:.1f} A".format(self.set_current))
        if self.warnings:
            lines.append("  Warnings: {}".format(", ".join(self.warnings)))
        if self.alarms:
            lines.append("  ALARMS  : {}".format(", ".join(self.alarms)))
        return "\n".join(lines) + "\n"

# ---------------------------------------------------------------------------
# Waveshare USB-CAN-A low-level driver
# ---------------------------------------------------------------------------
class WaveshareCANA:
    def __init__(self, port, can_bitrate=125000, serial_baudrate=2000000):
        self.port            = port
        self.can_bitrate     = can_bitrate
        self.serial_baudrate = serial_baudrate
        self.ser             = None
        self._rx_buf         = bytearray()

    @staticmethod
    def _checksum(data):
        return sum(data) & 0xFF

    def connect(self):
        if not os.path.exists(self.port):
            log.error("Port {} does not exist".format(self.port))
            return False
        try:
            self.ser = serial.Serial(
                self.port,
                baudrate=self.serial_baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_TWO,
                timeout=0.05,
            )
            log.info("Serial port {} opened at {} baud".format(
                self.port, self.serial_baudrate))
            return self._init_adapter()
        except serial.SerialException as e:
            log.error("Serial open failed: {}".format(e))
            return False

    def disconnect(self):
        if self.ser and self.ser.is_open:
            try:
                self.ser.close()
            except Exception:
                pass
        self.ser = None
        self._rx_buf.clear()

    def _init_adapter(self):
        speed_byte = CANUSB_SPEED.get(self.can_bitrate, 0x07)
        frame = bytearray(20)
        frame[0]  = 0xAA
        frame[1]  = 0x55
        frame[2]  = 0x12          # settings command
        frame[3]  = speed_byte
        frame[4]  = 0x02          # extended frame
        frame[13] = 0x00          # normal mode
        frame[14] = 0x01
        frame[19] = self._checksum(frame[2:19])
        try:
            self.ser.write(bytes(frame))
            time.sleep(0.2)
            log.info("Adapter initialized: {} bps extended normal".format(self.can_bitrate))
            return True
        except serial.SerialException as e:
            log.error("Adapter init failed: {}".format(e))
            return False

    def send_frame(self, arb_id, data):
        if not self.ser or not self.ser.is_open:
            return False
        data = bytes(data[:8])
        frame = bytearray([0xAA, 0xE0 | len(data)])
        for sh in [0, 8, 16, 24]:
            frame.append((arb_id >> sh) & 0xFF)
        frame.extend(data)
        frame.append(0x55)
        try:
            self.ser.write(bytes(frame))
            log.debug("TX  id=0x{:08X}  data={}".format(arb_id, data.hex()))
            return True
        except serial.SerialException as e:
            log.error("CAN send error: {}".format(e))
            return False

    def recv_frame(self, timeout=0.1):
        if not self.ser or not self.ser.is_open:
            return None
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                chunk = self.ser.read(128)
            except serial.SerialException as e:
                log.error("Serial read error: {}".format(e))
                raise

            if chunk:
                self._rx_buf.extend(chunk)

            while True:
                idx = self._rx_buf.find(0xAA)
                if idx == -1:
                    self._rx_buf.clear()
                    break
                if idx > 0:
                    del self._rx_buf[:idx]
                if len(self._rx_buf) < 3:
                    break

                tb = self._rx_buf[1]
                if not (tb & 0x80):
                    del self._rx_buf[0]
                    continue

                data_len = tb & 0x0F
                flen = 1 + 1 + 4 + data_len + 1
                if len(self._rx_buf) < flen:
                    break
                if self._rx_buf[flen - 1] != 0x55:
                    del self._rx_buf[0]
                    continue

                frame = bytes(self._rx_buf[:flen])
                del self._rx_buf[:flen]

                arb  = int.from_bytes(frame[2:6], 'little')
                data = bytes(frame[6:6 + data_len])
                log.debug("RX  id=0x{:08X}  data={}".format(arb, data.hex()))
                return (arb, data)

        return None

# ---------------------------------------------------------------------------
# Flatpack2 bus manager
# ---------------------------------------------------------------------------
class FlatpackBus:
    def __init__(self, cfg, psu_configs, terminal=None):
        self.cfg         = cfg
        self.psu_configs = psu_configs   # idx -> config dict
        self.terminal    = terminal      # Terminal instance for output
        self.ovp_voltage = cfg.getfloat("psu", "ovp_voltage")
        self.disc_timeout= cfg.getfloat("psu", "discovery_timeout")
        self.wd_enabled  = cfg.getboolean("watchdog", "enabled")
        self.wd_timeout  = cfg.getfloat("watchdog", "timeout")

        self.adapter     = None   # created in connect()
        self._port       = None

        self._tx_lock    = threading.Lock()
        self.psus        = {}     # serial_hex -> PSUState
        self._id_map     = {}     # psu_id -> serial_hex
        self._serial_map = {}     # serial_hex -> psu_id (reverse)
        self._next_id    = 1
        self._running    = False
        self._connected  = False
        self.charger     = None
        self.history     = None
        self.webgui      = None

    # ------------------------------------------------------------------
    # Output helper - writes to terminal or stdout
    # ------------------------------------------------------------------
    def _print(self, msg, async_msg=True):
        """Print to terminal. Always appends newline. async_msg=True redraws prompt after async output."""
        if self.terminal:
            self.terminal.write(msg + "\n", async_msg=async_msg)
        else:
            print(msg)
        # Direct write to web log buffer without dependency on logging handler
        if self.webgui is not None:
            import datetime as _dt
            ts = _dt.datetime.now().strftime("%H:%M:%S")
            self.webgui.add_log_line("{} {}".format(ts, msg.strip()))

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------
    def connect(self):
        port = detect_adapter(self.cfg)
        if not port:
            log.error("No CAN adapter found")
            return False
        self._port = port
        self.adapter = WaveshareCANA(
            port,
            self.cfg.getint("can", "bitrate"),
            self.cfg.getint("can", "serial_baudrate"),
        )
        result = self.adapter.connect()
        if result:
            self._connected = True
        return result
    def disconnect(self):
        self._running = False
        if self.adapter:
            self.adapter.disconnect()
        self._connected = False

    def _reconnect(self):
        log.warning("CAN bus error - attempting reconnect...")
        self._connected = False

        # Remember charger state before disconnect so we can resume if needed
        charger_was_active = (
            self.charger is not None and self.charger.state.is_active()
        )
        charger_phase_before = self.charger.state.phase if self.charger else None

        if self.adapter:
            self.adapter.disconnect()

        while self._running:
            time.sleep(5)
            log.info("Reconnecting...")
            # Re-detect port in case adapter was re-plugged
            port = detect_adapter(self.cfg)
            if not port:
                log.warning("No adapter found, retrying...")
                continue
            if port != self._port:
                log.info("Adapter moved to new port: {}".format(port))
                self._port = port
                self.adapter = WaveshareCANA(
                    port,
                    self.cfg.getint("can", "bitrate"),
                    self.cfg.getint("can", "serial_baudrate"),
                )
            if self.adapter.connect():
                self._connected = True
                log.info("Reconnected on {}".format(port))
                self._print("\n[flatpack2] CAN bus reconnected on {}".format(port))
                # Reset tracking so _startup_apply_loop will re-apply values.
                # last_set_voltage/current are intentionally preserved.
                for psu in list(self.psus.values()):
                    psu.last_login  = 0
                    psu.last_status = 0
                    psu.last_seen   = 0.0   # force is_stable() to wait for fresh STATUS
                    psu.start_applied = False
                # Immediately send login to all known PSUs so they respond quickly
                for psu in list(self.psus.values()):
                    self._login_psu(psu)
                # Wait for first STATUS (up to 10 s) then restore values directly here,
                # as a reliable fallback independent of _startup_apply_loop timing.
                threading.Thread(target=self._reconnect_restore,
                                 args=(charger_was_active, charger_phase_before),
                                 daemon=True, name="reconnect-restore").start()
                return

    def _reconnect_restore(self, charger_was_active=False, charger_phase_before=None):
        """
        Called in a new thread immediately after successful reconnect.
        Waits for each PSU to start sending STATUS again (up to 15 s),
        then re-applies the last known voltage/current.
        If charger was active before disconnect, resumes charging.
        """
        deadline = time.time() + 15.0
        pending = {shex: psu for shex, psu in self.psus.items()}

        log.info("Reconnect restore: waiting for {} PSU(s)".format(len(pending)))
        print("[reconnect-restore] waiting for {} PSU(s)".format(len(pending)), flush=True)

        while pending and time.time() < deadline and self._running:
            time.sleep(0.2)
            done = []
            for shex, psu in list(pending.items()):
                age_status = (time.time() - psu.last_status) if psu.last_status else 9999
                age_seen   = (time.time() - psu.last_seen)   if psu.last_seen   else 9999
                stable = psu.is_stable()
                log.info("Restore poll PSU{}: status=0x{:02X} last_status={:.1f}s ago "
                         "last_seen={:.1f}s ago stable={}".format(
                    psu.psu_id, psu.status, age_status, age_seen, stable))
                print("[reconnect-restore] PSU{}: 0x{:02X} status_age={:.1f}s seen_age={:.1f}s stable={}".format(
                    psu.psu_id, psu.status, age_status, age_seen, stable), flush=True)
                if not stable:
                    continue
                voltage = psu.last_set_voltage
                current = psu.last_set_current
                log.info("Reconnect restore PSU{}: last_set V={} I={}".format(
                    psu.psu_id, voltage, current))
                if voltage is None or current is None:
                    psu_cfg = self._get_psu_config(psu)
                    if psu_cfg and psu_cfg.get("apply_on_start") and \
                       psu_cfg.get("voltage") and psu_cfg.get("current"):
                        voltage = psu_cfg["voltage"]
                        current = psu_cfg["current"]
                        log.info("Reconnect restore PSU{}: fallback to config V={} I={}".format(
                            psu.psu_id, voltage, current))
                if voltage is not None and current is not None:
                    log.info("Reconnect restore PSU{} ({}): sending V={:.2f} I={:.1f}".format(
                        psu.psu_id, psu.serial_hex, voltage, current))
                    self._print("\n[flatpack2] Reconnect: restoring PSU {} -> "
                               "V={:.2f}V I={:.1f}A".format(psu.psu_id, voltage, current))
                    self.cmd_set(voltage, current)
                else:
                    log.warning("Reconnect restore PSU{}: nothing to restore "
                                "(no last_set and no apply_on_start config)".format(psu.psu_id))
                psu.start_applied = True
                done.append(shex)
            for shex in done:
                del pending[shex]

        if pending:
            log.warning("Reconnect restore: PSU(s) did not respond in 15s: {}".format(
                list(pending.keys())))

        # Resume charging if it was active before the disconnect
        if charger_was_active and self.charger is not None and self._running:
            if not self.charger.state.is_active():
                log.info("Reconnect: resuming charging (was in phase {})".format(
                    charger_phase_before))
                self._print("\n[flatpack2] Reconnect: resuming charging "
                           "(was in phase {})".format(charger_phase_before))
                psu_ids = list(self._id_map.keys())
                if psu_ids:
                    self.charger.start(psu_ids)

    # ------------------------------------------------------------------
    # Start/stop threads
    # ------------------------------------------------------------------
    def start(self):
        self._running = True
        threading.Thread(target=self._rx_loop,        daemon=True, name="rx").start()
        threading.Thread(target=self._keepalive_loop, daemon=True, name="keepalive").start()
        threading.Thread(target=self._watchdog_loop,  daemon=True, name="watchdog").start()
        threading.Thread(target=self._startup_apply_loop, daemon=True, name="startup").start()

    def stop(self):
        self._running = False

    # ------------------------------------------------------------------
    # TX
    # ------------------------------------------------------------------
    def _send(self, arb_id, data):
        if not self._connected:
            return False
        with self._tx_lock:
            return self.adapter.send_frame(arb_id, data)

    # ------------------------------------------------------------------
    # Login keepalive
    # ------------------------------------------------------------------
    def _login_psu(self, psu):
        data = psu.serial + b'\x00\x00'
        if self._send(LOGIN_ARB, data):
            psu.last_login = time.time()
            log.debug("Login sent: serial={}".format(psu.serial_hex))

    def _keepalive_loop(self):
        while self._running:
            time.sleep(0.2)
            if not self._connected:
                continue
            now = time.time()
            for psu in list(self.psus.values()):
                if now - psu.last_login >= LOGIN_INTERVAL:
                    self._login_psu(psu)

    # ------------------------------------------------------------------
    # Watchdog
    # ------------------------------------------------------------------
    def _watchdog_loop(self):
        if not self.wd_enabled:
            return
        # Give time for initial discovery
        time.sleep(self.disc_timeout + 5)
        while self._running:
            time.sleep(5)
            if not self._connected or not self.psus:
                continue
            now = time.time()
            for psu in list(self.psus.values()):
                if psu.last_status > 0 and now - psu.last_seen > self.wd_timeout:
                    log.warning("Watchdog: PSU {} no STATUS for {:.0f}s - reconnecting".format(
                        psu.serial_hex, now - psu.last_seen))
                    self._print("\n[flatpack2] Watchdog triggered - reconnecting CAN bus...")
                    self._reconnect()
                    break

    # ------------------------------------------------------------------
    # Startup apply loop
    # ------------------------------------------------------------------
    def _startup_apply_loop(self):
        """After discovery, wait for stable status then apply PSU configs.
        On reconnect, restore last-set values (user-commanded or from config)."""
        time.sleep(2)
        while self._running:
            time.sleep(0.5)
            for psu in list(self.psus.values()):
                if psu.start_applied:
                    continue
                # Wait for stable status before applying
                if not psu.is_stable():
                    continue

                # Decide what to apply:
                # 1. If user set values during this session, restore those.
                # 2. Otherwise fall back to config apply_on_start.
                if psu.last_set_voltage is not None and psu.last_set_current is not None:
                    voltage = psu.last_set_voltage
                    current = psu.last_set_current
                    log.info("Restoring last-set values for PSU {} ({}): V={:.2f} I={:.1f}".format(
                        psu.psu_id, psu.serial_hex, voltage, current))
                    self._print("\n[flatpack2] Restoring last-set values for PSU {}: "
                               "V={:.2f}V I={:.1f}A".format(psu.psu_id, voltage, current))
                    self.cmd_set(voltage, current)
                    psu.start_applied = True
                    continue

                # No previous values - use config
                psu_cfg = self._get_psu_config(psu)
                if psu_cfg is None:
                    psu.start_applied = True  # no config for this PSU
                    continue
                if not psu_cfg.get("apply_on_start", False):
                    psu.start_applied = True
                    continue
                voltage = psu_cfg.get("voltage")
                current = psu_cfg.get("current")
                if voltage is None or current is None:
                    psu.start_applied = True
                    continue

                log.info("Applying startup config for PSU {} ({}): V={:.2f} I={:.1f}".format(
                    psu.psu_id, psu.serial_hex, voltage, current))
                self._print("\n[flatpack2] Applying startup config for PSU {}: "
                           "V={:.2f}V I={:.1f}A".format(psu.psu_id, voltage, current))
                self.cmd_set(voltage, current)
                psu.start_applied = True

    def _get_psu_config(self, psu):
        """Find PSU config matching by serial or by discovery order."""
        # Match by serial number
        for idx, cfg_entry in self.psu_configs.items():
            if cfg_entry.get("serial") and cfg_entry["serial"] == psu.serial_hex:
                return cfg_entry
        # Match by discovery order (PSU_1 = psu_id 1, etc.)
        return self.psu_configs.get(psu.psu_id)

    # ------------------------------------------------------------------
    # RX loop
    # ------------------------------------------------------------------
    def _rx_loop(self):
        while self._running:
            if not self._connected:
                time.sleep(0.5)
                continue
            try:
                result = self.adapter.recv_frame(timeout=0.5)
            except serial.SerialException:
                log.error("Serial error in rx_loop - triggering reconnect")
                threading.Thread(target=self._reconnect, daemon=True).start()
                time.sleep(1)
                continue
            except Exception as e:
                log.error("Unexpected rx error: {}".format(e))
                time.sleep(0.5)
                continue

            if result is None:
                continue
            arb, data = result
            self._dispatch(arb, data)

    def _dispatch(self, arb, data):
        # Hello: 0x0500XXXX, data[0]=0x1B
        if (arb & 0xFFFF0000) == 0x05000000 and len(data) >= 7 and data[0] == 0x1B:
            self._handle_hello(data)
            return

        # Status: (arb & STATUS_MASK) == STATUS_BASE
        if (arb & STATUS_MASK) == STATUS_BASE:
            yy = arb & 0xFF
            if len(data) == 8:
                self._handle_status(yy, data)
            return

        # Login request
        if arb == LOGIN_REQ_ARB:
            log.debug("Login request received")
            for psu in list(self.psus.values()):
                self._login_psu(psu)
            return

        # Alert response
        if arb == ALERT_ARB:
            if len(data) >= 5 and data[0] == 0x0E:
                self._handle_alert(data)
            return

    def _handle_hello(self, data):
        serial_b   = data[1:7]
        serial_hex = serial_b.hex().upper()

        if serial_hex in self.psus:
            self._login_psu(self.psus[serial_hex])
            return

        psu_id = self._next_id
        if psu_id > 63:
            log.error("Max PSU count (63) reached")
            return
        self._next_id += 1

        psu = PSUState(serial_b, psu_id)
        self.psus[serial_hex]       = psu
        self._id_map[psu_id]        = serial_hex
        self._serial_map[serial_hex]= psu_id

        log.info("New PSU discovered: serial={} -> ID={}".format(serial_hex, psu_id))
        self._print("\n[flatpack2] PSU found: serial={} -> ID={}".format(
            serial_hex, psu_id))
        self._login_psu(psu)

    def _handle_status(self, yy, data):
        # Find PSU by serial - STATUS comes from single PSU after login
        # Map to correct PSU using _id_map[1] for single PSU setup
        # For multi-PSU: all share STATUS_BASE, differentiate by timing/serial
        if not self.psus:
            return

        # With single PSU this is straightforward
        # With multiple PSUs we use last_seen timing heuristic
        serial_hex = self._id_map.get(1)
        if not serial_hex and self.psus:
            serial_hex = next(iter(self.psus))
        if not serial_hex:
            return

        psu = self.psus.get(serial_hex)
        if not psu:
            return

        psu.temp_in  = data[0]
        psu.iout     = struct.unpack_from("<H", data, 1)[0] * 0.1
        psu.vout     = struct.unpack_from("<H", data, 3)[0] * 0.01
        psu.vin      = float(struct.unpack_from("<H", data, 5)[0])
        psu.temp_out = data[7]
        psu.status   = yy
        psu.last_seen    = time.time()
        psu.last_status  = time.time()

        log.info("Status PSU{}: Vout={:.2f}V Iout={:.1f}A {} start_applied={}".format(
            psu.psu_id, psu.vout, psu.iout, psu.status_name(), psu.start_applied))

        if yy in (STATUS_CC, STATUS_ALARM):
            alert_type = 0x04 if yy == STATUS_CC else 0x08
            self._send(ALERT_ARB, bytes([0x08, alert_type, 0x00]))

        if yy == STATUS_ALARM:
            self._print("\n[flatpack2] !! PSU ID={} ALARM!".format(psu.psu_id))
            log.warning("PSU ID={} ALARM".format(psu.psu_id))

        # Feed history ring buffer
        if self.history is not None:
            ah = self.charger.state.ah if self.charger is not None else 0.0
            wh = self.charger.state.wh if self.charger is not None else 0.0
            self.history.append(psu.vout, psu.iout, ah, wh)

        # Notify charger
        if self.charger is not None:
            self.charger.on_status(psu)

    def _handle_alert(self, data):
        if not self.psus:
            return
        serial_hex = self._id_map.get(1) or next(iter(self.psus), None)
        psu = self.psus.get(serial_hex) if serial_hex else None
        if not psu:
            return

        alert_type = data[1]
        b1 = data[3] if len(data) > 3 else 0
        b2 = data[4] if len(data) > 4 else 0
        active = []
        for bit in range(8):
            if b1 & (1 << bit): active.append(ALERT_BYTE1[bit])
            if b2 & (1 << bit): active.append(ALERT_BYTE2[bit])

        if alert_type == 0x04:
            psu.warnings = active
            if active:
                msg = "PSU ID={} WARNINGS: {}".format(psu.psu_id, ", ".join(active))
                log.warning(msg)
                self._print("\n[flatpack2] WARNING: " + msg)
        else:
            psu.alarms = active
            if active:
                msg = "PSU ID={} ALARMS: {}".format(psu.psu_id, ", ".join(active))
                log.error(msg)
                self._print("\n[flatpack2] ALARM: " + msg)

    # ------------------------------------------------------------------
    # User commands
    # ------------------------------------------------------------------
    def cmd_set(self, voltage, current, psu_id=None):
        """Send SET. Validates limits, uses broadcast 0x05FF4004."""
        # Validate voltage
        if not (PSU_V_MIN <= voltage <= PSU_V_MAX):
            self._print("[flatpack2] ERROR: Voltage {:.2f}V out of range "
                       "({:.1f}-{:.1f}V)".format(voltage, PSU_V_MIN, PSU_V_MAX),
                       async_msg=False)
            return False
        # Validate current
        if not (0 < current <= PSU_I_MAX):
            self._print("[flatpack2] ERROR: Current {:.1f}A out of range "
                       "(0-{:.1f}A)".format(current, PSU_I_MAX),
                       async_msg=False)
            return False
        # Power limit
        if voltage * current > PSU_P_MAX:
            current = PSU_P_MAX / voltage
            self._print("[flatpack2] WARNING: Power limit reached, "
                       "current limited to {:.1f}A".format(current),
                       async_msg=False)
        # OVP check
        if voltage >= self.ovp_voltage:
            self._print("[flatpack2] ERROR: Voltage {:.2f}V >= OVP {:.2f}V".format(
                voltage, self.ovp_voltage), async_msg=False)
            return False

        vout_cv = int(round(voltage * 100))
        iout_da = int(round(current * 10))
        ovp_cv  = int(round(self.ovp_voltage * 100))
        data    = struct.pack("<HHHH", iout_da, vout_cv, vout_cv, ovp_cv)

        ok = self._send(SET_ARB, data)
        if not ok:
            self._print("[flatpack2] ERROR: CAN send failed", async_msg=False)
            return False

        log.info("SET: V={:.2f}V I={:.1f}A OVP={:.1f}V".format(
            voltage, current, self.ovp_voltage))

        if psu_id is None:
            self._print("[flatpack2] SET all PSUs -> V={:.2f}V  I={:.1f}A".format(
                voltage, current), async_msg=False)
            for psu in self.psus.values():
                psu.set_voltage = voltage
                psu.set_current = current
                psu.last_set_voltage = voltage
                psu.last_set_current = current
        else:
            serial_hex = self._id_map.get(psu_id)
            if not serial_hex:
                self._print("[flatpack2] ERROR: unknown ID={}".format(psu_id),
                           async_msg=False)
                return False
            self._print("[flatpack2] SET PSU ID={} -> V={:.2f}V  I={:.1f}A".format(
                psu_id, voltage, current), async_msg=False)
            psu = self.psus[serial_hex]
            psu.set_voltage = voltage
            psu.set_current = current
            psu.last_set_voltage = voltage
            psu.last_set_current = current
        return True

    def cmd_standby(self, psu_id=None):
        """
        Enter standby mode: V=STANDBY_VOLTAGE, I=STANDBY_CURRENT.
        Overwrites last_set_voltage/current so reconnect restore keeps standby values.
        """
        ok = self.cmd_set(STANDBY_VOLTAGE, STANDBY_CURRENT, psu_id=psu_id)
        if ok:
            log.info("STANDBY: V={:.1f}V I={:.1f}A".format(STANDBY_VOLTAGE, STANDBY_CURRENT))
        return ok

    def get_psu(self, psu_id=None):
        if psu_id is None:
            return list(self.psus.values())
        serial_hex = self._id_map.get(psu_id)
        return [self.psus[serial_hex]] if serial_hex else []

    def wait_for_discovery(self):
        deadline = time.time() + self.disc_timeout
        print("[flatpack2] Waiting up to {}s for PSU discovery...".format(
            int(self.disc_timeout)))
        while time.time() < deadline:
            if self.psus:
                # Wait for first stable status
                t2 = time.time() + 3.0
                while time.time() < t2:
                    if any(p.last_status > 0 for p in self.psus.values()):
                        break
                    time.sleep(0.1)
                print("[flatpack2] {} PSU(s) ready".format(len(self.psus)))
                return
            time.sleep(0.2)
        print("[flatpack2] Discovery timeout - no PSUs found yet")

# ---------------------------------------------------------------------------
# Virtual PTY Terminal
# ---------------------------------------------------------------------------
class PTYTerminal:
    """
    Virtual PTY terminal for flatpack2.
    User connects via: screen /tmp/flatpack2.pty

    Fixes vs naive implementation:
      1. ECHO    - slave termios has ECHO enabled so user sees what they type
      2. CRLF    - ONLCR flag converts LF->CRLF so screen renders correctly
      3. SIGWINCH - window size changes from screen are propagated to master
      4. TIOCSWINSZ - initial window size copied from controlling terminal
      5. Async output - uses ANSI erase-line before async messages so prompt
                        is not corrupted by background PSU status prints
    """

    # ANSI escape sequences
    _ERASE_LINE  = "\r\033[K"   # move to col 0, erase to end of line
    _CRLF        = "\r\n"

    def __init__(self, symlink=None):
        self.symlink         = symlink
        self.master_fd       = None
        self.slave_fd        = None
        self.slave_name      = None
        self._lock           = threading.Lock()
        self._input_buf      = []
        self._input_event    = threading.Event()
        self._current_prompt = ""
        self._current_input  = ""
        self._reader_thread  = None
        self._reader_stop    = threading.Event()

    # ------------------------------------------------------------------
    # Open / close / disconnect
    # ------------------------------------------------------------------

    def open(self):
        """Create PTY pair and start reader thread."""
        self.master_fd, self.slave_fd = pty.openpty()
        self.slave_name = os.ttyname(self.slave_fd)

        self._configure_termios()
        self._init_winsize()
        signal.signal(signal.SIGWINCH, self._handle_sigwinch)

        log.info("PTY created: slave={}".format(self.slave_name))
        self._update_symlink()

        # Reset stop flag and start fresh reader thread
        self._reader_stop.clear()
        self._reader_thread = threading.Thread(
            target=self._reader_loop, daemon=True, name="pty-reader")
        self._reader_thread.start()

    def _update_symlink(self):
        """Create or update symlink pointing to current slave PTY."""
        if not self.symlink:
            print("[flatpack2] PTY terminal ready: screen {}".format(self.slave_name))
            return
        try:
            if os.path.exists(self.symlink) or os.path.islink(self.symlink):
                os.unlink(self.symlink)
            os.symlink(self.slave_name, self.symlink)
            log.info("PTY symlink: {} -> {}".format(self.symlink, self.slave_name))
            print("[flatpack2] PTY terminal ready: screen {}".format(self.symlink))
        except OSError as e:
            log.warning("Cannot create PTY symlink: {}".format(e))
            print("[flatpack2] PTY terminal ready: screen {}".format(self.slave_name))

    def close(self):
        """Full shutdown - stop reader thread, close PTY and remove symlink."""
        self._reader_stop.set()
        self._input_event.set()  # unblock readline()
        if self._reader_thread and self._reader_thread.is_alive():
            self._reader_thread.join(timeout=2.0)
        if self.symlink and os.path.islink(self.symlink):
            try:
                os.unlink(self.symlink)
            except OSError:
                pass
        slave_fd  = self.slave_fd
        master_fd = self.master_fd
        self.slave_fd  = None
        self.master_fd = None
        for fd in (slave_fd, master_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass

    # ------------------------------------------------------------------
    # Fix 1+2: termios configuration
    # ------------------------------------------------------------------

    def _configure_termios(self):
        """
        Configure slave PTY termios:
          - ECHO, ECHOE, ECHOK  : local echo of typed characters
          - ICANON              : canonical (line-buffered) input
          - ONLCR               : translate NL -> CR+NL on output (fixes line rendering)
          - OPOST               : enable output processing
        """
        try:
            attrs = termios.tcgetattr(self.slave_fd)
            # attrs = [iflag, oflag, cflag, lflag, ispeed, ospeed, cc]

            # Output flags (oflag = attrs[1]): enable OPOST + ONLCR
            attrs[1] |= termios.OPOST | termios.ONLCR

            # Local flags (lflag = attrs[3]): enable ECHO + ICANON
            attrs[3] |= (termios.ECHO | termios.ECHOE |
                         termios.ECHOK | termios.ICANON | termios.ISIG)

            termios.tcsetattr(self.slave_fd, termios.TCSANOW, attrs)
            log.debug("PTY slave termios configured (ECHO+ONLCR)")
        except (termios.error, OSError) as e:
            log.warning("Cannot configure PTY termios: {}".format(e))

    # ------------------------------------------------------------------
    # Fix 4: window size init
    # ------------------------------------------------------------------

    def _get_winsize(self, fd):
        """Read terminal window size via TIOCGWINSZ. Returns (rows, cols) or None."""
        try:
            import termios as _t
            TIOCGWINSZ = 0x5413  # Linux
            buf = array.array('H', [0, 0, 0, 0])
            fcntl.ioctl(fd, TIOCGWINSZ, buf)
            rows, cols = buf[0], buf[1]
            if rows > 0 and cols > 0:
                return rows, cols
        except (OSError, AttributeError):
            pass
        return None

    def _set_winsize(self, fd, rows, cols):
        """Set terminal window size via TIOCSWINSZ."""
        try:
            TIOCSWINSZ = 0x5414  # Linux
            buf = array.array('H', [rows, cols, 0, 0])
            fcntl.ioctl(fd, TIOCSWINSZ, buf)
            log.debug("PTY winsize set: {}x{}".format(cols, rows))
        except (OSError, AttributeError) as e:
            log.debug("Cannot set PTY winsize: {}".format(e))

    def _init_winsize(self):
        """Copy window size from controlling terminal to PTY master."""
        # Try stdin, stdout, stderr in order
        for fd in (sys.stdin.fileno(), sys.stdout.fileno(), sys.stderr.fileno()):
            try:
                size = self._get_winsize(fd)
                if size:
                    rows, cols = size
                    self._set_winsize(self.master_fd, rows, cols)
                    log.info("PTY window size: {}x{} (from fd {})".format(cols, rows, fd))
                    return
            except (OSError, AttributeError):
                continue
        # Fallback to common default
        self._set_winsize(self.master_fd, 24, 80)
        log.debug("PTY window size: 80x24 (default)")

    # ------------------------------------------------------------------
    # Fix 3: SIGWINCH handler
    # ------------------------------------------------------------------

    def _handle_sigwinch(self, signo, frame):
        """
        Propagate window resize from controlling terminal to PTY.
        Called when user resizes their screen window.
        """
        for fd in (sys.stdin.fileno(), sys.stdout.fileno()):
            try:
                size = self._get_winsize(fd)
                if size:
                    rows, cols = size
                    self._set_winsize(self.master_fd, rows, cols)
                    log.debug("PTY resized: {}x{}".format(cols, rows))
                    return
            except (OSError, AttributeError):
                continue

    # ------------------------------------------------------------------
    # Fix 5: write with async-safe prompt redraw
    # ------------------------------------------------------------------

    def write(self, text, async_msg=False):
        """
        Write text to PTY master (visible to screen user).

        async_msg=True: called from background thread (e.g. PSU status alert).
          - Erases current prompt line first
          - Writes the message
          - Redraws prompt + partial input so user can continue typing
        """
        if self.master_fd is None:
            return

        # Convert bare LF to CRLF (safety net in case ONLCR not active on master)
        text = text.replace("\r\n", "\n").replace("\n", "\r\n")

        with self._lock:
            try:
                if async_msg and self._current_prompt:
                    out = (self._ERASE_LINE +
                           text.rstrip("\r\n") + self._CRLF +
                           self._current_prompt + self._current_input)
                else:
                    out = text
                encoded = out.encode('utf-8', errors='replace')
                # Use non-blocking write: if PTY buffer is full (no reader connected)
                # we drop the output rather than blocking the calling thread.
                try:
                    flags = fcntl.fcntl(self.master_fd, fcntl.F_GETFL)
                    fcntl.fcntl(self.master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
                    os.write(self.master_fd, encoded)
                finally:
                    # Restore blocking mode so PTY reader loop works normally
                    fcntl.fcntl(self.master_fd, fcntl.F_SETFL, flags)
            except OSError:
                pass

    def set_prompt(self, prompt, current_input=""):
        """Track current prompt and partial input for async redraw."""
        self._current_prompt = prompt
        self._current_input  = current_input

    # ------------------------------------------------------------------
    # Input reader
    # ------------------------------------------------------------------

    def _reader_loop(self):
        """Read input lines from user via PTY master with manual echo."""
        buf = bytearray()
        while not self._reader_stop.is_set():
            # Guard: master_fd may be None if FD was closed
            master_fd = self.master_fd
            if master_fd is None:
                break
            try:
                r, _, _ = select.select([master_fd], [], [], 0.1)
                if not r:
                    continue
                data = os.read(master_fd, 256)
                if not data:
                    break

                for byte in data:
                    if self._reader_stop.is_set():
                        return

                    # Re-check master_fd before each write
                    mfd = self.master_fd
                    if mfd is None:
                        return

                    if byte in (ord('\r'), ord('\n')):
                        # Enter - echo CRLF, flush line to input buffer
                        try:
                            os.write(mfd, b'\r\n')
                        except OSError:
                            return
                        line = buf.decode('utf-8', errors='replace').strip()
                        self._input_buf.append(line)
                        self._input_event.set()
                        buf.clear()
                        self._current_input = ""

                    elif byte in (0x7f, 0x08):
                        # Backspace / DEL
                        if buf:
                            buf.pop()
                            try:
                                os.write(mfd, b'\x08 \x08')
                            except OSError:
                                return
                        self._current_input = buf.decode('utf-8', errors='replace')

                    elif byte == 0x03:
                        # Ctrl+C
                        try:
                            os.write(mfd, b'^C\r\n')
                        except OSError:
                            return
                        buf.clear()
                        self._current_input = ""

                    elif byte == 0x04:
                        # Ctrl+D - EOF
                        self._input_buf.append("")
                        self._input_event.set()
                        buf.clear()

                    elif 0x20 <= byte < 0x7f:
                        # Normal printable ASCII
                        buf.append(byte)
                        try:
                            os.write(mfd, bytes([byte]))
                        except OSError:
                            return
                        self._current_input = buf.decode('utf-8', errors='replace')

                    else:
                        # Other control chars - echo only
                        try:
                            os.write(mfd, bytes([byte]))
                        except OSError:
                            return

            except (OSError, ValueError):
                # OSError: FD closed; ValueError: invalid FD
                break

    def readline(self, timeout=None):
        """Read one complete input line. Blocks until available.
        Returns None only when session is being closed (stop flag set)."""
        while True:
            if self._input_buf:
                self._input_event.clear()
                val = self._input_buf.pop(0)
                if val is None:
                    # Sentinel - PTY closed
                    return None
                return val
            if self._reader_stop.is_set():
                return None
            self._input_event.wait(timeout=1.0)
            self._input_event.clear()

# ---------------------------------------------------------------------------
# Charger configuration loader
# ---------------------------------------------------------------------------

def get_charger_config(cfg):
    """Load [charger] section from config. Returns dict or None if not present."""
    if not cfg.has_section("charger"):
        return None
    c = {}
    c["cell_count"]           = cfg.getint  ("charger", "cell_count",           fallback=16)
    c["cell_voltage_max"]     = cfg.getfloat("charger", "cell_voltage_max",     fallback=3.65)
    c["capacity"]             = cfg.getfloat("charger", "capacity",             fallback=100.0)
    c["charge_current"]       = cfg.getfloat("charger", "charge_current",       fallback=20.0)
    c["charge_current_tail"]  = cfg.getfloat("charger", "charge_current_tail",  fallback=2.0)
    c["safety_time_limit"]    = cfg.getint  ("charger", "safety_time_limit",    fallback=600)
    c["min_current_detect"]   = cfg.getfloat("charger", "min_current_detect",   fallback=0.5)
    c["detect_voltage"]       = cfg.getfloat("charger", "detect_voltage",       fallback=48.0)
    c["detect_current"]       = cfg.getfloat("charger", "detect_current",       fallback=0.2)
    c["detect_threshold"]     = cfg.getfloat("charger", "detect_threshold",     fallback=1.0)
    c["ramp_step_voltage"]    = cfg.getfloat("charger", "ramp_step_voltage",    fallback=0.1)
    c["ramp_step_interval"]   = cfg.getfloat("charger", "ramp_step_interval",   fallback=5.0)
    c["voltage_tolerance"]    = cfg.getfloat("charger", "voltage_tolerance",    fallback=0.1)
    c["monitor_interval"]     = cfg.getfloat("charger", "monitor_interval",     fallback=5.0)
    c["auto_start"]           = cfg.getboolean("charger", "auto_start",         fallback=True)
    # Derived
    c["target_voltage"] = round(c["cell_count"] * c["cell_voltage_max"], 3)
    return c

# ---------------------------------------------------------------------------
# Charger state machine
# ---------------------------------------------------------------------------

class ChargePhase:
    IDLE    = "idle"
    DETECT  = "detect"   # waiting for battery: PSU at detect_voltage, detect_current
    RAMP    = "ramp"     # soft-start: voltage ramps from V_bat up to target
    CC      = "CC"
    CV      = "CV"
    DONE    = "done"
    ERROR   = "error"

class ChargerState:
    def __init__(self):
        self.phase         = ChargePhase.IDLE
        self.start_time    = None
        self.charge_current= None   # actual current used (may differ from config)
        self.ah            = 0.0
        self.wh            = 0.0
        self.last_status_t = None   # time of last STATUS packet used for integration
        self.stop_reason   = None
        self.active_psus   = []     # list of psu_ids participating in charge
        self.ramp_voltage  = None   # current V_set during RAMP phase

    @property
    def elapsed_minutes(self):
        if self.start_time is None:
            return 0.0
        return (time.time() - self.start_time) / 60.0

    @property
    def elapsed_str(self):
        if self.start_time is None:
            return "0:00:00"
        s = int(time.time() - self.start_time)
        return "{:d}:{:02d}:{:02d}".format(s // 3600, (s % 3600) // 60, s % 60)

    def is_active(self):
        return self.phase in (ChargePhase.DETECT, ChargePhase.RAMP,
                              ChargePhase.CC, ChargePhase.CV)

class Charger:
    """
    LiFePO4 CC/CV charger logic.

    CC phase : constant current until Vout >= target_voltage - tolerance
    CV phase : PSU holds voltage, current naturally decreases
    End      : total Iout across all PSUs <= charge_current_tail
    Safety   : time limit, High Temp, PSU ALARM
    """

    def __init__(self, bus, cfg_dict, print_fn):
        self.bus      = bus
        self.cfg      = cfg_dict
        self._print   = print_fn
        self.state    = ChargerState()
        self._lock    = threading.Lock()
        self._monitor_thread = None
        self._running = False

    # ------------------------------------------------------------------
    # Integration - called from FlatpackBus._handle_status
    # ------------------------------------------------------------------

    def on_status(self, psu):
        """
        Called every time a STATUS packet arrives for a PSU.
        Integrates Ah/Wh from measured Vout/Iout.
        Also drives phase transitions and safety checks.
        """
        with self._lock:
            if not self.state.is_active():
                return
            if psu.psu_id not in self.state.active_psus:
                return
            # DETECT and RAMP phases are handled by _detect_ramp_loop thread
            if self.state.phase in (ChargePhase.DETECT, ChargePhase.RAMP):
                return

            now = time.time()

            # Integrate Ah/Wh only when triggered by the first active PSU,
            # to avoid counting multiple times per interval with multiple PSUs.
            first_pid = self.state.active_psus[0] if self.state.active_psus else None
            if psu.psu_id == first_pid and self.state.last_status_t is not None:
                dt = now - self.state.last_status_t
                if 0 < dt < 2.0:   # sanity: ignore gaps > 2s
                    total_iout = sum(
                        self.bus.psus[self.bus._id_map[pid]].iout
                        for pid in self.state.active_psus
                        if pid in self.bus._id_map and
                           self.bus._id_map[pid] in self.bus.psus
                    )
                    self.state.ah += total_iout * dt / 3600.0
                    self.state.wh += total_iout * psu.vout * dt / 3600.0

            if psu.psu_id == first_pid:
                self.state.last_status_t = now

            # Phase transition CC -> CV
            target = self.cfg["target_voltage"]
            tol    = self.cfg["voltage_tolerance"]
            if self.state.phase == ChargePhase.CC:
                if psu.vout >= target - tol:
                    self.state.phase = ChargePhase.CV
                    self._print("[charger] CC -> CV phase (Vout={:.2f}V target={:.2f}V)".format(
                        psu.vout, target), async_msg=True)
                    log.info("Charger CC->CV: Vout={:.2f}V".format(psu.vout))
                    if self.bus.webgui is not None:
                        self.bus.webgui.mark_cc_to_cv()

            # End of charge detection in CV phase
            if self.state.phase == ChargePhase.CV:
                total_iout = sum(
                    self.bus.psus[self.bus._id_map[pid]].iout
                    for pid in self.state.active_psus
                    if pid in self.bus._id_map and
                       self.bus._id_map[pid] in self.bus.psus
                )
                if total_iout <= self.cfg["charge_current_tail"]:
                    self._finish("Charge complete (tail current {:.1f}A)".format(total_iout))
                    return

            # Safety: time limit (counted from RAMP start, i.e. state.start_time)
            if self.state.elapsed_minutes >= self.cfg["safety_time_limit"]:
                self._finish("Safety timeout ({} min)".format(self.cfg["safety_time_limit"]),
                             error=True)
                return

            # Safety: PSU alarm or high temp (checked via psu.alarms/warnings)
            if psu.status == 0x0C:  # STATUS_ALARM
                self._finish("PSU ID={} ALARM - charging stopped".format(psu.psu_id),
                             error=True)
                return
            if any("High Temp" in w for w in psu.warnings + psu.alarms):
                self._finish("PSU ID={} High Temp - charging stopped".format(psu.psu_id),
                             error=True)
                return

    # ------------------------------------------------------------------
    # Start / stop
    # ------------------------------------------------------------------

    def start(self, psu_ids, current=None):
        """
        Start charging. psu_ids = list of PSU IDs to use.
        current overrides config value.
        Enters DETECT phase first (PSU at detect_voltage / detect_current),
        then RAMP (soft-start), then CC/CV.
        """
        with self._lock:
            if self.state.is_active():
                self._print("[charger] Already charging. Use 'charge stop' first.",
                            async_msg=False)
                return False

            charge_current = current if current is not None else self.cfg["charge_current"]
            target_voltage = self.cfg["target_voltage"]

            if charge_current > PSU_I_MAX:
                self._print("[charger] ERROR: Current {:.1f}A exceeds PSU max {:.1f}A".format(
                    charge_current, PSU_I_MAX), async_msg=False)
                return False
            if target_voltage > PSU_V_MAX:
                self._print("[charger] ERROR: Target {:.2f}V exceeds PSU max {:.1f}V".format(
                    target_voltage, PSU_V_MAX), async_msg=False)
                return False

            self.state                = ChargerState()
            self.state.phase          = ChargePhase.DETECT
            self.state.start_time     = None   # timer starts at RAMP entry
            self.state.charge_current = charge_current
            self.state.active_psus    = list(psu_ids)
            self.state.last_status_t  = time.time()

        detect_v = self.cfg["detect_voltage"]
        detect_i = self.cfg["detect_current"]
        ok = self.bus.cmd_set(detect_v, detect_i)
        if not ok:
            with self._lock:
                self.state.phase = ChargePhase.IDLE
            return False

        self._print("[charger] DETECT phase: PSU at {:.1f}V / {:.1f}A - waiting for battery "
                   "(threshold: vout > {:.1f}V)".format(
                   detect_v, detect_i,
                   detect_v + self.cfg["detect_threshold"]), async_msg=False)
        log.info("Charger DETECT: psus={} V={:.2f} I={:.2f} threshold={:.2f}V".format(
            psu_ids, detect_v, detect_i, detect_v + self.cfg["detect_threshold"]))

        threading.Thread(target=self._detect_ramp_loop, daemon=True,
                        name="charger-detect").start()
        return True

    def _detect_ramp_loop(self):
        """
        DETECT phase: PSU holds detect_voltage / detect_current.
        When vout > detect_voltage + detect_threshold, battery is present
        and vout approximates V_bat. Transition to RAMP.

        RAMP phase: voltage steps up by ramp_step_voltage every ramp_step_interval seconds,
        starting from V_bat. Transitions to CC when iout >= charge_current,
        or directly to CV when ramp_voltage >= target_voltage.
        If battery disconnects (vout drops back below detect threshold), return to DETECT.
        """
        detect_v   = self.cfg["detect_voltage"]
        detect_i   = self.cfg["detect_current"]
        threshold  = self.cfg["detect_threshold"]
        step_v     = self.cfg["ramp_step_voltage"]
        step_int   = self.cfg["ramp_step_interval"]
        target_v   = self.cfg["target_voltage"]
        charge_i   = self.state.charge_current

        def _get_psu():
            """Return first active PSU object or None."""
            for pid in self.state.active_psus:
                shex = self.bus._id_map.get(pid)
                if shex and shex in self.bus.psus:
                    return self.bus.psus[shex]
            return None

        # ----------------------------------------------------------------
        # DETECT loop
        # ----------------------------------------------------------------
        while True:
            time.sleep(step_int)
            with self._lock:
                if self.state.phase != ChargePhase.DETECT:
                    return   # user stopped

            psu = _get_psu()
            if psu is None:
                continue

            vout = psu.vout
            iout = psu.iout
            # Battery detected by voltage rise (battery above detect_v)
            # OR by current draw (battery below detect_v but accepting charge)
            if vout > detect_v + threshold or iout >= self.cfg["min_current_detect"]:
                v_bat     = vout
                ramp_start = max(round(v_bat - 3 * step_v, 3), detect_v)
                self._print("[charger] Battery detected (Vout={:.2f}V Iout={:.2f}A) "
                           "- starting RAMP from {:.2f}V".format(
                           vout, iout, ramp_start), async_msg=True)
                log.info("Charger: battery detected Vout={:.2f}V Iout={:.2f}A "
                        "ramp_start={:.2f}V".format(vout, iout, ramp_start))
                with self._lock:
                    self.state.phase        = ChargePhase.RAMP
                    self.state.ramp_voltage = ramp_start
                    # start_time marks beginning of RAMP (safety timer counts from here)
                    self.state.start_time   = time.time()
                    self.state.last_status_t = time.time()
                # Apply initial ramp voltage immediately
                self.bus.cmd_set(ramp_start, charge_i)
                break
            else:
                log.debug("Charger DETECT: Vout={:.2f}V Iout={:.2f}A - no battery".format(
                    vout, iout))

        # ----------------------------------------------------------------
        # RAMP loop
        # ----------------------------------------------------------------
        while True:
            time.sleep(step_int)
            with self._lock:
                if self.state.phase != ChargePhase.RAMP:
                    return   # transitioned out (user stop / error)

            psu = _get_psu()
            if psu is None:
                continue

            vout  = psu.vout
            iout  = psu.iout
            ramp_v = self.state.ramp_voltage

            # Battery disconnect detection: vout dropped back near detect level
            if vout < detect_v + threshold - 0.5:
                self._print("[charger] Battery disconnected during RAMP "
                           "(Vout={:.2f}V) - returning to DETECT".format(vout), async_msg=True)
                log.warning("Charger RAMP: battery disconnect Vout={:.2f}V".format(vout))
                with self._lock:
                    self.state.phase        = ChargePhase.DETECT
                    self.state.ramp_voltage = None
                    self.state.start_time   = None
                    self.state.ah           = 0.0
                    self.state.wh           = 0.0
                    self.state.last_status_t = time.time()
                ok = self.bus.cmd_set(detect_v, detect_i)
                if not ok:
                    with self._lock:
                        self.state.phase      = ChargePhase.ERROR
                        self.state.stop_reason = "CAN send failed on battery disconnect"
                    return
                # re-enter DETECT loop
                while True:
                    time.sleep(step_int)
                    with self._lock:
                        if self.state.phase != ChargePhase.DETECT:
                            return
                    psu2 = _get_psu()
                    if psu2 is None:
                        continue
                    vout2 = psu2.vout
                    iout2 = psu2.iout
                    if vout2 > detect_v + threshold or iout2 >= self.cfg["min_current_detect"]:
                        v_bat2      = vout2
                        ramp_start2 = max(round(v_bat2 - 3 * step_v, 3), detect_v)
                        self._print("[charger] Battery re-detected (Vout={:.2f}V Iout={:.2f}A) "
                                   "- starting RAMP from {:.2f}V".format(
                                   vout2, iout2, ramp_start2), async_msg=True)
                        log.info("Charger: battery re-detected Vout={:.2f}V "
                                "ramp_start={:.2f}V".format(vout2, ramp_start2))
                        with self._lock:
                            self.state.phase        = ChargePhase.RAMP
                            self.state.ramp_voltage = ramp_start2
                            self.state.start_time   = time.time()
                            self.state.last_status_t = time.time()
                        self.bus.cmd_set(ramp_start2, charge_i)
                        ramp_v = ramp_start2
                        break
                    else:
                        log.debug("Charger DETECT: Vout={:.2f}V Iout={:.2f}A - no battery".format(
                            vout2, iout2))
                continue

            # CC transition: iout already at charge current → PSU entered CC on its own
            if iout >= charge_i * 0.95:
                self._print("[charger] RAMP -> CC (Iout={:.1f}A at Vramp={:.2f}V)".format(
                    iout, ramp_v), async_msg=True)
                log.info("Charger RAMP->CC: Iout={:.1f}A Vramp={:.2f}V".format(iout, ramp_v))
                with self._lock:
                    self.state.phase        = ChargePhase.CC
                    self.state.ramp_voltage = None
                return

            # Advance ramp voltage
            new_v = round(ramp_v + step_v, 3)
            if new_v >= target_v:
                new_v = target_v

            with self._lock:
                self.state.ramp_voltage = new_v

            ok = self.bus.cmd_set(new_v, charge_i)
            if not ok:
                with self._lock:
                    self.state.phase      = ChargePhase.ERROR
                    self.state.stop_reason = "CAN send failed during RAMP"
                return

            log.debug("Charger RAMP: V={:.2f}V Vout={:.2f}V Iout={:.1f}A".format(
                new_v, vout, iout))

            # Reached target voltage → skip CC, go straight to CV
            if new_v >= target_v:
                self._print("[charger] RAMP -> CV (target {:.2f}V reached, "
                           "Iout={:.1f}A)".format(target_v, iout), async_msg=True)
                log.info("Charger RAMP->CV: target reached Iout={:.1f}A".format(iout))
                with self._lock:
                    self.state.phase        = ChargePhase.CV
                    self.state.ramp_voltage = None
                if self.bus.webgui is not None:
                    self.bus.webgui.mark_cc_to_cv()
                return

    def stop(self, reason="User stop"):
        """Stop charging - set current to 0."""
        with self._lock:
            if not self.state.is_active():
                return
            self._do_stop(reason)

    def _finish(self, reason, error=False):
        """Called from on_status (already locked)."""
        self._do_stop(reason)
        if error:
            self._print("[charger] ERROR: {}".format(reason), async_msg=True)
            log.error("Charge stopped: {}".format(reason))
        else:
            self._print("[charger] {}".format(reason), async_msg=True)
            log.info("Charge finished: {}".format(reason))
        self._print("[charger] Total: {:.2f}Ah  {:.2f}Wh  Time: {}".format(
            self.state.ah, self.state.wh, self.state.elapsed_str), async_msg=True)

    def _do_stop(self, reason):
        """Set PSU to standby values and update state. Must be called with lock held."""
        self.state.phase       = ChargePhase.DONE
        self.state.stop_reason = reason
        self.bus.cmd_set(STANDBY_VOLTAGE, STANDBY_CURRENT)

    # ------------------------------------------------------------------
    # Status / monitoring
    # ------------------------------------------------------------------

    def get_status_lines(self):
        """Return list of status lines for display."""
        s = self.state
        lines = []
        lines.append("Phase     : {}".format(s.phase))
        lines.append("Time      : {}".format(s.elapsed_str))
        lines.append("Charged   : {:.3f} Ah  {:.3f} Wh".format(s.ah, s.wh))
        if s.charge_current:
            lines.append("Set I     : {:.1f} A per PSU".format(s.charge_current))
        if s.phase == ChargePhase.RAMP and s.ramp_voltage is not None:
            lines.append("Ramp V    : {:.2f} V  (target {:.2f} V)".format(
                s.ramp_voltage, self.cfg["target_voltage"]))
        elif s.phase == ChargePhase.DETECT:
            lines.append("Detect V  : {:.2f} V  (threshold +{:.1f} V)".format(
                self.cfg["detect_voltage"], self.cfg["detect_threshold"]))
        else:
            lines.append("Target V  : {:.2f} V  ({} cells x {:.3f}V)".format(
                self.cfg["target_voltage"],
                self.cfg["cell_count"],
                self.cfg["cell_voltage_max"]))
        lines.append("Tail I    : {:.1f} A".format(self.cfg["charge_current_tail"]))
        lines.append("Time limit: {} min".format(self.cfg["safety_time_limit"]))
        if s.active_psus:
            lines.append("PSUs      : {}".format(s.active_psus))
        # Live PSU data
        total_i = 0.0
        for pid in s.active_psus:
            shex = self.bus._id_map.get(pid)
            psu  = self.bus.psus.get(shex) if shex else None
            if psu:
                lines.append("PSU {:2d}    : Vout={:.2f}V  Iout={:.1f}A  "
                            "Tin={}C Tout={}C  {}".format(
                    pid, psu.vout, psu.iout,
                    psu.temp_in, psu.temp_out, psu.status_name()))
                total_i += psu.iout
        if len(s.active_psus) > 1:
            lines.append("Total I   : {:.1f} A".format(total_i))
        if s.stop_reason:
            lines.append("Stop reason: {}".format(s.stop_reason))
        return lines

    def start_monitor(self, out_fn, interval=None, stop_event=None):
        """Start continuous monitor output. Runs in calling thread until stop_event."""
        interval = interval or self.cfg["monitor_interval"]
        self._print("[charger] Monitor started (Enter to stop)".format(), async_msg=False)
        while True:
            if stop_event and stop_event.is_set():
                break
            lines = self.get_status_lines()
            out_fn("\n--- Charge status {} ---".format(
                time.strftime("%H:%M:%S")))
            for l in lines:
                out_fn("  " + l)
            time.sleep(interval)

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def format_help_text():
    """Built at call time (not import time) so it reflects the power_rating
    actually loaded from config (PSU_I_MAX / PSU_P_MAX may differ from the
    2000W defaults)."""
    return """
flatpack2 v{ver} - Eltek Flatpack2 CAN controller
===========================================
Commands:
  help               Show this help
  get                Show status of all PSUs
  get <id>           Show status of PSU with given ID
  set <id> <V> <I>   Set voltage (V) and current (A) for PSU
  set all <V> <I>    Set voltage and current for ALL PSUs
  standby            Set ALL PSUs to standby ({sv}V / {si}A)
  standby <id>       Set PSU <id> to standby ({sv}V / {si}A)
  map                Show serial-number to ID mapping
  shutdown           Stop program completely

Charger commands (LiFePO4 CC/CV):
  charge start [I]   Start charging (optional current override in A)
  charge stop        Stop charging (sets current to 0)
  charge status      Show current charge status
  charge monitor     Continuous status monitor (Enter to stop)
  charge config      Show charger configuration
  charge battery     Show battery parameters (static config)

Limits (Flatpack2 48V/{pmax:.0f}W HE):
  Voltage : {vmin:.1f} - {vmax:.1f} V
  Current : 0 - {imax:.1f} A
  Power   : max {pmax:.0f} W

Examples:
  get
  get 1
  set 1 54.0 20.0
  standby
  standby 1
  charge start
  charge start 15.0
  charge status
  charge battery
  charge monitor
""".format(ver=VERSION, vmin=PSU_V_MIN, vmax=PSU_V_MAX, imax=PSU_I_MAX, pmax=PSU_P_MAX,
           sv=STANDBY_VOLTAGE, si=STANDBY_CURRENT)

def run_cli(bus, terminal=None):
    """
    Main CLI loop. Works with PTY terminal and stdio.

    Returns:
      "shutdown" - stop program
    """
    from datetime import datetime

    def out(msg):
        if terminal:
            terminal.write(msg + "\n", async_msg=False)
        else:
            print(msg)

    def inp(prompt):
        if terminal:
            terminal.set_prompt(prompt)
            terminal.write(prompt, async_msg=False)
            line = terminal.readline()
            terminal.set_prompt("")
            # None means PTY was closed externally (not user disconnect command)
            return line
        else:
            try:
                return input(prompt)
            except (EOFError, KeyboardInterrupt):
                return None

    banner = ("=" * 52 + "\n"
              "  flatpack2 v{}  -  Eltek Flatpack2 controller\n".format(VERSION) +
              "  shutdown = stop program\n" +
              "=" * 52)
    out(banner)

    while True:
        raw = inp("fp2> ")
        if raw is None:
            out("[flatpack2] Session closed.")
            break
        raw = raw.strip()
        if not raw:
            continue

        parts = raw.split()
        cmd   = parts[0].lower()

        if cmd == "help":
            out(format_help_text())

        elif cmd == "shutdown":
            out("[flatpack2] Shutting down...")
            break

        elif cmd == "charge":
            charger = bus.charger
            if charger is None:
                out("[flatpack2] Charger not configured. Add [charger] section to config.")
                continue

            subcmd = parts[1].lower() if len(parts) > 1 else ""

            if subcmd == "start":
                # charge start [current]
                current = None
                if len(parts) >= 3:
                    try:
                        current = float(parts[2])
                    except ValueError:
                        out("[charger] ERROR: invalid current '{}'".format(parts[2]))
                        continue
                # Use all known PSUs
                psu_ids = list(bus._id_map.keys())
                if not psu_ids:
                    out("[charger] ERROR: No PSUs discovered yet.")
                    continue
                charger.start(psu_ids, current=current)

            elif subcmd == "stop":
                if not charger.state.is_active():
                    out("[charger] Not charging.")
                else:
                    charger.stop("User stop")
                    out("[charger] Charging stopped.")
                    out("[charger] Total: {:.3f} Ah  {:.3f} Wh  Time: {}".format(
                        charger.state.ah, charger.state.wh,
                        charger.state.elapsed_str))

            elif subcmd == "status":
                lines = charger.get_status_lines()
                out("")
                out("--- Charge status {} ---".format(time.strftime("%H:%M:%S")))
                for l in lines:
                    out("  " + l)
                out("")

            elif subcmd == "monitor":
                interval = charger.cfg["monitor_interval"]
                if len(parts) >= 3:
                    try:
                        interval = float(parts[2])
                    except ValueError:
                        pass
                out("[charger] Monitor started - press Enter to stop")
                stop_event = threading.Event()
                # Run monitor in background thread, wait for Enter
                def _monitor():
                    while not stop_event.is_set():
                        lines = charger.get_status_lines()
                        out("\n--- Charge status {} ---".format(
                            time.strftime("%H:%M:%S")))
                        for l in lines:
                            out("  " + l)
                        for _ in range(int(interval * 10)):
                            if stop_event.is_set():
                                break
                            time.sleep(0.1)
                t = threading.Thread(target=_monitor, daemon=True)
                t.start()
                inp("")   # wait for Enter
                stop_event.set()
                t.join(timeout=2)
                out("[charger] Monitor stopped.")

            elif subcmd == "config":
                cfg_d = charger.cfg
                out("")
                out("--- Charger configuration ---")
                out("  Cell count    : {}".format(cfg_d["cell_count"]))
                out("  Cell V max    : {:.3f} V".format(cfg_d["cell_voltage_max"]))
                out("  Target V      : {:.3f} V".format(cfg_d["target_voltage"]))
                out("  Capacity      : {:.0f} Ah".format(cfg_d["capacity"]))
                out("  Charge I      : {:.1f} A".format(cfg_d["charge_current"]))
                out("  Tail I        : {:.1f} A".format(cfg_d["charge_current_tail"]))
                out("  Time limit    : {} min".format(cfg_d["safety_time_limit"]))
                out("  Detect V      : {:.1f} V".format(cfg_d["detect_voltage"]))
                out("  Detect I      : {:.2f} A".format(cfg_d["detect_current"]))
                out("  Detect thresh : {:.1f} V".format(cfg_d["detect_threshold"]))
                out("  Ramp step V   : {:.2f} V".format(cfg_d["ramp_step_voltage"]))
                out("  Ramp interval : {:.1f} s".format(cfg_d["ramp_step_interval"]))
                out("  V tolerance   : {:.2f} V".format(cfg_d["voltage_tolerance"]))
                out("  Monitor intvl : {:.0f} s".format(cfg_d["monitor_interval"]))
                out("")

            elif subcmd == "battery":
                cfg_d = charger.cfg
                out("")
                out("--- Battery parameters ---")
                out("  Cell count    : {} cells".format(cfg_d["cell_count"]))
                out("  Cell V max    : {:.3f} V/cell".format(cfg_d["cell_voltage_max"]))
                out("  Target V      : {:.3f} V  ({} x {:.3f})".format(
                    cfg_d["target_voltage"], cfg_d["cell_count"], cfg_d["cell_voltage_max"]))
                out("  Capacity      : {:.0f} Ah".format(cfg_d["capacity"]))
                out("  Charge I (CC) : {:.1f} A".format(cfg_d["charge_current"]))
                out("  Tail I        : {:.1f} A  (end-of-charge)".format(cfg_d["charge_current_tail"]))
                out("  Detect V      : {:.1f} V  (PSU voltage during DETECT phase)".format(cfg_d["detect_voltage"]))
                out("  Detect I      : {:.2f} A  (PSU current during DETECT phase)".format(cfg_d["detect_current"]))
                out("  Detect thresh : {:.1f} V  (vout must exceed detect_voltage + threshold)".format(cfg_d["detect_threshold"]))
                out("  Ramp step     : {:.2f} V / {:.0f} s".format(cfg_d["ramp_step_voltage"], cfg_d["ramp_step_interval"]))
                out("  Time limit    : {} min  ({:.1f} h)".format(
                    cfg_d["safety_time_limit"], cfg_d["safety_time_limit"] / 60.0))
                out("  V tolerance   : {:.2f} V  (CC->CV threshold)".format(cfg_d["voltage_tolerance"]))
                out("  Auto-start    : {}".format("yes" if cfg_d.get("auto_start", True) else "no"))
                out("")

            else:
                out("[charger] Unknown subcommand '{}'. Use: start / stop / status / monitor / config / battery".format(subcmd))

        elif cmd == "standby":
            if len(parts) >= 2:
                try:
                    pid = int(parts[1])
                except ValueError:
                    out("[flatpack2] ERROR: invalid ID '{}' (use integer or omit for all)".format(parts[1]))
                    continue
                bus.cmd_standby(psu_id=pid)
            else:
                bus.cmd_standby()

        elif cmd in ("quit", "exit"):
            out("[flatpack2] Use 'shutdown' to stop program.")

        elif cmd == "map":
            if not bus.psus:
                out("[flatpack2] No PSUs discovered yet.")
            else:
                out("")
                out("{:>4}  {:>14}  {:>12}".format("ID", "Serial (hex)", "Last seen"))
                out("-" * 36)
                for pid, shex in sorted(bus._id_map.items()):
                    psu = bus.psus[shex]
                    ts  = datetime.fromtimestamp(psu.last_seen).strftime("%H:%M:%S")
                    out("{:>4}  {:>14}  {:>12}".format(pid, shex, ts))
                out("")

        elif cmd == "get":
            if len(parts) >= 2:
                try:
                    pid = int(parts[1])
                except ValueError:
                    out("[flatpack2] ERROR: invalid ID '{}'".format(parts[1]))
                    continue
                results = bus.get_psu(pid)
                if not results:
                    out("[flatpack2] No PSU with ID={}".format(pid))
                else:
                    out("")
                    for psu in results:
                        out("PSU ID={}:".format(psu.psu_id))
                        for line in str(psu).splitlines():
                            if line.strip():
                                out(line)
                        out("")
            else:
                results = bus.get_psu()
                if not results:
                    out("[flatpack2] No PSUs discovered yet.")
                else:
                    out("")
                    for psu in results:
                        out("PSU ID={}:".format(psu.psu_id))
                        for line in str(psu).splitlines():
                            if line.strip():
                                out(line)
                        out("")

        elif cmd == "set":
            if len(parts) < 4:
                out("[flatpack2] Usage: set <id|all> <voltage> <current>")
                continue
            target = parts[1].lower()
            try:
                voltage = float(parts[2])
                current = float(parts[3])
            except ValueError:
                out("[flatpack2] ERROR: invalid voltage or current value")
                continue
            if target == "all":
                bus.cmd_set(voltage, current, psu_id=None)
            else:
                try:
                    pid = int(target)
                except ValueError:
                    out("[flatpack2] ERROR: invalid ID '{}' (use integer or 'all')".format(target))
                    continue
                bus.cmd_set(voltage, current, psu_id=pid)

        else:
            out("[flatpack2] Unknown command: '{}'. Type 'help'.".format(cmd))

# ---------------------------------------------------------------------------
# Daemonization
# ---------------------------------------------------------------------------
def daemonize(pidfile, user=None, group=None):
    """Double-fork daemonization."""
    # First fork
    try:
        pid = os.fork()
        if pid > 0:
            sys.exit(0)
    except OSError as e:
        print("Fork #1 failed: {}".format(e))
        sys.exit(1)

    os.chdir("/")
    os.setsid()
    os.umask(0)

    # Second fork
    try:
        pid = os.fork()
        if pid > 0:
            sys.exit(0)
    except OSError as e:
        print("Fork #2 failed: {}".format(e))
        sys.exit(1)

    # Redirect stdio
    sys.stdout.flush()
    sys.stderr.flush()
    with open(os.devnull, 'r') as f:
        os.dup2(f.fileno(), sys.stdin.fileno())
    with open(os.devnull, 'a+') as f:
        os.dup2(f.fileno(), sys.stdout.fileno())
        os.dup2(f.fileno(), sys.stderr.fileno())

    # Drop privileges
    if group:
        import grp
        os.setgid(grp.getgrnam(group).gr_gid)
    if user:
        import pwd
        os.setuid(pwd.getpwnam(user).pw_uid)

    # Write PID file
    pid = str(os.getpid())
    try:
        with open(pidfile, 'w') as f:
            f.write(pid + '\n')
    except OSError as e:
        log.error("Cannot write PID file {}: {}".format(pidfile, e))

    # Remove PID file on exit
    import atexit
    atexit.register(lambda: os.unlink(pidfile) if os.path.exists(pidfile) else None)

# ---------------------------------------------------------------------------
# Data history ring buffer (12h @ 10s = 4320 points)
# ---------------------------------------------------------------------------
HISTORY_MAXLEN = 4320

class DataHistory:
    """Thread-safe ring buffer for time-series data."""
    def __init__(self, maxlen=HISTORY_MAXLEN):
        self._lock = threading.Lock()
        self._ts   = collections.deque(maxlen=maxlen)
        self._vout = collections.deque(maxlen=maxlen)
        self._iout = collections.deque(maxlen=maxlen)
        self._ah   = collections.deque(maxlen=maxlen)
        self._wh   = collections.deque(maxlen=maxlen)

    def append(self, vout, iout, ah, wh):
        ts = time.time() * 1000  # ms epoch for JS
        with self._lock:
            self._ts.append(ts)
            self._vout.append(round(vout, 2))
            self._iout.append(round(iout, 2))
            self._ah.append(round(ah, 3))
            self._wh.append(round(wh, 3))

    def get(self, window_s=None):
        """Return dict of lists. window_s: None = all, else last N seconds."""
        with self._lock:
            ts   = list(self._ts)
            vout = list(self._vout)
            iout = list(self._iout)
            ah   = list(self._ah)
            wh   = list(self._wh)
        if window_s and ts:
            cutoff = (time.time() - window_s) * 1000
            idx = 0
            for i, t in enumerate(ts):
                if t >= cutoff:
                    idx = i
                    break
            ts   = ts[idx:]
            vout = vout[idx:]
            iout = iout[idx:]
            ah   = ah[idx:]
            wh   = wh[idx:]
        return {"ts": ts, "vout": vout, "iout": iout, "ah": ah, "wh": wh}

# ---------------------------------------------------------------------------
# Web-GUI (Flask)
# ---------------------------------------------------------------------------

_FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    '<rect width="32" height="32" rx="6" fill="#1a1a2e"/>'
    '<polygon points="18,2 8,18 15,18 14,30 24,14 17,14" fill="#f0c040"/>'
    '</svg>'
)

_DASHBOARD_HTML = """\
<!DOCTYPE html>
<html lang="cs">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Flatpack2 Dashboard</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3.0.0/dist/chartjs-adapter-date-fns.bundle.min.js"></script>
<style>
:root {
  --bg:#0f0f1a; --surface:#1a1a2e; --surface2:#16213e;
  --accent:#f0c040; --accent2:#4fc3f7; --green:#4caf50;
  --red:#f44336; --orange:#ff9800; --text:#e0e0e0;
  --text2:#9e9e9e; --border:#2a2a4a; --radius:12px;
}
*{box-sizing:border-box;margin:0;padding:0;}
body{background:var(--bg);color:var(--text);font-family:'Segoe UI',system-ui,sans-serif;min-height:100vh;}
header{background:var(--surface);border-bottom:1px solid var(--border);padding:12px 16px;
       display:flex;align-items:center;gap:12px;position:sticky;top:0;z-index:10;}
header h1{font-size:1.1rem;font-weight:600;color:var(--accent);}
header .version{font-size:.75rem;color:var(--text2);margin-left:auto;}
#can-status{font-size:.78rem;padding:3px 8px;border-radius:20px;font-weight:600;}
#can-status.ok {background:#1b5e20;color:#a5d6a7;}
#can-status.err{background:#b71c1c;color:#ffcdd2;}
.grid{display:grid;grid-template-columns:1fr;gap:12px;padding:12px;max-width:1200px;margin:0 auto;}
@media(min-width:700px){.grid{grid-template-columns:1fr 1fr;}}
@media(min-width:1000px){.grid{grid-template-columns:1fr 1fr 1fr;}}
.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:16px;}
.card.full{grid-column:1/-1;}
.card.half{grid-column:span 1;}
.card.span2{grid-column:span 1;}
@media(min-width:700px){.card.span2{grid-column:span 2;}}
@media(min-width:700px){.card.half2{grid-column:span 1;}}
.card h2{font-size:.8rem;font-weight:600;text-transform:uppercase;letter-spacing:.08em;
         color:var(--text2);margin-bottom:12px;}
.stat-row{display:flex;justify-content:space-between;align-items:baseline;
          padding:5px 0;border-bottom:1px solid var(--border);}
.stat-row:last-child{border-bottom:none;}
.stat-label{font-size:.82rem;color:var(--text2);}
.stat-value{font-size:1rem;font-weight:600;font-variant-numeric:tabular-nums;}
.badge{display:inline-block;padding:2px 8px;border-radius:20px;font-size:.75rem;font-weight:700;}
.badge.cv    {background:#0d47a1;color:#90caf9;}
.badge.cc    {background:#e65100;color:#ffcc80;}
.badge.waiting{background:#4a148c;color:#ce93d8;}
.badge.alarm {background:#b71c1c;color:#ffcdd2;}
.badge.walkin{background:#263238;color:#90a4ae;}
.badge.detect{background:#1a237e;color:#90caf9;}
.badge.ramp{background:#4a148c;color:#ce93d8;}
.badge.idle  {background:#263238;color:#90a4ae;}
.badge.ok    {background:#1b5e20;color:#a5d6a7;}
.badge.done  {background:#1b5e20;color:#a5d6a7;}
.badge.error {background:#b71c1c;color:#ffcdd2;}
form{display:flex;flex-direction:column;gap:10px;margin-top:8px;}
.form-row{display:flex;gap:8px;align-items:center;}
.form-row label{font-size:.8rem;color:var(--text2);min-width:64px;}
input[type=number]{background:var(--surface2);border:1px solid var(--border);border-radius:6px;
                   color:var(--text);padding:7px 10px;font-size:.9rem;width:100%;}
input[type=number]:focus{outline:none;border-color:var(--accent);}
.btn{border:none;border-radius:8px;padding:9px 18px;font-size:.88rem;font-weight:600;
     cursor:pointer;transition:opacity .15s;}
.btn:active{opacity:.7;}
.btn-primary{background:var(--accent);color:#111;}
.btn-green{background:var(--green);color:#fff;}
.btn-red{background:var(--red);color:#fff;}
.btn-standby{background:#37474f;color:#cfd8dc;}
.btn-row{display:flex;gap:8px;flex-wrap:wrap;}
.phase-bar-bg{background:var(--surface2);border-radius:20px;height:8px;margin:8px 0;overflow:hidden;}
.phase-bar{height:100%;border-radius:20px;background:var(--accent);transition:width .5s;}
.chart-wrap{position:relative;height:200px;}
.window-btns{display:flex;gap:6px;margin-bottom:8px;}
.window-btn{background:var(--surface2);border:1px solid var(--border);color:var(--text2);
            border-radius:6px;padding:3px 10px;font-size:.75rem;cursor:pointer;}
.window-btn.active{background:var(--accent);color:#111;border-color:var(--accent);font-weight:700;}
.log-box{background:var(--surface2);border-radius:8px;padding:10px;font-family:monospace;
         font-size:.75rem;height:160px;overflow-y:auto;color:#a5d6a7;}
.log-line{padding:1px 0;border-bottom:1px solid #1a1a1a;}
.log-line.warn{color:#ffcc80;}
.log-line.err {color:#ef9a9a;}
#conn-banner{display:none;position:fixed;top:56px;left:0;right:0;z-index:20;
             background:#b71c1c;color:#fff;text-align:center;padding:8px;
             font-weight:600;font-size:.9rem;}
.result-msg{margin-top:8px;font-size:.82rem;min-height:1.2em;}
#mode-badge{font-size:.78rem;padding:3px 8px;border-radius:20px;font-weight:600;
            background:#1a237e;color:#90caf9;}
.bat-grid{display:grid;grid-template-columns:1fr;gap:0;}
@media(min-width:700px){.bat-grid{grid-template-columns:1fr 1fr;gap:0 16px;}}
</style>
</head>
<body>
<div id="conn-banner">&#9888; Connection to server lost &ndash; reconnecting&hellip;</div>
<header>
  <svg width="28" height="28" viewBox="0 0 32 32">
    <rect width="32" height="32" rx="6" fill="#1a1a2e"/>
    <polygon points="18,2 8,18 15,18 14,30 24,14 17,14" fill="#f0c040"/>
  </svg>
  <h1>Flatpack2 Dashboard</h1>
  <span id="can-status" class="ok">CAN OK</span>
  <span id="mode-badge">Zdroj</span>
  <span class="version">v__VERSION__</span>
</header>

<div class="grid">

  <!-- PSU status -->
  <div class="card">
    <h2>&#9889; PSU Status</h2>
    <div class="stat-row"><span class="stat-label">Vout</span><span class="stat-value" id="vout">--</span></div>
    <div class="stat-row"><span class="stat-label">Iout</span><span class="stat-value" id="iout">--</span></div>
    <div class="stat-row"><span class="stat-label">Vin</span><span class="stat-value" id="vin">--</span></div>
    <div class="stat-row"><span class="stat-label">Temp in</span><span class="stat-value" id="temp_in">--</span></div>
    <div class="stat-row"><span class="stat-label">Temp out</span><span class="stat-value" id="temp_out">--</span></div>
    <div class="stat-row"><span class="stat-label">Status</span><span class="stat-value" id="psu-status-badge"><span class="badge idle">--</span></span></div>
    <div class="stat-row"><span class="stat-label">Set V</span><span class="stat-value" id="set_v">--</span></div>
    <div class="stat-row"><span class="stat-label">Set I</span><span class="stat-value" id="set_i">--</span></div>
  </div>

  <!-- Set form -->
  <div class="card">
    <h2>&#9881; PSU Settings</h2>
    <form id="set-form">
      <div class="form-row"><label>Voltage V</label><input type="number" id="f-voltage" step="0.1" min="__V_MIN__" max="__V_MAX__" placeholder="54.0"></div>
      <div class="form-row"><label>Current A</label><input type="number" id="f-current" step="0.1" min="0.1" max="__I_MAX__" placeholder="20.0"></div>
      <div class="btn-row">
        <button type="submit" class="btn btn-primary">Set</button>
        <button type="button" class="btn btn-standby" onclick="doStandby()">&#9711; Standby</button>
      </div>
      <div style="font-size:.75rem;color:var(--text2);margin-top:4px;">Standby: __STANDBY_V__&nbsp;V&nbsp;/&nbsp;__STANDBY_I__&nbsp;A</div>
    </form>
    <div id="set-result" class="result-msg"></div>
  </div>

  <!-- Charger -->
  <div class="card">
    <h2>&#128267; Charger</h2>
    <div class="stat-row"><span class="stat-label">Phase</span><span class="stat-value" id="ch-phase"><span class="badge idle">idle</span></span></div>
    <div class="stat-row"><span class="stat-label">Time</span><span class="stat-value" id="ch-time">--</span></div>
    <div class="stat-row"><span class="stat-label">Actual V</span><span class="stat-value" id="ch-actual-v">--</span></div>
    <div class="stat-row"><span class="stat-label">Actual I</span><span class="stat-value" id="ch-actual-i">--</span></div>
    <div class="stat-row"><span class="stat-label">Charged Ah</span><span class="stat-value" id="ch-ah">--</span></div>
    <div class="stat-row"><span class="stat-label">Charged Wh</span><span class="stat-value" id="ch-wh">--</span></div>
    <div class="stat-row"><span class="stat-label">Target V</span><span class="stat-value" id="ch-target">--</span></div>
    <div class="stat-row"><span class="stat-label">Tail I</span><span class="stat-value" id="ch-tail">--</span></div>
    <div class="phase-bar-bg"><div class="phase-bar" id="ch-bar" style="width:0%"></div></div>
    <div class="btn-row" style="margin-top:8px;">
      <button class="btn btn-green" onclick="chargeStart()">&#9654; Start</button>
      <button class="btn btn-red"   onclick="chargeStop()">&#9646;&#9646; Stop</button>
    </div>
    <div id="ch-result" class="result-msg"></div>
  </div>

  <!-- System -->
  <div class="card">
    <h2>&#128202; System</h2>
    <div class="stat-row"><span class="stat-label">Version</span><span class="stat-value" id="sys-version">--</span></div>
    <div class="stat-row"><span class="stat-label">Uptime</span><span class="stat-value" id="sys-uptime">--</span></div>
    <div class="stat-row"><span class="stat-label">CAN</span><span class="stat-value" id="sys-can">--</span></div>
    <div class="stat-row"><span class="stat-label">PSU count</span><span class="stat-value" id="sys-psu-count">--</span></div>
    <div class="stat-row"><span class="stat-label">Charger</span><span class="stat-value" id="sys-charger">--</span></div>
  </div>

  <!-- Battery params -->
  <div class="card span2" id="battery-card" style="display:none">
    <h2>&#128267; Battery Parameters</h2>
    <div class="bat-grid">
      <div>
        <div class="stat-row"><span class="stat-label">Cell count</span><span class="stat-value" id="bat-cells">--</span></div>
        <div class="stat-row"><span class="stat-label">Cell voltage max</span><span class="stat-value" id="bat-cell-v">--</span></div>
        <div class="stat-row"><span class="stat-label">Target voltage</span><span class="stat-value" id="bat-target">--</span></div>
        <div class="stat-row"><span class="stat-label">Capacity</span><span class="stat-value" id="bat-capacity">--</span></div>
        <div class="stat-row"><span class="stat-label">Charge current (CC)</span><span class="stat-value" id="bat-charge-i">--</span></div>
        <div class="stat-row"><span class="stat-label">Detect voltage</span><span class="stat-value" id="bat-detect-v">--</span></div>
        <div class="stat-row"><span class="stat-label">Detect current</span><span class="stat-value" id="bat-detect-i-cur">--</span></div>
      </div>
      <div>
        <div class="stat-row"><span class="stat-label">Tail current (end-of-charge)</span><span class="stat-value" id="bat-tail-i">--</span></div>
        <div class="stat-row"><span class="stat-label">Detect threshold</span><span class="stat-value" id="bat-detect-thresh">--</span></div>
        <div class="stat-row"><span class="stat-label">Ramp step</span><span class="stat-value" id="bat-ramp-step">--</span></div>
        <div class="stat-row"><span class="stat-label">Detection min. current</span><span class="stat-value" id="bat-detect-i">--</span></div>
        <div class="stat-row"><span class="stat-label">Time limit</span><span class="stat-value" id="bat-time">--</span></div>
        <div class="stat-row"><span class="stat-label">Tolerance CC→CV</span><span class="stat-value" id="bat-tol">--</span></div>
        <div class="stat-row"><span class="stat-label">Auto-start</span><span class="stat-value" id="bat-autostart">--</span></div>
      </div>
    </div>
  </div>

  <!-- Log -->
  <div class="card full">
    <h2>&#128196; Log</h2>
    <div class="log-box" id="log-box"></div>
  </div>

  <!-- Graph V+I -->
  <div class="card full">
    <h2>&#128200; Voltage &amp; Current</h2>
    <div class="window-btns" id="wb-vi">
      <button class="window-btn active" onclick="setWin('vi',900,this)">15 min</button>
      <button class="window-btn" onclick="setWin('vi',3600,this)">1 h</button>
      <button class="window-btn" onclick="setWin('vi',43200,this)">12 h</button>
    </div>
    <div class="chart-wrap"><canvas id="chart-vi"></canvas></div>
  </div>

  <!-- Graph Ah -->
  <div class="card full">
    <h2>&#9889; Delivered capacity [Ah]</h2>
    <div class="window-btns" id="wb-ah">
      <button class="window-btn active" onclick="setWin('ah',900,this)">15 min</button>
      <button class="window-btn" onclick="setWin('ah',3600,this)">1 h</button>
      <button class="window-btn" onclick="setWin('ah',43200,this)">12 h</button>
    </div>
    <div class="chart-wrap"><canvas id="chart-ah"></canvas></div>
  </div>

  <!-- Graph Wh -->
  <div class="card full">
    <h2>&#9889; Delivered energy [Wh]</h2>
    <div class="window-btns" id="wb-wh">
      <button class="window-btn active" onclick="setWin('wh',900,this)">15 min</button>
      <button class="window-btn" onclick="setWin('wh',3600,this)">1 h</button>
      <button class="window-btn" onclick="setWin('wh',43200,this)">12 h</button>
    </div>
    <div class="chart-wrap"><canvas id="chart-wh"></canvas></div>
  </div>

</div><!-- /grid -->

<script>
// ── State ──────────────────────────────────────────────────────────
const history = {ts:[],vout:[],iout:[],ah:[],wh:[]};
const windows  = {vi:900, ah:900, wh:900};
let ccToCvTs   = null;
let knownLogs  = new Set();

// ── Chart factory ──────────────────────────────────────────────────
function makeOpts(yLabel, y2Label) {
  return {
    animation: false,
    responsive: true, maintainAspectRatio: false,
    interaction: {mode:'index', intersect:false},
    plugins: {
      legend: {labels:{color:'#9e9e9e', font:{size:11}}},
      tooltip: {backgroundColor:'#1a1a2e', titleColor:'#f0c040', bodyColor:'#e0e0e0'}
    },
    scales: {
      x: {
        type:'time',
        time:{tooltipFormat:'HH:mm:ss', displayFormats:{second:'HH:mm:ss',minute:'HH:mm',hour:'HH:mm'}},
        ticks:{color:'#9e9e9e', maxTicksLimit:6, font:{size:10}},
        grid:{color:'#2a2a4a'}
      },
      y: {
        ticks:{color:'#9e9e9e', font:{size:10}}, grid:{color:'#2a2a4a'},
        title:{display:!!yLabel, text:yLabel||'', color:'#9e9e9e', font:{size:10}}
      },
      ...(y2Label ? {y2:{
        position:'right',
        ticks:{color:'#4fc3f7', font:{size:10}}, grid:{drawOnChartArea:false},
        title:{display:true, text:y2Label, color:'#4fc3f7', font:{size:10}}
      }} : {})
    }
  };
}

const chartVI = new Chart(document.getElementById('chart-vi').getContext('2d'), {
  type:'line',
  data:{datasets:[
    {label:'Vout (V)', data:[], borderColor:'#f0c040', backgroundColor:'rgba(240,192,64,.08)',
     borderWidth:2, pointRadius:0, yAxisID:'y',  tension:0.3},
    {label:'Iout (A)', data:[], borderColor:'#4fc3f7', backgroundColor:'rgba(79,195,247,.08)',
     borderWidth:2, pointRadius:0, yAxisID:'y2', tension:0.3}
  ]},
  options: makeOpts('V','A')
});

const chartAh = new Chart(document.getElementById('chart-ah').getContext('2d'), {
  type:'line',
  data:{datasets:[{label:'Delivered capacity [Ah]', data:[], borderColor:'#4caf50', backgroundColor:'rgba(76,175,80,.08)',
    borderWidth:2, pointRadius:0, tension:0.3}]},
  options: makeOpts('Ah', null)
});

const chartWh = new Chart(document.getElementById('chart-wh').getContext('2d'), {
  type:'line',
  data:{datasets:[{label:'Delivered energy [Wh]', data:[], borderColor:'#ff9800', backgroundColor:'rgba(255,152,0,.08)',
    borderWidth:2, pointRadius:0, tension:0.3}]},
  options: makeOpts('Wh', null)
});

// ── Window toggle ──────────────────────────────────────────────────
function setWin(chart, s, btn) {
  windows[chart] = s;
  btn.closest('.window-btns').querySelectorAll('.window-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  updateCharts();
}

// ── Chart update ───────────────────────────────────────────────────
function filterWin(ts, vals, winS) {
  const cutoff = Date.now() - winS * 1000;
  const t = [], v = [];
  for (let i = 0; i < ts.length; i++) {
    if (ts[i] >= cutoff) { t.push(ts[i]); v.push(vals[i]); }
  }
  return {t, v};
}

function updateCharts() {
  // V+I
  const vf = filterWin(history.ts, history.vout, windows.vi);
  const if_ = filterWin(history.ts, history.iout, windows.vi);
  chartVI.data.datasets[0].data = vf.t.map((t,i) => ({x:t, y:vf.v[i]}));
  chartVI.data.datasets[1].data = if_.t.map((t,i) => ({x:t, y:if_.v[i]}));
  // CC->CV annotation via plugin
  if (ccToCvTs) {
    chartVI.options.plugins.annotation = {
      annotations: { cctocv: {
        type:'line', xMin:ccToCvTs, xMax:ccToCvTs,
        borderColor:'rgba(76,175,80,.9)', borderWidth:2, borderDash:[5,4],
        label:{content:'CC\u2192CV', display:true, position:'start',
               color:'#4caf50', font:{size:9}, backgroundColor:'rgba(0,0,0,.5)'}
      }}
    };
  }
  chartVI.update('none');
  // Ah
  const ahf = filterWin(history.ts, history.ah, windows.ah);
  chartAh.data.datasets[0].data = ahf.t.map((t,i) => ({x:t, y:ahf.v[i]}));
  chartAh.update('none');
  // Wh
  const whf = filterWin(history.ts, history.wh, windows.wh);
  chartWh.data.datasets[0].data = whf.t.map((t,i) => ({x:t, y:whf.v[i]}));
  chartWh.update('none');
}

// ── SSE ────────────────────────────────────────────────────────────
const banner = document.getElementById('conn-banner');
let sse = null, sseRetry = 2000;

function connectSSE() {
  sse = new EventSource('/events');
  sse.onopen = () => { banner.style.display='none'; sseRetry=2000; };
  sse.onmessage = (e) => { try { handleData(JSON.parse(e.data)); } catch(ex) {} };
  sse.onerror = () => {
    sse.close();
    banner.style.display = 'block';
    setTimeout(connectSSE, sseRetry);
    sseRetry = Math.min(sseRetry * 1.5, 15000);
  };
}
connectSSE();

// ── Data handler ───────────────────────────────────────────────────
function badge(cls, text) { return `<span class="badge ${cls}">${text}</span>`; }
function setText(id, val)  { const el=document.getElementById(id); if(el) el.textContent=val; }
function setHTML(id, html) { const el=document.getElementById(id); if(el) el.innerHTML=html; }

function handleData(d) {
  // History
  if (d.history) {
    history.ts   = d.history.ts;
    history.vout = d.history.vout;
    history.iout = d.history.iout;
    history.ah   = d.history.ah;
    history.wh   = d.history.wh;
  }
  if (d.cc_to_cv_ts != null) ccToCvTs = d.cc_to_cv_ts;
  updateCharts();

  // Mode badge (header)
  const modeBadge = document.getElementById('mode-badge');
  if (modeBadge && d.system) {
    if (d.system.charger_configured) {
      const phaseLabels = {
        idle:   'Idle',
        detect: 'Detect',
        ramp:   'Ramp',
        CC:     'CC',
        CV:     'CV',
        done:   'Done',
        error:  'Error',
      };
      const ph = d.system.charger_phase || 'idle';
      const phLabel = phaseLabels[ph] || ph;
      modeBadge.textContent = 'Charger – ' + phLabel;
    } else {
      modeBadge.textContent = 'Zdroj';
    }
  }

  // PSU
  const psu = d.psu;
  if (psu) {
    setText('vout',     psu.vout  != null ? psu.vout.toFixed(2)+' V'  : '--');
    setText('iout',     psu.iout  != null ? psu.iout.toFixed(1)+' A'  : '--');
    setText('vin',      psu.vin   != null ? psu.vin.toFixed(0)+' V'   : '--');
    setText('temp_in',  psu.temp_in  != null ? psu.temp_in+' \u00b0C'  : '--');
    setText('temp_out', psu.temp_out != null ? psu.temp_out+' \u00b0C' : '--');
    setText('set_v',    psu.set_v != null ? psu.set_v.toFixed(2)+' V' : '--');
    setText('set_i',    psu.set_i != null ? psu.set_i.toFixed(1)+' A' : '--');
    const sm = {CV:['cv','CV'], CC:['cc','CC'], ALARM:['alarm','ALARM'], WALKIN:['walkin','Walk-in'],
                DETECT:['detect','Detect'], RAMP:['ramp','Ramp']};
    const s = sm[psu.status_key] || ['idle', psu.status || '--'];
    setHTML('psu-status-badge', badge(s[0], s[1]));
  }

  // CAN
  const canEl = document.getElementById('can-status');
  if (d.can_connected === true)  { canEl.textContent='CAN OK';  canEl.className='ok'; }
  if (d.can_connected === false) { canEl.textContent='CAN ERR'; canEl.className='err'; }

  // Charger
  const ch = d.charger;
  if (ch) {
    const pm = {
      CC:     ['cc',     'CC'],
      CV:     ['cv',     'CV'],
      detect: ['detect', '&#128269; Detect'],
      ramp:   ['ramp',   '&#8679; Ramp'],
      done:   ['done',   'Done'],
      error:  ['error',  'Error'],
      idle:   ['idle',   'Idle']
    };
    const ph = pm[ch.phase] || ['idle', ch.phase || '--'];
    setHTML('ch-phase', badge(ph[0], ph[1]));
    setText('ch-time',   ch.elapsed  || '--');
    setText('ch-actual-v', psu && psu.vout != null ? psu.vout.toFixed(2)+' V' : '--');
    setText('ch-actual-i', psu && psu.iout != null ? psu.iout.toFixed(1)+' A' : '--');
    setText('ch-ah',     ch.ah   != null ? ch.ah.toFixed(3)+' Ah' : '--');
    setText('ch-wh',     ch.wh   != null ? ch.wh.toFixed(3)+' Wh' : '--');
    setText('ch-target', ch.target_v != null ? ch.target_v.toFixed(2)+' V' : '--');
    setText('ch-tail',   ch.tail_i   != null ? ch.tail_i.toFixed(1)+' A'   : '--');
    let pct = 0;
    if (ch.phase === 'CV' || ch.phase === 'done') pct = 100;
    else if (ch.phase === 'CC' && ch.target_v && psu && psu.vout)
      pct = Math.min(99, Math.round(psu.vout / ch.target_v * 100));
    else if (ch.phase === 'ramp' && ch.ramp_v && ch.target_v)
      pct = Math.min(98, Math.round(ch.ramp_v / ch.target_v * 100));
    document.getElementById('ch-bar').style.width = pct + '%';
  }

  // Battery params (static config)
  const bat = d.battery;
  const batCard = document.getElementById('battery-card');
  if (bat && batCard) {
    batCard.style.display = '';
    setText('bat-cells',        bat.cell_count + ' cells');
    setText('bat-cell-v',       bat.cell_voltage_max != null ? bat.cell_voltage_max.toFixed(3)+' V' : '--');
    setText('bat-target',       bat.target_voltage   != null ? bat.target_voltage.toFixed(2)+' V'   : '--');
    setText('bat-capacity',     bat.capacity != null ? bat.capacity.toFixed(0)+' Ah' : '--');
    setText('bat-charge-i',     bat.charge_current   != null ? bat.charge_current.toFixed(1)+' A'   : '--');
    setText('bat-detect-v',     bat.detect_voltage   != null ? bat.detect_voltage.toFixed(1)+' V'   : '--');
    setText('bat-detect-i-cur', bat.detect_current   != null ? bat.detect_current.toFixed(2)+' A'   : '--');
    setText('bat-tail-i',       bat.tail_current     != null ? bat.tail_current.toFixed(1)+' A'     : '--');
    setText('bat-detect-thresh',bat.detect_threshold != null ? '+'+bat.detect_threshold.toFixed(1)+' V' : '--');
    setText('bat-ramp-step',    (bat.ramp_step_v != null && bat.ramp_step_int != null)
      ? bat.ramp_step_v.toFixed(2)+' V / '+bat.ramp_step_int.toFixed(0)+' s' : '--');
    setText('bat-detect-i',     bat.min_detect       != null ? bat.min_detect.toFixed(1)+' A'       : '--');
    setText('bat-time',         bat.time_limit_min   != null
      ? bat.time_limit_min+' min ('+( bat.time_limit_min/60).toFixed(1)+' h)' : '--');
    setText('bat-tol',          bat.v_tolerance != null ? bat.v_tolerance.toFixed(2)+' V' : '--');
    setText('bat-autostart',    bat.auto_start ? 'Yes' : 'No');
  }

  // System
  const sys = d.system;
  if (sys) {
    setText('sys-version',   sys.version || '--');
    setText('sys-uptime',    sys.uptime  || '--');
    setText('sys-can',       sys.can_connected ? 'Connected' : 'Disconnected');
    setText('sys-psu-count', sys.psu_count != null ? sys.psu_count : '--');
    setText('sys-charger',   sys.charger_configured ? 'Configured' : 'Not configured');
  }

  // Log – append only new lines
  if (d.log_lines && d.log_lines.length) {
    const box = document.getElementById('log-box');
    d.log_lines.forEach(l => {
      if (knownLogs.has(l)) return;
      knownLogs.add(l);
      const div = document.createElement('div');
      div.className = 'log-line' +
        (l.includes('ERROR')||l.includes('ALARM') ? ' err' : l.includes('WARN') ? ' warn' : '');
      div.textContent = l;
      box.appendChild(div);
    });
    while (box.children.length > 200) { box.removeChild(box.firstChild); }
    box.scrollTop = box.scrollHeight;
  }
}

// ── Forms ──────────────────────────────────────────────────────────
document.getElementById('set-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const v = parseFloat(document.getElementById('f-voltage').value);
  const i = parseFloat(document.getElementById('f-current').value);
  if (isNaN(v)||isNaN(i)) { showResult('set-result','Enter valid values',false); return; }
  if (!confirm(`Set ${v.toFixed(2)} V / ${i.toFixed(1)} A?`)) return;
  const r = await apiPost('/api/set', {voltage:v, current:i});
  showResult('set-result', r.message||(r.ok?'OK':'Error'), r.ok);
});

async function chargeStart() {
  if (!confirm('Start charging?')) return;
  const r = await apiPost('/api/charge/start', {});
  showResult('ch-result', r.message||(r.ok?'Started':'Error'), r.ok);
}
async function chargeStop() {
  if (!confirm('Stop charging?')) return;
  const r = await apiPost('/api/charge/stop', {});
  showResult('ch-result', r.message||(r.ok?'Stopped':'Error'), r.ok);
}
async function doStandby() {
  if (!confirm('Set PSU to Standby (__STANDBY_V__ V / __STANDBY_I__ A)?')) return;
  const r = await apiPost('/api/standby', {});
  showResult('set-result', r.message||(r.ok?'Standby nastaven':'Chyba'), r.ok);
}

async function apiPost(url, body) {
  try {
    const res = await fetch(url, {method:'POST',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
    return await res.json();
  } catch(e) { return {ok:false, message:String(e)}; }
}

function showResult(id, msg, ok) {
  const el = document.getElementById(id);
  el.textContent = msg;
  el.style.color = ok ? '#4caf50' : '#f44336';
  setTimeout(()=>{ el.textContent=''; }, 4000);
}
</script>
</body>
</html>
"""


class WebGUI:
    """
    Flask-based web dashboard for flatpack2.
    Runs in a dedicated daemon thread alongside CLI and CAN loops.
    """

    SSE_INTERVAL = 10.0

    def __init__(self, bus, cfg, history):
        self.bus         = bus
        self.cfg         = cfg
        self.history     = history
        self._start_time = time.time()
        self._log_buf    = collections.deque(maxlen=200)
        self._cc_to_cv_ts = None

        self.host       = cfg.get("webgui", "host")
        self.port       = cfg.getint("webgui", "port")
        self.log_access = cfg.getboolean("webgui", "log_access")

        self.app = Flask(__name__)
        self.app.logger.disabled = True
        import logging as _logging
        _logging.getLogger("werkzeug").disabled = True
        # Flask/Werkzeug may call logging.disable() internally which blocks all loggers.
        # Reset it to allow our flatpack2 logger to work normally.
        _logging.disable(_logging.NOTSET)

        self._register_routes()

    # ------------------------------------------------------------------
    # Log capture
    # ------------------------------------------------------------------

    def add_log_line(self, line):
        """Called by WebGUILogHandler to buffer log lines for SSE delivery."""
        self._log_buf.append(line)

    def mark_cc_to_cv(self):
        """Called when CC->CV transition occurs; records JS timestamp for graph annotation."""
        self._cc_to_cv_ts = time.time() * 1000

    # ------------------------------------------------------------------
    # Routes
    # ------------------------------------------------------------------

    def _register_routes(self):
        app = self.app

        @app.route("/")
        def index():
            if self.log_access:
                log.debug("[webgui] GET / 200")
            html = _DASHBOARD_HTML.replace("__VERSION__", VERSION)
            html = html.replace("__STANDBY_V__", "{:.1f}".format(STANDBY_VOLTAGE))
            html = html.replace("__STANDBY_I__", "{:.1f}".format(STANDBY_CURRENT))
            html = html.replace("__V_MIN__", "{:.1f}".format(PSU_V_MIN))
            html = html.replace("__V_MAX__", "{:.1f}".format(PSU_V_MAX))
            html = html.replace("__I_MAX__", "{:.1f}".format(PSU_I_MAX))
            return html

        @app.route("/favicon.svg")
        def favicon():
            return Response(_FAVICON_SVG, mimetype="image/svg+xml")

        @app.route("/events")
        def events():
            if self.log_access:
                log.debug("[webgui] GET /events (SSE open)")

            @stream_with_context
            def generate():
                while True:
                    data = self._build_payload()
                    yield "data: {}\n\n".format(_json.dumps(data))
                    time.sleep(self.SSE_INTERVAL)

            return Response(generate(), mimetype="text/event-stream",
                            headers={"Cache-Control": "no-cache",
                                     "X-Accel-Buffering": "no"})

        @app.route("/api/status")
        def api_status():
            if self.log_access:
                log.debug("[webgui] GET /api/status 200")
            return jsonify(self._build_payload())

        @app.route("/api/standby", methods=["POST"])
        def api_standby():
            ok  = self.bus.cmd_standby()
            msg = "Standby: {:.1f}V / {:.1f}A set".format(
                STANDBY_VOLTAGE, STANDBY_CURRENT) if ok else "CAN send error"
            if self.log_access:
                log.debug("[webgui] POST /api/standby {}".format(200 if ok else 500))
            return jsonify({"ok": ok, "message": msg}), (200 if ok else 500)

        @app.route("/api/set", methods=["POST"])
        def api_set():
            data = request.get_json(force=True) or {}
            try:
                voltage = float(data["voltage"])
                current = float(data["current"])
            except (KeyError, ValueError, TypeError):
                if self.log_access:
                    log.debug("[webgui] POST /api/set 400")
                return jsonify({"ok": False, "message": "Missing voltage or current"}), 400
            ok  = self.bus.cmd_set(voltage, current)
            msg = "Set {:.2f} V / {:.1f} A".format(voltage, current) if ok else "CAN send error"
            if self.log_access:
                log.debug("[webgui] POST /api/set {}".format(200 if ok else 500))
            return jsonify({"ok": ok, "message": msg}), (200 if ok else 500)

        @app.route("/api/charge/start", methods=["POST"])
        def api_charge_start():
            if self.bus.charger is None:
                return jsonify({"ok": False, "message": "Charger not configured"}), 400
            data    = request.get_json(force=True) or {}
            current = data.get("current")
            if current is not None:
                try:    current = float(current)
                except: current = None
            psu_ids = list(self.bus._id_map.keys())
            if not psu_ids:
                return jsonify({"ok": False, "message": "No PSU found"}), 400
            ok = self.bus.charger.start(psu_ids, current=current)
            if self.log_access:
                log.debug("[webgui] POST /api/charge/start {}".format(200 if ok else 500))
            return jsonify({"ok": ok, "message": "Charging started" if ok else "Start failed"})

        @app.route("/api/charge/stop", methods=["POST"])
        def api_charge_stop():
            if self.bus.charger is None:
                return jsonify({"ok": False, "message": "Charger not configured"}), 400
            self.bus.charger.stop("Web stop")
            if self.log_access:
                log.debug("[webgui] POST /api/charge/stop 200")
            return jsonify({"ok": True, "message": "Charging stopped"})

        @app.route("/api/history")
        def api_history():
            window   = request.args.get("window")
            window_s = int(window) if window and window.isdigit() else None
            data     = self.history.get(window_s=window_s)
            lines    = ["timestamp_ms,vout,iout,ah,wh"]
            for i, ts in enumerate(data["ts"]):
                lines.append("{},{},{},{},{}".format(
                    int(ts), data["vout"][i], data["iout"][i],
                    data["ah"][i], data["wh"][i]))
            if self.log_access:
                log.debug("[webgui] GET /api/history 200 ({} rows)".format(len(data["ts"])))
            return Response("\n".join(lines), mimetype="text/csv",
                            headers={"Content-Disposition":
                                     "attachment; filename=flatpack2_history.csv"})

    # ------------------------------------------------------------------
    # SSE payload
    # ------------------------------------------------------------------

    def _build_payload(self):
        bus = self.bus

        psu_data = None
        if bus.psus:
            shex = bus._id_map.get(1) or next(iter(bus.psus))
            psu  = bus.psus.get(shex)
            if psu:
                sk = {0x04: "CV", 0x08: "CC", 0x0C: "ALARM", 0x10: "WALKIN"}.get(psu.status, "UNKNOWN")
                psu_data = {
                    "vout": psu.vout, "iout": psu.iout, "vin": psu.vin,
                    "temp_in": psu.temp_in, "temp_out": psu.temp_out,
                    "status_key": sk, "status": psu.status_name(),
                    "set_v": psu.set_voltage, "set_i": psu.set_current,
                }

        ch_data = None
        if bus.charger:
            cs = bus.charger.state
            ch_data = {
                "phase":    cs.phase,
                "elapsed":  cs.elapsed_str,
                "ah":       round(cs.ah, 3),
                "wh":       round(cs.wh, 3),
                "target_v": bus.charger.cfg["target_voltage"],
                "tail_i":   bus.charger.cfg["charge_current_tail"],
                "ramp_v":   cs.ramp_voltage,
            }

        up_s   = int(time.time() - self._start_time)
        uptime = "{:d}:{:02d}:{:02d}".format(up_s // 3600, (up_s % 3600) // 60, up_s % 60)

        return {
            "psu":           psu_data,
            "charger":       ch_data,
            "can_connected": bus._connected,
            "cc_to_cv_ts":   self._cc_to_cv_ts,
            "history":       self.history.get(),
            "battery": self._build_battery_params(),
            "system": {
                "version":            VERSION,
                "uptime":             uptime,
                "can_connected":      bus._connected,
                "psu_count":          len(bus.psus),
                "charger_configured": bus.charger is not None,
                "charger_phase":      bus.charger.state.phase if bus.charger is not None else None,
            },
            "log_lines": list(self._log_buf)[-20:],
        }

    def _build_battery_params(self):
        """Return static battery config dict for SSE payload, or None if no charger."""
        if self.bus.charger is None:
            return None
        c = self.bus.charger.cfg
        return {
            "cell_count":        c["cell_count"],
            "cell_voltage_max":  c["cell_voltage_max"],
            "target_voltage":    c["target_voltage"],
            "capacity":          c["capacity"],
            "charge_current":    c["charge_current"],
            "detect_voltage":    c["detect_voltage"],
            "detect_current":    c["detect_current"],
            "detect_threshold":  c["detect_threshold"],
            "ramp_step_v":       c["ramp_step_voltage"],
            "ramp_step_int":     c["ramp_step_interval"],
            "tail_current":      c["charge_current_tail"],
            "min_detect":        c["min_current_detect"],
            "time_limit_min":    c["safety_time_limit"],
            "v_tolerance":       c["voltage_tolerance"],
            "auto_start":        c.get("auto_start", True),
        }

    # ------------------------------------------------------------------
    # Start
    # ------------------------------------------------------------------

    def start(self):
        t = threading.Thread(
            target=lambda: self.app.run(
                host=self.host, port=self.port,
                threaded=True, use_reloader=False, debug=False),
            daemon=True, name="webgui")
        t.start()
        log.info("Web-GUI started on http://{}:{}".format(self.host, self.port))
        print("[flatpack2] Web-GUI: http://{}:{}".format(self.host, self.port))


# ---------------------------------------------------------------------------
# Web-GUI log handler
# ---------------------------------------------------------------------------

class WebGUILogHandler(logging.Handler):
    """Captures log records and feeds them into WebGUI log buffer for live tail."""
    def __init__(self, webgui):
        super().__init__()
        self.webgui = webgui
        self.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))

    def emit(self, record):
        try:
            self.webgui.add_log_line(self.format(record))
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Signal handlers
# ---------------------------------------------------------------------------
_bus_ref    = None
_config_path = "flatpack2.conf"

def _handle_sigterm(signo, frame):
    log.info("SIGTERM received - shutting down")
    if _bus_ref:
        _bus_ref.stop()
        _bus_ref.disconnect()
    sys.exit(0)

def _handle_sighup(signo, frame):
    log.info("SIGHUP received - reloading config")
    # Reload log level from the actual config file used at startup
    cfg = load_config(_config_path)
    level = getattr(logging, cfg.get("logging", "loglevel").upper(), logging.INFO)
    log.setLevel(level)

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    global _bus_ref, _config_path

    parser = argparse.ArgumentParser(description="flatpack2 v{} - Eltek Flatpack2 CAN controller".format(VERSION))
    parser.add_argument("--daemon",  action="store_true", help="Run as background daemon")
    parser.add_argument("--config",  default="flatpack2.conf", help="Config file path")
    parser.add_argument("--version", action="version", version="flatpack2 v{}".format(VERSION))
    args = parser.parse_args()

    _config_path = args.config

    # Load config
    cfg = load_config(args.config)

    # Setup logging
    setup_logging(
        cfg.get("logging", "logfile"),
        cfg.get("logging", "loglevel"),
        cfg.getint("logging", "max_bytes"),
        cfg.getint("logging", "backup_count"),
    )
    log.info("flatpack2 v{} starting".format(VERSION))

    # Daemonize if requested
    daemon_enabled = args.daemon or cfg.getboolean("daemon", "enabled")
    if daemon_enabled:
        print("[flatpack2] Starting as daemon...")
        daemonize(
            cfg.get("daemon", "pidfile"),
            cfg.get("daemon", "user") or None,
            cfg.get("daemon", "group") or None,
        )
        # Remove console log handler in daemon mode
        for h in log.handlers[:]:
            if h.get_name() == "console":
                log.removeHandler(h)

    # Signal handlers
    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGHUP,  _handle_sighup)

    # Setup terminal
    terminal = None
    term_type = cfg.get("terminal", "type").lower()

    if term_type == "pty" or daemon_enabled:
        terminal = PTYTerminal(symlink=cfg.get("terminal", "pty_symlink"))
        terminal.open()
    # else: stdio (interactive)

    # Load per-PSU configs
    psu_configs = get_psu_configs(cfg)
    if psu_configs:
        log.info("PSU configs loaded: {}".format(
            ", ".join("[{}]".format(v["section"]) for v in psu_configs.values())))

    # Create and connect bus
    bus = FlatpackBus(cfg, psu_configs, terminal=terminal)
    _bus_ref = bus

    # Setup charger if configured
    charger_cfg = get_charger_config(cfg)
    if charger_cfg:
        bus.charger = Charger(bus, charger_cfg, bus._print)
        log.info("Charger configured: {} cells, target={:.2f}V, I={:.1f}A".format(
            charger_cfg["cell_count"],
            charger_cfg["target_voltage"],
            charger_cfg["charge_current"]))
        print("[flatpack2] Charger ready: {} cells x {:.3f}V = {:.2f}V target".format(
            charger_cfg["cell_count"],
            charger_cfg["cell_voltage_max"],
            charger_cfg["target_voltage"]))

    # Setup data history
    history     = DataHistory()
    bus.history = history

    # Setup Web-GUI if enabled
    webgui = None
    if cfg.getboolean("webgui", "enabled"):
        webgui      = WebGUI(bus, cfg, history)
        bus.webgui  = webgui
        webgui.start()

    if not bus.connect():
        print("[flatpack2] FATAL: Cannot connect to CAN adapter.")
        log.error("Cannot connect to CAN adapter")
        sys.exit(1)

    print("[flatpack2] CAN adapter connected, starting threads...")
    bus.start()
    bus.wait_for_discovery()

    # Auto-start charger if configured and enabled
    if bus.charger is not None and charger_cfg.get("auto_start", True):
        psu_ids = list(bus._id_map.keys())
        if psu_ids:
            log.info("Auto-start charging (auto_start=true)")
            print("[flatpack2] Auto-start: starting charging...")
            bus.charger.start(psu_ids)
        else:
            log.warning("Auto-start: no PSUs found, skipping")
            print("[flatpack2] Auto-start: no PSUs found, charging skipped")

    # Run CLI
    try:
        run_cli(bus, terminal=terminal)
    except Exception as e:
        log.error("CLI error: {}".format(e))
    finally:
        bus.stop()
        bus.disconnect()
        if terminal:
            terminal.close()
        log.info("flatpack2 stopped")

if __name__ == "__main__":
    main()
