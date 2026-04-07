from pathlib import Path
import argparse
from enum import StrEnum
from typing import Self

from goofy_client import GoofyClient
from goofy_server import GoofyServer
from goofyio import TxtFileIo
from common import *


class LogLevel(StrEnum):
    Debug = "debug"
    Info = "info"
    Warning = "warning"
    Error = "error"
    Fatal = "fatal"

    def to_int(self) -> int:
        if self == self.Debug:
            return logging.DEBUG
        elif self == self.Info:
            return logging.INFO
        elif self == self.Warning:
            return logging.WARNING
        elif self == self.Error:
            return logging.ERROR
        elif self == self.Fatal:
            return logging.FATAL
        raise ValueError("invalid enum value")

    @classmethod
    def from_int(cls, i: int) -> Self:
        if i == logging.DEBUG:
            return cls.Debug
        elif i == logging.INFO:
            return cls.Info
        elif i == logging.WARNING:
            return cls.Warning
        elif i == logging.ERROR:
            return cls.Error
        elif i == logging.FATAL:
            return cls.Fatal
        raise ValueError("invalid log level number")


def main():
    global LOG_CONFIG

    # command line parser
    parser = argparse.ArgumentParser(
        description="goofy proxy using TxtFileIo"
    )
    parser.add_argument(
        "mode",
        choices=["s", "c"],
        help="s: run as server, c: run as client"
    )
    parser.add_argument(
        "-p",
        "--port",
        type=int,
        help="local SOCKS5 proxy server port in client mode"
    )
    parser.add_argument(
        "-l",
        "--log-level",
        type=LogLevel,
        default=LogLevel.from_int(LOG_CONFIG["level"]),
        help="one of: debug, info, warning, error, fatal"
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="disable terminal colors"
    )

    # parse
    args = parser.parse_args()

    # process

    LOG_CONFIG["level"] = args.log_level.to_int()
    LOG_CONFIG["colorize"] = not args.no_color

    channel_dir = Path(__file__).parent / "stuff" / "TxtFileIo"
    if args.mode == "s":
        io = TxtFileIo("flamingo", "s", "c", channel_dir)
        GoofyServer(io, log_level=LOG_CONFIG["level"])
    elif args.mode == "c":
        if not args.port:
            print("port is required in client mode")
            return

        io = TxtFileIo("flamingo", "c", "s", channel_dir)
        GoofyClient(
            io,
            host="0.0.0.0",
            port=args.port,
            log_level=LOG_CONFIG["level"]
        )
    else:
        print("invalid mode")


if __name__ == "__main__":
    main()
