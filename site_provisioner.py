#!/usr/bin/env python3
"""
Frappe Site Provisioner Sidecar

Lightweight HTTP server (stdlib only — no pip installs) that runs inside
the ERPNext Docker backend container on port 9090. Wraps `bench new-site`
and `bench drop-site` behind a simple JSON API.

Endpoints:
  POST /create-site    { "site_name": "abc.localhost", "admin_password": "..." }
  POST /claim-site     { "target_name": "abc.localhost" }   ← instant (renames a pool site)
  POST /drop-site      { "site_name": "abc.localhost" }
  POST /reset-tenant   { "tenant_name": "abc.localhost" }   ← atomic drop + claim
  GET  /list-sites
  GET  /pool-status
  GET  /health

Pre-warm pool:
  On startup and after each claim, the sidecar creates warm sites in the
  background (bench new-site). When a tenant needs a site, /claim-site
  renames a warm site instantly (~1s) instead of waiting 3 min for bench.

Reset-tenant flow (for import abandon):
  /reset-tenant drops the tenant's current site and atomically claims a fresh
  one from the warm pool. Returns the new site details. If the pool is empty,
  the drop is still performed and a 503 is returned so the caller can show
  a blocking "Preparing fresh workspace…" UI while replenishment runs.

Environment variables:
  MARIADB_ROOT_PASSWORD   — MariaDB root password (default: "admin")
  MAX_SITES               — Maximum number of tenant sites allowed
                            (defaults: 3 for dev/staging, tune to 10 in prod)
  POOL_SIZE               — Number of warm sites to keep ready
                            (defaults: 2 for dev/staging, tune to 5 in prod)
  PROVISIONER_PORT        — Port to listen on (default: 9090)
  DEFAULT_ADMIN_PASSWORD  — Default admin password for new sites (default: "Admin@2026")
  POOL_LOW_WARN_SECONDS   — Seconds pool can stay at 0 before log WARN
                            (default: 60; 0 disables)
"""

import json
import logging
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("site-provisioner")

# ── Configuration ──
MARIADB_ROOT_PASSWORD = os.environ.get("MARIADB_ROOT_PASSWORD", "admin")
MAX_SITES = int(os.environ.get("MAX_SITES", "3"))
POOL_SIZE = int(os.environ.get("POOL_SIZE", "2"))
PORT = int(os.environ.get("PROVISIONER_PORT", "9090"))
DEFAULT_ADMIN_PASSWORD = os.environ.get("DEFAULT_ADMIN_PASSWORD", "Admin@2026")
POOL_LOW_WARN_SECONDS = int(os.environ.get("POOL_LOW_WARN_SECONDS", "60"))
SITES_DIR = Path("/home/frappe/frappe-bench/sites")

# Lock to prevent concurrent bench operations (bench is not concurrency-safe).
#
# PHASE3-Issue-3 — split into separate locks for new-site (slow, 3-5
# minutes for warm-pool replenishment) and drop-site (cheap, seconds).
# Holding one lock for both used to block fast `/drop-site` and
# `/reset-tenant` requests behind a background pool replenisher,
# returning 503 to QA seed-iteration scripts. The two operations
# don't conflict at the bench level when they target different sites
# (which is always the case here — drop targets an existing tenant
# site, new-site targets a fresh `_pool-N.localhost`).
#
# `_bench_lock` is kept as an alias for any callers we missed; new
# code should use the specific lock.
_bench_new_site_lock = threading.Lock()
_bench_drop_site_lock = threading.Lock()
_bench_lock = _bench_new_site_lock  # back-compat alias

# Timestamp when pool first observed as empty; reset to None when pool becomes
# non-empty. Used by the pool-health monitor to emit a single WARN per sustained
# outage rather than spamming logs every check.
_pool_empty_since: float | None = None
_pool_monitor_lock = threading.Lock()

# The default site created by docker-compose (should not be counted or deleted)
DEFAULT_SITE = "frontend"

# Pool site naming: _pool-0.localhost, _pool-1.localhost, etc.
# Prefix with _ so they sort before tenant sites and are easy to identify.
POOL_PREFIX = "_pool-"
POOL_DOMAIN = "localhost"


def _is_real_site(name: str) -> bool:
    """Check if a directory is a real Frappe site."""
    return (SITES_DIR / name / "site_config.json").exists()


