from __future__ import annotations

import socket
import sys


HOST = "127.0.0.1"
PORT = 8791


def _port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


def _create_app():
    from neo import create_app

    return create_app()


if __name__ == "__main__":
    if _port_in_use(HOST, PORT):
        print(f"Neo is already running at http://{HOST}:{PORT} or the port is occupied.", file=sys.stderr)
        print("Stop the existing Python process first, or choose a different port in app.py.", file=sys.stderr)
        raise SystemExit(1)
    app = _create_app()
    app.run(host=HOST, port=PORT, debug=False, threaded=True, use_reloader=False)
else:
    app = _create_app()
