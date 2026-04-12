import argparse
from enum import StrEnum
from typing import Self

from goofy_client import GoofyClient
from goofy_server import GoofyServer
from audioio import AudioIo, AudioDeviceType, Profile, paudio_terminate
from common import *


def run(args: argparse.Namespace):
    if args.mode == "list_audio_devices":
        input_devices = AudioIo.list_devices(AudioDeviceType.Input)
        output_devices = AudioIo.list_devices(AudioDeviceType.Output)

        s = "[input devices]\n"
        for i in range(len(input_devices)):
            device = input_devices[i]
            s += f"{i}. "
            s += device.name
            if device.is_default_input:
                s += " (default)"
            s += "\n"
        if not input_devices:
            s += "empty\n"

        s += "\n[output devices]\n"
        for i in range(len(output_devices)):
            device = output_devices[i]
            s += f"{i}. "
            s += device.name
            if device.is_default_output:
                s += " (default)"
            s += "\n"
        if not output_devices:
            s += "empty\n"

        print(s)
        return

    input_devices = AudioIo.list_devices(AudioDeviceType.Input)
    output_devices = AudioIo.list_devices(AudioDeviceType.Output)
    if not input_devices or not output_devices:
        raise Exception(
            f"need at least 1 input and 1 output audio device. found "
            f"{len(input_devices)} input devices and {len(output_devices)} "
            f"output devices."
        )
    if args.input_device < 0 or args.input_device >= len(input_devices):
        raise Exception("invalid input audio device index")
    if args.output_device < 0 or args.output_device >= len(output_devices):
        raise Exception("invalid output audio device index")

    gio = AudioIo(
        input_devices[args.input_device],
        output_devices[args.output_device],
        True,
        Profile.Fast,
        Profile.Fast
    )

    print("spinnin'")
    while True:
        msg = input()
        gio.send(msg.encode())

    if args.mode == "server":
        GoofyServer(gio)
    elif args.mode == "client":
        if not args.port:
            print("port is required in client mode")
            return

        GoofyClient(
            gio,
            host="0.0.0.0",
            port=args.port
        )
    else:
        print("invalid mode")


class LogLevel(StrEnum):
    Debug = "debug"
    Info = "info"
    Warning = "warning"
    Error = "error"
    Fatal = "fatal"

    def to_int(self) -> int:
        if self == self.Debug:
            return logging.DEBUG
        elif self == self.Info:
            return logging.INFO
        elif self == self.Warning:
            return logging.WARNING
        elif self == self.Error:
            return logging.ERROR
        elif self == self.Fatal:
            return logging.FATAL
        raise ValueError("invalid enum value")

    @classmethod
    def from_int(cls, i: int) -> Self:
        if i == logging.DEBUG:
            return cls.Debug
        elif i == logging.INFO:
            return cls.Info
        elif i == logging.WARNING:
            return cls.Warning
        elif i == logging.ERROR:
            return cls.Error
        elif i == logging.FATAL:
            return cls.Fatal
        raise ValueError("invalid log level number")


def main():
    global LOG_CONFIG

    # command line parser
    parser = argparse.ArgumentParser(
        description="goofy proxy using TxtFileIo"
    )
    parser.add_argument(
        "mode",
        choices=["server", "client", "list_audio_devices"],
        help="which mode to run in"
    )
    parser.add_argument(
        "-i",
        "--input-device",
        type=int,
        default=0,
        help="input audio device index (use list_audio_devices), will use the "
        "default if not provided."
    )
    parser.add_argument(
        "-o",
        "--output-device",
        type=int,
        default=0,
        help="output audio device index (use list_audio_devices), will use the "
        "default if not provided."
    )
    parser.add_argument(
        "-p",
        "--port",
        type=int,
        help="local SOCKS5 proxy server port in client mode"
    )
    parser.add_argument(
        "-l",
        "--log-level",
        type=LogLevel,
        default=LogLevel.from_int(LOG_CONFIG["level"]),
        help="one of: debug, info, warning, error, fatal"
    )
    parser.add_argument(
        "-f",
        "--log-file",
        type=str,
        help="optional path to a log file, e.g. 'log.txt'"
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="disable terminal colors"
    )

    # parse
    args = parser.parse_args()

    # logging settings
    LOG_CONFIG["level"] = args.log_level.to_int()
    LOG_CONFIG["colorize"] = not args.no_color
    if args.log_file:
        try:
            f = open(args.log_file, "a")
            LOG_CONFIG["file"] = f
        except Exception as e:
            logger.fatal(f"failed to open log file: {format_exception(e)}")
            return

    # run
    try:
        run(args)
    except BaseException as e:
        logger.fatal(format_exception(e))
    finally:
        if isinstance(LOG_CONFIG["file"], io.TextIOWrapper):
            LOG_CONFIG["file"].flush()
            LOG_CONFIG["file"].close()

        paudio_terminate()


if __name__ == "__main__":
    main()
