#!/usr/bin/env python3
"""GraphHopper admin UI — region selection and graph management.

Serves /admin with a web UI that lets the OpenHost owner:
  - Browse available Geofabrik regions
  - Select and download a region
  - Build the routing graph
  - Monitor download/build progress

Only accessible to the OpenHost owner (X-OpenHost-Is-Owner: true).
Runs on 127.0.0.1:8091, proxied by auth_proxy.py.
"""

from __future__ import annotations

import http.server
import json
import os
import shutil
import signal
import socketserver
import subprocess
import sys
import threading
import time
import urllib.request

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 8091

APP_DATA = os.environ.get("OPENHOST_APP_DATA_DIR", "/data/app_data/graphhopper")
GH_DIR = "/graphhopper"
PBF_FILE = os.path.join(APP_DATA, "region.osm.pbf")
GRAPH_DIR = os.path.join(APP_DATA, "graph-cache")
REGION_CONF = os.path.join(APP_DATA, "region.conf")
STATUS_FILE = os.path.join(APP_DATA, ".admin_status.json")
SETUP_SENTINEL = os.path.join(APP_DATA, ".setup_complete")
SETUP_MODE = os.environ.get("GRAPHHOPPER_SETUP_MODE", "") == "1"

GEOFABRIK_INDEX = "https://download.geofabrik.de/index-v1.json"

# Global state for background operations
_state_lock = threading.Lock()
_state = {
    "operation": None,     # None, "downloading", "building", "ready", "error"
    "progress": "",
    "error": "",
    "current_region": "",
}


def _read_current_region() -> str:
    if os.path.isfile(REGION_CONF):
        with open(REGION_CONF) as f:
            for line in f:
                line = line.strip()
                if line.startswith("PBF_URL="):
                    return line.split("=", 1)[1].strip()
    return ""


def _set_state(**kwargs):
    with _state_lock:
        _state.update(kwargs)


def _get_state() -> dict:
    with _state_lock:
        return dict(_state)


