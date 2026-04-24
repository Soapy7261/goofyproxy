from .goofyio import GoofyIo
from .goofy_server import GoofyServer
from .goofy_client import GoofyClient

__all__ = ["GoofyIo", "GoofyServer", "GoofyClient"]

import importlib.metadata
__version__ = importlib.metadata.version("goofyproxy")
