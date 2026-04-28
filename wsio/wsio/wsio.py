"""
provides `WsIo`, a `GoofyIo` child class for data transfer through WebSocket
connections.
"""

import time
import threading
import random
import gzip
import base64

import requests
from http import HTTPStatus
import websocket
import ssl
from urllib.parse import urlencode
import urllib3

from goofyproxy import GoofyIo
from goofyproxy.common import *

N_RETRIES: int = 10
RETRY_INTERVAL: float = 2.


class WsIoError(IOError):
    pass


class WsClient:
    """
    a thin wrapper around WebSocket from websocket-client.

    Args:
        url (str): WebSocket URL (ws:// or wss://)
        headers (list | dict | None): optional HTTP headers
        ssl_verify (bool): whether to verify SSL certificates
    """

    ws: websocket.WebSocket

    def __init__(
        self,
        url: str,
        params: dict | None = None,
        headers: list | dict | None = None,
        ssl_verify: bool = True
    ):
        # configure SSL options
        ssl_opts = {
            "cert_reqs": ssl.CERT_REQUIRED if ssl_verify else ssl.CERT_NONE
        }

        url = url.replace("https://", "wss://").replace("http://", "ws://")
        if params is not None:
            url += "?" + urlencode(params)

        # connect
        self.ws = websocket.WebSocket(
            sslopt=ssl_opts,
            enable_multithread=False  # ensure sync behavior
        )
        self.ws.connect(
            url,
            header={} if headers is None else headers
        )

    def read(self) -> str | bytes:
        """
        read a new message from the WebSocket.

        Returns:
            received data as bytes or string depending on the frame type.
        """
        if not self.ws.connected:
            raise ConnectionError("WebSocket is not connected")
        return self.ws.recv()

    def send(self, data):
        """
        send data through the WebSocket.

        Args:
            data (str | bytes): what to send
        """
        if not self.ws.connected:
            raise RuntimeError("WebSocket is not connected")
        if isinstance(data, bytes):
            # binary frame
            self.ws.send_binary(data)
        else:
            # text frame
            self.ws.send(str(data))

    def close(self):
        """close the connection."""
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass

    def __enter__(self):
        """context manager entry"""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """context manager exit"""
        self.close()


