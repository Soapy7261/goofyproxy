"""
provides `AudioIo`, a `GoofyIo` child class for data transfer through audio.
"""

import time
import threading
from enum import IntEnum
from typing import NamedTuple, Mapping

import numpy as np
from scipy import signal
from scipy import fft

import pyaudio
import zlib

from goofyio import GoofyIo
from common import *


# audio oversampling factor for input and output
IOVERSAMPLE: int = 2
OOVERSAMPLE: int = 4

# remove older outgoing packets that are already transmitted until we go below
# the memory limit or reach the minimum outgoing packet count because we need to
# keep the last few packets in case the other side asks for a retransmission.
OUT_PACKETS_MEMORY_LIMIT = 512 * 1024 * 1024
OUT_PACKETS_MIN_COUNT = 16

# how many periods of the marker frequency to play for the packet start marker.
# NOTE: both sides must use the same value.
MARKER_PERIODS = 200

# numeric data type for audio samples
Float = np.float32


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


"""
packet header (8 bytes):
[2 bytes] header checksum
[2 bytes] packet index
[2 bytes] packet data checksum
[2 bytes] packet data length

retransmission request header (8 bytes):
[2 bytes] header checksum
[2 bytes] constant: RETRANSMIT_REQ_PACKET_IDX
[2 bytes] index of the packet to start retransmitting from
[2 bytes] constant: 0
"""

PACKET_HEADER_BYTES: int = 8
PACKET_IDX_MAX = 64000
RETRANSMIT_REQ_PACKET_IDX = 65000  # must be bigger than PACKET_IDX_MAX


class Packet:
    idx: int
    data: bytes
    transmitted: bool = False

    def __init__(self, idx: int, data: bytes):
        if idx > PACKET_IDX_MAX:
            raise ValueError("invalid packet index (too large)")
        self.idx = idx
        self.data = data

    def to_bytes(self) -> bytes:
        header = \
            self.idx.to_bytes(2) \
            + compute_checksum(self.data).to_bytes(2) \
            + len(self.data).to_bytes(2)

        header_checksum = compute_checksum(header)
        header = header_checksum.to_bytes(2) + header

        return header + self.data


def make_retransmission_request_header(idx: int) -> bytes:
    header = \
        RETRANSMIT_REQ_PACKET_IDX.to_bytes(2) \
        + idx.to_bytes(2) \
        + b"\0\0"

    header_checksum = compute_checksum(header)
    header = header_checksum.to_bytes(2) + header

    return header


class ModulationParams(NamedTuple):
    carrier: float  # carrier frequency
    symrate: float  # symbol rate
    marker: float  # packet start marker frequency


class Profile(IntEnum):
    Slowest = 0  # most reliable
    Slow = 1
    Medium = 2
    Fast = 3
    Fastest = 4  # requires ideal environment

    def params(self, server_side: bool) -> ModulationParams:
        if self == self.Slowest:
            return ModulationParams(16000., 160., 12000.) if server_side \
                else ModulationParams(14000., 140., 10000.)

            return ModulationParams(4000., 200., 2200.) if server_side \
                else ModulationParams(3000., 150., 1200.)
        elif self == self.Slow:
            return ModulationParams(8000., 400., 4500.) if server_side \
                else ModulationParams(6000., 300., 3500.)
        elif self == self.Medium:
            return ModulationParams(12000., 1200., 3600.) if server_side \
                else ModulationParams(6000., 600., 2600.)
        elif self == self.Fast:
            return ModulationParams(18000., 2250., 5000.) if server_side \
                else ModulationParams(9000., 1125., 3500.)
        elif self == self.Fastest:
            return ModulationParams(20000., 4000., 5000.) if server_side \
                else ModulationParams(9000., 1800., 3500.)
        raise ValueError("invalid enum value")


class InputState(IntEnum):
    WaitingForMarker = 0
    ReadingHeader = 1
    ReadingPayload = 2