def _fetch_geofabrik_index() -> list[dict]:
    """Fetch and parse Geofabrik index, return sorted region list."""
    cache_file = os.path.join(APP_DATA, ".geofabrik_index.json")
    cache_age = 86400  # 1 day

    # Use cache if fresh
    if os.path.isfile(cache_file):
        age = time.time() - os.path.getmtime(cache_file)
        if age < cache_age:
            with open(cache_file) as f:
                return json.load(f)

    try:
        req = urllib.request.Request(GEOFABRIK_INDEX, headers={"User-Agent": "OpenHost-GraphHopper/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        print(f"[admin] Failed to fetch Geofabrik index: {e}", file=sys.stderr)
        # Fall back to cache if available
        if os.path.isfile(cache_file):
            with open(cache_file) as f:
                return json.load(f)
        return []

    regions = []
    for feature in data.get("features", []):
        props = feature.get("properties", {})
        urls = props.get("urls", {})
        pbf_url = urls.get("pbf", "")
        if not pbf_url:
            continue

        name = props.get("name", "")
        parent = props.get("parent", "")
        region_id = props.get("id", "")

        # Build a readable path like "Europe / Germany" or "North America / US / California"
        path_parts = []
        if parent:
            path_parts.append(parent.replace("/", " / ").title())
        path_parts.append(name)
        display = " / ".join(path_parts)

        regions.append({
            "id": region_id,
            "name": name,
            "display": display,
            "parent": parent,
            "pbf_url": pbf_url,
        })

    regions.sort(key=lambda r: r["display"])

    # Cache
    try:
        with open(cache_file, "w") as f:
            json.dump(regions, f)
    except OSError:
        pass

    return regions


def _background_download_and_build(pbf_url: str, region_name: str):
    """Download PBF and build graph in background."""
    try:
        _set_state(operation="downloading", progress="Starting download...", error="")

        # Remove old data
        if os.path.isfile(PBF_FILE):
            os.remove(PBF_FILE)
        if os.path.isdir(GRAPH_DIR):
            shutil.rmtree(GRAPH_DIR)

        # Update region.conf
        with open(REGION_CONF, "w") as f:
            f.write(f"# Region: {region_name}\n")
            f.write(f"PBF_URL={pbf_url}\n")

        # Download
        tmp_file = PBF_FILE + ".tmp"
        _set_state(progress=f"Downloading {region_name}...")

        proc = subprocess.Popen(
            ["wget", "-q", "--show-progress", "-O", tmp_file, pbf_url],
            stderr=subprocess.PIPE,
            text=True,
        )
        # Read wget progress from stderr
        last_update = time.time()
        for line in proc.stderr:
            line = line.strip()
            if line and time.time() - last_update > 2:
                # Parse wget progress line
                if "%" in line:
                    _set_state(progress=f"Downloading: {line[:80]}")
                last_update = time.time()
        proc.wait()

        if proc.returncode != 0:
            _set_state(operation="error", error="Download failed", progress="")
            return

        os.rename(tmp_file, PBF_FILE)
        size_mb = os.path.getsize(PBF_FILE) / (1024 * 1024)
        _set_state(progress=f"Download complete ({size_mb:.0f} MB). Building graph...")

        # Build graph
        _set_state(operation="building", progress="Building routing graph (this may take 10-60 minutes)...")

        # Read heap settings
        cgroup_file = "/sys/fs/cgroup/memory.max"
        if not os.path.isfile(cgroup_file):
            cgroup_file = "/sys/fs/cgroup/memory/memory.limit_in_bytes"
        try:
            with open(cgroup_file) as f:
                limit = f.read().strip()
            if limit == "max":
                total_mb = 8192
            else:
                total_mb = int(limit) // (1024 * 1024)
        except (OSError, ValueError):
            total_mb = 8192
        heap_mb = max(1024, total_mb * 75 // 100)

        config_file = os.path.join(GH_DIR, "config-example.yml")
        jar_files = [f for f in os.listdir(GH_DIR) if f.startswith("graphhopper-web-") and f.endswith(".jar")]
        if not jar_files:
            _set_state(operation="error", error="GraphHopper JAR not found", progress="")
            return

        proc = subprocess.Popen(
            [
                "java",
                f"-Xmx{heap_mb}m", f"-Xms{heap_mb // 2}m",
                f"-Ddw.graphhopper.datareader.file={PBF_FILE}",
                f"-Ddw.graphhopper.graph.location={GRAPH_DIR}",
                "-jar", os.path.join(GH_DIR, jar_files[0]),
                "import", config_file,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=GH_DIR,
        )

        last_update = time.time()
        for line in proc.stdout:
            line = line.strip()
            if time.time() - last_update > 5:
                # Show interesting progress lines
                if any(kw in line for kw in ["pass1", "pass2", "processed", "sorting", "CH", "nodes", "edges", "Graph"]):
                    short = line[line.find("]") + 2:] if "]" in line else line
                    _set_state(progress=f"Building: {short[:100]}")
                last_update = time.time()
        proc.wait()

        if proc.returncode != 0:
            _set_state(operation="error", error="Graph build failed (check app logs)", progress="")
            if os.path.isdir(GRAPH_DIR):
                shutil.rmtree(GRAPH_DIR)
            return

        # Mark setup as complete so next boot starts in normal mode
        try:
            with open(SETUP_SENTINEL, "w") as f:
                f.write(f"{region_name}\n")
        except OSError:
            pass

        _set_state(
            operation="ready",
            progress=f"Graph built successfully for {region_name}. Restarting...",
            current_region=region_name,
        )

        print(f"[admin] Graph built. Sending SIGTERM to restart.", file=sys.stderr, flush=True)
        os.kill(1, signal.SIGTERM)

    except Exception as e:
        _set_state(operation="error", error=str(e), progress="")


ADMIN_HTML = """<!DOCTYPE html>
<html>
<head>
<title id="page-title">GraphHopper</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: system-ui, sans-serif; max-width: 800px; margin: 2rem auto; padding: 0 1rem; color: #333; }
  h1 { margin-bottom: 0.5rem; }
  .subtitle { color: #666; margin-bottom: 1.5rem; }
  .card { background: #f8f9fa; border: 1px solid #dee2e6; border-radius: 8px; padding: 1.5rem; margin-bottom: 1rem; }
  .card h2 { margin-bottom: 0.5rem; font-size: 1.1rem; }
  .status { padding: 0.5rem 1rem; border-radius: 4px; margin-bottom: 1rem; }
  .status.ok { background: #d4edda; color: #155724; }
  .status.busy { background: #fff3cd; color: #856404; }
  .status.error { background: #f8d7da; color: #721c24; }
  .status.setup { background: #d1ecf1; color: #0c5460; }
  label { display: block; margin-bottom: 0.3rem; font-weight: 600; }
  select, input[type=text] { width: 100%; padding: 0.5rem; border: 1px solid #ccc; border-radius: 4px; font-size: 1rem; margin-bottom: 1rem; }
  button { padding: 0.6rem 1.5rem; border: none; border-radius: 4px; cursor: pointer; font-size: 1rem; }
  button.primary { background: #0d6efd; color: white; }
  button.primary:disabled { background: #6c757d; cursor: not-allowed; }
  button.danger { background: #dc3545; color: white; }
  .progress { margin-top: 0.5rem; font-style: italic; color: #666; }
  .search-box { position: relative; }
  .search-box input { margin-bottom: 0; }
  #region-list { max-height: 300px; overflow-y: auto; border: 1px solid #ccc; border-top: none; border-radius: 0 0 4px 4px; display: none; }
  #region-list div { padding: 0.5rem; cursor: pointer; }
  #region-list div:hover { background: #e9ecef; }
  #region-list div.selected { background: #0d6efd; color: white; }
  .meta { font-size: 0.85rem; color: #888; margin-top: 0.5rem; }
</style>
</head>
<body>
<h1 id="heading">GraphHopper</h1>
<p id="subtitle" class="subtitle"></p>

<div id="status-box" class="status ok">Loading...</div>

<div class="card">
  <h2 id="region-heading">Select Region</h2>
  <p class="meta">Data from <a href="https://download.geofabrik.de/">Geofabrik</a>. Type to search.</p>
  <br>
  <div class="search-box">
    <input type="text" id="search" placeholder="Search regions (e.g. California, Germany, Japan)..." autocomplete="off">
    <div id="region-list"></div>
  </div>
  <input type="hidden" id="selected-url" value="">
  <input type="hidden" id="selected-name" value="">
  <div id="selected-display" style="margin-bottom:1rem;"></div>
  <button class="primary" id="deploy-btn" disabled onclick="deploy()">Download &amp; Build</button>
</div>

<div class="card" id="progress-card" style="display:none;">
  <h2>Progress</h2>
  <div id="progress-text" class="progress">...</div>
</div>

<script>
let regions = [];
let pollTimer = null;
let setupMode = false;

async function loadRegions() {
  try {
    const resp = await fetch('/admin/api/regions');
    regions = await resp.json();
  } catch(e) {
    console.error('Failed to load regions:', e);
  }
}

async function loadStatus() {
  try {
    const resp = await fetch('/admin/api/status');
    const s = await resp.json();
    const box = document.getElementById('status-box');
    const prog = document.getElementById('progress-card');
    const btn = document.getElementById('deploy-btn');

    if (s.setup_mode && !setupMode) {
      setupMode = true;
      document.getElementById('page-title').textContent = 'GraphHopper Setup';
      document.getElementById('heading').textContent = 'Welcome to GraphHopper';
      document.getElementById('subtitle').textContent =
        'Choose a region to get started. The map data will be downloaded and a routing graph built. This usually takes 10-60 minutes depending on the region size.';
      document.getElementById('region-heading').textContent = 'Choose Your Region';
      btn.textContent = 'Get Started';
    }

    if (s.operation === 'downloading' || s.operation === 'building') {
      box.className = 'status busy';
      box.textContent = s.operation === 'downloading' ? 'Downloading...' : 'Building graph...';
      prog.style.display = 'block';
      document.getElementById('progress-text').textContent = s.progress || '...';
      btn.disabled = true;
      if (!pollTimer) pollTimer = setInterval(loadStatus, 3000);
    } else if (s.operation === 'error') {
      box.className = 'status error';
      box.textContent = 'Error: ' + s.error;
      prog.style.display = s.progress ? 'block' : 'none';
      document.getElementById('progress-text').textContent = s.progress;
      btn.disabled = false;
      if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    } else if (s.setup_mode && s.operation === 'no_graph') {
      box.className = 'status setup';
      box.textContent = 'No region configured yet. Select one below to get started.';
      btn.disabled = false;
    } else {
      const region = s.current_region || 'Unknown';
      box.className = 'status ok';
      box.textContent = 'Current region: ' + region;
      if (s.operation === 'ready') {
        prog.style.display = 'block';
        document.getElementById('progress-text').textContent = s.progress;
      } else {
        prog.style.display = 'none';
      }
      btn.disabled = false;
      if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    }
  } catch(e) {
    console.error('Status error:', e);
  }
}

function filterRegions(query) {
  const list = document.getElementById('region-list');
  const q = query.toLowerCase();
  if (!q) { list.style.display = 'none'; return; }
  const matches = regions.filter(r => r.display.toLowerCase().includes(q) || r.id.toLowerCase().includes(q));
  if (matches.length === 0) { list.style.display = 'none'; return; }
  list.innerHTML = matches.slice(0, 50).map(r =>
    '<div onclick="selectRegion(this)" data-url="' + r.pbf_url + '" data-name="' + r.display + '">' + r.display + '</div>'
  ).join('');
  list.style.display = 'block';
}

function selectRegion(el) {
  document.getElementById('selected-url').value = el.dataset.url;
  document.getElementById('selected-name').value = el.dataset.name;
  document.getElementById('selected-display').innerHTML = '<strong>Selected:</strong> ' + el.dataset.name;
  document.getElementById('search').value = el.dataset.name;
  document.getElementById('region-list').style.display = 'none';
  document.getElementById('deploy-btn').disabled = false;
}

async function deploy() {
  const url = document.getElementById('selected-url').value;
  const name = document.getElementById('selected-name').value;
  if (!url) { alert('Select a region first'); return; }
  const msg = setupMode
    ? 'Download ' + name + ' and build the routing graph?'
    : 'Download ' + name + ' and rebuild the routing graph? This will restart GraphHopper.';
  if (!confirm(msg)) return;

  document.getElementById('deploy-btn').disabled = true;
  try {
    const resp = await fetch('/admin/api/deploy', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({pbf_url: url, region_name: name})
    });
    const r = await resp.json();
    if (r.error) alert(r.error);
    pollTimer = setInterval(loadStatus, 3000);
    loadStatus();
  } catch(e) {
    alert('Deploy failed: ' + e);
    document.getElementById('deploy-btn').disabled = false;
  }
}

document.getElementById('search').addEventListener('input', e => filterRegions(e.target.value));
document.addEventListener('click', e => {
  if (!e.target.closest('.search-box')) document.getElementById('region-list').style.display = 'none';
});

loadRegions();
loadStatus();
</script>
</body>
</html>"""


class AdminHandler(http.server.BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass

    def _json_response(self, data, code=200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html_response(self, html, code=200):
        body = html.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]

        if path in ("/admin", "/admin/"):
            self._html_response(ADMIN_HTML)

        elif path == "/admin/api/regions":
            regions = _fetch_geofabrik_index()
            self._json_response(regions)

        elif path == "/admin/api/status":
            state = _get_state()
            state["current_region"] = _read_current_region()
            state["setup_mode"] = SETUP_MODE
            has_graph = os.path.isdir(GRAPH_DIR) and len(os.listdir(GRAPH_DIR)) > 0
            if state["operation"] is None:
                state["operation"] = "ok" if has_graph else "no_graph"
            self._json_response(state)

        else:
            self.send_error(404)

    def do_POST(self):
        path = self.path.split("?")[0]

        if path == "/admin/api/deploy":
            cl = self.headers.get("Content-Length", "0")
            try:
                body = json.loads(self.rfile.read(int(cl)))
            except (ValueError, json.JSONDecodeError):
                self._json_response({"error": "Invalid JSON"}, 400)
                return

            pbf_url = body.get("pbf_url", "").strip()
            region_name = body.get("region_name", "").strip()

            if not pbf_url:
                self._json_response({"error": "No PBF URL provided"}, 400)
                return

            # Check not already running
            state = _get_state()
            if state["operation"] in ("downloading", "building"):
                self._json_response({"error": "Operation already in progress"}, 409)
                return

            # Start background operation
            t = threading.Thread(
                target=_background_download_and_build,
                args=(pbf_url, region_name),
                daemon=True,
            )
            t.start()
            self._json_response({"ok": True, "status": "started"})

        else:
            self.send_error(404)


class ThreadedServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    server = ThreadedServer((LISTEN_HOST, LISTEN_PORT), AdminHandler)
    print(f"[admin] listening on {LISTEN_HOST}:{LISTEN_PORT}", file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
