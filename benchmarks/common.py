"""Shared helpers for ethercat_master benchmarks."""

import statistics


def compute_timing_stats(samples, target_ms=None):
    n = len(samples)
    if n == 0:
        return {}
    sorted_s = sorted(samples)
    mean = statistics.mean(samples)
    stdev = statistics.stdev(samples) if n > 1 else 0.0
    p95_idx = int(n * 0.95)
    p99_idx = int(n * 0.99)
    if p95_idx >= n:
        p95_idx = n - 1
    if p99_idx >= n:
        p99_idx = n - 1
    result = {
        "count": n,
        "min_ms": min(samples) * 1000.0,
        "max_ms": max(samples) * 1000.0,
        "mean_ms": mean * 1000.0,
        "median_ms": statistics.median(samples) * 1000.0,
        "stdev_ms": stdev * 1000.0,
        "p95_ms": sorted_s[p95_idx] * 1000.0,
        "p99_ms": sorted_s[p99_idx] * 1000.0,
    }
    if target_ms is not None:
        result["target_ms"] = target_ms
        result["missed"] = sum(1 for s in samples if s * 1000.0 > target_ms * 1.05)
    return result


def print_stats(title, stats):
    print(f"\n{title}")
    print(f"  count   = {stats.get('count')}")
    target = stats.get("target_ms")
    print(f"  target  = {target if target is None else f'{target:.3f} ms'}")
    for key, label in (
        ("min_ms", "min/max"),
        ("mean_ms", "mean"),
        ("median_ms", "median"),
        ("std_ms", "std"),
        ("stdev_ms", "stdev"),
        ("p95_ms", "p95"),
        ("p99_ms", "p99"),
    ):
        if key not in stats:
            continue
        val = stats[key]
        if key == "min_ms" and "max_ms" in stats:
            print(f"  {label}   = {val:.3f} / {stats['max_ms']:.3f} ms")
        else:
            print(f"  {label}   = {val:.3f} ms")
    if "missed" in stats:
        print(f"  missed  = {stats['missed']} (>5% over target)")