class AudioIo(GoofyIo):
    """
    a `GoofyIo` that transfers data through audio. both sides (server and
    client) must have a microphone and a speaker to transmit and receive audio
    samples.

    # how data is modulated over audio

    the method we use here is very basic. we simply pick a carrier frequency
    (e.g. 10 kHz) and turn it on (for 1) and off (for 0) really fast, depending
    on the bits. so effectively we just multiply a pure sine tone by our bit
    stream. we use oversampling to avoid aliasing.

    # packet start markers

    before each packet, we transmit a marker. a marker is basically a sine tone
    at the marker frequency for MARKER_PERIODS periods wrapped in MARKER_PERIODS
    periods of silence before and after it. we also play the carrier frequency
    at the same time:
    1. silence for "MARKER_PERIODS / marker frequency" seconds
    2. marker + carrier frequency together for the same duration
    3. silence for the same duration

    # full-duplex

    for full-duplex (two-sided) communication, the two sides (e.g. server and
    client) must use different carrier and marker frequencies. here, we make it
    asymmetric (like ADSL) and use higher frequency carriers with higher symbol
    rates for the server.
    """

    _log: logging.Logger

    # True if running as the server side
    _server_side: bool

    # audio callback parameters
    _in_samprate: int
    _in_bufsize: int
    _out_samprate: int
    _out_bufsize: int

    # quality profile used to get the modulation parameters
    _profile: Profile

    # input and output modulation parameters
    _in_mod: ModulationParams
    _out_mod: ModulationParams

    # used for smoothing out the correlation of input buffer and the marker clip
    _in_cor_hanning_smooth_kernel: np.ndarray | None = None

    # output volume
    _out_volume: float

    _running: bool = False
    _istream: pyaudio.Stream | None = None
    _ostream: pyaudio.Stream | None = None

    # precise amount of time in seconds passed since the end of the last input
    # marker detected. this represents the time at _in_buf[0].
    _in_time: Float

    # precise amount of time in seconds passed since the start of the last
    # output packet.
    _out_time: Float

    _out_packets: list[Packet]
    _out_packet_idx_offs: int = 0
    _out_packet_idx: int = 0
    _out_packets_lock: threading.Lock

    _out_curr_packet: Packet | None = None
    _out_curr_packet_bits: np.ndarray | None = None

    # input marker clip, used to detect incoming packets
    _in_marker_clip: np.ndarray | None = None
    _in_marker_clip_fft: np.ndarray | None = None

    # input symbol clip, used to detect "on" (1) symbols
    _in_sym_clip_cos: np.ndarray | None = None
    _in_sym_clip_sin: np.ndarray | None = None

    _in_buf: np.ndarray | None = None
    _in_state: InputState = InputState.WaitingForMarker
    _in_inverse_ir: np.ndarray | None = None
    _in_bits: np.ndarray | None = None
    _in_bytes: bytearray | None = None

    _in_packet_idx: int = 0
    _in_packet_checksum: int = 0
    _in_packet_datalen: int = 0

    _in_valid_packet_idx: int = 0

    _in_real_data: bytearray
    _in_real_data_lock: threading.Lock

    _request_retransmission: bool = False

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
        devices.sort(key=lambda d: 0 if d.is_default_input else 1)
        devices.sort(key=lambda d: 0 if d.is_default_output else 1)

        return devices

    def __init__(
        self,
        input_device: AudioDevice,
        output_device: AudioDevice,
        server_side: bool,  # True if running as the server side
        profile: Profile,  # quality profile
        output_volume: float = .75
    ):
        self._log = make_logger(f"AudioIo")

        self._server_side = server_side
        self._in_samprate = 48000
        self._in_bufsize = 4096
        self._out_samprate = 48000
        self._out_bufsize = 4096

        self.set_profile(profile)
        self._out_volume = output_volume

        self._in_time = Float(0.)
        self._out_time = Float(0.)
        self._out_packets = []
        self._out_packets_lock = threading.Lock()

        self._in_buf = np.zeros(0, dtype=Float)

        self._in_real_data = bytearray()
        self._in_real_data_lock = threading.Lock()

        self._log.debug(
            f"[input]\n"
            f"  sample rate: {self._in_samprate:.1f} Hz\n"
            f"  buffer size: {self._in_bufsize} samples "
            f"  ({self._in_bufsize / self._in_samprate * 1000.:.1f} ms)\n"
            f"  modulation: {self._in_mod}\n"
            f"[output]\n"
            f"  sample rate: {self._out_samprate} Hz\n"
            f"  buffer size: {self._out_bufsize} samples "
            f"  ({self._out_bufsize / self._out_samprate * 1000.:.1f} ms)\n"
            f"  modulation: {self._out_mod}"
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

    def set_profile(self, profile: Profile):
        self._profile = profile

        self._in_mod = self._profile.params(not self._server_side)
        self._out_mod = self._profile.params(self._server_side)

        # cache some stuff that's used frequently in the input callback
        self._in_cor_hanning_smooth_kernel = hanning_smooth_kernel(int(
            .25 * MARKER_PERIODS / self._in_mod.marker
            * self._in_samprate * IOVERSAMPLE
        ))
        self._compute_input_marker_clip()
        self._compute_input_symbol_clip()

    def _receive(self, size: int) -> bytes:
        POLL_INTERVAL = .05
        while True:
            if not self._in_real_data_lock.acquire():
                time.sleep(POLL_INTERVAL)
                continue

            if len(self._in_real_data) < size:
                self._in_real_data_lock.release()
                time.sleep(POLL_INTERVAL)
                continue

            data = bytes(self._in_real_data[:size])
            self._in_real_data = self._in_real_data[size:]

            self._in_real_data_lock.release()
            return data

    def _send(self, data: bytes):
        force_acquire(self._out_packets_lock)

        self._out_packets.append(Packet(
            (len(self._out_packets) + self._out_packet_idx_offs)
            % (PACKET_IDX_MAX + 1),
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
            self._out_packet_idx_offs += 1

        self._out_packets_lock.release()

    def _input_callback(
        self,
        in_data: bytes | None,
        frame_count: int,
        time_info: Mapping[str, float],
        status: int
    ) -> tuple[bytes | None, int]:
        if not (self._running and in_data and frame_count > 0):
            self._running = False
            self._in_buf = None
            return (None, pyaudio.paAbort)

        # just some constants for later
        real_samprate = self._in_samprate * IOVERSAMPLE
        marker_duration = len(self._in_marker_clip) / real_samprate
        if marker_duration < 1. / self._in_mod.symrate:
            raise ValueError(
                f"marker duration is shorter than a symbol ({self._in_mod=})"
            )

        # convert to numpy float array
        samples = np.frombuffer(in_data, dtype=np.int16).astype(Float) \
            / 32768.

        # oversample and add to the buffer
        samples = oversample(samples, IOVERSAMPLE)
        self._in_buf = np.concatenate((self._in_buf, samples))

        if self._in_state == InputState.WaitingForMarker:
            # continue until we have enough samples in the buffer
            if len(self._in_buf) < len(self._in_marker_clip) * 1.1:
                return (None, pyaudio.paContinue)

            # compute marker and input buffer norm for normalization
            marker_norm = np.linalg.norm(self._in_marker_clip)
            in_norm = np.linalg.norm(self._in_buf)

            # skip if the absolute norm of the input buffer (divided by its
            # duration) is too low.
            if in_norm / (len(self._in_buf) / real_samprate) < 6.:
                self._in_buf_pop(len(self._in_marker_clip))
                return (None, pyaudio.paContinue)

            # normalized cross-correlation
            cor = np.abs(
                signal.correlate(
                    self._in_buf,
                    self._in_marker_clip,
                    "same"
                ) / (in_norm * marker_norm)
            )

            # smooth out
            cor_smoothed = signal.convolve(
                cor,
                self._in_cor_hanning_smooth_kernel,
                "same"
            )

            # find the average correlation value in places where it's lower than
            # the total mean (basically the overall minimum-ish value).
            cor_mean = np.mean(cor_smoothed)
            cor_lower_than_mean = cor_smoothed * (cor_smoothed < cor_mean)
            if len(cor_lower_than_mean) > 0:
                cor_min = \
                    .5 * (np.mean(cor_lower_than_mean) + np.min(cor_smoothed))
            else:
                cor_min = np.min(cor_mean)

            # find rising and falling intervals
            high_cor = cor_smoothed > cor_min * 15.
            edges = np.diff(high_cor.astype(np.int8))
            rising = np.where(edges == 1)[0] + 1
            falling = np.where(edges == -1)[0] + 1
            intervals: list[tuple[int, int]] = []
            for rise_idx in rising:
                fall_idx = -1
                for i in falling:
                    if i > rise_idx:
                        fall_idx = i
                        break
                if fall_idx == -1:
                    continue
                intervals.append((rise_idx, fall_idx))

            # try to find an interval where the distance between rising and
            # falling matches the expected duration (find the last one if more
            # than one).
            expected_n_samples = \
                2. * MARKER_PERIODS / self._in_mod.marker * real_samprate
            interval: tuple[int, int] | None = None
            for rise_idx, fall_idx in intervals:
                n_samples = fall_idx - rise_idx
                if n_samples > .7 * expected_n_samples \
                        and n_samples < 1.43 * expected_n_samples:
                    interval = (rise_idx, fall_idx)

            # no interval found with a high enough correlation for the right
            # duration.
            if not interval:
                self._in_buf_pop(len(self._in_marker_clip))
                return (None, pyaudio.paContinue)

            # congratulations! we've detected a marker!
            self._in_state = InputState.ReadingHeader

            # isolate the received marker clip, ensuring the same size as
            # _in_marker_clip.
            received_marker = self._in_buf[interval[0]:interval[1] + 1]
            len_diff = len(received_marker) - len(self._in_marker_clip)
            len_diff_small_half = abs(len_diff) // 2
            len_diff_big_half = abs(len_diff) - len_diff_small_half
            if len_diff > 0:
                received_marker = received_marker[
                    len_diff_big_half:-len_diff_small_half
                ]
            elif len_diff < 0:
                received_marker = np.pad(
                    received_marker,
                    (len_diff_small_half, len_diff_big_half),
                    mode="reflect"
                )

            # we now have the ideal marker clip (_in_marker_clip) and what we've
            # received from the input device (received_marker), so we can use
            # deconvolution to compute the impulse response (and therefore also
            # frequency response) of the channel (e.g. room reverb) and its
            # inverse and use it to eliminate echoes and reverb tails when
            # reading symbols.
            self._in_inverse_ir = np.astype(np.real(fft.ifft(
                self._in_marker_clip_fft / fft.fft(received_marker)
            )), Float)

            # calculate the ending time of the detected marker
            rise_t = interval[0] / real_samprate
            fall_t = interval[1] / real_samprate
            middle_t = .5 * (rise_t + fall_t)
            marker_start_t = middle_t - (.5 * marker_duration)

            # pop the input buffer until the marker start
            self._in_buf_pop(int(marker_start_t * real_samprate))

            # reset _in_time, _in_bits, and _in_bytes.
            # NOTE: we set _in_time to negative marker_duration so real data
            # starts at exactly 0.
            self._in_time = Float(-marker_duration)
            self._in_bits = np.ndarray(0, np.uint8)
            self._in_bytes = bytearray()

            return (None, pyaudio.paContinue)
        elif self._in_state in [
            InputState.ReadingHeader,
            InputState.ReadingPayload
        ]:
            buf_duration = len(self._in_buf) / real_samprate

            # make sure to keep at least a marker_duration in the buffer for
            # effective room reverb cancelation.
            buf_duration -= marker_duration

            # continue until we have at least one symbol
            if buf_duration < 1. / self._in_mod.symrate:
                return (None, pyaudio.paContinue)

            # convolve with the inverse impulse response of the channel to
            # cancel room reverb.
            in_conv = signal.convolve(
                self._in_buf,
                self._in_inverse_ir,
                "same"
            )

            # see how many symbols (bits) we can read
            if self._in_state == InputState.ReadingHeader:
                n_bits_left = PACKET_HEADER_BYTES * 8 - len(self._in_bits)
            elif self._in_state == InputState.ReadingPayload:
                n_bits_left = self._in_packet_datalen * 8 - len(self._in_bits)
            n_syms = min(
                int(buf_duration * self._in_mod.symrate),
                n_bits_left
            )

            # read symbols
            n_samples_read = 0
            for i in range(n_syms):
                # calculate the current symbol's start and end times and indices
                sym_start_idx = int(
                    (i / self._in_mod.symrate + marker_duration) * real_samprate
                )
                sym_end_idx = sym_start_idx + int(
                    real_samprate / self._in_mod.symrate
                )

                # isolate the symbol from the input buffer
                sym_clip = np.copy(in_conv[sym_start_idx:sym_end_idx])
                sym_clip /= np.linalg.norm(sym_clip)

                # dot-product with the ideal "on" (1) symbol to see how much of
                # the carrier frequency is present.
                carrier_strength = np.sqrt(
                    np.dot(sym_clip, self._in_sym_clip_cos) ** 2.
                    + np.dot(sym_clip, self._in_sym_clip_sin) ** 2.
                )
                print(f"{i} {carrier_strength=}")

                # add to the bits
                bit_value = np.uint8(carrier_strength > 60.)
                self._in_bits = np.pad(
                    self._in_bits,
                    (0, 1),
                    constant_values=[bit_value]
                )

                n_samples_read += (sym_end_idx - sym_start_idx)

            # pop n_samples_read samples from the input buffer
            self._in_buf_pop(n_samples_read)

            # after reading the last ever bit in the packet, we pop a
            # marker duration's worth of samples from the beginning of the input
            # buffer. if you've read everything above, you'll know why we do
            # this (effective room reverb cancelation).
            n_offset_samples = int(marker_duration * real_samprate)

            # pack every 8 bits into a byte
            while len(self._in_bits) >= 8:
                self._in_bytes.append(
                    np.packbits(self._in_bits[:8])[0]
                )
                self._in_bits = self._in_bits[8:]

            if self._in_state == InputState.ReadingHeader \
                    and len(self._in_bytes) >= PACKET_HEADER_BYTES:
                header = bytes(self._in_bytes)[:PACKET_HEADER_BYTES]
                self._in_bytes = self._in_bytes[PACKET_HEADER_BYTES:]

                header_checksum = int.from_bytes(header[:2])
                if header_checksum != compute_checksum(header[2:]):
                    # couldn't verify header checksum. either the header has
                    # errors or we detected a false marker.
                    self._log.debug(
                        "header checksum could not be verified, ignoring."
                    )
                    self._in_state = InputState.WaitingForMarker
                    self._in_buf_pop(n_offset_samples)
                    return (None, pyaudio.paContinue)

                self._in_packet_idx = int.from_bytes(header[2:4])
                self._in_packet_checksum = int.from_bytes(header[4:6])
                self._in_packet_datalen = int.from_bytes(header[6:8])

                if self._in_packet_idx == RETRANSMIT_REQ_PACKET_IDX:
                    # this is a retransmission request, the "checksum" value
                    # tells us the index from which we should start
                    # retransmitting.
                    force_acquire(self._out_packets_lock)
                    self._out_packet_idx = min(
                        self._out_packet_idx,
                        self._in_packet_checksum
                    )
                    self._out_packets_lock.release()

                    self._in_state = InputState.WaitingForMarker
                    self._in_buf_pop(n_offset_samples)
                elif self._in_packet_idx > PACKET_IDX_MAX:
                    self._log.debug(
                        f"received header with invalid packet index "
                        f"{self._in_packet_idx}, ignoring."
                    )
                    self._in_state = InputState.WaitingForMarker
                    self._in_buf_pop(n_offset_samples)
                else:
                    self._in_state = InputState.ReadingPayload

            if self._in_state == InputState.ReadingPayload \
                    and len(self._in_bytes) >= self._in_packet_datalen:
                # done reading the packet data

                data = bytes(self._in_bytes)[:self._in_packet_datalen]
                self._in_bytes = self._in_bytes[self._in_packet_datalen:]

                self._in_state = InputState.WaitingForMarker
                self._in_buf_pop(n_offset_samples)

                checksum_verified = True
                if self._in_packet_datalen > 0:
                    checksum_verified = \
                        self._in_packet_checksum == compute_checksum(data)

                if self._in_packet_idx < self._in_valid_packet_idx:
                    # we've already received this packet index, ignore
                    pass
                elif self._in_packet_idx > self._in_valid_packet_idx:
                    # we've missed at least one packet since the last valid one,
                    # so we'll need to ask for retransmission in the output.
                    self._request_retransmission = True
                elif not checksum_verified:
                    # data checksum couldn't be verified so there must be errors
                    # in the data.
                    self._request_retransmission = True
                else:
                    force_acquire(self._in_real_data_lock)
                    self._in_valid_packet_idx += 1
                    self._in_real_data += data
                    self._in_real_data_lock.release()

            return (None, pyaudio.paContinue)
        raise ValueError("invalid enum value")

    def _output_callback(
        self,
        in_data: bytes | None,
        frame_count: int,
        time_info: Mapping[str, float],
        status: int
    ) -> tuple[bytes | None, int]:
        if not self._running:
            return (None, pyaudio.paAbort)

        # if we don't have any bits left to transmit
        if self._out_curr_packet_bits is None and self._request_retransmission:
            # send a special header to request retransmission
            self._request_retransmission = False
            self._out_curr_packet_bits = unpack_bits(
                make_retransmission_request_header(self._in_valid_packet_idx)
            )
        elif self._out_curr_packet_bits is None:
            force_acquire(self._out_packets_lock)

            self._out_packet_idx = max(
                self._out_packet_idx,
                self._out_packet_idx_offs
            )

            # output silence until there's a new packet to transmit
            if self._out_packet_idx - self._out_packet_idx_offs \
                    >= len(self._out_packets):
                self._out_packets_lock.release()
                return (np.zeros((frame_count,), np.int16), pyaudio.paContinue)

            # got a new packet to transmit
            self._out_curr_packet = self._out_packets[
                self._out_packet_idx - self._out_packet_idx_offs
            ]
            self._out_packet_idx += 1
            self._out_curr_packet_bits = unpack_bits(
                self._out_curr_packet.to_bytes()
            )
            self._out_time = Float(0.)

            self._out_packets_lock.release()

        # time since the beginning of the packet
        t = np.arange(
            frame_count * OOVERSAMPLE,
            dtype=Float
        ) / (self._out_samprate * OOVERSAMPLE) + self._out_time

        # packet start marker: transmit marker frequency and carrier frequency
        # together, followed and preceded by silence for the same duration.
        marker_duration = MARKER_PERIODS * 3. / self._out_mod.marker
        marker_mask = \
            (t - marker_duration * .5) < (MARKER_PERIODS / self._out_mod.marker)
        samples = (
            np.sin(2. * np.pi * self._out_mod.marker * t) * marker_mask
            + np.sin(2. * np.pi * self._out_mod.carrier * t) * marker_mask
        ) * .5

        # bit index for each sample
        bit_indices_unbounded = np.astype(
            np.floor((t - marker_duration) * self._out_mod.symrate),
            np.int32
        )
        bit_indices = np.clip(
            bit_indices_unbounded,
            0,
            len(self._out_curr_packet_bits) - 1
        )

        # actual bit value for each sample (0 or 1)
        bit_values = self._out_curr_packet_bits[bit_indices]

        # modulate the carrier
        samples += np.sin(
            2. * np.pi * self._out_mod.carrier * t
        ) * bit_values * (
            (bit_indices_unbounded >= 0)
            & (bit_indices_unbounded < len(self._out_curr_packet_bits))
        )

        # un-oversample
        samples = downsample(samples, OOVERSAMPLE)

        # volume and clip
        samples = np.clip(samples * self._out_volume, -1., 1.)

        # update _out_time
        buf_duration = float(frame_count) / self._out_samprate
        self._out_time += buf_duration

        # see if we reached the end of the packet
        if bit_indices[-1] >= len(self._out_curr_packet_bits) - 1:
            force_acquire(self._out_packets_lock)

            if self._out_curr_packet is not None:
                self._out_curr_packet.transmitted = True
                self._out_curr_packet = None
            self._out_curr_packet_bits = None

            self._out_packets_lock.release()

        # convert to 16-bit integers
        samples_i16 = (samples * 32767.).astype(np.int16)
        return (samples_i16.tobytes(), pyaudio.paContinue)

    def _compute_input_marker_clip(self):
        """
        compute the input marker clip used to detect incoming packets.
        """

        duration = MARKER_PERIODS * 3. / self._in_mod.marker

        t = np.arange(
            int(duration * self._in_samprate * IOVERSAMPLE),
            dtype=Float
        ) / self._in_samprate / IOVERSAMPLE

        mask = (t - duration * .5) < (MARKER_PERIODS / self._in_mod.marker * .5)
        self._in_marker_clip = (
            np.sin(2. * np.pi * self._in_mod.marker * t)
            + np.sin(2. * np.pi * self._in_mod.carrier * t)
        ) * .5 * mask

        self._in_marker_clip_fft = fft.fft(self._in_marker_clip)

    def _compute_input_symbol_clip(self):
        duration = 1. / self._in_mod.symrate

        t = np.arange(
            int(duration * self._in_samprate * IOVERSAMPLE),
            dtype=Float
        ) / self._in_samprate / IOVERSAMPLE

        a = 2. * np.pi * self._in_mod.carrier * t
        self._in_sym_clip_cos = np.cos(a)
        self._in_sym_clip_sin = np.sin(a)

        self._in_sym_clip_cos /= np.linalg.norm(self._in_sym_clip_cos)
        self._in_sym_clip_sin /= np.linalg.norm(self._in_sym_clip_sin)

    def _in_buf_pop(self, n_samples: int):
        if n_samples < 0:
            return
        actual_n = min(int(n_samples), len(self._in_buf))
        self._in_time += actual_n / self._in_samprate / IOVERSAMPLE
        self._in_buf = self._in_buf[actual_n:]


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


def oversample(samples: np.ndarray, factor: int) -> np.ndarray:
    return signal.resample_poly(
        samples,
        factor,
        1,
        window=("kaiser", 14)
    )


def downsample(samples: np.ndarray, factor: int) -> np.ndarray:
    return signal.resample_poly(
        samples,
        1,
        factor,
        window=("kaiser", 14)
    )


def hanning_smooth(a: np.ndarray, n: int) -> np.ndarray:
    h = np.hanning(n)
    return np.convolve(a, h / np.sum(h), "same")


def hanning_smooth_kernel(n: int) -> np.ndarray:
    h = np.hanning(n)
    return h / np.sum(h)


def compute_checksum(data: bytes) -> int:
    return zlib.crc32(data) % 2**16


def unpack_bits(data: bytes) -> np.ndarray:
    return np.unpackbits(np.frombuffer(data, dtype=np.uint8))
