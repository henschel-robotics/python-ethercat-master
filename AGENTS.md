# Agent Notes — python-ethercat-master

## Verification commands

Run tests:
```bash
python -m pytest tests -q
```

Lint (new/modified files):
```bash
ruff check ethercat_master tests benchmarks
```

Build package:
```bash
python -m build
```

Install development dependencies:
```bash
pip install -e .[dev]
```

## Benchmarks

Micro-benchmark timing policies without hardware:
```bash
python -m benchmarks.timing_precision --cycle-ms 0.7 --iterations 2000 --policy precise
python -m benchmarks.timing_precision --cycle-ms 0.7 --iterations 2000 --policy balanced
python -m benchmarks.timing_precision --cycle-ms 0.7 --iterations 2000 --policy low_cpu
```

Full PDO loop benchmark on real hardware:
```bash
python -m benchmarks.pdo_loop --adapter "\\Device\\NPF_{...}" --cycle-ms 0.7 --duration 10
```

## Notes

- Sub-millisecond cycles rely on the `precise` timing policy (busy-yield). The `balanced` policy reduces CPU for cycles > ~2 ms but is still close to `precise` for sub-ms. `low_cpu` is only suitable when jitter requirements are relaxed or cycle times are well above the OS timer resolution.
- `EtherCATBus` now accepts `timing_stats=True` to collect per-loop timing statistics and `timing_policy="precise|balanced|low_cpu"`.
- `ruff` still reports pre-existing broad `except Exception:` blocks and a mutable `_AL_STATUS_CODES` class dict; these were intentionally left unchanged to preserve existing fault-tolerance behavior.
