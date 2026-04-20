import sys
import logging
import time
import socket
import select
import threading
from dataclasses import dataclass
from enum import IntEnum
import struct
import io

from goofyio import *

# goofy proxy version
GOOFY_VERSION = 1

# the goofy client can only work with servers of this version or higher.
GOOFY_MIN_SERVER_VERSION = 1

# the goofy server can only work with clients of this version or higher.
GOOFY_MIN_CLIENT_VERSION = 1

# logging settings
LOG_CONFIG = {
    # logging level
    "level": logging.INFO,
    # use terminal colors
    "colorize": True,
    # always include the thread ID, even when it has a name.
    "always_include_thread_id": False,
    "file": None
}


# ANSI color codes
COL_RESET = "\033[0m"
COL_BLACK = "\033[30m"
COL_RED = "\033[31m"
COL_GREEN = "\033[32m"
COL_YELLOW = "\033[33m"
COL_BLUE = "\033[34m"
COL_MAGENTA = "\033[35m"
COL_CYAN = "\033[36m"
COL_WHITE = "\033[37m"
COL_BRIGHT_BLACK = "\033[90m"
COL_BRIGHT_RED = "\033[91m"
COL_BRIGHT_GREEN = "\033[92m"
COL_BRIGHT_YELLOW = "\033[93m"
COL_BRIGHT_BLUE = "\033[94m"
COL_BRIGHT_MAGENTA = "\033[95m"
COL_BRIGHT_CYAN = "\033[96m"
COL_BRIGHT_WHITE = "\033[97m"


class Formatter(logging.Formatter):
    # force UTC timestamps
    converter = time.gmtime

    def format(self, record: logging.LogRecord):
        # thread ID and name
        if record.threadName and LOG_CONFIG["always_include_thread_id"]:
            record.threadName = f"{record.thread} {record.threadName}"
        elif not record.threadName:
            record.threadName = f"{record.thread}"

        if record.levelname == "CRITICAL":
            record.levelname = "FATAL"

        message = super().format(record)

        if isinstance(LOG_CONFIG["file"], io.TextIOWrapper):
            LOG_CONFIG["file"].write(message + "\n")

        # terminal colors
        if LOG_CONFIG["colorize"]:
            color = COL_RESET
            if record.levelno >= logging.FATAL:
                color = COL_BRIGHT_RED
            elif record.levelno >= logging.ERROR:
                color = COL_RED
            elif record.levelno >= logging.WARNING:
                color = COL_YELLOW
            elif record.levelno < logging.INFO:
                color = COL_CYAN
            return f"{color}{message}{COL_RESET}"
        else:
            return message


log_formatter = Formatter(
    "\n{levelname[0]} | {asctime} | {threadName} | {name}\n{message}",
    datefmt="%Y-%m-%d %H:%M:%S UTC",
    style="{"
)
log_handler = logging.StreamHandler(sys.stdout)
log_handler.setFormatter(log_formatter)


def make_logger(name: str, level: int | None = None) -> logging.Logger:
    if level is None:
        level = LOG_CONFIG["level"]
    l = logging.Logger(name, level)
    l.addHandler(log_handler)
    return l


logger = make_logger("root")


def encode_str_len(s: str) -> bytes:
    """
    returns a bytes object with two bytes for the length of the string followed
    by the actual string with UTF-8 encoding.
    """
    b = s.encode()
    if len(b) >= 2**16:
        raise ValueError(
            f"tried to encode a ginormous string ({len(b)} bytes in UTF-8)"
        )
    return len(b).to_bytes(2) + b


def encode_float32(v: float) -> bytes:
    return struct.pack('>f', v)


def decode_float32(b: bytes) -> float:
    if len(b) != 4:
        raise BufferError(
            f"need exactly 4 bytes to decode a float32 (got {len(b)} bytes)"
        )
    return float(struct.unpack('>f', b)[0])


def close_socket(sock: socket.socket):
    """
    close a socket, ignoring exceptions.
    """
    try:
        sock.close()
    except Exception:
        pass


