"""Demonstrate the effect of Windows timer resolution on sleep-based timing."""

import time
import statistics

from ethercat_master.bus import _set_win_timer_resolution, _reset_win_timer_resolution, _make_wait_deadline


def benchmark_policy(wait_deadline, cycle_ms, iterations):
    cycle_s = cycle_ms / 1000.0
    intervals = []
    t_next = time.perf_counter()
    t_prev = t_next
    for _ in range(iterations):
        t_next += cycle_s
        wait_deadline(t_next)
        t_now = time.perf_counter()
        intervals.append((t_now - t_prev) * 1000.0)
        t_prev = t_now
    return intervals


def print_stats(intervals, target_ms):
    mean = statistics.mean(intervals)
    stdev = statistics.stdev(intervals) if len(intervals) > 1 else 0.0
    p99 = sorted(intervals)[int(len(intervals) * 0.99)]
    max_ms = max(intervals)
    missed = sum(1 for v in intervals if v > target_ms * 1.05)
    print(f"  target={target_ms:.3f} ms  mean={mean:.3f}  stdev={stdev:.3f}  "
          f"p99={p99:.3f}  max={max_ms:.3f}  missed={missed}")


def main():
    cycle_ms = 5.0
    iterations = 1000
    print(f"Benchmarking 'low_cpu' policy at {cycle_ms} ms for {iterations} cycles")

    wait_deadline = _make_wait_deadline("low_cpu")

    print("\nDefault Windows timer resolution:")
    intervals_default = benchmark_policy(wait_deadline, cycle_ms, iterations)
    print_stats(intervals_default, cycle_ms)

    actual = _set_win_timer_resolution(1000)
    print(f"\nWith timer resolution set to {actual} us:")
    intervals_tuned = benchmark_policy(wait_deadline, cycle_ms, iterations)
    print_stats(intervals_tuned, cycle_ms)
    _reset_win_timer_resolution(1000)


if __name__ == "__main__":
    main()
