"""
fully functioning SOCKS5 server (RFC 1928 / RFC 1929).
only used as a reference in this project.

supports:
  - CONNECT: standard TCP proxying
  - BIND: inbound TCP connection brokering
  - UDP ASSOCIATE: UDP datagram proxying
  - no authentication (NO_AUTH only)
"""

import socket
import struct
import threading
import select
import logging
import ipaddress

from .common import *


SOCKS_VERSION = 5

# authentication methods
AUTH_NO_AUTH = 0
AUTH_NO_ACCEPTABLE = 255

# commands
CMD_CONNECT = 1
CMD_BIND = 2
CMD_UDP_ASSOCIATE = 3

# address types
ATYP_IPV4 = 1
ATYP_DOMAIN = 3
ATYP_IPV6 = 4

# reply codes
REP_SUCCESS = 0
REP_GENERAL_FAILURE = 1
REP_NOT_ALLOWED = 2
REP_NET_UNREACHABLE = 3
REP_HOST_UNREACHABLE = 4
REP_CONN_REFUSED = 5
REP_TTL_EXPIRED = 6
REP_CMD_NOT_SUPPORTED = 7
REP_ATYP_NOT_SUPPORTED = 8

# reserved byte, must be 0x00
RSV = 0


class Socks5Server:
    """
    a SOCKS5 proxy server supporting CONNECT, BIND, and UDP ASSOCIATE.

    Args:
        host (str):
            interface to listen on
        port (int):
            port to listen on
        timeout (float):
            socket operation timeout in seconds
        buf_size (int):
            relay buffer size in bytes
        backlog (int):
            TCP listen backlog (queue size)
        bind_timeout (float):
            how long (seconds) a BIND socket waits for the inbound
                      connection from the remote peer.
        udp_timeout (float):
            idle timeout for UDP ASSOCIATE relay threads
        log_level (int):
            logging level
    """

    _log: logging.Logger

    host: str
    port: int
    timeout: float
    buf_size: int
    backlog: int
    bind_timeout: float
    udp_timeout: float

    _server_sock: socket.socket | None = None
    _running: bool = False

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 1080,
        timeout: float = 30.0,
        buf_size: int = 4096,
        backlog: int = 200,
        bind_timeout: float = 60.0,
        udp_timeout: float = 60.0,
        log_level: int | None = None
    ) -> None:
        self._log = make_logger(f"SOCKS5 server", log_level)

        self.host = host
        self.port = port
        self.timeout = timeout
        self.buf_size = buf_size
        self.backlog = backlog
        self.bind_timeout = bind_timeout
        self.udp_timeout = udp_timeout

        self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_sock.bind((self.host, self.port))
        self._server_sock.listen(self.backlog)
        self._running = True
        self._log.info(f"proxy server running on {self.host}:{self.port}")

        try:
            while self._running:
                try:
                    client_sock, client_addr = self._server_sock.accept()
                except OSError:
                    break

                self._log.debug(
                    f"accepted local client {format_addr(client_addr)}"
                )

                t = threading.Thread(
                    target=self._handle_client,
                    name=f"client {format_addr(client_addr)}",
                    args=(client_sock, client_addr),
                    daemon=True,
                )
                t.start()
        except BaseException as e:
            self._log.fatal(format_exception(e))
        finally:
            self.stop()

    def stop(self) -> None:
        """gracefully stop the server"""
        self._running = False
        if self._server_sock:
            close_socket(self._server_sock)
        self._log.info("stopped the server")

    def _handle_client(
        self,
        client: socket.socket,
        addr: tuple[str, int]
    ) -> None:
        """entry point for each client thread"""
        client.settimeout(self.timeout)
        try:
            # handshake
            self._handshake(client)

            # call the appropriate command handler

            cmd, atyp, dst_host, dst_port = self._read_request(client)

            if cmd == CMD_CONNECT:
                cmd_name = "CONNECT"
            elif cmd == CMD_BIND:
                cmd_name = "BIND"
            elif cmd == CMD_UDP_ASSOCIATE:
                cmd_name = "UDP_ASSOCIATE"
            else:
                cmd_name = f"{cmd} (unsupported)"

            self._log.info(
                f"command {cmd_name}, dest: {dst_host}:{dst_port}"
            )

            if cmd == CMD_CONNECT:
                self._cmd_connect(client, atyp, dst_host, dst_port)
            elif cmd == CMD_BIND:
                self._cmd_bind(client, atyp, dst_host, dst_port)
            elif cmd == CMD_UDP_ASSOCIATE:
                self._cmd_udp_associate(client, atyp, dst_host, dst_port)
            else:
                self._send_error(client, REP_CMD_NOT_SUPPORTED)
        except Exception as e:
            self._log.error(format_exception(e))
        finally:
            close_socket(client)

    def _handshake(self, client: socket.socket) -> None:
        """
        perform the SOCKS5 method-negotiation handshake.

        client greeting:
            +-----+----------+----------+
            | VER | NMETHODS | METHODS  |
            +-----+----------+----------+

        server choice:
            +-----+--------+
            | VER | METHOD |
            +-----+--------+
        """

        header = recv_exact(client, 2)
        version, n_methods = struct.unpack("!BB", header)

        if version != SOCKS_VERSION:
            raise OSError(f"unsupported SOCKS version: {version}")

        methods = set(recv_exact(client, n_methods))

        if AUTH_NO_AUTH not in methods:
            client.sendall(struct.pack(
                "!BB",
                SOCKS_VERSION,
                AUTH_NO_ACCEPTABLE
            ))
            raise OSError(
                "client offered no acceptable authentication methods"
            )

        # accept NO_AUTH
        client.sendall(struct.pack("!BB", SOCKS_VERSION, AUTH_NO_AUTH))

    def _read_request(
        self, client: socket.socket
    ) -> tuple[int, int, str, int]:
        """
        parse a SOCKS5 request and return (cmd, atyp, dst_host, dst_port).

        request structure:
            +-----+-----+-------+------+----------+----------+
            | VER | CMD |  RSV  | ATYP | DST.ADDR | DST.PORT |
            +-----+-----+-------+------+----------+----------+
        """

        header = recv_exact(client, 4)
        version, cmd, _, atyp = struct.unpack("!BBBB", header)

        if version != SOCKS_VERSION:
            raise OSError(f"unexpected SOCKS version in request: {version}")

        if atyp == ATYP_IPV4:
            dst_host = socket.inet_ntoa(recv_exact(client, 4))
        elif atyp == ATYP_IPV6:
            dst_host = socket.inet_ntop(
                socket.AF_INET6, recv_exact(client, 16))
        elif atyp == ATYP_DOMAIN:
            length = recv_exact(client, 1)[0]
            dst_host = recv_exact(client, length).decode(
                "ascii", errors="replace")
        else:
            self._send_error(client, REP_ATYP_NOT_SUPPORTED)
            raise OSError(f"unsupported SOCKS5 address type: {atyp}")

        dst_port = struct.unpack("!H", recv_exact(client, 2))[0]
        return cmd, atyp, dst_host, dst_port

    def _cmd_connect(
        self,
        client: socket.socket,
        atyp: int,
        dst_host: str,
        dst_port: int,
    ) -> None:
        """
        CONNECT: establish a TCP connection to the target and relay data.
        """

        # resolve domain names to an IP address
        try:
            info = socket.getaddrinfo(
                dst_host,
                dst_port,
                type=socket.SOCK_STREAM
            )
            if not info:
                raise OSError("address resolution failed")
            family, _, _, _, sockaddr = info[0]
            resolved_ip = sockaddr[0]
        except OSError as e:
            self._send_error(client, REP_HOST_UNREACHABLE)
            raise e

        # connect to the target address
        target = socket.socket(family, socket.SOCK_STREAM)
        target.settimeout(self.timeout)
        try:
            target.connect(sockaddr)
        except ConnectionRefusedError as e:
            close_socket(target)
            self._send_error(client, REP_CONN_REFUSED)
            raise e
        except OSError as e:
            close_socket(target)
            self._send_error(client, REP_HOST_UNREACHABLE)
            raise e

        # inform the client which local address we bound to
        bind_host, bind_port = target.getsockname()[:2]
        self._send_reply(client, REP_SUCCESS, bind_host, bind_port)

        self._log.debug(
            f"CONNECT relaying: local client <-> {dst_host}:{dst_port}"
        )

        try:
            self._relay(client, target)
        finally:
            close_socket(client)
            close_socket(target)

    def _cmd_bind(
        self,
        client: socket.socket,
        atyp: int,
        dst_host: str,
        dst_port: int,
    ) -> None:
        """
        BIND: open a listening socket and send its address to the client.

        flow (RFC 1928 §6):
        1. server opens a TCP listener and sends a reply to the SOCKS client
           containing the bind address.
        2. a remote host connects to that listener (typically the host the
           client asked for in dst_host/dst_port).
        3. server sends a second reply containing the remote peer's address and
           then relays data between the SOCKS client and the remote peer.
        """

        bind_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        bind_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        bind_sock.settimeout(self.bind_timeout)
        try:
            # bind to the server's external interface on a random port
            bind_sock.bind((self.host, 0))
            bind_sock.listen(1)
            bind_host, bind_port = bind_sock.getsockname()
        except OSError as e:
            close_socket(bind_sock)
            self._send_error(client, REP_GENERAL_FAILURE)
            raise e

        # first reply: tell the client where the server is listening
        self._send_reply(client, REP_SUCCESS, bind_host, bind_port)

        self._log.debug(f"BIND listening on {bind_host}:{bind_port}")

        # wait for the expected remote peer to connect
        try:
            remote_sock, remote_addr = bind_sock.accept()
        except TimeoutError as e:
            close_socket(bind_sock)
            self._send_error(client, REP_TTL_EXPIRED)
            raise e
        except OSError as e:
            close_socket(bind_sock)
            self._send_error(client, REP_GENERAL_FAILURE)
            raise e
        finally:
            close_socket(bind_sock)

        self._log.debug(
            f"BIND: inbound connection from {format_addr(remote_addr)}"
        )

        # second reply: tell the client who connected
        remote_host, remote_port = remote_addr[0], remote_addr[1]
        self._send_reply(
            client,
            REP_SUCCESS,
            remote_host,
            remote_port
        )

        remote_sock.settimeout(self.timeout)
        try:
            self._relay(client, remote_sock)
        finally:
            close_socket(client)
            close_socket(remote_sock)

    def _cmd_udp_associate(
        self,
        client: socket.socket,
        atyp: int,
        dst_host: str,
        dst_port: int,
    ) -> None:
        """
        UDP ASSOCIATE: open a UDP relay socket, then forward datagrams between
        the SOCKS client and target hosts according to RFC 1928 §7.

        each datagram is framed with a SOCKS5 UDP request header:
            +-----+------+------+----------+----------+----------+
            | RSV | FRAG | ATYP | DST.ADDR | DST.PORT |   DATA   |
            +-----+------+------+----------+----------+----------+
            |  2  |  1   |  1   | Variable |    2     | Variable |
            +-----+------+------+----------+----------+----------+

        fragmentation (FRAG != 0) is not supported and such datagrams are
        silently dropped per RFC recommendation.
        """

        # open the UDP relay socket on a random port
        udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp_sock.settimeout(self.udp_timeout)
        try:
            udp_sock.bind((self.host, 0))
            udp_host, udp_port = udp_sock.getsockname()
        except OSError as e:
            close_socket(udp_sock)
            self._send_error(client, REP_GENERAL_FAILURE)
            raise e

        # tell the client which UDP address to send its datagrams to
        self._send_reply(client, REP_SUCCESS, udp_host, udp_port)

        self._log.debug(f"UDP ASSOCIATE relay on {udp_host}:{udp_port}")

        # identify the client's UDP source from the TCP control connection
        client_tcp_host = client.getpeername()[0]

        # track the client's UDP address so we can reverse replies from targets
        # back to the correct SOCKS client UDP endpoint.
        client_udp_addr: tuple[str, int] | None = None

        def udp_relay_loop() -> None:
            nonlocal self, udp_sock, client_udp_addr
            while True:
                try:
                    data, sender_addr = udp_sock.recvfrom(65535)
                except OSError:
                    # timeout or socket closed
                    self._log.debug("relay closed")
                    break

                if not data:
                    continue

                sender_host = sender_addr[0]

                # forward datagram from SOCKS client to target.
                # a datagram from the SOCKS client must have a SOCKS5 UDP
                # header. we identify the client by matching its IP with the TCP
                # control IP.
                if sender_host == client_tcp_host:
                    if len(data) < 4:
                        # too short to be a valid SOCKS5 UDP header
                        continue

                    # remember client's UDP address for the return path
                    client_udp_addr = sender_addr

                    rsv, frag, sub_atyp = struct.unpack("!HBB", data[:4])

                    if frag != 0:
                        # fragmentation not supported, drop silently
                        continue

                    offset = 4
                    try:
                        if sub_atyp == ATYP_IPV4:
                            target_host = socket.inet_ntoa(
                                data[offset:offset + 4])
                            offset += 4
                        elif sub_atyp == ATYP_IPV6:
                            target_host = socket.inet_ntop(
                                socket.AF_INET6, data[offset:offset + 16]
                            )
                            offset += 16
                        elif sub_atyp == ATYP_DOMAIN:
                            dlen = data[offset]
                            offset += 1
                            target_host = data[offset:offset + dlen].decode(
                                "ascii",
                                errors="replace"
                            )
                            offset += dlen

                            # resolve the domain to an IP
                            target_host = socket.gethostbyname(target_host)
                        else:
                            # unknown SOCKS5 address type, drop
                            continue

                        target_port = struct.unpack(
                            "!H",
                            data[offset:offset + 2]
                        )[0]
                        offset += 2
                        payload = data[offset:]
                    except (struct.error, OSError, IndexError):
                        continue

                    try:
                        udp_sock.sendto(payload, (target_host, target_port))
                    except OSError:
                        pass

                # receive reply datagram from target, wrap, and forward to the
                # client.
                elif client_udp_addr is not None:
                    target_host, target_port = sender_addr[0], sender_addr[1]

                    # build the SOCKS5 UDP reply header
                    addr_bytes, atyp = self._encode_socks5_addr(
                        target_host,
                        target_port
                    )
                    udp_header = (
                        struct.pack("!HB", 0, 0)  # RSV=0, FRAG=0
                        + addr_bytes
                    )

                    try:
                        udp_sock.sendto(udp_header + data, client_udp_addr)
                    except OSError:
                        pass

        # start a UDP relay thread
        relay_thread = threading.Thread(
            target=udp_relay_loop,
            name=f"{threading.current_thread().name} (UDP relay)",
            daemon=True
        )
        relay_thread.start()

        # block on the TCP control connection. when it closes, tear down UDP
        try:
            while True:
                readable, _, _ = select.select([client], [], [], self.timeout)
                if readable:
                    # EOF on the control socket ends the session
                    if not client.recv(1):
                        break
                # if the relay thread died (e.g. timeout), stop waiting
                if not relay_thread.is_alive():
                    break
        finally:
            close_socket(udp_sock)
            self._log.debug("UDP ASSOCIATE session ended")

    def _relay(
        self,
        a: socket.socket,
        b: socket.socket
    ) -> None:
        """
        bidirectional TCP relay between two sockets.
        runs until either side closes the connection or timeout fires.
        """
        sockets = [a, b]
        while True:
            try:
                readable, _, exceptional = select.select(
                    sockets, [], sockets, self.timeout)
            except (ValueError, OSError):
                break

            if exceptional or not readable:
                break

            for src in readable:
                dst = b if src is a else a
                try:
                    data = src.recv(self.buf_size)
                    if not data:
                        return
                    dst.sendall(data)
                except OSError:
                    return

    def _encode_socks5_addr(self, host: str, port: int) -> tuple[bytes, int]:
        atyp = ATYP_DOMAIN
        try:
            temp = ipaddress.ip_address(host)
            if isinstance(temp, ipaddress.IPv4Address):
                atyp = ATYP_IPV4
            elif isinstance(temp, ipaddress.IPv6Address):
                atyp = ATYP_IPV6
        except ValueError:
            # not a valid IP address so it must be a domain
            pass

        if atyp == ATYP_IPV4:
            host_bytes = socket.inet_aton(host)
        elif atyp == ATYP_IPV6:
            host_bytes = socket.inet_pton(socket.AF_INET6, host)
        elif atyp == ATYP_DOMAIN:
            encoded = host.encode()
            if len(encoded) > 255:
                raise ValueError(
                    "domain name (with UTF-8 encoding) is larger than 255 bytes"
                )
            host_bytes = len(encoded).to_bytes(1) + encoded
        else:
            host_bytes = b"\x00\x00\x00\x00"  # fallback: 0.0.0.0

        port_bytes = struct.pack("!H", port)

        return (host_bytes + port_bytes, atyp)

    def _send_reply(
        self,
        client: socket.socket,
        rep: int,
        bind_host: str,
        bind_port: int
    ) -> bytes:
        """
        send a SOCKS5 reply packet.

            +-----+-----+-------+------+----------+----------+
            | VER | REP |  RSV  | ATYP | BND.ADDR | BND.PORT |
            +-----+-----+-------+------+----------+----------+
        """

        addr_bytes, atyp = self._encode_socks5_addr(bind_host, bind_port)
        header = struct.pack("!BBBB", SOCKS_VERSION, rep, RSV, atyp)
        client.sendall(header + addr_bytes)

    def _send_error(self, client: socket.socket, rep: int) -> None:
        """send a SOCKS5 error reply with a zeroed BND address."""
        try:
            self._send_reply(client, rep, "0.0.0.0", 0)
        except OSError:
            pass


if __name__ == "__main__":
    server = Socks5Server(
        host="0.0.0.0",
        port=10100
    )
