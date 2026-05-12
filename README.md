# openhost-graphhopper

GraphHopper routing engine packaged for OpenHost. Provides turn-by-turn car, bike, and foot navigation with a built-in web UI.

## Architecture

Single container running:
- GraphHopper (loopback, Java)
- Auth proxy (port 8080, externally routed)

## First boot

On first boot, downloads an OSM PBF extract (default: Germany, ~4GB) and builds the routing graph. This takes 10-60 minutes depending on region size. Subsequent boots use the cached graph and start in seconds.

## Changing region

Edit `$OPENHOST_APP_DATA_DIR/region.conf` to set a different PBF_URL, then delete the `graph-cache/` directory and restart. Regional extracts available at https://download.geofabrik.de/

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
