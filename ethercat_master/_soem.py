"""ctypes binding to SOEM 2.x via the bundled ``ecm`` shim (drop-in for the pysoem subset used here)."""

import ctypes
import os
import sys
import threading
from collections import namedtuple

NONE_STATE = 0x00
INIT_STATE = 0x01
PREOP_STATE = 0x02
BOOT_STATE = 0x03
SAFEOP_STATE = 0x04
OP_STATE = 0x08
STATE_ACK = 0x10
STATE_ERROR = 0x10

_ERR_SDO = 0
_ERR_EMERGENCY = 1
_ERR_PACKET = 3
_ERR_MBX = 9

_SDO_DEFAULT_SIZE = 256
_ADAPTER_NAME_LEN = 128
_MAX_ADAPTERS = 64


class SdoError(Exception):
    def __init__(self, slave_pos, index, subindex, abort_code, desc=""):
        self.slave_pos, self.index, self.subindex = slave_pos, index, subindex
        self.abort_code, self.desc = abort_code, desc
        super().__init__(f"SDO abort 0x{abort_code & 0xFFFFFFFF:08X} "
                         f"slave {slave_pos} 0x{index:04X}:{subindex}")


class Emergency(Exception):
    def __init__(self, slave_pos, error_code, error_reg, b1, w1, w2):
        self.slave_pos, self.error_code, self.error_reg = slave_pos, error_code, error_reg
        self.b1, self.w1, self.w2 = b1, w1, w2
        super().__init__(f"EMCY slave {slave_pos}: 0x{error_code:04X} reg 0x{error_reg:02X}")


class PacketError(Exception):
    def __init__(self, slave_pos, error_code):
        self.slave_pos, self.error_code = slave_pos, error_code
        super().__init__(f"Packet error slave {slave_pos}: {error_code}")


class MailboxError(Exception):
    def __init__(self, slave_pos, error_code):
        self.slave_pos, self.error_code = slave_pos, error_code
        super().__init__(f"Mailbox error slave {slave_pos}: {error_code}")


class WkcError(Exception):
    def __init__(self, wkc):
        self.wkc = wkc
        super().__init__(f"Working counter {wkc}")


class ConfigMapError(Exception):
    def __init__(self, error_list):
        self.error_list = error_list
        super().__init__("; ".join(str(e) for e in error_list))


class _SlaveInfo(ctypes.Structure):
    _fields_ = [
        ("name", ctypes.c_char * 41),
        ("man", ctypes.c_uint32), ("id", ctypes.c_uint32),
        ("rev", ctypes.c_uint32), ("ser", ctypes.c_uint32),
        ("state", ctypes.c_uint16), ("al_status", ctypes.c_uint16),
        ("configadr", ctypes.c_uint16), ("aliasadr", ctypes.c_uint16),
        ("obytes", ctypes.c_uint32), ("ibytes", ctypes.c_uint32),
        ("obits", ctypes.c_uint16), ("ibits", ctypes.c_uint16),
        ("hasdc", ctypes.c_uint8), ("coe_details", ctypes.c_uint8),
        ("islost", ctypes.c_uint8),
        ("mbx_proto", ctypes.c_uint16),
    ]


class _Error(ctypes.Structure):
    _fields_ = [
        ("etype", ctypes.c_int),
        ("slave", ctypes.c_uint16), ("index", ctypes.c_uint16),
        ("subidx", ctypes.c_uint8),
        ("abort_code", ctypes.c_int32),
        ("error_code", ctypes.c_uint16),
        ("error_reg", ctypes.c_uint8), ("b1", ctypes.c_uint8),
        ("w1", ctypes.c_uint16), ("w2", ctypes.c_uint16),
    ]


def _add_npcap_dll_dir():
    if sys.platform == "win32" and hasattr(os, "add_dll_directory"):
        npcap = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "Npcap")
        if os.path.isdir(npcap):
            os.add_dll_directory(npcap)


