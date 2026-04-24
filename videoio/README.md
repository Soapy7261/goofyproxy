# VideoIo

`VideoIo` is a child class of `GoofyIo` that uses video calls to transfer data.

1. Each side opens up a window and displays a grid of colorful squares encoding
binary data.

2. Each side uses something like OBS Studio's **Virtual Camera** to disguise its
screen as a webcam to the messenger website.

3. Each side takes screenshots of the other side's video feed to decode its
data.

VideoIo uses QR code at the beginning for handshake and deducing the peer's
video feed coordinates on the screen.

# Prerequisites

The following Python packages are required for this project.

```
numpy scipy PySide6 mss Pillow qrcode pyzbar netifaces2 opencv-python
```

## Windows

Make sure to install the
[Microsoft Visual C++ Redistributables.](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist?view=msvc-170)

## Linux

Unfortunately, Wayland-based compositors (GNOME with Wayland, hyprland, etc.)
don't seem to work with `mss` for taking screenshots. If you know a fix, please
open an issue.

## Fractional Scaling

For more precision, try to avoid non-integer display scaling.

## Computing Power

For fast data transfer, the program needs to process many screenshots per
second, so you're gonna need a fairly powerful computer to be able to keep up,
otherwise VideoIo will keep falling behind and asking for retransmissions.

# Usage

`main.py` can run in different modes based on command line arguments.

- `server`: **Goofy Ahh Proxy Server** using VideoIo
- `client`: **Goofy Ahh Proxy Client** using VideoIo
- `chat`: Chat mode for testing VideoIo
- `list_monitors`: Prints the list of available monitors.

Here's an example command to run as the server:
```bash
python ./main.py server -f 720x540-12-3@1 -S max -P bean -s 1.5 -c 2 -l debug -L goofy-server-log.txt
```

And here's an example command to run as the client:
```bash
python ./main.py client -p 4000 -b 512 -f 720x540-12-3@1 -S bean -P max -s 1.5 -c 2 -l debug -L goofy-client-log.txt
```

**Run the script with `-h` to see a detailed and human-readable help message.**

# Disclaimer

1. I do not encourage illegal activities. Please stay within legal bounds.
2. Anything you do with this project is solely your responsibility.
