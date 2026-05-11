import time
from enum import StrEnum
from typing import Self
import logging
import argparse

from goofyproxy import GoofyServer, GoofyClient, AddressFilterType, \
    ADDRESS_FILTER_HELP, ADDRESS_FILTER_LAN
import goofyproxy.common as goofycommon

from s3io import S3Io


def run(args: argparse.Namespace):
    if args.command == "filter-help":
        print(ADDRESS_FILTER_HELP)
        return

    max_out_size = goofycommon.parse_data_size(
        args.max_out_size
    )

    if args.command == "server":
        while True:
            gio = S3Io(
                endpoint_url=args.url,
                access_key=args.access_key,
                secret_key=args.secret_key,
                bucket_name=args.bucket_name,
                id=args.id,
                peer_id=args.peer_id,
                max_out_size=max_out_size,
            )

            GoofyServer(
                gio,
                send_interval=.005,
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
            gio = S3Io(
                endpoint_url=args.url,
                access_key=args.access_key,
                secret_key=args.secret_key,
                bucket_name=args.bucket_name,
                id=args.id,
                peer_id=args.peer_id,
                max_out_size=max_out_size,
            )

            GoofyClient(
                gio,
                host="0.0.0.0",
                port=args.port,
                buf_size=args.bufsize,
                poll_interval=.005,
                send_interval=.005,
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
        description="goofy proxy on s3io: share your internet connection with "
        "a friend by creating and reading files on an AWS S3 Bucket."
    )
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
        help="sub-commands"
    )

    parser_server = subparsers.add_parser(
        "server",
        help="run GoofyServer on top of s3io."
    )
    parser_client = subparsers.add_parser(
        "client",
        help="run GoofyClient on top of s3io. the peer must be running "
        "GoofyServer on their side."
    )
    parser_filter_help = subparsers.add_parser(
        "filter-help",
        help="print a detailed help message on address filter patterns."
    )

    for p in (parser_server, parser_client):
        p.add_argument(
            "url",
            type=str,
            help="S3 endpoint URL"
        )
        p.add_argument(
            "access_key",
            type=str,
            help="S3 access key"
        )
        p.add_argument(
            "secret_key",
            type=str,
            help="S3 secret key"
        )
        p.add_argument(
            "bucket_name",
            type=str,
            help="S3 bucket name"
        )
        p.add_argument(
            "id",
            type=str,
            help="sender ID to include in outgoing files so the other side "
            "knows who sent it."
        )
        p.add_argument(
            "peer_id",
            type=str,
            help="sender ID of the peer."
        )
        p.add_argument(
            "-e",
            "--endless",
            action="store_true",
            help="restart when an error occurs."
        )
        default = 30.
        p.add_argument(
            "-w",
            "--endless-wait",
            type=float,
            default=default,
            help=f"[{default=}] how long to sleep in seconds before restarting."
        )
        default = "64 KiB"
        p.add_argument(
            "-O",
            "--max-out-size",
            type=str,
            default=default,
            help=f"[{default=}] maximum outgoing file size in each iteration "
            "of the send-receive loop."
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
        "4. some clients may use a shorter timeout for IO than for the initial "
        "connection and close the connection before we ever get a real "
        "response from the remote target.\n"
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
