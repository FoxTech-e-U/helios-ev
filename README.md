# ⚡ Helios EV

**ABB Terra AC Wallbox PV Surplus Charger for Victron Venus OS**

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)
[![Victron Venus OS](https://img.shields.io/badge/Victron-Venus%20OS%203.70+-blue)](https://www.victronenergy.com/)
[![ABB Terra AC](https://img.shields.io/badge/ABB-Terra%20AC-red)](https://new.abb.com/ev-charging)
[!["Buy Me A Coffee"](https://www.buymeacoffee.com/assets/img/custom_images/orange_img.png)](https://buymeacoffee.com/olli_foxtech)

Standalone daemon that integrates an ABB Terra AC wallbox into a Victron Energy GX
system and automatically charges an EV using PV surplus.

**`helios-abb-solar-charger.py`** does two jobs in one process:
1. Reads all ABB Terra data via Modbus RTU and publishes it to D-Bus
   (`com.victronenergy.evcharger.abb_terra_ac_2`) - the wallbox appears natively
   in the Victron dashboard and VRM Portal, with no `dbus-modbus-client` plugin needed
2. Controls charging current and start/stop based on available PV surplus

## ⚠️ Why this daemon owns the RS485 bus exclusively

Earlier versions of this project used a `dbus-modbus-client` plugin (`abb_terra.py`)
to read the ABB, with a separate daemon writing `SetCurrent`/`Start`/`Stop` commands.
Both processes accessed the same RS485 bus independently. RS485 is half-duplex:
simultaneous access from two processes caused packet collisions, corrupted reads,
and repeated `dbus-modbus-client` crashes (`Device failed: Error reading registers...`
in its logs).

**The fix: one process, exclusive bus ownership.** This daemon now reads AND writes
the ABB directly, and registers its own D-Bus service. `dbus-modbus-client` is
explicitly configured to never touch this Modbus address. `install.sh` handles this
automatically, including removing any leftover legacy plugin.

### If you have (or had) a Huawei inverter on the same RS485 cable

Some Huawei SUN2000 inverters keep transmitting on their RS485 port continuously,
even when nothing is actively polling them - for example after switching the
inverter to Modbus TCP (see [helios-victron](https://github.com/FoxTech-e-U/helios-victron)).
This unsolicited traffic corrupts communication for every other device sharing the
same physical wire, including a perfectly healthy ABB Terra setup.

**If you experience intermittent, hard-to-explain communication failures on a
shared RS485 bus, physically disconnect any Huawei RS485 wiring** - don't just
stop polling it in software. This was the root cause of extended instability in
earlier testing of this project.

## 🌟 Features

- ✅ **Native Victron Integration** — EV Charger in Device List and VRM, no separate plugin
- ✅ **Exclusive RS485 ownership** — eliminates the bus-collision class of failures entirely
- ✅ **PV Surplus Charging** — dynamic current control (6–16A) based on solar surplus
- ✅ **Force Charge Mode** — RFID tap or ABB app → charges at full power immediately
- ✅ **Smart Hysteresis** — 60s before starting, 300s before stopping (avoids rapid switching)
- ✅ **RFID Support** — vehicle plugged in waits for RFID authorization before PV charging
- ✅ **Fully-charged vehicle handling** — detects when the vehicle stops drawing current
  despite an active session (e.g. reached 100%) and stops retrying every cycle
- ✅ **Venus OS 3.70+** — compatible with read-only filesystem via `/data/` + rc.local

## 📋 Compatibility

### Tested Hardware
- **Wallbox**: ABB Terra AC 16A (W11-T-0)
- **GX Device**: Cerbo GX (Venus OS v3.70)
- **Interface**: dedicated RS485 bus (see note above if sharing with other devices)
- **Vehicle**: BMW iX3 G08

### ABB Terra AC Configuration (Terra Config App)
- **Mode**: Secondary (Local Controller → Modbus RTU)
- **Baud Rate**: 9600, 8N1
- **Address**: 2 (or your choice, configurable in `install.sh`)
- **Authorization**: RFID recommended (prevents immediate charging on plug-in)
- **Max Current**: 16A (set to your installation limit)

## 🔌 Hardware Connection

```
ABB Terra AC (RS485 terminals)     RS485-USB Adapter
┌──────────────┐                  ┌─────────────┐
│ A+           │──────────────────│ A / DATA+   │
│ B-           │──────────────────│ B / DATA-   │
│ GND          │──────────────────│ GND         │
└──────────────┘                  └─────────────┘
                                          │ USB
                                   Cerbo GX USB Port
```

If other RS485 devices need to share this bus, be aware that the daemon requires
**exclusive** access to the ABB's Modbus address - other devices at *different*
addresses on the same physical bus are fine as long as nothing else polls or writes
the ABB's address, and as long as none of the other devices transmits unsolicited
data (see the Huawei note above).

## 🚀 Installation

### One-line install (recommended)
```bash
wget -O /tmp/install.sh https://raw.githubusercontent.com/FoxTech-e-U/helios-ev/master/install.sh
bash /tmp/install.sh ttyUSB1 2
```
Arguments: `<serial device>` `<Modbus address>` (both optional, default `ttyUSB1` / `2`)

### What the installer does
1. Downloads `helios-abb-solar-charger.py` from GitHub (or uses local copy)
2. **Removes any legacy `abb_terra.py` `dbus-modbus-client` plugin**, if found
3. Configures `dbus-modbus-client` to exclude the ABB's Modbus address
4. Installs the daemon to `/data/helios-abb-terra-ac/` (survives firmware updates)
5. Verifies ABB connectivity before handing over exclusive access
6. Installs and starts a runit service
7. Adds `rc.local` entries to restore the bus exclusion and service after
   firmware updates

### Update to latest version
```bash
wget -O /tmp/install.sh https://raw.githubusercontent.com/FoxTech-e-U/helios-ev/master/install.sh
bash /tmp/install.sh ttyUSB1 2
```

## ⚙️ Configuration

Edit the top of `helios-abb-solar-charger.py` (or pass device/address to `install.sh`):

```python
MODBUS_PORT    = '/dev/ttyUSB1'                          # RS485 adapter
MODBUS_ADDRESS = 2                                        # ABB Terra address
MIN_CURRENT    = 6                                        # A (IEC 61851 minimum)
MAX_CURRENT    = 16                                       # A (your installation limit)
PHASES         = 3                                        # number of phases
START_HYSTERESIS_S = 60                                   # seconds before starting
STOP_HYSTERESIS_S  = 300                                  # seconds before stopping
POLL_INTERVAL  = 10                                       # seconds
GRID_SERVICE   = 'com.victronenergy.grid.cgwacs_ttyUSB0_mb1'  # your grid meter
```

To find your grid meter service:
```bash
dbus -y | grep grid
```

## 📊 Charging Modes

### PV Surplus Mode (automatic)
```
surplus_W  = -(grid_L1 + grid_L2 + grid_L3) + charging_W
charge_A   = surplus_W / 230V / 3 phases
charge_A   = clamp(6A, 16A)

surplus >= 4140W (6A×3ph) for 60s  → start charging
surplus <  4140W for 300s          → pause (stop session)
```

### Force Mode (RFID / App)
- Triggered when charging starts externally (RFID, ABB app)
- Charges at `MAX_CURRENT` until vehicle unplugged or charging stopped externally

### Fully-charged vehicle
If the vehicle reaches 100% while a charge session is active (state stays "EVSE
ready" but no current is drawn despite sufficient surplus), the daemon attempts
`start_charging()` exactly once, then waits quietly for a real state change
(unplug/replug, or the vehicle drawing current again) instead of retrying every
cycle.

### Charging State Reference
| ABB State (register 0x400C, low byte) | Meaning |
|----------------------------------------|---------|
| 0 | A — Idle, no vehicle |
| 1 | B1 — Vehicle plugged, pending RFID auth |
| 2 | B2 — Vehicle plugged, EVSE ready (PWM active) |
| 4 | C2 — Charging |
| 5 | Session stopped externally |

## 📊 Available D-Bus Data Points

Service: `com.victronenergy.evcharger.abb_terra_ac_2`

| Path | Unit | Description |
|------|------|-------------|
| `/Ac/Power` | W | Active charging power |
| `/Ac/L1/Current`, `L2`, `L3` | A | Phase currents |
| `/Ac/L1/Voltage`, `L2`, `L3` | V | Phase voltages |
| `/Ac/Energy/Forward` | kWh | Session energy |
| `/Current` | A | Active current limit |
| `/MaxCurrent` | A | Hardware maximum |
| `/Status` | - | Raw charging state register |
| `/ErrorCode` | - | Error code (0 = OK) |
| `/Connected` | - | 1 if the last poll cycle read at least one register successfully |

## 🔧 Monitoring & Troubleshooting

```bash
# Service status
svstat /service/helios-abb-solar-charger

# Live log
tail -f /var/log/helios-abb-solar-charger/current | tai64nlocal

# D-Bus values
dbus -y com.victronenergy.evcharger.abb_terra_ac_2 / GetValue

# Restart daemon
svc -t /service/helios-abb-solar-charger
```

### "Could not read ABB status via Modbus"
Occasional single occurrences (e.g. during manual interaction with the ABB app)
are normal and self-recover. If this repeats continuously:
1. Confirm nothing else is accessing this Modbus address — check for a
   `dbus-modbus-client` process still polling it:
   ```bash
   dbus -y com.victronenergy.settings /Settings/ModbusClient/ttyUSB1/Devices GetValue
   ```
   should NOT list your ABB's address.
2. Check for unsolicited traffic on the bus from another device (see the Huawei
   note above) — capture raw serial data with nothing sending requests:
   ```bash
   python3 -c "
   import serial, time
   s = serial.Serial('/dev/ttyUSB1', baudrate=9600, bytesize=8, parity='N', stopbits=1, timeout=2)
   print('Listening for 15s...')
   start = time.time(); data = b''
   while time.time() - start < 15:
       chunk = s.read(256)
       if chunk: data += chunk
   print(f'{len(data)} bytes received without any request sent')
   s.close()"
   ```
   Any non-zero byte count here means something is transmitting unsolicited data.

### ABB Terra not responding at all
Power-cycle the wallbox (circuit breaker off, 10s, back on).

### After Venus OS update
`rc.local` restores the service and re-excludes the ABB address from
`dbus-modbus-client` automatically on boot. If it doesn't come back, re-run
`install.sh`.

### Dashboard shows the wallbox as "offline" right after switching to this daemon
The Cerbo GUI / VRM Portal can cache the old device state briefly when migrating
from the `dbus-modbus-client` plugin. Toggling charging mode once in the ABB app,
or a hard refresh of the VRM Portal page, typically resolves this immediately.

## 🤝 Contributing

Contributions welcome! Especially:
- Testing with other ABB Terra AC models
- Testing with different vehicles (phase switching compatibility)
- Reports on shared-bus setups with other Modbus devices

## 📜 License

GPL-3.0 — see [LICENSE](LICENSE)

## 🙏 Acknowledgments

- Victron Energy for the open GX platform
- ABB for publishing the Modbus interface documentation
- [helios-victron](https://github.com/FoxTech-e-U/helios-victron) — sister project

## ⚠️ Disclaimer

Provided "as-is" without warranty. Use at your own risk.

## 📧 Support

- **Issues**: [GitHub Issues](https://github.com/FoxTech-e-U/helios-ev/issues)
- **Buy Me a Coffee**: [!["Buy Me A Coffee"](https://www.buymeacoffee.com/assets/img/custom_images/orange_img.png)](https://buymeacoffee.com/olli_foxtech)

---

**Named after Helios** ⚡ — sister project to [helios-victron](https://github.com/FoxTech-e-U/helios-victron)
