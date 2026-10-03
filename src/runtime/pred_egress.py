#!/usr/bin/env python3
"""Pred-only HTTP/HTTPS forwarder: allowlisted hosts, optional upstream proxy.

Listens on the host so Docker containers can use it as http_proxy.
Hosts not in the allowlist get 403. GitHub and package registries stay out.
Evaluation must not start this process.
"""
from __future__ import annotations

import argparse
import os
import select
import socket
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse


ALLOWED_PORTS = {80, 443}
TUNNEL_IDLE = 300


def load_allowlist(path: str) -> tuple[set[str], list[str]]:
    exact: set[str] = set()
    suffixes: list[str] = []
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.split("#", 1)[0].strip().lower().rstrip(".")
            if not line:
                continue
            if any(ch.isspace() or ch in "/:@" for ch in line):
                raise SystemExit(f"invalid allowlist entry: {raw!r}")
            if line.startswith("."):
                if len(line) < 3 or "." not in line[1:]:
                    raise SystemExit(f"invalid suffix entry: {raw!r}")
                suffixes.append(line)
            else:
                exact.add(line)
    return exact, suffixes


def host_allowed(host: str, exact: set[str], suffixes: list[str]) -> bool:
    host = host.lower().rstrip(".")
    if not host or host[0].isdigit() or ":" in host:
        # Bare IPv4 / IPv6: deny unless explicitly listed (no accidental GitHub IP).
        return host in exact
    if host in exact:
        return True
    for suf in suffixes:
        if host == suf[1:] or host.endswith(suf):
            return True
    return False


def parse_upstream(url: str) -> tuple[str, int] | None:
    url = (url or "").strip()
    if not url:
        return None
    parsed = urlparse(url if "://" in url else "http://" + url)
    if parsed.scheme not in ("http", "https"):
        raise SystemExit(f"unsupported upstream proxy URL: {url}")
    host = parsed.hostname
    if not host:
        raise SystemExit(f"upstream proxy missing host: {url}")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return host, port


def connect_upstream(upstream: tuple[str, int], target_host: str, target_port: int) -> socket.socket:
    sock = socket.create_connection(upstream, timeout=30)
    req = (
        f"CONNECT {target_host}:{target_port} HTTP/1.1\r\n"
        f"Host: {target_host}:{target_port}\r\n"
        f"Proxy-Connection: keep-alive\r\n"
        f"\r\n"
    ).encode("ascii")
    sock.sendall(req)
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            sock.close()
            raise OSError("upstream proxy closed during CONNECT")
        buf += chunk
        if len(buf) > 65536:
            sock.close()
            raise OSError("upstream CONNECT response too large")
    status_line = buf.split(b"\r\n", 1)[0].decode("ascii", "replace")
    parts = status_line.split(" ", 2)
    if len(parts) < 2 or parts[1] != "200":
        sock.close()
        raise OSError(f"upstream CONNECT failed: {status_line}")
    return sock


def tunnel(left: socket.socket, right: socket.socket) -> None:
    sockets = [left, right]
    try:
        while True:
            readable, _, _ = select.select(sockets, [], [], TUNNEL_IDLE)
            if not readable:
                break
            for src in readable:
                dest = right if src is left else left
                try:
                    data = src.recv(65536)
                except OSError:
                    return
                if not data:
                    return
                try:
                    dest.sendall(data)
                except OSError:
                    return
    finally:
        for s in (left, right):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                s.close()
            except OSError:
                pass


class EgressHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 60

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stderr.write("proxy-egress-prediction: " + (fmt % args) + "\n")

    def _deny(self, message: str) -> None:
        try:
            self.send_response(403, "Forbidden")
            self.send_header("Content-Type", "text/plain")
            body = (message + "\n").encode("ascii", "replace")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass

    def do_CONNECT(self) -> None:
        raw = (self.path or "").strip()
        if "/" in raw or raw.count(":") != 1:
            self._deny("invalid CONNECT target")
            return
        host, port_s = raw.rsplit(":", 1)
        host = host.lower().strip("[]")
        try:
            port = int(port_s)
        except ValueError:
            self._deny("invalid CONNECT port")
            return
        if port not in ALLOWED_PORTS:
            self._deny("port not allowed")
            return
        if not host_allowed(host, self.server.exact, self.server.suffixes):  # type: ignore[attr-defined]
            self._deny(f"host not allowlisted: {host}")
            return
        try:
            if self.server.upstream:  # type: ignore[attr-defined]
                remote = connect_upstream(self.server.upstream, host, port)  # type: ignore[attr-defined]
            else:
                remote = socket.create_connection((host, port), timeout=30)
        except OSError as exc:
            self._deny(f"connect failed: {exc}")
            return
        try:
            self.send_response(200, "Connection Established")
            self.send_header("Connection", "close")
            self.end_headers()
        except OSError:
            remote.close()
            return
        tunnel(self.connection, remote)

    def do_GET(self) -> None:
        self._proxy_http()

    def do_POST(self) -> None:
        self._proxy_http()

    def do_PUT(self) -> None:
        self._proxy_http()

    def do_HEAD(self) -> None:
        self._proxy_http()

    def do_DELETE(self) -> None:
        self._proxy_http()

    def do_PATCH(self) -> None:
        self._proxy_http()

    def _proxy_http(self) -> None:
        parsed = urlparse(self.path)
        if parsed.scheme not in ("http",) or not parsed.hostname:
            self._deny("absolute http URL required")
            return
        host = parsed.hostname.lower()
        port = parsed.port or 80
        if port not in ALLOWED_PORTS:
            self._deny("port not allowed")
            return
        if not host_allowed(host, self.server.exact, self.server.suffixes):  # type: ignore[attr-defined]
            self._deny(f"host not allowlisted: {host}")
            return
        # HTTPS APIs use CONNECT; refuse sending plaintext via a second hop
        # except when talking to the allowlisted host directly or via CONNECT.
        self._deny("plain HTTP proxying disabled; use HTTPS CONNECT")


def main() -> int:
    parser = argparse.ArgumentParser(description="Pred allowlist egress proxy")
    parser.add_argument("--allowlist", required=True)
    parser.add_argument("--upstream", default="")
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--portfile", required=True)
    args = parser.parse_args()

    exact, suffixes = load_allowlist(args.allowlist)

    if not exact and not suffixes:
        raise SystemExit("allowlist is empty")

    upstream = parse_upstream(args.upstream)
    server = ThreadingHTTPServer((args.bind, args.port), EgressHandler)
    server.exact = exact  # type: ignore[attr-defined]
    server.suffixes = suffixes  # type: ignore[attr-defined]
    server.upstream = upstream  # type: ignore[attr-defined]

    port = server.server_address[1]
    with open(args.portfile, "w", encoding="utf-8") as fh:
        fh.write(str(port) + "\n")
    sys.stderr.write(
        f"proxy-egress-prediction listen {args.bind}:{port} "
        f"upstream={args.upstream or 'direct'} "
        f"exact={len(exact)} suffix={len(suffixes)}\n"
    )
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