def is_ready_to_read(sock: socket.socket, timeout: float = 0.) -> bool:
    """
    check if a socket is ready to read immediately without blocking.
    returns True if recv() would return or raise an exception immediately, False
    if it would block the current thread.
    """
    try:
        readable, _, _ = select.select([sock], [], [], timeout)
        return bool(readable)
    except (ValueError, OSError, socket.error):
        # socket is closed, invalid, or not connected
        return True


def recv_exact(sock: socket.socket, n: int) -> bytes:
    """read exactly n bytes from a socket, raising EOFError on early close."""
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError(
                "connection closed before enough data was received"
            )
        buf += chunk
    return buf


def format_exception(e: Exception) -> str:
    if (isinstance(e, int) or isinstance(e, str) or isinstance(e, bool)
            or isinstance(e, tuple)
            or isinstance(e, list)
            or isinstance(e, dict)):
        return str(e)
    s = str(e)
    if s and type(e) is Exception:
        return s
    elif s:
        return f"{e.__class__.__name__}: {s}"
    else:
        return e.__class__.__name__


def format_addr(addr: object) -> str:
    if isinstance(addr, tuple) \
            and len(addr) == 2 \
            and isinstance(addr[0], str) \
            and isinstance(addr[1], int):
        return f"{addr[0]}:{addr[1]}"
    return str(addr)


def format_bytes(b: bytes) -> str:
    return b.hex('|').upper()


def goofy_handshake_solve(question: bytes) -> tuple[bytes, int]:
    if len(question) < 8:
        raise ValueError("goofy handshake question too small")

    answer = bytearray(question)
    for i in range(len(answer)):
        answer[i] = (answer[i] * 937) % 256
        if i % 8 == 4:
            answer[i] = (answer[i - 1] * 7) % 256
        if i % 5 == 3:
            if answer[i] in range(100, 210):
                answer[i] //= 11
            answer[i] = (answer[i] + 193) % 256
        if i % 3 > 0:
            answer[i] = (answer[i] - 13) % 256
        if i % 9 == 6:
            answer[i] = (answer[i] + answer[i - 5]) % 256

    # trim or pad
    new_length = len(answer) + (answer[5] % 6 - 3)
    if new_length > len(answer):
        for i in range(len(answer), new_length):
            answer.append(
                (answer[(i * 111973) % len(answer)] + 3373) % 256
            )
    else:
        answer = answer[:new_length]

    welcome_byte = (answer[answer[2] % len(answer)] * 37 + 14) % 256
    if welcome_byte < 1:
        welcome_byte = 240

    return (bytes(answer), welcome_byte)


def force_acquire(lock: threading.Lock):
    while True:
        if lock.acquire():
            return


SOCKS5_REP_GENERAL_FAILURE = 1
SOCKS5_REP_HOST_UNREACHABLE = 4
SOCKS5_REP_CONN_REFUSED = 5


class GoofySocketStatus(IntEnum):
    WaitingToOpen = 0
    FailedToOpenHostUnreachable = 1
    FailedToOpenConnRefused = 2
    FailedToOpenGeneral = 3
    Open = 4
    Closed = 5

    def failure_to_socks_reply(self) -> tuple[int, str]:
        """
        convert failure type to SOCKS5 reply code, returns -1 if not a failure.
        also returns the name of the error as a string.
        """
        if self == self.FailedToOpenHostUnreachable:
            return (SOCKS5_REP_HOST_UNREACHABLE, "HOST_UNREACHABLE")
        elif self == self.FailedToOpenConnRefused:
            return (SOCKS5_REP_CONN_REFUSED, "CONN_REFUSED")
        elif self == self.FailedToOpenGeneral:
            return (SOCKS5_REP_GENERAL_FAILURE, "GENERAL_FAILURE")
        return (-1, "not a failure")


