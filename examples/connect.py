"""
Minimal example: connect to one EtherCAT slave using ethercat_config.json
"""

import os
import time
from ethercat_master import EtherCATBus, GenericSlave

ADAPTER = None  # set to adapter name string, e.g. r"\Device\NPF_{...}"
SLAVE = 0
PDO_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ethercat_config.json")

print("Available adapters:")
for a in EtherCATBus.list_adapters():
    name = a.name.decode("utf-8", errors="replace") if isinstance(a.name, bytes) else str(a.name)
    desc = a.desc.decode("utf-8", errors="replace") if isinstance(a.desc, bytes) else str(a.desc)
    print(f"  {desc}  ->  {name}")

slave = GenericSlave(SLAVE, use_default_pdo=False)
bus = EtherCATBus(adapter=ADAPTER, cycle_time_ms=1, pdo_config_path=PDO_CONFIG_PATH)
bus.register_slave(slave)
bus.open()

print("Connected! Reading PDO data (Ctrl+C to stop)\n")

try:
    while True:
        data = slave.input
        if data:
            print(f"  RX ({len(data)}B): {data[:16].hex(' ')} ...", end="\r")
        time.sleep(0.1)
except KeyboardInterrupt:
    print("\n\nDisconnecting...")
finally:
    bus.close()
    print("Done.")
