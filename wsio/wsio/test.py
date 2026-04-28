import argparse

# 1. Create the main parser
parser = argparse.ArgumentParser(
    description="Network tool with server/client modes")

# 2. Create subparsers (use `required=True` for Python 3.7+)
subparsers = parser.add_subparsers(
    dest="command", required=True, help="Sub-commands")

# 3. Add subparser for 'server' command
parser_server = subparsers.add_parser("server", help="Run in server mode")
parser_server.add_argument(
    "--buffer", type=int, required=True, help="Buffer size in bytes")

# 4. Add subparser for 'client' command
parser_client = subparsers.add_parser("client", help="Run in client mode")
parser_client.add_argument(
    "--port", type=int, required=True, help="Port number")

# 5. Parse arguments
args = parser.parse_args()

# 6. Handle each command
if args.command == "server":
    print(f"Starting server with buffer size {args.buffer}")
    # your server logic here
elif args.command == "client":
    print(f"Connecting client to port {args.port}")
    # your client logic here
