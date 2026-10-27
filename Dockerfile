# GraphHopper for OpenHost — self-hosted routing and map navigation.
#
# Single container running:
#   - GraphHopper          (127.0.0.1:8989, loopback)
#   - Auth proxy           (0.0.0.0:8080, externally routed)
#
# No app-level SSO — OpenHost zone_auth gates all access.
# On first boot, downloads an OSM PBF extract and builds routing graph.

# Pinned to an exact release and its multi-arch index digest. Bump both
# together, deliberately, after CI passes. (:latest is a nightly build of
# GraphHopper master, so it is not used.)
FROM docker.io/israelhikingmap/graphhopper:12.0-pre3@sha256:e5bfc3e2b7f5435ef7963e89a3a47cf8e6cf4b880b0e58514fb3c88572470ab3

USER root

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
        tini \
        curl \
        wget \
    && rm -rf /var/lib/apt/lists/*

COPY start.sh /opt/openhost/start.sh
COPY auth_proxy.py /opt/openhost/auth_proxy.py
COPY admin.py /opt/openhost/admin.py
RUN chmod 0755 /opt/openhost/start.sh /opt/openhost/auth_proxy.py /opt/openhost/admin.py

EXPOSE 8080

ENTRYPOINT ["tini", "--", "/opt/openhost/start.sh"]
