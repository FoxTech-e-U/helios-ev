#!/bin/bash
#
# Helios ABB Terra AC Solar Charger - Installation Script
# ========================================================
#
# Installs the PV surplus charging daemon on Victron Cerbo GX.
#
# Usage:
#   ./install.sh
#

set -e

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

print_header "⚡ Helios ABB Terra AC Solar Charger Installation"

# Check daemon file
[ ! -f "helios-abb-solar-charger.py" ] && {
    print_error "helios-abb-solar-charger.py not found in current directory!"
    exit 1
}

# ---------------------------------------------------------------------------
# Step 1: Install daemon
# ---------------------------------------------------------------------------
print_header "Step 1: Install Daemon"

INSTALL_DIR="/data/helios-abb-terra-ac"
mkdir -p "$INSTALL_DIR"
cp helios-abb-solar-charger.py "$INSTALL_DIR/"
chmod 755 "$INSTALL_DIR/helios-abb-solar-charger.py"
print_success "Daemon installed to $INSTALL_DIR/"

# ---------------------------------------------------------------------------
# Step 2: Verify configuration
# ---------------------------------------------------------------------------
print_header "Step 2: Verify Configuration"

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

print_info "Checking Victron grid meter on D-Bus..."
GRID=$(dbus -y com.victronenergy.grid.cgwacs_ttyUSB0_mb1 /Ac/Power GetValue 2>/dev/null || echo "N/A")
if [ "$GRID" != "N/A" ]; then
    print_success "Grid meter found: ${GRID}W"
else
    print_warning "Grid meter not found - check service: com.victronenergy.grid.cgwacs_ttyUSB0_mb1"
    print_warning "Edit GRID_SERVICE in the daemon if your meter service name differs"
fi

# ---------------------------------------------------------------------------
# Step 3: Install runit service
# ---------------------------------------------------------------------------
print_header "Step 3: Install Service"

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
print_success "runit service installed: $SERVICE_DIR"

# ---------------------------------------------------------------------------
# Step 4: Start service
# ---------------------------------------------------------------------------
print_header "Step 4: Start Service"

# Create down file first, then remove to trigger clean start
touch "$SERVICE_DIR/down"
sleep 1
rm -f "$SERVICE_DIR/down"
sleep 3

STATUS=$(svstat "$SERVICE_DIR" 2>/dev/null || echo "unknown")
print_info "Service status: $STATUS"

if echo "$STATUS" | grep -q "^$SERVICE_DIR: up"; then
    print_success "Service is running"
else
    print_warning "Service may still be starting, check logs:"
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
print_header "✅ Installation Complete!"

echo "Daemon installed and running."
echo ""
echo "  Install dir:  $INSTALL_DIR"
echo "  Service:      $SERVICE_DIR"
echo "  Log:          /var/log/helios-abb-solar-charger/"
echo ""
echo "Monitor:"
echo "  svstat /service/helios-abb-solar-charger"
echo "  tail -f /var/log/helios-abb-solar-charger/current | tai64nlocal"
echo ""
echo "Control:"
echo "  svc -d /service/helios-abb-solar-charger  # stop"
echo "  svc -u /service/helios-abb-solar-charger  # start"
echo "  svc -t /service/helios-abb-solar-charger  # restart"
echo ""
echo "⚠ Note: After a Venus OS update, re-run this script to restore the service."
print_success "Done! ⚡"
