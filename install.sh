#!/bin/bash
#
# Helios EV Installation Script
# ================================
#
# Installs ABB Terra AC Wallbox integration and PV surplus charging daemon
# on Victron Cerbo GX.
# Compatible with Venus OS 3.70+ (read-only filesystem).
#
# Usage (run directly on Cerbo GX):
#   wget -O /tmp/install.sh https://raw.githubusercontent.com/FoxTech-e-U/helios-ev/master/install.sh
#   bash /tmp/install.sh
#
# Or if you have the repo cloned locally:
#   ./install.sh
#

set -e

REPO_URL="https://raw.githubusercontent.com/FoxTech-e-U/helios-ev/master"
DATA_DIR="/data/helios-ev"
DAEMON_DIR="/data/helios-abb-terra-ac"
TARGET_DIR="/opt/victronenergy/dbus-modbus-client"
RC_LOCAL="/data/rc.local"
RC_MARKER="# helios-ev"

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

print_header "⚡ Helios EV Installation"

# ---------------------------------------------------------------------------
# Step 1: Get files
# ---------------------------------------------------------------------------
print_header "Step 1: Get Files"

if [ -f "abb_terra.py" ]; then
    print_info "Using local files"
    ABB_TERRA_PY="abb_terra.py"
    DAEMON_PY="helios-abb-solar-charger.py"
else
    print_info "Downloading files from GitHub..."
    wget -q -O /tmp/abb_terra.py "$REPO_URL/abb_terra.py" || {
        print_error "Download of abb_terra.py failed."; exit 1
    }
    wget -q -O /tmp/helios-abb-solar-charger.py "$REPO_URL/helios-abb-solar-charger.py" || {
        print_error "Download of helios-abb-solar-charger.py failed."; exit 1
    }
    ABB_TERRA_PY="/tmp/abb_terra.py"
    DAEMON_PY="/tmp/helios-abb-solar-charger.py"
    print_success "Files downloaded"
fi

# ---------------------------------------------------------------------------
# Step 2: Install abb_terra.py to /data/ with symlink and import patch
# ---------------------------------------------------------------------------
print_header "Step 2: Install ABB Terra Plugin"

mkdir -p "$DATA_DIR"
cp "$ABB_TERRA_PY" "$DATA_DIR/abb_terra.py"
chmod 644 "$DATA_DIR/abb_terra.py"
print_success "abb_terra.py installed to $DATA_DIR/"

print_info "Patching filesystem (remount rw)..."
mount -o remount,rw /

# Symlink
[ -f "$TARGET_DIR/abb_terra.py" ] && [ ! -L "$TARGET_DIR/abb_terra.py" ] && \
    cp "$TARGET_DIR/abb_terra.py" "$TARGET_DIR/abb_terra.py.backup.$(date +%Y%m%d_%H%M%S)"
ln -sf "$DATA_DIR/abb_terra.py" "$TARGET_DIR/abb_terra.py"
print_success "Symlink: $TARGET_DIR/abb_terra.py → $DATA_DIR/abb_terra.py"

# Patch dbus-modbus-client.py to import abb_terra
if ! grep -q "import abb_terra" "$TARGET_DIR/dbus-modbus-client.py"; then
    sed -i 's/^import victron_em$/import victron_em\nimport abb_terra/' \
        "$TARGET_DIR/dbus-modbus-client.py"
    print_success "Added 'import abb_terra' to dbus-modbus-client.py"
else
    print_info "dbus-modbus-client.py already imports abb_terra"
fi

# Clear pycache
rm -rf "$TARGET_DIR/__pycache__/" 2>/dev/null || true

mount -o remount,ro /
print_success "Filesystem restored to read-only"

# ---------------------------------------------------------------------------
# Step 3: Install solar charger daemon
# ---------------------------------------------------------------------------
print_header "Step 3: Install Solar Charger Daemon"

mkdir -p "$DAEMON_DIR"
cp "$DAEMON_PY" "$DAEMON_DIR/helios-abb-solar-charger.py"
chmod 755 "$DAEMON_DIR/helios-abb-solar-charger.py"
print_success "Daemon installed to $DAEMON_DIR/"

# ---------------------------------------------------------------------------
# Step 4: Persist via rc.local
# ---------------------------------------------------------------------------
print_header "Step 4: Persist via rc.local"

if ! grep -q "$RC_MARKER" "$RC_LOCAL" 2>/dev/null; then
    cat >> "$RC_LOCAL" << EOF

$RC_MARKER
mount -o remount,rw /
ln -sf $DATA_DIR/abb_terra.py $TARGET_DIR/abb_terra.py
if ! grep -q "import abb_terra" $TARGET_DIR/dbus-modbus-client.py; then
    sed -i 's/^import victron_em\$/import victron_em\\nimport abb_terra/' $TARGET_DIR/dbus-modbus-client.py
fi
rm -rf $TARGET_DIR/__pycache__/ 2>/dev/null || true
mount -o remount,ro /
EOF
    chmod +x "$RC_LOCAL"
    print_success "rc.local updated (auto-restore after firmware updates)"
else
    print_info "rc.local already has helios-ev entry"
fi

