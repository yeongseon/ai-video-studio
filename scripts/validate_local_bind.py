#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
# How to run: python3 scripts/validate_local_bind.py (or uv run this script).
"""Parse operator bind input; inspect legacy .env text without loading or executing it."""
from __future__ import annotations

from ipaddress import IPv4Address, IPv6Address, ip_address, ip_network
import os
from pathlib import Path
import re
import shlex
import sys
from typing import Final

LOOPBACK_NETWORKS: Final = (ip_network("127.0.0.0/8"), ip_network("::1/128"))
WILDCARD_NETWORKS: Final = (ip_network("0.0.0.0/32"), ip_network("::/128"))
LAN_NETWORKS: Final = tuple(map(ip_network, (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10",
    "169.254.0.0/16", "fe80::/10", "fc00::/7",
))) + WILDCARD_NETWORKS


def literal_address(raw: str) -> IPv4Address | IPv6Address | None:
    """Normalize mapped addresses consistently across supported host Pythons."""
    if "%" in raw:
        return None
    try:
        address = ip_address(raw)
    except ValueError:
        return None
    if address.version == 6 and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def legacy_env_is_safe(confirmed: bool) -> bool:
    """Diagnose assignments without trusting .env for bind or confirmation values."""
    try:
        lines = Path(".env").read_text(encoding="utf-8-sig", errors="replace").splitlines()
    except FileNotFoundError:
        return True
    except OSError as error:
        print(f"WARNING: cannot inspect .env ({error.strerror}); "
              "legacy STUDIO_WEB_BIND check skipped", file=sys.stderr)
        return True
    safe = True
    for line in lines:
        assignment = re.fullmatch(r"\s*(?:export\s+)?STUDIO_WEB_BIND\s*[=:](.*)", line)
        if assignment is None:
            continue
        try:
            values = shlex.split(assignment.group(1), comments=True)
        except ValueError:
            values = [assignment.group(1)]
        if not values or values == [""]:
            continue
        raw = values[0] if len(values) == 1 else ""
        if raw.startswith("[") and raw.endswith("]"):
            raw = raw[1:-1]
        address = literal_address(raw)
        if address is not None and any(address in network for network in LOOPBACK_NETWORKS):
            continue
        print("WARNING: .env contains non-loopback STUDIO_WEB_BIND; remove this line: "
              "direct Compose bypasses the Make guard and can expose the shared-key proxy", file=sys.stderr)
        if address is not None and any(address in network for network in WILDCARD_NETWORKS) and not confirmed:
            print("Legacy .env wildcard STUDIO_WEB_BIND requires STUDIO_WEB_LAN_OK=1 in the shell", file=sys.stderr)
            safe = False
    return safe


def main() -> int:
    confirmed = os.environ.get("STUDIO_WEB_LAN_OK") == "1"
    if not legacy_env_is_safe(confirmed):
        return 2
    raw = os.environ.get("STUDIO_WEB_BIND", "127.0.0.1")
    address = literal_address(raw)
    if address is None:
        print("STUDIO_WEB_BIND must be a nonempty literal IPv4/IPv6 address", file=sys.stderr)
        return 2
    loopback = any(address in network for network in LOOPBACK_NETWORKS)
    if not loopback and not any(address in network for network in LAN_NETWORKS):
        print("STUDIO_WEB_BIND outside local allowlist; global addresses are forbidden", file=sys.stderr)
        return 2
    if not loopback and not confirmed:
        print("STUDIO_WEB_BIND non-loopback address requires STUDIO_WEB_LAN_OK=1", file=sys.stderr)
        return 2
    print(f"[{address}]" if address.version == 6 else str(address))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
