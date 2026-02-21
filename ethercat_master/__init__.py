"""
EtherCAT Master
~~~~~~~~~~~~~~~

Generic EtherCAT master library built on PySOEM.  Provides bus management,
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

from .bus import EtherCATBus
from .slave import GenericSlave
from .pdo import load_pdo_config, get_slave_pdo, configure_pdo_mapping
from .exceptions import EtherCATError, ConnectionError, CommunicationError, ConfigurationError
from .network_test import NetworkLatencyTest

__version__ = "0.1.0"
__all__ = [
    "EtherCATBus",
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
