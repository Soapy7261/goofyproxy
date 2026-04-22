"""
provides `VideoIo`, a `GoofyIo` child class for data transfer through video
calls.
"""

import sys
import time
import threading
import getpass
import random
from enum import IntEnum
from typing import NamedTuple

import numpy as np
from scipy.spatial.distance import cdist
import zlib
import gzip

from PySide6.QtWidgets import QApplication, QMainWindow, QLabel
from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QImage, QPixmap, QPainter, QBrush, QColor

import mss
from mss.base import MSSBase
from mss.models import Monitor

from PIL import Image
import qrcode
import qrcode.constants
from pyzbar import pyzbar
import cv2

from goofyio import GoofyIo
from common import *

VIDEOIO_VERSION = 1
VIDEOIO_MIN_PEER_VERSION = 1
assert VIDEOIO_MIN_PEER_VERSION <= VIDEOIO_VERSION

# remove older outgoing packets that are already transmitted until we go below
# the memory limit or reach the minimum outgoing packet count, because we need
# to keep the last few packets in case the other side asks for a retransmission.
OUT_PACKETS_MEMORY_LIMIT = 512 * 1024 * 1024
OUT_PACKETS_MIN_COUNT = 16  # must be bigger than 1

QR_BORDER_FACTOR = .15

HANDSHAKE_CORNER_DOT_COLOR = np.asarray([1, 0, 0], dtype=np.float32)
HANDSHAKE_CORNER_DOT_SIZE = 5


"""
packet header (16 bytes):
[2 bytes] header checksum
[4 bytes] retransmission request packet index (FFFFFFFF if none)
[4 bytes] packet index
[2 bytes] packet data checksum (or plus 1 to mark data as compressed)
[4 bytes] packet data length in bytes
"""

PACKET_HEADER_BYTES: int = 16


class Packet:
    retransmission_req_idx: int
    idx: int
    data: bytes
    is_compressed: bool

    def __init__(
        self,
        retransmission_req_idx: int | None,
        idx: int,
        data: bytes,
        is_compressed: bool
    ):
        self.retransmission_req_idx = retransmission_req_idx
        self.idx = idx
        self.data = data
        self.is_compressed = is_compressed

    def to_bytes(self, format: Format) -> bytes:
        if self.retransmission_req_idx is None \
                or self.retransmission_req_idx < 0:
            retran_req = 2**32 - 1
        else:
            retran_req = int(self.retransmission_req_idx)

        if len(self.data) > format.data_bytes_per_frame:
            raise ValueError(
                f"data is too large ({self.data} bytes) for the format "
                f"({format})"
            )

        data_checksum = compute_checksum(self.data)

        # if the data is compressed, increment data checksum by one. this way we
        # avoid wasting more bits for the header.
        if self.is_compressed:
            data_checksum = (data_checksum + 1) % 2**16

        header = \
            retran_req.to_bytes(4) \
            + self.idx.to_bytes(4) \
            + data_checksum.to_bytes(2) \
            + len(self.data).to_bytes(4)

        header_checksum = compute_checksum(header)
        header = header_checksum.to_bytes(2) + header

        n_trailing_zeros = format.data_bytes_per_frame - len(self.data)
        trailing_zeros = b"\0" * n_trailing_zeros

        return header + self.data + trailing_zeros


class Format:
    """
    color grid format used to encode data.

    Arguments:

      width (int): total width

      height (int): total height

      cell_size (int): width and height of individual cells

      bits_per_cell (int): how many bits to encode in each cell. the total
          number of possible colors will be 2 to the power of this number (e.g.
          16 possible colors for 4 bits).

      rate (float): how many times per second to update the colors
    """

    width: int
    height: int
    cell_size: int
    bits_per_cell: int
    rate: float

    res_x: int
    res_y: int

    bytes_per_frame: int
    data_bytes_per_frame: int

    def __init__(
        self,
        width: int,
        height: int,
        cell_size: int,
        bits_per_cell: int,
        rate: float
    ):
        self.width = int(width)
        self.height = int(height)
        self.cell_size = int(cell_size)
        self.bits_per_cell = int(bits_per_cell)
        self.rate = round(float(rate), 1)

        if self.width < 1 or self.height < 1:
            raise ValueError(
                f"width={self.width} or height={self.height} is smaller than 1"
            )
        if self.cell_size < 1:
            raise ValueError(
                f"cell_size={self.cell_size} is smaller than 1"
            )
        if self.bits_per_cell not in range(1, 7):
            raise ValueError(
                f"bits_per_cell={self.bits_per_cell} is not in the [1, 6] range"
            )
        if self.rate < .1:
            raise ValueError(f"rate={self.rate} is too low")

        self.res_x = width // cell_size
        self.res_y = height // cell_size

        self.bytes_per_frame = self.res_x * self.res_y * self.bits_per_cell // 8
        self.data_bytes_per_frame = self.bytes_per_frame - PACKET_HEADER_BYTES

        if self.data_bytes_per_frame < 1:
            raise ValueError(
                f"grid resolution is too low to contain real data "
                f"(data_bytes_per_frame={self.data_bytes_per_frame})."
            )

    def data_rate(self) -> float:
        """
        compute how many bytes can be encoded per second using this format.
        """
        return self.data_bytes_per_frame * self.rate

    def __str__(self) -> str:
        """
        convert to a string in the following format:

        `{width}x{height}-{cell_size}-{bits_per_cell}@{rate}`

        example: `720x480-8-4@10.0`.
        """
        return \
            f"{self.width}x{self.height}-{self.cell_size}-" \
            f"{self.bits_per_cell}@{self.rate:.1f}"

    def create(f: str | Format) -> Format:
        """
        if `f` is a Format, returns `f`, if it's a `str`, returns a Format
        converted from the string in the following format, raises TypeError
        otherwise.

        `{width}x{height}-{cell_size}-{bits_per_cell}@{rate}`

        example: `720x480-8-4@10.0`.
        """
        if isinstance(f, str):
            try:
                wh, cell_size, f = f.strip().split("-")
                w, h = wh.split("x")
                bits_per_cell, rate = f.split("@")
                return Format(
                    int(w), int(h), int(cell_size), int(bits_per_cell),
                    float(rate)
                )
            except Exception as e:
                raise ValueError(
                    f"failed to decode format: {format_exception(e)}"
                )
        elif isinstance(f, Format):
            return f
        else:
            raise TypeError("invalid format")


