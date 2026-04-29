from .goofyio import GoofyIo
from .goofy_server import GoofyServer
from .goofy_client import GoofyClient
from .address_filter import ADDRESS_FILTER_HELP, ADDRESS_FILTER_LAN, \
    AddressFilterType, match_address, is_address_allowed

__all__ = [
    "GoofyIo", "GoofyServer", "GoofyClient", "ADDRESS_FILTER_HELP",
    "ADDRESS_FILTER_LAN", "AddressFilterType", "match_address",
    "is_address_allowed"
]

import importlib.metadata
__version__ = importlib.metadata.version("goofyproxy")