def _load_library():
    _add_npcap_dll_dir()
    names = {"win32": ["ecm.dll", "libecm.dll"], "darwin": ["libecm.dylib"]}.get(
        sys.platform, ["libecm.so"])
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [os.environ.get("ETHERCAT_MASTER_ECM_LIB")]
    candidates += [os.path.join(here, n) for n in names]
    for path in filter(None, candidates):
        if os.path.exists(path):
            return ctypes.CDLL(path)
    raise OSError(f"SOEM shim library not found (looked for {names} in {here})")


class _MissingLibrary:
    """Defers the load error until a SOEM function is actually called."""

    def __init__(self, error):
        self._error = error

    def __getattr__(self, name):
        error = self._error

        class _Raise:
            restype = argtypes = None

            def __call__(self, *args):
                raise error

        return _Raise()


try:
    _lib = _load_library()
except OSError as _exc:
    _lib = _MissingLibrary(_exc)
_P = ctypes.c_void_p
_I = ctypes.c_int
_U8P = ctypes.POINTER(ctypes.c_uint8)


def _sig(name, res, *args):
    fn = getattr(_lib, name)
    fn.restype, fn.argtypes = res, list(args)
    return fn


_ecm_find_adapters = _sig("ecm_find_adapters", _I, ctypes.c_char_p, ctypes.c_char_p, _I, _I)
_ecm_create = _sig("ecm_create", _P)
_ecm_destroy = _sig("ecm_destroy", None, _P)
_ecm_open = _sig("ecm_open", _I, _P, ctypes.c_char_p)
_ecm_close = _sig("ecm_close", None, _P)
_ecm_config_init = _sig("ecm_config_init", _I, _P)
_ecm_config_map = _sig("ecm_config_map", _I, _P, _I)
_ecm_config_dc = _sig("ecm_config_dc", _I, _P)
_ecm_slavecount = _sig("ecm_slavecount", _I, _P)
_ecm_expected_wkc = _sig("ecm_expected_wkc", _I, _P, _I)
_ecm_set_sdo_timeouts = _sig("ecm_set_sdo_timeouts", None, _P, _I, _I)
_ecm_get_info = _sig("ecm_get_info", _I, _P, _I, ctypes.POINTER(_SlaveInfo))
_ecm_outputs = _sig("ecm_outputs", _P, _P, _I)
_ecm_inputs = _sig("ecm_inputs", _P, _P, _I)
_ecm_get_state = _sig("ecm_get_state", _I, _P, _I)
_ecm_set_state = _sig("ecm_set_state", None, _P, _I, _I)
_ecm_read_state = _sig("ecm_read_state", _I, _P)
_ecm_write_state = _sig("ecm_write_state", _I, _P, _I)
_ecm_state_check = _sig("ecm_state_check", _I, _P, _I, _I, _I)
_ecm_set_islost = _sig("ecm_set_islost", None, _P, _I, _I)
_ecm_recover = _sig("ecm_recover", _I, _P, _I, _I)
_ecm_reconfig = _sig("ecm_reconfig", _I, _P, _I, _I)
_ecm_send = _sig("ecm_send", _I, _P)
_ecm_receive = _sig("ecm_receive", _I, _P, _I)
_ecm_sdo_read = _sig("ecm_sdo_read", _I, _P, _I, _I, _I, _I, _U8P, ctypes.POINTER(_I))
_ecm_sdo_write = _sig("ecm_sdo_write", _I, _P, _I, _I, _I, _I, ctypes.c_char_p, _I)
_ecm_pop_error = _sig("ecm_pop_error", _I, _P, ctypes.POINTER(_Error))
_ecm_dc_sync0 = _sig("ecm_dc_sync0", None, _P, _I, _I, ctypes.c_uint32, ctypes.c_int32)
_ecm_dc_sync01 = _sig("ecm_dc_sync01", None, _P, _I, _I, ctypes.c_uint32, ctypes.c_uint32,
                      ctypes.c_int32)
_ecm_fprd = _sig("ecm_fprd", _I, _P, _I, _I, _U8P, _I, _I)

Adapter = namedtuple("Adapter", ["name", "desc"])