class Aabb(NamedTuple):
    """
    2D axis-aligned bounding box
    """
    top_left: tuple[float, float]
    bottom_right: tuple[float, float]


COLOR_PALETTES = {
    # 1 bit: black and white
    1: np.asarray([[0, 0, 0], [255, 255, 255]], dtype=np.uint8),

    # 2-bit: 4 colors
    2: np.asarray([[0, 0, 0], [0, 255, 0], [255, 255, 255], [255, 0, 255]], dtype=np.uint8),

    # 3-bit: 8 colors
    3: np.asarray([[0, 0, 0], [0, 0, 255], [0, 255, 255], [0, 255, 0], [255, 255, 0], [255, 255, 255], [255, 0, 255], [255, 0, 0]], dtype=np.uint8),

    # 4-bit: 16 colors
    4: np.asarray([[0, 0, 0], [0, 0, 127], [0, 0, 255], [0, 127, 255], [0, 255, 255], [0, 255, 127], [0, 255, 0], [127, 255, 0], [255, 255, 0], [255, 255, 127], [255, 255, 255], [255, 127, 255], [255, 0, 255], [255, 0, 127], [255, 0, 0], [127, 127, 127]], dtype=np.uint8),

    # 5-bit: 32 colors
    5: np.asarray([[131, 86, 51], [64, 175, 104], [197, 27, 157], [31, 116, 211], [164, 205, 8], [97, 57, 61], [231, 145, 115], [14, 234, 168], [147, 7, 221], [81, 96, 19], [214, 185, 72], [47, 37, 125], [181, 126, 179], [114, 214, 232], [247, 66, 29], [6, 155, 83], [139, 244, 136], [72, 17, 189], [206, 106, 243], [39, 195, 40], [172, 47, 93], [106, 136, 147], [239, 224, 200], [22, 76, 253], [156, 165, 0], [89, 254, 53], [222, 1, 106], [56, 89, 159], [189, 178, 213], [122, 30, 10], [255, 119, 63], [2, 208, 117]], dtype=np.uint8),

    # 6-bit: 64 colors
    6: np.asarray([[129, 85, 50], [63, 172, 102], [194, 27, 155], [30, 114, 207], [161, 201, 8], [96, 56, 60], [227, 143, 113], [14, 230, 165], [145, 7, 218], [79, 95, 18], [210, 182, 71], [47, 36, 123], [178, 124, 176], [112, 211, 228], [243, 65, 29], [6, 153, 81], [137, 240, 134], [71, 17, 186], [202, 104, 239], [38, 192, 39], [169, 46, 92], [104, 133, 144], [235, 221, 197], [22, 75, 249], [153, 163, 0], [88, 250, 52], [219, 1, 104], [55, 88, 157], [186, 175, 209], [120, 30, 10], [251, 117, 62], [2, 205, 115], [133, 59, 167], [67, 146, 220], [198, 234, 21], [34, 10, 73], [165, 98, 125], [100, 185, 178], [231, 40, 230], [18, 127, 31], [149, 214, 83], [83, 69, 136], [215, 156, 188], [51, 243, 241], [182, 20, 41], [116, 107, 94], [247, 195, 146], [10, 49, 199], [141, 137, 251], [75, 224, 2], [206, 78, 54], [43, 166, 107], [174, 253, 159], [108, 4, 211], [239, 91, 12], [26, 179, 65], [157, 33, 117], [92, 120, 169], [223, 208, 222], [59, 62, 23], [190, 150, 75], [124, 237, 127], [255, 14, 180], [0, 101, 232]], dtype=np.uint8),
}


class HandshakeStage(IntEnum):
    ShowingQr = 0
    LookingForPeerQr = 1
    ShowingAck = 2
    WaitingForAck = 3
    Done = 4


