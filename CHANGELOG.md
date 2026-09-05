# Changelog

## [2.1.0] - 2026-09-05
### Changed
- Grid-Messquelle auf `com.victronenergy.system` (/Ac/Grid/Lx/Power) umgestellt,
  passend zur neuen Anlagentopologie (Grid → 3x Multiplus → Haus mit EVCS/WR)
- Default RS485-Port auf `/dev/ttyUSB0` geändert (bisher ttyUSB1)

### Fixed
- Neustart-Bug: ein bereits laufender PV-Ladevorgang wurde nach einem
  Daemon-Neustart fälschlich als externer Trigger erkannt und auf
  MAX_CURRENT gezwungen. Der Daemon übernimmt jetzt beim Start den echten
  ABB-Status und bleibt bei einer laufenden Session im PV_CHARGE-Modus.
- "resume_sent"-Bug: eine Session konnte nach einem kurzen Kommunikations-
  aussetzer oder bei vollem Fahrzeug dauerhaft hängen bleiben, weil der
  Daemon nur einmal einen Neustartversuch unternahm. Er sendet jetzt bei
  Bedarf jeden Zyklus erneut ein (idempotentes) Start-Kommando.

## v1.0.0 (2026-06-09)

Initial release.

### Features
- `abb_terra.py`: dbus-modbus-client plugin for ABB Terra AC wallbox
  - Exposes EV charger in Victron dashboard and VRM portal
  - Supports all ABB Terra AC models (6A–32A) via MaxCurrent auto-detection
  - Fixed: `/Position` set in `device_init_late()` to avoid NoneType crash
  - Fixed: removed unwritable `SetCurrent` register 0x4100 (IllegalAddress on 16A)
- `helios-abb-solar-charger.py`: PV surplus charging daemon
  - Automatic PV surplus charging with dynamic current control (6–16A)
  - Force mode via RFID tap or ABB app (detected automatically)
  - 60s hysteresis before starting and stopping
  - Reads all sensor data from Victron D-Bus (no bus conflicts)
  - Writes Modbus commands only when needed (SetCurrent, Start/Stop)
  - Keepalive to prevent ABB communication timeout
  - runit service integration for Venus OS
- `install.sh`: automated installation script
  - Installs both components
  - Verifies ABB Terra and grid meter connectivity
  - Installs and starts runit service
