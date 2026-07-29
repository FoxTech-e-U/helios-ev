#!/usr/bin/env python3
"""
Helios ABB Terra AC Solar Charger Daemon
=========================================

Controls ABB Terra AC wallbox based on PV surplus from Victron system.
Exclusively owns the RS485 bus for the ABB (address 2) - reads AND writes
directly via Modbus, and publishes its own D-Bus service.

Why exclusive bus ownership:
  Previously, dbus-modbus-client (reading the ABB) and this daemon (writing
  SetCurrent/Start/Stop) both accessed the same RS485 bus independently.
  RS485 is half-duplex; simultaneous access from two processes caused
  collisions, corrupted reads, and repeated dbus-modbus-client crashes/
  restarts (visible as "Device failed: Error reading registers 0x4006-0x4009"
  in its logs, and "Could not read ABB status from D-Bus" here).
  A comparable Victron smartmeter on its own dedicated RS485 bus runs
  reliably - confirming the bus itself is fine, only concurrent access is
  the problem. This daemon now owns ttyUSB1 address 2 exclusively: it is
  the ONLY process reading or writing the ABB. dbus-modbus-client must be
  configured to ignore address 2 (see install.sh).

Modes:
  IDLE        - No vehicle connected (State A)
  PV_WAIT     - Vehicle connected, waiting for PV surplus (hysteresis)
  PV_CHARGE   - Charging with PV surplus (6-16A dynamic)
  FORCE       - Force charging at max current (triggered by RFID/App/external)

PV Surplus logic:
  surplus_w = -(grid_L1 + grid_L2 + grid_L3) + charging_w
  charge_a  = surplus_w / 230 / 3               # 3-phase
  charge_a  = clamp(MIN_CURRENT, MAX_CURRENT)
  If charge_a < MIN_CURRENT for STOP_HYSTERESIS_S  → pause (stop session)
  If charge_a >= MIN_CURRENT for START_HYSTERESIS_S → start/resume

Force mode:
  Triggered when charging starts externally (RFID tap, ABB app, etc.)
  Detected: State transitions to CHARGING but daemon did not initiate it
  Ends when vehicle is disconnected

Fully-charged vehicle handling:
  If the vehicle stops drawing current despite an active charge session
  (state B2, ready, but 0A actually flowing - e.g. vehicle reached 100%),
  the daemon attempts start_charging() exactly once, then waits quietly
  for a real state change instead of retrying every cycle.

Author: FoxTech e.U.
Repository: https://github.com/FoxTech-e-U/helios-ev
License: GPL-3.0
"""

import sys
import os
import time
import logging
import signal
import threading
from enum import Enum
from pymodbus.client.sync import ModbusSerialClient

sys.path.insert(0, '/opt/victronenergy/dbus-modbus-client')
sys.path.insert(0, '/opt/victronenergy/velib_python')

from vedbus import VeDbusService
import dbus
import dbus.mainloop.glib
from gi.repository import GLib

# =============================================================================
# Configuration
# =============================================================================

# RS485 device and Modbus address
MODBUS_PORT    = '/dev/ttyUSB1'
MODBUS_ADDRESS = 2          # ABB Terra AC default address
MODBUS_BAUD    = 9600

# Charging limits
MIN_CURRENT    = 6          # A - IEC 61851-1 minimum
MAX_CURRENT    = 16         # A - hardware limit of this installation
PHASES         = 3          # number of phases
VOLTAGE        = 230        # V per phase (nominal)
MIN_POWER_W    = MIN_CURRENT * PHASES * VOLTAGE   # ~4140W

# Control timing
POLL_INTERVAL      = 10     # seconds between control loop iterations
START_HYSTERESIS_S = 60     # seconds surplus must be stable before starting
STOP_HYSTERESIS_S  = 300    # seconds surplus must be below minimum before pausing
MODBUS_TIMEOUT_S   = 120    # seconds - write to 0x4106 to keep ABB alive

# Victron D-Bus
GRID_SERVICE     = 'com.victronenergy.grid.cgwacs_ttyUSB0_mb1'
DEVICE_INSTANCE  = 40

