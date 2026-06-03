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
        }
    return config


def get_slave_pdo(pdo_config, slave_index, default_rx=None, default_tx=None):
    """Look up (rx_pdo, tx_pdo) lists for a slave index.

    Falls back to the ``"default"`` key, then to the provided defaults.
    If no defaults are given and no config entry exists, returns empty
    lists so that ``configure_pdo_mapping`` skips unconfigured SMs.

    Returns:
        tuple[list[int], list[int]]: (rx_pdo, tx_pdo) index lists.
    """
    if pdo_config:
        entry = pdo_config.get(slave_index, pdo_config.get("default"))
        if entry:
            return entry["rx_pdo"], entry["tx_pdo"]
    return list(default_rx or []), list(default_tx or [])


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
    """
    if not slave_supports_coe_pdo_mapping(slave):
        return

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

    rx_pdo = filter_existing_pdos(slave, rx_pdo, label="RxPDO")
    tx_pdo = filter_existing_pdos(slave, tx_pdo, label="TxPDO")

    name = getattr(slave, "name", "?")
    if isinstance(name, bytes):
        name = name.decode("utf-8", errors="replace")

    def _sdo_write(index, subindex, data, label=""):
        try:
            slave.sdo_write(index, subindex, data)
        except Exception as exc:
            raise RuntimeError(
                f"SDO write failed on {name}: "
                f"0x{index:04X}:0x{subindex:02X} {label} — {exc}"
            ) from exc

    def _try_sdo_write(index, subindex, data, label=""):
        try:
            slave.sdo_write(index, subindex, data)
            return True
        except Exception:
            return False

    if rx_pdo:
        if _try_sdo_write(0x1C12, 0x00, struct.pack("<B", 0), "clear SM2 RxPDO count"):
            for i, pdo in enumerate(rx_pdo, start=1):
                _sdo_write(0x1C12, i, struct.pack("<H", pdo), f"RxPDO[{i}]=0x{pdo:04X}")
            _sdo_write(0x1C12, 0x00, struct.pack("<B", len(rx_pdo)),
                       f"set SM2 RxPDO count={len(rx_pdo)}")
        else:
            print(f"[PDO] {name}: SM2 (RxPDO) not supported, skipping")

    if tx_pdo:
        if _try_sdo_write(0x1C13, 0x00, struct.pack("<B", 0), "clear SM3 TxPDO count"):
            for i, pdo in enumerate(tx_pdo, start=1):
                _sdo_write(0x1C13, i, struct.pack("<H", pdo), f"TxPDO[{i}]=0x{pdo:04X}")
            _sdo_write(0x1C13, 0x00, struct.pack("<B", len(tx_pdo)),
                       f"set SM3 TxPDO count={len(tx_pdo)}")
        else:
            print(f"[PDO] {name}: SM3 (TxPDO) not supported, skipping")
