"""
provides `AudioIo`, a `GoofyIo` child class for data transfer through audio.
"""

from enum import IntEnum
from typing import NamedTuple, Mapping, Self
from dataclasses import dataclass

import pyaudio
import numpy as np
import zlib

from goofyio import GoofyIo


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


class Modulation(IntEnum):
    # binary phase shift keying: 0° and 180° (1 bits/symbol)
    Bpsk = 0

    # quadrature phase shift keying: 0°, 90°, 180°, 270° (2 bits/symbol)
    Qpsk = 1

    # quadrate amplitude modulation: 16 different combinations of phase and
    # amplitude (4 bits/symbol)
    Qam16 = 2

    def bits_per_symbol(self) -> int:
        if self == self.Bpsk:
            return 1
        elif self == self.Qpsk:
            return 2
        elif self == self.Qam16:
            return 4
        raise ValueError("invalid enum value")


@dataclass
class Carrier:
    freq: np.float64
    amp: np.float64
    modulation: Modulation


def total_bits_per_symbol(carriers: list[Carrier]) -> int:
    bps = 0
    for c in carriers:
        bps += c.modulation.bits_per_symbol()
    return bps


SYMBOL_RATE = 200.
SYMBOL_DURATION = 1. / SYMBOL_RATE

PACKET_START_FREQ = 2000.

TRAINING_BITS = 0b10101100  # 172
ESCAPE = 0b11000001  # 193
TRAINING_REPLACER = 0b11000010  # 194
ESCAPE_REPLACER = 0b11000011  # 195


class Packet:
    idx: int
    data: bytes

    def __init__(self, idx: int, data: bytes):
        self.idx = idx
        self.data = data

    def to_bytes(self) -> bytes:
        # start with the training bits, used to learn the impulse and frequency
        # response of the channel (environment) for room reverb cancelation and
        # eliminating multi-path reflections.
        raw = bytes([TRAINING_BITS])

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

        # and finally, the data itself
        raw += bytes(data)

        return raw


