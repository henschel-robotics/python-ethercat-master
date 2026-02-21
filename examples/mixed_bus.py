"""
Example: mixed bus — HDrive motor + Beckhoff terminals on the same bus.

Bus layout example:
  Slave 0: HDrive17-ETC  (Henschel-Robotics servo motor)
  Slave 1: EK1100        (Beckhoff coupler, no I/O)
  Slave 2: EL2008        (Beckhoff 8x digital output)
  Slave 3: EL1008        (Beckhoff 8x digital input)

The HDriveETC class is a slave handle that plugs into EtherCATBus
just like GenericSlave. You can mix HDrive motors and generic
terminals on the same bus.
"""

import os
import time
from ethercat_master import EtherCATBus, GenericSlave
from hdrive_etc import HDriveETC, Mode

ADAPTER = None  # set to adapter name string, e.g. r"\Device\NPF_{...}"
PDO_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pdo_mapping.json")

# -- Create shared bus --
bus = EtherCATBus(adapter=ADAPTER, cycle_time_ms=1, pdo_config_path=PDO_CONFIG)

# -- HDrive motor on slave 0 --
motor = HDriveETC(slave_index=0, bus=bus)

# -- Beckhoff terminals on slaves 2, 3 (slave 1 = EK1100 coupler, skip) --
dio = GenericSlave(2)
din = GenericSlave(3)
bus.register_slave(dio)
bus.register_slave(din)

# -- Connect --
bus.open()
print("Bus connected!\n")

# -- Get Position of HDrive motor --
print(f"Motor position: {motor.get_position()}°")

motor.set_mode(Mode.VELOCITY)
motor.set_velocity(1000) # 1000 rad/s
time.sleep(2)
motor.stop()

# -- Use Beckhoff terminals (uncomment above first) --
dio.output = bytes([0xFF])         # all digital outputs ON
print(f"DIN: {din.input.hex()}")   # read digital inputs

bus.close()
print("Done.")
