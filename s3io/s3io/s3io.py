"""
provides `S3Io`, a `StorageBasedGoofyIo` that creates and reads files on an AWS
S3 "Bucket" to send and receive binary data.
"""

import io
import logging

import boto3
from goofyproxy import StorageBasedGoofyIo
from goofyproxy.common import *


MAX_FILE_AGE: float = 29.5
"delete s3io files older than this many seconds."


class S3Io(StorageBasedGoofyIo):
    """
    a `StorageBasedGoofyIo` that creates and reads files on an AWS S3 "Bucket"
    to send and receive binary data.

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

        max_out_size (int):
            maximum outgoing file size in bytes.

        interval (float):
            minimum delay in seconds between outgoing files.

        log_level (int | None):
            logging level (e.g. `logging.INFO`)
    """

    endpoint_url: str
    access_key: str
    secret_key: str
    bucket_name: str

    _log: logging.Logger
    _bucket: object

    def __init__(
        self,
        endpoint_url: str,
        access_key: str,
        secret_key: str,
        bucket_name: str,
        id: str,
        peer_id: str,
        max_out_size: int = 200 * 1024,
        interval: float = .2,
        log_level: int | None = None
    ):
        self._log = make_logger(f"s3io", log_level)
        self._log.warning(
            "[IMPORTANT NOTICE] your system clock must be accurate to the "
            "second for s3io to work properly."
        )

        self.endpoint_url = endpoint_url
        self.access_key = access_key
        self.secret_key = secret_key
        self.bucket_name = bucket_name

        # initialize S3 bucket
        s3_resource = boto3.resource(
            's3',
            endpoint_url=self.endpoint_url,
            aws_access_key_id=self.access_key,
            aws_secret_access_key=self.secret_key
        )
        self._bucket = s3_resource.Bucket(self.bucket_name)

        # initialize super
        super().__init__(
            id,
            peer_id,
            max_out_size,
            interval,
            MAX_FILE_AGE,
            self._log
        )

    def _name(self) -> str:
        return "s3io"

    def _format_path(
        self,
        sender_id: str,
        peer_id: str,
        packet_idx: int
    ) -> str:
        return f"s3io#{sender_id}#{peer_id}#{packet_idx}"

    def _unformat_path(self, path: str) -> tuple[str, str, int] | None:
        try:
            if not path.startswith("s3io#"):
                return None

            parts = path.split("#")
            if len(parts) < 4:
                return None

            sender = parts[1]
            receiver = parts[2]
            packet_idx = int(parts[3])
            return sender, receiver, packet_idx
        except Exception:
            return None

    def _list_files(self) -> list[StorageBasedGoofyIo.File]:
        files: list[StorageBasedGoofyIo.File] = []
        for obj in self._bucket.objects.all():
            if not str(obj.key).startswith("s3io#"):
                continue
            files.append(StorageBasedGoofyIo.File(
                obj.key,
                obj.last_modified.timestamp()
            ))
        return files

    def _download_files(self, files: list[StorageBasedGoofyIo.File]):
        for file in files:
            bytes_io = io.BytesIO()
            self._bucket.download_fileobj(file.path, bytes_io)
            bytes_io.seek(0)
            file.data = bytes_io.getvalue()

    def _upload_files(self, files: list[StorageBasedGoofyIo.File]):
        for file in files:
            if file.data is None:
                raise ValueError(
                    f"cannot upload file \"{file.path}\" with no data"
                )
            self._bucket.put_object(
                Key=file.path,
                Body=file.data,
                ACL='private'
            )

    def _delete_files(self, paths: list[str]):
        self._bucket.delete_objects(
            Delete={
                "Objects": list(
                    [{"Key": path} for path in paths]
                ),
                "Quiet": True
            },
            RequestPayer="requester",
            BypassGovernanceRetention=False
        )