class GoofyPacket:
    @classmethod
    def _packet_type(cls) -> int:
        """
        abstract function returning a unique 8-bit integer from 0 to 255
        defining the packet type for each derived class.
        """
        raise NotImplementedError()

    def _to_bytes(self) -> bytes:
        """
        abstract function for converting the packet's data to bytes. the first
        byte must be the packet type.
        """
        raise NotImplementedError()

    @classmethod
    def packet_type(cls) -> int:
        i = cls._packet_type()
        if i not in range(256):
            raise ValueError("packet type must be in the [0-255] range")
        return i

    def to_bytes(self) -> bytes:
        b = self._to_bytes()
        if len(b) < 1:
            raise ValueError(
                "_to_bytes() from derived class returned empty buffer"
            )
        if b[0] != self.packet_type():
            raise ValueError(
                "_to_bytes() from derived class must contain the "
                "packet type in the first byte"
            )
        return b

    def send(self, io: GoofyIo):
        io.send(self.to_bytes())


@dataclass
class GoofyCommandSetLimits(GoofyPacket):
    """
    goofy packet sent by the goofy client commanding the server to update limits
    such as timeouts and the relay buffer size.
    """

    buf_size: int
    timeout: float
    bind_timeout: float
    udp_timeout: float

    @classmethod
    def _packet_type(cls) -> int:
        return 10

    def _to_bytes(self) -> bytes:
        return self._packet_type().to_bytes(1) \
            + self.buf_size.to_bytes(4) \
            + encode_float32(self.timeout) \
            + encode_float32(self.bind_timeout) \
            + encode_float32(self.udp_timeout)


@dataclass
class GoofyCommandOpenSocket(GoofyPacket):
    """
    goofy packet sent by the goofy client commanding the server to open a TCP
    socket with given ID and connect to given address.
    """

    socket_id_u32: int
    dst_host: str
    dst_port: int

    @classmethod
    def _packet_type(cls) -> int:
        return 11

    def _to_bytes(self) -> bytes:
        return self._packet_type().to_bytes(1) \
            + self.socket_id_u32.to_bytes(4) \
            + encode_str_len(self.dst_host) \
            + self.dst_port.to_bytes(2)


@dataclass
class GoofyCommandBind(GoofyPacket):
    """
    goofy packet sent by the goofy client commanding the server to bind a
    listening socket on a random port, send us information about the bind
    host and port, and inform us when a remote peer (inbound) connects.
    """

    socket_id_u32: int

    @classmethod
    def _packet_type(cls) -> int:
        return 12

    def _to_bytes(self) -> bytes:
        return self._packet_type().to_bytes(1) \
            + self.socket_id_u32.to_bytes(4)


@dataclass
class GoofyCommandCloseSocket(GoofyPacket):
    """
    goofy packet commanding the other side to close socket with given ID.
    """

    socket_id_u32: int

    @classmethod
    def _packet_type(cls) -> int:
        return 13

    def _to_bytes(self) -> bytes:
        return self._packet_type().to_bytes(1) \
            + self.socket_id_u32.to_bytes(4)


@dataclass
class GoofyCommandOpenUdpRelay(GoofyPacket):
    """
    goofy packet sent by the goofy client commanding the server to start a UDP
    relay thread with given relay ID.
    """

    udp_relay_id_u16: int

    @classmethod
    def _packet_type(cls) -> int:
        return 14

    def _to_bytes(self) -> bytes:
        return self._packet_type().to_bytes(1) \
            + self.udp_relay_id_u16.to_bytes(2)


@dataclass
class GoofySocketIoPacket(GoofyPacket):
    """
    goofy packet containing raw data for socket with given ID. supports up to
    65535 bytes.
    """

    socket_id_u32: int
    data: bytes

    @classmethod
    def _packet_type(cls) -> int:
        return 20

    def _to_bytes(self) -> bytes:
        return self._packet_type().to_bytes(1) \
            + self.socket_id_u32.to_bytes(4) \
            + len(self.data).to_bytes(2) \
            + self.data


