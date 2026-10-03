#!/usr/bin/env python3
"""Bind 127.0.0.1:PRODUCT_PUBLISH_PORT inside the agent and forward to the host.

The product is published on the VM host. This is a single-port TCP hole so the
agent's browser can open http://127.0.0.1:13200 without --network host and
without putting the docker gateway on NO_PROXY.
"""
from __future__ import annotations

import os
import socket
import threading

PORT = int(os.environ.get("PRODUCT_PUBLISH_PORT", "13200"))
HOST_ALIAS = os.environ.get("PRODUCT_UI_HOST", "host.docker.internal")
LISTEN = ("127.0.0.1", PORT)


def pipe(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try:
            src.shutdown(socket.SHUT_RD)
        except OSError:
            pass
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def handle(client: socket.socket, upstream: tuple[str, int]) -> None:
    remote = None
    try:
        remote = socket.create_connection(upstream, timeout=10)
        threading.Thread(target=pipe, args=(client, remote), daemon=True).start()
        pipe(remote, client)
    except OSError:
        pass
    finally:
        try:
            client.close()
        except OSError:
            pass
        if remote is not None:
            try:
                remote.close()
            except OSError:
                pass


def main() -> None:
    dest_host = socket.gethostbyname(HOST_ALIAS)
    upstream = (dest_host, PORT)
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(LISTEN)
    server.listen(32)
    print(
        f"forward-product-ui listen {LISTEN[0]}:{LISTEN[1]} -> {HOST_ALIAS}({dest_host}):{PORT}",
        flush=True,
    )
    while True:
        client, _ = server.accept()
        threading.Thread(target=handle, args=(client, upstream), daemon=True).start()


if __name__ == "__main__":
    main()
