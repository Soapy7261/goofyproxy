import time
from enum import StrEnum
from typing import Self
import logging
import argparse
import json

from goofyproxy import GoofyServer, GoofyClient, AddressFilterType, \
    ADDRESS_FILTER_HELP, ADDRESS_FILTER_LAN
import goofyproxy.common as goofycommon

from bincallio import BincallIo, CallerMode, CalleeMode, \
    ConnectionModePreference, delete_account


def run(args: argparse.Namespace):
    if args.command == "filter-help":
        print(ADDRESS_FILTER_HELP)
        return

    max_out_packet_size = goofycommon.parse_data_size(
        args.max_out_packet_size
    )

    headers: list | dict | None = None
    if args.headers:
        try:
            headers = json.loads(args.headers)
        except Exception as e:
            raise ValueError(
                f"failed to parse headers from \"{args.headers}\": "
                f"{goofycommon.format_exception(e)}"
            )

    http_proxy: tuple[str, int] | None = None
    if args.proxy:
        try:
            host, port = str(args.proxy).rsplit(":", 1)
            http_proxy = (host, int(port))
        except Exception as e:
            raise ValueError(
                f"failed to parse HTTP proxy hostname and port from "
                f"\"{args.proxy}\": {goofycommon.format_exception(e)}"
            )

    http_proxy_auth: tuple[str, str] | None = None
    if args.proxy_auth:
        try:
            username, password = str(args.proxy_auth).split(":", 1)
            http_proxy_auth = (username, password)
        except Exception as e:
            raise ValueError(
                f"failed to parse HTTP proxy username and password: "
                f"{goofycommon.format_exception(e)}"
            )

    connection_mode_preference = ConnectionModePreference(
        args.prefer_conn_mode
    )

    if args.command == "delete-acc":
        delete_account(
            url=args.url,
            id=args.id,
            password=args.password,
            ssl_verify=not args.no_ssl_verify,
            headers=headers,
            http_proxy=http_proxy,
            http_proxy_auth=http_proxy_auth
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

        while True:
            gio = BincallIo(
                url=args.url,
                id=args.id,
                password=args.password,
                call_mode=CalleeMode(peers, args.peers_block),
                interval_min=args.interval_min,
                interval_max=args.interval_max,
                max_out_packet_size=max_out_packet_size,
                warm_up=not args.no_warmup,
                ssl_verify=not args.no_ssl_verify,
                n_retries=args.retries,
                retry_interval=args.retry_interval,
                headers=headers,
                http_proxy=http_proxy,
                http_proxy_auth=http_proxy_auth,
                connection_mode_preference=connection_mode_preference,
                compress=True
            )

            GoofyServer(
                gio,
                address_filter=args.address_filter,

                address_filter_type=AddressFilterType.Allow
                if args.address_filter_allow else AddressFilterType.Block,

                fake_bind_address=not args.send_bind_address,
                enable_bind=args.enable_bind,
                enable_udp_relay=not args.no_udp_relay
            )

            gio.stop()

            if goofycommon.keyboard_interrupt is not None:
                raise goofycommon.keyboard_interrupt
            if not args.endless:
                break
            goofycommon.root_log.info(
                f"sleeping for {args.endless_wait} s before starting again"
            )
            time.sleep(args.endless_wait)
    elif args.command == "client":
        while True:
            gio = BincallIo(
                url=args.url,
                id=args.id,
                password=args.password,
                call_mode=CallerMode(args.peer),
                interval_min=args.interval_min,
                interval_max=args.interval_max,
                max_out_packet_size=max_out_packet_size,
                warm_up=not args.no_warmup,
                ssl_verify=not args.no_ssl_verify,
                n_retries=args.retries,
                retry_interval=args.retry_interval,
                headers=headers,
                http_proxy=http_proxy,
                http_proxy_auth=http_proxy_auth,
                connection_mode_preference=connection_mode_preference,
                compress=True
            )

            GoofyClient(
                gio,
                host="0.0.0.0",
                port=args.port,
                max_relay_size=goofycommon.parse_data_size(
                    args.max_relay_size
                ),
                address_filter=args.address_filter,

                address_filter_type=AddressFilterType.Allow
                if args.address_filter_allow else AddressFilterType.Block,

                bypass_filter=args.bypass_filter,

                bypass_filter_type=AddressFilterType.Block
                if args.bypass_filter_reverse else AddressFilterType.Allow,

                early_success=args.early_success,
                enable_bind=args.enable_bind,
                enable_udp_relay=not args.no_udp_relay
            )

            gio.stop()

            if goofycommon.keyboard_interrupt is not None:
                raise goofycommon.keyboard_interrupt
            if not args.endless:
                break
            goofycommon.root_log.info(
                f"sleeping for {args.endless_wait} s before starting again"
            )
            time.sleep(args.endless_wait)
    else:
        print("invalid command")
        return


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
        description="goofy proxy on bincall: share your internet connection "
        "with a friend through bincall, a basic binary call service."
    )
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
        help="sub-commands"
    )

    parser_server = subparsers.add_parser(
        "server",
        help="wait for an incoming call, pick it up, and run GoofyServer "
        "on top of it."
    )
    parser_client = subparsers.add_parser(
        "client",
        help="call a user and run GoofyClient on top of it. the peer must be "
        "running GoofyServer on its side."
    )
    parser_delete_acc = subparsers.add_parser(
        "delete-acc",
        help="delete user account."
    )
    parser_filter_help = subparsers.add_parser(
        "filter-help",
        help="print a detailed help message on address filter patterns."
    )

    for p in (parser_server, parser_client, parser_delete_acc):
        p.add_argument(
            "url",
            type=str,
            help="bincall API URL (HTTPS or HTTP). "
            "example: \"https://example.com/bincall\""
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

    parser_server.add_argument(
        "-e",
        "--endless",
        action="store_true",
        help="wait for another call every time one ends"
    )
    default = 3.
    parser_server.add_argument(
        "-w",
        "--endless-wait",
        type=float,
        default=default,
        help=f"[{default=}] how long to sleep in seconds before checking for a "
        "new incoming call"
    )
    parser_client.add_argument(
        "-e",
        "--endless",
        action="store_true",
        help="call the peer again every time the call ends"
    )
    default = 2.
    parser_client.add_argument(
        "-w",
        "--endless-wait",
        type=float,
        default=default,
        help=f"[{default=}] how long to sleep in seconds before calling the "
        "peer again"
    )

    for p in (parser_server, parser_client):
        default = .05
        p.add_argument(
            "-i",
            "--interval-min",
            type=float,
            default=default,
            help=f"[{default=}] minimum delay in seconds between each "
            "iteration of the send-receive loop."
        )
        default = .25
        p.add_argument(
            "-I",
            "--interval-max",
            type=float,
            default=default,
            help=f"[{default=}] maximum delay in seconds between each "
            "iteration of the send-receive loop."
        )
        default = "128 KiB"
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

    for p in (parser_server, parser_client):
        default = 10
        p.add_argument(
            "-r",
            "--retries",
            type=int,
            default=default,
            help=f"[{default=}] how many times to retry when a request to the "
            "bincall server fails."
        )
        default = 3.
        p.add_argument(
            "-t",
            "--retry-interval",
            type=float,
            default=default,
            help=f"[{default=}] how long to wait in seconds before retrying a "
            "request to the bincall server."
        )

    for p in (parser_server, parser_client, parser_delete_acc):
        p.add_argument(
            "-H",
            "--headers",
            type=str,
            help="optional HTTP headers in JSON format to use in requests to "
            "the bincall server."
        )
        p.add_argument(
            "-R",
            "--proxy",
            type=str,
            help="optional HTTP proxy hostname and port separated by a colon."
        )
        p.add_argument(
            "-A",
            "--proxy-auth",
            type=str,
            help="optional HTTP proxy username and password separated by a "
            "colon."
        )
        default = ConnectionModePreference.PreferWebSocket
        p.add_argument(
            "-m",
            "--prefer-conn-mode",
            type=str,
            default=default,
            choices=[
                ConnectionModePreference.PreferWebSocket,
                ConnectionModePreference.PreferHttp,
                ConnectionModePreference.PreferHttpB85
            ],
            help=f"[default={default}] which connection mode to prefer for "
            "calls."
        )

    parser_client.add_argument(
        "-p",
        "--port",
        required=True,
        type=int,
        help=f"local SOCKS5 proxy server port"
    )
    default = "16 KiB"
    parser_client.add_argument(
        "-s",
        "--max-relay-size",
        type=str,
        default=default,
        help=f"[{default=}] maximum number of bytes forwarded from all client "
        "sockets to the goofy server (in socket IO packets) before we send "
        "other enqueued packets (typically command packets for opening sockets "
        "or UDP relays) in each iteration of the GoofyClient's send thread.\n"
        "NOTE: this parameter is always sent to the goofy server in a "
        "GoofyCommandSetLimits packet, so the goofy server will use the same "
        "value when forwarding data from remote sockets to the goofy client."
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

    parser_server.add_argument(
        "-j",
        "--send-bind-address",
        action="store_true",
        help="send the real local bind address (host and port) instead of "
        "0.0.0.0:0 in the open (SOCKS5 CONNECT) command which could expose the "
        "server's local network topology. most clients ignore this anyway."
    )
    parser_client.add_argument(
        "-E",
        "--early-success",
        action="store_true",
        help="when a CONNECT command is received from a local SOCKS5 client, "
        "immediately send a success reply, lying to it that we've connected to "
        "the target so it can start its handshake or send its first message as "
        "early as possible to be buffered. this can save us a full round-trip "
        "to the goofy server, but:\n"
        "1. it is against the SOCKS5 specification.\n"
        "2. we will send a fake bind address (host and port) to the client "
        "because we haven't had time to receive it from the goofy server. this "
        "is fine in 99.9%% of cases because clients usually ignore the bind "
        "address and the goofy server may send fake values for security "
        "anyways.\n"
        "3. if the goofy server informs us that the connection actually "
        "failed, we have no way of sending a proper error to the local client "
        "because we're already started relaying, so we'll just close the "
        "connection.\n"
        "NOTE: this only applies to proxied connections, not direct (bypassed) "
        "ones."
    )

    for p in (parser_server, parser_client):
        p.add_argument(
            "-g",
            "--enable-bind",
            action="store_true",
            help="enable support for the bind command which tells the server "
            "to listen on a random port and send the bind address to the "
            "client which then tells a peer to connect to it. this is "
            "unnecessary for everyday use and could expose the server's local "
            "network topology, so it's disabled by default."
        )
        p.add_argument(
            "-u",
            "--no-udp-relay",
            action="store_true",
            help="disable support for the UDP relay command."
        )

    for p in (
        parser_server,
        parser_client,
        parser_delete_acc,
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