def find_adapters():
    n, ln = _MAX_ADAPTERS, _ADAPTER_NAME_LEN
    names = ctypes.create_string_buffer(n * ln)
    descs = ctypes.create_string_buffer(n * ln)
    count = _ecm_find_adapters(names, descs, n, ln)

    def _s(buf, i):
        return buf.raw[i * ln:(i + 1) * ln].split(b"\0", 1)[0].decode("utf-8", "replace")

    return [Adapter(_s(names, i), _s(descs, i)) for i in range(count)]


def _raise_from(err):
    if err.etype == _ERR_SDO:
        raise SdoError(err.slave, err.index, err.subidx, err.abort_code)
    if err.etype == _ERR_EMERGENCY:
        raise Emergency(err.slave, err.error_code, err.error_reg, err.b1, err.w1, err.w2)
    if err.etype == _ERR_PACKET:
        raise PacketError(err.slave, err.error_code)
    if err.etype == _ERR_MBX:
        raise MailboxError(err.slave, err.error_code)
    raise MailboxError(err.slave, err.error_code)


class Master:
    """Subset of ``pysoem.Master`` used by ethercat_master."""

    def __init__(self):
        self._h = _ecm_create()
        if not self._h:
            raise MemoryError()
        self._lock = threading.RLock()
        self._slaves = []
        self._sdo_rd = 0
        self._sdo_wr = 0
        self.state = NONE_STATE

    def __del__(self):
        h, self._h = getattr(self, "_h", None), None
        if h:
            _ecm_destroy(h)

    def open(self, ifname):
        if not _ecm_open(self._h, ifname.encode()):
            raise ConnectionError(f"could not open interface {ifname}")

    def close(self):
        _ecm_close(self._h)

    def config_init(self, usetable=False):
        with self._lock:
            n = _ecm_config_init(self._h)
            self._slaves = [Slave(self, i + 1) for i in range(max(n, 0))]
            return n

    def config_map(self):
        with self._lock:
            size = _ecm_config_map(self._h, 0)
            errors = self._drain_errors()
            if errors:
                raise ConfigMapError(errors)
            return size

    def config_dc(self):
        return bool(_ecm_config_dc(self._h))

    @property
    def slaves(self):
        return self._slaves

    @property
    def expected_wkc(self):
        return _ecm_expected_wkc(self._h, 0)

    def _set_timeouts(self):
        _ecm_set_sdo_timeouts(self._h, self._sdo_rd, self._sdo_wr)

    @property
    def sdo_read_timeout(self):
        return self._sdo_rd

    @sdo_read_timeout.setter
    def sdo_read_timeout(self, us):
        self._sdo_rd = int(us)
        self._set_timeouts()

    @property
    def sdo_write_timeout(self):
        return self._sdo_wr

    @sdo_write_timeout.setter
    def sdo_write_timeout(self, us):
        self._sdo_wr = int(us)
        self._set_timeouts()

    def read_state(self):
        return _ecm_read_state(self._h)

    def write_state(self):
        _ecm_set_state(self._h, 0, self.state)
        return _ecm_write_state(self._h, 0)

    def state_check(self, expected_state, timeout=50000):
        return _ecm_state_check(self._h, 0, expected_state, timeout)

    # ctypes releases the GIL for every foreign call, so release_gil is a no-op.
    def send_processdata(self, release_gil=None):
        return _ecm_send(self._h)

    def receive_processdata(self, timeout=2000, release_gil=None):
        return _ecm_receive(self._h, timeout)

    def _drain_errors(self, slave_obj=None):
        errors, err = [], _Error()
        while _ecm_pop_error(self._h, ctypes.byref(err)):
            if err.etype == _ERR_EMERGENCY and slave_obj is not None and slave_obj._emcy_callbacks:
                emcy = Emergency(err.slave, err.error_code, err.error_reg, err.b1, err.w1, err.w2)
                for cb in slave_obj._emcy_callbacks:
                    cb(emcy)
                continue
            errors.append(_Error.from_buffer_copy(err))
        return errors