# Logging
LOG_FILE       = '/var/log/helios-abb-solar-charger.log'
LOG_LEVEL      = logging.INFO

# =============================================================================
# ABB Terra AC Modbus Registers
# =============================================================================
REG_MAX_CURRENT     = 0x4006   # RO U32 - max hardware current (mA)
REG_ERROR_CODE      = 0x4008   # RO U32 - error code
REG_SOCKET_LOCK     = 0x400A   # RO U32 - socket lock state
REG_CHARGING_STATE  = 0x400C   # RO U32 - charging state
REG_CURRENT_LIMIT   = 0x400E   # RO U32 - actual current limit (mA)
REG_CURRENT_L1      = 0x4010   # RO U32 - phase currents (mA)
REG_CURRENT_L2      = 0x4012
REG_CURRENT_L3      = 0x4014
REG_VOLTAGE_L1      = 0x4016   # RO U32 - phase voltages (0.1V)
REG_VOLTAGE_L2      = 0x4018
REG_VOLTAGE_L3      = 0x401A
REG_ACTIVE_POWER    = 0x401C   # RO U32 - active power (W)
REG_ENERGY          = 0x401E   # RO U32 - session energy (Wh)
REG_SET_CURRENT     = 0x4100   # WO U32 - set current limit (mA)
REG_START_STOP      = 0x4105   # WO U16 - 0=start, 1=stop
REG_COM_TIMEOUT     = 0x4106   # RW U16 - communication timeout (s)

# Charging State values (bits 6-0 of byte 0)
STATE_IDLE             = 0   # State A - no vehicle
STATE_EV_PLUGGED_AUTH  = 1   # State B1 - plugged, pending auth
STATE_EV_PLUGGED_READY = 2   # State B2 - plugged, EVSE ready
STATE_EV_READY         = 3   # State C1 - EV ready, no PWM
STATE_CHARGING         = 4   # State C2 - charging

# =============================================================================
# Daemon modes
# =============================================================================
class Mode(Enum):
    IDLE        = 'IDLE'
    PV_WAIT     = 'PV_WAIT'
    PV_CHARGE   = 'PV_CHARGE'
    FORCE       = 'FORCE'

# =============================================================================
# Logging setup
# =============================================================================
def setup_logging():
    handlers = [logging.StreamHandler(sys.stdout)]
    try:
        handlers.append(logging.FileHandler(LOG_FILE))
    except Exception:
        pass
    logging.basicConfig(
        level=LOG_LEVEL,
        format='%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
        handlers=handlers
    )

log = logging.getLogger(__name__)

# =============================================================================
# D-Bus helper for reading OTHER services (grid meter) - still via CLI,
# since that's a separate service we don't own.
# =============================================================================
import subprocess
DBUS_CMD = 'dbus'

