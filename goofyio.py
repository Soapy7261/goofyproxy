"""
abstraction for data transfer through goofy ahh channels
"""

import base64
import gzip
from pathlib import Path


class GoofyIo:
    """
    an abstraction for data transfer through goofy ahh channels. the channel
    could be a voice or video call, botted text messages, radio waves, you name
    it.

    the channel is a continuous stream of data. any packet boundaries or
    protocols you use are not GoofyIo's concern. it simply sends a buncha data
    and receives a buncha data.

    derived classes must:
    1. ensure correct ordering of bytes and between subsequent calls to send()
       or receive().
    2. avoid data corruption using error detection and correction if needed.
    3. adjust the speed and other parameters of the data channel for maximum
       efficiency.
    4. handle compression and encryption if needed in your use case.
    5. handle thread safety if needed in your use case.
    """

    def _receive(self, size: int) -> bytes:
        """
        abstract function that receives exactly `size` bytes.

        - must block until the required amount of data is received.
        - must raise OSError/IOError (or one of its derived classes) when the
          connection is broken and there's no hope to continue as normal.
        """
        raise NotImplementedError()

    def _send(self, data: bytes):
        """
        abstract function for sending every byte in `data`.

        - avoid blocking. if transmission takes too long, push the data to a
          buffer/queue and transmit on another thread.
        - must raise OSError/IOError (or one of its derived classes) when the
          connection is broken and there's no hope to continue as normal.
        """
        raise NotImplementedError()

    def receive(self, size: int) -> bytes:
        b = self._receive(size)
        if len(b) != size:
            raise ValueError(
                f"derived class was expected to return buffer with {size=} but "
                f"returned {len(b)} bytes instead"
            )
        return b

    def send(self, data: bytes):
        self._send(data)


class TxtFileIo(GoofyIo):
    session_id: str
    send_as_id: str
    receive_from_id: str
    channel_dir: Path

    downlink_idx: int
    downlink_buf: bytes = b""
    uplink_idx: int

    # if the first byte of a packet is this value, the data is compressed
    GZIP_MARK = 133

    def __init__(
        self,
        session_id: str,
        send_as_id: str,
        receive_from_id: str,
        channel_dir: Path = Path(__file__).parent / "file_io",
        initial_downlink_idx: int = 0,
        initial_uplink_idx: int = 0
    ):
        self.session_id = session_id
        self.send_as_id = send_as_id
        self.receive_from_id = receive_from_id
        self.channel_dir = channel_dir
        self.downlink_idx = initial_downlink_idx
        self.uplink_idx = initial_uplink_idx

        self.channel_dir.mkdir(parents=True, exist_ok=True)
        for p in self.channel_dir.iterdir():
            if not p.name.startswith(f"{self.session_id}-") \
                    or not p.name.endswith(".txt"):
                continue
            p.unlink(missing_ok=True)

    def _receive(self, size: int) -> bytes:
        buf = b""
        if len(self.downlink_buf) > 0:
            read_size = min(size, len(self.downlink_buf))
            buf += self.downlink_buf[:read_size]

            if read_size >= len(self.downlink_buf):
                self.downlink_buf = b""
            else:
                self.downlink_buf = self.downlink_buf[read_size:]

            if len(buf) == size:
                return bytes(buf)
            if len(buf) > size:
                raise OverflowError("this should never happen")

        filename_prefix = f"{self.session_id}-{self.receive_from_id}-"
        filename_suffix = ".txt"
        while True:
            next_packet_path: Path | None = None
            for path in self.channel_dir.iterdir():
                if not path.name.startswith(filename_prefix) \
                        or not path.name.endswith(filename_suffix):
                    continue

                idx = -1
                try:
                    idx = int(path.name[
                        len(filename_prefix):-len(filename_suffix)
                    ])
                except:
                    continue

                if idx == self.downlink_idx:
                    next_packet_path = path
                elif idx < self.downlink_idx:
                    path.unlink(missing_ok=True)

            if not next_packet_path:
                continue

            # keep waiting if the file is incomplete
            text = next_packet_path.read_text()
            if not text:
                continue

            # continue waiting if can't decode base85
            try:
                temp_buf = base64.b85decode(text)
            except:
                continue

            # coninute if we don't even have the gzip mark and the size metadata
            if len(temp_buf) < 5:
                continue

            gzip_mark = temp_buf[0]

            # continue if the size doesn't match the metadata
            size_metadata = int.from_bytes(temp_buf[1:5])
            if len(temp_buf) != size_metadata + 5:
                continue

            # decompress if needed
            if gzip_mark == TxtFileIo.GZIP_MARK:
                temp_buf = gzip.decompress(temp_buf[5:])
            else:
                temp_buf = temp_buf[5:]

            # we can finally increment the index now
            self.downlink_idx += 1
            self.downlink_buf = temp_buf

            read_size = min(size - len(buf), len(self.downlink_buf))
            buf += self.downlink_buf[:read_size]

            if read_size >= len(self.downlink_buf):
                self.downlink_buf = b""
            else:
                self.downlink_buf = self.downlink_buf[read_size:]

            if len(buf) == size:
                return buf
            if len(buf) > size:
                raise OverflowError("this should never happen")

    def _send(self, data: bytes):
        p = self.channel_dir / \
            f"{self.session_id}-{self.send_as_id}-{self.uplink_idx}.txt"

        compressed = gzip.compress(data)
        if len(compressed) < len(data):
            data = \
                bytes([self.GZIP_MARK]) \
                + len(compressed).to_bytes(4) \
                + compressed
        else:
            data = b"\0" + len(data).to_bytes(4) + data

        with open(p, "wb") as f:
            f.write(base64.b85encode(data))
            f.close()
        self.uplink_idx += 1
