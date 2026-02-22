"""
EtherCAT Master — Generic Slave Handle
========================================

Ready-to-use slave handle for any EtherCAT device.  Exposes raw PDO
bytes via :attr:`input` / :attr:`output` and an optional callback for
custom per-cycle logic.

Usage::

    from ethercat_master import EtherCATBus, GenericSlave

    bus = EtherCATBus(adapter=r"\\Device\\NPF_{...}", cycle_time_ms=1)
    slave = GenericSlave(0)
    bus.register_slave(slave)
    bus.open()

    # read inputs
    print(slave.input.hex())

    # write outputs
    slave.output = bytes([0xFF])

    bus.close()
"""

from .pdo import configure_pdo_mapping


class GenericSlave:
    """Generic slave handle that works with any EtherCAT device.

    Implements the full slave-handle interface expected by
    :class:`~ethercat_master.EtherCATBus`.

    For devices with standard SII PDO mappings (e.g. Beckhoff terminals),
    set ``use_default_pdo=True`` (the default) to skip custom PDO
    configuration.  For devices that need explicit PDO assignments from
    ``ethercat_config.json``, set ``use_default_pdo=False``.

    Args:
        slave_index: EtherCAT slave position on the bus.
        use_default_pdo: If ``True``, keep the factory PDO mapping from
            the slave's SII EEPROM.  If ``False``, apply PDO indices
            from the bus config file.
        on_cycle: Optional callback ``fn(slave)`` invoked every PDO
            cycle after inputs are read.
    """

    def __init__(self, slave_index, use_default_pdo=True, on_cycle=None):
        self.slave_index = slave_index
        self.use_default_pdo = use_default_pdo
        self.on_cycle = on_cycle
        self._pysoem_slave = None
        self._input = b""
        self._output = b""

    @property
    def input(self) -> bytes:
        """Latest PDO input data (slave -> master)."""
        return self._input

    @property
    def output(self) -> bytes:
        """Current PDO output data (master -> slave)."""
        return self._output

    @output.setter
    def output(self, data: bytes):
        self._output = data

    def configure(self, pysoem_slave, rx_pdo=None, tx_pdo=None):
        """Called by EtherCATBus during config_init.  Stores the pysoem
        slave reference and optionally writes custom PDO mapping via SDO."""
        self._pysoem_slave = pysoem_slave
        if not self.use_default_pdo and (rx_pdo or tx_pdo):
            configure_pdo_mapping(pysoem_slave, rx_pdo=rx_pdo, tx_pdo=tx_pdo)

    def seed_tx(self, pysoem_slave):
        """Called once after config_map to initialise the output buffer
        with zeros so the slave receives valid data on the first cycle."""
        self._output = bytes(len(pysoem_slave.output))
        pysoem_slave.output = self._output

    def pdo_update(self, master, reconnecting):
        """Internal callback — called automatically by EtherCATBus every
        cycle.  Reads and writes the _input and _output buffers."""
        if reconnecting.is_set() or self._pysoem_slave is None:
            return
        self._input = self._pysoem_slave.input
        if self._output and len(self._output) == len(self._pysoem_slave.output):
            self._pysoem_slave.output = self._output
        if self.on_cycle:
            self.on_cycle(self)

    def safe_stop(self):
        """Called during bus shutdown.  Zeroes all outputs so the slave
        does not hold its last commanded state."""
        if self._pysoem_slave and len(self._pysoem_slave.output) > 0:
            self._pysoem_slave.output = bytes(len(self._pysoem_slave.output))

    def on_reconnect(self, master):
        """Called after the bus recovers from a connection loss.
        Re-acquires the pysoem slave reference which may have changed."""
        self._pysoem_slave = master.slaves[self.slave_index]
