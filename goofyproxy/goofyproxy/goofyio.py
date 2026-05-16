"""
an abstraction for data transfer through goofy ahh channels.
"""

import time
import socket
from typing import NamedTuple
from pathlib import Path
from dataclasses import dataclass
import threading
import logging
import gzip
import base64

from .common import *
from .threadpool import ThreadPool


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
        - must raise OSError/IOError or one of its subclasseswhen the connection
          is broken and there's no hope to properly receive the required amount
          of data.
        """
        raise NotImplementedError()

    def _send(self, data: bytes):
        """
        abstract function for sending every byte in `data`.

        - avoid blocking. push the data to a buffer or queue and transmit on
          a separate thread.
        - must raise OSError/IOError or one of its subclasses when the
          connection is broken and there's no hope to fully send `data`.
        """
        raise NotImplementedError()

    def receive(self, size: int) -> bytes:
        b = self._receive(size)
        if not isinstance(b, bytes):
            raise ValueError(
                f"GoofyIo subclass was expected to return bytes, got {type(b)}."
            )
        if len(b) != size:
            raise ValueError(
                f"GoofyIo subclass was expected to return {size} bytes, got "
                f"{len(b)} bytes instead."
            )
        return b

    def send(self, data: bytes):
        self._send(data)


class SocketIo(GoofyIo):
    """
    a not so `GoofyIo` that uses a socket to transfer data. useful for testing
    and debugging.
    """

    sock: socket.socket

    def __init__(self, sock: socket.socket):
        self.sock = sock

    def _receive(self, size: int) -> bytes:
        buf = b""
        while len(buf) < size:
            chunk = self.sock.recv(size - len(buf))
            if not chunk:
                raise EOFError(
                    "connection closed before enough data was received"
                )
            buf += chunk
        return buf

    def _send(self, data: bytes):
        self.sock.sendall(data)


class StorageBasedGoofyIo(GoofyIo):
    """
    base class for `GoofyIo`s that use a file storage system for data transfer
    (creating and reading files for sending and receiving), whether local or on
    the cloud. subclasses must implement abstract functions `_name`,
    `_format_path`, `_unformat_path`, `_list_files`, `_download_files`,
    `_upload_files`, and `_delete_files`.

    NOTE: the two peers must use synchronized clocks.

    Args:
        id (str):
            sender ID to include in outgoing files so the other side knows who
            sent it.

        peer_id (str):
            sender ID of the peer. any incoming file with a different sender ID
            will be ignored.

        max_out_size (int):
            maximum outgoing file size in bytes.

        interval (float):
            send-receive loop interval in seconds.

        max_file_age (float):
            files older than this many seconds will be deleted. prefer using
            identical values for the sender and receiver.

        log (logging.Logger):
            logger
    """

    class InPacket(NamedTuple):
        idx: int
        data: bytes

    @dataclass
    class File:
        path: str
        "file path"

        data: bytes | None = None
        "binary content of the file (optional)"

    id: str
    peer_id: str
    max_out_size: int
    interval: float
    max_file_age: float
    log: logging.Logger

    _last_out_time: float = 0.
    _out_idx: int = 0
    _out_buf: bytearray
    _out_buf_lock: threading.Lock

    _last_in_time: float = 0.
    _in_packets: list[InPacket]
    _in_packets_lock: threading.Lock

    _in_idx: int = 0
    _in_buf: bytearray
    _in_buf_lock: threading.Lock

    _thread: threading.Thread | None = None
    _stopping: bool = False

    def _name(self) -> str:
        """
        abstract function returning the name of the method used for data
        transfer or the name of the subclass. example: "TxtFileIo".
        """
        raise NotImplementedError()

    def _format_path(
        self,
        sender_id: str,
        peer_id: str,
        packet_idx: int,
        timestamp: float
    ) -> str:
        """
        abstract function for constructing a file path string based on given
        sender ID, peer/receiver ID, packet index, and timestamp.
        """
        raise NotImplementedError()

    def _unformat_path(self, path: str) -> tuple[str, str, int, float] | None:
        """
        abstract function that parses a file path and returns a tuple containing
        the sender ID, peer/receiver ID, packet index, and timestamp. if parsing
        fails, it must return `None` and not raise any exceptions.
        """
        raise NotImplementedError()

    def _list_files(self) -> list[StorageBasedGoofyIo.File]:
        """
        abstract function returning the list of all files in the storage used
        for data transfer, regardless of the sender and receiver ID. avoid
        including unrelated files not used by `StorageBasedGoofyIo`.

        Returns:
            a list of `StorageBasedGoofyIo.File` objects with values for the
            `path` field, but not `data` (file contents should not be be
            downloaded).
        """
        raise NotImplementedError()

    def _download_files(self, files: list[StorageBasedGoofyIo.File]):
        """
        abstract function which sets the `data` field in-place for given files
        by downloading them. if the `data` field is already set, it is optional
        to re-download.

        Args:
            files (list[StorageBasedGoofyIo.File]):
                list of `StorageBasedGoofyIo.File` objects whose `data` fields
                should be retreived.
        """
        raise NotImplementedError()

    def _upload_files(self, files: list[StorageBasedGoofyIo.File]):
        """
        abstract function for uploading new files to the storage. if the `data`
        field is set to `None` for any of the files, a `ValueError` must be
        raised.

        Args:
            files (list[StorageBasedGoofyIo.File]):
                list of files to upload to the storage.
        """
        raise NotImplementedError()

    def _delete_files(self, paths: list[str]):
        """
        abstract function for deleting files with given paths from the storage.
        if any path is not found on the storage, it must be discarded silently
        without raising an exception.

        Args:
            files (list[str]):
                list of paths for files to delete from the storage.
        """
        raise NotImplementedError()

    def __init__(
        self,
        id: str,
        peer_id: str,
        max_out_size: int = 200 * 1024,
        interval: float = .2,
        max_file_age: float = 29.5,
        log: logging.Logger | None = None
    ):
        self._validate_id(id)
        self._validate_id(peer_id)

        self.id = id
        self.peer_id = peer_id
        self.max_out_size = int(max_out_size)
        self.interval = float(interval)
        self.max_file_age = float(max_file_age)
        if log is None:
            self.log = make_logger("StorageBasedGoofyIo")
        else:
            self.log = log

        self._out_buf = bytearray()
        self._out_buf_lock = threading.Lock()
        self._in_packets = []
        self._in_packets_lock = threading.Lock()
        self._in_buf = bytearray()
        self._in_buf_lock = threading.Lock()

        # clean up old files
        to_be_deleted: list[str] = []
        for file in self._list_files():
            unformat_result = self._unformat_path(file.path)
            if unformat_result is None:
                continue
            _, _, _, timestamp = unformat_result
            if time.time() - timestamp > self.max_file_age:
                to_be_deleted.append(file.path)
        self._delete_files(to_be_deleted)
        self.log.info(f"deleted {len(to_be_deleted)} old files.")

        # start the background thread for sending and receiving
        self._thread = threading.Thread(
            name=f"{self._name()} thread",
            target=self._thread_run,
            daemon=True
        )
        self._thread.start()

    def __del__(self):
        self.stop()

    def running(self) -> bool:
        return not self._stopping

    def stop(self):
        global keyboard_interrupt
        if self._stopping:
            return
        self._stopping = True

    def _receive(self, size: int) -> bytes:
        if self._last_in_time < .01:
            self._last_in_time = time.time()

        while True:
            if not self.running():
                raise ConnectionError(f"{self._name()} has stopped.")

            if time.time() - self._last_in_time > self.max_file_age:
                raise TimeoutError(
                    f"{self._name()} received no packets in over "
                    f"max_file_age={self.max_file_age} seconds."
                )

            poll_interval = min(.02, self.interval)

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
            raise ConnectionError(f"{self._name()} has stopped.")

        force_acquire(self._out_buf_lock)
        self._out_buf += data
        self._out_buf_lock.release()

    def _thread_run(self):
        global keyboard_interrupt
        try:
            while not self._stopping:
                time_start = time.time()

                send_future = ThreadPool.enqueue(
                    self._send_packet_if_needed
                )
                receive_future = ThreadPool.enqueue(
                    self._receive_new_packets
                )
                send_future.get()
                receive_future.get()

                remaining_time = time_start + self.interval - time.time()
                if remaining_time > 0.:
                    time.sleep(remaining_time)
        except KeyboardInterrupt as e:
            keyboard_interrupt = e
        except BaseException as e:
            if not self._stopping:
                self.log.fatal(format_exception(e))
        finally:
            self.stop()

    def _compress_out_data_if_worth_it(self) -> tuple[bytes, bool]:
        force_acquire(self._out_buf_lock)

        orig_size = self.max_out_size
        data = bytes(self._out_buf[:orig_size])
        is_compressed: bool = False

        for ratio in [3, 2, 1.5, 1]:
            temp_orig_size = min(
                int(self.max_out_size * ratio),
                len(self._out_buf)
            )
            temp = gzip.compress(self._out_buf[:temp_orig_size])
            if len(temp) < min(temp_orig_size, self.max_out_size):
                orig_size = temp_orig_size
                data = temp
                is_compressed = True
                break

        self._out_buf = self._out_buf[orig_size:]
        self._out_buf_lock.release()

        return data, is_compressed

    def _send_packet_if_needed(self):
        if self._stopping:
            return

        data, is_compressed = self._compress_out_data_if_worth_it()

        elapsed_since_last_packet = time.time() - self._last_out_time
        if not data and (
            elapsed_since_last_packet < self.max_file_age / 3.
            or self._out_idx == 0
        ):
            return
        elif not data:
            # too much time passed with no outgoing packets, so we send an empty
            # packet to say we're alive.
            data = b""

        self._last_out_time = time.time()

        if data and is_compressed:
            data = b"C" + data
        elif data:
            data = b"c" + data

        path = self._format_path(
            self.id,
            self.peer_id,
            self._out_idx,
            time.time()
        )
        self._out_idx += 1

        try:
            self._upload_files([
                StorageBasedGoofyIo.File(path, data)
            ])
        except Exception as e:
            raise ConnectionError(
                f"{self._name()} failed to upload \"{path}\": "
                f"{format_exception(e)}"
            )

    def _receive_new_packets(self):
        if self._stopping:
            return

        to_be_deleted: list[str] = []

        # check new files
        for file in self._list_files():
            # extract sender, receiver, packet index, and timestamp
            unformat_result = self._unformat_path(file.path)
            if unformat_result is None:
                continue
            sender, receiver, packet_idx, timestamp = unformat_result

            # skip and delete old files
            if time.time() - timestamp > self.max_file_age:
                to_be_deleted.append(file.path)
                continue

            # skip if the packet isn't sent from the peer to us
            if sender != self.peer_id or receiver != self.id:
                continue

            # ignore and delete packets we've already read
            if packet_idx < self._in_idx:
                to_be_deleted.append(file.path)
                continue

            # skip if the packet index is too far ahead. for the first 4
            # packets, it must be identical to _in_idx, after that it must be
            # 16 or less indices ahead of _in_idx.
            if (self._in_idx < 4 and packet_idx != self._in_idx) or \
                    (packet_idx - self._in_idx > 16):
                continue

            # download the file
            try:
                self._download_files([file])
            except Exception as e:
                if not self._stopping:
                    self.log.warning(
                        f"failed to download \"{file.path}\": "
                        f"{format_exception(e)}"
                    )
                continue

            # successful read, can be safely deleted now
            to_be_deleted.append(file.path)

            # update the last incoming packet receive time
            self._last_in_time = time.time()

            # decompress if needed
            if file.data and file.data[0] == ord('C'):
                file.data = gzip.decompress(file.data[1:])
            elif file.data and file.data[0] == ord('c'):
                file.data = file.data[1:]

            # add to incoming packets (remove older ones with the same index)
            force_acquire(self._in_packets_lock)
            for i in range(len(self._in_packets)):
                packet = self._in_packets[i]
                if packet.idx == packet_idx:
                    self._in_packets.pop(i)
                    i -= 1
            self._in_packets.append(
                StorageBasedGoofyIo.InPacket(packet_idx, file.data)
            )
            self._in_packets_lock.release()

        # delete old or invalid files
        self._delete_files(to_be_deleted)

        # sort incoming packets and add data to the input buffer

        force_acquire(self._in_packets_lock)

        self._in_packets = list(filter(
            lambda p: p.idx >= self._in_idx,
            self._in_packets
        ))
        self._in_packets.sort(key=lambda p: p.idx)

        if not self._in_packets:
            self._in_packets_lock.release()
            return

        force_acquire(self._in_buf_lock)
        while self._in_packets \
                and self._in_packets[0].idx == self._in_idx:
            self._in_idx += 1
            self._in_buf += self._in_packets[0].data
            self._in_packets = self._in_packets[1:]
        self._in_buf_lock.release()

        self._in_packets_lock.release()

    def _validate_id(self, id: str):
        ID_VALID_CHARS = \
            "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
        try:
            if not isinstance(id, str):
                raise Exception(
                    f"must be a string, not {type(id)}"
                )
            if not id:
                raise Exception("cannot be empty")
            if len(id) > 64:
                raise ValueError("cannot contain more than 64 characters")
            for c in id:
                if c not in ID_VALID_CHARS:
                    raise Exception(
                        "can only contain Latin letters, digits, '-', and '_'"
                    )
            if id[0] in "-_" or id[-1] in "-_":
                raise Exception("cannot start or end with '-' or '_'")
        except Exception as e:
            raise ValueError(
                f"invalid user ID \"{id}\": {e}"
            )


class TxtFileIo(GoofyIo):
    """
    an example `GoofyIo` that creates and reads .txt files with base85 encoding
    and gzip compression to transfer data.
    """

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
