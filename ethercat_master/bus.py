"""
EtherCAT Master — Bus Manager
===============================

Provides :class:`EtherCATBus`, which owns the ``pysoem.Master`` and manages
low-level EtherCAT communication for one or more slaves on a single adapter.

Architecture
------------

::

    EtherCATBus
    ├── pysoem.Master          (adapter handle)
    ├── ProcessData thread     (default 1 ms — raw frame send/receive, configurable)
    ├── PDO Update thread      (configurable — decode RX, encode TX per slave)
    └── State Check thread     (300 ms — health monitoring, auto-reconnect)

Slave handles register via ``register_slave()`` and must implement:
``slave_index``, ``configure()``, ``pdo_update()``, ``seed_tx()``,
``safe_stop()``, ``on_reconnect()``.

Usage
-----

::

    bus = EtherCATBus(adapter="\\Device\\NPF_{...}", cycle_time_ms=1)
    bus.register_slave(my_slave_handle)
    bus.open()
    ...
    bus.close()

Bus discovery (no OP transition)::

    slaves = EtherCATBus.discover(adapter="\\Device\\NPF_{...}")

"""

import ctypes
import inspect as _inspect
import json
import os
import struct
import threading
import time
from contextlib import nullcontext
from pathlib import Path

import pysoem

from .exceptions import ConfigurationError, ConnectionError
from .pdo import (
    apply_startup_sdos,
    configure_pdo_mapping,
    get_slave_pdo,
    get_slave_startup,
    load_pdo_config,
    pdo_mapping_exists,
    sanitize_invalid_pdo_assignments,
    slave_supports_coe_pdo_mapping,
    slave_supports_pdo_assignment,
)

_EC_STATES = {
    pysoem.NONE_STATE:   "NONE",
    pysoem.INIT_STATE:   "INIT",
    pysoem.PREOP_STATE:  "PRE-OP",
    pysoem.BOOT_STATE:   "BOOT",
    pysoem.SAFEOP_STATE: "SAFE-OP",
    pysoem.OP_STATE:     "OP",
}


def _state_name(state_code):
    """Human-readable EtherCAT state from a raw state code."""
    base = state_code & ~pysoem.STATE_ACK
    name = _EC_STATES.get(base, f"0x{state_code:02X}")
    if state_code & pysoem.STATE_ACK:
        name += "+ERR"
    return name


def _on_slave_emergency(_emcy):
    """CoE emergency handler so SDO traffic uses pysoem's callback path (not deprecated)."""


def register_emergency_callbacks(master):
    """Attach an emergency callback to every slave on *master*.

    PySOEM >= 1.1.8 routes mailbox emergencies through registered callbacks.
    Without this, ``sdo_read`` / ``sdo_write`` emit a ``FutureWarning`` and may
    raise :class:`pysoem.Emergency` when a slave sends an EMCY during CoE access.

    Call this once per ``config_init()`` — ``CdefSlave`` is a Cython type
    without ``__dict__`` or weakref support, so we can't dedupe across calls.
    """
    slaves = getattr(master, "slaves", None) or []
    if not slaves or not hasattr(slaves[0], "add_emergency_callback"):
        return
    for slave in slaves:
        try:
            slave.add_emergency_callback(_on_slave_emergency)
        except Exception:
            pass


# ------------------------------------------------------------------------------
# High-resolution timing (sub-millisecond) helpers
# ------------------------------------------------------------------------------
if hasattr(ctypes, "windll") and ctypes.windll:
    _SwitchToThread = ctypes.windll.kernel32.SwitchToThread
else:
    _SwitchToThread = None

_SCHED_YIELD = getattr(os, "sched_yield", None)
_TIME_SLEEP = time.sleep
_PERF_COUNTER = time.perf_counter


def _yield_thread():
    """Yield the current time slice if an OS API is available."""
    switch = _SwitchToThread
    if switch is not None:
        switch()
        return
    sched = _SCHED_YIELD
    if sched is not None:
        try:
            sched()
        except OSError:
            pass


def _set_process_high_priority():
    """Best-effort raise of the current process priority class (Windows only)."""
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.GetCurrentProcess.argtypes = []
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        kernel32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        kernel32.SetPriorityClass.restype = ctypes.c_bool
        process = kernel32.GetCurrentProcess()
        # REALTIME_PRIORITY_CLASS = 0x100, HIGH_PRIORITY_CLASS = 0x80
        if not kernel32.SetPriorityClass(process, 0x00000100):
            kernel32.SetPriorityClass(process, 0x00000080)
    except Exception:  # noqa: BLE001, S110
        pass


def _set_current_thread_high_priority():
    """Best-effort raise of the calling thread's scheduler priority.

    On Windows this raises the current thread to TIME_CRITICAL within its
    process class.  On Linux it attempts SCHED_FIFO with priority 80.
    Failures are silently ignored so that the bus keeps running on restricted
    accounts.
    """
    try:
        if _SwitchToThread is not None:
            kernel32 = ctypes.windll.kernel32
            kernel32.GetCurrentThread.argtypes = []
            kernel32.GetCurrentThread.restype = ctypes.c_void_p
            kernel32.SetThreadPriority.argtypes = [ctypes.c_void_p, ctypes.c_int]
            kernel32.SetThreadPriority.restype = ctypes.c_bool
            thread = kernel32.GetCurrentThread()
            # THREAD_PRIORITY_TIME_CRITICAL
            kernel32.SetThreadPriority(thread, 15)
            return
        # Linux: SCHED_FIFO on the calling thread.
        libc = ctypes.CDLL("libc.so.6", use_errno=True)

        class _SchedParam(ctypes.Structure):
            _fields_ = [("sched_priority", ctypes.c_int)]

        param = _SchedParam(80)
        # SCHED_FIFO = 1, pid 0 -> calling thread/process
        libc.sched_setscheduler(0, 1, ctypes.byref(param))
    except Exception:  # noqa: BLE001, S110
        pass


def _affinity_mask(cpus):
    """Convert a CPU affinity description to a bitmask.

    Accepts an integer bitmask or an iterable of CPU indices.
    Returns an integer mask or ``None`` if *cpus* is empty/None.
    """
    if cpus is None:
        return None
    if isinstance(cpus, int):
        return cpus
    mask = 0
    for cpu in cpus:
        mask |= 1 << int(cpu)
    return mask if mask else None


def _set_current_thread_affinity(cpus):
    """Best-effort pin of the calling thread to the given CPU(s) (Windows only).

    *cpus* may be an integer bitmask or an iterable of CPU indices.
    Failures are silently ignored so that the bus keeps running on restricted
    accounts or non-Windows systems.
    """
    mask = _affinity_mask(cpus)
    if mask is None or _SwitchToThread is None:
        return None
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.GetCurrentThread.argtypes = []
        kernel32.GetCurrentThread.restype = ctypes.c_void_p
        kernel32.SetThreadAffinityMask.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        kernel32.SetThreadAffinityMask.restype = ctypes.c_size_t
        thread = kernel32.GetCurrentThread()
        return kernel32.SetThreadAffinityMask(thread, mask)
    except Exception:  # noqa: BLE001
        return None


