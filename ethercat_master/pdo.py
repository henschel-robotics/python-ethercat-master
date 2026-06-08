"""
EtherCAT Master — PDO mapping configuration
=============================================

Utilities for configuring SyncManager PDO assignments on EtherCAT slaves
via SDO writes.  Supports file-based (JSON) per-slave configuration.
"""

import json
import struct
from pathlib import Path


DEFAULT_RX_PDO = [0x1600]
DEFAULT_TX_PDO = [0x1A00]

# Beckhoff EL2574 (4-ch pixel LED output): process data is on RxPDO 0x1600 (SM2).
# TxPDO 0x1A00 (status/diagnostics) is optional and may be absent depending on FW.
EL2574_RX_PDO = [0x1600]


def pdo_mapping_exists(slave, pdo_index):
    """Return True if the PDO mapping object (0x160n / 0x1An0) exists in CoE."""
    try:
        slave.sdo_read(pdo_index, 0, 1)
        return True
    except Exception:
        return False


def pdo_assignment_object_exists(slave, assign_index):
    """Return True if the SM PDO assignment object (0x1C12 / 0x1C13) is present.

    Some terminals (e.g. Beckhoff EL2574 pixel LED) keep the mapping objects
    (0x1600...) fixed in firmware and *not* readable via SDO, while still
    exposing the SM assignment objects.  TwinCAT configures such devices with
    "PDO assignment download" only — we mirror that by writing 0x1C12 / 0x1C13
    directly even though :func:`pdo_mapping_exists` returns False for them.
    """
    try:
        slave.sdo_read(assign_index, 0, 1)
        return True
    except Exception:
        return False


def slave_supports_pdo_assignment(slave):
    """Return True if SM2 (0x1C12) or SM3 (0x1C13) assignment objects exist."""
    return (pdo_assignment_object_exists(slave, 0x1C12)
            or pdo_assignment_object_exists(slave, 0x1C13))


def slave_supports_coe_pdo_mapping(slave):
    """Return True if the slave exposes configurable CoE PDO mapping objects.

    Simple terminals (e.g. EL1808) use fixed SII/EEPROM process data only; they
    have no 0x1600/0x1A00 mapping entries and must not be sanitized via CoE.
    """
    for idx in range(0x1600, 0x1610):
        if pdo_mapping_exists(slave, idx):
            return True
    for idx in range(0x1A00, 0x1A10):
        if pdo_mapping_exists(slave, idx):
            return True
    return False


def filter_existing_pdos(slave, pdo_list, label="PDO"):
    """Drop PDO indices that are not present in the slave object dictionary."""
    name = getattr(slave, "name", "?")
    if isinstance(name, bytes):
        name = name.decode("utf-8", errors="replace")
    existing = []
    for pdo in pdo_list:
        if pdo_mapping_exists(slave, pdo):
            existing.append(pdo)
        else:
            print(
                f"[PDO] {name}: skipping {label} 0x{pdo:04X} "
                f"(not in object dictionary)"
            )
    return existing


def read_assigned_pdos(slave, assign_index):
    """Read PDO indices currently assigned to 0x1C12 (outputs) or 0x1C13 (inputs)."""
    try:
        raw = slave.sdo_read(assign_index, 0, 1)
        n_pdos = raw[0] if raw else 0
    except Exception:
        return []

    assigned = []
    for sub in range(1, n_pdos + 1):
        try:
            raw = slave.sdo_read(assign_index, sub, 2)
            assigned.append(struct.unpack("<H", raw[:2])[0])
        except Exception:
            continue
    return assigned


def clear_pdo_assignment(slave, assign_index):
    """Set PDO assign object (0x1C12 / 0x1C13) count to zero."""
    try:
        slave.sdo_write(assign_index, 0, struct.pack("<B", 0))
        return True
    except Exception:
        return False


def sanitize_invalid_pdo_assignments(slave):
    """Clear SM PDO assignments that reference missing mapping objects.

    Beckhoff terminals may still have 0x1C13 -> 0x1A00 in EEPROM while the
    0x1A00 mapping object is absent (e.g. EL2574 status PDO disabled). That
    breaks PRE-OP / config_map unless the assignment is cleared.

    Skipped on SII-only devices (e.g. EL1808): their process data is fixed in
    EEPROM and assignment objects are not visible in CoE.
    """
    if not slave_supports_coe_pdo_mapping(slave):
        return

    name = getattr(slave, "name", "?")
    if isinstance(name, bytes):
        name = name.decode("utf-8", errors="replace")

    for assign_index, label in ((0x1C12, "RxPDO"), (0x1C13, "TxPDO")):
        assigned = read_assigned_pdos(slave, assign_index)
        invalid = [p for p in assigned if p and not pdo_mapping_exists(slave, p)]
        if not invalid:
            continue
        if clear_pdo_assignment(slave, assign_index):
            indices = ", ".join(f"0x{p:04X}" for p in invalid)
            print(
                f"[PDO] {name}: cleared {label} assignment "
                f"(missing mapping: {indices})"
            )


