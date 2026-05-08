# Multi-site provisioner image

ERPSense's HTTP sidecar that wraps `bench new-site` / `bench drop-site` /
`bench claim-site` for tenant onboarding. Listens on `:9090`, called by
`erpsense-backend`'s `ERPNextProvisioningService`.

## Why a custom image (not volume-mount)

The original `pwd.yml` from upstream `frappe_docker` expects
`site_provisioner.py` to be volume-mounted from the host. That works for
local dev but is fragile in deployed environments:

- A `docker volume rm` (during VM reset, disk pressure cleanup, or pool
  refresh) wipes the script. Container starts but the script is gone.
- Hot-patching the script on the VM is invisible to git history.
- Rolling forward to a new provisioner version requires a manual SCP
  to every environment's VM.

This image bakes the script into the image so a `docker pull` is the
only operation needed to roll forward, and downgrades work via image
tag pinning.

## Build & publish

Automated by `.github/workflows/build-publish-provisioner.yml`:

| Trigger | Tags published |
|---|---|
| Push to `main` | `:main`, `:sha-<short>` |
| Tag `v*` | `:<version>`, `:latest`, `:sha-<short>` |
| `workflow_dispatch` | `:manual-<short>`, `:sha-<short>` |

Images are pushed to Artifact Registry per environment:
`asia-south1-docker.pkg.dev/erpsense-<env>/docker/frappe-multisite-provisioner:<tag>`

CI authenticates via Workload Identity Federation (no static keys). The
following GitHub repository secrets must be set per environment:

- `WIF_PROVIDER_<env>` — full provider resource name
- `CI_SERVICE_ACCOUNT_<env>` — service account email with
  `roles/artifactregistry.writer` on the target AR repository

## Roll out to a VM

CI publishes the image but does **not** auto-deploy to environments —
rollouts are explicit operator actions per environment.

```bash
./scripts/deploy-provisioner.sh dev sha-cd2bcef
```

The script:
1. Verifies the image exists in Artifact Registry
2. SSHs into the env's VM via IAP
3. Pulls the new image
4. Updates the `image:` line of the `site-provisioner` service in
   `/mnt/erpnext-data/frappe_docker/docker-compose.yml` (idempotent)
5. `docker compose up -d --no-deps site-provisioner` to recreate the
   container
6. Health-checks `GET /list-sites`

## Container behaviour

`entrypoint.sh` copies the baked-in script into the `sites` volume
on every container start. This means:

- Other Frappe containers (backend, workers) that share the `sites`
  volume see the same `site_provisioner.py` for tooling consistency.
- A volume-mount override in compose still wins over the baked-in
  copy if a developer needs to test a local edit (just mount
  `./site_provisioner.py:/home/frappe/frappe-bench/sites/site_provisioner.py`).

## Operational runbook

### Health check

```bash
curl -fsS http://<vm>:9090/list-sites
# Expected: {"success":true,"sites":[...],"count":N,...}
```

### Inspect logs

```bash
sudo docker logs frappe_docker-site-provisioner-1 --tail 100
```

On startup you should see `Fixed DB password for site ...` lines for any
site whose MariaDB user password was out of sync — that's the fix in
commit `ee0a897` doing its job. If the count is non-zero on every
restart there's something else rotating passwords behind our back.

### Rollback

```bash
./scripts/deploy-provisioner.sh dev <previous-sha>
```

The previous image is still in AR for at least 30 days per default
Artifact Registry retention, longer if you've configured retention
policies.
