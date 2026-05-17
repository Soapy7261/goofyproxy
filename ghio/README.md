# ghio

`GhIo` is a child class of `StorageBasedGoofyIo` that creates and reads
files on a GitHub repository for data transfer.

The main script (`main.py`) provides a command line program that runs
[goofyproxy](../goofyproxy) on top of s3io.

Run `python ./main.py -h` to see a detailed and human-readable help message.

# Note

As of now, I haven't been able to get a lot of success with ghio. It always
fails shortly after the goofy proxy handshake.

# Disclaimer

1. I do not encourage illegal activities. Please stay within legal bounds.
2. Anything you do with this project is solely your responsibility.
