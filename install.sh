#!/bin/bash
#
# Helios EV Installation Script
# ================================
#
# Installs the ABB Terra AC PV surplus charging daemon on Victron Cerbo GX.
# The daemon owns the RS485 bus for the ABB exclusively - it reads AND
# writes directly via Modbus and publishes its own D-Bus service. It does
# NOT rely on dbus-modbus-client for the ABB.
#
# Why: dbus-modbus-client (reading the ABB) and an earlier version of this
# daemon (writing SetCurrent/Start/Stop) both accessed the RS485 bus
# independently, causing collisions and repeated dbus-modbus-client
# crashes. Exclusive bus ownership fixes this at the root.
#
# Compatible with Venus OS 3.70+ (read-only filesystem).
#
# Usage (run directly on Cerbo GX):
#   wget -O /tmp/install.sh https://raw.githubusercontent.com/FoxTech-e-U/helios-ev/master/install.sh
#   bash /tmp/install.sh [ttyUSBX] [modbus-address]
#
# Example:
#   bash /tmp/install.sh ttyUSB1 2
#

set -e

REPO_URL="https://raw.githubusercontent.com/FoxTech-e-U/helios-ev/master"
DAEMON_DIR="/data/helios-abb-terra-ac"
TARGET_DIR="/opt/victronenergy/dbus-modbus-client"
RC_LOCAL="/data/rc.local"
SERVICE_DIR="/service/helios-abb-solar-charger"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

print_info()    { echo -e "${BLUE}ℹ ${NC}$1"; }
print_success() { echo -e "${GREEN}✓${NC} $1"; }
print_warning() { echo -e "${YELLOW}⚠${NC} $1"; }
print_error()   { echo -e "${RED}✗${NC} $1"; }
print_header()  { echo ""; echo "========================================="; echo "$1"; echo "========================================="; echo ""; }

[ "$EUID" -ne 0 ] && { print_error "Please run as root"; exit 1; }

print_header "⚡ Helios EV Installation (exclusive RTU bus)"

DEVICE="${1:-ttyUSB1}"
MODBUS_ADDR="${2:-2}"

# ---------------------------------------------------------------------------
# Step 1: Get daemon file
# ---------------------------------------------------------------------------
print_header "Step 1: Get Daemon"

if [ -f "helios-abb-solar-charger.py" ]; then
    print_info "Using local helios-abb-solar-charger.py"
    DAEMON_PY="helios-abb-solar-charger.py"
else
    print_info "Downloading from GitHub..."
    wget -q -O /tmp/helios-abb-solar-charger.py "$REPO_URL/helios-abb-solar-charger.py" || {
        print_error "Download failed."; exit 1
    }
    DAEMON_PY="/tmp/helios-abb-solar-charger.py"
    print_success "Downloaded"
fi

# ---------------------------------------------------------------------------
# Step 2: Remove any legacy dbus-modbus-client ABB plugin (avoid bus conflict)
# ---------------------------------------------------------------------------
print_header "Step 2: Remove Legacy dbus-modbus-client Plugin"

if [ -f "$TARGET_DIR/abb_terra.py" ] || grep -q "^import abb_terra$" "$TARGET_DIR/dbus-modbus-client.py" 2>/dev/null; then
    print_warning "Legacy abb_terra.py plugin detected - removing to avoid RS485 bus conflict"
    mount -o remount,rw /
    sed -i '/^import abb_terra$/d' "$TARGET_DIR/dbus-modbus-client.py" 2>/dev/null || true
    rm -f "$TARGET_DIR/abb_terra.py"
    rm -rf "$TARGET_DIR/__pycache__/" 2>/dev/null || true
    mount -o remount,ro /
    print_success "Legacy plugin removed"

    # Remove old rc.local entries for it
    sed -i "/# helios-ev$/,/^mount -o remount,ro \//d" "$RC_LOCAL" 2>/dev/null || true
else
    print_info "No legacy plugin found"
fi

