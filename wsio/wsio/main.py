import argparse
from enum import StrEnum
from typing import Self

from goofyproxy import GoofyServer, GoofyClient, AddressFilterType, \
    ADDRESS_FILTER_HELP, ADDRESS_FILTER_LAN
from goofyproxy.common import *

from wsio import WsIo, CallerMode, CalleeMode, delete_account


def run(args: argparse.Namespace):
    if args.command == "filter-help":
        print(ADDRESS_FILTER_HELP)
        return

    if args.command == "delete-acc":
        delete_account(
            url=args.url,
            id=args.id,
            password=args.password,
            ssl_verify=not args.no_ssl_verify,
        )
        return

    if args.command == "server":
        peers: list[str] = list(filter(
            lambda s: bool(s),
            map(
                lambda s: s.strip(),
                args.peers.split(",")
            )
        ))

        if not args.peers_block and not peers:
            print(
                "no peers to accept calls from! either define one or more user "
                "IDs in --peers or enable --peers-block."
            )
            return

        gio = WsIo(
            url=args.url,
            id=args.id,
            password=args.password,
            call_mode=CalleeMode(peers, args.peers_block),
            interval_min=args.interval_min,
            interval_max=args.interval_max,
            max_out_packet_size=parse_data_size(args.max_out_packet_size),
            warm_up=not args.no_warmup,
            ssl_verify=not args.no_ssl_verify,
        )

        GoofyServer(
            gio,
            send_interval=min(.05, gio.interval_min),
            address_filter=args.address_filter,

            address_filter_type=AddressFilterType.Allow
            if args.address_filter_allow else AddressFilterType.Block,
        )
    elif args.command == "client":
        gio = WsIo(
            url=args.url,
            id=args.id,
            password=args.password,
            call_mode=CallerMode(args.peer),
            interval_min=args.interval_min,
            interval_max=args.interval_max,
            max_out_packet_size=parse_data_size(args.max_out_packet_size),
            warm_up=not args.no_warmup,
            ssl_verify=not args.no_ssl_verify,
        )

        GoofyClient(
            gio,
            host="0.0.0.0",
            port=args.port,
            buf_size=args.bufsize,
            poll_interval=min(.05, gio.interval_min),
            send_interval=min(.05, gio.interval_min),
            address_filter=args.address_filter,

            address_filter_type=AddressFilterType.Allow
            if args.address_filter_allow else AddressFilterType.Block,

            bypass_filter=args.bypass_filter,

            bypass_filter_type=AddressFilterType.Block
            if args.bypass_filter_reverse else AddressFilterType.Allow,
        )
    else:
        print("invalid mode")
        return

    gio.stop()


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
        description="goofy proxy on WsIo: share your internet connection with "
        "a friend through WsIo, a very basic WebSocket-based binary call  "
        "service."
    )
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
        help="sub-commands"
    )

    parser_server = subparsers.add_parser(
        "server",
        help="wait for an incoming WsIo call, pick it up, and run GoofyServer "
        "on top of it."
    )
    parser_client = subparsers.add_parser(
        "client",
        help="call a WsIo user and run GoofyClient on top of it. the peer must "
        "be running GoofyServer on its side."
    )
    parser_delete_acc = subparsers.add_parser(
        "delete-acc",
        help="delete WsIo user account"
    )
    parser_filter_help = subparsers.add_parser(
        "filter-help",
        help="print a detailed help message on address filter patterns."
    )

    for p in (parser_server, parser_client, parser_delete_acc):
        p.add_argument(
            "url",
            type=str,
            help="WsIo server URL (HTTPS) ending with a slash. example: "
            "\"https://example.com/wsio/\""
        )
        p.add_argument(
            "id",
            type=str,
            help="user ID to send packets as. a new user will be created if "
            "the ID doesn't match an existing one."
        )
        p.add_argument(
            "password",
            type=str,
            help="password for the provided user ID"
        )

    default = ""
    parser_server.add_argument(
        "-P",
        "--peers",
        default=default,
        type=str,
        help=f"[{default=}] comma-separated list of user IDs to accept or "
        "block calls from, depending on --peers-block."
    )
    parser_server.add_argument(
        "-b",
        "--peers-block",
        action="store_true",
        help="if disabled (default), will only accept calls from user IDs "
        "in --peers. if enabled, will accept calls from anyone but --peers."
    )
    parser_client.add_argument(
        "-P",
        "--peer",
        required=True,
        type=str,
        help="user ID to call"
    )

    for p in (parser_server, parser_client):
        default = .1
        p.add_argument(
            "-i",
            "--interval-min",
            type=float,
            default=default,
            help=f"[{default=}] minimum delay in seconds between each "
            "iteration of the send-receive loop."
        )
        default = .5
        p.add_argument(
            "-I",
            "--interval-max",
            type=float,
            default=default,
            help=f"[{default=}] maximum delay in seconds between each "
            "iteration of the send-receive loop."
        )
        default = "512 KiB"
        p.add_argument(
            "-O",
            "--max-out-packet-size",
            type=str,
            default=default,
            help=f"[{default=}] maximum outgoing packet size  in each "
            "iteration of the send-receive loop."
        )
        p.add_argument(
            "-q",
            "--no-warmup",
            action="store_true",
            help="disable warm-up which sends a few dummy requests to the "
            "server with random delays between them before starting or waiting "
            "for a call."
        )

    for p in (parser_server, parser_client, parser_delete_acc):
        p.add_argument(
            "-k",
            "--no-ssl-verify",
            action="store_true",
            help="disable SSL certificate verification (not recommended)."
        )

    parser_client.add_argument(
        "-p",
        "--port",
        required=True,
        type=int,
        help=f"local SOCKS5 proxy server port"
    )
    default = 4096
    parser_client.add_argument(
        "-s",
        "--bufsize",
        type=int,
        default=default,
        help=f"[{default=}] relay buffer size in bytes"
    )

    default = ADDRESS_FILTER_LAN
    parser_server.add_argument(
        "-f",
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
        "-f",
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
        parser_delete_acc,
        parser_filter_help
    ):
        p.add_argument(
            "-l",
            "--log-level",
            type=LogLevel,
            default=LogLevel.from_int(LOG_CONFIG["level"]),
            help="one of: debug, info, warning, error, fatal"
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