def _parse_hex_bytes(value):
    """Parse a CoE data payload into ``bytes``.

    Accepts space-separated hex bytes as shown in the TwinCAT Startup tab
    (``"04 00 0E 0A"``), a contiguous hex string (``"04000E0A"``), or an
    already-``bytes`` value.  Bytes are taken in the written order (the order
    TwinCAT sends them on the wire — little-endian for scalar values).
    """
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if not isinstance(value, str):
        raise ValueError(f"unsupported startup data: {value!r}")
    tokens = value.split()
    if len(tokens) > 1:
        return bytes(int(t, 16) for t in tokens)
    t = tokens[0]
    if t.lower().startswith("0x"):
        t = t[2:]
    if len(t) % 2:
        t = "0" + t
    return bytes.fromhex(t)


def _parse_index(value):
    """Parse a CoE index given as ``"0x1C12"`` or an int."""
    if isinstance(value, str) and value.lower().startswith("0x"):
        return int(value, 16)
    return int(value)


def _parse_startup_list(entries):
    """Normalize the JSON ``startup`` list into ready-to-write dicts.

    Each entry writes ``index:subindex`` either from literal ``data`` bytes or,
    when ``copy_from`` is given, from the Complete-Access contents of another
    object (e.g. ``0xF030`` <- ``0xF050`` to make a modular device's configured
    module list equal its detected module list).
    """
    parsed = []
    for e in entries or []:
        try:
            entry = {
                "transition": str(e.get("transition", "PS")).upper(),
                "index": _parse_index(e["index"]),
                "subindex": int(e.get("subindex", 0)),
                "ca": bool(e.get("ca", False)),
                "comment": e.get("comment", ""),
                "copy_from": _parse_index(e["copy_from"]) if e.get("copy_from") is not None else None,
                "data": _parse_hex_bytes(e["data"]) if e.get("data") is not None else None,
            }
            parsed.append(entry)
        except Exception as exc:
            print(f"[PDO] Ignoring invalid startup entry {e!r}: {exc}")
    return parsed


def get_slave_startup(pdo_config, slave_index):
    """Return the parsed ``startup`` SDO list for a slave (empty if none)."""
    if not pdo_config:
        return []
    entry = pdo_config.get(slave_index)
    if entry:
        return entry.get("startup", []) or []
    return []


def apply_startup_sdos(slave, entries, transition, name="?"):
    """Apply the startup SDO writes whose transition matches *transition*.

    *transition* is ``"IP"`` (Init->PreOP) or ``"PS"`` (PreOP->SafeOP).  Writes
    are best-effort: a rejected write is logged and skipped so one bad/optional
    entry never aborts bring-up.  Returns the number of successful writes.
    """
    done = 0
    for e in entries:
        if e.get("transition", "PS") != transition:
            continue
        idx = e["index"]
        sub = e["subindex"]
        ca = e.get("ca", False)
        copy_from = e.get("copy_from")
        comment = (' (' + e['comment'] + ')') if e.get("comment") else ""

        data = e.get("data")
        if copy_from is not None:
            try:
                data = bytes(slave.sdo_read(copy_from, 0, 0, True))  # CA read
            except Exception as exc:
                print(f"[PDO] {name}: startup 0x{idx:04X}:{sub:02X} "
                      f"copy_from 0x{copy_from:04X} READ failed: {exc}")
                continue

        if data is None:
            print(f"[PDO] {name}: startup 0x{idx:04X}:{sub:02X} has no data, skipping")
            continue

        try:
            slave.sdo_write(idx, sub, data, ca)
            done += 1
            src = f"= 0x{copy_from:04X}" if copy_from is not None else ""
            print(f"[PDO] {name}: startup {transition} 0x{idx:04X}:{sub:02X} "
                  f"{'CA ' if ca else ''}<- {data.hex(' ')}{src}{comment}")
        except Exception as exc:
            print(f"[PDO] {name}: startup 0x{idx:04X}:{sub:02X} write FAILED: {exc}")
    return done


