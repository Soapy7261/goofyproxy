import socket
import threading
import time
from dataclasses import dataclass, field
import logging
from pympler.asizeof import asizeof

from .address_filter import *
from .goofyio import *
from .common import *


@dataclass
class GoofyServerSocket:
    remote: socket.socket  # remote peer

    in_buf: bytearray = field(default_factory=bytearray)
    relaying: bool = False
    last_io_time: float = 0.

    lock: threading.Lock = field(default_factory=threading.Lock)


@dataclass
class GoofyServerUdpRelay:
    sock: socket.socket


class GoofyServer:
    """
    the goofy server receives commands from a goofy client using a `GoofyIo` to
    open and manage sockets and relay/forward internet traffic to and from the
    client.

    Args:

        io (GoofyIo):
            goofy ahh data channel to communicate with the goofy client

        send_interval (float):
            interval in seconds for the send thread to send all queued outgoing
            packets and relay data from remote peers to the goofy client.

        address_filter (str):
            address filter for remote connections. see `address_filter.py` for
            more details on the format.

        address_filter_type (AddressFilterType):
            if set to `AddressFilterType.Allow`, will only allow remote
            connections to addresses matching `address_filter`. if set to
            `AddressFilterType.Block`, will block remote connections to
            addresses matching `address_filter`.

        fake_bind_address (bool):
            send a fake bind address (0.0.0.0:0) in the open (SOCKS5 CONNECT)
            command to avoid exposing the server's local network topology. most
            clients ignore this anyway.

        enable_bind (bool):
            enable support for the bind command which tells the server to listen
            on a random port and send the bind address to the client which then
            tells a peer to connect to it. this is unnecessary for everyday use
            and could expose the server's local network topology, so it's
            disabled by default.

        enable_udp_relay (bool):
            enable support for the UDP relay command.

        memory_limit_mib (float):
            maximum memory usage of sockets and UDP relays in Mebibytes (1 MiB =
            1048576 bytes) before we start deleting the ones with the highest
            memory usage.

        log_level (int | None):
            logging level (e.g. `logging.INFO`)
    """

    _log: logging.Logger

    _io: GoofyIo

    _buf_size: int = 4096
    _timeout: float = 60.
    _bind_timeout: float = 60.
    _udp_timeout: float = 60.

    _send_interval: float
    _address_filter: str
    _address_filter_type: AddressFilterType

    _fake_bind_address: bool
    _enable_bind: bool
    _enable_udp_relay: bool
    _memory_limit_mib: float

    _running: bool = True

    @property
    def io(self) -> GoofyIo:
        return self._io

    @property
    def buf_size(self) -> int:
        """
        the buffer size and timeouts are typically overridden by the client
        using a `GoofyCommandSetLimits` packet right after handshake so the
        initial values don't matter much.
        """
        return self._buf_size

    @property
    def timeout(self) -> float:
        """
        the buffer size and timeouts are typically overridden by the client
        using a `GoofyCommandSetLimits` packet right after handshake so the
        initial values don't matter much.
        """
        return self._timeout

    @property
    def bind_timeout(self) -> float:
        """
        the buffer size and timeouts are typically overridden by the client
        using a `GoofyCommandSetLimits` packet right after handshake so the
        initial values don't matter much.
        """
        return self._bind_timeout

    @property
    def udp_timeout(self) -> float:
        """
        the buffer size and timeouts are typically overridden by the client
        using a `GoofyCommandSetLimits` packet right after handshake so the
        initial values don't matter much.
        """
        return self._udp_timeout

    @property
    def send_interval(self) -> float:
        return self._send_interval

    @send_interval.setter
    def send_interval(self, value: float):
        self._send_interval = float(value)

    @property
    def address_filter(self) -> str:
        return self._address_filter

    @address_filter.setter
    def address_filter(self, value: str):
        self._address_filter = str(value)

    @property
    def address_filter_type(self) -> AddressFilterType:
        return self._address_filter_type

    @address_filter_type.setter
    def address_filter_type(self, value: AddressFilterType):
        self._address_filter_type = AddressFilterType(value)

    @property
    def fake_bind_address(self) -> bool:
        return self._fake_bind_address

    @fake_bind_address.setter
    def fake_bind_address(self, value: bool):
        self._fake_bind_address = bool(value)

    @property
    def enable_bind(self) -> bool:
        return self._enable_bind

    @enable_bind.setter
    def enable_bind(self, value: bool):
        self._enable_bind = bool(value)

    @property
    def enable_udp_relay(self) -> bool:
        return self._enable_udp_relay

    @enable_udp_relay.setter
    def enable_udp_relay(self, value: bool):
        self._enable_udp_relay = bool(value)

    @property
    def memory_limit_mib(self) -> float:
        return self._memory_limit_mib

    @memory_limit_mib.setter
    def memory_limit_mib(self, value: float):
        self._memory_limit_mib = float(value)

    @property
    def running(self) -> bool:
        return self._running

    _sockets: dict[int, GoofyServerSocket]
    _sockets_lock: threading.Lock

    _udp_relays: dict[int, GoofyServerUdpRelay]
    _udp_relays_lock: threading.Lock

    """
    thread flow

    [thread where the object was constructed ("main")]
    1. handshakes with the client.
    2. starts the send thread.
    3. receives commands from the goofy client and spawns new command threads
       if needed (some simple commands don't need a separate thread).
    4. receives socket IO packets and relays their data to remote peers (if
       their `relaying` flag is enabled) or stores it in a buffer for later (if
       the `relaying` flag is not enabled yet).

    [command threads]
    1. execute the command at hand (open a socket, bind, etc.)
    2. enqueue outgoing event packets (e.g. update socket info) to be sent to
       the goofy client on the send thread.
    3. set the `relaying` flag to true and let the send and main threads handle
       handle the rest (relaying data and closing when needed).

    [send thread]
    1. periodically (send_interval) takes everything in the outgoing packets
       queue and sends them to the goofy client.
    2. relays data from remote peers (if their `relaying` flag is enabled) to
       the goofy client by sending socket IO packets.
    """

    _send_thread: threading.Thread | None = None

    # accumulate outgoing GoofyPackets from different threads and send them all
    # at once on the send thread. helps with avoiding sending many small
    # messages instead of fewer, larger messages.
    _outgoing_packet_queue: list[GoofyPacket]
    _outgoing_packet_queue_lock: threading.Lock

    _last_memory_cleanup_time: float = 0.

    def __init__(
        self,
        io: GoofyIo,
        send_interval: float = .004,
        address_filter: str = ADDRESS_FILTER_LAN,
        address_filter_type: AddressFilterType = AddressFilterType.Block,
        fake_bind_address: bool = True,
        enable_bind: bool = False,
        enable_udp_relay: bool = True,
        memory_limit_mib: float = 2048.,
        log_level: int | None = None
    ) -> None:
        global keyboard_interrupt

        self._log = make_logger(f"GoofyServer", log_level)

        self._io = io
        self._send_interval = float(send_interval)
        self._address_filter = address_filter
        self._address_filter_type = address_filter_type

        self._fake_bind_address = fake_bind_address
        self._enable_bind = enable_bind
        self._enable_udp_relay = enable_udp_relay
        self._memory_limit_mib = float(memory_limit_mib)

        self._sockets = {}
        self._sockets_lock = threading.Lock()

        self._udp_relays = {}
        self._udp_relays_lock = threading.Lock()

        self._outgoing_packet_queue = []
        self._outgoing_packet_queue_lock = threading.Lock()

        sockets_locked = False
        try:
            self._log.info("waiting for handshake question from the client")

            # handshake: receive question followed by the client's version
            question_len = self._io.receive(1)[0]
            buf = self._io.receive(question_len + 4)
            question = buf[:question_len]
            client_version = int.from_bytes(buf[-4:])

            # handshake: verify client version
            if client_version < GOOFY_MIN_CLIENT_VERSION:
                try:
                    self._io.send(GOOFY_VERSION.to_bytes(4) + b"\0")
                except Exception:
                    pass
                self._log.fatal(
                    f"client version ({client_version}) is older than the "
                    f"minimum supported ({GOOFY_MIN_CLIENT_VERSION})."
                )
                self.stop()
                return

            # handshake: send our version followed by the answer
            answer, correct_welcome_byte = goofy_handshake_solve(question)
            self._io.send(
                GOOFY_VERSION.to_bytes(4)
                + len(answer).to_bytes(1)
                + answer
            )

            # handshake: receive and verify welcome byte
            welcome_byte = self._io.receive(1)[0]
            if welcome_byte != correct_welcome_byte:
                self._log.fatal(
                    f"handshake welcome byte was incorrect (expected "
                    f"{correct_welcome_byte:02X} but got {welcome_byte:02X})."
                )
                self.stop()
                return
            self._log.info("goofy proxy handshake was successful")

            # start the send thread
            self._send_thread = threading.Thread(
                target=self._send_thread_run,
                name="send thread",
                args=(),
                daemon=True
            )
            self._send_thread.start()

            # start receiving commands
            while self._running:
                if sockets_locked:
                    self._sockets_lock.release()
                    sockets_locked = False

                packet = receive_goofy_packet(self._io)

                force_acquire(self._sockets_lock)
                sockets_locked = True

                if isinstance(packet, GoofyCommandSetLimits):
                    self._buf_size = packet.buf_size
                    self._timeout = packet.timeout
                    self._bind_timeout = packet.bind_timeout
                    self._udp_timeout = packet.udp_timeout
                elif isinstance(packet, GoofyCommandOpenSocket):
                    addr = f"{packet.dst_host}:{packet.dst_port}"
                    if is_address_allowed(
                        addr,
                        address_filter,
                        address_filter_type
                    ):
                        # register the socket ID as early as possible so we can
                        # buffer potential incoming data from the client.
                        self._sockets[packet.socket_id_u32] = \
                            GoofyServerSocket(None)

                        # then start the actual thread
                        t = threading.Thread(
                            target=self._cmd_open_socket,

                            name=f"[open {addr}] {packet.socket_id_u32}",

                            args=(packet,),
                            daemon=True,
                        )
                        t.start()
                    else:
                        self._enqueue_outgoing_packet(GoofyEventSocketStatus(
                            packet.socket_id_u32,
                            GoofySocketStatus.FailedToOpenConnRefused
                        ))
                elif isinstance(packet, GoofyCommandBind):
                    if self._enable_bind:
                        t = threading.Thread(
                            target=self._cmd_bind,
                            name=f"[bind] {packet.socket_id_u32}",
                            args=(packet,),
                            daemon=True,
                        )
                        t.start()
                    else:
                        self._enqueue_outgoing_packet(GoofyEventSocketStatus(
                            packet.socket_id_u32,
                            GoofySocketStatus.FailedToOpenGeneral
                        ))
                elif isinstance(packet, GoofyCommandCloseSocket):
                    if packet.socket_id_u32 not in self._sockets.keys():
                        continue
                    sock = self._sockets[packet.socket_id_u32]

                    force_acquire(sock.lock)
                    close_socket(sock.remote)
                    self._sockets.pop(packet.socket_id_u32, None)
                    sock.lock.release()
                elif isinstance(packet, GoofyCommandOpenUdpRelay):
                    if self._enable_udp_relay:
                        t = threading.Thread(
                            target=self._cmd_udp_relay,
                            name=f"[UDP relay] {packet.udp_relay_id_u16}",
                            args=(packet,),
                            daemon=True,
                        )
                        t.start()
                    else:
                        self._enqueue_outgoing_packet(
                            GoofyEventUdpRelayClosed(packet.udp_relay_id_u16)
                        )
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

                    try:
                        sock.remote.sendall(
                            bytes(sock.in_buf) + packet.data
                        )
                        sock.in_buf.clear()
                    except OSError:
                        sock.relaying = False
                        close_socket(sock.remote)

                        self._sockets.pop(packet.socket_id_u32, None)
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

                    # forward UDP packet from goofy client to the remote target
                    try:
                        relay = self._udp_relays[packet.udp_relay_id_u16]
                        relay.sock.sendto(
                            packet.payload,
                            (packet.host, packet.port)
                        )
                    except Exception:
                        pass

                    self._udp_relays_lock.release()
                else:
                    self._log.warning(
                        f"received unexpected packet type {type(packet)}"
                    )

                self._sockets_lock.release()
                sockets_locked = False

                # clean up memory if needed
                self._cleanup_memory()
        except KeyboardInterrupt as e:
            keyboard_interrupt = e
        except BaseException as e:
            if self._running:
                self._log.fatal(format_exception(e))
        finally:
            if sockets_locked:
                self._sockets_lock.release()
            self.stop()

    def stop(self) -> None:
        """stop the execution permanently and irreversibly."""
        if not self._running:
            return
        self._running = False
        self._log.info("stopped.")

    def _cmd_open_socket(self, packet: GoofyCommandOpenSocket):
        global keyboard_interrupt
        try:
            self._log.info("connecting")

            # resolve domain names to an IP address
            try:
                info = socket.getaddrinfo(
                    packet.dst_host,
                    packet.dst_port,
                    type=socket.SOCK_STREAM
                )
                if not info:
                    raise Exception("address resolution failed")
                family, _, _, _, sockaddr = info[0]
                resolved_ip = sockaddr[0]
            except Exception as e:
                self._enqueue_outgoing_packet(GoofyEventSocketStatus(
                    packet.socket_id_u32,
                    GoofySocketStatus.FailedToOpenHostUnreachable
                ))
                raise e

            # connect to the target host
            target = socket.socket(family, socket.SOCK_STREAM)
            target.settimeout(self._timeout)
            try:
                target.connect(sockaddr)
            except ConnectionRefusedError:
                close_socket(target)

                self._enqueue_outgoing_packet(GoofyEventSocketStatus(
                    packet.socket_id_u32,
                    GoofySocketStatus.FailedToOpenConnRefused
                ))

                raise e
            except OSError as e:
                close_socket(target)

                self._enqueue_outgoing_packet(GoofyEventSocketStatus(
                    packet.socket_id_u32,
                    GoofySocketStatus.FailedToOpenHostUnreachable
                ))

                raise e

            force_acquire(self._sockets_lock)
            if packet.socket_id_u32 not in self._sockets.keys():
                self._sockets_lock.release()
                raise LookupError(
                    f"socket ID {packet.socket_id_u32} missing"
                )
            sock = self._sockets[packet.socket_id_u32]
            self._sockets_lock.release()

            # inform the client which local address we bound to
            if self._fake_bind_address:
                bind_host, bind_port = ("0.0.0.0", 0)
            else:
                bind_host, bind_port = target.getsockname()[:2]
            self._enqueue_outgoing_packet(GoofyEventSocketBindInfo(
                packet.socket_id_u32,
                GoofySocketStatus.Open,
                bind_host,
                bind_port
            ))

            # the send and main threads will handle relaying and closing
            force_acquire(sock.lock)
            sock.remote = target.dup()
            sock.relaying = True
            sock.last_io_time = time.time()
            sock.lock.release()
            self._log.debug(
                f"relay planned: {packet.dst_host}:{packet.dst_port}"
            )
        except KeyboardInterrupt as e:
            keyboard_interrupt = e
            self.stop()
        except BaseException as e:
            if self._running:
                self._log.error(format_exception(e))

    def _cmd_bind(self, packet: GoofyCommandBind):
        global keyboard_interrupt
        try:
            self._log.info("binding")

            bind_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            bind_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            bind_sock.settimeout(self._bind_timeout)
            try:
                # bind to the server's external interface on a random port
                bind_sock.bind(("0.0.0.0", 0))
                bind_sock.listen(1)
                bind_host, bind_port = bind_sock.getsockname()
            except OSError as e:
                close_socket(bind_sock)

                self._enqueue_outgoing_packet(GoofyEventSocketStatus(
                    packet.socket_id_u32,
                    GoofySocketStatus.FailedToOpenGeneral
                ))

                raise e

            # first reply: tell the client where the server is listening
            self._enqueue_outgoing_packet(GoofyEventSocketBindInfo(
                packet.socket_id_u32,
                GoofySocketStatus.Open,
                bind_host,
                bind_port
            ))

            self._log.debug(f"listening on {bind_host}:{bind_port}")

            # wait for the expected remote peer to connect
            try:
                start_time = time.time()
                while True:
                    remote_sock, remote_addr = bind_sock.accept()
                    remote_sock.settimeout(self._timeout)
                    remote_host = remote_addr[0]
                    remote_port = remote_addr[1]

                    if is_address_allowed(
                        f"{remote_host}:{remote_port}",
                        self._address_filter,
                        self._address_filter_type
                    ):
                        break
                    elif time.time() - start_time > self._bind_timeout:
                        raise TimeoutError()
            except OSError as e:
                close_socket(bind_sock)

                self._enqueue_outgoing_packet(
                    GoofyCommandCloseSocket(packet.socket_id_u32)
                )

                raise e

            self._log.debug(
                f"inbound connection from {remote_host}:{remote_port}"
            )

            # register the socket ID to keep track of it
            sock = GoofyServerSocket(remote_sock.dup())
            force_acquire(self._sockets_lock)
            self._sockets[packet.socket_id_u32] = sock
            self._sockets_lock.release()

            # second reply: tell the client who connected
            self._enqueue_outgoing_packet(GoofyEventSocketInboundInfo(
                packet.socket_id_u32,
                GoofySocketStatus.Open,
                remote_host,
                remote_port
            ))

            # the send and main threads will handle relaying and closing
            force_acquire(sock.lock)
            sock.relaying = True
            sock.last_io_time = time.time()
            sock.lock.release()
            self._log.debug(
                f"relay planned: {remote_host}:{remote_port}"
            )
        except KeyboardInterrupt as e:
            keyboard_interrupt = e
            self.stop()
        except BaseException as e:
            if self._running:
                self._log.error(format_exception(e))

    def _cmd_udp_relay(self, packet: GoofyCommandOpenUdpRelay):
        global keyboard_interrupt

        udp_sock: socket.socket | None = None
        relay: GoofyServerUdpRelay | None = None
        try:
            self._log.info("starting UDP relay")

            # open the UDP relay socket on a random port
            udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            udp_sock.settimeout(self._udp_timeout)
            try:
                udp_sock.bind(("0.0.0.0", 0))
            except OSError as e:
                self._enqueue_outgoing_packet(
                    GoofyEventUdpRelayClosed(packet.udp_relay_id_u16)
                )
                raise e

            # register the UDP relay ID to keep track of it
            relay = GoofyServerUdpRelay(udp_sock.dup())
            force_acquire(self._udp_relays_lock)
            self._udp_relays[packet.udp_relay_id_u16] = relay
            self._udp_relays_lock.release()

            # start receiving packets from remote hosts and forwarding them to
            # the goofy client. on the other hand, the main thread will receive
            # `GoofyUdpPacket`s from the goofy client and send them to their
            # targets using our udp_sock (relay.sock).
            while True:
                try:
                    data, sender_addr = udp_sock.recvfrom(65535)
                except OSError as e:
                    # timeout or socket closed
                    if self._running:
                        self._log.debug(format_exception(e))
                    break

                if not data:
                    continue

                if not is_address_allowed(
                    f"{sender_addr[0]}:{sender_addr[1]}",
                    self._address_filter,
                    self._address_filter_type
                ):
                    continue

                self._enqueue_outgoing_packet(GoofyUdpPacket(
                    packet.udp_relay_id_u16,
                    sender_addr[0],
                    sender_addr[1],
                    data
                ))

            if self._running:
                self._log.debug("UDP relay session ended")
        except KeyboardInterrupt as e:
            keyboard_interrupt = e
        except BaseException as e:
            if self._running:
                self._log.error(format_exception(e))
        finally:
            if udp_sock is not None:
                close_socket(udp_sock)
            if relay is not None:
                close_socket(relay.sock)

            force_acquire(self._udp_relays_lock)
            self._udp_relays.pop(packet.udp_relay_id_u16, None)
            self._udp_relays_lock.release()

            if keyboard_interrupt:
                self.stop()

    def _send_thread_run(self):
        global keyboard_interrupt

        sockets_locked = False
        try:
            while self._running:
                packets_to_send: list[GoofyPacket] = []

                # get outgoing packets enqueued by other threads
                if self._outgoing_packet_queue_lock.acquire():
                    packets_to_send.extend(self._outgoing_packet_queue)
                    self._outgoing_packet_queue.clear()
                    self._outgoing_packet_queue_lock.release()

                # relay: forward data from remote peers to the goofy client.

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
                        if is_ready_to_read(sock.remote):
                            data = sock.remote.recv(self._buf_size)
                            sock.last_io_time = time.time()
                            if not data:
                                raise OSError()

                            packets_to_send.append(make_goofy_socket_io_packet(
                                socket_id,
                                data
                            ))
                        elif time.time() - sock.last_io_time > self._timeout:
                            raise TimeoutError()
                    except OSError:
                        sock.relaying = False
                        close_socket(sock.remote)

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
                    self._io.send(data)

                # chill out
                time.sleep(self._send_interval)
        except BaseException as e:
            if sockets_locked:
                self._sockets_lock.release()

            if isinstance(e, KeyboardInterrupt):
                keyboard_interrupt = e
            elif self._running:
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

    def _cleanup_memory(self):
        if time.time() - self._last_memory_cleanup_time \
                < GOOFY_MEMORY_CLEANUP_INTERVAL:
            return
        self._last_memory_cleanup_time = time.time()

        force_acquire(self._sockets_lock)
        force_acquire(self._udp_relays_lock)

        extra_bytes = int(
            asizeof(self._sockets, self._udp_relays)
            - (self._memory_limit_mib * 1024. * 1024.)
        )
        initial_extra_bytes = extra_bytes
        if extra_bytes < 1:
            self._udp_relays_lock.release()
            self._sockets_lock.release()
            return

        # sort sockets and UDP relays by decreasing size
        flat: list[tuple[int, GoofyServerSocket | GoofyServerUdpRelay]] = \
            list(self._sockets.items()) + list(self._udp_relays.items())
        flat.sort(key=lambda s: asizeof(s[1]), reverse=True)

        # keep deleting until we go below the memory limit
        n_sockets_deleted: int = 0
        n_udp_relays_deleted: int = 0
        while extra_bytes > 0 and flat:
            item = flat[0]
            flat = flat[1:]
            if isinstance(item[1], GoofyServerSocket):
                socket_id, sock = item

                force_acquire(sock.lock)
                extra_bytes -= asizeof(sock)

                sock.relaying = False
                sock.in_buf.clear()
                close_socket(sock.remote)

                self._sockets.pop(socket_id, None)
                sock.lock.release()

                n_sockets_deleted += 1
            elif isinstance(item[1], GoofyServerUdpRelay):
                relay_id, relay = item

                force_acquire(relay.lock)
                extra_bytes -= asizeof(relay)
                close_socket(relay.sock)
                self._udp_relays.pop(relay_id, None)
                relay.lock.release()

                n_udp_relays_deleted += 1
            else:
                raise TypeError(
                    f"unsupported type for memory cleanup: {type(item[1])}"
                )

        self._udp_relays_lock.release()
        self._sockets_lock.release()

        self._log.warning(
            f"cleaned up {format_data_size(initial_extra_bytes - extra_bytes)} "
            f"of memory by deleting {n_sockets_deleted} sockets and "
            f"{n_udp_relays_deleted} UDP relays."
        )
