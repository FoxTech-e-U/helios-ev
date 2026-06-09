"""
ABB Terra AC Wallbox Modbus RTU Integration for Victron Venus OS
================================================================

Plugin for dbus-modbus-client to integrate ABB Terra AC series wallboxes
via Modbus RTU (RS485) into Victron Energy GX devices.

Tested with: ABB Terra AC 22kW
Protocol: Modbus RTU @ 9600 8N1
Device Address: 2

Author: FoxTech e.U.
License: GPL-3.0
"""

import device
import probe
from register import *

# =============================================================================
# ABB Terra AC Device Class  
# =============================================================================

class ABB_Terra_AC(device.EnergyMeter):
    """
    ABB Terra AC Wallbox.
    
    Uses EnergyMeter as base class for D-Bus compatibility.
    """
    
    vendor_id = 'abb'
    vendor_name = 'ABB'
    productid = 0xB044
    productname = 'ABB Terra AC Wallbox'
    min_timeout = 1.0
    
    # EV Charger settings
    default_role = 'evcharger'
    default_instance = 40
    allowed_roles = ['evcharger']
    
    nr_phases = 3
    
    def __init__(self, *args):
        super().__init__(*args)
        
        self.info_regs = []
        
        # Data registers
        # ABB uses 32-bit registers (2x16bit, big-endian)
        self.data_regs = [
            # Max hardware current (0.001A resolution)
            Reg_u32b(0x4006, '/MaxCurrent', 1000, '%.1f A'),
            
            # Error code
            Reg_u32b(0x4008, '/ErrorCode', 1, '%d'),
            
            # Charging state (bits 6-0 contain state)
            Reg_u32b(0x400C, '/Status', 1, '%d'),
            
            # Actual current limit being used
            Reg_u32b(0x400E, '/Current', 1000, '%.1f A'),
            
            # Per-phase currents
            Reg_u32b(0x4010, '/Ac/L1/Current', 1000, '%.2f A'),
            Reg_u32b(0x4012, '/Ac/L2/Current', 1000, '%.2f A'),
            Reg_u32b(0x4014, '/Ac/L3/Current', 1000, '%.2f A'),
            
            # Per-phase voltages
            Reg_u32b(0x4016, '/Ac/L1/Voltage', 10, '%.1f V'),
            Reg_u32b(0x4018, '/Ac/L2/Voltage', 10, '%.1f V'),
            Reg_u32b(0x401A, '/Ac/L3/Voltage', 10, '%.1f V'),
            
            # Total power
            Reg_u32b(0x401C, '/Ac/Power', 1, '%.0f W'),
            
            # Session energy (Wh -> kWh)
            Reg_u32b(0x401E, '/Ac/Energy/Forward', 1000, '%.3f kWh'),
            
            # Writable: Set current limit
            #Reg_u32b(0x4100, '/SetCurrent', 1000, '%.1f A', write=True),
        ]
    
    def device_init(self):
        super().device_init()

    def device_init_late(self):
        super().device_init_late()
        self.dbus['/Position'] = 1  # AC output
    
    def get_ident(self):
        return 'abb_terra_ac_%d' % self.unit


# =============================================================================
# Model Detection via Max Current Register
# =============================================================================

# Models dictionary keyed by max current value in mA
models = {
    6000:  {'model': 'Terra AC 6A',  'handler': ABB_Terra_AC},
    7000:  {'model': 'Terra AC 7A',  'handler': ABB_Terra_AC},
    11000: {'model': 'Terra AC 11A', 'handler': ABB_Terra_AC},
    16000: {'model': 'Terra AC 16A', 'handler': ABB_Terra_AC},
    22000: {'model': 'Terra AC 22A', 'handler': ABB_Terra_AC},
    32000: {'model': 'Terra AC 32A', 'handler': ABB_Terra_AC},
}

# =============================================================================
# Probe Handler - using same pattern as Huawei
# =============================================================================

# Register to read for detection: Max Current at 0x4006 (2 registers = 32-bit)
# This returns the max current in mA (e.g., 16000 for 16A)
probe.add_handler(probe.ModelRegister(
    Reg_u32b(0x4006),  # Read max current register
    models,
    methods=['rtu'],
    rates=[9600],
    units=[2]  # ABB is on address 2
))
