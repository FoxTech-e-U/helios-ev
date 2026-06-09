#!/usr/bin/env python3
"""
Helios ABB Terra AC Solar Charger Daemon
=========================================

Controls ABB Terra AC wallbox based on PV surplus from Victron system.

Modes:
  IDLE        - No vehicle connected (State A)
  PV_WAIT     - Vehicle connected, waiting for PV surplus (hysteresis)
  PV_CHARGE   - Charging with PV surplus (6-16A dynamic)
  FORCE       - Force charging at max current (triggered by RFID/App/external)

PV Surplus logic:
  surplus_w = -(grid_L1 + grid_L2 + grid_L3)   # negative grid = export = surplus
  charge_a  = surplus_w / 230 / 3               # 3-phase
  charge_a  = clamp(MIN_CURRENT, MAX_CURRENT)
  If charge_a < MIN_CURRENT for HYSTERESIS_TIME → pause (set 0A)
  If charge_a >= MIN_CURRENT for HYSTERESIS_TIME → start/resume

Force mode:
  Triggered when charging starts externally (RFID tap, ABB app, etc.)
  Detected: State transitions to CHARGING but daemon did not initiate it
  Ends when vehicle is disconnected

Author: FoxTech e.U.
Repository: https://github.com/FoxTech-e-U/helios-abb-terra-ac
License: GPL-3.0
"""

import sys
import os
import time
import logging
import signal
import subprocess
from enum import Enum
from pymodbus.client.sync import ModbusSerialClient

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
POLL_INTERVAL  = 10         # seconds between control loop iterations
HYSTERESIS_S   = 60         # seconds surplus must be stable before acting
MODBUS_TIMEOUT_S = 120      # seconds - write to 0x4106 to keep ABB alive

# Victron D-Bus
GRID_SERVICE   = 'com.victronenergy.grid.cgwacs_ttyUSB0_mb1'
DBUS_CMD       = 'dbus'

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
REG_ACTIVE_POWER    = 0x401C   # RO U32 - active power (W)
REG_ENERGY          = 0x401E   # RO U32 - session energy (Wh)
REG_SET_CURRENT     = 0x4100   # WO U32 - set current limit (mA)
REG_START_STOP      = 0x4105   # WO U16 - 0=start, 1=stop
REG_COM_TIMEOUT     = 0x4106   # RW U16 - communication timeout (s)

