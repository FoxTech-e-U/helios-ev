#!/usr/bin/env python3
"""
Helios ABB Terra AC Solar Charger Daemon
=========================================

Controls ABB Terra AC wallbox based on PV surplus from a Victron ESS system
(3x Multiplus, AC-In = Grid, AC-Out = House; wallbox currently on the house/
AC-Out side - see FORCE mode note below). Exclusively owns the RS485 bus for
the ABB (address 2) - reads AND writes directly via Modbus, and publishes its
own D-Bus service.

Why exclusive bus ownership:
  RS485 is half-duplex. Two independent processes accessing the same address
  caused collisions, corrupted reads, and repeated dbus-modbus-client crashes.
  This daemon is the ONLY process reading or writing the ABB's address.
  dbus-modbus-client is configured to ignore it (see install.sh).

Surplus source:
  Grid power comes from com.victronenergy.system (/Ac/Grid/Lx/Power), which
  reflects the real import/export at the Multiplus AC-In - i.e. after the ESS
  has already prioritised charging the battery. As long as free feed-in is
  allowed (no export limit configured), a genuine PV surplus shows up
  directly as grid export once the battery is full, with no need to read PV
  or battery values separately.

  surplus_w = -(grid_L1 + grid_L2 + grid_L3) + charging_w
  charge_a  = clamp(surplus_w / 230V / phases, MIN_CURRENT, MAX_CURRENT)

  This is inherently battery-safe: it only ever commands a current once the
  battery is already full and the extra is otherwise being exported. No
  special handling needed here.

Modes:
  IDLE        - No vehicle connected (State A)
  PV_WAIT     - Vehicle connected, waiting for PV surplus (hysteresis)
  PV_CHARGE   - Charging with PV surplus (6-16A dynamic)
  FORCE       - Force charging at max current (triggered by RFID/App/ChargerSync)

Force mode and the battery-drain problem:
  As long as the wallbox sits on the Multiplus AC-Out (house) side, a forced
  full-power charge (11kW at 16A) draws from wherever the ESS decides -
  normally the battery first, only falling back to the grid once the battery
  can't supply it. That's fine for the house's normal background load, but
  actively harmful for an EV charge session, so FORCE temporarily switches
  the ESS Battery-Life state to "Keep batteries charged" for the duration of
  the session (this sources everything from the grid instead), and restores
  the previous state when the session ends. Once the wallbox is moved to the
  grid side of the installation (planned), the battery becomes physically
  unreachable from the wallbox and this workaround can be removed - FORCE can
  then simply set MAX_CURRENT again.

  BATTERY_LIFE_KEEP_CHARGED must be filled in before this is active (see
  README) - the exact value for "Keep batteries charged" is firmware/
  installation-specific. Until it is set, FORCE behaves as before (no
  Battery-Life switching) - fill it in as soon as you know it.

  A small marker file (BATTERY_LIFE_STATE_FILE) records the previous
  Battery-Life value while it's overridden. If the daemon crashes or is
  restarted mid-FORCE-session before it could restore that value, the ESS
  would otherwise stay stuck in "Keep batteries charged" indefinitely. On
  every startup, the daemon checks for this marker and restores the saved
  value immediately if found.

Bugfixes vs. the earlier RTU-exclusive baseline:
  1. Restart no longer forces max current. Previously, restarting the daemon
     while a PV-managed session was already active made it look identical to
     an externally-triggered charge (daemon_started_charging defaults False
     on a fresh instance), so it jumped straight to FORCE/MAX_CURRENT. Now
     the daemon reads the ABB's real state once at startup and adopts
     PV_CHARGE directly if it's already charging, instead of guessing.
  2. Removed the one-shot "resume_sent" flag. It was meant to avoid retrying
     start_charging() every cycle once a fully-charged vehicle stopped
     drawing current, but could leave a session stuck waiting for a state
     change that never came (e.g. after a brief comms hiccup). The daemon
     now simply re-sends start_charging() every cycle while surplus is
     sufficient but the ABB isn't reporting STATE_CHARGING - the command is
     idempotent, so this is harmless.

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
import subprocess
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
MODBUS_PORT    = '/dev/ttyUSB0'
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

# Grid power: real import/export at the Multiplus AC-In, post battery-
# priority. Negative = export = genuine surplus (requires free feed-in).
GRID_SERVICE = 'com.victronenergy.system'
GRID_PATHS   = ['/Ac/Grid/L1/Power', '/Ac/Grid/L2/Power', '/Ac/Grid/L3/Power']

# ESS Battery-Life override used during FORCE mode (see docstring above).
# Fill in once known: dbus -y com.victronenergy.settings
#   /Settings/CGwacs/BatteryLife/State GetValue
# after manually setting "Keep batteries charged" in Remote Console once.
BATTERY_LIFE_SERVICE      = 'com.victronenergy.settings'
BATTERY_LIFE_PATH         = '/Settings/CGwacs/BatteryLife/State'
BATTERY_LIFE_KEEP_CHARGED = 9      # "Keep batteries charged" (ermittelt: vorher 10, danach 9)

# Marker file to survive a crash/restart mid-FORCE-session without leaving
# the ESS stuck in "Keep batteries charged" forever.
BATTERY_LIFE_STATE_FILE = '/data/helios-abb-terra-ac/.battery_life_saved'

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
# D-Bus helpers for reading/writing OTHER services - via CLI, since those
# are separate services we don't own.
# =============================================================================
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

def dbus_set(service, path, value):
    """Write a D-Bus value via CLI. Returns True on success."""
    try:
        result = subprocess.run(
            [DBUS_CMD, '-y', service, path, 'SetValue', str(value)],
            capture_output=True, text=True, timeout=5
        )
        return result.returncode == 0
    except Exception as e:
        log.debug(f"dbus_set {service} {path}: {e}")
        return False

def get_grid_power():
    """Return total grid power in W. Negative = export (surplus)."""
    total = 0.0
    for path in GRID_PATHS:
        p = dbus_get(GRID_SERVICE, path)
        if p is None:
            return None
        total += p
    return total

# =============================================================================
# Battery-Life override persistence (survives a crash mid-FORCE-session)
# =============================================================================
def save_battery_life_marker(value):
    try:
        os.makedirs(os.path.dirname(BATTERY_LIFE_STATE_FILE), exist_ok=True)
        with open(BATTERY_LIFE_STATE_FILE, 'w') as f:
            f.write(str(value))
    except Exception as e:
        log.warning(f"Could not write Battery-Life marker file: {e}")

def load_battery_life_marker():
    try:
        with open(BATTERY_LIFE_STATE_FILE) as f:
            return f.read().strip()
    except FileNotFoundError:
        return None
    except Exception as e:
        log.warning(f"Could not read Battery-Life marker file: {e}")
        return None

def clear_battery_life_marker():
    try:
        os.remove(BATTERY_LIFE_STATE_FILE)
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning(f"Could not remove Battery-Life marker file: {e}")

# =============================================================================
# Modbus helpers (exclusive bus access)
# =============================================================================
def _flush_serial(client):
    """Actively clear the serial buffers before a transaction. Without this,
    leftover garbage bytes from a failed transfer can corrupt the *next*
    transaction too (seen in logs as "Cleanup recv buffer before send") -
    one bad read cascading into several more."""
    try:
        if client.socket:
            client.socket.reset_input_buffer()
            client.socket.reset_output_buffer()
    except Exception:
        pass

def read_u32(client, reg, retries=5):
    """Read a 32-bit unsigned register (2x16bit big-endian). Retries on
    failure with increasing backoff - failures appear to come in short
    noise bursts rather than independent random packet loss, so a wider
    retry window (not just more attempts close together) matters here."""
    for attempt in range(retries):
        if attempt > 0:
            time.sleep(0.3 * attempt)  # 0.3, 0.6, 0.9, 1.2s - spans a longer window
        _flush_serial(client)
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
        _flush_serial(client)
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
        _flush_serial(client)
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
        self.initialized = False              # becomes True after first ABB read
        self.surplus_above_min_since = None   # timestamp when surplus exceeded min
        self.surplus_below_min_since = None   # timestamp when surplus dropped below min
        self.daemon_started_charging = False  # True if we sent the start command
        self.saved_battery_life_state = None  # previous Battery-Life value, while FORCE overrides it
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

    def _enter_force_battery_override(self):
        """Switch ESS Battery-Life to 'Keep charged' for the FORCE session,
        remembering the previous value so it can be restored afterwards."""
        if BATTERY_LIFE_KEEP_CHARGED is None:
            log.debug("BATTERY_LIFE_KEEP_CHARGED not configured - FORCE will not "
                      "override Battery-Life (may draw from the battery)")
            return
        if self.saved_battery_life_state is not None:
            return  # already overridden
        current_state = dbus_get(BATTERY_LIFE_SERVICE, BATTERY_LIFE_PATH)
        if current_state is None:
            log.warning("Could not read current Battery-Life state - not overriding")
            return
        self.saved_battery_life_state = current_state
        save_battery_life_marker(current_state)
        dbus_set(BATTERY_LIFE_SERVICE, BATTERY_LIFE_PATH, BATTERY_LIFE_KEEP_CHARGED)
        log.info(f"FORCE: Battery-Life → 'Keep charged' (was {current_state}) "
                 f"- forced charge will draw from grid, not battery")

    def _exit_force_battery_override(self):
        """Restore the Battery-Life value saved before FORCE started."""
        if self.saved_battery_life_state is None:
            return
        dbus_set(BATTERY_LIFE_SERVICE, BATTERY_LIFE_PATH, self.saved_battery_life_state)
        log.info(f"FORCE ended: Battery-Life restored to {self.saved_battery_life_state}")
        self.saved_battery_life_state = None
        clear_battery_life_marker()

    def _create_service(self):
        svc = VeDbusService('com.victronenergy.evcharger.abb_terra_ac_2', register=False)

        svc.add_path('/Mgmt/ProcessName', __file__)
        svc.add_path('/Mgmt/ProcessVersion', '2.3.0-exclusive-rtu')
        svc.add_path('/Mgmt/Connection', f'Modbus RTU {MODBUS_PORT}:{MODBUS_ADDRESS}')
        svc.add_path('/DeviceInstance', DEVICE_INSTANCE)
        svc.add_path('/ProductId', 0xB044)
        svc.add_path('/ProductName', 'ABB Terra AC Wallbox')
        svc.add_path('/Model', 'Terra AC 16A')
        svc.add_path('/Connected', 0)
        svc.add_path('/AllowedRoles', ['evcharger'])
        svc.add_path('/Role', 'evcharger')
        svc.add_path('/Position', 1)  # AC output (behind the Multiplus, in the house)
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

        val = read_u32(client, REG_MAX_CURRENT)
        if val is not None:
            svc['/MaxCurrent'] = round(val / 1000, 1)
            ok_any = True
        time.sleep(0.1)

        val = read_u32(client, REG_ERROR_CODE)
        if val is not None:
            svc['/ErrorCode'] = val
            ok_any = True
        time.sleep(0.1)

        status_raw = read_u32(client, REG_CHARGING_STATE)
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
        log.info(f"  Grid source:  {GRID_SERVICE} {GRID_PATHS}")
        log.info(f"  Start hysteresis: {START_HYSTERESIS_S}s")
        log.info(f"  Stop hysteresis:  {STOP_HYSTERESIS_S}s")
        log.info(f"  Poll interval:{POLL_INTERVAL}s")
        log.info("  Bus mode: EXCLUSIVE (dbus-modbus-client must ignore this address)")
        log.info("=" * 60)

        # --- Crash recovery: if a marker is present, a previous instance
        #     switched Battery-Life to "Keep charged" for FORCE and never
        #     got to restore it (crash / hard restart). Fix that now,
        #     before anything else. ---
        marker = load_battery_life_marker()
        if marker is not None:
            log.warning(f"Found leftover Battery-Life marker ({marker}) from an "
                        f"unclean shutdown during FORCE - restoring it now")
            dbus_set(BATTERY_LIFE_SERVICE, BATTERY_LIFE_PATH, marker)
            clear_battery_life_marker()

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

        # --- Startup safety (bugfix): adopt the ABB's real state instead of
        #     assuming a fresh IDLE/disconnected start. Without this, a
        #     daemon restart during an already-active PV session looked
        #     identical to an externally-triggered charge (see FORCE
        #     detection below) and jumped straight to MAX_CURRENT. ---
        if not self.initialized:
            self.initialized = True
            if state == STATE_CHARGING:
                log.info("Startup: ABB already charging → resuming PV_CHARGE "
                         "management (not forcing max current)")
                self.mode = Mode.PV_CHARGE
                self.daemon_started_charging = True
            elif state in (STATE_EV_PLUGGED_AUTH, STATE_EV_PLUGGED_READY, STATE_EV_READY):
                self.mode = Mode.PV_WAIT
            else:
                self.mode = Mode.IDLE

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
                if self.mode == Mode.FORCE:
                    self._exit_force_battery_override()
                self.mode = Mode.IDLE
                self.daemon_started_charging = False
                self.surplus_above_min_since = None
                self.surplus_below_min_since = None
            return

        # Vehicle is connected (state >= 1) ─────────────────────────────────

        # Detect externally triggered charging (RFID / App / ChargerSync)
        if state == STATE_CHARGING and not self.daemon_started_charging:
            if self.mode not in (Mode.FORCE,):
                log.info("External charge trigger detected (RFID/App/ChargerSync) → FORCE mode")
                self.mode = Mode.FORCE
                self._enter_force_battery_override()
                set_current(client, MAX_CURRENT)
                return

        # FORCE mode: full speed until vehicle unplugged or stopped externally
        if self.mode == Mode.FORCE:
            if state != STATE_CHARGING:
                log.info("Charging stopped externally → PV_WAIT")
                self._exit_force_battery_override()
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
                    self.surplus_below_min_since = None
            else:
                if self.surplus_above_min_since is not None:
                    log.debug("Surplus dropped below minimum, resetting hysteresis timer")
                self.surplus_above_min_since = None

        elif self.mode == Mode.PV_CHARGE:
            if target_a >= MIN_CURRENT:
                self.surplus_below_min_since = None
                current_a = self.service['/Current'] or 0
                if current_a == 0 or state != STATE_CHARGING:
                    # Not actually charging right now (paused, fully charged,
                    # or a brief comms hiccup) but surplus is sufficient -
                    # keep nudging. Idempotent, so no "already tried once"
                    # bookkeeping is needed (that was the earlier bug).
                    log.info(f"Nudging charge start at {target_a:.1f}A (surplus {surplus_w:.0f}W)")
                    set_current(client, target_a)
                    start_charging(client)
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