class WsIo(GoofyIo):
    """
    a `GoofyIo` that transfers data through WsIo, a very basic WebSocket-based
    binary call service. note that this only works with Node.js servers running
    the WsIo server.

    Args:

        url (str):
            server URL (HTTPS) ending with a slash.
            example: `https://example.com/wsio/`

        id (str):
            user ID to send packets as. a new user will be created if the ID
            doesn't match an existing one.

        password (str):
            password for the provided user ID.

        peer_id (str | None):
            user ID to communicate with. optional only if is_caller is False.

        is_caller (bool):
            if True, will call the peer through the WsIo server. otherwise, will
            wait for an incoming call from the peer (or anyone if peer_id is
            `None`).

        interval_min (float):
            minimum delay in seconds between each iteration of the send-receive
            loop.

        interval_max (float):
            maximum delay in seconds between each iteration of the send-receive
            loop.

        max_out_packet_size (int):
            maximum outgoing packet size (in bytes) in each iteration of the
            send-receive loop.

        warm_up (bool):
            send a few dummy requests to the server with random delays between
            them before starting or waiting for a call.

        ssl_verify (bool):
            enable SSL certificate verification (recommended).

        log_level (int | None):
            logging level (e.g. `logging.INFO`)
    """

    _log: logging.Logger

    url: str
    id: str
    password: str
    peer_id: str | None
    is_caller: bool
    interval_min: float
    interval_max: float
    max_out_packet_size: int
    warm_up: bool
    ssl_verify: bool

    _auth_code: str

    _out_buf: bytearray
    _out_buf_lock: threading.Lock

    _in_buf: bytearray
    _in_buf_lock: threading.Lock

    _thread: threading.Thread
    _stopping: bool = False

    _call_timestamp: int = 0
    _ws: WsClient | None = None

    def __init__(
        self,
        url: str,
        id: str,
        password: str,
        peer_id: str | None,
        is_caller: bool,
        interval_min: float = .1,
        interval_max: float = .5,
        max_out_packet_size: int = 512 * 1024,
        warm_up: bool = True,
        ssl_verify: bool = True,
        log_level: int | None = None
    ):
        validate_server_url(url)
        validate_id(id)
        validate_password(password)

        if peer_id is not None:
            validate_id(peer_id)
            if peer_id == id:
                raise ValueError(
                    f"id and peer_id must be different (both are {id})"
                )

        self._log = make_logger(f"WsIo", log_level)

        self.url = url
        self.id = id
        self.password = password
        self.peer_id = peer_id
        self.is_caller = is_caller
        self.interval_min = float(interval_min)
        self.interval_max = float(interval_max)
        self.max_out_packet_size = int(max_out_packet_size)
        self.warm_up = warm_up
        self.ssl_verify = ssl_verify

        if self.interval_max < self.interval_min:
            raise ValueError(
                f"interval_max={self.interval_max} must be larger than or "
                f"equal to interval_min={self.interval_min}."
            )

        avg_interval = (self.interval_min + self.interval_max) * .5
        out_data_rate = self.max_out_packet_size / avg_interval
        self._log.info(
            f"URL: {self.url}\n"
            f"ID: {self.id}\n"
            f"peer ID: {self.peer_id}\n"
            f"interval range: {self.interval_min}-{self.interval_max} s\n"
            f"max. outgoing packet size: "
            f"{format_data_size(self.max_out_packet_size)}\n"
            f"outgoing data rate: ~{format_data_rate(out_data_rate)}"
        )

        if not self.ssl_verify:
            # disable SSL warnings and log a single warning ourselves
            urllib3.disable_warnings()
            self._log.warning(
                "WARNING: you have disabled SSL certificate verification. your "
                "connection is vulnerable to man-in-the-middle attacks."
            )

        self._out_buf = bytearray()
        self._out_buf_lock = threading.Lock()
        self._in_buf = bytearray()
        self._in_buf_lock = threading.Lock()

        # authentication

        self._auth_code = generate_auth_code(self.id, self.password)
        res = self._request_json(
            "prepare",
            {
                "auth": self._auth_code,
                "peer": peer_id
            }
        )

        auth_result = res["authResult"]
        if auth_result == "ok":
            self._log.info("authentication was successful")
        elif auth_result == "ok-created":
            self._log.info("authentication was successful (created new user)")
        else:
            raise WsIoError(f"authentication failed: {auth_result}")

        # continue in a separate thread
        self._thread = threading.Thread(
            name="WsIo thread",
            target=self._thread_run,
            daemon=True
        )
        self._thread.start()

    def running(self) -> bool:
        return not self._stopping

    def stop(self):
        if self._stopping:
            return
        self._stopping = True
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass

    def _receive(self, size: int) -> bytes:
        while True:
            if not self.running():
                raise ConnectionError("not running")

            poll_interval = min(.05, self.interval_min)

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

    def _thread_run(self):
        try:
            # warm up
            if self.warm_up:
                self._log.info("warming up")
                n = random.randint(2, 6)
                for _ in range(n):
                    time.sleep(.5 + 1.8 * random.random())
                    self._dummy_request("dummy")

            if self.is_caller:
                # call the peer
                if self.peer_id is None:
                    raise ValueError(
                        "peer_id is required when is_caller is True (who are "
                        "we supposed to call?)"
                    )
                self._log.info("calling peer")
                self._ws = WsClient(
                    f"{self.url}call",
                    {
                        "auth": self._auth_code,
                        "peer": self.peer_id
                    },
                    ssl_verify=self.ssl_verify
                )
                msg = self._ws.read()
                if msg == "call-start":
                    self._log.info("call started")
                else:
                    raise ConnectionAbortedError(
                        f"couldn't start call: {msg}"
                    )
            else:
                # wait for an incoming call from the peer (or anyone if peer_id
                # is None).

                if self.peer_id is None:
                    self._log.info("waiting for an incoming call")
                else:
                    self._log.info(
                        f"waiting for an incoming call from {self.peer_id}"
                    )

                while True:
                    res = self._request_json(
                        "whos-calling",
                        {"auth": self._auth_code}
                    )
                    auth_result = res["authResult"]
                    if auth_result != "ok":
                        raise WsIoError(
                            f"authentication failed: {auth_result}"
                        )
                    calls = res["calls"]

                    if not calls:
                        time.sleep(5.)
                        continue

                    if self.peer_id is None:
                        self.peer_id = calls[0]["caller"]
                        self._call_timestamp = calls[0]["timestamp"]
                        break

                    found_call = False
                    for call in calls:
                        if call["caller"] == self.peer_id:
                            self._call_timestamp = call["timestamp"]
                            found_call = True
                            break
                    if not found_call:
                        time.sleep(5.)
                        continue

                    break

                self._log.info(f"answering incoming call from {self.peer_id}")
                self._ws = WsClient(
                    f"{self.url}pickup",
                    {
                        "auth": self._auth_code,
                        "peer": self.peer_id
                    },
                    ssl_verify=self.ssl_verify
                )
                msg = self._ws.read()
                if msg == "call-start":
                    self._log.info("call started")
                else:
                    raise ConnectionAbortedError(
                        f"couldn't start call: {msg}"
                    )

            # send and receive
            while not self._stopping:
                time.sleep(
                    self.interval_min
                    + random.random() * (self.interval_max - self.interval_min)
                )

                self._send_packet_if_needed()
                if is_ready_to_read(self._ws.ws.sock):
                    self._read_packet()
        except BaseException as e:
            self._log.fatal(format_exception(e))
        finally:
            self.stop()

    def _send_packet_if_needed(self):
        force_acquire(self._out_buf_lock)

        # use compression if it's worth it
        data_orig_size = self.max_out_packet_size
        data = bytes(self._out_buf[:data_orig_size])
        is_compressed = False
        if len(self._out_buf) > self.max_out_packet_size:
            orig_size = self.max_out_packet_size * 3
            temp = gzip.compress(self._out_buf[:orig_size])
            if len(temp) < self.max_out_packet_size:
                data_orig_size = orig_size
                data = temp
                is_compressed = True

            if not is_compressed:
                orig_size = self.max_out_packet_size * 2
                temp = gzip.compress(self._out_buf[:orig_size])
                if len(temp) < self.max_out_packet_size:
                    data_orig_size = orig_size
                    data = temp
                    is_compressed = True

            if not is_compressed:
                orig_size = self.max_out_packet_size * 3 // 2
                temp = gzip.compress(self._out_buf[:orig_size])
                if len(temp) < self.max_out_packet_size:
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

        self._ws.send(data)

    def _read_packet(self):
        data = self._ws.read()
        if isinstance(data, str):
            self._log.warning(f"received text frame: \"{data}\"")
            return
        elif not isinstance(data, bytes):
            raise ValueError("received data is neither a str or bytes")

        if not data:
            return

        # decompress if needed
        if data[0] == b"C":
            data = gzip.decompress(data[1:])
        else:
            data = data[1:]

        # push data to the input buffer
        force_acquire(self._in_buf_lock)
        self._in_buf += data
        self._in_buf_lock.release()

    def _dummy_request(self, path: str):
        try:
            requests.get(
                f"{self.url}{path}",
                timeout=20.,
                allow_redirects=False,
                verify=self.ssl_verify
            )
        except Exception:
            pass

    def _request(self, path: str, params: dict) -> requests.Response:
        for i in range(N_RETRIES + 1):
            try:
                if i > 0:
                    time.sleep(RETRY_INTERVAL)

                res = requests.get(
                    f"{self.url}{path}",
                    params,
                    timeout=20.,
                    allow_redirects=False,
                    verify=self.ssl_verify
                )

                if res.status_code != 200:
                    try:
                        phrase = " " + HTTPStatus(res.status_code).phrase
                    except ValueError:
                        phrase = ""
                    raise Exception(f"{res.status_code}{phrase}")

                return res
            except Exception as e:
                self._log.error(
                    f"\"{path}\" failed ({i}/{N_RETRIES}): "
                    f"{format_exception(e)}"
                )
        raise ConnectionError(f"too many failures in \"{path}\"")

    def _request_json(self, path: str, params: dict) -> dict:
        j = self._request(path, params).json()
        if not isinstance(j, dict):
            raise ValueError("response JSON is not a dict")
        return j