def load_pdo_config(path):
    """Load PDO mapping configuration from a JSON file.

    File format::

        {
          "default": {
            "rx_pdo": ["0x1600", "0x1605"],
            "tx_pdo": ["0x1A00", "0x1A05"]
          },
          "slaves": {
            "0": {
              "rx_pdo": ["0x1600", "0x1605"],
              "tx_pdo": ["0x1A00", "0x1A05"]
            }
          }
        }

    Hex strings (``"0x1600"``) and plain integers (``5632``) are both
    accepted for PDO indices.

    A slave may also carry a ``"startup"`` list of raw CoE SDO writes that
    mirror the TwinCAT *Startup* tab (e.g. the modular EL2574 slot config
    ``0xF030``).  Each entry::

        {
          "transition": "PS",          # "IP" (Init->PreOP) or "PS" (PreOP->SafeOP)
          "index": "0xF030",
          "subindex": 0,
          "ca": true,                  # Complete Access (optional, default false)
          "data": "04 00 0E 0A ...",   # space-separated hex bytes (as TwinCAT shows)
          "comment": "download slot cfg"
        }

    Args:
        path: Path to the JSON file.

    Returns:
        dict: Parsed config with integer PDO values, or ``None`` on
        error.
    """
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[PDO] Could not load {path}: {exc}")
        return None

    def _parse_list(lst):
        return [int(v, 16) if isinstance(v, str) and v.startswith("0x") else int(v)
                for v in lst]

    config = {}
    if "default" in raw:
        d = raw["default"]
        config["default"] = {
            "rx_pdo": _parse_list(d.get("rx_pdo", [])),
            "tx_pdo": _parse_list(d.get("tx_pdo", [])),
        }
    for key, val in raw.get("slaves", {}).items():
        config[int(key)] = {
            "rx_pdo": _parse_list(val.get("rx_pdo", [])),
            "tx_pdo": _parse_list(val.get("tx_pdo", [])),
            "startup": _parse_startup_list(val.get("startup", [])),
        }
    return config


def get_slave_pdo(pdo_config, slave_index, default_rx=None, default_tx=None):
    """Look up (rx_pdo, tx_pdo) lists for a slave index.

    Resolution order:

    1. Per-slave entry in ``pdo_config``, else the ``"default"`` section
       (only when ``pdo_config`` is loaded from JSON).
    2. If ``default_rx`` / ``default_tx`` arguments are set, those lists
       are used (after step 1 misses).
    3. Otherwise :data:`DEFAULT_RX_PDO` / :data:`DEFAULT_TX_PDO` so that
       discovery and :meth:`~ethercat_master.bus.EtherCATBus.open` still
       assign the usual Beckhoff SM2/SM3 PDOs when no file is present.
       Slaves without these objects in the CoE dictionary are unchanged
       because :func:`configure_pdo_mapping` filters non-existent PDOs.

    An explicit JSON entry with empty ``rx_pdo`` / ``tx_pdo`` lists is
    honored (intentional “do not remap via CoE”).

    Returns:
        tuple[list[int], list[int]]: (rx_pdo, tx_pdo) index lists.
    """
    if pdo_config:
        entry = pdo_config.get(slave_index, pdo_config.get("default"))
        if entry is not None:
            return entry["rx_pdo"], entry["tx_pdo"]
    if default_rx is not None or default_tx is not None:
        return list(default_rx or []), list(default_tx or [])
    return list(DEFAULT_RX_PDO), list(DEFAULT_TX_PDO)


