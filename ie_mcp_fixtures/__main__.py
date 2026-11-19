"""Serve the fixture site by hand: ``python -m ie_mcp_fixtures [--host H]``."""

import argparse
import os
import threading

from .server import start_fixture_server


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m ie_mcp_fixtures",
        description="Serve the ie-mcp legacy fixture site on two origins.")
    parser.add_argument("--host", default=os.environ.get("IE_FIXTURE_HOST", "127.0.0.1"),
                        help="address to bind (default: IE_FIXTURE_HOST or 127.0.0.1)")
    parser.add_argument("--port", type=int, default=0, help="main port (default: free port)")
    parser.add_argument("--alt-port", type=int, default=0,
                        help="second-origin port (default: free port)")
    args = parser.parse_args(argv)

    with start_fixture_server(args.host, args.port, args.alt_port) as server:
        print("Fixture site:  " + server.base_url, flush=True)
        print("Second origin: " + server.alt_base_url, flush=True)
        print("Press Ctrl+C to stop.", flush=True)
        try:
            # Event.wait with a timeout keeps Ctrl+C responsive on Windows.
            stop = threading.Event()
            while not stop.wait(0.5):
                pass
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
