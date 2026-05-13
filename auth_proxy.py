#!/usr/bin/env python3
"""OpenHost auth proxy for GraphHopper (Pattern E — no app-level SSO).

GraphHopper has no user accounts. OpenHost zone_auth gates all access.
Listens on 0.0.0.0:8080, proxies to GraphHopper at 127.0.0.1:8989.
Routes /admin* to the admin UI at 127.0.0.1:8091 (owner-only).
"""

from __future__ import annotations

import http.client
import http.server
import os
import socket
import socketserver
import sys
import threading

LISTEN_ADDR = "0.0.0.0"
LISTEN_PORT = int(os.environ.get("AUTH_PROXY_LISTEN_PORT", "8080"))
UPSTREAM_HOST = "127.0.0.1"
UPSTREAM_PORT = int(os.environ.get("AUTH_PROXY_UPSTREAM_PORT", "8989"))
ADMIN_HOST = "127.0.0.1"
ADMIN_PORT = 8091

STRIP_HEADERS = frozenset(h.lower() for h in [
    "x-openhost-is-owner", "x-openhost-app-token",
    "x-openhost-user", "x-openhost-zone-domain", "x-openhost-app-name",
])

HOP_BY_HOP = frozenset(h.lower() for h in [
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
])

MAX_BODY = 10 * 1024 * 1024


class ProxyHandler(http.server.BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass

    def _serve_healthz(self):
        try:
            s = socket.create_connection((UPSTREAM_HOST, UPSTREAM_PORT), timeout=2)
            s.close()
            body = b'{"status":"ok"}'
            code = 200
        except OSError:
            body = b'{"status":"starting"}'
            code = 503
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _proxy_to(self, host, port):
        """Proxy current request to the given host:port."""
        body = None
        cl = self.headers.get("Content-Length")
        if cl:
            try:
                body = self.rfile.read(min(int(cl), MAX_BODY))
            except (ValueError, OSError):
                pass

        headers = {}
        for key in self.headers:
            lk = key.lower()
            if lk in STRIP_HEADERS or lk in HOP_BY_HOP or lk == "host":
                continue
            values = self.headers.get_all(key)
            if values:
                headers[key] = ", ".join(values)

        fh = self.headers.get("X-Forwarded-Host")
        headers["Host"] = fh if fh else f"{host}:{port}"
        if "X-Forwarded-Proto" not in headers:
            headers["X-Forwarded-Proto"] = "https"

        try:
            conn = http.client.HTTPConnection(host, port, timeout=120)
            conn.request(self.command, self.path, body=body, headers=headers)
            resp = conn.getresponse()
        except (ConnectionRefusedError, socket.timeout, OSError):
            try:
                self.send_error(502, "bad gateway")
            except OSError:
                pass
            return

        try:
            resp_body = resp.read()
        except Exception:
            try:
                self.send_error(502, "bad gateway")
            except OSError:
                pass
            return

        try:
            self.send_response(resp.status)
            for key, value in resp.getheaders():
                lk = key.lower()
                if lk in HOP_BY_HOP or lk in ("transfer-encoding", "content-length"):
                    continue
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(resp_body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(resp_body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            conn.close()

    def _handle(self):
        path_only = self.path.split("?", 1)[0]

        if path_only == "/healthz":
            self._serve_healthz()
            return

        # Route /admin* to admin service (owner-only)
        if path_only.startswith("/admin"):
            is_owner = self.headers.get("X-OpenHost-Is-Owner", "").lower() == "true"
            if not is_owner:
                self.send_error(403, "Admin access requires OpenHost owner")
                return
            self._proxy_to(ADMIN_HOST, ADMIN_PORT)
            return

        # Everything else goes to GraphHopper
        self._proxy_to(UPSTREAM_HOST, UPSTREAM_PORT)

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _handle


class ThreadedServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    server = ThreadedServer((LISTEN_ADDR, LISTEN_PORT), ProxyHandler)
    print(f"[auth_proxy] {LISTEN_ADDR}:{LISTEN_PORT} -> GH {UPSTREAM_HOST}:{UPSTREAM_PORT}, "
          f"admin {ADMIN_HOST}:{ADMIN_PORT}", file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
