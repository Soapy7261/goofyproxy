from pathlib import Path
import argparse
from enum import StrEnum
from typing import Self
import socket

from goofy_client import GoofyClient
from goofy_server import GoofyServer
from goofyio import SocketIo
from common import *


def run(args: argparse.Namespace):
    if args.mode == "s":
        server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_sock.bind(("127.0.0.1", 50505))
        server_sock.listen(1)
        client_sock, client_addr = server_sock.accept()

        GoofyServer(
            SocketIo(client_sock),
            log_level=LOG_CONFIG["level"]
        )
    elif args.mode == "c":
        if not args.port:
            print("port is required in client mode")
            return

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect(("127.0.0.1", 50505))

        GoofyClient(
            SocketIo(sock),
            host="0.0.0.0",
            port=args.port,
            log_level=LOG_CONFIG["level"]
        )
    else:
        print("invalid mode")


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
        "-f",
        "--log-file",
        type=str,
        help="optional path to a log file, e.g. 'log.txt'"
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="disable terminal colors"
    )

    # parse
    args = parser.parse_args()

    # logging settings
    LOG_CONFIG["level"] = args.log_level.to_int()
    LOG_CONFIG["colorize"] = not args.no_color
    if args.log_file:
        try:
            f = open(args.log_file, "a")
            LOG_CONFIG["file"] = f
        except Exception as e:
            logger.fatal(f"failed to open log file: {format_exception(e)}")
            return

    # run
    try:
        run(args)
    except BaseException as e:
        logger.fatal(format_exception(e))
    finally:
        if isinstance(LOG_CONFIG["file"], io.TextIOWrapper):
            LOG_CONFIG["file"].flush()
            LOG_CONFIG["file"].close()


if __name__ == "__main__":
    main()
