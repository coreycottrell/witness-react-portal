#!/usr/bin/env python3
"""
Witness Extension API — /api/witness/* endpoints

Mounted by portal_server.py alongside the base portal routes.
All endpoints are Witness-only (not deployed to born CIVs).

Endpoints:
  GET  /api/witness/fleet          — list all fleet containers
  GET  /api/witness/margins        — per-CIV margin data
  GET  /api/witness/alerts         — operational alerts
  POST /api/witness/alerts/{id}/ack — acknowledge an alert
"""
import json
import subprocess
import time
import uuid
from pathlib import Path
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

SCRIPT_DIR = Path(__file__).parent
FLEET_REGISTRY = Path.home() / "civ" / "registry" / "fleet-registry.json"
MARGIN_COREY = SCRIPT_DIR / "margin-corey.json"
MARGIN_PRIMARY = SCRIPT_DIR / "margin-primary.json"
ALERTS_FILE = SCRIPT_DIR / ".witness-alerts.json"


# ---------------------------------------------------------------------------
# Fleet
# ---------------------------------------------------------------------------

async def fleet_list(request: Request) -> JSONResponse:
    """List all AiCIV containers from fleet registry, enriched with live Docker status."""
    containers = []
    docker_error = None

    # Load fleet registry — source of truth for metadata
    aicivs: dict = {}
    if FLEET_REGISTRY.exists():
        try:
            reg_data = json.loads(FLEET_REGISTRY.read_text())
            if isinstance(reg_data, dict):
                aicivs = reg_data.get("aicivs", {})
        except Exception:
            pass

    # Build container-name -> registry entry lookup for Docker enrichment
    container_lookup: dict = {}
    for civ_key, meta in aicivs.items():
        if isinstance(meta, dict):
            cname = meta.get("container")
            if cname:
                container_lookup[cname] = (civ_key, meta)

    # Get live Docker status (best-effort)
    docker_status: dict = {}
    try:
        result = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{.Names}}\t{{.Status}}"],
            capture_output=True, text=True, timeout=10,
        )
        for line in result.stdout.strip().splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                docker_status[parts[0]] = parts[1]
    except Exception as e:
        docker_error = str(e)

    # Build response from registry, enriched with Docker live status
    for civ_key, meta in aicivs.items():
        if not isinstance(meta, dict):
            continue
        container = meta.get("container") or ""
        reg_status = meta.get("status", "unknown")

        # Determine live status
        if container and container in docker_status:
            ds = docker_status[container]
            live_status = "running" if "Up" in ds else "stopped"
        else:
            live_status = reg_status  # fall back to registry status

        containers.append({
            "name": container or civ_key,
            "civ_name": civ_key,
            "human": meta.get("human") or meta.get("human_name") or "",
            "status": live_status,
            "portal_url": meta.get("portal_url") or "",
            "ssh_command": meta.get("ssh_command") or "",
            "ssh_port": meta.get("ssh_port"),
            "api_port": meta.get("api_port"),
            "host_ip": meta.get("host_ip") or "",
            "tmux_session": meta.get("tmux_session") or "",
        })

    containers.sort(key=lambda c: c["civ_name"] or "")
    response: dict = {"containers": containers, "count": len(containers)}
    if docker_error:
        response["docker_error"] = docker_error
    return JSONResponse(response)


# ---------------------------------------------------------------------------
# Margins
# ---------------------------------------------------------------------------

async def margins_list(request: Request) -> JSONResponse:
    """Return per-CIV margin data from margin JSON files."""
    entries = []

    def load_margin_file(path: Path) -> dict:
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text())
        except Exception:
            return {}

    corey_data = load_margin_file(MARGIN_COREY)
    primary_data = load_margin_file(MARGIN_PRIMARY)

    # The margin JSON files are journal arrays, not revenue dicts.
    # Financial margin data not yet populated — return empty response.
    if isinstance(corey_data, list) or isinstance(primary_data, list):
        return JSONResponse({"entries": [], "totals": {"revenue": 0.0, "cost": 0.0, "margin": 0.0}})

    # Merge: primary_data has cost estimates, corey_data has revenue
    all_keys = set(list(corey_data.keys()) + list(primary_data.keys()))
    total_revenue = total_cost = total_margin = 0.0

    for key in sorted(all_keys):
        corey = corey_data.get(key, {})
        primary = primary_data.get(key, {})
        revenue = float(corey.get("subscription_amount", 0) or primary.get("subscription_amount", 0))
        cost = float(primary.get("cost_estimate", corey.get("cost_estimate", 0)))
        margin = revenue - cost
        margin_pct = (margin / revenue * 100) if revenue > 0 else 0.0

        entries.append({
            "civ_name": key,
            "human_name": corey.get("human_name") or primary.get("human_name", ""),
            "subscription_amount": revenue,
            "cost_estimate": cost,
            "margin": margin,
            "margin_pct": margin_pct,
        })
        total_revenue += revenue
        total_cost += cost
        total_margin += margin

    return JSONResponse({
        "entries": entries,
        "totals": {
            "revenue": total_revenue,
            "cost": total_cost,
            "margin": total_margin,
        },
    })


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------

def _load_alerts() -> list:
    if not ALERTS_FILE.exists():
        return []
    try:
        return json.loads(ALERTS_FILE.read_text())
    except Exception:
        return []


def _save_alerts(alerts: list) -> None:
    ALERTS_FILE.write_text(json.dumps(alerts, indent=2))


async def alerts_list(request: Request) -> JSONResponse:
    """Return active operational alerts."""
    alerts = _load_alerts()
    return JSONResponse({"alerts": alerts})


async def alert_acknowledge(request: Request) -> JSONResponse:
    """Mark an alert as acknowledged."""
    alert_id = request.path_params.get("id")
    alerts = _load_alerts()
    found = False
    for a in alerts:
        if a.get("id") == alert_id:
            a["acknowledged"] = True
            found = True
            break
    if found:
        _save_alerts(alerts)
        return JSONResponse({"ok": True})
    return JSONResponse({"error": "alert not found"}, status_code=404)


def add_alert(severity: str, title: str, body: str) -> None:
    """Helper for other modules to add alerts programmatically."""
    alerts = _load_alerts()
    alerts.insert(0, {
        "id": str(uuid.uuid4()),
        "severity": severity,
        "title": title,
        "body": body,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "acknowledged": False,
    })
    # Keep at most 100 alerts
    _save_alerts(alerts[:100])


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------

WITNESS_ROUTES = [
    Route("/api/witness/fleet", endpoint=fleet_list, methods=["GET"]),
    Route("/api/witness/margins", endpoint=margins_list, methods=["GET"]),
    Route("/api/witness/alerts", endpoint=alerts_list, methods=["GET"]),
    Route("/api/witness/alerts/{id}/ack", endpoint=alert_acknowledge, methods=["POST"]),
]
