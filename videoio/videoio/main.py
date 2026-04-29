import time
import threading
from enum import StrEnum
from typing import Self
import logging
import argparse

from goofyproxy import GoofyServer, GoofyClient, AddressFilterType, \
    ADDRESS_FILTER_HELP, ADDRESS_FILTER_LAN
import goofyproxy.common as goofycommon

from videoio import VideoIo


DEFAULT_FORMAT = "640x480-16-2@2"


def chat_send(gio: VideoIo):
    try:
        print("type something and hit [Enter] to send.")
        while gio.running():
            msg = input().encode()
            gio.send(len(msg).to_bytes(2) + msg)
    except BaseException as e:
        goofycommon.root_log.error(goofycommon.format_exception(e))


def run(args: argparse.Namespace):
    if args.command == "filter-help":
        print(ADDRESS_FILTER_HELP)
        return

    if args.command == "list-monitors":
        monitors = VideoIo.get_monitors()
        for i in range(len(monitors)):
            print(f"monitor {i}: {monitors[i]}")
        if not monitors:
            print("(no monitors found)")
        return

    window_position = None
    if args.position:
        try:
            x, y = args.position.split(",")
            window_position = (int(x), int(y))
        except Exception:
            print(f"invalid position \"{args.position}\"")
            return

    gio = VideoIo(
        out_format=args.format,
        in_monitor_idx=args.monitor,
        sender_id=args.sender_id,
        peer_id=args.peer_id,
        screenshot_speed=args.screenshot_speed,
        corrupt_packet_threshold=args.corrupt_packet_threshold,
        handshake_interval=args.handshake_interval,
        window_position=window_position
    )

    if args.start_immediately:
        gio.start()
    else:
        print(
            "double click on the window to start the VideoIo handshake "
            "process..."
        )
    while not gio.started():
        time.sleep(.05)
    if not gio.running():
        return

    if args.command == "chat":
        # sending
        threading.Thread(target=chat_send, args=(gio,), daemon=True).start()

        # receiving
        while gio.running():
            msg_len = int.from_bytes(gio.receive(2))
            msg = gio.receive(msg_len).decode()
            print(f"<<< {msg}")
    elif args.command == "server":
        GoofyServer(
            gio,
            send_interval=.05,
            address_filter=args.address_filter,

            address_filter_type=AddressFilterType.Allow
            if args.address_filter_allow else AddressFilterType.Block,
        )
        gio.stop()
    elif args.command == "client":
        GoofyClient(
            gio,
            host="0.0.0.0",
            port=args.port,
            buf_size=args.bufsize,
            poll_interval=.05,
            send_interval=.05,
            address_filter=args.address_filter,

            address_filter_type=AddressFilterType.Allow
            if args.address_filter_allow else AddressFilterType.Block,

            bypass_filter=args.bypass_filter,

            bypass_filter_type=AddressFilterType.Block
            if args.bypass_filter_reverse else AddressFilterType.Allow,
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
    # command line parser

    parser = argparse.ArgumentParser(
        description="goofy proxy using VideoIo: share your internet connection "
        "with a friend through a video call"
    )
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
        help="sub-commands"
    )

    parser_server = subparsers.add_parser(
        "server",
        help="run GoofyServer on top of VideoIo."
    )
    parser_client = subparsers.add_parser(
        "client",
        help="run GoofyClient on top of VideoIo."
    )
    parser_chat = subparsers.add_parser(
        "chat",
        help="run a basic chat session over VideoIo."
    )
    parser_list_monitors = subparsers.add_parser(
        "list-monitors",
        help="print a list of available monitors for reading the peer's video "
        "feed."
    )
    parser_filter_help = subparsers.add_parser(
        "filter-help",
        help="print a detailed help message on address filter patterns."
    )

    for p in (parser_server, parser_client, parser_chat):
        p.add_argument(
            "-f",
            "--format",
            type=str,
            default=DEFAULT_FORMAT,
            help="output grid format represented as "
            "\"{width}x{height}-{cell_size}-{bits_per_cell}@{rate}\" (default: "
            f"{DEFAULT_FORMAT})"
        )
        p.add_argument(
            "-m",
            "--monitor",
            type=int,
            default=0,
            help="index of the monitor that's displaying the other side's video "
            "feed (see list_monitors), starting from 0."
        )
        p.add_argument(
            "-S",
            "--sender-id",
            type=str,
            help="VideoIo sender ID. if not provided, one will be generated."
        )
        p.add_argument(
            "-P",
            "--peer-id",
            type=str,
            help="sender ID of the peer. if not provided, the first detected peer "
            "will be chosen."
        )
        p.add_argument(
            "--start-immediately",
            action="store_true",
            help="start the VideoIo handshake process as soon as the window opens. "
            "if not enabled, will wait for a double click."
        )
        default = 2.
        p.add_argument(
            "-g",
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
        default = 2
        p.add_argument(
            "-c",
            "--corrupt-packet-threshold",
            type=int,
            default=default,
            help=f"[{default=}] if we get this many (or more) corrupt packets "
            "(e.g. index too far ahead or checksum unverified), we'll ask the "
            "other side to start retransmitting from the last packet index we "
            "properly received."
        )
        default = 2.
        p.add_argument(
            "-w",
            "--handshake-interval",
            type=float,
            default=default,
            help=f"[{default=}] how much to wait (in seconds) after each handshake "
            "stage so the other side has time to see our responses."
        )
        p.add_argument(
            "-W",
            "--position",
            type=str,
            help="optional initial window position represented as \"x,y\" "
            "(example: 25,100)"
        )

    parser_client.add_argument(
        "-p",
        "--port",
        required=True,
        type=int,
        help="local SOCKS5 proxy server port in client mode"
    )
    default = 256
    parser_client.add_argument(
        "-s",
        "--bufsize",
        type=int,
        default=default,
        help=f"[{default=}] relay buffer size in client mode"
    )

    default = ADDRESS_FILTER_LAN
    parser_server.add_argument(
        "-F",
        "--address-filter",
        type=str,
        default=default,
        help=f"[{default=}] address filter for remote connections. defaults to "
        "LAN addresses. use command filter-help to learn about the format."
    )
    parser_server.add_argument(
        "-a",
        "--address-filter-allow",
        action="store_true",
        help="if disabled (default), all remote connections matching "
        "--address-filter will be blocked. if enabled, all connections will be "
        "blocked except ones matching --address-filter."
    )

    default = ""
    parser_client.add_argument(
        "-F",
        "--address-filter",
        type=str,
        default=default,
        help=f"[{default=}] address filter for both direct (bypassed) and "
        "proxied connections. use command filter-help to learn about the "
        "format."
    )
    parser_client.add_argument(
        "-a",
        "--address-filter-allow",
        action="store_true",
        help="if disabled (default), all remote connections (direct or "
        "proxied) matching --address-filter filter will be blocked. if "
        "enabled, all connections will be blocked except ones matching "
        "--address-filter."
    )
    default = ADDRESS_FILTER_LAN
    parser_client.add_argument(
        "-b",
        "--bypass-filter",
        type=str,
        default=default,
        help=f"[{default=}] address filter for direct connections. defaults to "
        "LAN addresses. use command filter-help to learn about the format."
    )
    parser_client.add_argument(
        "-B",
        "--bypass-filter-reverse",
        action="store_true",
        help="if disabled (default), addresses matching --bypass-filter will "
        "use direct connections and other addresses will be proxied. if "
        "enabled, only addresses matching --bypass-filter will be proxied and "
        "the rest will use direct connections."
    )

    for p in (
        parser_server,
        parser_client,
        parser_chat,
        parser_list_monitors,
        parser_filter_help
    ):
        default = LogLevel.from_int(goofycommon.log_level)
        p.add_argument(
            "-l",
            "--log-level",
            type=LogLevel,
            default=default,
            help=f"[{default=}] one of: debug, info, warning, error, fatal"
        )
        p.add_argument(
            "-L",
            "--log-file",
            type=str,
            help="optional path to a log file, e.g. 'log.txt'"
        )
        p.add_argument(
            "--no-color",
            action="store_true",
            help="disable terminal colors"
        )

    # parse
    args = parser.parse_args()

    # logging settings
    goofycommon.log_level = args.log_level.to_int()
    goofycommon.log_colorize = not args.no_color
    if args.log_file:
        try:
            f = open(args.log_file, "a")
            goofycommon.log_file = f
        except Exception as e:
            goofycommon.root_log.fatal(
                f"failed to open log file: {goofycommon.format_exception(e)}"
            )
            return

    # run
    try:
        run(args)
    except BaseException as e:
        goofycommon.root_log.fatal(goofycommon.format_exception(e))
    finally:
        if goofycommon.log_file is not None:
            goofycommon.log_file.flush()
            goofycommon.log_file.close()


if __name__ == "__main__":
    main()
