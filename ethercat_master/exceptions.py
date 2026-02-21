"""
EtherCAT Master — Custom exceptions
"""


class EtherCATError(Exception):
    """Base exception for all EtherCAT master errors."""
    pass


class ConnectionError(EtherCATError):
    """Raised when the EtherCAT connection cannot be established."""
    pass


class CommunicationError(EtherCATError):
    """Raised when EtherCAT communication fails or times out."""
    pass


class ConfigurationError(EtherCATError):
    """Raised when PDO mapping or slave configuration fails."""
    pass