# Charging State values (bits 6-0 of byte 0)
STATE_IDLE          = 0   # State A - no vehicle
STATE_EV_PLUGGED_AUTH = 1 # State B1 - plugged, pending auth
STATE_EV_PLUGGED_READY= 2 # State B2 - plugged, EVSE ready
STATE_EV_READY      = 3   # State C1 - EV ready, no PWM
STATE_CHARGING      = 4   # State C2 - charging

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
# D-Bus helper (uses dbus CLI - no gi.repository dependency)
# =============================================================================
def dbus_get(service, path):
    """Read a D-Bus value via CLI. Returns float or None."""
    try:
        result = subprocess.run(
            [DBUS_CMD, '-y', service, path, 'GetValue'],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            val = result.stdout.strip()
            # Output format: "208.0" or "value = 208.0"
            val = val.replace('value =', '').strip()
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
# Modbus helpers
# =============================================================================
def read_u32(client, reg):
    """Read a 32-bit unsigned register (2x16bit big-endian)."""
    r = client.read_holding_registers(reg, 2, unit=MODBUS_ADDRESS)
    if hasattr(r, 'registers') and len(r.registers) == 2:
        return (r.registers[0] << 16) | r.registers[1]
    return None

def write_u32(client, reg, value):
    """Write a 32-bit unsigned value (2x16bit big-endian)."""
    hi = (value >> 16) & 0xFFFF
    lo = value & 0xFFFF
    r = client.write_registers(reg, [hi, lo], unit=MODBUS_ADDRESS)
    return not r.isError() if hasattr(r, 'isError') else False

def write_u16(client, reg, value):
    """Write a single 16-bit register."""
    r = client.write_register(reg, value, unit=MODBUS_ADDRESS)
    return not r.isError() if hasattr(r, 'isError') else False

def get_charging_state(client):
    """Return charging state (bits 6-0 of low byte)."""
    val = read_u32(client, REG_CHARGING_STATE)
    if val is None:
        return None
    return val & 0x7F

def set_current(client, amps):
    """Set charging current in amps (will be clamped to 6-16A or 0 for pause)."""
    ma = int(amps * 1000)
    ok = write_u32(client, REG_SET_CURRENT, ma)
    if ok:
        log.debug(f"SetCurrent → {amps:.1f}A ({ma}mA)")
    else:
        log.warning(f"SetCurrent write failed")
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

# =============================================================================
# Main control daemon
# =============================================================================
class SolarCharger:
    def __init__(self):
        self.mode = Mode.IDLE
        self.surplus_above_min_since = None   # timestamp when surplus exceeded min
        self.surplus_below_min_since = None   # timestamp when surplus dropped below min
        self.daemon_started_charging = False  # True if we sent the start command
        self.last_keepalive = 0
        self.running = True

        signal.signal(signal.SIGTERM, self._shutdown)
        signal.signal(signal.SIGINT, self._shutdown)

    def _shutdown(self, *_):
        log.info("Shutdown signal received")
        self.running = False

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

    def calculate_target_current(self, grid_w):
        """
        Calculate target charging current based on grid power.
        grid_w > 0 = import (consuming from grid)
        grid_w < 0 = export (surplus PV)
        Returns target amps (float), or 0 if insufficient surplus.
        """
        surplus_w = -grid_w
        if surplus_w < MIN_POWER_W:
            return 0.0
        amps = surplus_w / VOLTAGE / PHASES
        return max(MIN_CURRENT, min(MAX_CURRENT, amps))

    def run(self):
        log.info("=" * 60)
        log.info("Helios ABB Terra AC Solar Charger Daemon starting")
        log.info(f"  Min current:  {MIN_CURRENT}A ({MIN_POWER_W:.0f}W)")
        log.info(f"  Max current:  {MAX_CURRENT}A ({MAX_CURRENT*PHASES*VOLTAGE:.0f}W)")
        log.info(f"  Hysteresis:   {HYSTERESIS_S}s")
        log.info(f"  Poll interval:{POLL_INTERVAL}s")
        log.info("=" * 60)

        client = None
        while self.running:
            try:
                # Reconnect if needed
                if client is None:
                    client = self.connect_modbus()
                    if client is None:
                        log.warning("Retrying Modbus connection in 30s...")
                        time.sleep(30)
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
                time.sleep(15)

            time.sleep(POLL_INTERVAL)

        log.info("Daemon stopped")

    def control_loop(self, client):
        now = time.time()

        # --- Read ABB state from D-Bus (non-invasive, no bus conflict) ---
        abb_status = dbus_get(
            'com.victronenergy.evcharger.abb_terra_ac_2', '/Status')
        if abb_status is None:
            log.warning("Could not read ABB status from D-Bus")
            return
        # Charging state is bits 6-0 of low byte
        state = int(abb_status) & 0x7F

        grid_w = get_grid_power()
        if grid_w is None:
            log.warning("Could not read grid power from D-Bus")
            return

        surplus_w = -grid_w
        target_a  = self.calculate_target_current(grid_w)

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
            log.info(f"[IDLE] Grid={grid_w:+.0f}W Surplus={surplus_w:.0f}W")
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
            log.debug(f"FORCE mode: charging at {MAX_CURRENT}A")
            set_current(client, MAX_CURRENT)
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
                             f"waiting {HYSTERESIS_S}s before starting...")
                elif now - self.surplus_above_min_since >= HYSTERESIS_S:
                    log.info(f"Surplus stable for {HYSTERESIS_S}s → starting PV charge "
                             f"at {target_a:.1f}A")
                    set_current(client, target_a)
                    start_charging(client)
                    self.daemon_started_charging = True
                    self.mode = Mode.PV_CHARGE
                    self.surplus_below_min_since = None
            else:
                # Surplus dropped, reset timer
                if self.surplus_above_min_since is not None:
                    log.debug("Surplus dropped below minimum, resetting hysteresis timer")
                self.surplus_above_min_since = None

        elif self.mode == Mode.PV_CHARGE:
            if target_a >= MIN_CURRENT:
                # Sufficient surplus → adjust current
                self.surplus_below_min_since = None
                current_a = dbus_get('com.victronenergy.evcharger.abb_terra_ac_2', '/Current') or 0
                # Only write if change is > 0.5A to avoid constant bus traffic
                if abs(target_a - current_a) > 0.5:
                    log.info(f"Adjusting charge current: {current_a:.1f}A → {target_a:.1f}A "
                             f"(surplus {surplus_w:.0f}W)")
                    set_current(client, target_a)
            else:
                # Insufficient surplus
                if self.surplus_below_min_since is None:
                    self.surplus_below_min_since = now
                    log.info(f"Surplus {surplus_w:.0f}W below minimum {MIN_POWER_W:.0f}W, "
                             f"will pause in {HYSTERESIS_S}s...")
                elif now - self.surplus_below_min_since >= HYSTERESIS_S:
                    log.info(f"Surplus below minimum for {HYSTERESIS_S}s → pausing charge")
                    set_current(client, 0)  # 0A = pause per ABB spec
                    self.mode = Mode.PV_WAIT
                    self.surplus_above_min_since = None
                    self.surplus_below_min_since = None
                    self.daemon_started_charging = False

        # Log status every cycle (read power from D-Bus, no extra Modbus traffic)
        power_w = dbus_get('com.victronenergy.evcharger.abb_terra_ac_2', '/Ac/Power') or 0
        log.info(f"[{self.mode.value}] State={state} Grid={grid_w:+.0f}W "
                 f"Surplus={surplus_w:.0f}W Target={target_a:.1f}A "
                 f"Charging={power_w:.0f}W")


# =============================================================================
# Entry point
# =============================================================================
if __name__ == '__main__':
    setup_logging()
    daemon = SolarCharger()
    daemon.run()
