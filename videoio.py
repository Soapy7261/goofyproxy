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
import zlib

from PySide6.QtWidgets import QApplication, QMainWindow, QLabel
from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QImage, QPixmap

import mss
from mss.base import MSSBase
from mss.models import Monitor

import qrcode
import qrcode.constants
from pyzbar import pyzbar

from goofyio import GoofyIo
from common import *

VIDEOIO_VERSION = 1
VIDEOIO_MIN_PEER_VERSION = 1

# remove older outgoing packets that are already transmitted until we go below
# the memory limit or reach the minimum outgoing packet count, because we need
# to keep the last few packets in case the other side asks for a retransmission.
OUT_PACKETS_MEMORY_LIMIT = 512 * 1024 * 1024
OUT_PACKETS_MIN_COUNT = 16  # must be bigger than 1


"""
packet header (12 bytes):
[2 bytes] header checksum
[2 bytes] retransmission request packet index (FFFF if none)
[2 bytes] packet index
[2 bytes] packet data checksum
[4 bytes] packet data length in bytes
"""

PACKET_HEADER_BYTES: int = 12
PACKET_IDX_MAX = 64000


class Packet:
    retransmission_req_idx: int
    idx: int
    data: bytes

    def __init__(
        self,
        retransmission_req_idx: int | None,
        idx: int,
        data: bytes
    ):
        self.retransmission_req_idx = retransmission_req_idx
        self.idx = idx
        self.data = data

        if self.idx > PACKET_IDX_MAX:
            raise ValueError(
                f"invalid packet index ({self.idx} > {PACKET_IDX_MAX})"
            )

    def to_bytes(self, format: Format) -> bytes:
        if self.retransmission_req_idx is None \
                or self.retransmission_req_idx < 0:
            retran_req = 65535
        else:
            retran_req = int(self.retransmission_req_idx)

        header = \
            retran_req.to_bytes(2) \
            + self.idx.to_bytes(2) \
            + compute_checksum(self.data).to_bytes(2) \
            + len(self.data).to_bytes(4)

        header_checksum = compute_checksum(header)
        header = header_checksum.to_bytes(2) + header

        if len(self.data) > format.data_bytes_per_frame:
            raise ValueError(
                f"data is too large ({len(self.data)} bytes) for the format "
                f"({format})"
            )

        data = self.data

        n_trailing_zeros = format.data_bytes_per_frame - len(data)
        data += b"\0" * n_trailing_zeros

        return header + data


