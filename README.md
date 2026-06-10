# ⚡ Helios EV

**ABB Terra AC Wallbox Integration & PV Surplus Charger for Victron Venus OS**

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)
[![Victron Venus OS](https://img.shields.io/badge/Victron-Venus%20OS%203.70+-blue)](https://www.victronenergy.com/)
[![ABB Terra AC](https://img.shields.io/badge/ABB-Terra%20AC-red)](https://new.abb.com/ev-charging)
[!["Buy Me A Coffee"](https://www.buymeacoffee.com/assets/img/custom_images/orange_img.png)](https://buymeacoffee.com/olli_foxtech)

Two-component integration for ABB Terra AC wallboxes on Victron Energy GX devices:

1. **`abb_terra.py`** — dbus-modbus-client plugin: exposes the wallbox as an EV Charger in the Victron dashboard and VRM portal
2. **`helios-abb-solar-charger.py`** — PV surplus charging daemon: automatically charges your EV using solar surplus

## 🌟 Features

- ✅ **Native Victron Integration** — EV Charger in Device List and VRM
- ✅ **PV Surplus Charging** — dynamic current control (6–16A) based on solar surplus
- ✅ **Force Charge Mode** — RFID tap or ABB app → charges at full power immediately
- ✅ **Smart Hysteresis** — 60s delay before starting/stopping to avoid rapid switching
- ✅ **RFID Support** — vehicle plugged in waits for RFID authorization before PV charging
- ✅ **D-Bus Native** — reads sensor data from Victron D-Bus, minimal bus traffic
- ✅ **Shared RS485 Bus** — works alongside other Modbus devices (e.g. Huawei SUN2000)
- ✅ **Venus OS 3.70+** — compatible with read-only filesystem via symlink + rc.local

## 📋 Compatibility

### Tested Hardware
- **Wallbox**: ABB Terra AC 16A (W11-T-0)
- **GX Device**: Cerbo GX (Venus OS v3.70)
- **Interface**: RS485 shared bus with Huawei SUN2000
- **Vehicle**: BMW iX3 G08

### ABB Terra AC Configuration (Terra Config App)
- **Mode**: Secondary (Local Controller → Modbus RTU)
- **Baud Rate**: 9600, 8N1
- **Address**: 2
- **Authorization**: RFID recommended (prevents immediate charging on plug-in)
- **Max Current**: 16A (set to your installation limit)

## ⚠️ Important Notes

### Venus OS 3.70+ compatibility
Venus OS 3.70 introduced a **read-only root filesystem**. The install script handles this automatically:
1. Plugin stored in `/data/helios-ev/` (persistent, survives updates)
2. Symlink from `/opt/victronenergy/dbus-modbus-client/` to `/data/`
3. `import abb_terra` patched into `dbus-modbus-client.py`
4. `rc.local` restores everything after every firmware update

### RFID configuration
With RFID authorization enabled:
- Vehicle plugged in (no RFID) → **PV surplus mode** (waits for solar)
- RFID tap → **Force mode** (charges at MaxCurrent immediately)
- App start → **Force mode** (detected automatically)

Without RFID: every plug-in triggers Force mode immediately.

## 🔌 Hardware Connection

### Wiring (Shared RS485 Bus)
```
Huawei SUN2000     ABB Terra AC        RS485-USB Adapter
(COM Port)         (RS485 terminals)    (Cerbo GX USB)
┌──────────┐      ┌──────────────┐    ┌─────────────┐
│ Pin7: A+ │──────│ A+           │────│ A / DATA+   │
│ Pin9: B- │──────│ B-           │────│ B / DATA-   │
│ Pin5:GND │──────│ GND          │────│ GND         │
└──────────┘      └──────────────┘    └─────────────┘
  Addr 1            Addr 2
```

## 🚀 Installation

### One-line install (recommended)
```bash
wget -O /tmp/install.sh https://raw.githubusercontent.com/FoxTech-e-U/helios-ev/master/install.sh
bash /tmp/install.sh
```

### Update to latest version
```bash
wget -O /tmp/install.sh https://raw.githubusercontent.com/FoxTech-e-U/helios-ev/master/install.sh
bash /tmp/install.sh
```

### What the installer does
1. Downloads `abb_terra.py` and `helios-abb-solar-charger.py` from GitHub
2. Installs `abb_terra.py` to `/data/helios-ev/` (survives firmware updates)
3. Creates symlink in `/opt/victronenergy/dbus-modbus-client/`
4. Patches `import abb_terra` into `dbus-modbus-client.py`
5. Adds `rc.local` entries to auto-restore everything after firmware updates
6. Installs solar charger daemon to `/data/helios-abb-terra-ac/`
7. Installs and starts runit service

## ⚙️ Configuration

Edit the configuration at the top of `helios-abb-solar-charger.py`:

```python
MODBUS_PORT    = '/dev/ttyUSB1'                          # RS485 adapter
MODBUS_ADDRESS = 2                                        # ABB Terra address
MIN_CURRENT    = 6                                        # A (IEC 61851 minimum)
MAX_CURRENT    = 16                                       # A (your installation limit)
PHASES         = 3                                        # number of phases
HYSTERESIS_S   = 60                                       # seconds
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
surplus_W  = -(grid_L1 + grid_L2 + grid_L3)
charge_A   = surplus_W / 230V / 3 phases
charge_A   = clamp(6A, 16A)

surplus >= 4140W (6A×3ph) for 60s → start charging
surplus <  4140W for 60s           → pause (0A per IEC 61851)
```

### Force Mode (RFID / App)
- Triggered when charging starts externally (RFID, ABB app)
- Charges at `MAX_CURRENT` until vehicle unplugged or charging stopped

### Charging State Reference
| D-Bus `/Status` (byte 1) | State | Description |
|--------------------------|-------|-------------|
| 0 | A | Idle, no vehicle |
| 1 | B1 | Vehicle plugged, pending RFID auth |
| 2 | B2 | Vehicle plugged, EVSE ready |
| 4 | C2 | Charging |
| 5 | Other | Stopped externally |

## 📊 Available D-Bus Data Points

`com.victronenergy.evcharger.abb_terra_ac_2`:

| Path | Unit | Description |
|------|------|-------------|
| `/Ac/Power` | W | Active charging power |
| `/Ac/L1/Current` | A | Phase L1 current |
| `/Ac/L2/Current` | A | Phase L2 current |
| `/Ac/L3/Current` | A | Phase L3 current |
| `/Ac/L1/Voltage` | V | Phase L1 voltage |
| `/Ac/Energy/Forward` | kWh | Session energy |
| `/Current` | A | Active current limit |
| `/MaxCurrent` | A | Hardware maximum |
| `/Status` | - | Charging state |
| `/ErrorCode` | - | Error (0=OK) |

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

### ABB Terra not responding
Power-cycle the wallbox (circuit breaker off, 10s, back on).

### After Venus OS update
Re-run `install.sh` — or `rc.local` will auto-restore on next reboot.

## 🤝 Contributing

Contributions welcome! Especially:
- Testing with other ABB Terra AC models
- Testing with different vehicles (phase switching compatibility)

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