@dataclass
class GoofySocketIoSmallPacket(GoofyPacket):
    """
    goofy packet containing raw data for socket with given ID. supports up to
    255 bytes.
    """

    socket_id_u32: int
    data: bytes

    @classmethod
    def _packet_type(cls) -> int:
        return 21

    def _to_bytes(self) -> bytes:
        return self._packet_type().to_bytes(1) \
            + self.socket_id_u32.to_bytes(4) \
            + len(self.data).to_bytes(1) \
            + self.data


def make_goofy_socket_io_packet(socket_id: int, data: bytes) -> GoofyPacket:
    """
    make a small or normal socket IO packet based on the data size.
    """
    if len(data) < 256:
        return GoofySocketIoSmallPacket(socket_id, data)
    elif len(data) < 65536:
        return GoofySocketIoPacket(socket_id, data)
    raise ValueError(
        "data is too large for a goofy socket IO packet. try lowering buf_size."
    )


@dataclass
class GoofyUdpPacket(GoofyPacket):
    """
    goofy packet representing a UDP packet.
    """

    udp_relay_id_u16: int
    host: str
    port: int
    payload: bytes

    @classmethod
    def _packet_type(cls) -> int:
        return 22

    def _to_bytes(self) -> bytes:
        return self._packet_type().to_bytes(1) \
            + self.udp_relay_id_u16.to_bytes(2) \
            + encode_str_len(self.host) \
            + self.port.to_bytes(2) \
            + len(self.payload).to_bytes(2) \
            + self.payload


@dataclass
class GoofyEventSocketStatus(GoofyPacket):
    """
    goofy packet sent by the goofy server informing the client about the new
    status of socket with given ID.
    """

    socket_id_u32: int
    new_status: GoofySocketStatus

    @classmethod
    def _packet_type(cls) -> int:
        return 30

    def _to_bytes(self) -> bytes:
        return self._packet_type().to_bytes(1) \
            + self.socket_id_u32.to_bytes(4) \
            + self.new_status.to_bytes(1)


@dataclass
class GoofyEventSocketBindInfo(GoofyPacket):
    """
    goofy packet sent by the goofy server informing the client about the new
    status, bind host, and bind port of socket with given ID.
    """

    socket_id_u32: int
    new_status: GoofySocketStatus
    bind_host: str
    bind_port: int

    @classmethod
    def _packet_type(cls) -> int:
        return 31

    def _to_bytes(self) -> bytes:
        return self._packet_type().to_bytes(1) \
            + self.socket_id_u32.to_bytes(4) \
            + self.new_status.to_bytes(1) \
            + encode_str_len(self.bind_host) \
            + self.bind_port.to_bytes(2)


@dataclass
class GoofyEventSocketInboundInfo(GoofyPacket):
    """
    goofy packet sent by the goofy server informing the client about the new
    status and inbound peer address of socket with given ID.
    """

    socket_id_u32: int
    new_status: GoofySocketStatus
    inbound_host: str
    inbound_port: int

    @classmethod
    def _packet_type(cls) -> int:
        return 32

    def _to_bytes(self) -> bytes:
        return self._packet_type().to_bytes(1) \
            + self.socket_id_u32.to_bytes(4) \
            + self.new_status.to_bytes(1) \
            + encode_str_len(self.inbound_host) \
            + self.inbound_port.to_bytes(2)


@dataclass
class GoofyEventUdpRelayClosed(GoofyPacket):
    """
    goofy packet sent by the goofy server letting the client know that UDP relay
    with given ID was closed for some reason.
    """

    udp_relay_id_u16: int

    @classmethod
    def _packet_type(cls) -> int:
        return 33

    def _to_bytes(self) -> bytes:
        return self._packet_type().to_bytes(1) \
            + self.udp_relay_id_u16.to_bytes(2)