class Format:
    """
    color grid format used to encode data.

    Arguments:

      width (int): total width

      height (int): total height

      cell_width (int): width of individual cells

      cell_height (int): height of individual cells

      bits_per_cell (int): how many bits to encode in each cell. the total
        number of possible colors will be 2 to the power of this number (e.g. 16
        possible colors for 4 bits).

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
        if self.bits_per_cell not in range(1, 9):
            raise ValueError(
                f"bits_per_cell={self.bits_per_cell} is not in the [1, 8] range"
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
    # 5: np.asarray([[131, 86, 51], [64, 175, 104], [197, 27, 157], [31, 116, 211], [164, 205, 8], [97, 57, 61], [231, 145, 115], [14, 234, 168], [147, 7, 221], [81, 96, 19], [214, 185, 72], [47, 37, 125], [181, 126, 179], [114, 214, 232], [247, 66, 29], [6, 155, 83], [139, 244, 136], [72, 17, 189], [206, 106, 243], [39, 195, 40], [172, 47, 93], [106, 136, 147], [239, 224, 200], [22, 76, 253], [156, 165, 0], [89, 254, 53], [222, 1, 106], [56, 89, 159], [189, 178, 213], [122, 30, 10], [255, 119, 63], [2, 208, 117]], dtype=np.uint8),

    # 6-bit: 64 colors
    # 6: np.asarray([[129, 85, 50], [63, 172, 102], [194, 27, 155], [30, 114, 207], [161, 201, 8], [96, 56, 60], [227, 143, 113], [14, 230, 165], [145, 7, 218], [79, 95, 18], [210, 182, 71], [47, 36, 123], [178, 124, 176], [112, 211, 228], [243, 65, 29], [6, 153, 81], [137, 240, 134], [71, 17, 186], [202, 104, 239], [38, 192, 39], [169, 46, 92], [104, 133, 144], [235, 221, 197], [22, 75, 249], [153, 163, 0], [88, 250, 52], [219, 1, 104], [55, 88, 157], [186, 175, 209], [120, 30, 10], [251, 117, 62], [2, 205, 115], [133, 59, 167], [67, 146, 220], [198, 234, 21], [34, 10, 73], [165, 98, 125], [100, 185, 178], [231, 40, 230], [18, 127, 31], [149, 214, 83], [83, 69, 136], [215, 156, 188], [51, 243, 241], [182, 20, 41], [116, 107, 94], [247, 195, 146], [10, 49, 199], [141, 137, 251], [75, 224, 2], [206, 78, 54], [43, 166, 107], [174, 253, 159], [108, 4, 211], [239, 91, 12], [26, 179, 65], [157, 33, 117], [92, 120, 169], [223, 208, 222], [59, 62, 23], [190, 150, 75], [124, 237, 127], [255, 14, 180], [0, 101, 232]], dtype=np.uint8),

    # 7-bit: 128 colors
    # 7: np.asarray([[128, 85, 51], [64, 171, 102], [193, 28, 154], [31, 114, 206], [161, 200, 9], [96, 57, 61], [225, 143, 113], [15, 229, 164], [144, 9, 216], [80, 95, 20], [209, 181, 71], [48, 37, 123], [177, 124, 175], [112, 210, 227], [241, 66, 30], [7, 152, 82], [136, 238, 133], [72, 18, 185], [201, 104, 237], [39, 191, 40], [169, 47, 92], [104, 133, 144], [233, 219, 195], [23, 76, 247], [153, 162, 1], [88, 248, 53], [217, 2, 105], [56, 88, 156], [185, 175, 208], [120, 31, 11], [249, 117, 63], [3, 203, 115], [132, 60, 167], [68, 146, 218], [197, 232, 22], [35, 12, 74], [165, 98, 125], [100, 184, 177], [229, 41, 229], [19, 127, 32], [148, 213, 84], [84, 69, 136], [213, 155, 187], [52, 242, 239], [181, 21, 43], [116, 108, 94], [245, 194, 146], [11, 50, 198], [140, 136, 249], [76, 222, 3], [205, 79, 55], [44, 165, 107], [173, 251, 158], [108, 5, 210], [237, 92, 14], [27, 178, 65], [157, 34, 117], [92, 120, 169], [221, 206, 220], [60, 63, 24], [189, 149, 76], [124, 235, 127], [253, 15, 179], [1, 101, 231], [130, 187, 34], [66, 44, 86], [195, 130, 138], [33, 216, 189], [163, 72, 241], [98, 159, 45], [227, 245, 96], [17, 25, 148], [146, 111, 200], [82, 197, 251], [211, 53, 5], [50, 139, 57], [179, 226, 109], [114, 82, 160], [243, 168, 212], [9, 254, 16], [138, 0, 67], [74, 86, 119], [203, 172, 171], [41, 29, 222], [171, 115, 26], [106, 201, 78], [235, 58, 129], [25, 144, 181], [155, 230, 233], [90, 10, 36], [219, 96, 88], [58, 182, 140], [187, 38, 191], [122, 125, 243], [251, 211, 47], [5, 67, 98], [134, 153, 150], [70, 239, 202], [199, 19, 253], [37, 105, 7], [167, 192, 59], [102, 48, 111], [231, 134, 162], [21, 220, 214], [151, 77, 18], [86, 163, 69], [215, 249, 121], [54, 3, 173], [183, 89, 224], [118, 176, 28], [247, 32, 80], [13, 118, 131], [142, 204, 183], [78, 61, 235], [207, 147, 38], [46, 233, 90], [175, 13, 142], [110, 99, 193], [239, 185, 245], [29, 42, 49], [159, 128, 100], [94, 214, 152], [223, 70, 204], [62, 156, 255], [191, 243, 0], [126, 22, 51], [255, 109, 103], [0, 195, 155]], dtype=np.uint8),

    # 8-bit: 256 colors
    # 8: np.asarray([[128, 85, 51], [63, 171, 102], [192, 28, 154], [31, 114, 205], [160, 199, 9], [96, 56, 61], [224, 142, 112], [15, 228, 164], [144, 9, 215], [80, 94, 20], [208, 180, 71], [47, 37, 123], [176, 123, 174], [112, 209, 226], [240, 66, 30], [7, 152, 82], [136, 237, 133], [72, 18, 184], [200, 104, 236], [39, 190, 40], [168, 47, 92], [104, 133, 143], [232, 218, 195], [23, 75, 246], [152, 161, 1], [88, 247, 53], [216, 2, 104], [55, 88, 156], [184, 174, 207], [120, 31, 12], [248, 117, 63], [3, 202, 114], [132, 60, 166], [68, 145, 217], [196, 231, 22], [35, 12, 73], [164, 98, 125], [100, 183, 176], [228, 40, 228], [19, 126, 32], [148, 212, 84], [84, 69, 135], [212, 155, 187], [51, 241, 238], [180, 21, 42], [116, 107, 94], [244, 193, 145], [11, 50, 197], [140, 136, 248], [76, 222, 3], [204, 79, 55], [43, 164, 106], [172, 250, 158], [108, 6, 209], [236, 91, 14], [27, 177, 65], [156, 34, 117], [92, 120, 168], [220, 206, 219], [59, 63, 24], [188, 148, 75], [124, 234, 127], [252, 15, 178], [1, 101, 230], [130, 187, 34], [65, 44, 86], [194, 129, 137], [33, 215, 189], [162, 72, 240], [98, 158, 44], [226, 244, 96], [17, 25, 147], [146, 110, 199], [82, 196, 250], [210, 53, 5], [49, 139, 57], [178, 225, 108], [114, 82, 160], [242, 168, 211], [9, 253, 16], [138, 0, 67], [74, 86, 119], [202, 172, 170], [41, 29, 222], [170, 115, 26], [106, 200, 77], [234, 57, 129], [25, 143, 180], [154, 229, 232], [90, 10, 36], [218, 96, 88], [57, 181, 139], [186, 38, 191], [122, 124, 242], [250, 210, 47], [5, 67, 98], [134, 153, 149], [70, 238, 201], [198, 19, 252], [37, 105, 7], [166, 191, 59], [102, 48, 110], [230, 134, 162], [21, 219, 213], [150, 76, 18], [86, 162, 69], [214, 248, 121], [53, 3, 172], [182, 89, 224], [118, 175, 28], [246, 32, 79], [13, 118, 131], [142, 204, 182], [78, 61, 234], [206, 146, 38], [45, 232, 90], [174, 13, 141], [110, 99, 193], [238, 184, 244], [29, 42, 49], [158, 127, 100], [94, 213, 152], [222, 70, 203], [61, 156, 254], [190, 242, 0], [126, 22, 51], [254, 108, 103], [0, 194, 154], [129, 51, 205], [64, 137, 10], [193, 223, 61], [32, 80, 113], [161, 165, 164], [97, 251, 216], [225, 7, 20], [16, 92, 72], [145, 178, 123], [81, 35, 175], [209, 121, 226], [48, 207, 30], [177, 64, 82], [113, 150, 133], [241, 235, 185], [8, 16, 236], [137, 102, 41], [73, 188, 92], [201, 45, 144], [40, 130, 195], [169, 216, 247], [105, 73, 2], [233, 159, 53], [24, 245, 105], [153, 26, 156], [89, 111, 208], [217, 197, 12], [56, 54, 63], [185, 140, 115], [121, 226, 166], [249, 83, 218], [4, 169, 22], [133, 254, 74], [69, 1, 125], [197, 87, 177], [36, 173, 228], [165, 30, 33], [101, 116, 84], [229, 201, 135], [20, 58, 187], [149, 144, 238], [85, 230, 43], [213, 11, 94], [52, 97, 146], [181, 182, 197], [117, 39, 249], [245, 125, 4], [12, 211, 55], [141, 68, 107], [77, 154, 158], [205, 240, 210], [44, 20, 14], [173, 106, 65], [109, 192, 117], [237, 49, 168], [28, 135, 220], [157, 220, 24], [93, 78, 76], [221, 163, 127], [60, 249, 179], [189, 4, 230], [125, 90, 35], [253, 176, 86], [2, 33, 138], [131, 119, 189], [67, 205, 240], [195, 62, 45], [34, 147, 96], [163, 233, 148], [99, 14, 199], [227, 100, 251], [18, 186, 6], [147, 43, 57], [83, 128, 109], [211, 214, 160], [50, 71, 212], [179, 157, 16], [115, 243, 68], [243, 24, 119], [10, 109, 170], [139, 195, 222], [75, 52, 26], [203, 138, 78], [42, 224, 129], [171, 81, 181], [107, 166, 232], [235, 252, 37], [26, 8, 88], [155, 93, 140], [91, 179, 191], [219, 36, 243], [58, 122, 47], [187, 208, 98], [123, 65, 150], [251, 151, 201], [6, 236, 253], [135, 17, 8], [71, 103, 59], [199, 189, 111], [38, 46, 162], [167, 132, 214], [103, 217, 18], [231, 74, 70], [22, 160, 121], [151, 246, 173], [87, 27, 224], [215, 112, 28], [54, 198, 80], [183, 55, 131], [119, 141, 183], [247, 227, 234], [14, 84, 39], [143, 170, 90], [79, 255, 142], [207, 0, 193], [46, 85, 245], [175, 171, 49], [111, 28, 100], [239, 114, 152], [30, 200, 203], [159, 57, 255], [95, 142, 0], [223, 228, 51], [62, 9, 103], [191, 95, 154], [127, 181, 206], [255, 38, 10], [0, 123, 62]], dtype=np.uint8),
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
    other side's video feed.
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

    _out_pixels: np.ndarray

    _in_packet_idx: int = 0
    _in_packet_checksum: int = 0
    _in_packet_datalen: int = 0

    _in_valid_packet_idx: int = 0

    _in_buf: bytearray
    _in_buf_lock: threading.Lock

    _request_retransmission: bool = False

    _running: bool = True
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

    _ignored_peers: list[str]

    def __init__(
        self,
        out_format: str | Format,
        in_monitor_idx: int = 0,
        peer_id: str | None = None
    ):
        self._log = make_logger(f"VideoIo")

        self.out_format = Format.create(out_format)
        self._in_monitor_idx = int(in_monitor_idx)
        self._peer_sender_id = peer_id

        self._out_palette = COLOR_PALETTES[self.out_format.bits_per_cell]
        self._ignored_peers = []

        self._log.debug(
            f"output format: {self.out_format} "
            f"({self.out_format.data_rate() / 1024:.1f} KiB/s)"
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
            self._sender_id = getpass.getuser().strip().replace(" ", "-") \
                .replace("#", "-")
        except OSError:
            pass
        if not self._sender_id:
            self._sender_id = "unknown"
        self._sender_id += f"-{random.randint(0, 999)}"
        self._log.info(f"using sender ID {self._sender_id}")

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

    def running(self) -> bool:
        return self._running

    def stop(self):
        self._running = False

    def get_monitors() -> list[Monitor]:
        sct = mss.mss()
        mons = sct.monitors[1:]
        sct.close()
        return mons

    def _receive(self, size: int) -> bytes:
        if not self._running:
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
        if not self._running:
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

            self._label = QLabel()
            self._label.setScaledContents(False)
            self._window.setCentralWidget(self._label)

            self._timer = QTimer()
            self._timer.timeout.connect(self._update_image)
            self._timer.start(1000. / self.out_format.rate)

            self._window.show()
            self._app.exec()
        except BaseException as e:
            self._log.fatal(format_exception(e))
        finally:
            self._running = False
            try:
                self._timer.stop()
                self._window.destroy()
                self._app.quit()
            except:
                pass

    def _update_image(self):
        if not self._running:
            self._timer.stop()
            self._window.destroy()
            self._app.quit()
            return

        try:
            # show hello QR code
            if self._handshake_stage == HandshakeStage.ShowingQr:
                self._set_image(generate_qr(
                    f"VideoIo-{VIDEOIO_VERSION}#{self._sender_id}"
                    f"#{self.out_format}#hello"
                ))

                self._log.info("looking for a peer's QR code")
                self._handshake_stage = HandshakeStage.LookingForPeerQr
                return

            # wait for the receive thread to detect the peer's QR code
            if self._handshake_stage == HandshakeStage.LookingForPeerQr:
                return

            # show acknowledgement QR code
            if self._handshake_stage == HandshakeStage.ShowingAck:
                self._set_image(generate_qr(
                    f"VideoIo-{VIDEOIO_VERSION}#{self._sender_id}"
                    f"#{self.out_format}#ack#{self._peer_sender_id}"
                ))

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
            data = bytes(self._out_buf[:self.out_format.data_bytes_per_frame])
            self._out_buf = self._out_buf[self.out_format.data_bytes_per_frame:]
            self._out_buf_lock.release()

            if not data and not self._request_retransmission:
                return

            retran_req_idx = None
            if self._request_retransmission:
                self._request_retransmission = False
                retran_req_idx = self._in_valid_packet_idx

            force_acquire(self._out_packets_lock)

            self._out_packets.append(Packet(
                retran_req_idx,
                (len(self._out_packets) + self._out_packet_idx_offs)
                % (PACKET_IDX_MAX + 1),
                data
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
            self._running = False

    def _set_image(
        self,
        img: np.ndarray,
        keep_aspect_ratio: bool = True,
        smooth: bool = True
    ):
        if img.dtype in (np.float32, np.float64, np.float128):
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

            while self._running:
                start_time = time.time_ns()

                self._read_screen()

                elapsed = float(time.time_ns() - start_time) / 1e9
                if self._peer_format:
                    interval = .95 / self._peer_format.rate
                else:
                    interval = .1
                time.sleep(max(0., interval - elapsed))
        except BaseException as e:
            self._log.fatal(format_exception(e))
        finally:
            self._running = False

    def _read_screen(self):
        return
        if self._handshake_stage == HandshakeStage.LookingForPeerQr:
            qr_codes = find_qr_codes(self._take_screenshot())
            senders: list[tuple[Aabb, str, Format]] = []
            for qr in qr_codes:
                # skip invalid format

                parts = qr.text.split("#")
                if len(parts) < 4:
                    continue

                sender_version, sender_id, sender_format_str, cmd = parts[:4]
                parts = parts[4:]
                if not sender_version.startswith("VideoIo-"):
                    continue
                try:
                    sender_version = int(sender_version)
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
                    sender_format = Format(sender_format_str)
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
                elif cmd != "hello":
                    continue

                senders.append((qr.aabb, sender_id, sender_format))

            if not senders:
                return

            qr_aabb = self._peer_sender_id, self._peer_format = senders[0]
            self._in_palette = COLOR_PALETTES[self._peer_format.bits_per_cell]

            self._log.info(
                f"found peer \"{self._peer_sender_id}\" with format "
                f"{self._peer_format} "
                f"({self._peer_format.data_rate() / 1024:.1f} KiB/s). please "
                f"make sure the peer's video feed does not move around in the "
                f"screen."
            )
            self._handshake_stage = HandshakeStage.ShowingAck
        elif self._handshake_stage == HandshakeStage.WaitingForAck:
            return
        elif self._handshake_stage != HandshakeStage.Done:
            return

    def _take_screenshot(self) -> np.ndarray:
        screenshot = self._sct.grab(self._monitor)
        return np.array(screenshot)[:, :, :3][:, :, ::-1]  # BGRA → RGB


def compute_checksum(data: bytes) -> int:
    return zlib.crc32(data) % 2**16


def unpack_bits(data: bytes) -> np.ndarray:
    return np.unpackbits(np.frombuffer(data, dtype=np.uint8))


def pack_bits(bits: np.ndarray, m: int) -> np.ndarray:
    """
    pack every M bits into an integer from 0 to 2^M-1.
    """

    # ensure clean multiple and reshape into (N//M, M)
    if bits.size % m != 0:
        bits = np.pad(bits, (0, m - bits.size % m), constant_values=[0])
    bits = bits.reshape(-1, m)

    # sum with bit-place weights, e.g. [8, 4, 2, 1] for m=4.
    weights = 1 << np.arange(m - 1, -1, -1, bits.dtype)
    return np.sum(bits * weights, axis=1, dtype=bits.dtype)


QR_BORDER_FACTOR = .15


def generate_qr(data: str | bytes) -> np.ndarray:
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=0,
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
    padding = int(QR_BORDER_FACTOR * size)
    img = np.pad(
        img,
        ((padding, padding), (padding, padding)),
        constant_values=[255]
    )

    return img


class Aabb(NamedTuple):
    """
    2D axis-aligned bounding box
    """
    top_left: tuple[float, float]
    bottom_right: tuple[float, float]


class DetectedQr(NamedTuple):
    aabb: Aabb
    text: str


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

    decoded_objects = pyzbar.decode(img)

    results = []
    for obj in decoded_objects:
        left, top, width, height = obj.rect

        center_x = left + width * .5
        center_y = top + height * .5
        size = float(np.sqrt(width * height) * (1. + QR_BORDER_FACTOR * 2.))
        half_size = size * .5

        aabb = Aabb(
            top_left=(center_x - half_size, center_y - half_size),
            bottom_right=(center_x + half_size, center_y + half_size)
        )

        try:
            text = obj.data.decode('utf-8')
        except Exception:
            continue

        results.append(DetectedQr(aabb=aabb, text=text))

    return results
