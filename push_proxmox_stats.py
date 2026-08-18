#!/usr/bin/env python3
"""
push_proxmox_stats.py

Polls a Proxmox VE node's API for host + VM/LXC status, packages it into a
compact JSON payload, and pushes it to a TRMNL private plugin webhook so it
shows up on your e-ink display.

Run manually:
    python push_proxmox_stats.py --once

Run on a schedule:
    See the systemd timer / cron examples in README.md.

Configuration is read from environment variables (or a .env file next to
this script — see .env.example).
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from dataclasses import dataclass

import requests

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass  # dotenv is optional; env vars can be set directly instead

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("trmnl-proxmox")


@dataclass
class Config:
    proxmox_host: str
    proxmox_node: str
    token_id: str
    token_secret: str
    verify_ssl: bool
    trmnl_uuid: str
    max_payload_bytes: int
    max_vms_listed: int

    @classmethod
    def from_env(cls) -> "Config":
        missing = [
            name
            for name in (
                "PROXMOX_HOST",
                "PROXMOX_NODE",
                "PROXMOX_TOKEN_ID",
                "PROXMOX_TOKEN_SECRET",
                "TRMNL_PLUGIN_UUID",
            )
            if not os.environ.get(name)
        ]
        if missing:
            log.error("Missing required environment variables: %s", ", ".join(missing))
            sys.exit(1)

        return cls(
            proxmox_host=os.environ["PROXMOX_HOST"],
            proxmox_node=os.environ["PROXMOX_NODE"],
            token_id=os.environ["PROXMOX_TOKEN_ID"],
            token_secret=os.environ["PROXMOX_TOKEN_SECRET"],
            verify_ssl=os.environ.get("PROXMOX_VERIFY_SSL", "false").lower() == "true",
            trmnl_uuid=os.environ["TRMNL_PLUGIN_UUID"],
            # TRMNL standard accounts: 2KB payload cap. TRMNL+: 5KB.
            # Leave headroom under the merge_variables wrapper.
            max_payload_bytes=int(os.environ.get("TRMNL_MAX_PAYLOAD_BYTES", "1900")),
            max_vms_listed=int(os.environ.get("MAX_VMS_LISTED", "20")),
        )


def proxmox_get(cfg: Config, path: str) -> dict:
    url = f"https://{cfg.proxmox_host}:8006/api2/json{path}"
    headers = {
        "Authorization": f"PVEAPIToken={cfg.token_id}={cfg.token_secret}"
    }
    resp = requests.get(url, headers=headers, verify=cfg.verify_ssl, timeout=10)
    resp.raise_for_status()
    return resp.json()["data"]


def format_uptime(seconds: int) -> str:
    days, rem = divmod(int(seconds), 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def collect_host_stats(cfg: Config) -> dict:
    status = proxmox_get(cfg, f"/nodes/{cfg.proxmox_node}/status")

    mem = status["memory"]
    disk = status["rootfs"]
    cpu_percent = round(status["cpu"] * 100, 1)
    mem_total_gb = round(mem["total"] / 1024**3, 1)
    mem_used_gb = round(mem["used"] / 1024**3, 1)
    mem_percent = round(mem["used"] / mem["total"] * 100, 1) if mem["total"] else 0
    disk_total_gb = round(disk["total"] / 1024**3, 1)
    disk_used_gb = round(disk["used"] / 1024**3, 1)
    disk_percent = round(disk["used"] / disk["total"] * 100, 1) if disk["total"] else 0

    return {
        "node": cfg.proxmox_node,
        "cpu_percent": cpu_percent,
        "mem_used_gb": mem_used_gb,
        "mem_total_gb": mem_total_gb,
        "mem_percent": mem_percent,
        "disk_used_gb": disk_used_gb,
        "disk_total_gb": disk_total_gb,
        "disk_percent": disk_percent,
        "uptime": format_uptime(status["uptime"]),
    }


def collect_vms(cfg: Config) -> list[dict]:
    vms = []
    for kind, path in (("qemu", "qemu"), ("lxc", "lxc")):
        try:
            entries = proxmox_get(cfg, f"/nodes/{cfg.proxmox_node}/{path}")
        except requests.HTTPError as exc:
            log.warning("Could not list %s: %s", path, exc)
            continue
        for e in entries:
            vms.append(
                {
                    "vmid": e["vmid"],
                    "name": e.get("name", f"{kind}-{e['vmid']}"),
                    "type": kind,
                    "status": e.get("status", "unknown"),
                }
            )

    # Running first, then stopped; alphabetical within each group.
    vms.sort(key=lambda v: (v["status"] != "running", v["name"].lower()))
    return vms


def build_payload(cfg: Config) -> dict:
    host = collect_host_stats(cfg)
    vms = collect_vms(cfg)

    payload = {
        **host,
        "vm_total": len(vms),
        "vm_running": sum(1 for v in vms if v["status"] == "running"),
        "updated_at": time.strftime("%b %d, %-I:%M %p"),
        "vms": vms,
    }

    # Trim the VM list to respect TRMNL's payload size limit. Drop from the
    # end (stopped VMs sort last) until we're under budget or hit the cap.
    payload["vms"] = payload["vms"][: cfg.max_vms_listed]
    while len(payload["vms"]) > 0 and _payload_size(payload) > cfg.max_payload_bytes:
        payload["vms"].pop()

    return payload


def _payload_size(payload: dict) -> int:
    return len(json.dumps({"merge_variables": payload}, separators=(",", ":")).encode())


def push_to_trmnl(cfg: Config, payload: dict) -> None:
    url = f"https://trmnl.com/api/custom_plugins/{cfg.trmnl_uuid}"
    body = {"merge_variables": payload}
    size = _payload_size(payload)
    log.info("Payload size: %d bytes (limit %d)", size, cfg.max_payload_bytes)

    resp = requests.post(url, json=body, headers={"Content-Type": "application/json"}, timeout=10)
    if resp.ok:
        log.info("Pushed to TRMNL: %s", resp.status_code)
    else:
        log.error("TRMNL push failed: %s %s", resp.status_code, resp.text[:300])
        resp.raise_for_status()


def main() -> None:
    cfg = Config.from_env()
    try:
        payload = build_payload(cfg)
        log.info(
            "Node %s: CPU %.1f%%  Mem %.1f%%  Disk %.1f%%  VMs %d/%d running",
            payload["node"],
            payload["cpu_percent"],
            payload["mem_percent"],
            payload["disk_percent"],
            payload["vm_running"],
            payload["vm_total"],
        )
        push_to_trmnl(cfg, payload)
    except requests.RequestException as exc:
        log.error("Request failed: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