def _is_pool_site(name: str) -> bool:
    """Check if a site name is a pool site."""
    return name.startswith(POOL_PREFIX)


def _get_all_sites() -> list[str]:
    """List all Frappe sites excluding the default site and special files."""
    if not SITES_DIR.exists():
        return []
    exclude = {
        DEFAULT_SITE,
        "common_site_config.json",
        "apps.txt",
        "apps.json",
        "assets",
        ".DS_Store",
        "logs",
        "site_provisioner.py",
    }
    return sorted(
        entry.name
        for entry in SITES_DIR.iterdir()
        if entry.is_dir()
        and entry.name not in exclude
        and not entry.name.startswith(".")
        and _is_real_site(entry.name)
    )


def _get_tenant_sites() -> list[str]:
    """List tenant sites (excludes pool sites and default site)."""
    return [s for s in _get_all_sites() if not _is_pool_site(s)]


def _get_pool_sites() -> list[str]:
    """List available warm pool sites."""
    return [s for s in _get_all_sites() if _is_pool_site(s)]


def _next_pool_name() -> str:
    """Generate the next pool site name."""
    existing = _get_pool_sites()
    for i in range(100):
        name = f"{POOL_PREFIX}{i}.{POOL_DOMAIN}"
        if name not in existing:
            return name
    return f"{POOL_PREFIX}99.{POOL_DOMAIN}"


