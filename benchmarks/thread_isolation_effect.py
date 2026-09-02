"""Demonstrate the effect of thread priority and affinity under load."""

import threading
import time
import statistics

from ethercat_master.bus import (
    _set_win_timer_resolution,
    _reset_win_timer_resolution,
    _set_current_thread_high_priority,
    _set_current_thread_affinity,
    _make_wait_deadline,
)


def background_load(stop_event, duration_s):
    """Simulate CPU load similar to a Flask web server thread."""
    deadline = time.perf_counter() + duration_s
    while not stop_event.is_set() and time.perf_counter() < deadline:
        # Busy-yield work to consume time slices without sleeping.
        for _ in range(10000):
            _ = 42 * 42


def benchmark_loop(cycle_ms, iterations, tune=False, affinity=None):
    stop_event = threading.Event()
    duration_s = cycle_ms * iterations / 1000.0 + 1.0
    load_threads = [
        threading.Thread(target=background_load, args=(stop_event, duration_s))
        for _ in range(4)
    ]
    for t in load_threads:
        t.start()

    if tune:
        _set_current_thread_high_priority()
    if affinity:
        _set_current_thread_affinity(affinity)

    wait_deadline = _make_wait_deadline("precise")
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

    stop_event.set()
    for t in load_threads:
        t.join()
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
    cycle_ms = 0.7
    iterations = 1000
    print(f"Benchmarking 'precise' policy at {cycle_ms} ms under load "
          f"({iterations} cycles, 4 background load threads)")

    _set_win_timer_resolution(1000)

    print("\nNo priority / no affinity:")
    intervals_baseline = benchmark_loop(cycle_ms, iterations)
    print_stats(intervals_baseline, cycle_ms)

    print("\nWith TIME_CRITICAL priority + affinity [2,3]:")
    intervals_tuned = benchmark_loop(cycle_ms, iterations, tune=True, affinity=[2, 3])
    print_stats(intervals_tuned, cycle_ms)

    _reset_win_timer_resolution(1000)


if __name__ == "__main__":
    main()
