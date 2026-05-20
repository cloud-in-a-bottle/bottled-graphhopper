#!/usr/bin/env python3
"""OpenHost auth proxy for GraphHopper (Pattern E — no app-level SSO).

GraphHopper has no user accounts. OpenHost zone_auth gates all access.
Listens on 0.0.0.0:8080, proxies to GraphHopper at 127.0.0.1:8989.
Routes /admin* to the admin UI at 127.0.0.1:8091 (owner-only).
"""

from __future__ import annotations

import http.client
import http.server
import json
import os
import socket
import socketserver
import ssl
import sys
import threading
import urllib.parse
import urllib.request

LISTEN_ADDR = "0.0.0.0"
LISTEN_PORT = int(os.environ.get("AUTH_PROXY_LISTEN_PORT", "8080"))
UPSTREAM_HOST = "127.0.0.1"
UPSTREAM_PORT = int(os.environ.get("AUTH_PROXY_UPSTREAM_PORT", "8989"))
ADMIN_HOST = "127.0.0.1"
ADMIN_PORT = 8091
SETUP_MODE = os.environ.get("GRAPHHOPPER_SETUP_MODE", "") == "1"

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
        # Log geocode and admin requests, suppress the rest
        path = getattr(self, "path", "")
        if "/geocode" in path or "/admin" in path or "/config.js" in path:
            sys.stderr.write(f"[auth_proxy] {self.address_string()} {fmt % args}\n")
            sys.stderr.flush()

    def _serve_healthz(self):
        if SETUP_MODE:
            body = b'{"status":"setup"}'
            code = 200
        else:
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

    def _serve_config_js(self):
        """Serve a patched config.js that enables geocoding."""
        body = b"""const config = {
    routingApi: location.origin + '/',
    geocodingApi: location.origin + '/',
    defaultTiles: 'OpenStreetMap',
    keys: {
        graphhopper: "",
        maptiler: "",
        omniscale: "",
        thunderforest: "",
        kurviger: ""
    },
    routingGraphLayerAllowed: true,
    request: {
        details: [
            'road_class',
            'road_environment',
            'max_speed',
            'average_speed',
        ],
        snapPreventions: ['ferry'],
    },
    profile_group_mapping: {},
}
"""
        self.send_response(200)
        self.send_header("Content-Type", "application/javascript")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def _serve_maps_with_geocoding_patch(self):
        """Proxy /maps/ but inject a geocoding shim script.

        The GH Maps UI's autocomplete uses a complex Overpass-based pipeline
        that doesn't work with just Nominatim. We inject a small script that
        intercepts fetch() calls to /geocode with provider=default and converts
        them to direct Nominatim lookups, returning results the UI can display.
        """
        # Fetch original /maps/ from upstream
        try:
            conn = http.client.HTTPConnection(UPSTREAM_HOST, UPSTREAM_PORT, timeout=30)
            conn.request("GET", self.path)
            resp = conn.getresponse()
            html = resp.read()
            conn.close()
        except Exception:
            self._proxy_to(UPSTREAM_HOST, UPSTREAM_PORT)
            return

        # Inject geocoding patch before </head>
        patch = b"""<script>
// Geocoding patch: intercept fetch to /geocode?provider=default
// and return Nominatim results in a format the dropdown understands
(function() {
    const origFetch = window.fetch;
    window.fetch = function(url, opts) {
        if (typeof url === 'string' && url.includes('/geocode?') && url.includes('provider=default')) {
            const u = new URL(url, location.origin);
            const q = u.searchParams.get('q');
            if (!q || q.length < 2) return origFetch.apply(this, arguments);
            const locale = u.searchParams.get('locale') || 'en';
            const nomUrl = 'https://nominatim.openstreetmap.org/search?format=json&limit=5&addressdetails=1' +
                '&accept-language=' + encodeURIComponent(locale) +
                '&q=' + encodeURIComponent(q);
            return fetch(nomUrl, {headers: {'Accept': 'application/json'}})
                .then(r => r.json())
                .then(results => {
                    const hits = results.map(r => ({
                        point: {lat: parseFloat(r.lat), lng: parseFloat(r.lon)},
                        name: r.display_name,
                        osm_id: r.osm_id,
                        osm_type: r.osm_type,
                        osm_key: r.class || '',
                        osm_value: r.type || '',
                        country: (r.address||{}).country || '',
                        city: (r.address||{}).city || (r.address||{}).town || (r.address||{}).village || '',
                        state: (r.address||{}).state || '',
                        street: (r.address||{}).road || '',
                        housenumber: (r.address||{}).house_number || '',
                        postcode: (r.address||{}).postcode || '',
                        extent: r.boundingbox ? [
                            parseFloat(r.boundingbox[2]), parseFloat(r.boundingbox[0]),
                            parseFloat(r.boundingbox[3]), parseFloat(r.boundingbox[1])
                        ] : undefined
                    }));
                    return new Response(JSON.stringify({hits: hits, locale: locale}), {
                        status: 200,
                        headers: {'Content-Type': 'application/json'}
                    });
                })
                .catch(() => origFetch.apply(this, arguments));
        }
        return origFetch.apply(this, arguments);
    };
})();
</script>
"""
        html = html.replace(b'</head>', patch + b'</head>')

        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(html)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _geocode_proxy(self):
        """Proxy geocoding requests to Nominatim, translating to GH format.

        GraphHopper Maps sends: GET /geocode?q=...&limit=...&locale=...
        We query Nominatim and translate the response to GH geocoding format.
        """
        qs = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(qs)
        query = params.get("q", [""])[0]
        limit = params.get("limit", ["5"])[0]
        locale = params.get("locale", ["en"])[0]

        if not query:
            self._json_response({"hits": []})
            return

        # Query Nominatim
        nom_params = urllib.parse.urlencode({
            "q": query,
            "format": "json",
            "limit": limit,
            "accept-language": locale,
            "addressdetails": "1",
        })
        nom_url = f"https://nominatim.openstreetmap.org/search?{nom_params}"

        try:
            req = urllib.request.Request(nom_url, headers={
                "User-Agent": "OpenHost-GraphHopper/1.0",
                "Accept": "application/json",
            })
            ctx = ssl.create_default_context()
            with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
                nom_results = json.loads(resp.read().decode())
        except Exception as e:
            print(f"[auth_proxy] Nominatim error: {e}", file=sys.stderr)
            self._json_response({"hits": []})
            return

        # Translate Nominatim results to GraphHopper geocoding format
        hits = []
        for r in nom_results:
            try:
                hit = {
                    "point": {
                        "lat": float(r["lat"]),
                        "lng": float(r["lon"]),
                    },
                    "osm_id": r.get("osm_id", ""),
                    "osm_type": r.get("osm_type", ""),
                    "name": r.get("display_name", ""),
                    "country": r.get("address", {}).get("country", ""),
                    "city": (
                        r.get("address", {}).get("city", "")
                        or r.get("address", {}).get("town", "")
                        or r.get("address", {}).get("village", "")
                    ),
                    "state": r.get("address", {}).get("state", ""),
                    "street": r.get("address", {}).get("road", ""),
                    "housenumber": r.get("address", {}).get("house_number", ""),
                    "postcode": r.get("address", {}).get("postcode", ""),
                }
                # Bounding box
                if "boundingbox" in r and len(r["boundingbox"]) == 4:
                    bb = r["boundingbox"]
                    hit["extent"] = [float(bb[2]), float(bb[0]), float(bb[3]), float(bb[1])]
                hits.append(hit)
            except (KeyError, ValueError):
                continue

        self._json_response({"hits": hits, "locale": locale})

    def _json_response(self, data, code=200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location):
        try:
            self.send_response(302)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()
        except OSError:
            pass

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

        # Setup mode: redirect everything else to the region picker
        if SETUP_MODE:
            self._redirect("/admin")
            return

        # Serve patched config.js with geocoding enabled
        if path_only == "/maps/config.js":
            self._serve_config_js()
            return

        # Serve /maps/ with geocoding shim injected
        if path_only in ("/maps/", "/maps"):
            self._serve_maps_with_geocoding_patch()
            return

        # Geocoding proxy (Nominatim -> GH format) for nominatim provider
        if path_only == "/geocode":
            self._geocode_proxy()
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
