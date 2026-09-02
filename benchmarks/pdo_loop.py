"""Benchmark the full EtherCAT PDO loop on real hardware.

Requires an EtherCAT adapter and at least one slave. Use --adapter to select
an interface; if omitted the first available adapter is used.
"""

import argparse
import time

from ethercat_master import EtherCATBus, GenericSlave
from ethercat_master.exceptions import EtherCATError

from .common import print_stats


def _stats_for_print(bus_stats, key, target_ms):
    return {
        "count": bus_stats[f"{key}_cycles"],
        "target_ms": target_ms,
        "min_ms": bus_stats[f"{key}_min_ms"],
        "max_ms": bus_stats[f"{key}_max_ms"],
        "mean_ms": bus_stats[f"{key}_mean_ms"],
        "stdev_ms": bus_stats[f"{key}_std_ms"],
        "missed": bus_stats[f"{key}_missed"],
    }


def run(adapter, cycle_ms, processdata_cycle_ms, duration_s, pdo_config_path):
    bus = EtherCATBus(
        adapter=adapter,
        cycle_time_ms=cycle_ms,
        processdata_cycle_ms=processdata_cycle_ms,
        pdo_config_path=pdo_config_path,
        timing_stats=True,
    )

    try:
        slaves_info = EtherCATBus.discover(adapter=adapter, pdo_config_path=pdo_config_path)
        for s in slaves_info:
            if s["input_bytes"] > 0 or s["output_bytes"] > 0:
                bus.register_slave(GenericSlave(s["index"]))
    except (OSError, RuntimeError, EtherCATError) as exc:
        print(f"[BENCH] discovery failed: {exc}")
        print("[BENCH] falling back to single slave handle at index 0")
        bus.register_slave(GenericSlave(0))

    bus.open()
    pd_target = (processdata_cycle_ms if processdata_cycle_ms is not None else cycle_ms)
    print(f"[BENCH] Bus running for {duration_s}s at {pd_target} ms process-data cycle ...")
    try:
        time.sleep(duration_s)
    finally:
        stats = bus.get_timing_stats()
        bus.close()

    if stats is None:
        print("[BENCH] timing stats not available")
        return

    print(f"\n[BENCH] ProcessData: {stats['pd_cycles']} cycles, {stats['pd_errors']} WKC errors")
    print_stats("ProcessData", _stats_for_print(stats, "pd", pd_target))
    print(f"\n[BENCH] PDO Update: {stats['pdo_cycles']} cycles")
    print_stats("PDO Update", _stats_for_print(stats, "pdo", cycle_ms))


def main():
    parser = argparse.ArgumentParser(description="Benchmark EtherCAT PDO loop")
    parser.add_argument("--adapter", type=str, default=None)
    parser.add_argument("--cycle-ms", type=float, default=1.0)
    parser.add_argument("--processdata-cycle-ms", type=float, default=None)
    parser.add_argument("--duration", type=float, default=10.0)
    parser.add_argument("--pdo-config", type=str, default=None)
    args = parser.parse_args()
    run(args.adapter, args.cycle_ms, args.processdata_cycle_ms,
        args.duration, args.pdo_config)


if __name__ == "__main__":
    main()