class VideoIo(GoofyIo):
    """
    a `GoofyIo` that transfers data through video calls. data is sent by showing
    a grid of colorful squares to the other side and received by capturing the
    other side's video feed. QR code is used for the handshake process at the
    beginning.

    once a `VideoIo` is created, an empty window will appear. the user has time
    to move it around if needed, then you must call `start()` to start the
    handshake process. the window must not be moved around on the screen after
    that point.

    Args:

        out_format (str | Format):
            output grid format represented as
            `{width}x{height}-{cell_size}-{bits_per_cell}@{rate}`.
            example: `720x480-16-2@5`

        in_monitor_idx (int = 0):
            index of the monitor on which the peer's video feed is visible. you
            can use static function `get_monitors()` to get the list of
            available monitors.

        sender_id (str | None = None):
            sender ID to use for the handshake process. if `None`, one will be
            generated.

        peer_id (str | None = None):
            sender ID of the peer we're looking for in the handshake process.
            if `None`, the first detected peer will be chosen.

        screenshot_speed (float = 2.):
            the receive thread will take a screenshot and read the peer's video
            feed this many times for every "frame" (1 / peer_format.rate). it
            may be helpful to use a higher value for this in certain cases where
            the frame rate of the peer's format is low (e.g. rate <= 2) while
            the cells are small and detailed, because video compression usually
            improves the image quality if the image stays still for some time
            (so by taking more screenshots we effectively wait for the image
            quality to improve so we can read the data without corruption).

        corrupt_packet_threshold (int = 4):
            if we get more than this many corrupt packets (e.g. index too far
            ahead or checksum unverified), we'll ask the other side to start
            retransmitting from the last packet index we properly received.
    """

    _log: logging.Logger

    out_format: Format

    _in_monitor_idx: int

    _in_palette: np.ndarray | None = None
    _out_palette: np.ndarray | None = None

    _out_buf: bytearray
    _out_buf_lock: threading.Lock

    _out_packets: list[Packet]
    _out_packet_idx_offs: int = 0
    _out_packet_idx: int = 0
    _out_packets_lock: threading.Lock

    _last_retran_req_time: float = 0.
    _last_retran_req_idx: int = -1

    _out_pixels: np.ndarray

    _in_valid_packet_idx: int = 0
    _in_buf: bytearray
    _in_buf_lock: threading.Lock

    _n_corrupt_receives: int = False
    _request_retransmission: bool = False

    _started: bool = False
    _stopping: bool = False

    _send_thread: threading.Thread
    _receive_thread: threading.Thread

    _app: QApplication
    _window: QMainWindow
    _label: QLabel
    _timer: QTimer

    _sct: MSSBase | None = None
    _monitor: Monitor | None = None

    _sender_id: str = ""
    _handshake_stage = HandshakeStage.ShowingQr

    _peer_sender_id: str | None = None
    _peer_format: Format | None = None
    _peer_aabb: Aabb | None = None

    _ignored_peers: list[str]

    _screenshot_speed: float
    _corrupt_packet_threshold: int

    def __init__(
        self,
        out_format: str | Format,
        in_monitor_idx: int = 0,
        sender_id: str | None = None,
        peer_id: str | None = None,
        screenshot_speed: float = 2.,
        corrupt_packet_threshold: int = 4
    ):
        self._log = make_logger(f"VideoIo")

        self.out_format = Format.create(out_format)
        self._in_monitor_idx = int(in_monitor_idx)
        self._peer_sender_id = peer_id

        self._out_palette = COLOR_PALETTES[self.out_format.bits_per_cell]
        self._ignored_peers = []

        self._screenshot_speed = screenshot_speed
        self._corrupt_packet_threshold = corrupt_packet_threshold

        self._log.info(
            f"output format: {self.out_format} "
            f"({format_data_rate(self.out_format.data_rate())})"
        )

        self._out_packets = []
        self._out_packets_lock = threading.Lock()
        self._out_pixels = np.zeros(
            (self.out_format.res_y, self.out_format.res_x, 3),
            np.uint8
        )

        self._out_buf = bytearray()
        self._out_buf_lock = threading.Lock()
        self._in_buf = bytearray()
        self._in_buf_lock = threading.Lock()

        try:
            if isinstance(sender_id, str):
                self._sender_id = sender_id
            else:
                self._sender_id = \
                    getpass.getuser().strip() + f"-{random.randint(0, 999)}"
            self._sender_id = \
                self._sender_id.strip().replace(" ", "-").replace("#", "-")
        except OSError:
            pass
        if not self._sender_id:
            self._sender_id = f"unknown-{random.randint(0, 999)}"
        self._log.info(f"using sender ID \"{self._sender_id}\"")

        if isinstance(self._peer_sender_id, str):
            if self._peer_sender_id == sender_id:
                raise ValueError("peer ID is the same as our own sender ID")
        elif self._peer_sender_id is not None:
            raise TypeError("peer_id is neither a str or None")

        self._send_thread = threading.Thread(
            name="VideoIo send thread",
            target=self._send_thread_run,
            args=(),
            daemon=True
        )
        self._send_thread.start()

        self._receive_thread = threading.Thread(
            name="VideoIo receive thread",
            target=self._receive_thread_run,
            args=(),
            daemon=True
        )
        self._receive_thread.start()

    def start(self):
        if self._stopping:
            raise RuntimeError("can't continue running after stopping")
        self._started = True

    def running(self) -> bool:
        return self._started and not self._stopping

    def stop(self):
        self._stopping = True

    def get_monitors() -> list[Monitor]:
        sct = mss.mss()
        mons = sct.monitors[1:]
        sct.close()
        return mons

    def _receive(self, size: int) -> bytes:
        if not self.running():
            raise ConnectionError("not running")

        while True:
            if self._peer_format:
                poll_interval = .5 / self._peer_format.rate
            else:
                poll_interval = .1

            if not self._in_buf_lock.acquire():
                time.sleep(poll_interval)
                continue

            if len(self._in_buf) < size:
                self._in_buf_lock.release()
                time.sleep(poll_interval)
                continue

            data = bytes(self._in_buf[:size])
            self._in_buf = self._in_buf[size:]

            self._in_buf_lock.release()
            return data

    def _send(self, data: bytes):
        if not self.running():
            raise ConnectionError("not running")

        force_acquire(self._out_buf_lock)
        self._out_buf += data
        self._out_buf_lock.release()

    def _send_thread_run(self):
        try:
            self._app = QApplication(sys.argv)

            self._window = QMainWindow()
            self._window.setWindowTitle(f"VideoIo - {self._sender_id}")
            self._window.setFixedSize(
                self.out_format.width / self._window.devicePixelRatio(),
                self.out_format.height / self._window.devicePixelRatio()
            )
            self._window.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint)

            # show warning for non-integer display scaling
            pix_ratio = self._window.devicePixelRatio()
            if abs(pix_ratio - round(pix_ratio)) > .0001:
                self._log.warning(
                    f"non-integer display scaling detected (x{pix_ratio}). "
                    "precision may be reduced."
                )

            self._label = QLabel()
            self._label.setScaledContents(False)
            self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._window.setCentralWidget(self._label)

            self._timer = QTimer()
            self._timer.timeout.connect(self._update_image)
            self._timer.start(1000. / self.out_format.rate)

            self._window.show()
            self._app.exec()
        except BaseException as e:
            self._log.fatal(format_exception(e))
        finally:
            self._stopping = True
            try:
                self._timer.stop()
                self._window.destroy()
                self._app.quit()
            except:
                pass

    def _update_image(self):
        if self._stopping:
            self._timer.stop()
            self._window.destroy()
            self._app.quit()
            return

        if not self._started:
            return

        try:
            # show hello QR code
            if self._handshake_stage == HandshakeStage.ShowingQr:
                self._set_image(
                    generate_qr(
                        f"VideoIo-{VIDEOIO_VERSION}#{self._sender_id}"
                        f"#{self.out_format}#hello"
                    ),
                    put_corner_dots_for_handshake=True
                )

                self._log.info(
                    "looking for a peer" if self._peer_sender_id is None
                    else f"looking for peer \"{self._peer_sender_id}\""
                )
                self._handshake_stage = HandshakeStage.LookingForPeerQr
                return

            # wait for the receive thread to detect the peer's QR code
            if self._handshake_stage == HandshakeStage.LookingForPeerQr:
                return

            # show acknowledgement QR code
            if self._handshake_stage == HandshakeStage.ShowingAck:
                self._set_image(
                    generate_qr(
                        f"VideoIo-{VIDEOIO_VERSION}#{self._sender_id}"
                        f"#{self.out_format}#ack#{self._peer_sender_id}"
                    ),
                    put_corner_dots_for_handshake=True
                )

                self._log.info(
                    "waiting for the peer's acknowledgement QR code")
                self._handshake_stage = HandshakeStage.WaitingForAck
                return

            # wait for the receive thread to detect the peer's acknowledgement
            if self._handshake_stage == HandshakeStage.WaitingForAck:
                return

            if self._handshake_stage != HandshakeStage.Done:
                raise ValueError("unknown handshake stage")

            force_acquire(self._out_buf_lock)

            # use compression if it's worth it
            data_orig_size = self.out_format.data_bytes_per_frame
            data = bytes(self._out_buf[:data_orig_size])
            is_compressed = False
            if len(self._out_buf) > self.out_format.data_bytes_per_frame:
                orig_size = self.out_format.data_bytes_per_frame * 3
                temp = gzip.compress(self._out_buf[:orig_size])
                if len(temp) < self.out_format.data_bytes_per_frame:
                    data_orig_size = orig_size
                    data = temp
                    is_compressed = True

                if not is_compressed:
                    orig_size = self.out_format.data_bytes_per_frame * 2
                    temp = gzip.compress(self._out_buf[:orig_size])
                    if len(temp) < self.out_format.data_bytes_per_frame:
                        data_orig_size = orig_size
                        data = temp
                        is_compressed = True

                if not is_compressed:
                    orig_size = self.out_format.data_bytes_per_frame * 3 // 2
                    temp = gzip.compress(self._out_buf[:orig_size])
                    if len(temp) < self.out_format.data_bytes_per_frame:
                        data_orig_size = orig_size
                        data = temp
                        is_compressed = True
            self._out_buf = \
                self._out_buf[data_orig_size:]

            self._out_buf_lock.release()

            if not data and not self._request_retransmission:
                return

            retran_req_idx = None
            if self._request_retransmission:
                self._request_retransmission = False
                retran_req_idx = self._in_valid_packet_idx
                self._log.warning(
                    f"too many corrupt packets, sending a retransmission "
                    f"request with index {retran_req_idx}."
                )

            force_acquire(self._out_packets_lock)

            self._out_packets.append(Packet(
                retran_req_idx,
                len(self._out_packets) + self._out_packet_idx_offs,
                data,
                is_compressed
            ))

            self._out_packet_idx = min(
                max(self._out_packet_idx, self._out_packet_idx_offs),
                len(self._out_packets) + self._out_packet_idx_offs - 1
            )
            curr_packet = self._out_packets[
                self._out_packet_idx - self._out_packet_idx_offs
            ]
            self._out_packet_idx += 1

            # clean up old outgoing packets that are already transmitted.
            total_size = 0
            for packet in self._out_packets:
                total_size += len(packet.data)
            while total_size > OUT_PACKETS_MEMORY_LIMIT \
                    and len(self._out_packets) > OUT_PACKETS_MIN_COUNT:
                total_size -= len(self._out_packets[0].data)
                self._out_packets.pop(0)
                self._out_packet_idx_offs += 1

            self._out_packets_lock.release()

            # update the colors

            bits = unpack_bits(curr_packet.to_bytes(self.out_format))
            values = pack_bits(bits, self.out_format.bits_per_cell)

            n_cells = self.out_format.res_x * self.out_format.res_y
            if values.size < n_cells:
                values = np.pad(
                    values,
                    (0, n_cells - values.size),
                    constant_values=[0]
                )

            self._out_pixels = self._out_palette[values].reshape(
                (self.out_format.res_y, self.out_format.res_x, 3)
            )

            self._set_image(self._out_pixels, False, False)
        except BaseException as e:
            self._log.fatal(format_exception(e))
            self._stopping = True

    def _set_image(
        self,
        img: np.ndarray,
        keep_aspect_ratio: bool = True,
        smooth: bool = True,
        put_corner_dots_for_handshake: bool = False
    ):
        if img.dtype in (np.float32, np.float64):
            img = np.round(np.clip(img, 0., 1.) * 255.)
        img = img.astype(np.uint8)
        h, w = img.shape[:2]

        if len(img.shape) == 3:
            if img.shape[2] == 1:
                bytes_per_line = w
                fmt = QImage.Format.Format_Grayscale8
            elif img.shape[2] == 3:
                bytes_per_line = w * 3
                fmt = QImage.Format.Format_RGB888
            elif img.shape[2] == 4:
                bytes_per_line = w * 4
                fmt = QImage.Format.Format_RGBA8888
            else:
                raise ValueError(
                    "the third axis of an image must have a size of 4 (RGBA) "
                    "or 3 (RGB) or 1 (grayscale)."
                )
        elif len(img.shape) == 2:
            bytes_per_line = w
            fmt = QImage.Format.Format_Grayscale8
        else:
            raise ValueError("image shape is neither 3D or 2D (grayscale)")

        q_image = QImage(img, w, h, bytes_per_line, fmt)

        pixmap = QPixmap.fromImage(q_image)
        scaled_pixmap = pixmap.scaled(
            self._window.size(),

            Qt.AspectRatioMode.KeepAspectRatio if keep_aspect_ratio
            else Qt.AspectRatioMode.IgnoreAspectRatio,

            Qt.TransformationMode.SmoothTransformation if smooth
            else Qt.TransformationMode.FastTransformation
        )

        if put_corner_dots_for_handshake:
            painter = QPainter(scaled_pixmap)
            painter.setPen(Qt.PenStyle.NoPen)
            color = np.round(HANDSHAKE_CORNER_DOT_COLOR *
                             255.).astype(np.uint8)
            painter.setBrush(QBrush(
                QColor(*color),
                Qt.BrushStyle.SolidPattern
            ))
            painter.drawRect(
                0, 0, HANDSHAKE_CORNER_DOT_SIZE, HANDSHAKE_CORNER_DOT_SIZE
            )
            painter.drawRect(
                scaled_pixmap.width() - HANDSHAKE_CORNER_DOT_SIZE,
                scaled_pixmap.height() - HANDSHAKE_CORNER_DOT_SIZE,
                HANDSHAKE_CORNER_DOT_SIZE,
                HANDSHAKE_CORNER_DOT_SIZE
            )
            painter.end()

        self._label.setPixmap(scaled_pixmap)

    def _receive_thread_run(self):
        try:
            self._sct = mss.mss()
            monitors = self._sct.monitors

            if self._in_monitor_idx + 1 >= len(monitors):
                raise Exception(
                    f"invalid monitor index {self._in_monitor_idx}"
                )
            self._monitor = self._sct.monitors[self._in_monitor_idx + 1]

            while not self._started and not self._stopping:
                time.sleep(.02)

            while not self._stopping:
                start_time = time.time_ns()

                self._read_screen()

                elapsed = float(time.time_ns() - start_time) / 1e9
                if self._peer_format:
                    interval = \
                        1. / self._peer_format.rate / self._screenshot_speed
                else:
                    interval = .1
                time.sleep(max(0., interval - elapsed))
        except BaseException as e:
            self._log.fatal(format_exception(e))
        finally:
            self._stopping = True

    def _read_screen(self):
        if self._handshake_stage == HandshakeStage.LookingForPeerQr:
            img = self._take_screenshot()

            qr_codes = find_qr_codes(img)
            senders: list[tuple[Aabb, str, Format]] = []
            for qr in qr_codes:
                # skip invalid format

                parts = qr.text.split("#")
                if len(parts) < 4:
                    continue

                sender_version, sender_id, sender_format_str, cmd = parts[:4]
                parts = parts[4:]

                version_prefix = "VideoIo-"
                if not sender_version.startswith(version_prefix):
                    continue
                try:
                    sender_version = int(sender_version[len(version_prefix):])
                except Exception:
                    continue
                if sender_version < VIDEOIO_MIN_PEER_VERSION:
                    if sender_id not in self._ignored_peers:
                        self._ignored_peers.append(sender_id)
                        self._log.warning(
                            f"ignoring peer \"{sender_id}\" with version "
                            f"{sender_version} which is lower than the minimum "
                            f"supported ({VIDEOIO_MIN_PEER_VERSION})."
                        )
                    continue

                # skip our own QR
                if sender_id == self._sender_id:
                    continue

                # if a peer ID was explicitly requested by the user, skip any
                # other ID.
                if self._peer_sender_id is not None \
                        and sender_id != self._peer_sender_id:
                    continue

                # skip if the sender's format is invalid
                try:
                    sender_format = Format.create(sender_format_str)
                except Exception:
                    continue

                # look for hello or ack
                if len(parts) == 1:
                    if cmd != "ack":
                        continue
                    sender_acked_who = parts[0]
                    if sender_acked_who != self._sender_id:
                        # the sender is acknowledging someone else, not us
                        if self._peer_sender_id is not None:
                            raise Exception(
                                f"requested peer with ID "
                                f"\"{self._peer_sender_id}\" is acknowledging "
                                f"another peer with ID \"{sender_acked_who}\"."
                            )
                        continue
                elif cmd != "hello" or len(parts) > 1:
                    continue

                senders.append((qr.aabb, sender_id, sender_format))

            if not senders:
                return

            # found peer
            qr_aabb, self._peer_sender_id, self._peer_format = senders[0]

            # expand qr_aabb based on QR_BORDER_FACTOR
            center_x = (qr_aabb.top_left[0] + qr_aabb.bottom_right[0]) * .5
            center_y = (qr_aabb.top_left[1] + qr_aabb.bottom_right[1]) * .5
            width = qr_aabb.bottom_right[0] - qr_aabb.top_left[0]
            height = qr_aabb.bottom_right[1] - qr_aabb.top_left[1]
            size = float(
                np.sqrt(width * height) * (1. + QR_BORDER_FACTOR * 2.)
            )
            half_size = size * .5
            qr_aabb = Aabb(
                top_left=(center_x - half_size, center_y - half_size),
                bottom_right=(center_x + half_size, center_y + half_size)
            )

            # approximate scale of the peer's video feed
            rough_scale = \
                size / min(self._peer_format.width, self._peer_format.height)

            # refine qr_aabb based on the corner "dots" (squares) at the top
            # left and bottom right.

            padding = rough_scale * HANDSHAKE_CORNER_DOT_SIZE
            img_h, img_w = img.shape[:2]

            tl_x0 = int(max(0., qr_aabb.top_left[0] - padding))
            tl_y0 = int(max(0., qr_aabb.top_left[1] - padding))
            tl_x1 = int(min(img_w - .001, qr_aabb.top_left[0] + padding))
            tl_y1 = int(min(img_h - .001, qr_aabb.top_left[1] + padding))

            br_x0 = int(max(0., qr_aabb.bottom_right[0] - padding))
            br_y0 = int(max(0., qr_aabb.bottom_right[1] - padding))
            br_x1 = int(min(img_w - .001, qr_aabb.bottom_right[0] + padding))
            br_y1 = int(min(img_h - .001, qr_aabb.bottom_right[1] + padding))

            tl_window = img[tl_y0:tl_y1, tl_x0:tl_x1]
            br_window = img[br_y0:br_y1, br_x0:br_x1]

            tl_dot_aabb = find_colored_square_aabb(
                tl_window,
                HANDSHAKE_CORNER_DOT_COLOR
            )
            br_dot_aabb = find_colored_square_aabb(
                br_window,
                HANDSHAKE_CORNER_DOT_COLOR
            )

            if tl_dot_aabb:
                qr_aabb.top_left = (
                    tl_dot_aabb.top_left[0] + tl_x0,
                    tl_dot_aabb.top_left[1] + tl_y0
                )
            if br_dot_aabb:
                qr_aabb.bottom_right = (
                    br_dot_aabb.bottom_right[0] + br_x0,
                    br_dot_aabb.bottom_right[1] + br_y0
                )

            # compute the bounding box of the peer's video feed
            if self._peer_format.width > self._peer_format.height:
                center_x = (qr_aabb.top_left[0] + qr_aabb.bottom_right[0]) * .5
                h = qr_aabb.bottom_right[1] - qr_aabb.top_left[1]
                ratio = self._peer_format.width / self._peer_format.height
                self._peer_aabb = Aabb(
                    top_left=(
                        center_x - h * ratio * .5,
                        qr_aabb.top_left[1]
                    ),
                    bottom_right=(
                        center_x + h * ratio * .5,
                        qr_aabb.bottom_right[1]
                    )
                )
            else:
                center_y = (qr_aabb.top_left[1] + qr_aabb.bottom_right[1]) * .5
                w = qr_aabb.bottom_right[0] - qr_aabb.top_left[0]
                ratio = self._peer_format.height / self._peer_format.width
                self._peer_aabb = Aabb(
                    top_left=(
                        qr_aabb.top_left[0],
                        center_y - w * ratio * .5
                    ),
                    bottom_right=(
                        qr_aabb.bottom_right[0],
                        center_y + w * ratio * .5
                    )
                )

            # peer's color palette
            self._in_palette = COLOR_PALETTES[self._peer_format.bits_per_cell]
            self._in_palette = self._in_palette.astype(np.float32) / 255.

            self._log.info(
                f"found peer \"{self._peer_sender_id}\" with format "
                f"{self._peer_format} "
                f"({format_data_rate(self._peer_format.data_rate())})."
            )
            self._log.info(
                f"peer bounding box: {self._peer_aabb}. please make sure the "
                "peer's video feed does not move around on the screen."
            )

            self._handshake_stage = HandshakeStage.ShowingAck
        elif self._handshake_stage == HandshakeStage.WaitingForAck:
            qr_codes = find_qr_codes(self._take_screenshot())
            found_ack = False
            for qr in qr_codes:
                parts = qr.text.split("#")
                if len(parts) < 5:
                    continue

                _, sender_id, _, cmd, acked_who = parts[:5]
                parts = parts[5:]

                if sender_id != self._peer_sender_id:
                    continue

                if cmd != "ack":
                    continue

                if acked_who != self._sender_id:
                    raise Exception(
                        f"peer is acknowledging another peer with ID "
                        f"\"{acked_who}\"."
                    )

                found_ack = True
                break

            if not found_ack:
                return

            self._log.info("VideoIo handshake was successful")
            self._handshake_stage = HandshakeStage.Done
        elif self._handshake_stage != HandshakeStage.Done:
            return

        # take a screenshot
        img = self._take_screenshot()

        # scale up the image to handle non-integer AABB's more precisely and
        # then convert to float32.
        SCALE = 2.
        img = scale_image_u8(img, SCALE).astype(np.float32) / 255.

        # scaled peer AABB
        x0 = int(self._peer_aabb.top_left[0] * SCALE)
        y0 = int(self._peer_aabb.top_left[1] * SCALE)
        x1 = int(self._peer_aabb.bottom_right[0] * SCALE)
        y1 = int(self._peer_aabb.bottom_right[1] * SCALE)
        w = x1 - x0
        h = y1 - y0

        if x0 < 0. or y1 < 0. or x1 > img.shape[1] or y1 > img.shape[0]:
            raise RuntimeError(
                f"peer's video feed is not fully contained inside the monitor ("
                f"{x0=}, {y0=}, {x1=}, {y1=}, {img.shape=}, {SCALE=})."
            )

        cell_x = np.arange(self._peer_format.res_x, dtype=np.float32)
        cell_y = np.arange(self._peer_format.res_y, dtype=np.float32)

        cell_x0 = (
            x0 + w * (cell_x / self._peer_format.res_x)
        ).astype(np.int32)
        cell_x1 = (
            x0 + w * ((cell_x + 1.) / self._peer_format.res_x)
        ).astype(np.int32)
        cell_y0 = (
            y0 + h * (cell_y / self._peer_format.res_y)
        ).astype(np.int32)
        cell_y1 = (
            y0 + h * ((cell_y + 1.) / self._peer_format.res_y)
        ).astype(np.int32)

        # create meshgrid of cell indices
        x_indices, y_indices = np.meshgrid(
            cell_x.astype(np.int32),
            cell_y.astype(np.int32),
            indexing='xy'
        )

        # flatten for easier processing
        x0_flat = cell_x0[x_indices.ravel()]
        x1_flat = cell_x1[x_indices.ravel()]
        y0_flat = cell_y0[y_indices.ravel()]
        y1_flat = cell_y1[y_indices.ravel()]

        # extract the average color for every cell
        colors = []
        for i in range(len(x0_flat)):
            cell_average_color = np.mean(
                img[y0_flat[i]:y1_flat[i], x0_flat[i]:x1_flat[i]],
                axis=(0, 1)
            )
            colors.append(cell_average_color)
        colors = np.array(colors)

        # for every color, find the index of the closest color in the palette
        color_indices = np.argmin(
            cdist(colors, self._in_palette),
            axis=1
        ).astype(np.uint32)

        # unpack each value to "bits_per_cell" bits
        bits = unpack_n_bits(color_indices, self._peer_format.bits_per_cell)

        # remove any trailing zeros so the final size is a multiple of 8
        bits = bits[:bits.size // 8 * 8]

        # pack into bytes
        data = bytes(np.packbits(bits).data)

        if len(data) < PACKET_HEADER_BYTES:
            raise Exception(
                f"received packet data is too small ({len(data)} bytes) to "
                f"contain a header. this should never happen. there must be a "
                f"bug in the code."
            )

        # extract header and data
        header = data[:PACKET_HEADER_BYTES]
        data = data[PACKET_HEADER_BYTES:]

        # verify header checksum
        header_checksum = int.from_bytes(header[:2])
        header_actual_checksum = compute_checksum(header[2:])
        if header_checksum != header_actual_checksum:
            # header checksum couldn't be verified.

            # if we haven't read any packets yet and the header checksum is
            # incorrect, then the other side is probably still showing a QR code
            # as part of the handshake process, so we won't count it as a
            # corrupt receive.
            if self._in_valid_packet_idx > 0:
                self._log.debug(
                    "corrupt receive: header checksum couldn't be verified"
                )
                self.n_corrupt_receives_increment()
            return

        # handle retransmission request
        retransmission_req_idx = int.from_bytes(header[2:6])
        if retransmission_req_idx != 2**32 - 1 and (
            self._last_retran_req_idx != retransmission_req_idx
            or time.time() - self._last_retran_req_time > max(
                4,
                self._corrupt_packet_threshold
            ) / self._peer_format.rate
        ):
            self._last_retran_req_idx = retransmission_req_idx
            self._last_retran_req_time = time.time()

            force_acquire(self._out_packets_lock)

            prev_packet_idx = self._out_packet_idx
            self._out_packet_idx = max(0, min(
                self._out_packet_idx,
                retransmission_req_idx
            ))

            self._log.warning(
                f"retransmitting from packet index {self._out_packet_idx} "
                f"(was at {prev_packet_idx})."
            )

            self._out_packets_lock.release()

        packet_idx = int.from_bytes(header[6:10])
        data_checksum = int.from_bytes(header[10:12])
        data_len = int.from_bytes(header[12:16])
        if len(data) < data_len:
            raise Exception(
                f"packet's data length ({len(data)} bytes) is smaller than the "
                f"reported value in the header ({data_len} bytes)."
            )
        data = data[:data_len]

        # verify data checksum and packet index

        if data_len > 0:
            correct_checksum = compute_checksum(data)
            if data_checksum == correct_checksum:
                checksum_verified = True
            elif data_checksum == (correct_checksum + 1) % 2**16:
                # data is compressed, decompress it
                checksum_verified = True
                data = gzip.decompress(data)
            else:
                checksum_verified = False
        else:
            checksum_verified = True

        if packet_idx < self._in_valid_packet_idx:
            # we've already received this packet index, ignore
            return
        elif packet_idx > self._in_valid_packet_idx:
            # we've missed at least one packet since the last valid one
            self._log.debug(
                f"corrupt receive: index too far ahead "
                f"({packet_idx} > {self._in_valid_packet_idx})"
            )
            self.n_corrupt_receives_increment()
            return
        elif not checksum_verified:
            # data checksum couldn't be verified so there must be errors in the
            # data.
            self._log.debug(
                "corrupt receive: data checksum couldn't be verified"
            )
            self.n_corrupt_receives_increment()
            return

        # at last! the header is correct, the index is correct, and the data is
        # correct, so we can add it to the input buffer.
        force_acquire(self._in_buf_lock)
        self._in_valid_packet_idx += 1
        self._in_buf += data
        self._in_buf_lock.release()

        self.n_corrupt_receives_decrement()

    def _take_screenshot(self) -> np.ndarray:
        screenshot = self._sct.grab(self._monitor)
        return np.array(screenshot)[:, :, :3][:, :, ::-1]  # BGRA → RGB

    def n_corrupt_receives_increment(self):
        self._n_corrupt_receives += 1

        n_corrupt_packets = int(
            self._n_corrupt_receives / self._screenshot_speed
        )
        if n_corrupt_packets > self._corrupt_packet_threshold:
            self._n_corrupt_receives = 0
            self._request_retransmission = True

    def n_corrupt_receives_decrement(self):
        self._n_corrupt_receives = max(
            0,
            self._n_corrupt_receives - 1
        )


def compute_checksum(data: bytes) -> int:
    return zlib.crc32(data) % 2**16


def unpack_bits(data: bytes) -> np.ndarray:
    return np.unpackbits(np.frombuffer(data, dtype=np.uint8))


def unpack_n_bits(arr: np.ndarray, n: int) -> np.ndarray:
    """
    unpack an array of integers in the [0, 2^N-1] range to an array of bits with
    N bits for each element in arr.
    """
    return (
        (arr[:, None] >> ((n - 1) - np.arange(n)))
        & 1
    ).flatten().astype(np.uint8)


def pack_bits(bits: np.ndarray, m: int) -> np.ndarray:
    """
    pack every M bits into an integer from 0 to 2^M-1.
    """

    # ensure clean multiple and reshape into (N//M, M)
    if bits.size % m != 0:
        bits = np.pad(bits, (0, m - bits.size % m), constant_values=[0])
    bits = bits.reshape(-1, m)

    # sum with bit-place weights, e.g. [8, 4, 2, 1] for M=4.
    weights = 1 << np.arange(m - 1, -1, -1, bits.dtype)
    return np.sum(bits * weights, axis=1, dtype=bits.dtype)


def generate_qr(
    data: str | bytes,
    border_factor: float = QR_BORDER_FACTOR
) -> np.ndarray:
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=12,
        border=0
    )
    qr.add_data(data)
    qr.make(fit=True)

    img = np.array(
        qr.make_image(
            fill_color="black",
            back_color="white"
        ).convert("L"),
        dtype=np.uint8
    )

    size = img.shape[0]
    padding = int(border_factor * size)
    img = np.pad(
        img,
        ((padding, padding), (padding, padding)),
        constant_values=[255]
    )

    return img