class AudioIo(GoofyIo):
    """
    a `GoofyIo` that transfers data through audio. both sides (server and
    client) must have a microphone and a speaker to transmit and receive audio
    samples.

    # how data is modulated over audio

    we use OFDM (orthogonal frequency division multiplexing) to utilize the
    available bandwidth (2 kHz - 18 kHz) more efficiently. I know that sounds
    like gibberish, so in simpler terms, we put several carriers with different
    frequencies and use different modulation schemes for each one based on its
    SNR (signal-to-noise ratio).

    a carrier is simply a pure sine tone whose amplitude and phase are modified
    over time to encode data. the rate at which we modify the amplitudes and
    phases of the carriers is our "symbol rate". a single symbol can encode
    multiple bits depending on the number of carriers and their modulation
    schemes.

    a modulation scheme provides a set of phase/amplitude combinations where
    each one represents one possible state. for example, binary
    phase-shift-keying or BPSK uses a 0° phase to encode a 0, and a 180° phase
    to encode a 1. a more advanced scheme like QAM-16 (quadrate amplitude
    modulation) uses 16 different phase/amplitude combinations (16 possible
    states), giving us 4 bits per symbol for every carrier that uses this
    scheme.

    the total speed in bits/second is the sum of:
    (bits/symbol for every modulation scheme)
    x (number of carriers using that scheme)
    x (symbol rate).

    in general, higher-frequency carriers are less noisy and support more
    complex modulation schemes with more bits per symbol.

    # full-duplex

    for full-duplex (two-sided) communication, the two sides (e.g. server and
    client) must use different sets of carriers whose frequencies do NOT
    overlap. here, we make it asymmteric (like ADSL) and dedicate more carriers
    with higher frequnecies to the server.
    """

    _in_samprate: int
    _in_bufsize: int
    _out_samprate: int
    _out_bufsize: int

    _running: bool = False
    _istream: pyaudio.Stream | None = None
    _ostream: pyaudio.Stream | None = None

    # precise amount of time in seconds passed since the first input or output
    # audio callback until the last callback.
    _in_time = np.float64(0.)
    _out_time = np.float64(0.)

    # subcarrier list
    _in_carriers: list[Carrier] = []
    _out_carriers: list[Carrier] = []

    # bits per symbol
    _in_bits_per_symbol: int = 0
    _out_bits_per_symbol: int = 0

    _out_packet: Packet
    _out_packet_bits: np.array

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
        server_side: bool,  # True if running as the server, False for client.
        input_device: AudioDevice,
        input_sample_rate: int,
        input_buffer_size: int,
        output_device: AudioDevice,
        output_sample_rate: int,
        output_buffer_size: int
    ):
        self._out_packet = Packet(23773, b"TAA, wassup brother")
        self._out_packet_bits = np.unpackbits(np.frombuffer(
            self._out_packet.to_bytes(),
            dtype=np.uint8
        ))

        client_carriers: list[Carrier] = []
        server_carriers: list[Carrier] = []
        for i in range(23):
            freq = np.float64(2250. + i * 250.)
            client_carriers.append(Carrier(
                freq,
                np.float64(.1),
                Modulation.Bpsk if freq < 3999. else Modulation.Qpsk
            ))
        for i in range(41):
            server_carriers.append(Carrier(
                np.float64(8000. + i * 250.),
                np.float64(.08),
                Modulation.Qam16
            ))
        self._in_carriers = client_carriers if server_side else server_carriers
        self._out_carriers = server_carriers if server_side else client_carriers

        self._in_bits_per_symbol = total_bits_per_symbol(self._in_carriers)
        self._out_bits_per_symbol = total_bits_per_symbol(self._out_carriers)

        self._in_samprate = input_sample_rate
        self._in_bufsize = input_buffer_size
        self._out_samprate = output_sample_rate
        self._out_bufsize = output_buffer_size

        self._istream = paudio().open(
            rate=input_sample_rate,
            channels=1,
            format=pyaudio.paInt16,
            input=True,
            input_device_index=input_device.id,
            frames_per_buffer=input_buffer_size,
            stream_callback=self._input_callback
        )
        self._ostream = paudio().open(
            rate=output_sample_rate,
            channels=1,
            format=pyaudio.paInt16,
            output=True,
            output_device_index=output_device.id,
            frames_per_buffer=output_buffer_size,
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

    def _receive(self, size: int) -> bytes:
        pass

    def _send(self, data: bytes):
        pass

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

        rms = np.sqrt(np.mean(samples ** 2))
        print(f"RMS: {rms:.2f}, n. samples: {len(samples)}")

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

        # time since the beginning of the stream
        t = np.arange(frame_count, dtype=np.float64) / self._out_samprate \
            + self._out_time

        # transmit packet start frequency for 4 symbol durations
        samples = .9 * np.sin(
            2. * np.pi * PACKET_START_FREQ * t
        ) * (t < SYMBOL_DURATION * 4)

        # keep silent after the packet start frequency for another 4 symbol
        # durations (8 symbol durations in total).
        symbol_idx = np.astype(np.floor(t * SYMBOL_RATE - 8), np.int32)

        # bit indices: a 2D array where the second axis has a size of
        # self._out_bits_per_symbol and contains bit indices in the raw packet.
        bit_indices = np.stack([
            symbol_idx * self._out_bits_per_symbol + i
            for i in range(self._out_bits_per_symbol)
        ], axis=1)
        bit_indices = np.clip(bit_indices, 0, len(self._out_packet_bits) - 1)

        # a 2D array where the second axis contains the actual bits (0 or 1) for
        # each sample's corresponding symbol.
        bits = self._out_packet_bits[bit_indices]

        # TODO: OFDM modulation. will implement myself. you only implement the
        # two ellipsis above.

        buf_duration = float(frame_count) / self._out_samprate
        self._out_time += buf_duration

        # convert to 16-bit integers
        samples_i16 = (samples * 32767.).astype(np.int16)
        return (samples_i16.tobytes(), pyaudio.paContinue)


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
