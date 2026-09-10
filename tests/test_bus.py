"""Unit tests for ethercat_master.bus timing and helpers."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pysoem
import pytest

from ethercat_master.bus import (
    EtherCATBus,
    _make_wait_deadline,
    _wait_deadline_balanced,
    _wait_deadline_low_cpu,
    _wait_deadline_precise,
)
from ethercat_master.webserver import _load_net_config, _save_net_config


def test_make_wait_deadline_maps_policies():
    assert _make_wait_deadline("precise") is _wait_deadline_precise
    assert _make_wait_deadline("balanced") is _wait_deadline_balanced
    assert _make_wait_deadline("low_cpu") is _wait_deadline_low_cpu
    assert _make_wait_deadline("unknown") is _wait_deadline_precise


def test_ethercatbus_default_timing_policy():
    bus = EtherCATBus()
    assert bus.timing_policy == "precise"
    assert bus._wait_deadline is _wait_deadline_precise


@pytest.mark.parametrize("policy,expected", [
    ("precise", _wait_deadline_precise),
    ("balanced", _wait_deadline_balanced),
    ("low_cpu", _wait_deadline_low_cpu),
])
def test_ethercatbus_timing_policy_parameter(policy, expected):
    bus = EtherCATBus(timing_policy=policy)
    assert bus.timing_policy == policy
    assert bus._wait_deadline is expected


@pytest.mark.parametrize("wait_fn", [
    _wait_deadline_precise,
    _wait_deadline_balanced,
])
def test_wait_deadline_returns_after_deadline(wait_fn):
    deadline = time.perf_counter() + 0.001
    wait_fn(deadline)
    after = time.perf_counter()
    assert after >= deadline
    assert after < deadline + 0.005


def test_wait_deadline_low_cpu_returns_after_deadline():
    deadline = time.perf_counter() + 0.010
    _wait_deadline_low_cpu(deadline)
    after = time.perf_counter()
    assert after >= deadline
    assert after < deadline + 0.020


def test_update_timing_tracks_stats():
    stats = EtherCATBus._empty_timing_stats()
    EtherCATBus._update_timing(stats, "pd", 0.001, 0.001)
    EtherCATBus._update_timing(stats, "pd", 0.0011, 0.001)
    assert stats["pd_cycles"] == 2
    assert stats["pd_missed"] == 1
    assert stats["pd_min_s"] == 0.001
    assert stats["pd_max_s"] == 0.0011


def test_get_timing_stats_computes_mean_and_std():
    bus = EtherCATBus(timing_stats=True)
    for _ in range(10):
        EtherCATBus._update_timing(bus._timing_stats, "pd", 0.001, 0.001)
    stats = bus.get_timing_stats()
    assert stats is not None
    assert stats["pd_cycles"] == 10
    assert stats["pd_mean_ms"] == pytest.approx(1.0, abs=0.001)
    assert stats["pd_std_ms"] == pytest.approx(0.0, abs=0.001)


def test_get_timing_stats_returns_none_when_disabled():
    bus = EtherCATBus()
    assert bus.get_timing_stats() is None


def test_processdata_loop_handles_none_master_during_reconnect():
    """Regression: _processdata_loop must not crash when reconnect nulls master."""
    bus = EtherCATBus()
    bus._pd_stop = threading.Event()
    bus._reconnecting.set()

    mock_master = MagicMock()
    mock_master.send_processdata = MagicMock()
    mock_master.receive_processdata = MagicMock(return_value=0)
    bus.master = mock_master

    thread = threading.Thread(target=bus._processdata_loop, daemon=True)
    thread.start()

    # Let the loop enter the reconnect branch, then simulate a failed
    # reconnect attempt that sets self.master to None.
    time.sleep(0.05)
    bus.master = None

    # Run with None master for another reconnect poll cycle.
    time.sleep(0.07)
    bus._pd_stop.set()
    thread.join(timeout=1.0)
    assert not thread.is_alive()


def _bus_with_slave(state, actual_wkc, expected_wkc=3):
    bus = EtherCATBus()
    bus._recover_slave = MagicMock()
    slave = MagicMock()
    slave.state = state
    master = MagicMock()
    master.in_op = True
    master.do_check_state = False
    master.expected_wkc = expected_wkc
    master.slaves = [slave]
    bus.master = master
    bus._actual_wkc = actual_wkc
    return bus, slave


def test_check_once_recoverable_drop_within_grace_does_not_count():
    """SAFE-OP+ERROR (SM watchdog) right after OP is a recovery in progress."""
    bus, slave = _bus_with_slave(pysoem.SAFEOP_STATE + pysoem.STATE_ERROR, actual_wkc=1)
    bus._last_op_seen = time.monotonic()
    lost = bus._check_once(0)
    assert lost == 0
    bus._recover_slave.assert_called_once_with(slave, 0)


def test_check_once_recoverable_drop_after_grace_counts():
    bus, _ = _bus_with_slave(pysoem.SAFEOP_STATE, actual_wkc=1)
    bus._last_op_seen = time.monotonic() - bus.recover_grace_s - 1.0
    assert bus._check_once(0) == 1


def test_check_once_lost_slave_counts_immediately():
    bus, _ = _bus_with_slave(pysoem.NONE_STATE, actual_wkc=-1)
    bus._last_op_seen = time.monotonic()
    assert bus._check_once(3) == 4


def test_check_once_op_resets_counter_and_marks_op_seen():
    bus, _ = _bus_with_slave(pysoem.OP_STATE, actual_wkc=1)
    bus._last_op_seen = 0.0
    assert bus._check_once(5) == 0
    assert bus._last_op_seen > 0.0
    bus._recover_slave.assert_not_called()


def test_settled_requires_clean_bus_for_settle_time():
    bus, _ = _bus_with_slave(pysoem.OP_STATE, actual_wkc=3)
    bus.settle_time_s = 0.05
    bus._check_once(0)
    assert not bus.settled.is_set()
    time.sleep(0.1)
    bus._check_once(0)
    assert bus.settled.is_set()
    assert bus.wait_settled(timeout=0)


def test_settled_resets_when_wkc_drops():
    bus, slave = _bus_with_slave(pysoem.OP_STATE, actual_wkc=3)
    bus.settle_time_s = 0.05
    bus._check_once(0)
    time.sleep(0.03)
    bus._actual_wkc = 1
    slave.state = pysoem.SAFEOP_STATE + pysoem.STATE_ERROR
    bus._check_once(0)
    assert bus._clean_since is None
    bus._actual_wkc = 3
    slave.state = pysoem.OP_STATE
    bus._check_once(0)
    time.sleep(0.03)
    bus._check_once(0)
    assert not bus.settled.is_set()


def _mock_master(n_slaves=1):
    master = MagicMock()
    master.slaves = [MagicMock(name=f"slave{i}") for i in range(n_slaves)]
    for s in master.slaves:
        s.name = "S"
        s.output = b"\x00" * 4
        s.input = b"\x00" * 4
    master.state_check = MagicMock(return_value=pysoem.OP_STATE)
    return master


def test_configure_dc_disabled_by_default():
    bus = EtherCATBus()
    bus.master = _mock_master()
    bus._configure_dc()
    bus.master.slaves[0].dc_sync.assert_not_called()


def test_configure_dc_enables_sync0_on_all_slaves():
    bus = EtherCATBus(dc_sync0_cycle_ns=714_285.7)
    bus.master = _mock_master(n_slaves=2)
    bus._configure_dc()
    for s in bus.master.slaves:
        s.dc_sync.assert_called_once_with(1, 714285)


def test_map_io_raises_configuration_error_without_filter():
    from ethercat_master.exceptions import ConfigurationError
    bus = EtherCATBus()
    bus.master = _mock_master()
    bus.master.config_map.side_effect = RuntimeError("boom")
    bus._slave_state_report = MagicMock(return_value="")
    with pytest.raises(ConfigurationError):
        bus._map_io()


def test_map_io_filter_tolerates_error():
    seen = []
    bus = EtherCATBus(config_map_error_filter=lambda exc: seen.append(exc) or True)
    bus.master = _mock_master()
    bus.master.config_map.side_effect = RuntimeError("0x1C00")
    bus._map_io()
    assert len(seen) == 1


def test_reach_op_retries_then_succeeds():
    bus = EtherCATBus(op_attempts=3, op_timeout_s=0.01)
    bus.master = _mock_master()
    bus._slave_state_report = MagicMock(return_value="")
    # The slave only accepts OP once it has been requested a second time.
    master = bus.master
    master.state_check.side_effect = lambda *_: (
        pysoem.OP_STATE if master.write_state.call_count >= 2 else pysoem.SAFEOP_STATE)
    start = time.perf_counter()
    bus._reach_op(pump=False)
    assert master.write_state.call_count == 2
    assert time.perf_counter() - start < 2.0


def test_reach_op_pump_sends_processdata_and_raises_after_attempts():
    from ethercat_master.exceptions import ConnectionError as BusConnectionError
    bus = EtherCATBus(op_attempts=2, op_timeout_s=0.01)
    bus.master = _mock_master()
    bus.master.state_check.return_value = pysoem.SAFEOP_STATE
    bus._slave_state_report = MagicMock(return_value="")
    with pytest.raises(BusConnectionError):
        bus._reach_op(pump=True)
    assert bus.master.write_state.call_count == 2
    assert bus.master.send_processdata.called
    assert bus.master.receive_processdata.called


def test_open_master_applies_sdo_timeouts():
    bus = EtherCATBus(sdo_read_timeout_us=20_000_000, sdo_write_timeout_us=60_000_000)
    master = _mock_master()
    master.config_init.return_value = 1
    import ethercat_master.bus as busmod
    orig_master = busmod.pysoem.Master
    orig_cb = busmod.register_emergency_callbacks
    busmod.pysoem.Master = lambda: master
    busmod.register_emergency_callbacks = MagicMock()
    try:
        bus._open_master("adapter")
    finally:
        busmod.pysoem.Master = orig_master
        busmod.register_emergency_callbacks = orig_cb
    assert master.sdo_read_timeout == 20_000_000
    assert master.sdo_write_timeout == 60_000_000
    assert master.slaves[0].is_lost is False


def test_call_hook_swallows_exceptions():
    calls = []

    def ok():
        calls.append("ok")

    def bad():
        raise RuntimeError("nope")

    EtherCATBus._call_hook(None)
    EtherCATBus._call_hook(ok)
    EtherCATBus._call_hook(bad)
    assert calls == ["ok"]


def test_reconnect_lock_and_hooks_used():
    """_attempt_reconnect holds reconnect_lock around teardown/bring-up and
    fires on_connection_lost / on_reconnected."""
    events = []

    class Lock:
        def __enter__(self):
            events.append("lock")

        def __exit__(self, *a):
            events.append("unlock")

    bus = EtherCATBus(reconnect_lock=Lock(),
                      on_connection_lost=lambda: events.append("lost"),
                      on_reconnected=lambda: events.append("reconnected"))
    bus._check_stop = threading.Event()
    bus.master = _mock_master()
    bus._resolve_adapter = MagicMock(return_value=MagicMock(name="adp"))
    bus._open_master = MagicMock(side_effect=lambda name: setattr(bus, "master", _mock_master()))
    bus._bring_up = MagicMock()

    orig_sleep = time.sleep
    time.sleep = lambda s: None
    try:
        bus._attempt_reconnect()
    finally:
        time.sleep = orig_sleep

    assert events[0] == "lost"
    assert events[-1] == "reconnected"
    # close + bring-up each wrapped in the lock
    assert events.count("lock") == 2 and events.count("unlock") == 2
    assert not bus._reconnecting.is_set()
    bus._bring_up.assert_called_once_with(start_threads=False)


def _write_config(tmp_path: Path, network: dict, slaves: dict | None = None) -> Path:
    cfg = {"network": network, "default": {}, "slaves": slaves or {}}
    path = tmp_path / "ethercat_config.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    return path


def test_bus_reads_network_config(tmp_path):
    cfg_path = _write_config(tmp_path, {
        "adapter": "\\Device\\NPF_{TEST}",
        "cycle_ms": 0.5,
        "processdata_cycle_ms": 0.25,
        "timing_policy": "balanced",
        "high_priority": True,
    })
    bus = EtherCATBus(pdo_config_path=str(cfg_path))
    assert bus.adapter == "\\Device\\NPF_{TEST}"
    assert bus.cycle_time == pytest.approx(0.0005)
    assert bus.processdata_cycle_time == pytest.approx(0.00025)
    assert bus.timing_policy == "balanced"
    assert bus._wait_deadline is _wait_deadline_balanced
    assert bus.high_priority is True


def test_bus_constructor_overrides_network_config(tmp_path):
    cfg_path = _write_config(tmp_path, {
        "adapter": "\\Device\\NPF_{CFG}",
        "cycle_ms": 2.0,
        "timing_policy": "low_cpu",
        "high_priority": True,
    })
    bus = EtherCATBus(
        adapter="\\Device\\NPF_{ARG}",
        cycle_time_ms=5.0,
        timing_policy="precise",
        high_priority=False,
        pdo_config_path=str(cfg_path),
    )
    assert bus.adapter == "\\Device\\NPF_{ARG}"
    assert bus.cycle_time == pytest.approx(0.005)
    assert bus.timing_policy == "precise"
    assert bus.high_priority is False


def test_bus_network_config_defaults_when_section_missing():
    bus = EtherCATBus()
    assert bus.timing_policy == "precise"
    assert bus.high_priority is False


def test_load_net_config_reads_realtime_fields(tmp_path):
    cfg = _write_config(tmp_path, {
        "adapter": "\\Device\\NPF_{TEST}",
        "cycle_ms": 0.5,
        "processdata_cycle_ms": 0.25,
        "timing_policy": "low_cpu",
        "high_priority": True,
    })
    net = _load_net_config(str(cfg))
    assert net["adapter"] == "\\Device\\NPF_{TEST}"
    assert net["cycle_ms"] == 0.5
    assert net["processdata_cycle_ms"] == 0.25
    assert net["timing_policy"] == "low_cpu"
    assert net["high_priority"] is True


def test_save_net_config_preserves_realtime_fields(tmp_path):
    cfg = _write_config(tmp_path, {
        "adapter": "\\Device\\NPF_{OLD}",
        "cycle_ms": 0.5,
        "processdata_cycle_ms": 0.25,
        "timing_policy": "balanced",
        "high_priority": True,
    })
    _save_net_config(str(cfg), "\\Device\\NPF_{NEW}", 1.0)
    saved = json.loads(cfg.read_text(encoding="utf-8"))
    assert saved["network"]["adapter"] == "\\Device\\NPF_{NEW}"
    assert saved["network"]["cycle_ms"] == 1.0
    assert saved["network"]["processdata_cycle_ms"] == 0.25
    assert saved["network"]["timing_policy"] == "balanced"
    assert saved["network"]["high_priority"] is True
