# openhost-graphhopper

GraphHopper routing engine packaged for OpenHost. Provides turn-by-turn car, bike, and foot navigation with a built-in web UI.

## Architecture

Single container running:
- GraphHopper (loopback, Java) -- only after initial setup
- Admin UI (loopback, Python) -- region management
- Auth proxy (port 8080, externally routed)

## First boot

On first boot, the app starts in setup mode and presents an onboarding
screen where you choose your region. Once selected, the OSM PBF extract
is downloaded and the routing graph is built (10-60 minutes depending on
region size). The container then restarts into normal mode.

Subsequent boots use the cached graph and start in seconds.

## Changing region

Visit `/admin` to select a different region. The admin UI is available
to the zone owner at any time. Selecting a new region triggers a fresh
download and rebuild, then the container restarts.

You can also edit `$OPENHOST_APP_DATA_DIR/region.conf` manually, delete
the `graph-cache/` directory, and restart.

Regional extracts available at https://download.geofabrik.de/

## Resource requirements

| Region | PBF Size | Import RAM | Serving RAM | Graph cache |
|--------|----------|-----------|-------------|-------------|
| Germany | ~4 GB | 8 GB | 4 GB | ~3 GB |
| France | ~4 GB | 8 GB | 4 GB | ~3 GB |
| Europe | ~32 GB | 32 GB | 16 GB | ~20 GB |

## Data

- `$OPENHOST_APP_DATA_DIR/region.osm.pbf` -- downloaded OSM data
- `$OPENHOST_APP_DATA_DIR/graph-cache/` -- precomputed routing graph
- `$OPENHOST_APP_DATA_DIR/region.conf` -- region configuration
- `$OPENHOST_APP_DATA_DIR/.setup_complete` -- sentinel indicating initial setup is done
