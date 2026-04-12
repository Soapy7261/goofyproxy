"""
provides `AudioIo`, a `GoofyIo` child class for data transfer through audio.
"""

import threading
from enum import IntEnum
from typing import NamedTuple, Mapping
from dataclasses import dataclass

import numpy as np
from scipy import signal
import pyaudio
import zlib

from goofyio import GoofyIo
from common import *


# audio oversampling factor
OVERSAMPLE: int = 4

# remove older outgoing packets that are already transmitted until we go below
# the memory limit or reach the minimum outgoing packet count because we need to
# keep the last few packets in case the other side asks for a retransmission.
OUT_PACKETS_MEMORY_LIMIT = 512 * 1024 * 1024
OUT_PACKETS_MIN_COUNT = 16


class AudioDeviceType(IntEnum):
    Input = 0
    Output = 1
    InputOutput = 2


class AudioDevice(NamedTuple):
    id: int  # PortAudio device index
    name: str
    max_input_channels: int
    max_output_channels: int
    is_default_input: bool
    is_default_output: bool


# NOTE: the training byte is always wrapped in two 0 bytes.
TRAINING_BITS = 0b11111110  # 254
ESCAPE = 0b10010111  # 151
TRAINING_REPLACER = 0b11000010  # 194
ESCAPE_REPLACER = 0b11000011  # 195


class Packet:
    idx: int
    data: bytes
    transmitted: bool = False

    def __init__(self, idx: int, data: bytes):
        self.idx = idx
        self.data = data

    def to_bytes(self) -> bytes:
        # start with the training bits, used to find the beginning of a packet
        # and learn the impulse frequency response of the channel (room reverb).
        # the training byte is followed by a 0 byte to transmit silence for 8
        # symbol durations.
        raw = bytes([0, TRAINING_BITS, 0])

        # make sure the training byte doesn't appear in the actual data. we do
        # this by replacing every instance of the training byte by an escape
        # byte followed by a replacer code for the training byte. for every
        # coincidential instance of the escape byte itself in the actual data,
        # we add a different replacer code after the escape byte. example:
        # [before]
        # ..., TRAINING_BITS, ..., ESCAPE (coincidential), ...
        # [after]
        # ..., ESCAPE, TRAINING_REPLACER, ..., ESCAPE, ESCAPE_REPLACER, ...
        data = bytearray(self.data)
        i = 0
        while i < len(data):
            if data[i] == ESCAPE:
                data.insert(i + 1, ESCAPE_REPLACER)
                i += 2
                continue
            elif data[i] == TRAINING_BITS:
                data[i] = ESCAPE
                data.insert(i + 1, TRAINING_REPLACER)
                i += 2
                continue
            i += 1

        # header: packet index, data checksum, data length
        checksum = zlib.crc32(data) % 2**16
        header = \
            self.idx.to_bytes(2) \
            + checksum.to_bytes(2) \
            + len(self.data).to_bytes(2)

        # add the header. repeated 3 times because we're using the most naive
        # error correction scheme ever (majority voting).
        raw += header * 3

        # the data itself
        raw += bytes(data)

        # end with a zero
        raw += b"\0"

        return raw


class Profile(IntEnum):
    Slowest = 0  # most reliable
    Slow = 1
    Medium = 2
    Fast = 3
    Fastest = 4  # requires ideal environment

    def params(self, server_side: bool) -> tuple[float, float]:
        """
        returns the carrier frequency and symbol rate.
        """
        if self == self.Slowest:
            return (4000., 400.) if server_side else (2400., 240.)
        elif self == self.Slow:
            return (8000., 1000.) if server_side else (5000., 625.)
        elif self == self.Medium:
            return (12000., 2000.) if server_side else (6000., 1000.)
        elif self == self.Fast:
            return (18000., 3000.) if server_side else (9000., 1500.)
        elif self == self.Fastest:
            return (20000., 4000.) if server_side else (9000., 1800.)
        raise ValueError("invalid enum value")


