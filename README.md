# ⚡ Helios ABB Terra AC

**ABB Terra AC Wallbox Integration & PV Surplus Charger for Victron Venus OS**

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)
[![Victron Venus OS](https://img.shields.io/badge/Victron-Venus%20OS-blue)](https://www.victronenergy.com/)
[![ABB Terra AC](https://img.shields.io/badge/ABB-Terra%20AC-red)](https://new.abb.com/ev-charging)
[!["Buy Me A Coffee"](https://www.buymeacoffee.com/assets/img/custom_images/orange_img.png)](https://buymeacoffee.com/olli_foxtech)

Two-component integration for ABB Terra AC wallboxes on Victron Energy GX devices:

1. **`abb_terra.py`** — dbus-modbus-client plugin: exposes the wallbox as an EV Charger in the Victron dashboard and VRM portal
2. **`helios-abb-solar-charger.py`** — PV surplus charging daemon: automatically charges your EV when solar production exceeds household consumption

## 🌟 Features

- ✅ **Native Victron Integration** — appears as EV Charger in Device List and VRM
- ✅ **PV Surplus Charging** — dynamic current control based on available solar surplus
- ✅ **Force Charge Mode** — RFID tap or ABB app start → charges at full power immediately
- ✅ **Smart Hysteresis** — 60s delay before starting/stopping to avoid rapid switching
- ✅ **D-Bus Native** — reads all sensor data from Victron D-Bus, minimal bus traffic
- ✅ **Shared RS485 Bus** — works alongside other Modbus devices (e.g. Huawei SUN2000)
- ✅ **Automatic Recovery** — reconnects after communication errors

## 📋 Compatibility

### Tested Hardware
- **Wallbox**: ABB Terra AC 16A (W11-T-0)
- **GX Device**: Cerbo GX (Venus OS v3.67)
- **Interface**: RS485 shared bus (shared with Huawei SUN2000)
- **PV Inverter**: Huawei SUN2000-8KTL-M1 (via [helios-victron](https://github.com/FoxTech-e-U/helios-victron))

### Potentially Compatible Models
All ABB Terra AC models that support Modbus RTU as secondary device:
Terra AC 6A, 7A, 11A, 16A, 22A, 32A

## 🔌 Hardware Connection

### Wiring (Shared RS485 Bus)
```
Huawei SUN2000          ABB Terra AC            RS485-USB Adapter
(COM Port)              (RS485 terminals)        (Cerbo GX USB)
┌──────────┐           ┌──────────────┐         ┌─────────────┐
│ Pin7: A+ │───────────│ A+           │─────────│ A / DATA+   │
│ Pin9: B- │───────────│ B-           │─────────│ B / DATA-   │
│ Pin5:GND │───────────│ GND          │─────────│ GND         │
└──────────┘           └──────────────┘         └─────────────┘
  Modbus addr 1          Modbus addr 2
```

### ABB Terra AC Configuration
Via **Terra Config app** → Communication Settings:
- **Mode**: Secondary (controlled by external system)
- **Baud Rate**: 9600
- **Parity**: None
- **Stop Bits**: 1
- **Address**: 2

## 🚀 Installation

### Quick Installation

```bash
# Download repository
wget https://github.com/FoxTech-e-U/helios-abb-terra-ac/archive/refs/heads/master.zip
unzip master.zip
cd helios-abb-terra-ac-master

# Run installer
chmod +x install.sh
./install.sh
```

The script will:
- Install `abb_terra.py` to the Victron dbus-modbus-client directory
- Configure Modbus settings for the ABB Terra (address 2)
- Install and start the PV surplus charging daemon as a runit service

### Manual Installation

#### 1. Install dbus-modbus-client plugin

```bash
cp abb_terra.py /opt/victronenergy/dbus-modbus-client/
chmod 644 /opt/victronenergy/dbus-modbus-client/abb_terra.py

# Add ABB address to Modbus config (adjust ttyUSBX as needed)
# If Huawei is already on address 1:
dbus -y com.victronenergy.settings /Settings/ModbusClient/ttyUSB1/Devices \
    SetValue "rtu:ttyUSB1:9600:1,rtu:ttyUSB1:9600:2"

svc -t /service/serial-starter
sleep 35
dbus -y | grep abb_terra
```

#### 2. Install PV surplus charging daemon

```bash
mkdir -p /data/helios-abb-terra-ac
cp helios-abb-solar-charger.py /data/helios-abb-terra-ac/

# Create runit service
mkdir -p /service/helios-abb-solar-charger/log
cat > /service/helios-abb-solar-charger/run << 'RUNEOF'
#!/bin/sh
exec /usr/bin/python3 -u /data/helios-abb-terra-ac/helios-abb-solar-charger.py 2>&1
RUNEOF
cat > /service/helios-abb-solar-charger/log/run << 'LOGEOF'
#!/bin/sh
exec multilog t s25000 n4 /var/log/helios-abb-solar-charger
LOGEOF
chmod 755 /service/helios-abb-solar-charger/run
chmod 755 /service/helios-abb-solar-charger/log/run
```

## ⚙️ Configuration

Edit the configuration section at the top of `helios-abb-solar-charger.py`:

```python
MODBUS_PORT    = '/dev/ttyUSB1'   # RS485 adapter device
MODBUS_ADDRESS = 2                # ABB Terra Modbus address
MIN_CURRENT    = 6                # A - IEC 61851-1 minimum (do not change)
MAX_CURRENT    = 16               # A - your installation limit
PHASES         = 3                # number of phases
HYSTERESIS_S   = 60               # seconds before starting/stopping
POLL_INTERVAL  = 10               # seconds between control loop runs
GRID_SERVICE   = 'com.victronenergy.grid.cgwacs_ttyUSB0_mb1'  # your grid meter
```

To find your grid meter service name:
```bash
dbus -y | grep grid
```

## 📊 Charging Modes

### PV Surplus Mode (automatic)
When a vehicle is plugged in without RFID/app trigger:

```
surplus_W  = -(grid_L1 + grid_L2 + grid_L3)   # negative grid = export
charge_A   = surplus_W / 230V / 3 phases
charge_A   = clamp(6A, 16A)

surplus >= 4140W (6A × 3ph) for 60s → start charging
surplus <  4140W for 60s           → pause (set 0A per IEC 61851)
```

### Force Mode (RFID / ABB App)
When charging is triggered externally (RFID tap, ABB app):
- Detected automatically: charging state changes without daemon initiating it
- Charges at `MAX_CURRENT` (16A) immediately
- Ends when vehicle is unplugged

### Charging State Reference
| D-Bus `/Status` | State | Description |
|-----------------|-------|-------------|
| 0 / 32768 | A | Idle, no vehicle |
| 1 | B1 | Vehicle plugged, pending auth |
| 2 | B2 | Vehicle plugged, EVSE ready |
| 3 | C1 | EV ready, no PWM |
| 4 | C2 | Charging |

## 📊 Available D-Bus Data Points

After installation, the ABB Terra appears as `com.victronenergy.evcharger.abb_terra_ac_2`:

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
| `/ErrorCode` | - | Error code (0=OK) |

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
The ABB Terra can hang internally after power fluctuations or heavy bus traffic.
Power-cycle the wallbox (circuit breaker off, 10s, back on).

### After Venus OS update
Re-run `install.sh` to restore the runit service (daemon files in `/data/` persist).

## 🤝 Contributing

Contributions welcome! Especially:
- Testing with other ABB Terra AC models
- Testing with different Victron GX devices
- Single-phase fallback logic improvements

## 📜 License

GPL-3.0 License — see [LICENSE](LICENSE) for details.

## 🙏 Acknowledgments

- Victron Energy for the open GX platform
- ABB for publishing the Modbus interface documentation
- [helios-victron](https://github.com/FoxTech-e-U/helios-victron) — sister project for Huawei SUN2000 integration

## ⚠️ Disclaimer

This software is provided "as-is" without warranty. Use at your own risk.
The author is not responsible for any damage to equipment, vehicles, or loss of data.

## 📧 Support

- **Issues**: [GitHub Issues](https://github.com/FoxTech-e-U/helios-abb-terra-ac/issues)
- **Buy Me a Coffee**: [!["Buy Me A Coffee"](https://www.buymeacoffee.com/assets/img/custom_images/orange_img.png)](https://buymeacoffee.com/olli_foxtech)

---

**Named after Helios** ⚡ — sister project to [helios-victron](https://github.com/FoxTech-e-U/helios-victron)
