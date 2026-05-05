import socket
import struct
import threading
import time
from dataclasses import dataclass, field
import logging
import random
import ipaddress

from .address_filter import *
from .goofyio import *
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


@dataclass
class GoofyClientSocket:
    client: socket.socket  # local client
    status: GoofySocketStatus = GoofySocketStatus.WaitingToOpen
    bind_host: str = ""
    bind_port: int = 0

    # only for bind/listen
    inbound_host: str = ""
    inbound_port: int = 0

    in_buf: bytearray = field(default_factory=bytearray)
    relaying: bool = False
    last_io_time: float = 0.

    lock: threading.Lock = field(default_factory=threading.Lock)


@dataclass
class GoofyClientUdpRelay:
    sock: socket.socket

    # track the client's UDP address so we can reverse replies from targets back
    # to the correct SOCKS client UDP endpoint.
    client_addr: tuple[str, int] | None = None


class GoofyClient:
    """
    the goofy client runs a local SOCKS5 proxy server that devices/programs on
    the LAN can connect to. it then relays/forwards internet traffic to and from
    a goofy server using a `GoofyIo`.

    Args:

        io (GoofyIo):
            goofy ahh data channel to communicate with the goofy server

        host (str):
            interface to listen on for the local SOCKS5 proxy server

        port (int):
            port to listen on for the local SOCKS5 proxy server

        buf_size (int):
            relay buffer size (how many bytes to forward at a time)

        backlog (int):
            TCP listen backlog (queue size)

        timeout (float):
            socket operation (connecting and IO) timeout in seconds

        bind_timeout (float):
            how long (seconds) a BIND socket waits for the inbound connection
            from the remote peer.

        udp_timeout (float):
            idle timeout for UDP ASSOCIATE relay threads

        poll_interval (float):
            interval in seconds for checking socket status updates

        send_interval (float):
            interval in seconds for the control thread to send all queued
            outgoing packets and relay data from local clients to the server.

        address_filter (str):
            address filter for both bypassed (direct) and proxied connections.
            see `address_filter.py` for more details on the format.

        address_filter_type (AddressFilterType):
            if set to `AddressFilterType.Allow`, will only allow connections
            (direct or proxied) to addresses matching `address_filter`. if set
            to `AddressFilterType.Block`, will block connections to addresses
            matching `address_filter`.

        bypass_filter (str):
            address filter for direct connections. see `address_filter.py` for
            more details on the format.

        bypass_filter_type (AddressFilterType):
            if set to `AddressFilterType.Allow`, will only bypass addresses
            matching `bypass_filter`. if set to `AddressFilterType.Block`, will
            bypass every address except ones matching `bypass_filter`.

        early_success (bool):
            when a CONNECT command is received from a local SOCKS5 client,
            immediately send a success reply, lying to it that we've connected
            to the target so it can start its handshake or send its first
            message as early as possible to be buffered. this can save us a full
            round-trip to the goofy server, but
            1. it is against the SOCKS5 specification.
            2. we will send a fake bind address (host and port) to the client
               because we haven't had time to receive it from the goofy server.
               this is fine in 99.9% of cases because clients usually ignore the
               bind address and the goofy server may send fake values for
               security anyways.
            3. if the goofy server informs us that the connection actually
               failed, we have no way of sending a proper error to the local
               client because we're already started relaying, so we'll just
               close the connection.

            NOTE: this only applies to proxied connections, not direct
            (bypassed) ones.

        log_level (int | None):
            logging level (e.g. `logging.INFO`)
    """

    _log: logging.Logger

    io: GoofyIo
    host: str
    port: int
    buf_size: int
    backlog: int
    timeout: float
    bind_timeout: float
    udp_timeout: float
    poll_interval: float
    send_interval: float
    address_filter: str
    address_filter_type: AddressFilterType
    bypass_filter: str
    bypass_filter_type: AddressFilterType
    early_success: bool

    _server_sock: socket.socket | None = None
    _running: bool = False

    _sockets: dict[int, GoofyClientSocket]
    _sockets_lock: threading.Lock

    _udp_relays: dict[int, GoofyClientUdpRelay]
    _udp_relays_lock: threading.Lock

    """
    thread flow

    [thread where the object was constructed ("main")]
    1. starts the control thread.
    2. starts accepting local SOCKS5 clients and spawning new client threads.

    [client threads]
    1. talk to the client in SOCKS5 language and read its request.
    2. enqueue outgoing command packets to be sent to the goofy server by the
       control thread.
    3. wait for the receive thread to update socket status, bind info, etc. and
       inform the SOCKS5 client.
    4. set the `relaying` flag to true and let the control and receive threads
       handle the rest (relaying data and closing when needed).

    [control thread]
    1. handshakes with the goofy server.
    2. starts the receive thread.
    3. periodically (send_interval) takes everything in the outgoing packets
       queue and sends them to the goofy server.
    4. relays data from local SOCKS5 clients to their corresponding remote peers
       by sending socket IO packets to the goofy server (if their `relaying`
       flag is enabled).

    [receive thread]
    1. receives events and socket IO packets from the goofy server as fast as
       possible.
    2. updates socket info (status, bind address, etc.).
    3. relays data from socket IO packets to local SOCKS5 clients (if their
       `relaying` flag is enabled) or stores it in a buffer for later (if the
       `relaying` flag is not enabled yet).
    """

    _control_thread: threading.Thread | None = None
    _receive_thread: threading.Thread | None = None

    # whether we're currently accepting new local SOCKS5 clients. managed by the
    # control thread.
    _accepting_clients: bool = False

    # accumulate outgoing GoofyPackets from different threads and send them all
    # at once on the control thread. helps with avoiding sending many small
    # messages instead of fewer, larger messages.
    _outgoing_packet_queue: list[GoofyPacket]
    _outgoing_packet_queue_lock: threading.Lock

    def __init__(
        self,
        io: GoofyIo,
        host: str = "0.0.0.0",
        port: int = 1080,
        buf_size: int = 4096,
        backlog: int = 200,
        timeout: float = 60.0,
        bind_timeout: float = 60.0,
        udp_timeout: float = 60.0,
        poll_interval: float = .01,
        send_interval: float = .005,
        address_filter: str = "",
        address_filter_type: AddressFilterType = AddressFilterType.Block,
        bypass_filter: str = ADDRESS_FILTER_LAN,
        bypass_filter_type: AddressFilterType = AddressFilterType.Allow,
        early_success: bool = False,
        log_level: int | None = None
    ) -> None:
        global keyboard_interrupt

        self._log = make_logger(f"goofy client", log_level)

        self.io = io
        self.host = host
        self.port = int(port)
        self.buf_size = int(buf_size)
        self.backlog = int(backlog)
        self.timeout = float(timeout)
        self.bind_timeout = float(bind_timeout)
        self.udp_timeout = float(udp_timeout)
        self.poll_interval = float(poll_interval)
        self.send_interval = float(send_interval)
        self.address_filter = address_filter
        self.address_filter_type = address_filter_type
        self.bypass_filter = bypass_filter
        self.bypass_filter_type = bypass_filter_type
        self.early_success = early_success

        self._sockets = {}
        self._sockets_lock = threading.Lock()

        self._udp_relays = {}
        self._udp_relays_lock = threading.Lock()

        self._outgoing_packet_queue = []
        self._outgoing_packet_queue_lock = threading.Lock()

        self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_sock.bind((self.host, self.port))
        self._server_sock.listen(self.backlog)
        self._server_sock.settimeout(2.)
        self._running = True

        msg = f"local SOCKS5 proxy server running on {self.host}:{self.port}"
        if self.host == "0.0.0.0":
            machine_ips = get_machine_ips()
            if machine_ips:
                msg += f" ({", ".join(machine_ips)})"
        self._log.info(msg)

        # start the control thread
        self._control_thread = threading.Thread(
            target=self._control_thread_run,
            name="control thread",
            args=(),
            daemon=True
        )
        self._control_thread.start()

        # start accepting local SOCKS5 clients
        try:
            while self._running:
                try:
                    client_sock, client_addr = self._server_sock.accept()
                except socket.timeout:
                    # avoid blocking forever so we can get KeyboardInterrupt
                    continue
                except Exception as e:
                    self._log.fatal(f"accept failed: {format_exception(e)}")
                    break

                if not (self._running and self._accepting_clients):
                    try:
                        self._send_error_and_close(
                            client_sock,
                            REP_GENERAL_FAILURE
                        )
                    except OSError:
                        pass
                    continue

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
        except KeyboardInterrupt as e:
            keyboard_interrupt = e
        except BaseException as e:
            self._log.fatal(format_exception(e))
        finally:
            self.stop()

    def running(self) -> bool:
        return self._running

    def stop(self) -> None:
        if not self._running:
            return

        self._running = False
        if self._server_sock:
            close_socket(self._server_sock)

        self._log.info("stopped.")

    def sync_limits(self):
        """
        tell the goofy server to update its buffer size and timeouts to match
        the most recent values. you should only call this if you change
        `buf_size`, `timeout`, `bind_timeout`, or `udp_timeout` after the goofy
        client has already started.
        """
        self._enqueue_outgoing_packet(GoofyCommandSetLimits(
            self.buf_size,
            self.timeout,
            self.bind_timeout,
            self.udp_timeout
        ))

    def _handle_client(
        self,
        client: socket.socket,
        addr: tuple[str, int]
    ) -> None:
        """entry point for each client thread"""

        global keyboard_interrupt

        client.settimeout(self.timeout)
        try:
            # SOCKS5 handshake
            self._socks5_handshake(client)

            # call the appropriate command handler

            cmd, atyp, dst_host, dst_port = self._read_request(client)

            if cmd == CMD_CONNECT:
                cmd_name = f"CONNECT {dst_host}:{dst_port}"
            elif cmd == CMD_BIND:
                cmd_name = "BIND"
            elif cmd == CMD_UDP_ASSOCIATE:
                cmd_name = "UDP_ASSOCIATE"
            else:
                cmd_name = f"unsupported command ({cmd})"

            threading.current_thread().name = \
                f"[{cmd_name}] {threading.current_thread().name}"

            if cmd == CMD_CONNECT:
                dst_addr = f"{dst_host}:{dst_port}"
                if is_address_allowed(
                    dst_addr,
                    self.address_filter,
                    self.address_filter_type
                ):
                    if is_address_allowed(
                        dst_addr,
                        self.bypass_filter,
                        self.bypass_filter_type
                    ):
                        # bypass
                        self._log.info(f"{cmd_name} (direct / bypassed)")
                        self._cmd_connect_direct(
                            client, atyp, dst_host, dst_port
                        )
                    else:
                        # proxy
                        self._log.info(cmd_name)
                        self._cmd_connect(client, atyp, dst_host, dst_port)
                else:
                    self._send_error(client, REP_CONN_REFUSED)
            elif cmd == CMD_BIND:
                self._log.info(cmd_name)
                self._cmd_bind(client, atyp, dst_host, dst_port)
            elif cmd == CMD_UDP_ASSOCIATE:
                self._log.info(cmd_name)
                self._cmd_udp_associate(client, atyp, dst_host, dst_port)
            else:
                self._log.warning(cmd_name)
                self._send_error(client, REP_CMD_NOT_SUPPORTED)
        except KeyboardInterrupt as e:
            keyboard_interrupt = e

            close_socket(client)
            self.stop()
        except BaseException as e:
            self._log.error(format_exception(e))

            close_socket(client)

    def _socks5_handshake(self, client: socket.socket) -> None:
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
            raise Exception(f"unsupported SOCKS version: {version}")

        methods = set(recv_exact(client, n_methods))

        if AUTH_NO_AUTH not in methods:
            client.sendall(struct.pack(
                "!BB",
                SOCKS_VERSION,
                AUTH_NO_ACCEPTABLE
            ))
            raise Exception(
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
            raise Exception(f"unexpected SOCKS version in request: {version}")

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
            raise Exception(f"unsupported SOCKS5 address type: {atyp}")

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

        # generate a random socket ID and keep track of it

        socket_id = self._generate_socket_id()
        sock = GoofyClientSocket(client.dup())

        force_acquire(self._sockets_lock)
        self._sockets[socket_id] = sock
        self._sockets_lock.release()

        self._log.debug(f"registered socket ID {socket_id}")

        # tell goofy server to open the socket
        self._enqueue_outgoing_packet(GoofyCommandOpenSocket(
            socket_id,
            dst_host,
            dst_port
        ))

        if self.early_success:
            # lie to the client that we've already connected so it starts
            # sending its handshake or first message to be buffered. can save us
            # a whole round-trip to the goofy server.
            self._send_reply(
                client,
                REP_SUCCESS,
                "0.0.0.0",
                0
            )

        # wait for the receive thread to update the status
        time_start = time.time()
        while True:
            # stop if socket id is no longer in the dict
            force_acquire(self._sockets_lock)
            if socket_id not in self._sockets.keys():
                self._sockets_lock.release()

                sock.status = GoofySocketStatus.Closed
                if self.early_success:
                    close_socket(client)
                else:
                    self._send_error_and_close(client, REP_GENERAL_FAILURE)

                raise Exception("socket ID is no longer in the dictionary")

            force_acquire(sock.lock)

            status = sock.status
            fail_reply, fail_name = status.failure_to_socks_reply()

            if status == GoofySocketStatus.WaitingToOpen:
                # stop if we've been waiting for too long
                if time.time() - time_start > self.timeout * 1.2:
                    sock.status = GoofySocketStatus.Closed
                    if self.early_success:
                        close_socket(client)
                    else:
                        self._send_error_and_close(
                            client,
                            REP_HOST_UNREACHABLE
                        )

                    self._sockets.pop(socket_id, None)
                    sock.lock.release()
                    self._sockets_lock.release()

                    raise TimeoutError("been waiting to open for too long")

                # otherwise keep waiting
                sock.lock.release()
                self._sockets_lock.release()
                time.sleep(self.poll_interval)
                continue
            elif fail_reply != -1:
                # failed to open

                sock.status = GoofySocketStatus.Closed
                if self.early_success:
                    close_socket(client)
                else:
                    self._send_error_and_close(client, fail_reply)

                self._sockets.pop(socket_id, None)
                sock.lock.release()
                self._sockets_lock.release()

                raise Exception(f"failed to open ({fail_name})")
            elif status in [
                GoofySocketStatus.Open,
                GoofySocketStatus.Closed
            ]:
                # socket was opened (may be closed by now but still)
                self._sockets_lock.release()
                break
            else:
                sock.status = GoofySocketStatus.Closed
                if self.early_success:
                    close_socket(client)
                else:
                    self._send_error_and_close(client, REP_GENERAL_FAILURE)

                self._sockets.pop(socket_id, None)
                sock.lock.release()
                self._sockets_lock.release()

                raise ValueError("unsupported socket status")

        # inform the client which local address the goofy server bound to
        if not self.early_success:
            self._send_reply(
                client,
                REP_SUCCESS,
                sock.bind_host,
                sock.bind_port
            )

        # the control and receive threads will handle relaying and closing
        sock.relaying = True
        sock.last_io_time = time.time()
        sock.lock.release()
        self._log.debug(
            f"relay planned: {dst_host}:{dst_port}"
        )

    def _cmd_connect_direct(
        self,
        client: socket.socket,
        atyp: int,
        dst_host: str,
        dst_port: int,
    ) -> None:
        """
        CONNECT: establish a TCP connection to the target and relay data. uses
        a direct connection instead of proxying through the goofy server.
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
        except ConnectionRefusedError:
            close_socket(target)
            self._send_error(client, REP_CONN_REFUSED)
            return
        except OSError as e:
            close_socket(target)
            self._send_error(client, REP_HOST_UNREACHABLE)
            raise e

        # inform the client which local address we bound to
        bind_host, bind_port = target.getsockname()[:2]
        self._send_reply(client, REP_SUCCESS, bind_host, bind_port)

        # direct relay
        self._log.debug(
            f"direct relaying: {dst_host}:{dst_port}"
        )
        try:
            self._direct_relay(client, target)
        finally:
            close_socket(client)
            close_socket(target)

    def _direct_relay(
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

        # generate a random socket ID and keep track of it

        socket_id = self._generate_socket_id()
        sock = GoofyClientSocket(client.dup())

        force_acquire(self._sockets_lock)
        self._sockets[socket_id] = sock
        self._sockets_lock.release()

        self._log.debug(f"registered socket ID {socket_id}")

        # tell goofy server to bind a listening socket
        self._enqueue_outgoing_packet(GoofyCommandBind(socket_id))

        # wait for the receive thread to update the status
        time_start = time.time()
        while True:
            # stop if socket id is no longer in the dict
            force_acquire(self._sockets_lock)
            if socket_id not in self._sockets.keys():
                self._sockets_lock.release()

                sock.status = GoofySocketStatus.Closed
                self._send_error_and_close(client, REP_GENERAL_FAILURE)

                raise Exception("socket ID is no longer in the dictionary")

            force_acquire(sock.lock)

            status = sock.status
            fail_reply, fail_name = status.failure_to_socks_reply()

            if status == GoofySocketStatus.WaitingToOpen:
                # stop if we've been waiting for longer than bind_timeout
                if time.time() - time_start > self.bind_timeout * 1.2:
                    sock.status = GoofySocketStatus.Closed
                    self._send_error_and_close(client, REP_GENERAL_FAILURE)

                    self._sockets.pop(socket_id, None)
                    sock.lock.release()
                    self._sockets_lock.release()

                    raise TimeoutError("been waiting to open for too long")

                # otherwise keep waiting
                sock.lock.release()
                self._sockets_lock.release()
                time.sleep(self.poll_interval)
                continue
            elif fail_reply != -1:
                # failed to bind

                sock.status = GoofySocketStatus.Closed
                self._send_error_and_close(client, fail_reply)

                self._sockets.pop(socket_id, None)
                sock.lock.release()
                self._sockets_lock.release()

                raise Exception(f"failed to bind ({fail_name})")
            elif status in [
                GoofySocketStatus.Open,
                GoofySocketStatus.Closed
            ]:
                # socket was opened (may be closed by now but still)
                self._sockets_lock.release()
                break
            else:
                sock.status = GoofySocketStatus.Closed
                self._send_error_and_close(client, REP_GENERAL_FAILURE)

                self._sockets.pop(socket_id, None)
                sock.lock.release()
                self._sockets_lock.release()

                raise ValueError("unsupported socket status")

        # first reply: tell the client where the server is listening
        self._send_reply(
            client,
            REP_SUCCESS,
            sock.bind_host,
            sock.bind_port
        )
        self._log.debug(
            f"goofy server listening on "
            f"{sock.bind_host}:{sock.bind_port}"
        )

        # wait for the expected remote peer to connect. we will know it happened
        # when the inbound address is set by the receive thread.
        sock.lock.release()
        time_start = time.time()
        while True:
            # stop if socket id is no longer in the dict
            force_acquire(self._sockets_lock)
            if socket_id not in self._sockets.keys():
                self._sockets_lock.release()

                sock.status = GoofySocketStatus.Closed
                self._send_error_and_close(client, REP_TTL_EXPIRED)

                raise Exception("socket ID is no longer in the dictionary")

            force_acquire(sock.lock)

            if sock.status == GoofySocketStatus.Closed:
                # no remote peer connected
                self._send_error_and_close(client, REP_TTL_EXPIRED)

                self._sockets.pop(socket_id, None)
                sock.lock.release()
                self._sockets_lock.release()

                raise Exception("no remote peer connected")
            elif sock.status != GoofySocketStatus.Open:
                sock.status = GoofySocketStatus.Closed
                self._send_error_and_close(client, REP_GENERAL_FAILURE)

                self._sockets.pop(socket_id, None)
                sock.lock.release()
                self._sockets_lock.release()

                raise ValueError("unexpected socket status")

            if not sock.inbound_host:
                # stop if we've been waiting for longer than bind_timeout
                if time.time() - time_start > self.bind_timeout:
                    sock.status = GoofySocketStatus.Closed
                    self._send_error_and_close(client, REP_TTL_EXPIRED)

                    self._sockets.pop(socket_id, None)
                    sock.lock.release()
                    self._sockets_lock.release()

                    return

                # otherwise keep waiting
                sock.lock.release()
                self._sockets_lock.release()
                time.sleep(self.poll_interval)
                continue
            else:
                self._sockets_lock.release()
                break

        self._log.debug(
            f"inbound connection from "
            f"{sock.inbound_host}:{sock.inbound_port}"
        )

        # second reply: tell the client who connected
        self._send_reply(
            client,
            REP_SUCCESS,
            sock.inbound_host,
            sock.inbound_port
        )

        # the control and receive threads will handle relaying and closing
        sock.relaying = True
        sock.last_io_time = time.time()
        sock.lock.release()
        self._log.debug(
            f"relay planned: {sock.inbound_host}:{sock.inbound_port}"
        )

    def _udp_relay_loop(
        self,
        udp_relay_id: int,
        relay: GoofyClientUdpRelay,
        client_tcp_host: str
    ) -> None:
        global keyboard_interrupt
        try:
            while True:
                try:
                    data, sender_addr = relay.sock.recvfrom(65535)
                except OSError as e:
                    # timeout or socket closed
                    self._log.debug(format_exception(e))
                    break

                # forward datagram from SOCKS client to target.
                # a datagram from the SOCKS client must have a SOCKS5 UDP
                # header. we identify the client by matching its IP with the TCP
                # control IP.
                if sender_addr[0] == client_tcp_host:
                    if not data:
                        continue

                    if len(data) < 8:
                        # too short to be a valid SOCKS5 UDP header
                        continue

                    # remember client's UDP address for the return path
                    relay.client_addr = sender_addr

                    rsv, frag, sub_atyp = struct.unpack("!HBB", data[:4])

                    if frag != 0:
                        # fragmentation not supported, drop silently
                        continue

                    offset = 4
                    try:
                        if sub_atyp == ATYP_IPV4:
                            target_host = socket.inet_ntoa(
                                data[offset:offset + 4]
                            )
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

                    target_addr = f"{target_host}:{target_port}"

                    # ignore if the target address is blocked
                    if not is_address_allowed(
                        target_addr,
                        self.address_filter,
                        self.address_filter_type
                    ):
                        continue

                    if is_address_allowed(
                        target_addr,
                        self.bypass_filter,
                        self.bypass_filter_type
                    ):
                        try:
                            relay.sock.sendto(
                                payload,
                                (target_host, target_port)
                            )
                        except OSError:
                            pass
                    else:
                        # proxy
                        self._enqueue_outgoing_packet(GoofyUdpPacket(
                            udp_relay_id,
                            target_host,
                            target_port,
                            payload
                        ))

                # receive reply datagram from bypassed target, wrap, and forward
                # to the client.
                elif relay.client_addr is not None:
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
                        relay.sock.sendto(udp_header + data, relay.client_addr)
                    except OSError:
                        pass
        except KeyboardInterrupt as e:
            keyboard_interrupt = e
            self.stop()
        except BaseException as e:
            self._log.error(format_exception(e))

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
            self._send_error_and_close(client, REP_GENERAL_FAILURE)
            raise e

        # tell the client which UDP address to send its datagrams to
        self._send_reply(client, REP_SUCCESS, udp_host, udp_port)

        # identify the client's UDP source from the TCP control connection
        client_tcp_host = client.getpeername()[0]

        # generate a random UDP relay ID and keep track of it

        udp_relay_id = self._generate_udp_relay_id()
        relay = GoofyClientUdpRelay(udp_sock.dup())

        force_acquire(self._udp_relays_lock)
        self._udp_relays[udp_relay_id] = relay
        self._udp_relays_lock.release()

        # tell the goofy server to open a relay as well
        self._enqueue_outgoing_packet(
            GoofyCommandOpenUdpRelay(udp_relay_id)
        )

        self._log.debug(
            f"registered UDP relay ID {udp_relay_id} ({udp_host}:{udp_port})"
        )

        # start the relay thread to forward datagrams from the SOCKS5 client to
        # their targets.
        relay_thread = threading.Thread(
            target=self._udp_relay_loop,
            name=f"{threading.current_thread().name} (UDP relay)",
            args=(udp_relay_id, relay, client_tcp_host),
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
            close_socket(relay.sock)

            force_acquire(self._udp_relays_lock)
            self._udp_relays.pop(udp_relay_id, None)
            self._udp_relays_lock.release()

            self._log.debug("session ended")

    def _control_thread_run(self):
        global keyboard_interrupt

        sockets_locked = False
        try:
            self._accepting_clients = False

            self._log.info("starting handshake")

            # handshake: generate question (a random sequence of bytes)
            question_len = random.randint(8, 14)
            question = random.randbytes(question_len)

            # handshake: modify question in a specific way to get the answer
            correct_answer, welcome_byte = goofy_handshake_solve(question)

            # handshake: send the question followed by our version
            self.io.send(
                question_len.to_bytes(1)
                + question
                + GOOFY_VERSION.to_bytes(4)
            )

            if not self._running:
                return

            # handshake: receive the server's version and the answer's length
            buf = self.io.receive(5)
            server_version = int.from_bytes(buf[:4])
            answer_len = buf[-1]

            # handshake: verify server version
            if server_version < GOOFY_MIN_SERVER_VERSION:
                self._log.fatal(
                    f"server version ({server_version}) is older than the "
                    f"minimum supported ({GOOFY_MIN_SERVER_VERSION})."
                )
                try:
                    # send 0 as welcome byte (always incorrect)
                    self.io.send(b"\0")
                except Exception:
                    pass
                self.stop()
                return

            # handshake: verify answer length
            if answer_len != len(correct_answer):
                self._log.fatal(
                    f"handshake answer has the wrong length (expected "
                    f"{len(correct_answer)} bytes but got {answer_len} bytes)."
                )
                try:
                    # send 0 as welcome byte (always incorrect)
                    self.io.send(b"\0")
                except Exception:
                    pass
                self.stop()
                return
            answer = self.io.receive(answer_len)

            # handshake: verify the answer
            if answer != correct_answer:
                self._log.fatal(
                    f"handshake answer was incorrect (expected "
                    f"{format_bytes(correct_answer)} but got "
                    f"{format_bytes(answer)})."
                )
                try:
                    # send 0 as welcome byte (always incorrect)
                    self.io.send(b"\0")
                except Exception:
                    pass
                self.stop()
                return

            # handshake: send welcome byte
            self.io.send(bytes([welcome_byte]))
            self._log.info("goofy proxy handshake was successful")

            # send limits
            GoofyCommandSetLimits(
                self.buf_size,
                self.timeout,
                self.bind_timeout,
                self.udp_timeout
            ).send(self.io)

            # start the receive thread
            self._receive_thread = threading.Thread(
                target=self._receive_thread_run,
                name="receive thread",
                args=(),
                daemon=True
            )
            self._receive_thread.start()

            # start sending packets
            self._accepting_clients = True
            while self._running:
                packets_to_send: list[GoofyPacket] = []

                # get outgoing packets enqueued by other threads
                if self._outgoing_packet_queue_lock.acquire():
                    packets_to_send.extend(self._outgoing_packet_queue)
                    self._outgoing_packet_queue.clear()
                    self._outgoing_packet_queue_lock.release()

                # relay: forward data from local SOCKS5 clients to the goofy
                # server.

                force_acquire(self._sockets_lock)
                sockets_locked = True

                socket_ids_copy = list(self._sockets.keys())
                for socket_id in socket_ids_copy:
                    sock = self._sockets[socket_id]
                    force_acquire(sock.lock)

                    if not sock.relaying:
                        sock.lock.release()
                        continue

                    try:
                        if is_ready_to_read(sock.client):
                            data = sock.client.recv(self.buf_size)
                            sock.last_io_time = time.time()
                            if not data:
                                raise OSError()

                            packets_to_send.append(make_goofy_socket_io_packet(
                                socket_id,
                                data
                            ))
                        elif time.time() - sock.last_io_time > self.timeout:
                            raise TimeoutError()

                        if sock.status == GoofySocketStatus.Closed:
                            raise OSError()
                    except OSError as e:
                        sock.status = GoofySocketStatus.Closed
                        sock.relaying = False
                        close_socket(sock.client)

                        self._sockets.pop(socket_id, None)
                        packets_to_send.append(
                            GoofyCommandCloseSocket(socket_id)
                        )
                    finally:
                        sock.lock.release()

                self._sockets_lock.release()
                sockets_locked = False

                # actually send the packets we've been accumulating
                data = bytes()
                for packet in packets_to_send:
                    data += packet.to_bytes()
                if data:
                    self.io.send(data)

                # chill out
                time.sleep(self.send_interval)

            self._receive_thread.join()
        except BaseException as e:
            if sockets_locked:
                self._sockets_lock.release()

            if isinstance(e, KeyboardInterrupt):
                keyboard_interrupt = e
            else:
                self._log.fatal(format_exception(e))

            try:
                if self._receive_thread and self._receive_thread.is_alive():
                    self._receive_thread.join()
            except KeyboardInterrupt as e:
                keyboard_interrupt = e
            except BaseException:
                pass

            self.stop()

    def _receive_thread_run(self):
        global keyboard_interrupt

        sockets_locked = False
        try:
            while self._running:
                if sockets_locked:
                    self._sockets_lock.release()
                    sockets_locked = False

                packet = receive_goofy_packet(self.io)

                force_acquire(self._sockets_lock)
                sockets_locked = True

                if isinstance(packet, GoofyCommandCloseSocket):
                    if packet.socket_id_u32 not in self._sockets.keys():
                        continue
                    sock = self._sockets[packet.socket_id_u32]

                    force_acquire(sock.lock)
                    sock.status = GoofySocketStatus.Closed
                    self._sockets.pop(packet.socket_id_u32, None)
                    if sock.relaying:
                        close_socket(sock.client)
                    sock.lock.release()
                elif isinstance(
                    packet,
                    (GoofySocketIoPacket, GoofySocketIoSmallPacket)
                ):
                    if packet.socket_id_u32 not in self._sockets.keys():
                        continue
                    sock = self._sockets[packet.socket_id_u32]

                    force_acquire(sock.lock)

                    sock.last_io_time = time.time()

                    if not sock.relaying:
                        sock.in_buf += packet.data
                        sock.lock.release()
                        continue

                    called_sendall = False
                    try:
                        sock.client.sendall(
                            bytes(sock.in_buf) + packet.data
                        )
                        sock.in_buf.clear()
                        called_sendall = True

                        if sock.status == GoofySocketStatus.Closed:
                            raise OSError()
                    except OSError:
                        sock.status = GoofySocketStatus.Closed
                        sock.relaying = False
                        close_socket(sock.client)

                        self._sockets.pop(packet.socket_id_u32, None)

                        # if sendall() caused the error, tell the server to
                        # close the socket.
                        if not called_sendall:
                            self._enqueue_outgoing_packet(
                                GoofyCommandCloseSocket(packet.socket_id_u32)
                            )
                    finally:
                        sock.lock.release()
                elif isinstance(packet, GoofyUdpPacket):
                    force_acquire(self._udp_relays_lock)
                    if packet.udp_relay_id_u16 not in self._udp_relays.keys():
                        self._udp_relays_lock.release()
                        continue

                    relay = self._udp_relays[packet.udp_relay_id_u16]
                    if relay.client_addr is None:
                        self._udp_relays_lock.release()
                        continue

                    try:
                        # wrap with SOCKS5 UDP reply header
                        addr_bytes, atyp = self._encode_socks5_addr(
                            packet.host,
                            packet.port
                        )
                        udp_header = (
                            struct.pack("!HB", 0, 0)  # RSV=0, FRAG=0
                            + addr_bytes
                        )

                        # forward to the client
                        relay.sock.sendto(
                            udp_header + packet.payload,
                            relay.client_addr
                        )
                    except Exception:
                        pass

                    self._udp_relays_lock.release()
                elif isinstance(packet, GoofyEventSocketStatus):
                    if packet.socket_id_u32 not in self._sockets.keys():
                        continue
                    sock = self._sockets[packet.socket_id_u32]

                    force_acquire(sock.lock)
                    sock.status = packet.new_status
                    sock.lock.release()
                elif isinstance(packet, GoofyEventSocketBindInfo):
                    if packet.socket_id_u32 not in self._sockets.keys():
                        continue
                    sock = self._sockets[packet.socket_id_u32]

                    force_acquire(sock.lock)
                    sock.status = packet.new_status
                    sock.bind_host = packet.bind_host
                    sock.bind_port = packet.bind_port
                    sock.lock.release()
                elif isinstance(packet, GoofyEventSocketInboundInfo):
                    if packet.socket_id_u32 not in self._sockets.keys():
                        continue
                    sock = self._sockets[packet.socket_id_u32]

                    force_acquire(sock.lock)
                    sock.status = packet.new_status
                    sock.inbound_host = packet.inbound_host
                    sock.inbound_port = packet.inbound_port
                    sock.lock.release()
                elif isinstance(packet, GoofyEventUdpRelayClosed):
                    force_acquire(self._udp_relays_lock)
                    if packet.udp_relay_id_u16 not in self._udp_relays.keys():
                        self._udp_relays_lock.release()
                        continue

                    relay = self._udp_relays[packet.udp_relay_id_u16]
                    close_socket(relay.sock)

                    self._udp_relays_lock.release()
                else:
                    self._log.warning(
                        f"received unexpected packet type {type(packet)}"
                    )

                self._sockets_lock.release()
                sockets_locked = False
        except BaseException as e:
            if sockets_locked:
                self._sockets_lock.release()

            if isinstance(e, KeyboardInterrupt):
                keyboard_interrupt = e
            else:
                self._log.fatal(format_exception(e))

            self.stop()

    def _enqueue_outgoing_packet(self, packet: GoofyPacket):
        force_acquire(self._outgoing_packet_queue_lock)
        self._outgoing_packet_queue.append(packet)
        self._outgoing_packet_queue_lock.release()

    def _enqueue_outgoing_packets(self, packets: list[GoofyPacket]):
        force_acquire(self._outgoing_packet_queue_lock)
        self._outgoing_packet_queue.extend(packets)
        self._outgoing_packet_queue_lock.release()

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

    def _send_error_and_close(self, client: socket.socket, rep: int) -> None:
        """
        send a SOCKS5 error reply with a zeroed BND address and then close the
        socket.
        """
        try:
            self._send_reply(client, rep, "0.0.0.0", 0)
        except OSError:
            pass
        try:
            client.close()
        except OSError:
            pass

    def _generate_socket_id(self) -> int:
        force_acquire(self._sockets_lock)
        while True:
            id = random.randint(0, 2**32 - 1)
            if id in self._sockets.keys():
                continue

            self._sockets_lock.release()
            return id

    def _generate_udp_relay_id(self) -> int:
        force_acquire(self._udp_relays_lock)
        while True:
            id = random.randint(0, 2**16 - 1)
            if id in self._udp_relays.keys():
                continue

            self._udp_relays_lock.release()
            return id
