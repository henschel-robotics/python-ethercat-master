"""Unit tests for ethercat_master.slave."""

import pytest

from ethercat_master.slave import GenericSlave


def test_generic_slave_slots():
    s = GenericSlave(0)
    assert s.slave_index == 0
    assert s.use_default_pdo is True
    assert s.on_cycle is None
    with pytest.raises(AttributeError):
        s.not_a_slot = 1


def test_generic_slave_input_output():
    s = GenericSlave(1)
    assert s.input == b""
    assert s.output == b""
    s.output = b"\xff"
    assert s.output == b"\xff"
