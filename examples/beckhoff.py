"""
Example: connect to a Beckhoff EK1100 coupler with terminal modules.

The EK1100 itself is a passive coupler (0 bytes I/O). Each terminal
module behind it appears as its own EtherCAT slave with its own PDOs.

Typical bus layout:
  Slave 0: EK1100  (coupler, no I/O)
  Slave 1: EL2008  (8x digital output)
  Slave 2: EL1008  (8x digital input)
  Slave 3: EL3002  (2x analog input)
  ...

For standard Beckhoff terminals the factory default PDO mapping from
the SII EEPROM works fine — no ethercat_config.json needed.
"""

import time
from ethercat_master import EtherCATBus, GenericSlave

ADAPTER = None  # set to adapter name string, e.g. r"\Device\NPF_{...}"

# -- Discover what's on the bus first --
print("Scanning bus...")
slaves_info = EtherCATBus.discover(adapter=ADAPTER)
for s in slaves_info:
    name = s.get("device_name") or s.get("name", "?")
    print(f"  [{s['index']}] {name}  In={s['input_bytes']}B  Out={s['output_bytes']}B")

# -- Register only slaves that have actual I/O --
handles = []
bus = EtherCATBus(adapter=ADAPTER, cycle_time_ms=1)

for s in slaves_info:
    if s["input_bytes"] > 0 or s["output_bytes"] > 0:
        h = GenericSlave(s["index"])
        handles.append(h)
        bus.register_slave(h)

if not handles:
    print("No I/O terminals found behind the coupler.")
    raise SystemExit(1)

bus.open()
print(f"\nConnected — {len(handles)} I/O terminal(s). Ctrl+C to stop.\n")

try:
    while True:
        for h in handles:
            if h.input:
                print(f"  Slave {h.slave_index}: {h.input.hex(' ')}")
        time.sleep(0.5)
except KeyboardInterrupt:
    print("\n\nDisconnecting...")
finally:
    bus.close()
    print("Done.")
