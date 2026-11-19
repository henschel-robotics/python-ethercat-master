"""Select the EtherCAT backend: bundled SOEM 2.x shim (default) or pysoem.

Set ``ETHERCAT_MASTER_BACKEND=pysoem`` to use the legacy pysoem package.
"""

import os

if os.environ.get("ETHERCAT_MASTER_BACKEND", "soem").lower() == "pysoem":
    import pysoem
else:
    from . import _soem as pysoem  # noqa: F401
