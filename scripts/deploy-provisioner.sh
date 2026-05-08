#!/usr/bin/env bash
# Deploy a published multisite-provisioner image to a target VM.
#
# Run this AFTER the GitHub Actions "Build & publish multisite-provisioner
# image" workflow has tagged a build for the target environment. This
# script does the rollout: edit docker-compose.yml on the VM, pull the
# new image, recreate the container.
#
# Usage:
#   ./scripts/deploy-provisioner.sh <env> <image-tag>
#
# Example:
#   ./scripts/deploy-provisioner.sh dev sha-cd2bcef
#   ./scripts/deploy-provisioner.sh dev v1.2.0
#
# Requires:
#   - gcloud CLI authenticated
#   - SSH access to <env> VM via IAP
#   - The image already exists in Artifact Registry under
#     <region>-docker.pkg.dev/erpsense-<env>/docker/frappe-multisite-provisioner:<tag>
#
# Idempotency: replays the same tag are safe. The script only restarts
# the site-provisioner container; other Frappe containers are not touched.

set -euo pipefail

ENV="${1:-}"
TAG="${2:-}"

if [[ -z "$ENV" || -z "$TAG" ]]; then
  echo "Usage: $0 <env> <image-tag>" >&2
  echo "  env: dev | stage | prod" >&2
  echo "  image-tag: sha-<short> | main | v<version> | latest" >&2
  exit 1
fi

case "$ENV" in
dev)
  PROJECT=erpsense-dev
  ZONE=asia-south1-c
  VM=erpnext-vm-dev
  ;;
stage)
  PROJECT=erpsense-stage
  ZONE=asia-south1-c
  VM=erpnext-vm-stage
  ;;
prod)
  PROJECT=erpsense-prod
  ZONE=asia-south1-c
  VM=erpnext-vm-prod
  ;;
*)
  echo "Unknown env: $ENV" >&2
  exit 1
  ;;
esac

REGION=asia-south1
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/docker/frappe-multisite-provisioner:${TAG}"
COMPOSE_DIR=/mnt/erpnext-data/frappe_docker

echo "→ Verifying image exists: $IMAGE"
gcloud artifacts docker images describe "$IMAGE" --project="$PROJECT" >/dev/null

echo "→ Rolling out to $VM ($PROJECT)"
gcloud compute ssh "$VM" --tunnel-through-iap --project="$PROJECT" --zone="$ZONE" \
  --command="
    set -euo pipefail
    cd $COMPOSE_DIR
    sudo docker pull '$IMAGE'
    # Update the image: line of the site-provisioner service in
    # docker-compose.yml (idempotent — replays produce same line)
    sudo python3 -c \"
import re, pathlib, sys
p = pathlib.Path('$COMPOSE_DIR/docker-compose.yml')
src = p.read_text()
# Match the site-provisioner service block's image: line and replace
new = re.sub(
    r'(site-provisioner:\\n(?:\\s.*\\n)*?\\s+image:\\s+)\\S+',
    r'\\1$IMAGE',
    src, count=1,
)
if new == src:
    sys.stderr.write('site-provisioner image: line not found — refusing to deploy.\\n')
    sys.exit(1)
p.write_text(new)
print('docker-compose.yml updated.')
\"
    sudo docker compose -f $COMPOSE_DIR/docker-compose.yml up -d --no-deps site-provisioner
    sleep 5
    sudo docker ps --filter name=site-provisioner --format 'table {{.Names}}\\t{{.Status}}\\t{{.Image}}'
    echo '→ Provisioner /list-sites health check:'
    curl -fsS http://127.0.0.1:9090/list-sites | head -c 200
    echo
  "

echo "✓ Rollout complete."
