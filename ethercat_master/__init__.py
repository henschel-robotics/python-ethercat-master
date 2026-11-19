"""
EtherCAT Master
~~~~~~~~~~~~~~~

Generic EtherCAT master library built on SOEM 2.x.  Provides bus management,
slave discovery, PDO mapping configuration, and automatic reconnection.

Basic usage::

    from ethercat_master import EtherCATBus, GenericSlave

    bus = EtherCATBus(adapter=r"\\Device\\NPF_{...}", cycle_time_ms=1)
    slave = GenericSlave(0)
    bus.register_slave(slave)
    bus.open()
    print(slave.input.hex())
    bus.close()

Bus discovery::

    from ethercat_master import EtherCATBus
    slaves = EtherCATBus.discover(adapter=r"\\Device\\NPF_{...}")

:copyright: (c) Henschel Robotics GmbH
:license: MIT
"""

from .bus import EtherCATBus, register_emergency_callbacks
from .slave import GenericSlave
from .pdo import load_pdo_config, get_slave_pdo, configure_pdo_mapping
from .exceptions import EtherCATError, ConnectionError, CommunicationError, ConfigurationError
from .network_test import NetworkLatencyTest

try:
    from importlib.metadata import PackageNotFoundError, version as _pkg_version
    try:
        __version__ = _pkg_version("ethercat-master")
    except PackageNotFoundError:
        __version__ = "0.0.0.dev0"
except ImportError:
    __version__ = "0.0.0.dev0"
__all__ = [
    "EtherCATBus",
    "register_emergency_callbacks",
    "GenericSlave",
    "NetworkLatencyTest",
    "load_pdo_config",
    "get_slave_pdo",
    "configure_pdo_mapping",
    "EtherCATError",
    "ConnectionError",
    "CommunicationError",
    "ConfigurationError",
    "__version__",
]
