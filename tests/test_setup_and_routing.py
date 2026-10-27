import time
from collections.abc import Callable

import requests
from openhost_test_harness import OpenhostStack

# The smallest Geofabrik extract (~0.6 MB), so download and graph import take seconds on CI.
REGION = "Monaco"
PBF_URL = "https://download.geofabrik.de/europe/monaco-latest.osm.pbf"


def _logs(stack: OpenhostStack) -> str:
    return f"\n\napp logs:\n{stack.app_logs()[-5000:]}"


def _poll(stack: OpenhostStack, what: str, check: Callable[[], object], timeout: float) -> object:
    """Call ``check`` until it returns something truthy, tolerating the app restarting between modes."""
    deadline = time.monotonic() + timeout
    last: object = None
    while time.monotonic() < deadline:
        try:
            last = check()
            if last:
                return last
        except (requests.RequestException, ValueError, KeyError) as e:
            last = repr(e)
        time.sleep(3)
    raise AssertionError(f"timed out waiting for {what}; last: {last}{_logs(stack)}")


def test_admin_is_owner_only_in_the_proxy(stack: OpenhostStack) -> None:
    # Straight to the container, without the router's owner header: the proxy itself refuses the admin UI.
    response = requests.get(f"{stack.app_url}/admin", timeout=30)
    assert response.status_code == 403, f"admin without owner header: {response.status_code}: {response.text[:300]}"


def test_pick_region_then_route(stack: OpenhostStack) -> None:
    owner = stack.owner_session

    # First boot is setup mode: the app sends the owner to the region picker.
    root = owner.get(f"{stack.url}/", headers={"Accept": "text/html"}, timeout=30)
    assert root.status_code == 200 and root.url.rstrip("/").endswith("/admin"), f"/ went to {root.url}"
    regions = owner.get(f"{stack.url}/admin/api/regions", timeout=60).json()
    monaco = [r for r in regions if r["name"] == REGION]
    assert monaco and monaco[0]["pbf_url"] == PBF_URL, f"{REGION} not in the region list: {regions[:5]}"

    deploy = owner.post(f"{stack.url}/admin/api/deploy", json={"pbf_url": PBF_URL, "region_name": REGION}, timeout=30)
    assert deploy.status_code == 200, f"deploy: {deploy.status_code}: {deploy.text[:300]}"

    def normal_mode() -> bool:
        status = owner.get(f"{stack.url}/admin/api/status", timeout=30).json()
        assert status.get("operation") != "error", f"region setup failed: {status}"
        return status["setup_mode"] is False and status["operation"] == "ok"

    _poll(stack, "the graph build and switch to normal mode", normal_mode, timeout=600)
    assert (stack.data_dir / "graph-cache").is_dir(), "graph not stored under app_data"
    assert PBF_URL in (stack.data_dir / "region.conf").read_text()

    def profile() -> str:
        info = owner.get(f"{stack.url}/info", timeout=30)
        return str(info.json()["profiles"][0]["name"]) if info.status_code == 200 else ""

    name = _poll(stack, "GraphHopper to serve /info", profile, timeout=300)
    route = owner.get(
        f"{stack.url}/route",
        params={"point": ["43.7384,7.4246", "43.7311,7.4197"], "profile": name, "points_encoded": "false"},
        timeout=60,
    )
    assert route.status_code == 200, f"route: {route.status_code}: {route.text[:500]}{_logs(stack)}"
    assert route.json()["paths"][0]["distance"] > 0, route.text[:500]

    # The maps UI is served with our geocoding shim and config.
    maps = owner.get(f"{stack.url}/maps/", headers={"Accept": "text/html"}, timeout=30)
    assert maps.status_code == 200 and "/geocode?" in maps.text, (
        f"maps UI without the geocoding shim: {maps.text[:500]}"
    )
    config = owner.get(f"{stack.url}/maps/config.js", timeout=30)
    assert "geocodingApi: location.origin" in config.text, config.text[:500]