def _set_process_affinity(cpus):
    """Best-effort pin of the whole process to the given CPU(s) (Windows only).

    Use with caution: restricting the process to too few cores can starve
    other threads or make the system unresponsive.
    """
    mask = _affinity_mask(cpus)
    if mask is None or _SwitchToThread is None:
        return None
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.GetCurrentProcess.argtypes = []
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        kernel32.SetProcessAffinityMask.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        kernel32.SetProcessAffinityMask.restype = ctypes.c_bool
        process = kernel32.GetCurrentProcess()
        return kernel32.SetProcessAffinityMask(process, mask)
    except Exception:  # noqa: BLE001
        return None


def _set_win_timer_resolution(resolution_us):
    """Best-effort raise of the Windows timer resolution.

    First tries the undocumented ``NtSetTimerResolution`` (ntdll), which can
    reach ~0.5 ms on many systems. If that fails or is unavailable it falls
    back to the documented ``timeBeginPeriod`` API (winmm), which usually
    achieves 1 ms.

    Returns the actually achieved resolution in microseconds, or ``None`` if
    no API was available or the call failed.
    """
    if _SwitchToThread is None or resolution_us is None or resolution_us <= 0:
        return None

    # Try NtSetTimerResolution first: it can achieve sub-millisecond values.
    try:
        ntdll = ctypes.windll.ntdll
        ntdll.NtSetTimerResolution.argtypes = [
            ctypes.c_ulong, ctypes.c_ubyte, ctypes.POINTER(ctypes.c_ulong)
        ]
        ntdll.NtSetTimerResolution.restype = ctypes.c_long
        desired = ctypes.c_ulong(int(resolution_us) * 10)  # 100-ns units
        actual = ctypes.c_ulong()
        # STATUS_SUCCESS = 0
        if ntdll.NtSetTimerResolution(desired, 1, ctypes.byref(actual)) >= 0:
            return actual.value / 10.0
    except Exception:  # noqa: BLE001, S110
        pass

    # Fallback to the documented multimedia timer API.
    try:
        winmm = ctypes.windll.winmm
        winmm.timeBeginPeriod.argtypes = [ctypes.c_uint]
        winmm.timeBeginPeriod.restype = ctypes.c_uint
        # timeBeginPeriod takes milliseconds and is reference-counted per period.
        period_ms = max(1, int(resolution_us // 1000))
        if winmm.timeBeginPeriod(period_ms) == 0:
            return period_ms * 1000
    except Exception:  # noqa: BLE001, S110
        pass

    return None


def _reset_win_timer_resolution(resolution_us):
    """Reset a timer resolution previously set by ``_set_win_timer_resolution``.

    The value passed should be the *requested* ``resolution_us`` (used to
    decide whether ``timeEndPeriod`` or ``NtSetTimerResolution(..., 0)`` is
    needed).
    """
    if _SwitchToThread is None or resolution_us is None or resolution_us <= 0:
        return

    try:
        winmm = ctypes.windll.winmm
        winmm.timeEndPeriod.argtypes = [ctypes.c_uint]
        winmm.timeEndPeriod.restype = ctypes.c_uint
        period_ms = max(1, int(resolution_us // 1000))
        winmm.timeEndPeriod(period_ms)
    except Exception:  # noqa: BLE001, S110
        pass

    try:
        ntdll = ctypes.windll.ntdll
        ntdll.NtSetTimerResolution.argtypes = [
            ctypes.c_ulong, ctypes.c_ubyte, ctypes.POINTER(ctypes.c_ulong)
        ]
        ntdll.NtSetTimerResolution.restype = ctypes.c_long
        desired = ctypes.c_ulong(int(resolution_us) * 10)
        actual = ctypes.c_ulong()
        ntdll.NtSetTimerResolution(desired, 0, ctypes.byref(actual))
    except Exception:  # noqa: BLE001, S110
        pass


def _wait_deadline_precise(deadline):
    """Busy-wait/yield until *deadline* for the lowest possible jitter."""
    perf_counter = _PERF_COUNTER
    switch = _yield_thread
    sleep = _TIME_SLEEP
    while True:
        remaining = deadline - perf_counter()
        if remaining <= 0:
            return
        if remaining > 0.002:
            sleep(remaining - 0.001)
        else:
            switch()


def _wait_deadline_balanced(deadline):
    """Sleep for long waits, yield for short ones; lower CPU, moderate jitter."""
    perf_counter = _PERF_COUNTER
    switch = _yield_thread
    sleep = _TIME_SLEEP
    threshold = 0.0005
    min_sleep = 0.002
    while True:
        remaining = deadline - perf_counter()
        if remaining <= 0:
            return
        if remaining > threshold + min_sleep:
            sleep(remaining - threshold)
        else:
            switch()


def _wait_deadline_low_cpu(deadline):
    """Sleep-based wait; higher jitter but lowest CPU usage.

    Best suited for cycle times well above the OS timer resolution.
    """
    perf_counter = _PERF_COUNTER
    sleep = _TIME_SLEEP
    while True:
        remaining = deadline - perf_counter()
        if remaining <= 0:
            return
        sleep(remaining)


def _make_wait_deadline(policy):
    """Return the wait function for *policy*.

    Policies:
        ``precise``  — busy-yield for sub-ms precision (highest CPU).
        ``balanced`` — sleep until 0.5 ms from deadline, then yield.
        ``low_cpu``  — sleep only; accept OS timer resolution jitter.
    """
    if policy == "low_cpu":
        return _wait_deadline_low_cpu
    if policy == "balanced":
        return _wait_deadline_balanced
    return _wait_deadline_precise


# Sentinel for constructor arguments that may be overridden by the JSON config.
_MISSING = object()


# Detect whether this pysoem build supports release_gil= on the PDO calls.
_SEND_PD_KWARGS = {}
_RECV_PD_KWARGS = {}
if "release_gil" in _inspect.signature(pysoem.Master.send_processdata).parameters:
    _SEND_PD_KWARGS["release_gil"] = True
if "release_gil" in _inspect.signature(pysoem.Master.receive_processdata).parameters:
    _RECV_PD_KWARGS["release_gil"] = True


class EtherCATBus:
    """Manage an EtherCAT bus with one or more slaves.

    Owns the ``pysoem.Master``, the fast ProcessData thread, and the
    slave health-check thread.  Individual slave handles register via
    :meth:`register_slave` and are called each PDO cycle to decode RX
    and encode TX.

    Args:
        adapter: Network-adapter name/UID string (e.g.
            ``\\Device\\NPF_{GUID}``).  Use :meth:`list_adapters` to
            enumerate available adapters and their names.
        cycle_time_ms: PDO update cycle time in milliseconds.
        processdata_cycle_ms: Raw EtherCAT send/receive cycle in
            milliseconds.  Defaults to 1 ms when omitted, preserving the
            previous behaviour.  Set this to the desired bus frequency
            (e.g. 0.714 for ~1400 Hz) independently of *cycle_time_ms*.
        pdo_config_path: Optional path to an ``ethercat_config.json`` file.
            When provided, per-slave PDO assignments are read from
            this file instead of using the hardcoded defaults.
        timing_stats: When ``True``, the ProcessData and PDO Update loops
            collect cycle-time statistics. Call :meth:`get_timing_stats`
            to retrieve them. Adds a small per-cycle overhead.
        timing_policy: How the ProcessData and PDO Update loops wait for
            the next cycle deadline. ``"precise"`` busy-yields for the
            lowest jitter, ``"balanced"`` sleeps part of the cycle to
            reduce CPU, and ``"low_cpu"`` sleeps the full interval.  May
            also be set in ``ethercat_config.json`` under ``network``.
        high_priority: When ``True``, attempt to raise the process priority
            class (Windows) and the worker thread priorities at bus start.
            This can reduce scheduler-induced jitter, but may starve other
            threads/processes; use with caution.  May also be set in
            ``ethercat_config.json`` under ``network``.
        timer_resolution_us: Windows-only. Request a higher timer resolution
            (e.g. ``1000`` for 1 ms) when the bus starts. This improves the
            accuracy of ``time.sleep`` calls made by other threads such as
            the state-check or SDO-telemetry loops. Pass ``None`` to leave
            the system default unchanged. May also be set in
            ``ethercat_config.json`` under ``network``.
        cpu_affinity: Windows-only. Pin the ProcessData and PDO Update
            threads to the given CPU(s). Accepts an integer bitmask or a
            list of CPU indices, e.g. ``[2, 3]``. Leave as ``None`` to let
            the OS schedule freely. May also be set in
            ``ethercat_config.json`` under ``network``.
        dc_sync0_cycle_ns: Enable Distributed-Clocks SYNC0 on every slave
            with this cycle time (nanoseconds) between SAFE-OP and OP.
            Required by DC-synchronous devices (servo drives, SSC/netX
            slaves in DC mode). ``None`` (default) leaves DC untouched.
        op_attempts: How often the OP transition is requested before giving
            up. Some slaves need a few valid process-data cycles before they
            accept OP; retrying after a short pause helps them along.
        op_timeout_s: Time to wait for OP per attempt.
        sdo_read_timeout_us / sdo_write_timeout_us: Override pysoem's
            default SDO timeouts (microseconds) on the master, e.g. for
            firmware-update commands that block for many seconds.
        config_map_error_filter: Optional ``callable(exc) -> bool``. When
            ``config_map()`` raises and the filter returns ``True`` the error
            is logged and bring-up continues with whatever process-data image
            was built (possibly 0 bytes). Use this for slaves that expose no
            readable 0x1C00 and where a diagnostics-only connection is still
            useful. Default: any error aborts.
        reconnect_lock: Optional context manager that is held while the
            master is closed, replaced and brought back up during an
            automatic reconnect. Share it with application threads that do
            SDO/FoE traffic so they never touch a half-built master.
        on_connection_lost / on_reconnected: Optional zero-argument
            callbacks invoked when a reconnect starts / succeeds. Exceptions
            raised by a hook are logged and ignored.

    After a (re)connect :attr:`settled` is set once every slave has run
    clean in OP for :attr:`settle_time_s`; use :meth:`wait_settled` to defer
    non-essential SDO traffic (telemetry, version reads) until then.
    """

    def __init__(self, adapter=None, cycle_time_ms=10, processdata_cycle_ms=None,
                 pdo_config_path=None, timing_stats=False, timing_policy=_MISSING,
                 high_priority=_MISSING, timer_resolution_us=_MISSING,
                 cpu_affinity=_MISSING, dc_sync0_cycle_ns=None,
                 op_attempts=1, op_timeout_s=5.0,
                 sdo_read_timeout_us=None, sdo_write_timeout_us=None,
                 config_map_error_filter=None, reconnect_lock=None,
                 on_connection_lost=None, on_reconnected=None):
        if pdo_config_path:
            self.pdo_config = load_pdo_config(pdo_config_path)
            net = self._read_network_config(pdo_config_path)
            if adapter is None and net.get("adapter"):
                adapter = net["adapter"]
            if cycle_time_ms == 10 and net.get("cycle_ms"):
                cycle_time_ms = net["cycle_ms"]
            if processdata_cycle_ms is None and net.get("processdata_cycle_ms"):
                processdata_cycle_ms = net["processdata_cycle_ms"]
            if timing_policy is _MISSING:
                timing_policy = net.get("timing_policy", "precise")
            if high_priority is _MISSING:
                high_priority = net.get("high_priority", False)
            if timer_resolution_us is _MISSING:
                timer_resolution_us = net.get("timer_resolution_us", None)
            if cpu_affinity is _MISSING:
                cpu_affinity = net.get("cpu_affinity", None)
        else:
            self.pdo_config = None
            if timing_policy is _MISSING:
                timing_policy = "precise"
            if high_priority is _MISSING:
                high_priority = False
            if timer_resolution_us is _MISSING:
                timer_resolution_us = None
            if cpu_affinity is _MISSING:
                cpu_affinity = None

        self.adapter = adapter
        self.cycle_time = cycle_time_ms / 1000.0
        self.processdata_cycle_time = (processdata_cycle_ms / 1000.0
                                       if processdata_cycle_ms is not None
                                       else 0.001)

        self.master = None
        self._slaves = []
        self._slaves_snapshot = ()
        self._lock = threading.Lock()

        self._pd_thread = None
        self._pdo_thread = None
        self._check_thread = None
        self._pd_stop = None
        self._pdo_stop = None
        self._check_stop = None

        self._comm_ok_count = 0
        self._comm_error_count = 0
        self._actual_wkc = 0

        self._timing_stats_enabled = timing_stats
        self._timing_stats = self._empty_timing_stats()
        self.timing_policy = timing_policy
        self._wait_deadline = _make_wait_deadline(timing_policy)
        self.high_priority = high_priority
        self.timer_resolution_us = (int(timer_resolution_us)
                                    if timer_resolution_us is not None
                                    else None)
        self.cpu_affinity = cpu_affinity
        self._actual_timer_resolution_us = None

        self.auto_reconnect = True
        self._reconnecting = threading.Event()

        self.dc_sync0_cycle_ns = dc_sync0_cycle_ns
        self.op_attempts = op_attempts
        self.op_timeout_s = op_timeout_s
        self.sdo_read_timeout_us = sdo_read_timeout_us
        self.sdo_write_timeout_us = sdo_write_timeout_us
        self.config_map_error_filter = config_map_error_filter
        self.reconnect_lock = reconnect_lock
        self.on_connection_lost = on_connection_lost
        self.on_reconnected = on_reconnected

        # A slave that drops to SAFE-OP(+ERROR) — typically AL 0x1B, SM
        # watchdog, after a blocking mailbox call stalled the ProcessData
        # thread — is brought back by the ack -> OP-request cycle within
        # about a second.  Such checks only count toward the reconnect
        # threshold once the slave has not been seen in OP for this long.
        self.recover_grace_s = 5.0
        self._last_op_seen = 0.0

        # `settled` is set once every slave has been in OP with a full
        # working counter for `settle_time_s` after open()/reconnect and is
        # cleared whenever a reconnect starts.  Applications should hold
        # their non-essential SDO traffic (telemetry, version reads, ...)
        # until the bus is settled: mailbox calls block the ProcessData
        # thread, and a burst of them right after bring-up trips the slave's
        # SM watchdog before it ever runs a clean cycle.
        self.settle_time_s = 2.0
        self.settled = threading.Event()
        self._clean_since = None

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    def __del__(self):
        try:
            if self.master:
                self.close()
        except Exception:
            pass

    @staticmethod
    def _read_network_config(pdo_config_path):
        """Read the 'network' section from an ethercat_config.json file."""
        try:
            raw = json.loads(Path(pdo_config_path).read_text(encoding="utf-8"))
            return raw.get("network", {})
        except Exception:
            return {}

    # ------------------------------------------------------------------
    # Adapter discovery
    # ------------------------------------------------------------------

    @staticmethod
    def list_adapters():
        """Return available network adapters from PySOEM."""
        return pysoem.find_adapters()

    @staticmethod
    def _resolve_adapter(adapter):
        """Find a pysoem adapter object by name string.

        Returns the adapter whose ``.name`` matches *adapter*.
        Raises ``ConnectionError`` if not found.
        """
        adapters = pysoem.find_adapters()
        if not adapters:
            raise ConnectionError("No network adapters found")
        if adapter is None:
            return adapters[0]
        for a in adapters:
            name = a.name.decode("utf-8", errors="replace") if isinstance(a.name, bytes) else str(a.name)
            if name == adapter:
                return a
        available = ", ".join(
            (a.name.decode("utf-8", errors="replace") if isinstance(a.name, bytes) else str(a.name))
            for a in adapters
        )
        raise ConnectionError(f"Adapter '{adapter}' not found. Available: {available}")

    # ------------------------------------------------------------------
    # Bus discovery
    # ------------------------------------------------------------------

    @classmethod
    def discover(cls, adapter=None, pdo_config_path=None):
        """Scan the EtherCAT bus and return information about every slave.

        Opens the adapter, runs ``config_init`` + ``config_map`` to read
        each slave's identity, I/O sizes, and PDO assignments, then
        closes the adapter.  Does **not** transition to OP.

        Args:
            adapter: Network-adapter name/UID string.
            pdo_config_path: Optional path to an ``ethercat_config.json``
                file.  Per-slave PDO assignments are applied before
                ``config_map`` so that I/O sizes reflect the intended
                mapping.

        Returns:
            list[dict]: One dict per slave with identity, I/O sizes,
            PDO assignments, and available PDOs.
        """
        resolved = cls._resolve_adapter(adapter)

        pdo_config = load_pdo_config(pdo_config_path) if pdo_config_path else None

        master = pysoem.Master()
        master.open(resolved.name)

        try:
            n_slaves = master.config_init()
            if n_slaves <= 0:
                master.close()
                return []

            register_emergency_callbacks(master)

            for i, slave in enumerate(master.slaves):
                name = slave.name if isinstance(slave.name, str) else \
                    slave.name.decode("utf-8", errors="replace")
                apply_startup_sdos(slave, get_slave_startup(pdo_config, i),
                                   "IP", name=f"[{i}] {name}")

            for i, slave in enumerate(master.slaves):
                supports_mapping = slave_supports_coe_pdo_mapping(slave)
                supports_assign = slave_supports_pdo_assignment(slave)
                if not (supports_mapping or supports_assign):
                    continue
                try:
                    rx, tx = get_slave_pdo(pdo_config, i)
                    if rx or tx:
                        configure_pdo_mapping(slave, rx_pdo=rx, tx_pdo=tx)
                    elif supports_mapping:
                        sanitize_invalid_pdo_assignments(slave)
                except Exception:
                    pass

            for i, slave in enumerate(master.slaves):
                name = slave.name if isinstance(slave.name, str) else \
                    slave.name.decode("utf-8", errors="replace")
                apply_startup_sdos(slave, get_slave_startup(pdo_config, i),
                                   "PS", name=f"[{i}] {name}")

            master.config_map()

            slaves = []
            for i, slave in enumerate(master.slaves):
                pdo_cache = {}
                info = {
                    "index": i,
                    "name": slave.name if isinstance(slave.name, str)
                            else slave.name.decode("utf-8", errors="replace"),
                    "vendor_id": f"0x{slave.man:08X}",
                    "product_code": f"0x{slave.id:08X}",
                    "revision": f"0x{slave.rev:08X}",
                    "state": _state_name(slave.state),
                    "output_bytes": len(slave.output) if slave.output else 0,
                    "input_bytes": len(slave.input) if slave.input else 0,
                }

                cls._read_identity_strings(slave, info)
                info["rx_pdo"] = cls._read_pdo_assignment(slave, 0x1C12, "RxPDO", pdo_cache)
                info["tx_pdo"] = cls._read_pdo_assignment(slave, 0x1C13, "TxPDO", pdo_cache)
                avail_rx, avail_tx = cls._discover_available_pdos(slave, pdo_cache)
                info["available_rx_pdo"] = avail_rx
                info["available_tx_pdo"] = avail_tx

                has_io = info["input_bytes"] > 0 or info["output_bytes"] > 0
                no_coe = not avail_rx and not avail_tx
                if has_io and no_coe:
                    info["sii_only"] = True
                    if info["output_bytes"] > 0 and not avail_rx:
                        sii_rx = {
                            "pdo_index": "SII",
                            "label": f"Fixed EEPROM mapping ({info['output_bytes']} B outputs)",
                            "readonly": True,
                            "objects": [],
                        }
                        info["available_rx_pdo"] = [sii_rx]
                    if info["input_bytes"] > 0 and not avail_tx:
                        sii_tx = {
                            "pdo_index": "SII",
                            "label": f"Fixed EEPROM mapping ({info['input_bytes']} B inputs)",
                            "readonly": True,
                            "objects": [],
                        }
                        info["available_tx_pdo"] = [sii_tx]

                slaves.append(info)
        finally:
            master.close()

        return slaves

    @staticmethod
    def _read_identity_strings(slave, info):
        """Read CoE identity objects 0x1008 / 0x1009 / 0x100A via SDO."""
        for key, idx in [
            ("device_name", 0x1008),
            ("hw_version", 0x1009),
            ("fw_version", 0x100A),
        ]:
            info[key] = ""
            for sz in (128, None):
                try:
                    raw = (slave.sdo_read(idx, 0) if sz is None
                           else slave.sdo_read(idx, 0, sz))
                    if raw:
                        s = raw.decode("utf-8", errors="replace").rstrip("\x00").strip()
                        if len(s) > len(info[key]):
                            info[key] = s
                except Exception:
                    continue

    @classmethod
    def _read_pdo_assignment(cls, slave, sm_index, label, _cache=None):
        """Read PDO assignment list from SM2 (0x1C12) or SM3 (0x1C13).

        Returns a list of dicts with ``pdo_index`` and ``objects``.
        """
        result = []
        try:
            raw = slave.sdo_read(sm_index, 0)
            n_pdos = raw[0] if raw else 0
        except Exception:
            return result

        for sub in range(1, n_pdos + 1):
            try:
                raw = slave.sdo_read(sm_index, sub, 2)
                pdo_idx = struct.unpack("<H", raw[:2])[0]
            except Exception:
                continue

            if pdo_idx and not pdo_mapping_exists(slave, pdo_idx):
                continue

            pdo_entry = {"pdo_index": f"0x{pdo_idx:04X}", "objects": []}
            pdo_entry["objects"] = cls._read_pdo_mapping(slave, pdo_idx, _cache)
            result.append(pdo_entry)

        return result

    @staticmethod
    def _read_pdo_mapping(slave, pdo_index, _cache=None):
        """Read the mapping entries for a single PDO index.

        Each mapping entry is a 32-bit value:
          bits 31..16 = object index
          bits 15..8  = subindex
          bits  7..0  = bit length
        """
        if _cache is not None and pdo_index in _cache:
            return _cache[pdo_index]
        objects = []
        try:
            raw = slave.sdo_read(pdo_index, 0)
            n_entries = raw[0] if raw else 0
        except Exception:
            if _cache is not None:
                _cache[pdo_index] = objects
            return objects

        for sub in range(1, n_entries + 1):
            try:
                raw = slave.sdo_read(pdo_index, sub, 4)
                mapping = struct.unpack("<I", raw[:4])[0]
                obj_index = (mapping >> 16) & 0xFFFF
                obj_sub = (mapping >> 8) & 0xFF
                bit_len = mapping & 0xFF
                objects.append({
                    "index": f"0x{obj_index:04X}",
                    "subindex": obj_sub,
                    "bits": bit_len,
                })
            except Exception:
                continue
        if _cache is not None:
            _cache[pdo_index] = objects
        return objects

    @classmethod
    def _discover_available_pdos(cls, slave, _cache=None):
        """Probe a slave for all available RxPDO and TxPDO indices.

        Scans 0x1600..0x160F (RxPDO) and 0x1A00..0x1A0F (TxPDO).

        Returns:
            tuple[list, list]: (available_rx_pdo, available_tx_pdo).
        """
        rx = []
        for idx in range(0x1600, 0x1610):
            try:
                raw = slave.sdo_read(idx, 0)
                n = raw[0] if raw else 0
                if n > 0:
                    rx.append({
                        "pdo_index": f"0x{idx:04X}",
                        "objects": cls._read_pdo_mapping(slave, idx, _cache),
                    })
            except Exception:
                continue

        tx = []
        for idx in range(0x1A00, 0x1A10):
            try:
                raw = slave.sdo_read(idx, 0)
                n = raw[0] if raw else 0
                if n > 0:
                    tx.append({
                        "pdo_index": f"0x{idx:04X}",
                        "objects": cls._read_pdo_mapping(slave, idx, _cache),
                    })
            except Exception:
                continue

        return rx, tx

    # ------------------------------------------------------------------
    # Slave registration
    # ------------------------------------------------------------------

    def register_slave(self, slave_handle):
        """Register a slave handle to participate in the PDO cycle.

        The handle must implement:

        - ``slave_index`` (int) — which pysoem slave to read/write
        - ``configure(pysoem_slave, rx_pdo=, tx_pdo=)`` — PDO mapping
        - ``pdo_update(master, reconnecting)`` — called each PDO cycle
        - ``seed_tx(pysoem_slave)`` — initial TX buffer
        - ``safe_stop()`` — graceful shutdown
        - ``on_reconnect(master)`` — post-reconnect hook
        """
        with self._lock:
            self._slaves.append(slave_handle)
            self._slaves_snapshot = tuple(self._slaves)

    def unregister_slave(self, slave_handle):
        """Remove a slave handle from the PDO cycle."""
        with self._lock:
            self._slaves = [s for s in self._slaves if s is not slave_handle]
            self._slaves_snapshot = tuple(self._slaves)

    # ------------------------------------------------------------------
    # Open / Close
    # ------------------------------------------------------------------

    def open(self):
        """Open the EtherCAT connection and bring all slaves to OP.

        Raises:
            ConnectionError: If no adapters/slaves found or state
                transition fails.
            ConfigurationError: If PDO mapping fails.
        """
        if os.name != "nt" and os.geteuid() != 0:
            raise PermissionError(
                "EtherCAT requires raw socket access. "
                "Please run with sudo: sudo python your_script.py"
            )

        adapter = self._resolve_adapter(self.adapter)
        print(f"[BUS] Connecting to: {adapter.name}")

        self._open_master(adapter.name)
        print(f"[BUS] Found {len(self.master.slaves)} EtherCAT slave(s)")
        self._bring_up(start_threads=True)

    # ------------------------------------------------------------------
    # Bring-up steps (shared by open() and _attempt_reconnect())
    # ------------------------------------------------------------------
    # Subclasses may override the individual steps, e.g. `_configure_pdos`
    # for slaves that need a non-standard mapping sequence.

    def _open_master(self, adapter_name):
        """Create the pysoem master and scan the bus (INIT -> PRE-OP)."""
        self.settled.clear()
        self._clean_since = None
        self.master = pysoem.Master()
        self.master.open(adapter_name)
        self.master.in_op = False
        self.master.do_check_state = False
        if self.master.config_init() <= 0:
            raise ConnectionError("No EtherCAT slaves found")
        if self.sdo_read_timeout_us is not None:
            self.master.sdo_read_timeout = self.sdo_read_timeout_us
        if self.sdo_write_timeout_us is not None:
            self.master.sdo_write_timeout = self.sdo_write_timeout_us
        register_emergency_callbacks(self.master)
        for slave in self.master.slaves:
            slave.is_lost = False

    def _bring_up(self, start_threads):
        """PRE-OP -> SAFE-OP -> OP for a freshly scanned master.

        With ``start_threads=True`` (initial open) the cyclic threads are
        started before OP is requested.  With ``False`` (reconnect) the
        threads are already running but idle while ``_reconnecting`` is
        set, so :meth:`_reach_op` pumps the process data itself.
        """
        # CoE Init->PreOP startup writes (e.g. EL2574 revision/diag) before mapping.
        self._apply_startup_sdos("IP")
        self._configure_pdos()
        # CoE PreOP->SafeOP startup writes (e.g. EL2574 0xF030 slot config) after
        # the PDO assignment and before config_map() so process data is sized.
        self._apply_startup_sdos("PS")
        self._map_io()

        if self.master.state_check(pysoem.SAFEOP_STATE, 50000) != pysoem.SAFEOP_STATE:
            details = self._slave_state_report()
            raise ConnectionError(f"Failed to reach SAFE-OP state.\n{details}")
        print("[BUS] Reached SAFE-OP state")

        self._configure_dc()

        with self._lock:
            for handle in self._slaves:
                handle.seed_tx(self.master.slaves[handle.slave_index])

        if start_threads:
            self._start_threads()
        try:
            self._reach_op(pump=not start_threads)
        except Exception:
            if start_threads:
                self._stop_threads()
            raise

        self.master.in_op = True
        self._last_op_seen = time.monotonic()
        print("[BUS] Reached OP state — bus ready")

    def _configure_pdos(self):
        """Write the CoE PDO assignment for every slave (in PRE-OP)."""
        with self._lock:
            for handle in self._slaves:
                try:
                    rx, tx = get_slave_pdo(self.pdo_config, handle.slave_index)
                    pysoem_slave = self.master.slaves[handle.slave_index]
                    handle.configure(pysoem_slave, rx_pdo=rx, tx_pdo=tx)
                    print(f"[BUS] Slave {handle.slave_index}: "
                          f"configured RxPDO={[f'0x{p:04X}' for p in (rx or [])]} "
                          f"TxPDO={[f'0x{p:04X}' for p in (tx or [])]}")
                except Exception as exc:
                    raise ConfigurationError(
                        f"PDO mapping failed for slave {handle.slave_index}: {exc}"
                    ) from exc

        # Slaves with no registered handle (e.g. web UI omits 0‑byte devices after
        # discover) still need CoE PDO mapping from get_slave_pdo / ethercat_config.
        registered = {h.slave_index for h in self._slaves}
        for idx, slave in enumerate(self.master.slaves):
            if idx in registered:
                continue
            try:
                rx, tx = get_slave_pdo(self.pdo_config, idx)
                if not (rx or tx):
                    continue
                if not (slave_supports_coe_pdo_mapping(slave)
                        or slave_supports_pdo_assignment(slave)):
                    continue
                configure_pdo_mapping(slave, rx_pdo=rx, tx_pdo=tx)
                print(
                    f"[BUS] Slave {idx}: "
                    f"configured RxPDO={[f'0x{p:04X}' for p in (rx or [])]} "
                    f"TxPDO={[f'0x{p:04X}' for p in (tx or [])]} "
                    f"(pdo_config, no handle)"
                )
            except Exception as exc:
                raise ConfigurationError(
                    f"PDO mapping failed for slave {idx}: {exc}"
                ) from exc

        for slave in self.master.slaves:
            if not slave_supports_coe_pdo_mapping(slave):
                continue
            try:
                sanitize_invalid_pdo_assignments(slave)
            except Exception:
                pass

    def _map_io(self):
        """Build the process-data image (``config_map``)."""
        try:
            self.master.config_map()
        except Exception as exc:
            if self.config_map_error_filter is not None and self.config_map_error_filter(exc):
                print(f"[BUS] config_map() reported tolerated errors: {exc}")
            else:
                details = self._slave_state_report()
                raise ConfigurationError(
                    f"config_map() failed: {exc}. {details}"
                ) from exc

        print("[BUS] I/O map after config_map():")
        for i, slave in enumerate(self.master.slaves):
            out_sz = len(slave.output) if slave.output else 0
            in_sz = len(slave.input) if slave.input else 0
            name = slave.name if isinstance(slave.name, str) else slave.name.decode("utf-8", errors="replace")
            print(f"  [{i}] {name}: Out={out_sz}B, In={in_sz}B")

    def _configure_dc(self):
        """Enable DC Sync0 on every slave when ``dc_sync0_cycle_ns`` is set."""
        if not self.dc_sync0_cycle_ns:
            return
        cycle_ns = int(self.dc_sync0_cycle_ns)
        for i, slave in enumerate(self.master.slaves):
            slave.dc_sync(1, cycle_ns)
            print(f"[BUS] Slave {i}: DC Sync0 enabled, cycle={cycle_ns} ns")

    def _reach_op(self, pump):
        """Request OP up to ``op_attempts`` times, ``op_timeout_s`` each.

        Polls with short ``state_check`` timeouts so the ProcessData thread
        can keep feeding the slave watchdog between checks — a single 50 ms
        state_check holds the GIL and starves the cyclic frames.  With
        ``pump=True`` the frames are sent from here instead (the cyclic
        threads are parked during a reconnect); slaves refuse OP without
        valid process data.
        """
        attempts = max(1, int(self.op_attempts))
        for attempt in range(1, attempts + 1):
            self.master.state = pysoem.OP_STATE
            self.master.write_state()
            print(f"[BUS] Requested OP state transition ({attempt}/{attempts})...")
            deadline = time.perf_counter() + self.op_timeout_s
            while time.perf_counter() < deadline:
                if pump:
                    self.master.send_processdata(**_SEND_PD_KWARGS)
                    self.master.receive_processdata(10000, **_RECV_PD_KWARGS)
                if self.master.state_check(pysoem.OP_STATE, 1000) == pysoem.OP_STATE:
                    return
                time.sleep(0.005 if pump else 0.001)
            if attempt < attempts:
                print(f"[BUS] OP not reached: {self._slave_state_report()}")
                time.sleep(0.5)
        raise ConnectionError(
            f"Failed to reach OP state after {attempts} attempt(s).\n"
            f"{self._slave_state_report()}"
        )

    _AL_STATUS_CODES = {
        0x0000: "No error",
        0x0001: "Unspecified error",
        0x0003: "Invalid device setup (modular: 0xF030 != detected 0xF050)",
        0x0011: "Invalid requested state change",
        0x0012: "Unknown requested state",
        0x0013: "Bootstrap not supported",
        0x0014: "No valid firmware",
        0x0015: "Invalid mailbox configuration (BOOT)",
        0x0016: "Invalid mailbox configuration (PREOP)",
        0x0017: "Invalid sync manager configuration",
        0x0018: "No valid inputs available",
        0x0019: "No valid outputs",
        0x001A: "Synchronization error",
        0x001B: "Sync manager watchdog",
        0x001C: "Invalid sync manager types",
        0x001D: "Invalid output configuration",
        0x001E: "Invalid input configuration",
        0x001F: "Invalid watchdog configuration",
        0x0020: "Slave needs cold start",
        0x0021: "Slave needs INIT",
        0x0022: "Slave needs PREOP",
        0x0023: "Slave needs SAFEOP",
        0x0024: "Invalid input mapping",
        0x0025: "Invalid output mapping",
        0x0026: "Inconsistent settings",
        0x0027: "FreeRun not supported",
        0x0028: "SyncMode not supported",
        0x0029: "FreeRun needs 3-buffer mode",
        0x002A: "Background watchdog",
        0x002B: "No valid inputs and outputs",
        0x002C: "Fatal sync error",
        0x002D: "No sync error",
        0x002E: "Invalid input FMMU configuration",
        0x0030: "Invalid DC sync configuration",
        0x0031: "Invalid DC latch configuration",
        0x0032: "PLL error",
        0x0033: "DC sync I/O error",
        0x0034: "DC sync timeout",
        0x0035: "DC invalid sync cycle time",
        0x0036: "DC sync0 cycle time",
        0x0037: "DC sync1 cycle time",
        0x0041: "MBX_AOE",
        0x0042: "MBX_EOE",
        0x0043: "MBX_COE",
        0x0044: "MBX_FOE",
        0x0045: "MBX_SOE",
        0x004F: "MBX_VOE",
        0x0050: "EEPROM no access",
        0x0051: "EEPROM error",
        0x0060: "Slave restarted locally",
        0x0061: "Device identification value updated",
        0x0070: "Invalid module configuration (0xF030 != 0xF050)",
        0x00F0: "Application controller available",
    }

    def _apply_startup_sdos(self, transition):
        """Apply configured CoE startup SDO writes for the given transition.

        *transition* is ``"IP"`` (Init->PreOP) or ``"PS"`` (PreOP->SafeOP).
        Mirrors the TwinCAT Startup tab; needed for modular terminals such as
        the EL2574 (0xF030 slot config).
        """
        if not self.pdo_config:
            return
        for idx, slave in enumerate(self.master.slaves):
            entries = get_slave_startup(self.pdo_config, idx)
            if not entries:
                continue
            name = slave.name if isinstance(slave.name, str) else \
                slave.name.decode("utf-8", errors="replace")
            apply_startup_sdos(slave, entries, transition, name=f"[{idx}] {name}")

    @staticmethod
    def _read_al_status_code(slave):
        """Read the ESC AL Status Code register (0x0134) for a slave.

        Fallback when pysoem's ``al_status`` attribute is empty. Returns the
        16-bit code, or ``None`` if the register read is unavailable.
        """
        reader = getattr(slave, "read_reg", None) or getattr(slave, "fprd", None)
        if reader is None:
            return None
        try:
            raw = reader(0x0134, 2)
            if raw and len(raw) >= 2:
                return struct.unpack("<H", bytes(raw[:2]))[0]
        except Exception:
            return None
        return None

    def _slave_state_report(self):
        """Build a detailed diagnostic string for each slave."""
        # Refresh .state / .al_status from the slaves; pysoem only populates
        # al_status after an explicit read_state(), otherwise it reports N/A.
        try:
            self.master.read_state()
        except Exception:
            pass

        lines = []
        for i, slave in enumerate(self.master.slaves):
            state = _state_name(slave.state)
            name = getattr(slave, "name", "") or f"slave {i}"
            if isinstance(name, bytes):
                name = name.decode("utf-8", errors="replace")

            al_status = getattr(slave, "al_status", None)
            if not al_status:
                al_status = self._read_al_status_code(slave)
            al_hex = f"0x{al_status:04X}" if al_status else "N/A"
            al_text = self._AL_STATUS_CODES.get(al_status, "Unknown") if al_status else ""

            out_sz = len(slave.output) if slave.output else 0
            in_sz = len(slave.input) if slave.input else 0

            line = f"  [{i}] {name}: state={state}, AL={al_hex}"
            if al_text:
                line += f" ({al_text})"
            line += f", Out={out_sz}B, In={in_sz}B"
            lines.append(line)

        if lines:
            return "Slave details:\n" + "\n".join(lines)
        return "No slave state info available."

    def close(self):
        """Stop all slaves and close the EtherCAT connection."""
        with self._lock:
            for handle in self._slaves:
                try:
                    handle.safe_stop()
                except Exception:
                    pass

        if self.master:
            self.master.in_op = False
        self.settled.clear()

        self._stop_threads()

        if self.master:
            self.master.close()
            self.master = None
            print("[BUS] Disconnected")

    @property
    def connected(self):
        return self.master is not None and self.master.in_op

    @staticmethod
    def _empty_timing_stats():
        return {
            "pd_cycles": 0,
            "pd_missed": 0,
            "pd_errors": 0,
            "pd_min_s": float("inf"),
            "pd_max_s": 0.0,
            "pd_sum_s": 0.0,
            "pd_sum_sq_s": 0.0,
            "pdo_cycles": 0,
            "pdo_missed": 0,
            "pdo_min_s": float("inf"),
            "pdo_max_s": 0.0,
            "pdo_sum_s": 0.0,
            "pdo_sum_sq_s": 0.0,
        }

    @staticmethod
    def _update_timing(stats, key_prefix, duration, cycle_s):
        stats[f"{key_prefix}_cycles"] += 1
        stats[f"{key_prefix}_sum_s"] += duration
        stats[f"{key_prefix}_sum_sq_s"] += duration * duration
        stats[f"{key_prefix}_min_s"] = min(stats[f"{key_prefix}_min_s"], duration)
        stats[f"{key_prefix}_max_s"] = max(stats[f"{key_prefix}_max_s"], duration)
        if duration > cycle_s * 1.05:
            stats[f"{key_prefix}_missed"] += 1

    def get_timing_stats(self):
        """Return collected ProcessData / PDO Update timing statistics.

        Only available when the bus was created with ``timing_stats=True``.
        Returns ``None`` when timing is disabled.
        """
        if not self._timing_stats_enabled:
            return None
        stats = self._timing_stats.copy()
        for key in ("pd", "pdo"):
            n = stats[f"{key}_cycles"]
            if n == 0:
                continue
            mean = stats[f"{key}_sum_s"] / n
            variance = stats[f"{key}_sum_sq_s"] / n - mean * mean
            stats[f"{key}_mean_ms"] = mean * 1000.0
            stats[f"{key}_std_ms"] = (variance ** 0.5) * 1000.0
            stats[f"{key}_min_ms"] = stats[f"{key}_min_s"] * 1000.0
            stats[f"{key}_max_ms"] = stats[f"{key}_max_s"] * 1000.0
        return stats

    def reset_timing_stats(self):
        """Reset collected timing statistics."""
        self._timing_stats = self._empty_timing_stats()

    # ------------------------------------------------------------------
    # Internal: threads
    # ------------------------------------------------------------------

    def _start_threads(self):
        if self._timing_stats_enabled:
            self.reset_timing_stats()
        if self.high_priority:
            _set_process_high_priority()
        if self.timer_resolution_us:
            self._actual_timer_resolution_us = _set_win_timer_resolution(
                self.timer_resolution_us
            )
            if self._actual_timer_resolution_us:
                print(f"[BUS] Windows timer resolution set to "
                      f"{self._actual_timer_resolution_us:.0f} us")
        self._pd_stop = threading.Event()
        self._pd_thread = threading.Thread(
            target=self._processdata_loop, name="EtherCAT-ProcessData", daemon=False
        )
        self._pd_thread.start()
        print(f"[BUS] ProcessData thread started ({self.processdata_cycle_time * 1000:.3f} ms cycle)")

        self._pdo_stop = threading.Event()
        self._pdo_thread = threading.Thread(
            target=self._pdo_update_loop, name="EtherCAT-PDOUpdate", daemon=False
        )
        self._pdo_thread.start()
        print(f"[BUS] PDO Update thread started ({self.cycle_time * 1000:.1f} ms cycle)")

        self._check_stop = threading.Event()
        self._check_thread = threading.Thread(
            target=self._check_loop, name="EtherCAT-StateCheck", daemon=False
        )
        self._check_thread.start()
        print("[BUS] State check thread started (300 ms cycle)")

    def _stop_threads(self):
        for evt in (self._pd_stop, self._pdo_stop, self._check_stop):
            if evt:
                evt.set()
        for thr in (self._pd_thread, self._pdo_thread, self._check_thread):
            if thr:
                thr.join(timeout=2.0)
        if self._actual_timer_resolution_us is not None:
            _reset_win_timer_resolution(self.timer_resolution_us)
            self._actual_timer_resolution_us = None
        self._pd_thread = None
        self._pdo_thread = None
        self._check_thread = None

    def _processdata_loop(self):
        """Fast send/receive at a fixed absolute deadline.

        Uses release_gil when available so other Python threads can run
        while pysoem waits for the EtherCAT frame.
        """
        cycle_s = self.processdata_cycle_time
        timing = self._timing_stats_enabled
        stats = self._timing_stats
        update_timing = self._update_timing
        pd_stop = self._pd_stop
        reconnecting = self._reconnecting
        send_pd_kwargs = _SEND_PD_KWARGS
        recv_pd_kwargs = _RECV_PD_KWARGS
        wait_deadline = self._wait_deadline
        sleep = time.sleep
        perf_counter = time.perf_counter
        master = self.master
        send_processdata = master.send_processdata
        receive_processdata = master.receive_processdata
        t_next = perf_counter()
        t_start = t_next
        if self.high_priority:
            _set_current_thread_high_priority()
        if self.cpu_affinity:
            _set_current_thread_affinity(self.cpu_affinity)
        while not pd_stop.is_set():
            if reconnecting.is_set():
                sleep(0.05)
                t_next = perf_counter()
                t_start = t_next
                master = self.master
                if master is None:
                    continue
                send_processdata = master.send_processdata
                receive_processdata = master.receive_processdata
                continue
            try:
                send_processdata(**send_pd_kwargs)
                actual_wkc = receive_processdata(10000, **recv_pd_kwargs)
                self._actual_wkc = actual_wkc
                if actual_wkc != master.expected_wkc:
                    self._comm_error_count += 1
                    if master.in_op:
                        master.do_check_state = True
                    if timing:
                        stats["pd_errors"] += 1
                else:
                    self._comm_ok_count += 1
            except Exception:
                self._comm_error_count += 1
                if timing:
                    stats["pd_errors"] += 1
            t_next += cycle_s
            wait_deadline(t_next)
            if timing:
                t_now = perf_counter()
                update_timing(stats, "pd", t_now - t_start, cycle_s)
                t_start = t_now

    def _pdo_update_loop(self):
        """Iterate over all registered slaves: decode RX, encode TX."""
        cycle_s = self.cycle_time
        timing = self._timing_stats_enabled
        stats = self._timing_stats
        update_timing = self._update_timing
        pdo_stop = self._pdo_stop
        reconnecting = self._reconnecting
        wait_deadline = self._wait_deadline
        sleep = time.sleep
        perf_counter = time.perf_counter
        t_next = perf_counter()
        t_start = t_next
        if self.high_priority:
            _set_current_thread_high_priority()
        if self.cpu_affinity:
            _set_current_thread_affinity(self.cpu_affinity)
        while not pdo_stop.is_set():
            if reconnecting.is_set():
                sleep(0.05)
                t_next = perf_counter()
                t_start = t_next
                continue
            master = self.master
            for handle in self._slaves_snapshot:
                try:
                    handle.pdo_update(master, reconnecting)
                except Exception:
                    pass
            t_next += cycle_s
            wait_deadline(t_next)
            if timing:
                t_now = perf_counter()
                update_timing(stats, "pdo", t_now - t_start, cycle_s)
                t_start = t_now

    _RECOVERABLE_STATES = (pysoem.SAFEOP_STATE,
                           pysoem.SAFEOP_STATE + pysoem.STATE_ERROR)

    def is_clean(self):
        """True while the bus is in OP and every PDO datagram is answered.

        A slave that dropped to SAFE-OP disables its output SyncManager, so
        the working counter falls short of the expected value even though
        frames are still flowing.
        """
        master = self.master
        return (master is not None and master.in_op
                and not self._reconnecting.is_set()
                and self._actual_wkc >= master.expected_wkc)

    def wait_settled(self, timeout=None):
        """Block until :attr:`settled` is set (see ``__init__``)."""
        return self.settled.wait(timeout)

    def _update_settled(self, now):
        if not self.is_clean():
            self._clean_since = None
            return
        if self._clean_since is None:
            self._clean_since = now
        elif not self.settled.is_set() and now - self._clean_since >= self.settle_time_s:
            self.settled.set()
            print(f"[BUS] Settled — all slaves clean in OP for {self.settle_time_s:.0f}s")

    def _check_once(self, consecutive_lost):
        """One state-check tick.  Returns the updated lost counter."""
        now = time.monotonic()
        try:
            if self.master and self.master.in_op and (
                (self._actual_wkc < self.master.expected_wkc)
                or self.master.do_check_state
            ):
                self.master.do_check_state = False
                self.master.read_state()

                all_ok = True
                recoverable = True
                for i, slave in enumerate(self.master.slaves):
                    if slave.state != pysoem.OP_STATE:
                        all_ok = False
                        recoverable = recoverable and slave.state in self._RECOVERABLE_STATES
                        self.master.do_check_state = True
                        self._recover_slave(slave, i)

                if not self.master.do_check_state:
                    consecutive_lost = 0
                    self._last_op_seen = now
                elif not all_ok:
                    in_grace = now - self._last_op_seen < self.recover_grace_s
                    if not (recoverable and in_grace):
                        consecutive_lost += 1
            else:
                consecutive_lost = 0
                self._last_op_seen = now
        except Exception as exc:
            consecutive_lost += 1
            print(f"[BUS] State check failed ({consecutive_lost}): "
                  f"{type(exc).__name__}: {exc}")
        self._update_settled(now)
        return consecutive_lost

    def _check_loop(self):
        """Monitor slave health and attempt recovery — 300 ms cycle."""
        _consecutive_lost = 0
        _RECONNECT_THRESHOLD = 7
        _CYCLE_S = 0.3
        t_next = time.perf_counter()

        while not self._check_stop.is_set():
            if self._reconnecting.is_set():
                _consecutive_lost = 0
                time.sleep(0.1)
                t_next = time.perf_counter()
                continue

            _consecutive_lost = self._check_once(_consecutive_lost)

            if (
                _consecutive_lost >= _RECONNECT_THRESHOLD
                and self.auto_reconnect
                and not self._reconnecting.is_set()
            ):
                print(f"[BUS] Lost contact for "
                      f"{_consecutive_lost * _CYCLE_S:.1f}s — triggering reconnect")
                _consecutive_lost = 0
                self._attempt_reconnect()

            t_next += _CYCLE_S
            self._wait_deadline(t_next)

    # ------------------------------------------------------------------
    # Reconnect
    # ------------------------------------------------------------------

    def _attempt_reconnect(self):
        """Tear down the master and rebuild from scratch."""
        self._reconnecting.set()
        self.settled.clear()
        self._clean_since = None
        self.master.in_op = False
        print("[BUS] Connection lost — attempting reconnect ...")
        self._call_hook(self.on_connection_lost)
        time.sleep(0.1)

        # `reconnect_lock` is held while the master is closed, replaced and
        # brought up, so application threads that share the same lock never
        # run an SDO/FoE on a master that is being torn down (a use-after-free
        # in native code).  An SDO in flight is waited for first.
        lock = self.reconnect_lock if self.reconnect_lock is not None else nullcontext()

        with lock:
            try:
                self.master.close()
            except Exception:
                pass

        backoff = 1.0
        while not self._check_stop.is_set():
            try:
                with lock:
                    adapter = self._resolve_adapter(self.adapter)
                    self._open_master(adapter.name)
                    print(f"[BUS] Found {len(self.master.slaves)} EtherCAT slave(s)")
                    self._bring_up(start_threads=False)

                    self._comm_error_count = 0
                    # Stale value from the dead master; the ProcessData thread
                    # overwrites it on its first cycle.
                    self._actual_wkc = self.master.expected_wkc

                    with self._lock:
                        for handle in self._slaves:
                            handle.on_reconnect(self.master)

                    self._reconnecting.clear()
                print("[BUS] Successfully reconnected")
                self._call_hook(self.on_reconnected)
                return

            except Exception as exc:
                print(f"[BUS] Reconnect attempt failed: {exc} — retrying in {backoff:.0f}s")
                with lock:
                    try:
                        self.master.close()
                    except Exception:
                        pass
                    self.master = None
                self._check_stop.wait(backoff)
                backoff = min(backoff * 2, 10.0)

    @staticmethod
    def _call_hook(hook):
        if hook is None:
            return
        try:
            hook()
        except Exception as exc:
            print(f"[BUS] Hook {getattr(hook, '__name__', hook)} failed: "
                  f"{type(exc).__name__}: {exc}")

    @staticmethod
    def _recover_slave(slave, pos):
        """Attempt to recover a slave that left OP state."""
        if slave.state == (pysoem.SAFEOP_STATE + pysoem.STATE_ERROR):
            slave.state = pysoem.SAFEOP_STATE + pysoem.STATE_ACK
            slave.write_state()
        elif slave.state == pysoem.SAFEOP_STATE:
            slave.state = pysoem.OP_STATE
            slave.write_state()
        elif slave.state > pysoem.NONE_STATE:
            if slave.reconfig():
                slave.is_lost = False
        elif not slave.is_lost:
            slave.state_check(pysoem.OP_STATE)
            if slave.state == pysoem.NONE_STATE:
                slave.is_lost = True
                print(f"[BUS] ERROR: Slave {pos} lost!")

        if slave.is_lost:
            if slave.state == pysoem.NONE_STATE:
                if slave.recover():
                    slave.is_lost = False
                    print(f"[BUS] Slave {pos} recovered")
            else:
                slave.is_lost = False