def receive_goofy_packet(io: GoofyIo) -> GoofyPacket:
    packet_type = io.receive(1)[0]
    if packet_type == GoofyCommandSetLimits.packet_type():
        buf = io.receive(16)
        relay_buf_size = int.from_bytes(buf[:4])
        timeout = decode_float32(buf[4:8])
        bind_timeout = decode_float32(buf[8:12])
        udp_timeout = decode_float32(buf[12:16])
        return GoofyCommandSetLimits(
            relay_buf_size,
            timeout,
            bind_timeout,
            udp_timeout
        )
    elif packet_type == GoofyCommandOpenSocket.packet_type():
        buf = io.receive(6)
        socket_id = int.from_bytes(buf[:4])
        dst_host_len = int.from_bytes(buf[4:])

        buf = io.receive(dst_host_len + 2)
        dst_host = buf[:dst_host_len].decode()
        dst_port = int.from_bytes(buf[-2:])

        return GoofyCommandOpenSocket(socket_id, dst_host, dst_port)
    elif packet_type == GoofyCommandBind.packet_type():
        socket_id = int.from_bytes(io.receive(4))
        return GoofyCommandBind(socket_id)
    elif packet_type == GoofyCommandCloseSocket.packet_type():
        socket_id = int.from_bytes(io.receive(4))
        return GoofyCommandCloseSocket(socket_id)
    elif packet_type == GoofyCommandOpenUdpRelay.packet_type():
        udp_relay_id = int.from_bytes(io.receive(2))
        return GoofyCommandOpenUdpRelay(udp_relay_id)
    elif packet_type == GoofySocketIoPacket.packet_type():
        buf = io.receive(6)
        socket_id = int.from_bytes(buf[:4])
        data_len = int.from_bytes(buf[4:])
        data = io.receive(data_len)
        return GoofySocketIoPacket(socket_id, data)
    elif packet_type == GoofySocketIoSmallPacket.packet_type():
        buf = io.receive(5)
        socket_id = int.from_bytes(buf[:4])
        data_len = buf[4]
        data = io.receive(data_len)
        return GoofySocketIoSmallPacket(socket_id, data)
    elif packet_type == GoofyUdpPacket.packet_type():
        buf = io.receive(4)
        udp_relay_id = int.from_bytes(buf[:2])
        host_len = int.from_bytes(buf[2:])

        buf = io.receive(host_len + 4)
        host = buf[:host_len].decode()
        port = int.from_bytes(buf[-4:-2])
        payload_len = int.from_bytes(buf[-2:])

        payload = io.receive(payload_len)

        return GoofyUdpPacket(
            udp_relay_id,
            host,
            port,
            payload
        )
    elif packet_type == GoofyEventSocketStatus.packet_type():
        buf = io.receive(5)
        socket_id = int.from_bytes(buf[:4])
        new_status = int.from_bytes(buf[4:])
        return GoofyEventSocketStatus(
            socket_id,
            GoofySocketStatus(new_status)
        )
    elif packet_type == GoofyEventSocketBindInfo.packet_type():
        buf = io.receive(7)
        socket_id = int.from_bytes(buf[:4])
        new_status = int.from_bytes(buf[4:5])
        bind_host_len = int.from_bytes(buf[5:])

        buf = io.receive(bind_host_len + 2)
        bind_host = buf[:bind_host_len].decode()
        bind_port = int.from_bytes(buf[-2:])

        return GoofyEventSocketBindInfo(
            socket_id,
            GoofySocketStatus(new_status),
            bind_host,
            bind_port
        )
    elif packet_type == GoofyEventSocketInboundInfo.packet_type():
        buf = io.receive(7)
        socket_id = int.from_bytes(buf[:4])
        new_status = int.from_bytes(buf[4:5])
        inbound_host_len = int.from_bytes(buf[5:])

        buf = io.receive(inbound_host_len + 2)
        inbound_host = buf[:inbound_host_len].decode()
        inbound_port = int.from_bytes(buf[-2:])

        return GoofyEventSocketInboundInfo(
            socket_id,
            GoofySocketStatus(new_status),
            inbound_host,
            inbound_port
        )
    elif packet_type == GoofyEventUdpRelayClosed.packet_type():
        udp_relay_id = int.from_bytes(io.receive(2))
        return GoofyEventUdpRelayClosed(udp_relay_id)

    raise ValueError(f"unsupported goofy packet type ({packet_type})")
