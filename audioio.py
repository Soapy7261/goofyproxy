"""
provides `AudioIo`, a `GoofyIo` child class for data transfer through audio.
"""

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
MARKER_PERIODS = 80

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


class Packet:
    idx: int
    data: bytes
    transmitted: bool = False

    def __init__(self, idx: int, data: bytes):
        self.idx = idx
        self.data = data

    def to_bytes(self) -> bytes:
        # header: packet index, data checksum, data length
        checksum = zlib.crc32(self.data) % 2**16
        header = \
            self.idx.to_bytes(2) \
            + checksum.to_bytes(2) \
            + len(self.data).to_bytes(2)

        # repeat the header 3 times because we're using the most naive error
        # correction scheme ever (majority voting).
        return header * 3 + self.data


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
            return ModulationParams(4000., 400., 1700.) if server_side \
                else ModulationParams(2400., 240., 1200.)
        elif self == self.Slow:
            return ModulationParams(8000., 1000., 3500.) if server_side \
                else ModulationParams(5000., 625., 2800.)
        elif self == self.Medium:
            return ModulationParams(12000., 2000., 4500.) if server_side \
                else ModulationParams(6000., 1000., 3800.)
        elif self == self.Fast:
            return ModulationParams(18000., 3000., 6000.) if server_side \
                else ModulationParams(9000., 1500., 4500.)
        elif self == self.Fastest:
            return ModulationParams(20000., 4000., 6500.) if server_side \
                else ModulationParams(9000., 1800., 5000.)
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

    # input and output quality profiles
    _in_profile: Profile
    _out_profile: Profile

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
    _out_packets_idx_offs: int = 0
    _out_packet_idx: int = 0
    _out_packets_lock: threading.Lock

    _out_curr_packet: Packet | None = None
    _out_curr_packet_bits: np.ndarray | None = None

    # input marker clip, used to detect incoming packets
    _in_marker_clip: np.ndarray | None = None
    _in_marker_clip_fft: np.ndarray | None = None

    _in_buf: np.ndarray | None = None
    _in_state: InputState = InputState.WaitingForMarker
    _in_inverse_ir: np.ndarray | None = None

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

        self._in_time = Float(0.)
        self._out_time = Float(0.)
        self._out_packets = []
        self._out_packets_lock = threading.Lock()

        self._in_buf = np.zeros(0, dtype=Float)

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

    def set_input_profile(self, profile: Profile):
        self._in_profile = profile
        self._in_mod = self._in_profile.params(not self._server_side)

        self._in_cor_hanning_smooth_kernel = hanning_smooth_kernel(int(
            .25 * MARKER_PERIODS / self._in_mod.marker
            * self._in_samprate * IOVERSAMPLE
        ))

        self._compute_input_marker_clip()

    def set_output_profile(self, profile: Profile):
        self._out_profile = profile
        self._out_mod = self._out_profile.params(self._server_side)

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
        if not (self._running and in_data and frame_count > 0):
            self._running = False
            self._in_buf = None
            return (None, pyaudio.paAbort)

        # just some constants for later
        real_samprate = self._in_samprate * IOVERSAMPLE
        marker_duration = len(self._in_marker_clip) / real_samprate

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
            if in_norm / (len(self._in_buf) / real_samprate) < 5.:
                self._in_buf_pop(len(self._in_marker_clip))
                return (None, pyaudio.paContinue)

            # normalized cross-correlation
            cor = np.abs(
                signal.correlate(
                    self._in_buf,
                    self._in_marker_clip,
                    'same'
                ) / (in_norm * marker_norm)
            )

            # smooth out
            cor_smoothed = signal.convolve(
                cor,
                self._in_cor_hanning_smooth_kernel,
                'same'
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
            high_cor = cor_smoothed > cor_min * 8.
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
            self._in_inverse_ir = fft.ifft(
                self._in_marker_clip_fft / fft.fft(received_marker)
            )

            # calculate the ending time of the detected marker
            rise_t = interval[0] / real_samprate
            fall_t = interval[1] / real_samprate
            middle_t = .5 * (rise_t + fall_t)
            marker_end_t = middle_t + (.5 * marker_duration)

            # pop the input buffer until the marker end
            self._in_buf_pop(int(marker_end_t * real_samprate))

            # reset _in_time
            self._in_time = Float(0.)

            # the data always has a constant 0 bit at the start so the real
            # start time is one symbol after the marker.
            self._in_data_start_time = 1. / self._in_mod.symrate

            return (None, pyaudio.paContinue)
        elif self._in_state == InputState.ReadingHeader:
            # time since the end of the marker
            t = np.arange(len(self._in_buf), dtype=Float) / real_samprate \
                + (self._in_time - self._in_data_start_time)

            # apply _in_inverse_channel_ir convolution
            # TAA()

            # chop up into symbol-duration-long clips and correlate each one
            # with the carrier frequency.
            # TAA()

            return (None, pyaudio.paContinue)
        elif self._in_state == InputState.ReadingPayload:
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
            self._out_curr_packet_bits = np.pad(
                np.unpackbits(np.frombuffer(
                    self._out_curr_packet.to_bytes(),
                    dtype=np.uint8
                )),
                (1,),
                'constant',
                constant_values=(0,)
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
        samples = \
            np.sin(2. * np.pi * self._out_mod.marker * t) * marker_mask \
            + np.sin(2. * np.pi * self._out_mod.carrier * t) * marker_mask

        # bit index for each sample
        bit_indices = np.clip(
            np.astype(np.floor(
                (t - marker_duration) * self._out_mod.symrate
            ), np.int32),
            0,
            len(self._out_curr_packet_bits) - 1
        )

        # actual bit value for each sample (0 or 1)
        bit_values = self._out_curr_packet_bits[bit_indices]

        # modulate the carrier
        samples += np.sin(
            2. * np.pi * self._out_mod.carrier * t
        ) * bit_values

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

            self._out_curr_packet.transmitted = True
            self._out_curr_packet = None
            self._out_curr_packet_bits = None

            self._out_packet_idx += 1

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
            int(duration * self._in_samprate * IOVERSAMPLE)
        ) / self._in_samprate / IOVERSAMPLE

        mask = (t - duration * .5) < (MARKER_PERIODS / self._in_mod.marker * .5)
        self._in_marker_clip = (
            np.sin(2. * np.pi * self._in_mod.marker * t)
            + np.sin(2. * np.pi * self._in_mod.carrier * t)
        ) * mask

        self._in_marker_clip_fft = fft.fft(self._in_marker_clip)

    def _in_buf_pop(self, n_samples: int):
        actual_n = min(n_samples, len(self._in_buf))
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
        window=('kaiser', 14)
    )


def downsample(samples: np.ndarray, factor: int) -> np.ndarray:
    return signal.resample_poly(
        samples,
        1,
        factor,
        window=('kaiser', 14)
    )


def hanning_smooth(a: np.ndarray, n: int) -> np.ndarray:
    h = np.hanning(n)
    return np.convolve(a, h / np.sum(h), 'same')


def hanning_smooth_kernel(n: int) -> np.ndarray:
    h = np.hanning(n)
    return h / np.sum(h)