def dbus_get(service, path):
    """Read a D-Bus value via CLI. Returns float or None."""
    try:
        result = subprocess.run(
            [DBUS_CMD, '-y', service, path, 'GetValue'],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            val = result.stdout.strip().replace('value =', '').strip()
            return float(val)
    except Exception as e:
        log.debug(f"dbus_get {service} {path}: {e}")
    return None

def get_grid_power():
    """Return total grid power in W. Negative = export (surplus)."""
    total = 0.0
    for phase in ['L1', 'L2', 'L3']:
        p = dbus_get(GRID_SERVICE, f'/Ac/{phase}/Power')
        if p is None:
            return None
        total += p
    return total

# =============================================================================
# Modbus helpers (exclusive bus access)
# =============================================================================
def read_u32(client, reg):
    """Read a 32-bit unsigned register (2x16bit big-endian)."""
    r = client.read_holding_registers(reg, 2, unit=MODBUS_ADDRESS)
    if hasattr(r, 'registers') and len(r.registers) == 2:
        return (r.registers[0] << 16) | r.registers[1]
    return None

def write_u32(client, reg, value, retries=3):
    """Write a 32-bit unsigned value (2x16bit big-endian). Retries on failure."""
    hi = (value >> 16) & 0xFFFF
    lo = value & 0xFFFF
    for attempt in range(retries):
        if attempt > 0:
            time.sleep(0.5)
        r = client.write_registers(reg, [hi, lo], unit=MODBUS_ADDRESS)
        if hasattr(r, 'isError') and not r.isError():
            return True
        if hasattr(r, 'function_code') and r.function_code < 0x80:
            return True
    return False

def write_u16(client, reg, value, retries=3):
    """Write a single 16-bit register. Retries on failure."""
    for attempt in range(retries):
        if attempt > 0:
            time.sleep(0.5)
        r = client.write_register(reg, value, unit=MODBUS_ADDRESS)
        if hasattr(r, 'isError') and not r.isError():
            return True
        if hasattr(r, 'function_code') and r.function_code < 0x80:
            return True
    return False

def set_current(client, amps):
    """Set charging current in amps (will be clamped to 6-16A or 0 for pause)."""
    ma = int(amps * 1000)
    ok = write_u32(client, REG_SET_CURRENT, ma)
    if ok:
        log.debug(f"SetCurrent → {amps:.1f}A ({ma}mA)")
    else:
        log.warning("SetCurrent write failed")
    return ok

def start_charging(client):
    """Send start command (register 0x4105 = 0)."""
    ok = write_u16(client, REG_START_STOP, 0)
    log.info(f"Start charging command → {'OK' if ok else 'FAILED'}")
    return ok

def stop_charging(client):
    """Send stop command (register 0x4105 = 1)."""
    ok = write_u16(client, REG_START_STOP, 1)
    log.info(f"Stop charging command → {'OK' if ok else 'FAILED'}")
    return ok

def keepalive(client):
    """Write communication timeout to prevent ABB from stopping due to silence."""
    write_u16(client, REG_COM_TIMEOUT, MODBUS_TIMEOUT_S)

def i32(val):
    if val > 0x7FFFFFFF:
        val -= 0x100000000
    return val

# =============================================================================
# Main control daemon
# =============================================================================
class SolarCharger:
    def __init__(self):
        self.mode = Mode.IDLE
        self.surplus_above_min_since = None   # timestamp when surplus exceeded min
        self.surplus_below_min_since = None   # timestamp when surplus dropped below min
        self.daemon_started_charging = False  # True if we sent the start command
        self.resume_sent = False              # True once we tried start_charging(), waiting for state change
        self.last_seen_state = None           # last observed charging state, to detect real changes
        self.last_keepalive = time.time()
        self.running = True

        signal.signal(signal.SIGTERM, self._shutdown)
        signal.signal(signal.SIGINT, self._shutdown)

        self.service = self._create_service()
        self.mainloop = GLib.MainLoop()

    def _shutdown(self, *_):
        log.info("Shutdown signal received")
        self.running = False
        self.mainloop.quit()

    def _create_service(self):
        svc = VeDbusService('com.victronenergy.evcharger.abb_terra_ac_2', register=False)

        svc.add_path('/Mgmt/ProcessName', __file__)
        svc.add_path('/Mgmt/ProcessVersion', '3.0-exclusive-rtu')
        svc.add_path('/Mgmt/Connection', f'Modbus RTU {MODBUS_PORT}:{MODBUS_ADDRESS}')
        svc.add_path('/DeviceInstance', DEVICE_INSTANCE)
        svc.add_path('/ProductId', 0xB044)
        svc.add_path('/ProductName', 'ABB Terra AC Wallbox')
        svc.add_path('/Model', 'Terra AC 16A')
        svc.add_path('/Connected', 0)
        svc.add_path('/AllowedRoles', ['evcharger'])
        svc.add_path('/Role', 'evcharger')
        svc.add_path('/Position', 1)  # AC output
        svc.add_path('/NrOfPhases', 3)

        svc.add_path('/MaxCurrent', None, gettextcallback=lambda p, v: f"{v:.1f} A" if v is not None else None)
        svc.add_path('/ErrorCode', None)
        svc.add_path('/Status', None)
        svc.add_path('/Current', None, gettextcallback=lambda p, v: f"{v:.1f} A" if v is not None else None)
        svc.add_path('/Ac/L1/Current', None)
        svc.add_path('/Ac/L2/Current', None)
        svc.add_path('/Ac/L3/Current', None)
        svc.add_path('/Ac/L1/Voltage', None)
        svc.add_path('/Ac/L2/Voltage', None)
        svc.add_path('/Ac/L3/Voltage', None)
        svc.add_path('/Ac/Power', None, gettextcallback=lambda p, v: f"{v:.0f} W" if v is not None else None)
        svc.add_path('/Ac/Energy/Forward', None, gettextcallback=lambda p, v: f"{v:.3f} kWh" if v is not None else None)

        svc.register()
        log.info("D-Bus service registered: com.victronenergy.evcharger.abb_terra_ac_2")
        return svc

    def connect_modbus(self):
        client = ModbusSerialClient(
            method='rtu',
            port=MODBUS_PORT,
            baudrate=MODBUS_BAUD,
            bytesize=8,
            parity='N',
            stopbits=1,
            timeout=3
        )
        if client.connect():
            log.info(f"Modbus connected: {MODBUS_PORT} @ {MODBUS_BAUD} baud, address {MODBUS_ADDRESS}")
            return client
        log.error(f"Modbus connection failed: {MODBUS_PORT}")
        return None

    def calculate_target_current(self, surplus_w):
        """
        Calculate target charging current based on available surplus.
        surplus_w > 0 = available surplus (W)
        Returns target amps (float), or 0 if insufficient surplus.
        """
        if surplus_w < MIN_POWER_W:
            return 0.0
        amps = surplus_w / VOLTAGE / PHASES
        return max(MIN_CURRENT, min(MAX_CURRENT, amps))

    def read_abb_data(self, client):
        """
        Read all ABB values directly via Modbus (exclusive bus access) and
        publish them to our own D-Bus service. Returns the charging state
        (0-4), or None if the read failed.
        """
        svc = self.service
        ok_any = False

        log.debug("Reading REG_MAX_CURRENT...")
        val = read_u32(client, REG_MAX_CURRENT)
        log.debug(f"REG_MAX_CURRENT = {val}")
        if val is not None:
            svc['/MaxCurrent'] = round(val / 1000, 1)
            ok_any = True
        time.sleep(0.1)

        log.debug("Reading REG_ERROR_CODE...")
        val = read_u32(client, REG_ERROR_CODE)
        log.debug(f"REG_ERROR_CODE = {val}")
        if val is not None:
            svc['/ErrorCode'] = val
            ok_any = True
        time.sleep(0.1)

        log.debug("Reading REG_CHARGING_STATE...")
        status_raw = read_u32(client, REG_CHARGING_STATE)
        log.debug(f"REG_CHARGING_STATE = {status_raw}")
        state = None
        if status_raw is not None:
            svc['/Status'] = status_raw
            state = status_raw & 0x7F
            ok_any = True
        time.sleep(0.1)

        val = read_u32(client, REG_CURRENT_LIMIT)
        if val is not None:
            svc['/Current'] = round(val / 1000, 1)
            ok_any = True
        time.sleep(0.1)

        val = read_u32(client, REG_CURRENT_L1)
        if val is not None:
            svc['/Ac/L1/Current'] = round(val / 1000, 2)
            ok_any = True
        time.sleep(0.1)

        val = read_u32(client, REG_CURRENT_L2)
        if val is not None:
            svc['/Ac/L2/Current'] = round(val / 1000, 2)
            ok_any = True
        time.sleep(0.1)

        val = read_u32(client, REG_CURRENT_L3)
        if val is not None:
            svc['/Ac/L3/Current'] = round(val / 1000, 2)
            ok_any = True
        time.sleep(0.1)

        val = read_u32(client, REG_VOLTAGE_L1)
        if val is not None:
            svc['/Ac/L1/Voltage'] = round(val / 10, 1)
            ok_any = True
        time.sleep(0.1)

        val = read_u32(client, REG_VOLTAGE_L2)
        if val is not None:
            svc['/Ac/L2/Voltage'] = round(val / 10, 1)
            ok_any = True
        time.sleep(0.1)

        val = read_u32(client, REG_VOLTAGE_L3)
        if val is not None:
            svc['/Ac/L3/Voltage'] = round(val / 10, 1)
            ok_any = True
        time.sleep(0.1)

        val = read_u32(client, REG_ACTIVE_POWER)
        if val is not None:
            svc['/Ac/Power'] = i32(val)
            ok_any = True
        time.sleep(0.1)

        val = read_u32(client, REG_ENERGY)
        if val is not None:
            svc['/Ac/Energy/Forward'] = round(val / 1000, 3)
            ok_any = True

        svc['/Connected'] = 1 if ok_any else 0
        return state

    def control_loop_thread(self):
        """Runs in a background thread. GLib mainloop stays free for D-Bus."""
        log.info("=" * 60)
        log.info("Helios ABB Terra AC Solar Charger Daemon starting")
        log.info(f"  Min current:  {MIN_CURRENT}A ({MIN_POWER_W:.0f}W)")
        log.info(f"  Max current:  {MAX_CURRENT}A ({MAX_CURRENT*PHASES*VOLTAGE:.0f}W)")
        log.info(f"  Start hysteresis: {START_HYSTERESIS_S}s")
        log.info(f"  Stop hysteresis:  {STOP_HYSTERESIS_S}s")
        log.info(f"  Poll interval:{POLL_INTERVAL}s")
        log.info("  Bus mode: EXCLUSIVE (dbus-modbus-client must ignore this address)")
        log.info("=" * 60)

        client = None
        while self.running:
            try:
                if client is None:
                    client = self.connect_modbus()
                    if client is None:
                        log.warning("Retrying Modbus connection in 30s...")
                        self._sleep_responsive(30)
                        continue

                self.control_loop(client)

            except Exception as e:
                log.error(f"Control loop error: {e}", exc_info=True)
                if client:
                    try:
                        client.close()
                    except Exception:
                        pass
                client = None
                self._sleep_responsive(15)
                continue

            self._sleep_responsive(POLL_INTERVAL)

        log.info("Daemon stopped")

    def _sleep_responsive(self, seconds):
        """Sleep in small steps so shutdown is responsive."""
        remaining = seconds
        while remaining > 0 and self.running:
            step = min(0.5, remaining)
            time.sleep(step)
            remaining -= step

    def run(self):
        control_thread = threading.Thread(target=self.control_loop_thread, daemon=True)
        control_thread.start()

        self.mainloop.run()

    def control_loop(self, client):
        now = time.time()

        # --- Read ABB state directly via Modbus (exclusive bus access) ---
        state = self.read_abb_data(client)
        if state is None:
            log.warning("Could not read ABB status via Modbus")
            return

        grid_w = get_grid_power()
        if grid_w is None:
            log.warning("Could not read grid power from D-Bus")
            return

        charging_w = self.service['/Ac/Power'] or 0
        surplus_w = -grid_w + charging_w
        target_a  = self.calculate_target_current(surplus_w)

        log.debug(f"State={state} Mode={self.mode.value} Grid={grid_w:.0f}W "
                  f"Surplus={surplus_w:.0f}W Target={target_a:.1f}A")

        # --- Keepalive ---
        if now - self.last_keepalive > MODBUS_TIMEOUT_S / 2:
            keepalive(client)
            self.last_keepalive = now

        # --- Mode transitions ---

        # IDLE: vehicle not connected
        if state == STATE_IDLE:
            if self.mode != Mode.IDLE:
                log.info("Vehicle disconnected → IDLE")
                self.mode = Mode.IDLE
                self.daemon_started_charging = False
                self.surplus_above_min_since = None
                self.surplus_below_min_since = None
            return

        # Vehicle is connected (state >= 1) ─────────────────────────────────

        # Detect externally triggered charging (RFID / App)
        if state == STATE_CHARGING and not self.daemon_started_charging:
            if self.mode not in (Mode.FORCE,):
                log.info("External charge trigger detected (RFID/App) → FORCE mode")
                self.mode = Mode.FORCE
                set_current(client, MAX_CURRENT)
                return

        # FORCE mode: full speed until vehicle unplugged
        if self.mode == Mode.FORCE:
            if state != STATE_CHARGING:
                log.info("Charging stopped externally → PV_WAIT")
                self.mode = Mode.PV_WAIT
                self.daemon_started_charging = False
                self.surplus_above_min_since = None
            else:
                log.info(f"[FORCE] Charging at {MAX_CURRENT}A")
            return

        # PV modes ────────────────────────────────────────────────────────────

        if self.mode == Mode.IDLE:
            log.info("Vehicle connected → PV_WAIT")
            self.mode = Mode.PV_WAIT
            self.surplus_above_min_since = None
            self.surplus_below_min_since = None

        if self.mode == Mode.PV_WAIT:
            if target_a >= MIN_CURRENT:
                if self.surplus_above_min_since is None:
                    self.surplus_above_min_since = now
                    log.info(f"PV surplus {surplus_w:.0f}W detected, "
                             f"waiting {START_HYSTERESIS_S}s before starting...")
                elif now - self.surplus_above_min_since >= START_HYSTERESIS_S:
                    log.info(f"Surplus stable for {START_HYSTERESIS_S}s → starting PV charge "
                             f"at {target_a:.1f}A")
                    set_current(client, target_a)
                    start_charging(client)
                    self.daemon_started_charging = True
                    self.mode = Mode.PV_CHARGE
                    self.resume_sent = True  # we just sent start_charging() above
                    self.last_seen_state = state
                    self.surplus_below_min_since = None
            else:
                if self.surplus_above_min_since is not None:
                    log.debug("Surplus dropped below minimum, resetting hysteresis timer")
                self.surplus_above_min_since = None

        elif self.mode == Mode.PV_CHARGE:
            # Detect a real state change (vehicle starts drawing current,
            # or gets unplugged/replugged) - this re-arms resume attempts.
            if state != self.last_seen_state:
                self.resume_sent = False
                self.last_seen_state = state

            if target_a >= MIN_CURRENT:
                self.surplus_below_min_since = None
                current_a = self.service['/Current'] or 0
                if current_a == 0:
                    if not self.resume_sent:
                        log.info(f"Resuming charge at {target_a:.1f}A (surplus {surplus_w:.0f}W)")
                        set_current(client, target_a)
                        start_charging(client)
                        self.resume_sent = True
                    else:
                        log.debug(f"Waiting for vehicle (state={state}, no current drawn, "
                                  f"already attempted resume)")
                elif abs(target_a - current_a) > 0.5:
                    log.info(f"Adjusting charge current: {current_a:.1f}A → {target_a:.1f}A "
                             f"(surplus {surplus_w:.0f}W)")
                    set_current(client, target_a)
            else:
                if self.surplus_below_min_since is None:
                    self.surplus_below_min_since = now
                    log.info(f"Surplus {surplus_w:.0f}W below minimum {MIN_POWER_W:.0f}W, "
                             f"will pause in {STOP_HYSTERESIS_S}s...")
                elif now - self.surplus_below_min_since >= STOP_HYSTERESIS_S:
                    log.info(f"Surplus below minimum for {STOP_HYSTERESIS_S}s → pausing charge")
                    stop_charging(client)
                    self.mode = Mode.PV_WAIT
                    self.surplus_above_min_since = None
                    self.surplus_below_min_since = None
                    self.daemon_started_charging = False

        power_w = self.service['/Ac/Power'] or 0
        log.info(f"[{self.mode.value}] State={state} Grid={grid_w:+.0f}W "
                 f"Surplus={surplus_w:.0f}W Target={target_a:.1f}A "
                 f"Charging={power_w:.0f}W")


# =============================================================================
# Entry point
# =============================================================================
if __name__ == '__main__':
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    setup_logging()
    daemon = SolarCharger()
    daemon.run()