class DetectedQr(NamedTuple):
    aabb: Aabb
    text: str


def qr_refine_corners(
    img_gray: np.ndarray,
    corners: list[tuple[float, float]],
    win_size: int = 7,
    zero_zone: int = -1,
    max_iter: int = 50,
    eps: float = .001
) -> list[tuple[float, float]]:
    """
    refine QR code corner points to subpixel accuracy. used in find_qr_codes().
    """

    # convert to the shape expected by cornerSubPix: (N, 1, 2)
    corners_np = np.array(corners, dtype=np.float32).reshape(-1, 1, 2)

    criteria = (
        cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER,
        max_iter,
        eps
    )
    cv2.cornerSubPix(
        img_gray,
        corners_np,
        (win_size, win_size),
        (zero_zone, zero_zone),
        criteria
    )

    refined = corners_np.reshape(-1, 2).tolist()
    return [(float(x), float(y)) for x, y in refined]


def find_qr_codes(img: np.ndarray) -> list[DetectedQr]:
    """
    detect all QR codes in an image and return their bounding boxes and content.

    Args:
        img: input image in RGB format as a numpy array.

    Returns:
        list of `DetectedQr` objects, one per detected QR code.
    """

    if img is None or img.size == 0:
        return []

    # convert to grayscale for corner refinement
    if img.ndim == 3:
        img = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)

    # detect QR codes
    decoded_objects = pyzbar.decode(img)

    results: list[DetectedQr] = []
    for obj in decoded_objects:
        if hasattr(obj, 'polygon') and obj.polygon is not None:
            # obj.polygon is a list of 4 points (x, y)
            approx_corners = [(float(p.x), float(p.y)) for p in obj.polygon]
        else:
            # fallback to rect if polygon is missing
            left, top, width, height = obj.rect
            approx_corners = [
                (left, top),
                (left + width, top),
                (left + width, top + height),
                (left, top + height)
            ]

        if len(approx_corners) != 4:
            continue

        # refine the corners
        refined_corners = qr_refine_corners(img, approx_corners)

        # pixel center is 0.5
        refined_corners_centered = \
            [(x + .5, y + .5) for x, y in refined_corners]

        # compute the axis‑aligned bounding box from the four corners
        xs = [p[0] for p in refined_corners_centered]
        ys = [p[1] for p in refined_corners_centered]
        aabb = Aabb(
            top_left=(min(xs), min(ys)),
            bottom_right=(max(xs), max(ys))
        )

        try:
            text = obj.data.decode("utf-8")
        except Exception:
            continue

        results.append(DetectedQr(aabb=aabb, text=text))

    return results


