"""Unit tests for ethercat_master.pdo helpers."""

import json

from ethercat_master.pdo import (
    _parse_hex_bytes,
    _parse_index,
    _parse_startup_list,
    load_pdo_config,
)


def test_parse_hex_bytes_space_separated():
    assert _parse_hex_bytes("04 00 0E 0A") == bytes([0x04, 0x00, 0x0E, 0x0A])


def test_parse_hex_bytes_contiguous():
    assert _parse_hex_bytes("04000E0A") == bytes([0x04, 0x00, 0x0E, 0x0A])


def test_parse_hex_bytes_with_0x():
    assert _parse_hex_bytes("0x04000E0A") == bytes([0x04, 0x00, 0x0E, 0x0A])


def test_parse_index_string():
    assert _parse_index("0x1C12") == 0x1C12


def test_parse_index_int():
    assert _parse_index(0x1C12) == 0x1C12


def test_parse_startup_list():
    entries = _parse_startup_list([
        {"transition": "PS", "index": "0xF030", "subindex": 0,
         "data": "04 00 0E 0A", "comment": "slot cfg"},
    ])
    assert len(entries) == 1
    assert entries[0]["index"] == 0xF030
    assert entries[0]["data"] == bytes([0x04, 0x00, 0x0E, 0x0A])
    assert entries[0]["transition"] == "PS"


def test_load_pdo_config(tmp_path):
    p = tmp_path / "test_config.json"
    p.write_text(json.dumps({
        "default": {"rx_pdo": ["0x1600"], "tx_pdo": ["0x1A00"]},
        "slaves": {
            "0": {
                "rx_pdo": ["0x1600", "0x1605"],
                "tx_pdo": ["0x1A00", "0x1A05"],
            }
        }
    }), encoding="utf-8")
    cfg = load_pdo_config(str(p))
    assert cfg["default"]["rx_pdo"] == [0x1600]
    assert cfg[0]["rx_pdo"] == [0x1600, 0x1605]
    assert cfg[0]["tx_pdo"] == [0x1A00, 0x1A05]
