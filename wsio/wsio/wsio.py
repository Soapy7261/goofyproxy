"""
provides `WsIo`, a `GoofyIo` child class for data transfer through WebSocket
connections.
"""

import time
import threading
import random
import gzip
import base64
from typing import NamedTuple
import logging

import requests
from http import HTTPStatus
import websocket
import ssl
from urllib.parse import urlencode
import urllib3

from goofyproxy import GoofyIo
from goofyproxy.common import *


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

        http_prefix = "http://"
        https_prefix = "https://"
        if url.startswith(https_prefix):
            url = "wss://" + url[len(https_prefix):]
        elif url.startswith(http_prefix):
            url = "ws://" + url[len(http_prefix):]

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
        """close the connection gracefully."""
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass

    def shutdown(self):
        """close the underlying socket immediately."""
        if self.ws:
            try:
                self.ws.shutdown()
            except Exception:
                pass

    def __enter__(self):
        """context manager entry"""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """context manager exit"""
        self.close()


class CallerMode(NamedTuple):
    who_to_call: str
    """user ID to call"""


class CalleeMode(NamedTuple):
    accept_calls_from: list[str]
    """a list of user IDs to wait for an incoming call from"""

    block_mode: bool = False
    """
    if True, will accept incoming calls from anyone except the IDs in
    `accept_calls_from`.
    """