# ---------------------------------------------------------------------------
# Step 3: Ensure dbus-modbus-client does not scan this ABB address
# ---------------------------------------------------------------------------
print_header "Step 3: Exclude ABB from dbus-modbus-client"

CURRENT_DEVICES=$(dbus -y com.victronenergy.settings /Settings/ModbusClient/${DEVICE}/Devices GetValue 2>/dev/null || echo "")
NEW_DEVICES=$(echo "$CURRENT_DEVICES" | sed "s/rtu:${DEVICE}:9600:${MODBUS_ADDR}//g" | sed 's/,,/,/g' | sed 's/^,//;s/,$//')

dbus -y com.victronenergy.settings /Settings/ModbusClient/${DEVICE}/Devices SetValue "$NEW_DEVICES" >/dev/null 2>&1
print_success "dbus-modbus-client Devices for $DEVICE: '$NEW_DEVICES' (address $MODBUS_ADDR excluded)"

svc -t /service/dbus-modbus-client.serial.${DEVICE} 2>/dev/null || true

# ---------------------------------------------------------------------------
# Step 4: Install daemon
# ---------------------------------------------------------------------------
print_header "Step 4: Install Daemon"

mkdir -p "$DAEMON_DIR"
cp "$DAEMON_PY" "$DAEMON_DIR/helios-abb-solar-charger.py"
sed -i "s#^MODBUS_PORT    = '.*'#MODBUS_PORT    = '/dev/${DEVICE}'#" "$DAEMON_DIR/helios-abb-solar-charger.py"
sed -i "s/^MODBUS_ADDRESS = .*/MODBUS_ADDRESS = ${MODBUS_ADDR}          # ABB Terra AC address/" "$DAEMON_DIR/helios-abb-solar-charger.py"
chmod 755 "$DAEMON_DIR/helios-abb-solar-charger.py"
print_success "Installed to $DAEMON_DIR/ (port: /dev/$DEVICE, address: $MODBUS_ADDR)"

# ---------------------------------------------------------------------------
# Step 5: Verify connectivity (before daemon takes exclusive ownership)
# ---------------------------------------------------------------------------
print_header "Step 5: Verify ABB Connectivity"

python3 - "$DEVICE" "$MODBUS_ADDR" << 'PYEOF'
import sys, time
from pymodbus.client.sync import ModbusSerialClient
dev, addr = sys.argv[1], int(sys.argv[2])
c = ModbusSerialClient(method='rtu', port=f'/dev/{dev}',
    baudrate=9600, bytesize=8, parity='N', stopbits=1, timeout=3)
c.connect()
time.sleep(1)
r = c.read_holding_registers(0x4006, 2, unit=addr)
if hasattr(r, 'registers'):
    ma = (r.registers[0] << 16) | r.registers[1]
    print(f"  OK - ABB Terra MaxCurrent = {ma/1000:.0f}A")
else:
    print(f"  WARNING: no response - check wiring/address")
c.close()
PYEOF

# ---------------------------------------------------------------------------
# Step 6: Install runit service
# ---------------------------------------------------------------------------
print_header "Step 6: Install Service"

mkdir -p "$SERVICE_DIR/log"
cat > "$SERVICE_DIR/run" << RUNEOF
#!/bin/sh
exec /usr/bin/python3 -u ${DAEMON_DIR}/helios-abb-solar-charger.py 2>&1
RUNEOF
cat > "$SERVICE_DIR/log/run" << 'LOGEOF'
#!/bin/sh
exec multilog t s25000 n4 /var/log/helios-abb-solar-charger
LOGEOF
chmod 755 "$SERVICE_DIR/run"
chmod 755 "$SERVICE_DIR/log/run"

mkdir -p "$DAEMON_DIR/service/helios-abb-solar-charger/log"
cp "$SERVICE_DIR/run" "$DAEMON_DIR/service/helios-abb-solar-charger/"
cp "$SERVICE_DIR/log/run" "$DAEMON_DIR/service/helios-abb-solar-charger/log/"
chmod 755 "$DAEMON_DIR/service/helios-abb-solar-charger/run"
chmod 755 "$DAEMON_DIR/service/helios-abb-solar-charger/log/run"