# ---------------------------------------------------------------------------
# Step 5: Verify connectivity
# ---------------------------------------------------------------------------
print_header "Step 5: Verify Configuration"

print_info "Checking ABB Terra on Modbus RTU..."
python3 - << 'PYEOF'
import sys, time
from pymodbus.client.sync import ModbusSerialClient
c = ModbusSerialClient(method='rtu', port='/dev/ttyUSB1',
    baudrate=9600, bytesize=8, parity='N', stopbits=1, timeout=3)
c.connect()
time.sleep(1)
r = c.read_holding_registers(0x4006, 2, unit=2)
if hasattr(r, 'registers'):
    ma = (r.registers[0] << 16) | r.registers[1]
    print(f"  ABB Terra found: MaxCurrent = {ma/1000:.0f}A")
else:
    print("  WARNING: ABB Terra not responding - check wiring and address")
c.close()
PYEOF

print_info "Checking grid meter on D-Bus..."
GRID=$(dbus -y com.victronenergy.grid.cgwacs_ttyUSB0_mb1 /Ac/Power GetValue 2>/dev/null || echo "N/A")
[ "$GRID" != "N/A" ] && print_success "Grid meter: ${GRID}W" || \
    print_warning "Grid meter not found - check GRID_SERVICE in daemon config"

# ---------------------------------------------------------------------------
# Step 6: Install runit service
# ---------------------------------------------------------------------------
print_header "Step 6: Install Service"

SERVICE_DIR="/service/helios-abb-solar-charger"
mkdir -p "$SERVICE_DIR/log"

cat > "$SERVICE_DIR/run" << 'RUNEOF'
#!/bin/sh
exec /usr/bin/python3 -u /data/helios-abb-terra-ac/helios-abb-solar-charger.py 2>&1
RUNEOF

cat > "$SERVICE_DIR/log/run" << 'LOGEOF'
#!/bin/sh
exec multilog t s25000 n4 /var/log/helios-abb-solar-charger
LOGEOF

chmod 755 "$SERVICE_DIR/run"
chmod 755 "$SERVICE_DIR/log/run"

# Save service scripts to /data/ for rc.local restore
mkdir -p "$DATA_DIR/service/helios-abb-solar-charger/log"
cp "$SERVICE_DIR/run" "$DATA_DIR/service/helios-abb-solar-charger/"
cp "$SERVICE_DIR/log/run" "$DATA_DIR/service/helios-abb-solar-charger/log/"
chmod 755 "$DATA_DIR/service/helios-abb-solar-charger/run"
chmod 755 "$DATA_DIR/service/helios-abb-solar-charger/log/run"

# Add service restore to rc.local
if ! grep -q "helios-abb-solar-charger" "$RC_LOCAL" 2>/dev/null; then
    cat >> "$RC_LOCAL" << 'EOF'

# helios-ev service restore
if [ ! -f /service/helios-abb-solar-charger/run ]; then
    mkdir -p /service/helios-abb-solar-charger/log
    cp /data/helios-ev/service/helios-abb-solar-charger/run /service/helios-abb-solar-charger/
    cp /data/helios-ev/service/helios-abb-solar-charger/log/run /service/helios-abb-solar-charger/log/
    chmod 755 /service/helios-abb-solar-charger/run
    chmod 755 /service/helios-abb-solar-charger/log/run
fi
EOF
fi

print_success "runit service installed: $SERVICE_DIR"

# ---------------------------------------------------------------------------
# Step 7: Start service
# ---------------------------------------------------------------------------
print_header "Step 7: Start Service"

touch "$SERVICE_DIR/down"
sleep 1
rm -f "$SERVICE_DIR/down"
sleep 3

STATUS=$(svstat "$SERVICE_DIR" 2>/dev/null || echo "unknown")
echo "  $STATUS"
echo "$STATUS" | grep -q "up" && print_success "Service running" || print_warning "Service starting..."

# ---------------------------------------------------------------------------
# Step 8: Restart dbus-modbus-client
# ---------------------------------------------------------------------------
print_header "Step 8: Restart Services"

svc -t /service/dbus-modbus-client.serial.ttyUSB1 2>/dev/null || true
svc -t /service/serial-starter
print_success "Services restarted"

sleep 35
dbus -y | grep -q "abb_terra" && \
    print_success "ABB Terra found on D-Bus" || \
    print_warning "ABB Terra not yet on D-Bus - may need a few more seconds"

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
print_header "✅ Installation Complete!"
echo "  Plugin:     $DATA_DIR/abb_terra.py"
echo "  Symlink:    $TARGET_DIR/abb_terra.py"
echo "  Import:     patched into dbus-modbus-client.py"
echo "  Daemon:     $DAEMON_DIR/helios-abb-solar-charger.py"
echo "  Service:    $SERVICE_DIR"
echo "  Persistent: $RC_LOCAL"
echo ""
echo "Monitor:"
echo "  tail -f /var/log/helios-abb-solar-charger/current | tai64nlocal"
echo ""
echo "To update to latest version:"
echo "  wget -O /tmp/install.sh $REPO_URL/install.sh && bash /tmp/install.sh"
echo ""
print_success "Done! ⚡"