def _fix_db_password(site_name: str) -> None:
    """Ensure the MariaDB user password matches site_config.json.

    After bench new-site + container restarts, the MariaDB user's password
    can get out of sync with what's in site_config.json. This resets it.
    """
    config_path = SITES_DIR / site_name / "site_config.json"
    if not config_path.exists():
        return
    try:
        with open(config_path) as f:
            config = json.load(f)
        db_name = config.get("db_name", "")
        db_password = config.get("db_password", "")
        if not db_name or not db_password:
            return
        # Read DB host from common_site_config.json
        common_config_path = SITES_DIR / "common_site_config.json"
        db_host = "db"
        if common_config_path.exists():
            with open(common_config_path) as cf:
                db_host = json.load(cf).get("db_host", "db")

        result = subprocess.run(
            [
                "mariadb",
                f"-h{db_host}",
                f"-uroot",
                f"-p{MARIADB_ROOT_PASSWORD}",
                "-e",
                f"ALTER USER '{db_name}'@'%' IDENTIFIED BY '{db_password}'; FLUSH PRIVILEGES;",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            logger.info("Fixed DB password for site %s (db=%s)", site_name, db_name)
        else:
            logger.warning(
                "Failed to fix DB password for %s: %s", site_name, result.stderr[:200]
            )
    except Exception as e:
        logger.warning("Error fixing DB password for %s: %s", site_name, e)


def _rename_site(old_name: str, new_name: str) -> tuple[bool, str]:
    """Rename a Frappe site (directory + fix DB password)."""
    old_dir = SITES_DIR / old_name
    new_dir = SITES_DIR / new_name
    if not old_dir.exists():
        return False, f"Source site {old_name} does not exist"
    if new_dir.exists():
        return False, f"Target site {new_name} already exists"

    # Rename the directory
    try:
        old_dir.rename(new_dir)
    except OSError as e:
        return False, f"Failed to rename directory: {e}"

    # Fix MariaDB password to match site_config.json
    _fix_db_password(new_name)

    logger.info("Renamed site directory: %s → %s", old_name, new_name)
    return True, f"Site renamed to {new_name}"


def _replenish_pool_sync() -> None:
    """Create warm sites to fill the pool up to POOL_SIZE. Blocking."""
    current_pool = _get_pool_sites()
    needed = POOL_SIZE - len(current_pool)
    if needed <= 0:
        logger.info("Pool is full (%d/%d)", len(current_pool), POOL_SIZE)
        return

    logger.info(
        "Replenishing pool: %d sites needed (current: %d/%d)",
        needed,
        len(current_pool),
        POOL_SIZE,
    )

    for _ in range(needed):
        # Check max total sites limit
        all_sites = _get_all_sites()
        if len(all_sites) >= MAX_SITES + POOL_SIZE:
            logger.warning("Total site limit reached, cannot create more pool sites")
            break

        name = _next_pool_name()
        # PHASE3-Issue-3 — use the new-site-specific lock so this
        # 3-5 minute operation doesn't block fast /drop-site requests.
        if not _bench_new_site_lock.acquire(timeout=5):
            logger.warning("bench new-site lock busy, skipping pool replenish")
            break

        try:
            import uuid as _uuid

            unique_db = f"_p{_uuid.uuid4().hex[:16]}"
            success, output = _run_bench(
                [
                    "new-site",
                    name,
                    "--mariadb-user-host-login-scope=%",
                    "--admin-password",
                    DEFAULT_ADMIN_PASSWORD,
                    "--db-root-username",
                    "root",
                    "--db-root-password",
                    MARIADB_ROOT_PASSWORD,
                    "--db-name",
                    unique_db,
                    "--install-app",
                    "erpnext",
                ],
                timeout=300,
            )
            if success:
                _fix_db_password(name)  # Ensure DB user password matches config
                logger.info("Pool site created: %s", name)
            else:
                logger.error("Failed to create pool site %s: %s", name, output[-200:])
        finally:
            _bench_new_site_lock.release()


def _replenish_pool_background() -> None:
    """Start pool replenishment in a background thread."""
    thread = threading.Thread(target=_replenish_pool_sync, daemon=True)
    thread.start()


def _claimed_site_is_healthy(site_name: str) -> tuple[bool, str]:
    """PHASE3-Issue-4 — post-claim health check.

    Warm-pool occasionally ships half-baked sites (e.g. interrupted
    `bench new-site` left the DB partially seeded; first login then dies
    with `DocType System Settings not found`). Run a cheap
    `bench --site <S> list-apps` after the rename so the API caller can
    distinguish "site claimed cleanly" from "site claimed but corrupt"
    and retry without manual intervention. Returns (healthy, message).
    """
    success, output = _run_bench(["--site", site_name, "list-apps"], timeout=30)
    if not success:
        return False, f"list-apps failed: {output[-200:]}"
    # bench list-apps prints app names one per line; a healthy site has
    # at least `frappe` and (for pool sites) `erpnext`.
    out_lower = output.lower()
    if "frappe" not in out_lower:
        return False, f"list-apps output missing frappe: {output[-200:]}"
    return True, "site healthy"


def _drop_site_now(site_name: str) -> tuple[bool, str]:
    """Drop a site via `bench drop-site`. Returns (success, message).

    Extracted from _handle_drop_site so /reset-tenant can reuse the same path
    without duplicating validation, lock handling, and post-drop checks.
    """
    if site_name == DEFAULT_SITE:
        return False, f"Cannot drop the default site '{DEFAULT_SITE}'"
    if ".." in site_name or "/" in site_name or "\\" in site_name:
        return False, f"Invalid site_name: {site_name}"

    site_dir = SITES_DIR / site_name
    if not site_dir.exists():
        # Already dropped — idempotent success
        return True, f"Site {site_name} does not exist (already dropped)"

    # PHASE3-Issue-3 — drop-site uses its own lock so it doesn't queue
    # behind a 3-5 minute pool replenisher (`bench new-site`). Bench is
    # not concurrency-safe for the SAME operation, but new-site and
    # drop-site target different sites and don't conflict at the bench
    # level — so giving each its own lock is safe and unblocks
    # /drop-site + /reset-tenant during pool replenishment.
    if not _bench_drop_site_lock.acquire(timeout=5):
        return False, "Another drop-site operation is in progress"

    try:
        success, output = _run_bench(
            [
                "drop-site",
                site_name,
                "--force",
                "--db-root-username",
                "root",
                "--db-root-password",
                MARIADB_ROOT_PASSWORD,
            ],
            timeout=120,
        )
        if success:
            return True, f"Site {site_name} dropped"
        # Site may be gone despite non-zero exit
        if not site_dir.exists():
            return True, f"Site {site_name} dropped (with warnings)"
        return False, f"bench drop-site failed: {output[-300:]}"
    finally:
        _bench_drop_site_lock.release()


def _check_pool_health() -> None:
    """Log a WARN once when pool has been empty for POOL_LOW_WARN_SECONDS.

    Called opportunistically from read endpoints (/health, /pool-status) and
    after each claim. Uses a module-level timestamp so the warning fires once
    per sustained outage rather than on every check.
    """
    global _pool_empty_since
    if POOL_LOW_WARN_SECONDS <= 0:
        return

    pool = _get_pool_sites()
    now = __import__("time").time()
    with _pool_monitor_lock:
        if pool:
            _pool_empty_since = None
            return
        if _pool_empty_since is None:
            _pool_empty_since = now
            return
        elapsed = now - _pool_empty_since
        if elapsed >= POOL_LOW_WARN_SECONDS:
            logger.warning(
                "Warm pool empty for %.0fs (threshold=%ds). "
                "Next claim will block until replenishment completes.",
                elapsed,
                POOL_LOW_WARN_SECONDS,
            )
            # Reset so the next warning fires after another full threshold,
            # not every poll.
            _pool_empty_since = now


def _run_bench(args: list[str], timeout: int = 300) -> tuple[bool, str]:
    """Run a bench command and return (success, output)."""
    cmd = ["bench"] + args
    logger.info("Running: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd="/home/frappe/frappe-bench",
        )
        output = result.stdout + result.stderr
        if result.returncode == 0:
            logger.info("Command succeeded: %s", " ".join(cmd[:3]))
            return True, output
        logger.error("Command failed (rc=%d): %s", result.returncode, output[-500:])
        return False, output[-500:]
    except subprocess.TimeoutExpired:
        return False, f"Command timed out after {timeout}s"
    except Exception as exc:
        return False, str(exc)


class ProvisionerHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the site provisioner."""

    def _send_json(self, status_code: int, data: dict) -> None:
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        body = self.rfile.read(length)
        return json.loads(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            pool = _get_pool_sites()
            tenants = _get_tenant_sites()
            _check_pool_health()
            self._send_json(
                200,
                {
                    "success": True,
                    "status": "healthy",
                    "max_sites": MAX_SITES,
                    "current_sites": len(tenants),
                    "pool_size": len(pool),
                    "pool_target": POOL_SIZE,
                },
            )
        elif self.path == "/list-sites":
            sites = _get_tenant_sites()
            self._send_json(
                200,
                {
                    "success": True,
                    "sites": sites,
                    "count": len(sites),
                    "max_sites": MAX_SITES,
                    "remaining": max(0, MAX_SITES - len(sites)),
                },
            )
        elif self.path == "/pool-status":
            pool = _get_pool_sites()
            _check_pool_health()
            self._send_json(
                200,
                {
                    "success": True,
                    "pool_sites": pool,
                    "available": len(pool),
                    "target": POOL_SIZE,
                    "ready": len(pool) > 0,
                },
            )
        else:
            self._send_json(404, {"success": False, "error": "Not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/create-site":
            self._handle_create_site()
        elif self.path == "/claim-site":
            self._handle_claim_site()
        elif self.path == "/drop-site":
            self._handle_drop_site()
        elif self.path == "/reset-tenant":
            self._handle_reset_tenant()
        else:
            self._send_json(404, {"success": False, "error": "Not found"})

    def _handle_create_site(self) -> None:
        body = self._read_body()
        site_name = body.get("site_name", "").strip()
        admin_password = body.get("admin_password", DEFAULT_ADMIN_PASSWORD)

        if not site_name:
            self._send_json(400, {"success": False, "error": "site_name is required"})
            return

        # Safety: reject names that look dangerous
        if ".." in site_name or "/" in site_name or "\\" in site_name:
            self._send_json(400, {"success": False, "error": "Invalid site_name"})
            return

        # Check if site already exists
        site_dir = SITES_DIR / site_name
        if site_dir.exists() and (site_dir / "site_config.json").exists():
            self._send_json(
                200,
                {
                    "success": True,
                    "already_exists": True,
                    "site_name": site_name,
                    "message": "Site already exists",
                },
            )
            return

        # Enforce max sites limit
        current_sites = _get_tenant_sites()
        if len(current_sites) >= MAX_SITES:
            self._send_json(
                429,
                {
                    "success": False,
                    "error": (
                        f"Maximum site limit reached ({MAX_SITES}). "
                        f"Delete an existing tenant before creating a new one. "
                        f"Current sites: {', '.join(current_sites)}"
                    ),
                    "current_sites": len(current_sites),
                    "max_sites": MAX_SITES,
                },
            )
            return

        # Acquire lock — bench is not concurrency-safe
        if not _bench_lock.acquire(timeout=5):
            self._send_json(
                503,
                {
                    "success": False,
                    "error": "Another site operation is in progress. Try again in a moment.",
                },
            )
            return

        try:
            success, output = _run_bench(
                [
                    "new-site",
                    site_name,
                    "--mariadb-user-host-login-scope=%",
                    "--admin-password",
                    admin_password,
                    "--db-root-username",
                    "root",
                    "--db-root-password",
                    MARIADB_ROOT_PASSWORD,
                    "--install-app",
                    "erpnext",
                ],
                timeout=300,
            )

            if success:
                self._send_json(
                    201,
                    {
                        "success": True,
                        "site_name": site_name,
                        "message": "Site created successfully",
                    },
                )
            else:
                # Check if it actually got created despite error output
                if (site_dir / "site_config.json").exists():
                    self._send_json(
                        201,
                        {
                            "success": True,
                            "site_name": site_name,
                            "message": "Site created (with warnings)",
                            "warnings": output[-200:],
                        },
                    )
                else:
                    self._send_json(
                        500,
                        {
                            "success": False,
                            "error": f"bench new-site failed: {output[-300:]}",
                        },
                    )
        finally:
            _bench_lock.release()

    def _handle_claim_site(self) -> None:
        """Claim a warm pool site by renaming it to the target name. Instant (~1s)."""
        body = self._read_body()
        target_name = body.get("target_name", "").strip()

        if not target_name:
            self._send_json(400, {"success": False, "error": "target_name is required"})
            return

        if ".." in target_name or "/" in target_name or "\\" in target_name:
            self._send_json(400, {"success": False, "error": "Invalid target_name"})
            return

        # Check if target already exists
        if (SITES_DIR / target_name).exists():
            self._send_json(
                200,
                {
                    "success": True,
                    "already_exists": True,
                    "site_name": target_name,
                },
            )
            return

        # Check tenant limit
        tenants = _get_tenant_sites()
        if len(tenants) >= MAX_SITES:
            self._send_json(
                429,
                {
                    "success": False,
                    "error": f"Maximum tenant limit reached ({MAX_SITES}). Delete a tenant first.",
                },
            )
            return

        # Find an available pool site
        pool = _get_pool_sites()
        if not pool:
            # No warm sites — fall back to create-site
            self._send_json(
                503,
                {
                    "success": False,
                    "error": "No warm sites available in pool. Use /create-site as fallback.",
                    "pool_empty": True,
                },
            )
            return

        # Claim the first available pool site
        pool_site = pool[0]
        success, msg = _rename_site(pool_site, target_name)
        if not success:
            self._send_json(500, {"success": False, "error": msg})
            return

        # PHASE3-Issue-4 — post-claim health check. The warm pool
        # occasionally ships half-baked sites (e.g. an interrupted
        # `bench new-site` left the DB partially seeded). Verify the
        # claimed site responds to a basic bench query before returning
        # success — if it doesn't, drop it so the operator's next claim
        # picks a different pool member, and surface a 503 so the
        # caller can retry.
        healthy, health_msg = _claimed_site_is_healthy(target_name)
        if not healthy:
            logger.warning(
                "Claimed pool site %s failed health check, dropping: %s",
                target_name,
                health_msg,
            )
            _drop_site_now(target_name)
            _replenish_pool_background()
            self._send_json(
                503,
                {
                    "success": False,
                    "error": (
                        f"Claimed site failed post-claim health check: {health_msg}. "
                        "Bad site dropped. Retry to claim a different pool member."
                    ),
                    "claimed_from": pool_site,
                    "retryable": True,
                },
            )
            return

        logger.info("Claimed pool site: %s → %s", pool_site, target_name)
        # Trigger background replenishment
        _replenish_pool_background()
        self._send_json(
            200,
            {
                "success": True,
                "site_name": target_name,
                "claimed_from": pool_site,
                "message": "Site claimed from warm pool (instant)",
            },
        )

    def _handle_drop_site(self) -> None:
        body = self._read_body()
        site_name = body.get("site_name", "").strip()

        if not site_name:
            self._send_json(400, {"success": False, "error": "site_name is required"})
            return

        # Safety gates are centralized in _drop_site_now so /reset-tenant
        # enforces the same rules.
        success, msg = _drop_site_now(site_name)
        if not success:
            # Rough status mapping; _drop_site_now emits messages we classify here
            if msg.startswith("Cannot drop") or msg.startswith("Invalid"):
                status = 400 if msg.startswith("Invalid") else 403
            elif "Another site operation" in msg:
                status = 503
            else:
                status = 500
            self._send_json(status, {"success": False, "error": msg})
            return

        self._send_json(
            200,
            {
                "success": True,
                "site_name": site_name,
                "message": msg,
                "already_dropped": "does not exist" in msg,
            },
        )

    def _handle_reset_tenant(self) -> None:
        """Atomic drop-then-claim for import-abandon flow.

        Always attempts the drop (safe + idempotent). If the warm pool has a
        site, performs an instant claim of a fresh one. If the pool is empty
        after the drop, returns 503 with ``pool_empty=True`` so the caller can
        show a blocking "Preparing fresh workspace…" UI while replenishment
        runs in the background.
        """
        body = self._read_body()
        tenant_name = body.get("tenant_name", "").strip()
        if not tenant_name:
            # Accept `site_name` alias for symmetry with /drop-site
            tenant_name = body.get("site_name", "").strip()
        if not tenant_name:
            self._send_json(
                400,
                {
                    "success": False,
                    "error": "tenant_name (or site_name) is required",
                },
            )
            return

        if ".." in tenant_name or "/" in tenant_name or "\\" in tenant_name:
            self._send_json(400, {"success": False, "error": "Invalid tenant_name"})
            return
        if tenant_name == DEFAULT_SITE:
            self._send_json(
                403,
                {
                    "success": False,
                    "error": f"Cannot reset the default site '{DEFAULT_SITE}'",
                },
            )
            return

        # Step 1: drop the dirty site (idempotent if already gone)
        drop_ok, drop_msg = _drop_site_now(tenant_name)
        if not drop_ok:
            status = 503 if "Another site operation" in drop_msg else 500
            self._send_json(
                status,
                {
                    "success": False,
                    "phase": "drop",
                    "error": drop_msg,
                },
            )
            return

        # Step 2: claim a fresh pool site under the same name
        pool = _get_pool_sites()
        if not pool:
            # Kick off replenishment so the caller's next attempt succeeds,
            # then return 503 so the frontend shows the blocking UI.
            _replenish_pool_background()
            _check_pool_health()
            self._send_json(
                503,
                {
                    "success": False,
                    "phase": "claim",
                    "pool_empty": True,
                    "drop_message": drop_msg,
                    "error": (
                        "Warm pool is empty after drop. Replenishment triggered; "
                        "retry /claim-site or /reset-tenant in ~3 min."
                    ),
                },
            )
            return

        pool_site = pool[0]
        claim_ok, claim_msg = _rename_site(pool_site, tenant_name)
        if not claim_ok:
            self._send_json(
                500,
                {
                    "success": False,
                    "phase": "claim",
                    "drop_message": drop_msg,
                    "error": claim_msg,
                },
            )
            return

        logger.info(
            "Reset tenant: dropped + claimed %s (from %s)", tenant_name, pool_site
        )
        _replenish_pool_background()
        _check_pool_health()
        self._send_json(
            200,
            {
                "success": True,
                "site_name": tenant_name,
                "claimed_from": pool_site,
                "message": "Tenant site reset — fresh workspace claimed from pool",
            },
        )

    def log_message(self, format: str, *args: object) -> None:
        """Override to use our logger instead of stderr."""
        logger.info("%s %s", self.client_address[0], format % args)


def main() -> None:
    server = HTTPServer(("0.0.0.0", PORT), ProvisionerHandler)
    pool = _get_pool_sites()
    logger.info(
        "Site provisioner started on port %d (max_sites=%d, pool=%d/%d)",
        PORT,
        MAX_SITES,
        len(pool),
        POOL_SIZE,
    )

    # Fix DB passwords for all existing sites on startup
    all_sites = _get_all_sites()
    for site in all_sites:
        _fix_db_password(site)
    for p in pool:
        _fix_db_password(p)
    logger.info("Fixed DB passwords for %d sites", len(all_sites) + len(pool))

    # Start pool replenishment in background on startup
    if len(pool) < POOL_SIZE:
        logger.info("Warming up site pool in background...")
        _replenish_pool_background()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
