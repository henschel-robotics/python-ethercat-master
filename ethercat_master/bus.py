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
    ├── ProcessData thread     (1 ms — raw frame send/receive)
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

import json
import struct
import threading
import time
from pathlib import Path

import pysoem

from .exceptions import ConnectionError, CommunicationError, ConfigurationError
from .pdo import configure_pdo_mapping, load_pdo_config, get_slave_pdo


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
        pdo_config_path: Optional path to a ``pdo_mapping.json`` file.
            When provided, per-slave PDO assignments are read from
            this file instead of using the hardcoded defaults.
    """

    def __init__(self, adapter=None, cycle_time_ms=10, pdo_config_path=None):
        if pdo_config_path:
            self.pdo_config = load_pdo_config(pdo_config_path)
            net = self._read_network_config(pdo_config_path)
            if adapter is None and net.get("adapter"):
                adapter = net["adapter"]
            if cycle_time_ms == 10 and net.get("cycle_ms"):
                cycle_time_ms = net["cycle_ms"]
        else:
            self.pdo_config = None

        self.adapter = adapter
        self.cycle_time = cycle_time_ms / 1000.0

        self.master = None
        self._slaves = []
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

        self.auto_reconnect = True
        self._reconnecting = threading.Event()

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
        """Read the 'network' section from a pdo_mapping.json file."""
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
            pdo_config_path: Optional path to a ``pdo_mapping.json``
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

            for i, slave in enumerate(master.slaves):
                try:
                    rx, tx = get_slave_pdo(pdo_config, i)
                    configure_pdo_mapping(slave, rx_pdo=rx, tx_pdo=tx)
                except Exception:
                    pass

            master.config_map()

            slaves = []
            for i, slave in enumerate(master.slaves):
                info = {
                    "index": i,
                    "name": slave.name if isinstance(slave.name, str)
                            else slave.name.decode("utf-8", errors="replace"),
                    "vendor_id": f"0x{slave.man:08X}",
                    "product_code": f"0x{slave.id:08X}",
                    "revision": f"0x{slave.rev:08X}",
                    "state": _state_name(slave.state),
                    "output_bytes": len(bytes(slave.output)) if slave.output else 0,
                    "input_bytes": len(bytes(slave.input)) if slave.input else 0,
                }

                cls._read_identity_strings(slave, info)
                info["rx_pdo"] = cls._read_pdo_assignment(slave, 0x1C12, "RxPDO")
                info["tx_pdo"] = cls._read_pdo_assignment(slave, 0x1C13, "TxPDO")
                avail_rx, avail_tx = cls._discover_available_pdos(slave)
                info["available_rx_pdo"] = avail_rx
                info["available_tx_pdo"] = avail_tx

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
            for sz in (64, 32, 16, None):
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
    def _read_pdo_assignment(cls, slave, sm_index, label):
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

            pdo_entry = {"pdo_index": f"0x{pdo_idx:04X}", "objects": []}
            pdo_entry["objects"] = cls._read_pdo_mapping(slave, pdo_idx)
            result.append(pdo_entry)

        return result

    @staticmethod
    def _read_pdo_mapping(slave, pdo_index):
        """Read the mapping entries for a single PDO index.

        Each mapping entry is a 32-bit value:
          bits 31..16 = object index
          bits 15..8  = subindex
          bits  7..0  = bit length
        """
        objects = []
        try:
            raw = slave.sdo_read(pdo_index, 0)
            n_entries = raw[0] if raw else 0
        except Exception:
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
        return objects

    @classmethod
    def _discover_available_pdos(cls, slave):
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
                        "objects": cls._read_pdo_mapping(slave, idx),
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
                        "objects": cls._read_pdo_mapping(slave, idx),
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

    def unregister_slave(self, slave_handle):
        """Remove a slave handle from the PDO cycle."""
        with self._lock:
            self._slaves = [s for s in self._slaves if s is not slave_handle]

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
        adapter = self._resolve_adapter(self.adapter)
        print(f"[BUS] Connecting to: {adapter.name}")

        self.master = pysoem.Master()
        self.master.open(adapter.name)
        self.master.in_op = False
        self.master.do_check_state = False

        if self.master.config_init() <= 0:
            raise ConnectionError("No EtherCAT slaves found")

        print(f"[BUS] Found {len(self.master.slaves)} EtherCAT slave(s)")

        for slave in self.master.slaves:
            slave.is_lost = False

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

        try:
            self.master.config_map()
        except Exception as exc:
            details = self._slave_state_report()
            raise ConfigurationError(
                f"config_map() failed: {exc}. {details}"
            ) from exc

        print("[BUS] I/O map after config_map():")
        for i, slave in enumerate(self.master.slaves):
            out_sz = len(bytes(slave.output)) if slave.output else 0
            in_sz = len(bytes(slave.input)) if slave.input else 0
            name = slave.name if isinstance(slave.name, str) else slave.name.decode("utf-8", errors="replace")
            print(f"  [{i}] {name}: Out={out_sz}B, In={in_sz}B")

        if self.master.state_check(pysoem.SAFEOP_STATE, 50000) != pysoem.SAFEOP_STATE:
            details = self._slave_state_report()
            raise ConnectionError(
                f"Failed to reach SAFE-OP state.\n{details}"
            )
        print("[BUS] Reached SAFE-OP state")

        with self._lock:
            for handle in self._slaves:
                handle.seed_tx(self.master.slaves[handle.slave_index])

        self._start_threads()

        self.master.state = pysoem.OP_STATE
        self.master.write_state()
        print("[BUS] Requested OP state transition...")

        if self.master.state_check(pysoem.OP_STATE, 50000) != pysoem.OP_STATE:
            self._stop_threads()
            details = self._slave_state_report()
            raise ConnectionError(
                f"Failed to reach OP state.\n{details}"
            )

        self.master.in_op = True
        print("[BUS] Reached OP state — bus ready")

    _AL_STATUS_CODES = {
        0x0000: "No error",
        0x0001: "Unspecified error",
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
        0x00F0: "Application controller available",
    }

    def _slave_state_report(self):
        """Build a detailed diagnostic string for each slave."""
        lines = []
        for i, slave in enumerate(self.master.slaves):
            state = _state_name(slave.state)
            name = getattr(slave, "name", "") or f"slave {i}"
            if isinstance(name, bytes):
                name = name.decode("utf-8", errors="replace")

            al_status = getattr(slave, "al_status", None)
            al_hex = f"0x{al_status:04X}" if al_status else "N/A"
            al_text = self._AL_STATUS_CODES.get(al_status, "Unknown") if al_status else ""

            out_sz = len(bytes(slave.output)) if slave.output else 0
            in_sz = len(bytes(slave.input)) if slave.input else 0

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

        self._stop_threads()

        if self.master:
            self.master.close()
            self.master = None
            print("[BUS] Disconnected")

    @property
    def connected(self):
        return self.master is not None and self.master.in_op

    # ------------------------------------------------------------------
    # Internal: threads
    # ------------------------------------------------------------------

    def _start_threads(self):
        self._pd_stop = threading.Event()
        self._pd_thread = threading.Thread(
            target=self._processdata_loop, name="EtherCAT-ProcessData", daemon=False
        )
        self._pd_thread.start()
        print("[BUS] ProcessData thread started (1 ms cycle)")

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
        self._pd_thread = None
        self._pdo_thread = None
        self._check_thread = None

    def _processdata_loop(self):
        """Fast send/receive — 1 ms cycle. No locks, no processing."""
        while not self._pd_stop.is_set():
            if self._reconnecting.is_set():
                time.sleep(0.05)
                continue
            try:
                self.master.send_processdata()
                self._actual_wkc = self.master.receive_processdata(10000)
                if self._actual_wkc != self.master.expected_wkc:
                    self._comm_error_count += 1
                    if self.master.in_op:
                        self.master.do_check_state = True
                else:
                    self._comm_ok_count += 1
            except Exception:
                self._comm_error_count += 1
            time.sleep(0.001)

    def _pdo_update_loop(self):
        """Iterate over all registered slaves: decode RX, encode TX."""
        while not self._pdo_stop.is_set():
            if self._reconnecting.is_set():
                time.sleep(0.05)
                continue
            with self._lock:
                for handle in self._slaves:
                    try:
                        handle.pdo_update(self.master, self._reconnecting)
                    except Exception:
                        pass
            time.sleep(self.cycle_time)

    def _check_loop(self):
        """Monitor slave health and attempt recovery — 300 ms cycle."""
        _consecutive_lost = 0
        _RECONNECT_THRESHOLD = 7

        while not self._check_stop.is_set():
            if self._reconnecting.is_set():
                _consecutive_lost = 0
                time.sleep(0.1)
                continue

            try:
                if self.master and self.master.in_op and (
                    (self._actual_wkc < self.master.expected_wkc)
                    or self.master.do_check_state
                ):
                    self.master.do_check_state = False
                    self.master.read_state()

                    all_ok = True
                    for i, slave in enumerate(self.master.slaves):
                        if slave.state != pysoem.OP_STATE:
                            all_ok = False
                            self.master.do_check_state = True
                            self._recover_slave(slave, i)

                    if not self.master.do_check_state:
                        _consecutive_lost = 0
                    elif not all_ok:
                        _consecutive_lost += 1
                else:
                    _consecutive_lost = 0
            except Exception:
                _consecutive_lost += 1

            if (
                _consecutive_lost >= _RECONNECT_THRESHOLD
                and self.auto_reconnect
                and not self._reconnecting.is_set()
            ):
                print(f"[BUS] Lost contact for "
                      f"{_consecutive_lost * 0.3:.1f}s — triggering reconnect")
                _consecutive_lost = 0
                self._attempt_reconnect()

            time.sleep(0.3)

    # ------------------------------------------------------------------
    # Reconnect
    # ------------------------------------------------------------------

    def _attempt_reconnect(self):
        """Tear down the master and rebuild from scratch."""
        self._reconnecting.set()
        self.master.in_op = False
        print("[BUS] Connection lost — attempting reconnect ...")
        time.sleep(0.1)

        try:
            self.master.close()
        except Exception:
            pass

        backoff = 1.0
        while not self._check_stop.is_set():
            try:
                adapter = self._resolve_adapter(self.adapter)
                self.master = pysoem.Master()
                self.master.open(adapter.name)
                self.master.in_op = False
                self.master.do_check_state = False

                if self.master.config_init() <= 0:
                    raise CommunicationError("No EtherCAT slaves found")

                for slave in self.master.slaves:
                    slave.is_lost = False

                with self._lock:
                    for handle in self._slaves:
                        rx, tx = get_slave_pdo(self.pdo_config, handle.slave_index)
                        handle.configure(self.master.slaves[handle.slave_index],
                                         rx_pdo=rx, tx_pdo=tx)

                self.master.config_map()

                if self.master.state_check(
                    pysoem.SAFEOP_STATE, 50000
                ) != pysoem.SAFEOP_STATE:
                    raise CommunicationError("Failed to reach SAFE-OP")

                with self._lock:
                    for handle in self._slaves:
                        handle.seed_tx(self.master.slaves[handle.slave_index])

                self.master.state = pysoem.OP_STATE
                self.master.write_state()

                deadline = time.time() + 5.0
                reached_op = False
                while time.time() < deadline:
                    self.master.send_processdata()
                    self.master.receive_processdata(10000)
                    if self.master.state_check(
                        pysoem.OP_STATE, 1000
                    ) == pysoem.OP_STATE:
                        reached_op = True
                        break
                    time.sleep(0.005)

                if not reached_op:
                    raise CommunicationError("Failed to reach OP")

                self.master.in_op = True
                self._comm_error_count = 0

                with self._lock:
                    for handle in self._slaves:
                        handle.on_reconnect(self.master)

                self._reconnecting.clear()
                print("[BUS] Successfully reconnected")
                return

            except Exception as exc:
                print(f"[BUS] Reconnect attempt failed: {exc} — retrying in {backoff:.0f}s")
                try:
                    self.master.close()
                except Exception:
                    pass
                self.master = None
                self._check_stop.wait(backoff)
                backoff = min(backoff * 2, 10.0)

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