class AudioIo(GoofyIo):
    """
    a `GoofyIo` that transfers data through audio. both sides (server and
    client) must have a microphone and a speaker to transmit and receive audio
    samples.

    # how data is modulated over audio

    the method we use here is very basic. we simply pick a carrier frequency
    (e.g. 10 kHz) and turn it on (for 1) and off (for 0) really fast, depending
    on the bits. so effectively we just multiply a pure sine tone by our bit
    stream.

    # full-duplex

    for full-duplex (two-sided) communication, the two sides (e.g. server and
    client) must use different carrier frequencies. here, we make it asymmetric
    (like ADSL) and use a higher frequency carrier with a higher symbol rate for
    the server .
    """

    _log: logging.Logger

    # True if running as the server side
    _server_side: bool

    # audio callback parameters
    _in_samprate: int
    _in_bufsize: int
    _out_samprate: int
    _out_bufsize: int

    # input and output quality profiles
    _in_profile: Profile
    _out_profile: Profile

    # input and output carrier frequencies
    _in_carrier: float
    _out_carrier: float

    # input and output symbol rate (symbols/second)
    _in_symrate: float
    _out_symrate: float

    # output volume
    _out_volume: float

    _running: bool = False
    _istream: pyaudio.Stream | None = None
    _ostream: pyaudio.Stream | None = None

    # precise amount of time in seconds passed since the first input or output
    # audio callback until the last callback.
    _in_time: np.float64
    _out_time: np.float64

    _out_packets: list[Packet]
    _out_packets_idx_offs: int = 0
    _out_packet_idx: int = 0
    _out_packets_lock: threading.Lock

    _out_curr_packet: Packet | None = None
    _out_curr_packet_bits: np.ndarray | None = None
    _out_curr_packet_bit_offs: int = 0

    # input training clip, used to detect incoming packets
    _in_training_clip: np.ndarray | None = None

    def list_devices(
        device_type: AudioDeviceType = AudioDeviceType.InputOutput
    ) -> list[AudioDevice]:
        """
        get a list of all audio devices matching given type.
        """

        default_input_idx = paudio().get_default_input_device_info()["index"]
        default_output_idx = paudio().get_default_output_device_info()["index"]

        devices: list[AudioDevice] = []
        for i in range(paudio().get_device_count()):
            info = paudio().get_device_info_by_index(i)

            if device_type == AudioDeviceType.Input \
                    and info["maxInputChannels"] < 1:
                continue

            if device_type == AudioDeviceType.Output \
                    and info["maxOutputChannels"] < 1:
                continue

            if info["maxInputChannels"] < 1 and info["maxOutputChannels"] < 1:
                continue

            devices.append(AudioDevice(
                info["index"],
                info["name"],
                info["maxInputChannels"],
                info["maxOutputChannels"],
                info["index"] == default_input_idx,
                info["index"] == default_output_idx
            ))

        # make sure the default output device comes first, followed by the
        # default input device, if any are present.
        devices.sort(key=lambda device: 0 if device.is_default_input else 1)
        devices.sort(key=lambda device: 0 if device.is_default_output else 1)

        return devices

    def __init__(
        self,
        input_device: AudioDevice,
        output_device: AudioDevice,
        server_side: bool,  # True if running as the server side
        input_profile: Profile,  # quality profile of the other side
        output_profile: Profile,  # transmission quality profile
        output_volume: float = .75
    ):
        self._log = make_logger(f"AudioIo")

        self._server_side = server_side
        self._in_samprate = 48000
        self._in_bufsize = 4096
        self._out_samprate = 48000
        self._out_bufsize = 4096

        self.set_input_profile(input_profile)
        self.set_output_profile(output_profile)

        self._out_volume = output_volume

        self._in_time = np.float64(0.)
        self._out_time = np.float64(0.)
        self._out_packets = []
        self._out_packets_lock = threading.Lock()

        self._log.debug(
            f"[input]\n"
            f"  sample rate: {self._in_samprate:.1f} Hz\n"
            f"  buffer size: {self._in_bufsize} samples "
            f"  ({self._in_bufsize / self._in_samprate * 1000.:.1f} ms)\n"
            f"  carrier freq: {self._in_carrier:.1f} Hz\n"
            f"  symbol rate: {self._in_symrate:.1f} sym/s\n"
            f"[output]\n"
            f"  sample rate: {self._out_samprate} Hz\n"
            f"  buffer size: {self._out_bufsize} samples "
            f"  ({self._out_bufsize / self._out_samprate * 1000.:.1f} ms)\n"
            f"  carrier freq: {self._out_carrier:.1f} Hz\n"
            f"  symbol rate: {self._out_symrate:.1f} sym/s"
        )

        self._istream = paudio().open(
            rate=self._in_samprate,
            channels=1,
            format=pyaudio.paInt16,
            input=True,
            input_device_index=input_device.id,
            frames_per_buffer=self._in_bufsize,
            stream_callback=self._input_callback
        )
        self._ostream = paudio().open(
            rate=self._out_samprate,
            channels=1,
            format=pyaudio.paInt16,
            output=True,
            output_device_index=output_device.id,
            frames_per_buffer=self._out_bufsize,
            stream_callback=self._output_callback
        )

        self._running = True
        self._istream.start_stream()
        self._ostream.start_stream()

        if not self._istream or not self._istream.is_active():
            self._running = False
            raise RuntimeError("failed to open input audio stream")
        if not self._ostream or not self._ostream.is_active():
            self._running = False
            raise RuntimeError("failed to open output audio stream")

    def __del__(self):
        try:
            self._istream.stop_stream()
            self._istream.close()
        except:
            pass
        try:
            self._ostream.stop_stream()
            self._ostream.close()
        except:
            pass

    def running(self) -> bool:
        return self._running

    def stop(self):
        self._running = False

    def set_input_profile(self, profile: Profile):
        self._in_profile = profile
        self._in_carrier, self._in_symrate = self._in_profile.params(
            not self._server_side
        )
        self._compute_input_training_clip()

    def set_output_profile(self, profile: Profile):
        self._out_profile = profile
        self._out_carrier, self._out_symrate = self._out_profile.params(
            self.server_side
        )

    def _receive(self, size: int) -> bytes:
        raise NotImplementedError()

    def _send(self, data: bytes):
        force_acquire(self._out_packets_lock)

        self._out_packets.append(Packet(
            len(self._out_packets) + self._out_packets_idx_offs,
            data
        ))

        # clean up old outgoing packets that are already transmitted.
        total_size = 0
        for packet in self._out_packets:
            total_size += len(packet.data)
        while total_size > OUT_PACKETS_MEMORY_LIMIT \
                and len(self._out_packets) > OUT_PACKETS_MIN_COUNT \
                and self._out_packets[0].transmitted:
            total_size -= len(self._out_packets[0].data)
            self._out_packets.pop(0)
            self._out_packets_idx_offs += 1

        self._out_packets_lock.release()

    def _input_callback(
        self,
        in_data: bytes | None,
        frame_count: int,
        time_info: Mapping[str, float],
        status: int
    ) -> tuple[bytes | None, int]:
        if not (self._running and in_data and frame_count > 1):
            self._running = False
            return (None, pyaudio.paAbort)

        # convert bytes to numpy float32 array
        samples = np.frombuffer(in_data, dtype=np.int16).astype(np.float32) \
            / 32768.
        
        # oversample
        samples = oversample(samples)

        # cross-correlate with the training clip to detect incoming packets
        np.correlate(self._in_training_clip, )

        rms = np.sqrt(np.mean(samples ** 2))
        # print(f"RMS: {rms:.2f}, n. samples: {len(samples)}")

        return (None, pyaudio.paContinue)

    def _output_callback(
        self,
        in_data: bytes | None,
        frame_count: int,
        time_info: Mapping[str, float],
        status: int
    ) -> tuple[bytes | None, int]:
        if not self._running:
            return (None, pyaudio.paAbort)

        if self._out_curr_packet is None:
            force_acquire(self._out_packets_lock)

            # output silence until there's a new packet to transmit
            self._out_packet_idx = max(
                self._out_packet_idx,
                self._out_packets_idx_offs
            )
            if self._out_packet_idx - self._out_packets_idx_offs \
                    >= len(self._out_packets):
                self._out_packets_lock.release()
                return (np.zeros((frame_count,), np.int16), pyaudio.paContinue)

            # got a new packet to transmit
            self._out_curr_packet = self._out_packets[
                self._out_packet_idx - self._out_packets_idx_offs
            ]
            self._out_curr_packet_bits = np.unpackbits(np.frombuffer(
                self._out_curr_packet.to_bytes(),
                dtype=np.uint8
            ))
            self._out_curr_packet_bit_offs = np.astype(
                np.floor(self._out_time * self._out_symrate) + 1,
                np.int32
            )

            self._out_packets_lock.release()

        # time since the beginning of the stream
        t = np.arange(
            frame_count * OVERSAMPLE,
            dtype=np.float64
        ) / self._out_samprate / OVERSAMPLE + self._out_time

        # bit index for each sample
        bit_indices = np.clip(
            np.astype(np.floor(t * self._out_symrate), np.int32)
            - self._out_curr_packet_bit_offs,
            0,
            len(self._out_curr_packet_bits) - 1
        )

        # actual bit value for each sample (0 or 1)
        bit_values = self._out_curr_packet_bits[bit_indices]

        # sine wave * bits
        samples = self._out_volume * np.sin(
            2. * np.pi * self._out_carrier * t
        ) * bit_values

        # un-oversample
        samples = downsample(samples)

        # update _out_time
        buf_duration = float(frame_count) / self._out_samprate
        self._out_time += buf_duration

        # see if we reached the end of the packet
        if bit_indices[-1] >= len(self._out_curr_packet_bits) - 1:
            force_acquire(self._out_packets_lock)

            self._out_curr_packet.transmitted = True
            self._out_curr_packet = None
            self._out_curr_packet_bits = None

            self._out_packet_idx += 1

            self._out_packets_lock.release()

        # convert to 16-bit integers
        samples_i16 = (samples * 32767.).astype(np.int16)
        return (samples_i16.tobytes(), pyaudio.paContinue)

    def _compute_input_training_clip(self):
        """
        compute the input training clip used to detect incoming packets.
        """

        # the actual bits
        bits = np.unpackbits(np.frombuffer(
            bytes([0, TRAINING_BITS, 0]),
            dtype=np.uint8
        ))

        # time
        t = np.arange(
            len(bits) / self._in_symrate * self._in_samprate * OVERSAMPLE
        ) / self._in_samprate / OVERSAMPLE

        # bit index for each sample
        bit_indices = np.clip(
            np.astype(np.floor(t * self._in_symrate), np.int32),
            0,
            len(bits) - 1
        )

        # actual bit value for each sample (0 or 1)
        bit_values = bits[bit_indices]

        # sine wave * bits
        self._in_training_clip = np.sin(
            2. * np.pi * self._in_carrier * t
        ) * bit_values


