#!/usr/bin/env python3
"""On the VM host: docker-bridge:13200 -> 127.0.0.1:13200.

Product is published on 127.0.0.1 only. The agent forwarder reaches the
host via host.docker.internal (bridge gateway), so this hop must exist.
Does not bind 0.0.0.0; only the docker gateway address.
"""
from __future__ import annotations

import os
import socket
import subprocess
import threading

PORT = int(os.environ.get("PRODUCT_PUBLISH_PORT", "13200"))
UPSTREAM = ("127.0.0.1", PORT)


def docker_gateway() -> str:
    raw = os.environ.get("DOCKER_BRIDGE_GATEWAY", "").strip()
    if raw:
        return raw
    p = subprocess.run(
        [
            "docker",
            "network",
            "inspect",
            "bridge",
            "--format",
            "{{(index .IPAM.Config 0).Gateway}}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    gw = (p.stdout or "").strip()
    return gw or "172.17.0.1"


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


def handle(client: socket.socket) -> None:
    remote = None
    try:
        remote = socket.create_connection(UPSTREAM, timeout=10)
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
    listen = (docker_gateway(), PORT)
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(listen)
    server.listen(32)
    print(f"forward-product-ui-host listen {listen[0]}:{listen[1]} -> {UPSTREAM[0]}:{UPSTREAM[1]}", flush=True)
    while True:
        client, _ = server.accept()
        threading.Thread(target=handle, args=(client,), daemon=True).start()


if __name__ == "__main__":
    main()
