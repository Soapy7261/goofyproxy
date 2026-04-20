import argparse
from enum import StrEnum
from typing import Self

from goofy_client import GoofyClient
from goofy_server import GoofyServer
from videoio import VideoIo
from common import *


DEFAULT_FORMAT = "720x540-16-2@5"


def sendcin(gio: VideoIo):
    while True:
        msg = input()
        gio.send(msg.encode())


def run(args: argparse.Namespace):
    if args.mode == "list_monitors":
        monitors = VideoIo.get_monitors()
        for i in range(len(monitors)):
            print(f"monitor {i}: {monitors[i]}")
        if not monitors:
            print("(no monitors found)")
        return

    gio = VideoIo(args.format, args.monitor)

    threading.Thread(target=sendcin, args=(gio,), daemon=True).start()
    while True:
        try:
            s = gio.receive(1).decode()
            print(s, end="")
        except Exception as e:
            print(format_exception(e))
    return

    if args.mode == "server":
        GoofyServer(gio)
    elif args.mode == "client":
        if not args.port:
            print("port is required in client mode")
            return

        GoofyClient(
            gio,
            host="0.0.0.0",
            port=args.port
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
        description="goofy proxy using VideoIo: share your internet connection "
        "with a friend through a video call"
    )
    parser.add_argument(
        "mode",
        choices=["server", "client", "list_monitors"],
        help="which mode to run in"
    )
    parser.add_argument(
        "-f",
        "--format",
        type=str,
        default=DEFAULT_FORMAT,
        help="output format represented as "
        "\"{width}x{height}-{cell_size}-{bits_per_cell}@{rate}\" (default: "
        f"{DEFAULT_FORMAT})"
    )
    parser.add_argument(
        "-m",
        "--monitor",
        type=int,
        default=0,
        help="index of the monitor that's displaying the other side's video "
        "feed (see list_monitors), starting from 0."
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
        "-L",
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
