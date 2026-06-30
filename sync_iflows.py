#!/usr/bin/env python3
"""
sync_iflows.py
==============

Customer-agnostic CPI → GitHub sync script.

Pulls Integration Flows (and optionally Value Mappings) out of any SAP BTP
Integration Suite tenant and unpacks them into iflows/<PackageId>/<IFlowId>/
so that the parent GitHub Actions workflow can commit the changes.

The script is parameterised entirely via environment variables — no per-customer
code edits required. To onboard a new customer, you only configure their
GitHub repo secrets / workflow inputs (see CUSTOMIZATION_GUIDE.md).

Required env vars:
    CPI_TENANT_URL        e.g. https://acme.it-cpi.cfapps.eu10.hana.ondemand.com
    CPI_TOKEN_URL         e.g. https://acme.authentication.eu10.hana.ondemand.com/oauth/token
    CPI_CLIENT_ID
    CPI_CLIENT_SECRET

Optional env vars (any can be left empty / unset):
    PACKAGES_FILTER       Comma-separated list of PackageIds to include (supports wildcards, e.g., Custom_*, *ERP*).
                          Empty => all editable packages.
    PACKAGES_EXCLUDE      Comma-separated list of PackageIds to skip (supports wildcards).
    INCLUDE_VALUE_MAPS    "true" => also export ValueMappingDesigntimeArtifacts.
    INCLUDE_SAP_PACKAGES  "true" => attempt to export SAP-shipped packages too
                          (default false; SAP packages are usually read-only).
    OUTPUT_DIR            Where to write unpacked artifacts. Default: iflows
    REQUEST_TIMEOUT       HTTP timeout in seconds. Default: 120
    MAX_RETRIES           Retries for transient API errors. Default: 3
    LOG_LEVEL             DEBUG | INFO | WARNING | ERROR. Default: INFO
    CUSTOMER_LABEL        Free-text label printed in the run header (audit aid).

Exit codes:
    0   success (changes committed by parent workflow if any)
    1   misconfiguration (missing env, bad URL, etc.)
    2   authentication failure
    3   API failure during enumeration
    4   one or more iFlow downloads failed
"""

from __future__ import annotations

import fnmatch
import io
import logging
import os
import shutil
import sys
import time
import zipfile
from pathlib import Path
from typing import Iterable

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# --------------------------------------------------------------------------
# Config & Environment Fallback (No python-dotenv required)
# --------------------------------------------------------------------------
# Explicitly check for local environment file from \config\customer.env
env_path = Path(__file__).parent / "config" / "customer.env"

if env_path.exists():
    logging.info("Loading local configurations from %s", env_path)
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            # Skip comments and empty lines
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            # Strip potential quotes around values
            os.environ[key.strip()] = val.strip().strip("'\"")


def _env(name: str, default: str | None = None, required: bool = False) -> str | None:
    v = os.environ.get(name, default)
    if required and not v:
        logging.error("Missing required environment variable: %s", name)
        sys.exit(1)
    return v


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

def _env(name: str, default: str | None = None, required: bool = False) -> str | None:
    v = os.environ.get(name, default)
    if required and not v:
        logging.error("Missing required environment variable: %s", name)
        sys.exit(1)
    return v


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "y", "on")


def _csv_env(name: str) -> list[str]:
    raw = os.environ.get(name, "") or ""
    return [x.strip() for x in raw.split(",") if x.strip()]


# --------------------------------------------------------------------------
# HTTP session with retry
# --------------------------------------------------------------------------

def build_session(max_retries: int) -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=max_retries,
        backoff_factor=1.5,
        status_forcelist=(500, 502, 503, 504),
        allowed_methods=("GET", "POST"),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


# --------------------------------------------------------------------------
# OAuth
# --------------------------------------------------------------------------

def get_token(session: requests.Session, token_url: str,
              client_id: str, client_secret: str, timeout: int) -> str:
    logging.info("Requesting OAuth access token …")
    try:
        r = session.post(
            token_url,
            auth=(client_id, client_secret),
            data={"grant_type": "client_credentials"},
            headers={"Accept": "application/json"},
            timeout=timeout,
        )
    except requests.RequestException as e:
        logging.error("Network error requesting token: %s", e)
        sys.exit(2)

    if r.status_code != 200:
        logging.error("Token request failed: HTTP %s — %s", r.status_code, r.text[:300])
        sys.exit(2)

    try:
        token = r.json()["access_token"]
    except Exception:
        logging.error("Token response was not valid JSON or missing access_token")
        sys.exit(2)

    logging.info("Token acquired (length=%d)", len(token))
    return token