def configure_pdo_mapping(slave, rx_pdo=None, tx_pdo=None, use_defaults=False):
    """Write SDO objects to configure PDO mapping on *slave*.

    Writes the SyncManager 2 (0x1C12, RxPDO) and SyncManager 3
    (0x1C13, TxPDO) assignment lists.  Silently skips a SyncManager
    if the slave does not support it (e.g. input-only devices have no SM2).

    Args:
        slave: A pysoem slave object (in PRE-OP or higher).
        rx_pdo: List of RxPDO indices to assign to SM2 (master -> slave).
            Pass an empty list to skip RxPDO configuration.
            ``None`` with ``use_defaults=True`` uses ``DEFAULT_RX_PDO``;
            otherwise ``None`` is treated as "do not change".
        tx_pdo: List of TxPDO indices to assign to SM3 (slave -> master).
            Pass an empty list to skip TxPDO configuration.
            ``None`` with ``use_defaults=True`` uses ``DEFAULT_TX_PDO``.
        use_defaults: When True, ``None`` rx/tx lists fall back to
            ``DEFAULT_RX_PDO`` / ``DEFAULT_TX_PDO``.  Bus discovery and
            config-file driven setup pass explicit lists and leave this False.

    Two device classes are handled:

    * **Mapping-readable** terminals (most Beckhoff): 0x1600.../0x1A00... are
      SDO-readable, so invalid assignments are sanitized and the configured
      PDO list is filtered against the object dictionary before writing.
    * **Assignment-only** terminals (e.g. EL2574): mapping objects are fixed in
      firmware and *not* SDO-readable, but 0x1C12 / 0x1C13 are writable.  We
      then trust the configured PDO list and write the assignment directly
      (no filtering, no sanitize — both rely on reading 0x160n which fails and
      would wrongly drop/clear valid entries).
    """
    has_mapping_objs = slave_supports_coe_pdo_mapping(slave)
    assignment_only = not has_mapping_objs and slave_supports_pdo_assignment(slave)
    if not has_mapping_objs and not assignment_only:
        return

    if has_mapping_objs:
        sanitize_invalid_pdo_assignments(slave)

    if use_defaults:
        if rx_pdo is None:
            rx_pdo = list(DEFAULT_RX_PDO)
        if tx_pdo is None:
            tx_pdo = list(DEFAULT_TX_PDO)
    else:
        if rx_pdo is None:
            rx_pdo = []
        if tx_pdo is None:
            tx_pdo = []

    if has_mapping_objs:
        rx_pdo = filter_existing_pdos(slave, rx_pdo, label="RxPDO")
        tx_pdo = filter_existing_pdos(slave, tx_pdo, label="TxPDO")

    name = getattr(slave, "name", "?")
    if isinstance(name, bytes):
        name = name.decode("utf-8", errors="replace")

    if assignment_only:
        print(
            f"[PDO] {name}: assignment-only download "
            f"(mapping objects not SDO-readable) — "
            f"RxPDO={[f'0x{p:04X}' for p in rx_pdo]} "
            f"TxPDO={[f'0x{p:04X}' for p in tx_pdo]}"
        )

    def _sdo_write(index, subindex, data, label=""):
        try:
            slave.sdo_write(index, subindex, data)
        except Exception as exc:
            raise RuntimeError(
                f"SDO write failed on {name}: "
                f"0x{index:04X}:0x{subindex:02X} {label} — {exc}"
            ) from exc

    def _try_sdo_write(index, subindex, data, ca=False):
        try:
            slave.sdo_write(index, subindex, data, ca)
            return True
        except Exception:
            return False

    def _write_assignment(assign_index, pdo_list, label):
        """Assign *pdo_list* to an SM PDO-assign object (0x1C12 / 0x1C13).

        Beckhoff terminals such as the EL2574 only accept the assignment via
        **Complete Access** (whole object written at subindex 0:
        ``count:u8`` + pad + ``u16`` entries).  We try Complete Access first
        and fall back to the per-subindex method for terminals that reject CA.
        """
        ca_payload = struct.pack("<BB", len(pdo_list), 0) + b"".join(
            struct.pack("<H", p) for p in pdo_list
        )
        if _try_sdo_write(assign_index, 0x00, ca_payload, ca=True):
            print(f"[PDO] {name}: {label} assigned via Complete Access "
                  f"{[f'0x{p:04X}' for p in pdo_list]}")
            return

        if not _try_sdo_write(assign_index, 0x00, struct.pack("<B", 0)):
            print(f"[PDO] {name}: 0x{assign_index:04X} ({label}) not writable, skipping")
            return
        for i, pdo in enumerate(pdo_list, start=1):
            _sdo_write(assign_index, i, struct.pack("<H", pdo), f"{label}[{i}]=0x{pdo:04X}")
        _sdo_write(assign_index, 0x00, struct.pack("<B", len(pdo_list)),
                   f"set {label} count={len(pdo_list)}")
        print(f"[PDO] {name}: {label} assigned via subindex writes "
              f"{[f'0x{p:04X}' for p in pdo_list]}")

    if rx_pdo:
        _write_assignment(0x1C12, rx_pdo, "SM2 RxPDO")

    if tx_pdo:
        _write_assignment(0x1C13, tx_pdo, "SM3 TxPDO")
