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

    Falls back to the ``"default"`` key, then to the provided defaults
    (or ``DEFAULT_RX_PDO`` / ``DEFAULT_TX_PDO``).

    Returns:
        tuple[list[int], list[int]]: (rx_pdo, tx_pdo) index lists.
    """
    if pdo_config:
        entry = pdo_config.get(slave_index, pdo_config.get("default"))
        if entry:
            return entry["rx_pdo"], entry["tx_pdo"]
    return list(default_rx or DEFAULT_RX_PDO), list(default_tx or DEFAULT_TX_PDO)


def configure_pdo_mapping(slave, rx_pdo=None, tx_pdo=None):
    """Write SDO objects to configure PDO mapping on *slave*.

    Writes the SyncManager 2 (0x1C12, RxPDO) and SyncManager 3
    (0x1C13, TxPDO) assignment lists.

    Args:
        slave: A pysoem slave object (in PRE-OP or higher).
        rx_pdo: List of RxPDO indices to assign to SM2 (master -> slave).
            Defaults to ``DEFAULT_RX_PDO``.
        tx_pdo: List of TxPDO indices to assign to SM3 (slave -> master).
            Defaults to ``DEFAULT_TX_PDO``.
    """
    if rx_pdo is None:
        rx_pdo = DEFAULT_RX_PDO
    if tx_pdo is None:
        tx_pdo = DEFAULT_TX_PDO

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

    _sdo_write(0x1C12, 0x00, struct.pack("<B", 0), "clear SM2 RxPDO count")
    _sdo_write(0x1C13, 0x00, struct.pack("<B", 0), "clear SM3 TxPDO count")

    for i, pdo in enumerate(rx_pdo, start=1):
        _sdo_write(0x1C12, i, struct.pack("<H", pdo), f"RxPDO[{i}]=0x{pdo:04X}")
    _sdo_write(0x1C12, 0x00, struct.pack("<B", len(rx_pdo)),
               f"set SM2 RxPDO count={len(rx_pdo)}")

    for i, pdo in enumerate(tx_pdo, start=1):
        _sdo_write(0x1C13, i, struct.pack("<H", pdo), f"TxPDO[{i}]=0x{pdo:04X}")
    _sdo_write(0x1C13, 0x00, struct.pack("<B", len(tx_pdo)),
               f"set SM3 TxPDO count={len(tx_pdo)}")