# --------------------------------------------------------------------------
# CPI API helpers
# --------------------------------------------------------------------------

def list_packages(session: requests.Session, tenant: str,
                  token: str, timeout: int) -> list[dict]:
    url = f"{tenant.rstrip('/')}/api/v1/IntegrationPackages"
    r = session.get(
        url,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        timeout=timeout,
    )
    if r.status_code != 200:
        logging.error("List packages failed: HTTP %s — %s", r.status_code, r.text[:300])
        sys.exit(3)
    try:
        return r.json()["d"]["results"]
    except Exception:
        logging.error("Unexpected payload listing packages")
        sys.exit(3)


def list_iflows_in_package(session: requests.Session, tenant: str, token: str,
                             package_id: str, timeout: int) -> list[dict]:
    url = (f"{tenant.rstrip('/')}/api/v1/IntegrationPackages('{package_id}')"
           f"/IntegrationDesigntimeArtifacts")
    r = session.get(
        url,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        timeout=timeout,
    )
    if r.status_code == 404:
        logging.info("  ↳ package %s: IntegrationDesigntimeArtifacts not exposed (likely SAP-shipped). Skipping.",
                     package_id)
        return []
    if r.status_code != 200:
        logging.warning("  ↳ package %s: unexpected HTTP %s while listing iFlows: %s",
                        package_id, r.status_code, r.text[:200])
        return []
    try:
        return r.json()["d"]["results"]
    except Exception:
        return []


def list_valuemaps_in_package(session: requests.Session, tenant: str, token: str,
                               package_id: str, timeout: int) -> list[dict]:
    url = (f"{tenant.rstrip('/')}/api/v1/IntegrationPackages('{package_id}')"
           f"/ValueMappingDesigntimeArtifacts")
    r = session.get(
        url,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        timeout=timeout,
    )
    if r.status_code == 404:
        return []
    if r.status_code != 200:
        logging.warning("  ↳ package %s: HTTP %s listing value maps", package_id, r.status_code)
        return []
    try:
        return r.json()["d"]["results"]
    except Exception:
        return []


def download_artifact(session: requests.Session, tenant: str, token: str,
                       artifact_type: str, art_id: str, version: str,
                       timeout: int) -> bytes | None:
    """
    artifact_type: "IntegrationDesigntimeArtifacts" or "ValueMappingDesigntimeArtifacts"
    """
    url = (f"{tenant.rstrip('/')}/api/v1/{artifact_type}"
           f"(Id='{art_id}',Version='{version}')/$value")
    r = session.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=timeout,
        stream=False,
    )
    if r.status_code == 404:
        logging.info("    ↳ %s %s v%s: not exportable (404). Skipping.",
                     artifact_type, art_id, version)
        return None
    if r.status_code != 200:
        logging.warning("    ↳ %s %s v%s: HTTP %s — %s",
                        artifact_type, art_id, version, r.status_code, r.text[:200])
        return None
    return r.content


# --------------------------------------------------------------------------
# ZIP extraction
# --------------------------------------------------------------------------

