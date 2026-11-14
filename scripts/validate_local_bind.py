#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
# How to run: python3 scripts/validate_local_bind.py (or uv run this script).
"""Parse operator bind input; inspect legacy .env text without loading or executing it."""
from __future__ import annotations

from ipaddress import IPv4Address, IPv6Address, ip_address
import os
from pathlib import Path
import re
import shlex
import sys


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
        lines = Path(".env").read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return True
    safe = True
    for line in lines:
        assignment = re.fullmatch(r"\s*(?:export\s+)?STUDIO_WEB_BIND\s*=(.*)", line)
        if assignment is None:
            continue
        try:
            values = shlex.split(assignment.group(1), comments=True)
        except ValueError:
            values = []
        address = literal_address(values[0]) if len(values) == 1 else None
        if address is not None and address.is_loopback:
            continue
        print("WARNING: .env contains non-loopback STUDIO_WEB_BIND; remove this line: "
              "direct Compose bypasses the Make guard and can expose the shared-key proxy", file=sys.stderr)
        if address is not None and address.is_unspecified and not confirmed:
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
    if address.is_global:
        print("STUDIO_WEB_BIND global addresses are forbidden; use loopback or a trusted LAN", file=sys.stderr)
        return 2
    if not address.is_loopback and not confirmed:
        print("STUDIO_WEB_BIND non-loopback address requires STUDIO_WEB_LAN_OK=1", file=sys.stderr)
        return 2
    print(f"[{address}]" if address.version == 6 else str(address))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
