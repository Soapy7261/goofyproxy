"""
provides `ChatIo`, a `GoofyIo` child class that uses a chat client in a web
browser to send and receive binary data embedded in file messages.
"""

import io
import time
from datetime import datetime, timezone
import threading
import logging
import gzip
from typing import NamedTuple

import boto3
from goofyproxy import GoofyIo
from goofyproxy.common import *


MAX_FILE_AGE: int = 30
"delete s3io files older than this many seconds."

IN_IDX_THRESHOLD: int = 30
"if we miss this many or more incoming packets, we will raise an error."


class InPacket(NamedTuple):
    idx: int
    data: bytes


class S3Io(GoofyIo):
    """
    a `GoofyIo` that creates and reads files on an AWS S3 "Bucket" to send and
    receive binary data.

    Args:
        endpoint_url (str):
            S3 endpoint URL

        access_key (str):
            S3 access key

        secret_key (str):
            S3 secret key

        bucket_name (str):
            S3 bucket name

        id (str):
            sender ID to include in outgoing files so the other side knows who
            sent it.

        peer_id (str):
            sender ID of the peer. any incoming file with a different sender ID
            will be ignored.

        interval (float):
            send-receive cycle interval in seconds.

        max_out_size (int):
            maximum outgoing file size in bytes.

        log_level (int | None):
            logging level (e.g. `logging.INFO`)
    """

    _log: logging.Logger

    endpoint_url: str
    access_key: str
    secret_key: str
    bucket_name: str
    id: str
    peer_id: str
    interval: float
    max_out_size: int

    _out_idx: int = 0
    _out_buf: bytearray
    _out_buf_lock: threading.Lock

    _in_packets: list[InPacket]
    _in_packets_lock: threading.Lock

    _in_idx: int = 0
    _in_buf: bytearray
    _in_buf_lock: threading.Lock

    _bucket: object

    _thread: threading.Thread | None = None
    _stopping: bool = False

    def __init__(
        self,
        endpoint_url: str,
        access_key: str,
        secret_key: str,
        bucket_name: str,
        id: str,
        peer_id: str,
        interval: float = .05,
        max_out_size: int = 64 * 1024,
        log_level: int | None = None
    ):
        validate_id(id)
        validate_id(peer_id)

        self._log = make_logger(f"s3io", log_level)

        self.endpoint_url = endpoint_url
        self.access_key = access_key
        self.secret_key = secret_key
        self.bucket_name = bucket_name
        self.id = id
        self.peer_id = peer_id
        self.interval = float(interval)
        self.max_out_size = int(max_out_size)

        self._out_buf = bytearray()
        self._out_buf_lock = threading.Lock()
        self._in_packets = []
        self._in_packets_lock = threading.Lock()
        self._in_buf = bytearray()
        self._in_buf_lock = threading.Lock()

        # initialize S3 bucket
        s3_resource = boto3.resource(
            's3',
            endpoint_url=self.endpoint_url,
            aws_access_key_id=self.access_key,
            aws_secret_access_key=self.secret_key
        )
        self._bucket = s3_resource.Bucket(self.bucket_name)

        # clean up old s3io files
        to_be_deleted: list[str] = []
        for obj in self._bucket.objects.all():
            elapsed_sec = (
                datetime.now(timezone.utc) - obj.last_modified
            ).total_seconds()
            if str(obj.key).startswith("s3io#") and elapsed_sec > MAX_FILE_AGE:
                to_be_deleted.append(obj.key)
        self._bucket.delete_objects(
            Delete={
                "Objects": list(
                    [{"Key": key} for key in to_be_deleted]
                ),
                "Quiet": True
            },
            RequestPayer="requester",
            BypassGovernanceRetention=False
        )
        self._log.info(f"deleted {len(to_be_deleted)} old s3io files.")

        # start the background thread for sending outgoing files and receiving
        # incoming files.
        self._thread = threading.Thread(
            name="s3io thread",
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
        while True:
            if not self.running():
                raise ConnectionError("s3io has stopped")

            poll_interval = min(.1, self.interval)

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
            raise ConnectionError("s3io has stopped")

        force_acquire(self._out_buf_lock)
        self._out_buf += data
        self._out_buf_lock.release()

    def _thread_run(self):
        global keyboard_interrupt
        try:
            while not self._stopping:
                time.sleep(self.interval)
                self._send_packet_if_needed()
                self._receive_new_packets()
        except KeyboardInterrupt as e:
            keyboard_interrupt = e
        except BaseException as e:
            self._log.fatal(format_exception(e))
        finally:
            self.stop()

    def _send_packet_if_needed(self):
        if self._stopping:
            return

        force_acquire(self._out_buf_lock)

        # use compression if it's worth it
        data_orig_size = self.max_out_size
        data = bytes(self._out_buf[:data_orig_size])
        is_compressed = False
        if len(self._out_buf) > self.max_out_size:
            orig_size = self.max_out_size * 3
            temp = gzip.compress(self._out_buf[:orig_size])
            if len(temp) < self.max_out_size:
                data_orig_size = orig_size
                data = temp
                is_compressed = True

            if not is_compressed:
                orig_size = self.max_out_size * 2
                temp = gzip.compress(self._out_buf[:orig_size])
                if len(temp) < self.max_out_size:
                    data_orig_size = orig_size
                    data = temp
                    is_compressed = True

            if not is_compressed:
                orig_size = self.max_out_size * 3 // 2
                temp = gzip.compress(self._out_buf[:orig_size])
                if len(temp) < self.max_out_size:
                    data_orig_size = orig_size
                    data = temp
                    is_compressed = True
        self._out_buf = \
            self._out_buf[data_orig_size:]

        self._out_buf_lock.release()

        if not data:
            return

        if is_compressed:
            data = b"C" + data
        else:
            data = b"c" + data

        path = f"s3io#{self.id}#{self.peer_id}#{self._out_idx}"
        self._out_idx += 1

        try:
            self._bucket.put_object(
                Key=path,
                Body=data,
                ACL='private'
            )
        except Exception as e:
            raise ConnectionError(
                f"failed to upload file \"{path}\": {format_exception(e)}"
            )

    def _receive_new_packets(self):
        if self._stopping:
            return

        to_be_deleted: list[str] = []

        # check new files
        for obj in self._bucket.objects.all():
            path = str(obj.key)
            if not path.startswith("s3io#"):
                continue

            # delete old s3io files
            elapsed_sec = (
                datetime.now(timezone.utc) - obj.last_modified
            ).total_seconds()
            if elapsed_sec > MAX_FILE_AGE:
                to_be_deleted.append(path)
                continue

            # extract sender, receiver, and packet index

            parts = path.split("#")
            if len(parts) < 4:
                continue

            sender = parts[1]
            receiver = parts[2]
            try:
                packet_idx = int(parts[3])
            except Exception:
                continue

            # skip if the packet isn't sent from the peer to us
            if sender != self.peer_id or receiver != self.id:
                continue

            # ignore and delete packets we've already read
            if packet_idx < self._in_idx:
                to_be_deleted.append(path)
                continue

            # download the file
            try:
                bytes_io = io.BytesIO()
                self._bucket.download_fileobj(path, bytes_io)
                bytes_io.seek(0)
                data = bytes_io.getvalue()
            except Exception as e:
                self._log.warning(
                    f"failed to download file \"{path}\": {format_exception(e)}"
                )
                continue

            # successful read, can be safely deleted now
            to_be_deleted.append(path)

            # decompress if needed
            if data and data[0] == b"C":
                data = gzip.decompress(data[1:])
            elif data:
                data = data[1:]

            # add to incoming packets
            force_acquire(self._in_packets_lock)
            already_exists = False
            for packet in self._in_packets:
                if packet.idx == packet_idx:
                    self._in_packets_lock.release()
                    already_exists = True
                    break
            if already_exists:
                continue
            self._in_packets.append(InPacket(packet_idx, data))
            self._in_packets_lock.release()

        # delete old or invalid files
        self._bucket.delete_objects(
            Delete={
                "Objects": list(
                    [{"Key": key} for key in to_be_deleted]
                ),
                "Quiet": True
            },
            RequestPayer="requester",
            BypassGovernanceRetention=False
        )

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

        if self._in_packets[0].idx - self._in_idx > IN_IDX_THRESHOLD:
            self._in_packets_lock.release()
            raise ConnectionError(
                f"missing too many ({IN_IDX_THRESHOLD}+) incoming files."
            )

        force_acquire(self._in_buf_lock)
        while self._in_packets \
                and self._in_packets[0].idx == self._in_idx:
            self._in_idx += 1
            self._in_buf += self._in_packets[0].data
            self._in_packets = self._in_packets[1:]
        self._in_buf_lock.release()

        self._in_packets_lock.release()


ID_VALID_CHARS = \
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"


def validate_id(id: str):
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
