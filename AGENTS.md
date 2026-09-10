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
- `open()` and `_attempt_reconnect()` share `_bring_up()` (`_configure_pdos` → `_map_io` → SAFE-OP → `_configure_dc` → `_reach_op`). Subclass and override a step for device quirks instead of monkey-patching. Opt-in constructor options: `dc_sync0_cycle_ns`, `op_attempts`/`op_timeout_s`, `sdo_read_timeout_us`/`sdo_write_timeout_us`, `config_map_error_filter`, `reconnect_lock`, `on_connection_lost`/`on_reconnected`. Defaults preserve the previous behaviour.
- After (re)connect `bus.settled` is set once all slaves ran clean in OP for `settle_time_s`; applications should hold non-essential SDO traffic until then (blocking SDO calls stall the ProcessData thread and trip SM watchdogs). The state-check loop ignores SAFE-OP(+ERROR) recoveries within `recover_grace_s` of the last OP sighting.
- `ruff` still reports pre-existing broad `except Exception:` blocks and a mutable `_AL_STATUS_CODES` class dict; these were intentionally left unchanged to preserve existing fault-tolerance behavior.
