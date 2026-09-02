"""Unit tests for ethercat_master.bus timing and helpers."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

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
