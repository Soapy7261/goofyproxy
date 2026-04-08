# Goofy Ahh Proxy

Imagine this. You're living in a place with a very limited internet connection
where only a handful of local websites work and all VPNs, proxies, V2Ray,
HTTP Injector, SlipNet, NetMod, Psiphon, OpenVPN, Npv Tunnel, or whatever the
heck are effectively useless. You've been looking for a reliable way to connect to the
internet for over a month. You're exhausted...

...but all hope is not lost! Some of those local websites that work for you,
might work for people outside your region as well. That doesn't fix anything on
its own, but some of those websites are messengers. More specifically, they let
you communicate with people outside your region with full internet access.
*Some* of them even come with video calls.

If you haven't figured it out yet, the goal is to modulate internet traffic
through goofy ahh data channels. You could flash colorful squares in a video
call, play sine tones with different frequencies in a voice call, send botted
text messages, read and write `.txt` files, flicker a light really fast, send
DNS queries with custom TXT records, transmit pulses to an unused FM frequnecy
with a software-defined radio dongle, the list goes on.

Now let's see what this project provides.

# `GoofyIo`

`GoofyIo` is an abstract class for transferring data through a goofy ahh
channel/medium. It has `send()`, `receive()`, and a few simple rules mentioned
in `goofyio.py`.

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

# Usage

`main.py` runs either the **Goofy Ahh Proxy Server** or the
**Goofy Ahh Proxy Client** based on command line arguments. As usual, you can
run it with `-h` to see a help message printed to your terminal.

Here's an example command to run as the server:
```bash
python ./main.py s --log-level debug
```

And here's an example command to run as the client:
```bash
python ./main.py c --port 1080 --log-level debug
```

# Disclaimer

1. I do not encourage illegal activities. Please stay within legal bounds.
2. Anything you do with this project is solely your responsibility.
3. Everything in the `LICENSE` file.
