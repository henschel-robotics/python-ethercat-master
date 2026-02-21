"""
EtherCAT Network Latency Test
===============================

Measures the round-trip SDO read latency between the host and an
EtherCAT slave.  Performs many consecutive SDO reads of a standard
CoE object (0x1000 — Device Type) and records timing for each, then
computes statistics (min, max, mean, median, p95, p99, std) and
returns the raw samples for histogram plotting.

Can be used standalone::

    python -m ethercat_master.network_test

Or programmatically via an open ``EtherCATBus``::

    from ethercat_master import EtherCATBus
    from ethercat_master.network_test import NetworkLatencyTest

    bus = EtherCATBus(adapter=..., cycle_time_ms=1)
    bus.open()

    test = NetworkLatencyTest(bus.master, slave_index=0)
    test.run_measurement()
    print(test.analyze())

    bus.close()
"""

import time

try:
    from .bus import EtherCATBus
except ImportError:
    from ethercat_master.bus import EtherCATBus


class NetworkLatencyTest:
    """Measure EtherCAT SDO round-trip latency for any slave."""

    NUM_SAMPLES = 200
    SDO_INDEX = 0x1000
    SDO_SUBINDEX = 0x00

    def __init__(self, master, slave_index=0):
        """
        Args:
            master: A live ``pysoem.Master`` instance (bus must be in OP).
            slave_index: Which slave to probe (0-based).
        """
        self.master = master
        self.slave_index = slave_index
        self.latencies_ms: list[float] = []
        self.abort_event = None

    def run_measurement(self):
        """Perform NUM_SAMPLES SDO reads and record round-trip time."""
        slave = self.master.slaves[self.slave_index]
        self.latencies_ms = []
        errors = 0

        for _ in range(self.NUM_SAMPLES):
            if self.abort_event and self.abort_event.is_set():
                return

            t0 = time.perf_counter()
            try:
                slave.sdo_read(self.SDO_INDEX, self.SDO_SUBINDEX)
            except Exception:
                errors += 1
                continue
            t1 = time.perf_counter()
            self.latencies_ms.append((t1 - t0) * 1000.0)

        if errors:
            print(f"[NET-TEST] {errors}/{self.NUM_SAMPLES} reads failed")

    def analyze(self) -> dict | None:
        """Return statistics dict or ``None`` if insufficient data."""
        if len(self.latencies_ms) < 10:
            return None

        samples = self.latencies_ms
        n = len(samples)
        sorted_s = sorted(samples)
        mean = sum(samples) / n
        median = sorted_s[n // 2]
        variance = sum((x - mean) ** 2 for x in samples) / n
        std = variance ** 0.5
        p95 = sorted_s[int(n * 0.95)]
        p99 = sorted_s[int(n * 0.99)]

        return {
            "samples": [round(v, 3) for v in samples],
            "count": n,
            "errors": self.NUM_SAMPLES - n,
            "min_ms": round(min(samples), 3),
            "max_ms": round(max(samples), 3),
            "mean_ms": round(mean, 3),
            "median_ms": round(median, 3),
            "std_ms": round(std, 3),
            "p95_ms": round(p95, 3),
            "p99_ms": round(p99, 3),
        }


def main():
    """Standalone CLI: discover first slave and run the test."""
    import argparse

    parser = argparse.ArgumentParser(description="EtherCAT SDO Latency Test")
    parser.add_argument("--adapter", type=str, default=None,
                        help="Adapter name/UID")
    parser.add_argument("--slave", type=int, default=0,
                        help="Slave index to probe (default 0)")
    parser.add_argument("--samples", type=int, default=200,
                        help="Number of SDO reads (default 200)")
    parser.add_argument("--cycle", type=float, default=1.0,
                        help="PDO cycle time in ms (default 1.0)")
    args = parser.parse_args()

    bus = EtherCATBus(adapter=args.adapter, cycle_time_ms=args.cycle)
    bus.open()

    try:
        test = NetworkLatencyTest(bus.master, slave_index=args.slave)
        test.NUM_SAMPLES = args.samples
        test.run_measurement()

        metrics = test.analyze()
        if metrics is None:
            print("[ERROR] Not enough data captured")
            return

        print(f"Slave:   [{args.slave}] {bus.master.slaves[args.slave].name}")
        print(f"Samples: {metrics['count']}  (errors: {metrics['errors']})")
        print(f"Mean:    {metrics['mean_ms']:.3f} ms")
        print(f"Median:  {metrics['median_ms']:.3f} ms")
        print(f"Min/Max: {metrics['min_ms']:.3f} / {metrics['max_ms']:.3f} ms")
        print(f"Std:     {metrics['std_ms']:.3f} ms")
        print(f"P95:     {metrics['p95_ms']:.3f} ms")
        print(f"P99:     {metrics['p99_ms']:.3f} ms")
    finally:
        bus.close()


if __name__ == "__main__":
    main()