class WsIo(GoofyIo):
    """
    a `GoofyIo` that transfers data through WsIo, a very basic WebSocket-based
    binary call service. note that this only works with Node.js servers running
    the WsIo server.

    Args:

        url (str):
            WsIo server URL (HTTPS or HTTP) ending with a slash.
            example: `https://example.com/wsio/`

        id (str):
            user ID to send packets as. a new user will be created if the ID
            doesn't match an existing one.

        password (str):
            password for the provided user ID.

        call_mode (CallerMode | CalleeMode):
            either a CallerMode defining a user ID to call, or a CalleeMode
            defining which user IDs to accept or block calls from.

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

        n_retries (int):
            how many times to retry when a request to the WsIo server fails.

        retry_interval (float):
            how long to wait in seconds before retrying a request to the WsIo
            server.

        log_level (int | None):
            logging level (e.g. `logging.INFO`)
    """

    _log: logging.Logger

    url: str
    id: str
    password: str
    call_mode: CallerMode | CalleeMode
    interval_min: float
    interval_max: float
    max_out_packet_size: int
    warm_up: bool
    ssl_verify: bool
    n_retries: int
    retry_interval: float

    _auth_code: str

    _out_buf: bytearray
    _out_buf_lock: threading.Lock

    _in_buf: bytearray
    _in_buf_lock: threading.Lock

    _thread: threading.Thread
    _stopping: bool = False

    _peer_id: str | None = None
    _call_timestamp: int = 0
    _ws: WsClient | None = None

    def __init__(
        self,
        url: str,
        id: str,
        password: str,
        call_mode: CallerMode | CalleeMode,
        interval_min: float = .05,
        interval_max: float = .25,
        max_out_packet_size: int = 128 * 1024,
        warm_up: bool = True,
        ssl_verify: bool = True,
        n_retries: int = 10,
        retry_interval: float = 2.,
        log_level: int | None = None
    ):
        url = validate_server_url(url)
        validate_id(id)
        validate_password(password)

        if isinstance(call_mode, CallerMode):
            validate_id(call_mode.who_to_call)
            if call_mode.who_to_call == id:
                raise ValueError(
                    f"call_mode.who_to_call and id must be different (both are "
                    f"{id})."
                )
        elif isinstance(call_mode, CalleeMode):
            if not isinstance(call_mode.accept_calls_from, list):
                raise ValueError(
                    f"call_mode.accept_calls_from must be a list[str], not "
                    f"{type(call_mode.accept_calls_from)}."
                )
            for peer_id in call_mode.accept_calls_from:
                validate_id(peer_id)
                if peer_id == id:
                    raise ValueError(
                        "all user IDs in call_mode.accept_calls_from must be "
                        "different from id."
                    )

            if not call_mode.block_mode and not call_mode.accept_calls_from:
                raise ValueError(
                    "call_mode.accept_calls_from is empty and "
                    "call_mode.block_mode is False so we're accepting calls "
                    "from nobody!"
                )
        else:
            raise ValueError(
                f"call_mode must be either a CallerMode or CalleeMode, not "
                f"{type(call_mode)}."
            )

        self._log = make_logger(f"WsIo", log_level)

        self.url = url
        self.id = id
        self.password = password
        self.call_mode = call_mode
        self.interval_min = float(interval_min)
        self.interval_max = float(interval_max)
        self.max_out_packet_size = int(max_out_packet_size)
        self.warm_up = warm_up
        self.ssl_verify = ssl_verify
        self.n_retries = int(n_retries)
        self.retry_interval = float(retry_interval)

        if self.interval_max < self.interval_min:
            raise ValueError(
                f"interval_max={self.interval_max} must be larger than or "
                f"equal to interval_min={self.interval_min}."
            )

        avg_interval = (self.interval_min + self.interval_max) * .5
        out_data_rate = self.max_out_packet_size / avg_interval
        self._log.info(
            f"URL: {self.url}\n"
            f"user ID: {self.id}\n"
            f"interval range: {self.interval_min}-{self.interval_max} s\n"
            f"max. outgoing packet size: "
            f"{format_data_size(self.max_out_packet_size)}\n"
            f"outgoing data rate: ~{format_data_rate(out_data_rate)}"
        )

        if self.url.startswith("https://") and not self.ssl_verify:
            # disable SSL warnings and log a single warning ourselves
            urllib3.disable_warnings()
            self._log.warning(
                "WARNING: you have disabled SSL certificate verification. your "
                "connection is vulnerable to man-in-the-middle attacks."
            )
        elif self.url.startswith("http://"):
            self._log.warning(
                "WARNING: you are using the non-secure HTTP protocol instead "
                "of HTTPS to communicate with the WsIo server. your connection "
                "is vulnerable to man-in-the-middle attacks."
            )

        self._out_buf = bytearray()
        self._out_buf_lock = threading.Lock()
        self._in_buf = bytearray()
        self._in_buf_lock = threading.Lock()

        # authentication

        self._auth_code = generate_auth_code(self.id, self.password)
        res = self._request_json(
            "prepare",
            {"auth": self._auth_code}
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

    def __del__(self):
        self.stop()

    def running(self) -> bool:
        return not self._stopping

    def stop(self):
        global keyboard_interrupt

        if self._stopping:
            return

        self._stopping = True
        try:
            self._ws.shutdown()
        except KeyboardInterrupt as e:
            keyboard_interrupt = e
        except Exception:
            pass

    def _receive(self, size: int) -> bytes:
        while True:
            if not self.running():
                raise ConnectionError("WsIo has stopped")

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
            raise ConnectionError("WsIo has stopped")

        force_acquire(self._out_buf_lock)
        self._out_buf += data
        self._out_buf_lock.release()

    def _thread_run(self):
        global keyboard_interrupt
        try:
            # warm up
            if self.warm_up:
                self._log.info("warming up")
                n = random.randint(3, 5)
                for _ in range(n):
                    time.sleep(.8 + 1.1 * random.random())
                    self._dummy_request("dummy")
                time.sleep(1. + .2 * random.random())

            if isinstance(self.call_mode, CallerMode):
                # call the peer
                self._log.info(f"calling peer {self.call_mode.who_to_call}")
                self._ws = WsClient(
                    f"{self.url}call",
                    {
                        "auth": self._auth_code,
                        "peer": self.call_mode.who_to_call
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
            elif isinstance(self.call_mode, CalleeMode):
                # wait for an incoming call

                if self.call_mode.block_mode \
                        and self.call_mode.accept_calls_from:
                    self._log.info(
                        f"waiting for an incoming call from anyone except "
                        f"{", ".join(self.call_mode.accept_calls_from)}"
                    )
                elif self.call_mode.block_mode:
                    self._log.info(
                        "waiting for an incoming call from anyone"
                    )
                elif len(self.call_mode.accept_calls_from) == 1:
                    self._log.info(
                        f"waiting for an incoming call from user "
                        f"{", ".join(self.call_mode.accept_calls_from)}"
                    )
                elif len(self.call_mode.accept_calls_from) > 1:
                    self._log.info(
                        f"waiting for an incoming call from users "
                        f"{", ".join(self.call_mode.accept_calls_from)}"
                    )
                else:
                    self._log.warning(
                        f"waiting for an incoming call from nobody (what the?)"
                    )

                first_iter = True
                while True:
                    if first_iter:
                        first_iter = False
                    else:
                        time.sleep(5.)

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
                        continue

                    self._peer_id = None
                    if self.call_mode.block_mode:
                        for call in calls:
                            if call["caller"] not in \
                                    self.call_mode.accept_calls_from:
                                self._peer_id = call["caller"]
                                self._call_timestamp = call["timestamp"]
                                break
                    else:
                        for call in calls:
                            if call["caller"] in \
                                    self.call_mode.accept_calls_from:
                                self._peer_id = call["caller"]
                                self._call_timestamp = call["timestamp"]
                                break

                    if self._peer_id is None:
                        continue
                    else:
                        break

                self._log.info(f"answering incoming call from {self._peer_id}")
                self._ws = WsClient(
                    f"{self.url}pickup",
                    {
                        "auth": self._auth_code,
                        "peer": self._peer_id
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

        if self._stopping:
            return
        self._ws.send(data)

    def _read_packet(self):
        if self._stopping:
            return

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
        for i in range(self.n_retries + 1):
            try:
                if i > 0:
                    time.sleep(self.retry_interval)

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
                    f"\"/{path}\" failed ({i}/{self.n_retries}): "
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
          server URL (HTTPS or HTTP) ending with a slash.
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


def validate_server_url(url: str) -> str:
    if not url.endswith("/"):
        raise ValueError("server URL must end with \"/\"")

    prefix = "https://"
    if url.lower().startswith(prefix):
        return prefix + url[len(prefix):]

    prefix = "http://"
    if url.lower().startswith(prefix):
        return prefix + url[len(prefix):]

    raise ValueError(
        "server URL must start with \"https://\" or \"http://\""
    )


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
