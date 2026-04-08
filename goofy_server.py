import socket
import threading
import time
from dataclasses import dataclass, field
import logging

from goofyio import *
from common import *


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

    parameters
    ----------
    io           : goofy ahh data channel to communicate with the goofy client
    send_interval: interval in seconds for the send thread to send all queued
                   outgoing packets and relay data from remote peers to the
                   goofy client.
    log_level    : logging level
    """

    io: GoofyIo

    # these limits (buffer size and timeouts) are typically overridden by the
    # client using a `GoofyCommandSetLimits` packet right after handshake so the
    # default values don't matter much.
    buf_size: int = 4096
    timeout: float = 60.
    bind_timeout: float = 60.
    udp_timeout: float = 60.

    send_interval: float
    log: logging.Logger

    _running: bool = False

    _sockets: dict[int, GoofyServerSocket] = {}
    _sockets_lock = threading.Lock()

    _udp_relays: dict[int, GoofyServerUdpRelay] = {}
    _udp_relays_lock = threading.Lock()

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
    _outgoing_packet_queue: list[GoofyPacket] = []
    _outgoing_packet_queue_lock = threading.Lock()

    def __init__(
        self,
        io: GoofyIo,
        send_interval: float = .001,
        log_level: int = LOG_CONFIG["level"]
    ) -> None:
        self.io = io
        self.send_interval = send_interval
        self.log = make_logger(f"goofy server", log_level)

        sockets_locked = False
        try:
            self.log.info("waiting for handshake question from the client")

            # handshake: receive question followed by the client's version
            question_len = self.io.receive(1)[0]
            buf = self.io.receive(question_len + 4)
            question = buf[:question_len]
            client_version = int.from_bytes(buf[-4:])

            # handshake: verify client version
            if client_version < GOOFY_MIN_CLIENT_VERSION:
                try:
                    self.io.send(GOOFY_VERSION.to_bytes(4) + b"\0")
                except Exception:
                    pass
                self.log.fatal(
                    f"client version ({client_version}) is older than the "
                    f"minimum supported ({GOOFY_MIN_CLIENT_VERSION})."
                )
                self.stop()
                return

            # handshake: send our version followed by the answer
            answer, correct_welcome_byte = goofy_handshake_solve(question)
            self.io.send(
                GOOFY_VERSION.to_bytes(4)
                + len(answer).to_bytes(1)
                + answer
            )

            # handshake: receive and verify welcome byte
            welcome_byte = self.io.receive(1)[0]
            if welcome_byte != correct_welcome_byte:
                self.log.fatal(
                    f"handshake welcome byte was incorrect (expected "
                    f"{correct_welcome_byte:02X} but got {welcome_byte:02X})."
                )
                self.stop()
                return
            self.log.info("handshake was successful")

            self._running = True

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

                packet = receive_goofy_packet(self.io)

                force_acquire(self._sockets_lock)
                sockets_locked = True

                if isinstance(packet, GoofyCommandSetLimits):
                    self.buf_size = packet.buf_size
                    self.timeout = packet.timeout
                    self.bind_timeout = packet.bind_timeout
                    self.udp_timeout = packet.udp_timeout
                elif isinstance(packet, GoofyCommandOpenSocket):
                    t = threading.Thread(
                        target=self._cmd_open_socket,

                        name=f"[open {packet.dst_host}:{packet.dst_port}] "
                        f"{packet.socket_id_u32}",

                        args=(packet,),
                        daemon=True,
                    )
                    t.start()
                elif isinstance(packet, GoofyCommandBind):
                    t = threading.Thread(
                        target=self._cmd_bind,
                        name=f"[bind] {packet.socket_id_u32}",
                        args=(packet,),
                        daemon=True,
                    )
                    t.start()
                elif isinstance(packet, GoofyCommandCloseSocket):
                    if packet.socket_id_u32 not in self._sockets.keys():
                        continue
                    sock = self._sockets[packet.socket_id_u32]

                    force_acquire(sock.lock)
                    close_socket(sock.remote)
                    self._sockets.pop(packet.socket_id_u32, None)
                    sock.lock.release()
                elif isinstance(packet, GoofyCommandOpenUdpRelay):
                    t = threading.Thread(
                        target=self._cmd_udp_relay,
                        name=f"[udp relay] {packet.udp_relay_id_u16}",
                        args=(packet,),
                        daemon=True,
                    )
                    t.start()
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
                    self.log.warning(
                        f"received unexpected packet type {type(packet)}"
                    )

                self._sockets_lock.release()
                sockets_locked = False
        except BaseException as e:
            if sockets_locked:
                self._sockets_lock.release()
            self.log.fatal(format_exception(e))
        finally:
            self.stop()

    def running(self) -> bool:
        return self._running

    def stop(self) -> None:
        if not self._running:
            return

        self._running = False
        self.log.info("stopped.")

    def _cmd_open_socket(self, packet: GoofyCommandOpenSocket):
        try:
            self.log.info("connecting")

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
            target.settimeout(self.timeout)
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

            # register the socket ID to keep track of it
            sock = GoofyServerSocket(target.dup())
            force_acquire(self._sockets_lock)
            self._sockets[packet.socket_id_u32] = sock
            self._sockets_lock.release()

            # inform the client which local address we bound to
            bind_host, bind_port = target.getsockname()[:2]
            self._enqueue_outgoing_packet(GoofyEventSocketBindInfo(
                packet.socket_id_u32,
                GoofySocketStatus.Open,
                bind_host,
                bind_port
            ))

            # the send and main threads will handle relaying and closing
            force_acquire(sock.lock)
            sock.relaying = True
            sock.last_io_time = time.time()
            sock.lock.release()
            self.log.debug(
                f"relay planned: {packet.dst_host}:{packet.dst_port} "
                f"<-> goofy client"
            )
        except BaseException as e:
            self.log.error(format_exception(e))

    def _cmd_bind(self, packet: GoofyCommandBind):
        try:
            self.log.info("binding")

            bind_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            bind_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            bind_sock.settimeout(self.bind_timeout)
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

            self.log.debug(f"listening on {bind_host}:{bind_port}")

            # wait for the expected remote peer to connect
            try:
                remote_sock, remote_addr = bind_sock.accept()
                remote_sock.settimeout(self.timeout)
                remote_host = remote_addr[0]
                remote_port = remote_addr[1]
            except OSError as e:
                close_socket(bind_sock)

                self._enqueue_outgoing_packet(
                    GoofyCommandCloseSocket(packet.socket_id_u32)
                )

                raise e

            self.log.debug(
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
            self.log.debug(
                f"relay planned: {remote_host}:{remote_port} <-> goofy client"
            )
        except BaseException as e:
            self.log.error(format_exception(e))

    def _cmd_udp_relay(self, packet: GoofyCommandOpenUdpRelay):
        udp_sock: socket.socket | None = None
        relay: GoofyServerUdpRelay | None = None
        try:
            self.log.info("starting relay")

            # open the UDP relay socket on a random port
            udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            udp_sock.settimeout(self.udp_timeout)
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
                    self.log.debug(format_exception(e))
                    break

                if not data:
                    continue

                self._enqueue_outgoing_packet(GoofyUdpPacket(
                    packet.udp_relay_id_u16,
                    sender_addr[0],
                    sender_addr[1],
                    data
                ))

            self.log.debug("session ended")
        except BaseException as e:
            self.log.error(format_exception(e))
        finally:
            if udp_sock is not None:
                close_socket(udp_sock)
            if relay is not None:
                close_socket(relay.sock)

            force_acquire(self._udp_relays_lock)
            self._udp_relays.pop(packet.udp_relay_id_u16, None)
            self._udp_relays_lock.release()

    def _send_thread_run(self):
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
                            data = sock.remote.recv(self.buf_size)
                            sock.last_io_time = time.time()
                            if not data:
                                raise OSError()

                            packets_to_send.append(make_goofy_socket_io_packet(
                                socket_id,
                                data
                            ))
                        elif time.time() - sock.last_io_time > self.timeout:
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
                    self.io.send(data)

                # chill out
                time.sleep(self.send_interval)
        except BaseException as e:
            if sockets_locked:
                self._sockets_lock.release()
            self.log.fatal(format_exception(e))
            self.stop()

    def _enqueue_outgoing_packet(self, packet: GoofyPacket):
        force_acquire(self._outgoing_packet_queue_lock)
        self._outgoing_packet_queue.append(packet)
        self._outgoing_packet_queue_lock.release()

    def _enqueue_outgoing_packets(self, packets: list[GoofyPacket]):
        force_acquire(self._outgoing_packet_queue_lock)
        self._outgoing_packet_queue.extend(packets)
        self._outgoing_packet_queue_lock.release()
