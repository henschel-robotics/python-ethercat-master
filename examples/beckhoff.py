"""
Example: Beckhoff EK1100 coupler with terminal modules (EL1808, EL2828, …).

Slave 0 is usually the passive coupler (no I/O). Each terminal is its own slave.

Adapter, cycle time, and PDO mapping come from ``ethercat_config.json``
(save from ``ecmaster-web`` or edit by hand).

Adjust ``IO_SLAVE_INDICES`` to every slave that has process data (skip the coupler).
Adjust ``INPUT_SLAVE_INDICES`` to slaves that are digital/analog *inputs* (for the
read loop — avoids treating empty ``b""`` as “no input”).
"""

import time
from ethercat_master import EtherCATBus, GenericSlave

CONFIG = "ethercat_config.json"

# Slaves with I/O (not the EK1100 at index 0). Change to match your stack.
IO_SLAVE_INDICES = [1, 2]

# Subset of the above that are inputs to the master (EL1808, etc.). Change to match.
INPUT_SLAVE_INDICES = {1}

handles = []
bus = EtherCATBus(pdo_config_path=CONFIG)

for idx in IO_SLAVE_INDICES:
    h = GenericSlave(idx)
    handles.append(h)
    bus.register_slave(h)

bus.open()
print(f"Connected — {len(handles)} I/O slave(s). Ctrl+C to stop.\n")

try:
    while True:
        for h in handles:
            if h.slave_index in INPUT_SLAVE_INDICES:
                inp = h.input
                print(f"  Slave {h.slave_index} IN: {inp.hex(' ') if inp else '—'}")

        for h in handles:
            if h.output is not None and len(h.output) > 0:
                h.output = bytes([h.output[0] ^ 0x01]) + h.output[1:]
                print(f"  Slave {h.slave_index} OUT: DO1 {'ON' if h.output[0] & 0x01 else 'OFF'}")
                break

        time.sleep(0.5)

except KeyboardInterrupt:
    print("\n\nDisconnecting...")
finally:
    bus.close()
    print("Done.")
