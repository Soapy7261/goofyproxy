import argparse
from enum import StrEnum
from typing import Self

from goofy_client import GoofyClient
from goofy_server import GoofyServer
from videoio import VideoIo
from common import *


DEFAULT_FORMAT = "640x480-16-2@2"


def chat_send(gio: VideoIo):
    try:
        print("type something and hit [Enter] to send.")
        while gio.running():
            msg = input().encode()
            gio.send(len(msg).to_bytes(2) + msg)
    except BaseException as e:
        logger.error(format_exception(e))


def run(args: argparse.Namespace):
    if args.mode == "list_monitors":
        monitors = VideoIo.get_monitors()
        for i in range(len(monitors)):
            print(f"monitor {i}: {monitors[i]}")
        if not monitors:
            print("(no monitors found)")
        return

    gio = VideoIo(
        args.format,
        args.monitor,
        args.sender_id,
        args.peer_id,
        args.screenshot_speed,
        args.corrupt_packet_threshold
    )

    if not args.start_immediately:
        print("hit [Enter] to start the VideoIo handshake process...")
        input()
    gio.start()

    if args.mode == "chat":
        # sending
        threading.Thread(target=chat_send, args=(gio,), daemon=True).start()

        # receiving
        while gio.running():
            msg_len = int.from_bytes(gio.receive(2))
            msg = gio.receive(msg_len).decode()
            print(f"<<< {msg}")
    elif args.mode == "server":
        GoofyServer(gio)
        gio.stop()
    elif args.mode == "client":
        if not args.port:
            print("port is required in client mode")
            return

        GoofyClient(
            gio,
            host="0.0.0.0",
            port=args.port,
            buf_size=args.bufsize
        )
        gio.stop()
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
        choices=["server", "client", "chat", "list_monitors"],
        help="which mode to run in"
    )
    parser.add_argument(
        "-f",
        "--format",
        type=str,
        default=DEFAULT_FORMAT,
        help="output grid format represented as "
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
        "-S",
        "--sender-id",
        type=str,
        help="VideoIo sender ID. if not provided, one will be generated."
    )
    parser.add_argument(
        "-P",
        "--peer-id",
        type=str,
        help="sender ID of the peer. if not provided, the first detected peer "
        "will be chosen."
    )
    parser.add_argument(
        "--start-immediately",
        action="store_true",
        help="start the VideoIo handshake process as soon as the window opens. "
        "if not enabled, the program will wait for the user to hit [Enter] "
        "before it starts updating the window."
    )
    default = 2.
    parser.add_argument(
        "-s",
        "--screenshot-speed",
        type=float,
        default=default,
        help=f"[{default=}] the VideoIo receive thread will take a screenshot "
        "and read the peer's video feed this many times for every \"frame\" "
        "(1 / peer_format.rate). it may be helpful to use a higher value for "
        "this in certain cases where the frame rate of the peer's format is "
        "low (e.g. rate <= 2) while the cells are small and detailed, because "
        "video compression usually improves the image quality if the image "
        "stays still for some time (so by taking more screenshots we "
        "effectively wait for the image quality to improve so we can read the "
        "data without corruption)."
    )
    default = 4
    parser.add_argument(
        "-c",
        "--corrupt-packet-threshold",
        type=int,
        default=default,
        help=f"[{default=}] if we get more than this many corrupt packets "
        "(e.g. index too far ahead or checksum unverified), we'll ask the "
        "other side to start retransmitting from the last packet index we "
        "properly received."
    )
    parser.add_argument(
        "-p",
        "--port",
        type=int,
        help="local SOCKS5 proxy server port in client mode"
    )
    default = 256
    parser.add_argument(
        "-b",
        "--bufsize",
        type=int,
        default=default,
        help=f"[{default=}] relay buffer size in client mode"
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