def find_colored_square_aabb(
    img: np.ndarray,
    color: np.ndarray,
    tolerance: float = .25
) -> Aabb | None:
    """
    find the axis-aligned bounding box (AABB) of a square with a certain color
    inside a floating-point image.

    pixel center is 0.5.

    Args:
        img (np.ndarray): floating-point RGB image of shape (H, W, 3)
        color (np.ndarray): floating-point RGB triplet of shape (3,)
        tolerance (float): maximum Euclidean distance from the color

    Returns:
        an `Aabb` if the square was found, None if not.
    """

    assert img.dtype in (np.float32, np.float64)
    assert len(img.shape) == 3 and img.shape[2] == 3

    euclidean_distances = np.linalg.norm(img - color, axis=2)
    mask = euclidean_distances < tolerance

    # get pixel coordinates ((row, col) pairs) where mask==True
    coords = np.argwhere(mask)

    if len(coords) == 0:
        return None

    y_min, x_min = coords.min(axis=0)
    y_max, x_max = coords.max(axis=0)

    return Aabb(
        top_left=(float(x_min), float(y_min)),
        bottom_right=(float(x_max) + 1., float(y_max) + 1.)
    )


def scale_image_u8(img: np.ndarray[np.uint8], scale: float):
    img_pil = Image.fromarray(img)

    h, w = img.shape[:2]
    img_scaled = img_pil.resize(
        (int(w * scale), int(h * scale)),
        Image.Resampling.BILINEAR
    )

    return np.array(img_scaled)
