#!/bin/bash
# Sync the baked-in provisioner script into the sites volume on container
# start. The sites volume is shared with the bench backend so paths in
# bench resolve correctly. Always overwrite to roll forward when a new
# image is rolled out — that's the whole point of having the image as
# source of truth.
set -euo pipefail

SITES_DIR="${SITES_DIR:-/home/frappe/frappe-bench/sites}"
SOURCE_DIR=/opt/erpsense

if [ ! -d "$SITES_DIR" ]; then
  echo "[entrypoint] sites volume not mounted at $SITES_DIR — refusing to start" >&2
  exit 1
fi

cp -f "$SOURCE_DIR/site_provisioner.py" "$SITES_DIR/site_provisioner.py"
cp -f "$SOURCE_DIR/multisite-nginx.conf.template" "$SITES_DIR/multisite-nginx.conf.template" || true

echo "[entrypoint] synced provisioner script to $SITES_DIR ($(md5sum "$SITES_DIR/site_provisioner.py" | cut -d' ' -f1))"
exec "$@"