def safe_extract(zip_bytes: bytes, target_dir: Path) -> int:
    """Replace the artifact directory with the unpacked contents. Returns file count."""
    if target_dir.exists():
        shutil.rmtree(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    count = 0
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for member in zf.infolist():
            # Defend against zip-slip
            member_path = (target_dir / member.filename).resolve()
            if not str(member_path).startswith(str(target_dir.resolve())):
                logging.warning("Skipping suspicious zip entry: %s", member.filename)
                continue
            zf.extract(member, target_dir)
            count += 1
    return count


# --------------------------------------------------------------------------
# Filtering helpers
# --------------------------------------------------------------------------

def package_in_scope(pkg: dict,
                     include: list[str], exclude: list[str],
                     include_sap: bool) -> bool:
    pkg_id = pkg.get("Id", "")

    # Check if package matches any patterns in the include filter
    if include and not any(fnmatch.fnmatch(pkg_id, pattern) for pattern in include):
        return False

    # Check if package matches any patterns in the exclude filter
    if any(fnmatch.fnmatch(pkg_id, pattern) for pattern in exclude):
        return False

    if not include_sap:
        # Heuristic: SAP-shipped packages have SupportedSoftwareVersion or Vendor markers
        vendor = (pkg.get("Vendor") or "").strip().upper()
        mode = (pkg.get("Mode") or "").strip().upper()
        if vendor == "SAP" or mode == "READ_ONLY":
            return False
    return True


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main() -> int:
    log_level = (os.environ.get("LOG_LEVEL") or "INFO").upper()
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    tenant = _env("CPI_TENANT_URL", required=True)
    token_url = _env("CPI_TOKEN_URL", required=True)
    client_id = _env("CPI_CLIENT_ID", required=True)
    client_secret = _env("CPI_CLIENT_SECRET", required=True)

    include = _csv_env("PACKAGES_FILTER")
    exclude = _csv_env("PACKAGES_EXCLUDE")
    include_value_maps = _bool_env("INCLUDE_VALUE_MAPS", False)
    include_sap = _bool_env("INCLUDE_SAP_PACKAGES", False)

    output_dir = Path(_env("OUTPUT_DIR", "iflows"))
    timeout = int(_env("REQUEST_TIMEOUT", "120"))
    retries = int(_env("MAX_RETRIES", "3"))
    customer = _env("CUSTOMER_LABEL", "")

    print("=" * 72)
    print(f"CPI → GitHub Sync")
    if customer:
        print(f"Customer        : {customer}")
    print(f"Tenant          : {tenant}")
    print(f"Output dir      : {output_dir}")
    print(f"Include filter  : {include or '(all editable)'}")
    print(f"Exclude filter  : {exclude or '(none)'}")
    print(f"Value mappings  : {'yes' if include_value_maps else 'no'}")
    print(f"SAP packages    : {'yes' if include_sap else 'skip'}")
    print("=" * 72)

    session = build_session(retries)
    token = get_token(session, token_url, client_id, client_secret, timeout)

    packages = list_packages(session, tenant, token, timeout)
    in_scope = [p for p in packages
                if package_in_scope(p, include, exclude, include_sap)]
    logging.info("Found %d package(s) total, %d in scope.", len(packages), len(in_scope))

    failures: list[str] = []
    iflow_count = 0
    vmap_count = 0

    for pkg in in_scope:
        pkg_id = pkg["Id"]
        pkg_name = pkg.get("Name") or pkg_id
        print(f"📦 {pkg_id}  ({pkg_name})")

        # iFlows
        iflows = list_iflows_in_package(session, tenant, token, pkg_id, timeout)
        for art in iflows:
            art_id = art["Id"]
            ver = art.get("Version", "active")
            print(f"  ↳ iflow: {art_id} v{ver}")
            payload = download_artifact(
                session, tenant, token,
                "IntegrationDesigntimeArtifacts", art_id, ver, timeout)
            if payload is None:
                failures.append(f"iflow {pkg_id}/{art_id}@{ver}")
                continue
            target = output_dir / pkg_id / art_id
            try:
                count = safe_extract(payload, target)
                logging.debug("    extracted %d files into %s", count, target)
                iflow_count += 1
            except Exception as e:
                logging.error("    extract failed for %s/%s: %s", pkg_id, art_id, e)
                failures.append(f"extract {pkg_id}/{art_id}")

        # Value Mappings (optional)
        if include_value_maps:
            vmaps = list_valuemaps_in_package(session, tenant, token, pkg_id, timeout)
            for vm in vmaps:
                vm_id = vm["Id"]
                ver = vm.get("Version", "active")
                print(f"  ↳ valmap: {vm_id} v{ver}")
                payload = download_artifact(
                    session, tenant, token,
                    "ValueMappingDesigntimeArtifacts", vm_id, ver, timeout)
                if payload is None:
                    failures.append(f"valmap {pkg_id}/{vm_id}@{ver}")
                    continue
                target = output_dir / pkg_id / "_valuemappings" / vm_id
                try:
                    safe_extract(payload, target)
                    vmap_count += 1
                except Exception as e:
                    logging.error("    extract failed for valmap %s/%s: %s", pkg_id, vm_id, e)
                    failures.append(f"extract valmap {pkg_id}/{vm_id}")

    print("=" * 72)
    print(f"iFlows synced       : {iflow_count}")
    if include_value_maps:
        print(f"Value maps synced   : {vmap_count}")
    print(f"Failures            : {len(failures)}")
    if failures:
        for f in failures:
            print(f"   ✗ {f}")
        print("✖ Sync completed with errors.")
        return 4
    print("✅ Sync complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