_paudio_instance: pyaudio.PyAudio | None = None


def paudio() -> pyaudio.PyAudio:
    """
    creates a new instance of PyAudio on the first call, returns it on later
    calls.
    """
    global _paudio_instance
    if _paudio_instance is None:
        _paudio_instance = pyaudio.PyAudio()

        # PortAudio prints a lot of junk so the least we can do is make some
        # space for what comes after.
        print("\n\n")
    return _paudio_instance


def paudio_terminate():
    """
    terminates the PyAudio instance if it's been created before.
    """
    global _paudio_instance
    if _paudio_instance is None:
        return
    try:
        _paudio_instance.terminate()
    except:
        pass


def mvec_correct(received: bytes, size: int) -> bytes:
    """
    perform majority voting error correction on triplicated data.

    Arguments:
        received (bytes): the received data with triple the original size
        orig_size (int): original data size in bytes

    Returns:
        the error-corrected original data
    """
    assert len(received) == size * 3

    chunk1 = received[0:size]
    chunk2 = received[size:size*2]
    chunk3 = received[size*2:size*3]

    result = bytearray(size)
    for byte_idx in range(size):
        for bit_idx in range(8):
            # extract bit from each chunk (MSB first)
            bit1 = (chunk1[byte_idx] >> (7 - bit_idx)) & 1
            bit2 = (chunk2[byte_idx] >> (7 - bit_idx)) & 1
            bit3 = (chunk3[byte_idx] >> (7 - bit_idx)) & 1

            # majority vote: if 2 or more are 1, result is 1
            majority = 1 if (bit1 + bit2 + bit3) >= 2 else 0

            # set the bit in result
            if majority:
                result[byte_idx] |= (1 << (7 - bit_idx))

    return bytes(result)


def oversample(samples: np.ndarray) -> np.ndarray:
    return signal.resample_poly(
        samples,
        OVERSAMPLE,
        1,
        window=('kaiser', 14)
    )


def downsample(samples: np.ndarray) -> np.ndarray:
    return signal.resample_poly(
        samples,
        1,
        OVERSAMPLE,
        window=('kaiser', 14)
    )
