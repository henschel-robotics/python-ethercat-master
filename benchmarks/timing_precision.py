"""Micro-benchmark for the EtherCATBus timing helper.

Does not need an EtherCAT adapter; it measures the selected timing policy's
wait behavior at the requested cycle time.
"""

import argparse
import time

from ethercat_master.bus import _make_wait_deadline

from .common import compute_timing_stats, print_stats


def benchmark_wait_deadline(wait_deadline, cycle_ms, iterations):
    cycle_s = cycle_ms / 1000.0
    intervals = []
    t_next = time.perf_counter()
    t_prev = t_next

    t0 = time.perf_counter()
    for _ in range(iterations):
        t_next += cycle_s
        wait_deadline(t_next)
        t_now = time.perf_counter()
        intervals.append(t_now - t_prev)
        t_prev = t_now
    t1 = time.perf_counter()

    total = t1 - t0
    print(f"Benchmarked {iterations} cycles at {cycle_ms} ms "
          f"({iterations / total:.0f} Hz effective), elapsed {total:.2f} s")

    stats = compute_timing_stats(intervals, target_ms=cycle_ms)
    print_stats("_wait_deadline timing", stats)


def main():
    parser = argparse.ArgumentParser(description="Benchmark _wait_deadline timing precision")
    parser.add_argument("--cycle-ms", type=float, default=1.0)
    parser.add_argument("--iterations", type=int, default=10000)
    parser.add_argument("--policy", type=str, default="precise",
                        choices=["precise", "balanced", "low_cpu"])
    args = parser.parse_args()
    wait_deadline = _make_wait_deadline(args.policy)
    benchmark_wait_deadline(wait_deadline, args.cycle_ms, args.iterations)


if __name__ == "__main__":
    main()