def delete_account(
    url: str,
    id: str,
    password: str,
    ssl_verify: bool = True
):
    """
    delete WsIo user account with given ID and password.

    Args:

      url (str):
          server URL (HTTPS) ending with a slash.
          example: `https://example.com/wsio/`

      id (str):
          ID of the user account to delete.

      password (str):
          password for the provided user ID.

      ssl_verify (bool):
          enable SSL certificate verification (recommended).
    """

    validate_server_url(url)
    validate_id(id)
    validate_password(password)

    auth_code = generate_auth_code(id, password)
    res = requests.get(
        f"{url}delete-acc",
        {"auth": auth_code},
        timeout=20.,
        allow_redirects=False,
        verify=ssl_verify
    )
    res = dict(res.json())

    auth_result = res["authResult"]
    if auth_result != "ok":
        raise WsIoError(f"authentication failed: {auth_result}")

    delete_result = res["deleteResult"]
    if delete_result != "ok":
        raise WsIoError(
            f"account deletion failed: {delete_result}"
        )


ID_VALID_CHARS = \
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"


def validate_server_url(url: str):
    if not url.startswith("https://"):
        raise ValueError("server URL must start with \"https://\"")
    if not url.endswith("/"):
        raise ValueError("server URL must end with \"/\"")


def validate_id(id: str):
    try:
        if not isinstance(id, str):
            raise Exception("must be a string")
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


def validate_password(password: str):
    if not isinstance(password, str):
        raise ValueError("password must be a string")
    if len(password) < 10:
        raise ValueError("password must be at least 10 characters long")
    if len(password) > 64:
        raise ValueError("password cannot contain more than 64 characters")


def generate_auth_code(id: str, password: str) -> str:
    validate_id(id)
    validate_password(password)
    id = id.encode()
    password = password.encode()

    raw = len(id).to_bytes(1) + id + len(password).to_bytes(1) + password
    return base64.b64encode(raw)