class Slave:
    """Subset of ``pysoem.CdefSlave``. ``pos`` is 1-based like SOEM."""

    def __init__(self, master, pos):
        self._m = master
        self._h = master._h
        self._pos = pos
        self._emcy_callbacks = []

    def _info(self):
        info = _SlaveInfo()
        _ecm_get_info(self._h, self._pos, ctypes.byref(info))
        return info

    @property
    def name(self):
        return self._info().name.decode("utf-8", "replace")

    @property
    def man(self):
        return self._info().man

    @property
    def id(self):
        return self._info().id

    @property
    def rev(self):
        return self._info().rev

    @property
    def is_lost(self):
        return bool(self._info().islost)

    @is_lost.setter
    def is_lost(self, value):
        _ecm_set_islost(self._h, self._pos, int(bool(value)))

    @property
    def al_status(self):
        return self._info().al_status

    @property
    def state(self):
        return _ecm_get_state(self._h, self._pos)

    @state.setter
    def state(self, value):
        _ecm_set_state(self._h, self._pos, value)

    @property
    def input(self):
        info = self._info()
        ptr = _ecm_inputs(self._h, self._pos)
        n = info.ibytes or (1 if info.ibits else 0)
        return ctypes.string_at(ptr, n) if ptr and n else b""

    @property
    def output(self):
        info = self._info()
        ptr = _ecm_outputs(self._h, self._pos)
        n = info.obytes or (1 if info.obits else 0)
        return ctypes.string_at(ptr, n) if ptr and n else b""

    @output.setter
    def output(self, data):
        info = self._info()
        ptr = _ecm_outputs(self._h, self._pos)
        n = info.obytes or (1 if info.obits else 0)
        if len(data) != n:
            raise AttributeError(f"output length {len(data)} != {n}")
        if ptr and n:
            ctypes.memmove(ptr, bytes(data), n)

    def add_emergency_callback(self, callback):
        self._emcy_callbacks.append(callback)

    def _check(self, wkc):
        with self._m._lock:
            errors = self._m._drain_errors(self)
        if errors:
            _raise_from(errors[0])
        if wkc <= 0:
            raise WkcError(wkc)

    def sdo_read(self, index, subindex, size=0, ca=False, release_gil=None):
        n = size or _SDO_DEFAULT_SIZE
        buf = (ctypes.c_uint8 * n)()
        sz = _I(n)
        wkc = _ecm_sdo_read(self._h, self._pos, index, subindex, int(bool(ca)), buf,
                            ctypes.byref(sz))
        self._check(wkc)
        return bytes(buf[:sz.value])

    def sdo_write(self, index, subindex, data, ca=False, release_gil=None):
        data = bytes(data)
        wkc = _ecm_sdo_write(self._h, self._pos, index, subindex, int(bool(ca)), data, len(data))
        self._check(wkc)

    def write_state(self):
        return _ecm_write_state(self._h, self._pos)

    def state_check(self, expected_state, timeout=2000):
        return _ecm_state_check(self._h, self._pos, expected_state, timeout)

    def reconfig(self, timeout=500):
        return _ecm_reconfig(self._h, self._pos, timeout)

    def recover(self, timeout=500):
        return _ecm_recover(self._h, self._pos, timeout)

    def dc_sync(self, act, sync0_cycle_time, sync0_shift_time=0, sync1_cycle_time=None):
        if sync1_cycle_time is None:
            _ecm_dc_sync0(self._h, self._pos, int(bool(act)), sync0_cycle_time, sync0_shift_time)
        else:
            _ecm_dc_sync01(self._h, self._pos, int(bool(act)), sync0_cycle_time,
                           sync1_cycle_time, sync0_shift_time)

    def read_reg(self, address, size, timeout=2000):
        buf = (ctypes.c_uint8 * size)()
        wkc = _ecm_fprd(self._h, self._pos, address, buf, size, timeout)
        if wkc <= 0:
            raise WkcError(wkc)
        return bytes(buf)
