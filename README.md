# Goofy Ahh Proxy

Picture this. You're living in a place with a very limited internet connection
where only a handful of local websites work and all VPNs, proxies, V2Ray,
HTTP Injector, SlipNet, NetMod, Psiphon, OpenVPN, Npv Tunnel, or whatever the
heck are effectively useless. You've been looking for a reliable way to connect
to the internet for months. You're exhausted...

...but all hope is not lost! Some of those local websites that work for you,
might work for people outside your region as well. That doesn't fix anything on
its own, but some of those websites are messengers. More specifically, they let
you communicate with people outside your region with full internet access.*Some*
of them even come with video calls.

If you haven't figured it out yet, the goal here is to modulate internet traffic
through goofy ahh data channels. You could flash colorful squares in a video
call, play sine tones with different frequencies in a voice call, send botted
text messages, read and write `.txt` files, flicker a light really fast, embed
data in DNS queries, transmit pulses to an unused radio frequnecy with a
software-defined radio dongle, the list goes on!

Now let's see what this project provides.

# `GoofyIo`

`GoofyIo` is an abstract class for transferring data through a goofy ahh
channel/medium. It has `send()`, `receive()`, and a few simple rules mentioned
in `goofyio.py`.

# `VideoIo`

`VideoIo` is a child class of `GoofyIo` that uses video calls to transfer data.

1. Each side opens up a window and displays a grid of colorful squares encoding
binary data.

2. Each side uses something like OBS Studio's **Virtual Camera** to disguise its
screen as a webcam to the messenger website.

3. Each side takes screenshots of the other side's video feed to decode its
data.

`VideoIo` uses QR code at the beginning for handshake and deducing the peer's
video feed coordinates on the screen.

# `GoofyServer`

The **Goofy Ahh Proxy Server** is run by the lucky volunteer with normal
internet access. It works with any well-implemented goofy ahh channel
(`GoofyIo`).

See `goofy_server.py` for more details. It's human-readable code.

# `GoofyClient`

The **Goofy Ahh Proxy Client** is run by the unfortunate person with no proper
internet access. It works with any well-implemented goofy ahh channel
(`GoofyIo`) as long as the server is already running on the other side.

The **Goofy Ahh Proxy Client** runs a local SOCKS5 proxy server that other
devices or programs on the LAN can connect to. It then communicates with the
**Goofy Ahh Proxy Server** by sending commands (open socket, bind, etc.) and
receiving events (update socket status, bind info, etc.). Once a socket is
connected, the **Goofy Ahh Proxy Client and Server** both start relaying data
by sending socket IO packets.

See `goofy_client.py` for more details. It's human-readable code.

> [!NOTE]
> The word "packet" here is referring to `GoofyPacket` to be precise. See
> `common.py` to learn more.

# Prerequisites

The following Python packages are required for this project.

```
numpy scipy PySide6 mss Pillow qrcode pyzbar netifaces2 opencv-python
```

## Computing Power

For fast data transfer, the program needs to take and analyze many screenshots
every second, and my code isn't super optimized (skill issue), so you're gonna
need a fairly powerful computer to be able to keep up, otherwise the program
will keep falling behind and asking for retransmissions.

## Fractional Scaling

For more precision, try to avoid non-integer display scaling.

## Linux-specific

Unfortunately, Wayland-based compositors (GNOME with Wayland, hyprland, etc.)
don't seem to work with `mss` for taking screenshots. If you know a fix, please
open an issue.

# Usage

`main.py` can run in different modes based on command line arguments.

- `server`: **Goofy Ahh Proxy Server** using `VideoIo`
- `client`: **Goofy Ahh Proxy Client** using `VideoIo`
- `chat`: Chat mode for testing `VideoIo`
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