print_success "runit service installed: $SERVICE_DIR"

# ---------------------------------------------------------------------------
# Step 7: Persist via rc.local
# ---------------------------------------------------------------------------
print_header "Step 7: Persist via rc.local"

RC_MARKER="# helios-ev exclusive-rtu"
if ! grep -q "$RC_MARKER" "$RC_LOCAL" 2>/dev/null; then
    cat >> "$RC_LOCAL" << EOF

$RC_MARKER
# Ensure dbus-modbus-client never re-acquires the ABB address after an update
dbus -y com.victronenergy.settings /Settings/ModbusClient/${DEVICE}/Devices SetValue "$NEW_DEVICES" 2>/dev/null || true
if [ ! -f $SERVICE_DIR/run ]; then
    mkdir -p $SERVICE_DIR/log
    cp $DAEMON_DIR/service/helios-abb-solar-charger/run $SERVICE_DIR/
    cp $DAEMON_DIR/service/helios-abb-solar-charger/log/run $SERVICE_DIR/log/
    chmod 755 $SERVICE_DIR/run
    chmod 755 $SERVICE_DIR/log/run
fi
EOF
    chmod +x "$RC_LOCAL"
    print_success "rc.local updated (auto-restore + bus exclusion after firmware updates)"
else
    print_info "rc.local already configured"
fi

# ---------------------------------------------------------------------------
# Step 8: Start service
# ---------------------------------------------------------------------------
print_header "Step 8: Start Service"

touch "$SERVICE_DIR/down"
sleep 1
rm -f "$SERVICE_DIR/down"
sleep 5

STATUS=$(svstat "$SERVICE_DIR" 2>/dev/null || echo "unknown")
echo "  $STATUS"
echo "$STATUS" | grep -q "up" && print_success "Service running" || print_warning "Service starting..."

# ---------------------------------------------------------------------------
# Step 9: Verify D-Bus service
# ---------------------------------------------------------------------------
print_header "Step 9: Verification"

sleep 15
if dbus -y 2>/dev/null | grep -q "abb_terra"; then
    print_success "D-Bus service active: com.victronenergy.evcharger.abb_terra_ac_2"
    CONNECTED=$(dbus -y com.victronenergy.evcharger.abb_terra_ac_2 /Connected GetValue 2>/dev/null || echo "N/A")
    echo "  Connected: $CONNECTED"
else
    print_warning "D-Bus service not detected yet - check logs:"
    echo "  tail -f /var/log/helios-abb-solar-charger/current | tai64nlocal"
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
print_header "✅ Installation Complete!"
echo "  Daemon:     $DAEMON_DIR/helios-abb-solar-charger.py"
echo "  Service:    $SERVICE_DIR"
echo "  Persistent: $RC_LOCAL"
echo ""
echo "Monitor:"
echo "  tail -f /var/log/helios-abb-solar-charger/current | tai64nlocal"
echo "  dbus -y com.victronenergy.evcharger.abb_terra_ac_2 / GetValue"
echo ""
echo "⚠ IMPORTANT: This daemon requires EXCLUSIVE access to $DEVICE address $MODBUS_ADDR."
echo "  No other process (dbus-modbus-client, another script) may read or write"
echo "  this address on the same RS485 bus - even brief concurrent access causes"
echo "  half-duplex collisions and repeated 'no response' errors."
echo ""
echo "⚠ If a Huawei SUN2000 (or any other RS485 device using TCP/other transport)"
echo "  was ever wired to the same bus, its RS485 connection must be physically"
echo "  disconnected - some inverters keep transmitting on RS485 even when not"
echo "  being polled, which corrupts communication for every other device sharing"
echo "  the same wire."
echo ""
print_success "Done! ⚡"
