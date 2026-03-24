#!/usr/bin/env python3
"""
Frappe Site Provisioner Sidecar

Lightweight HTTP server (stdlib only — no pip installs) that runs inside
the ERPNext Docker backend container on port 9090. Wraps `bench new-site`
and `bench drop-site` behind a simple JSON API.

Endpoints:
  POST /create-site   { "site_name": "abc.localhost", "admin_password": "..." }
  POST /claim-site    { "target_name": "abc.localhost" }   ← instant (renames a pool site)
  POST /drop-site     { "site_name": "abc.localhost" }
  GET  /list-sites
  GET  /pool-status
  GET  /health

Pre-warm pool:
  On startup and after each claim, the sidecar creates warm sites in the
  background (bench new-site). When a tenant needs a site, /claim-site
  renames a warm site instantly (~1s) instead of waiting 3 min for bench.

Environment variables:
  MARIADB_ROOT_PASSWORD   — MariaDB root password (default: "admin")
  MAX_SITES               — Maximum number of tenant sites allowed (default: 3)
  POOL_SIZE               — Number of warm sites to keep ready (default: 2)
  PROVISIONER_PORT        — Port to listen on (default: 9090)
  DEFAULT_ADMIN_PASSWORD  — Default admin password for new sites (default: "Admin@2026")
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
SITES_DIR = Path("/home/frappe/frappe-bench/sites")

# Lock to prevent concurrent bench operations (bench is not concurrency-safe)
_bench_lock = threading.Lock()

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
    exclude = {DEFAULT_SITE, "common_site_config.json", "apps.txt", "apps.json",
               "assets", ".DS_Store", "logs", "site_provisioner.py"}
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
            logger.warning("Failed to fix DB password for %s: %s", site_name, result.stderr[:200])
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

    logger.info("Replenishing pool: %d sites needed (current: %d/%d)",
                needed, len(current_pool), POOL_SIZE)

    for _ in range(needed):
        # Check max total sites limit
        all_sites = _get_all_sites()
        if len(all_sites) >= MAX_SITES + POOL_SIZE:
            logger.warning("Total site limit reached, cannot create more pool sites")
            break

        name = _next_pool_name()
        if not _bench_lock.acquire(timeout=5):
            logger.warning("bench lock busy, skipping pool replenish")
            break

        try:
            success, output = _run_bench([
                "new-site", name,
                "--mariadb-user-host-login-scope=%",
                "--admin-password", DEFAULT_ADMIN_PASSWORD,
                "--db-root-username", "root",
                "--db-root-password", MARIADB_ROOT_PASSWORD,
                "--install-app", "erpnext",
            ], timeout=300)
            if success:
                _fix_db_password(name)  # Ensure DB user password matches config
                logger.info("Pool site created: %s", name)
            else:
                logger.error("Failed to create pool site %s: %s", name, output[-200:])
        finally:
            _bench_lock.release()


def _replenish_pool_background() -> None:
    """Start pool replenishment in a background thread."""
    thread = threading.Thread(target=_replenish_pool_sync, daemon=True)
    thread.start()


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
            self._send_json(200, {
                "success": True,
                "status": "healthy",
                "max_sites": MAX_SITES,
                "current_sites": len(tenants),
                "pool_size": len(pool),
                "pool_target": POOL_SIZE,
            })
        elif self.path == "/list-sites":
            sites = _get_tenant_sites()
            self._send_json(200, {
                "success": True,
                "sites": sites,
                "count": len(sites),
                "max_sites": MAX_SITES,
                "remaining": max(0, MAX_SITES - len(sites)),
            })
        elif self.path == "/pool-status":
            pool = _get_pool_sites()
            self._send_json(200, {
                "success": True,
                "pool_sites": pool,
                "available": len(pool),
                "target": POOL_SIZE,
                "ready": len(pool) > 0,
            })
        else:
            self._send_json(404, {"success": False, "error": "Not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/create-site":
            self._handle_create_site()
        elif self.path == "/claim-site":
            self._handle_claim_site()
        elif self.path == "/drop-site":
            self._handle_drop_site()
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
            self._send_json(200, {
                "success": True,
                "already_exists": True,
                "site_name": site_name,
                "message": "Site already exists",
            })
            return

        # Enforce max sites limit
        current_sites = _get_tenant_sites()
        if len(current_sites) >= MAX_SITES:
            self._send_json(429, {
                "success": False,
                "error": (
                    f"Maximum site limit reached ({MAX_SITES}). "
                    f"Delete an existing tenant before creating a new one. "
                    f"Current sites: {', '.join(current_sites)}"
                ),
                "current_sites": len(current_sites),
                "max_sites": MAX_SITES,
            })
            return

        # Acquire lock — bench is not concurrency-safe
        if not _bench_lock.acquire(timeout=5):
            self._send_json(503, {
                "success": False,
                "error": "Another site operation is in progress. Try again in a moment.",
            })
            return

        try:
            success, output = _run_bench([
                "new-site",
                site_name,
                "--mariadb-user-host-login-scope=%",
                "--admin-password", admin_password,
                "--db-root-username", "root",
                "--db-root-password", MARIADB_ROOT_PASSWORD,
                "--install-app", "erpnext",
            ], timeout=300)

            if success:
                self._send_json(201, {
                    "success": True,
                    "site_name": site_name,
                    "message": "Site created successfully",
                })
            else:
                # Check if it actually got created despite error output
                if (site_dir / "site_config.json").exists():
                    self._send_json(201, {
                        "success": True,
                        "site_name": site_name,
                        "message": "Site created (with warnings)",
                        "warnings": output[-200:],
                    })
                else:
                    self._send_json(500, {
                        "success": False,
                        "error": f"bench new-site failed: {output[-300:]}",
                    })
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
            self._send_json(200, {
                "success": True,
                "already_exists": True,
                "site_name": target_name,
            })
            return

        # Check tenant limit
        tenants = _get_tenant_sites()
        if len(tenants) >= MAX_SITES:
            self._send_json(429, {
                "success": False,
                "error": f"Maximum tenant limit reached ({MAX_SITES}). Delete a tenant first.",
            })
            return

        # Find an available pool site
        pool = _get_pool_sites()
        if not pool:
            # No warm sites — fall back to create-site
            self._send_json(503, {
                "success": False,
                "error": "No warm sites available in pool. Use /create-site as fallback.",
                "pool_empty": True,
            })
            return

        # Claim the first available pool site
        pool_site = pool[0]
        success, msg = _rename_site(pool_site, target_name)
        if success:
            logger.info("Claimed pool site: %s → %s", pool_site, target_name)
            # Trigger background replenishment
            _replenish_pool_background()
            self._send_json(200, {
                "success": True,
                "site_name": target_name,
                "claimed_from": pool_site,
                "message": "Site claimed from warm pool (instant)",
            })
        else:
            self._send_json(500, {"success": False, "error": msg})

    def _handle_drop_site(self) -> None:
        body = self._read_body()
        site_name = body.get("site_name", "").strip()

        if not site_name:
            self._send_json(400, {"success": False, "error": "site_name is required"})
            return

        # Safety: never allow dropping the default site
        if site_name == DEFAULT_SITE:
            self._send_json(403, {
                "success": False,
                "error": f"Cannot drop the default site '{DEFAULT_SITE}'",
            })
            return

        # Safety: reject dangerous names
        if ".." in site_name or "/" in site_name or "\\" in site_name:
            self._send_json(400, {"success": False, "error": "Invalid site_name"})
            return

        # Check if site exists
        site_dir = SITES_DIR / site_name
        if not site_dir.exists():
            self._send_json(200, {
                "success": True,
                "already_dropped": True,
                "message": f"Site {site_name} does not exist (already dropped)",
            })
            return

        if not _bench_lock.acquire(timeout=5):
            self._send_json(503, {
                "success": False,
                "error": "Another site operation is in progress. Try again in a moment.",
            })
            return

        try:
            success, output = _run_bench([
                "drop-site",
                site_name,
                "--force",
                "--db-root-username", "root",
                "--db-root-password", MARIADB_ROOT_PASSWORD,
            ], timeout=120)

            if success:
                self._send_json(200, {
                    "success": True,
                    "site_name": site_name,
                    "message": "Site dropped successfully",
                })
            else:
                # Check if it's actually gone despite error
                if not site_dir.exists():
                    self._send_json(200, {
                        "success": True,
                        "site_name": site_name,
                        "message": "Site dropped (with warnings)",
                    })
                else:
                    self._send_json(500, {
                        "success": False,
                        "error": f"bench drop-site failed: {output[-300:]}",
                    })
        finally:
            _bench_lock.release()

    def log_message(self, format: str, *args: object) -> None:
        """Override to use our logger instead of stderr."""
        logger.info("%s %s", self.client_address[0], format % args)


def main() -> None:
    server = HTTPServer(("0.0.0.0", PORT), ProvisionerHandler)
    pool = _get_pool_sites()
    logger.info(
        "Site provisioner started on port %d (max_sites=%d, pool=%d/%d)",
        PORT, MAX_SITES, len(pool), POOL_SIZE,
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
