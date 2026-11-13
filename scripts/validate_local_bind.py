#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
# How to run: python3 scripts/validate_local_bind.py (or uv run this script).
"""Parse operator input into a Compose host literal, without loading .env."""
from __future__ import annotations

from ipaddress import ip_address
import os
import sys


def main() -> int:
    raw = os.environ.get("STUDIO_WEB_BIND", "127.0.0.1")
    try:
        address = ip_address(raw)
    except ValueError:
        print("STUDIO_WEB_BIND must be a nonempty literal IPv4/IPv6 address", file=sys.stderr)
        return 2
    if "%" in raw:
        print("STUDIO_WEB_BIND must not contain a scope identifier", file=sys.stderr)
        return 2
    # The Makefile runs the host's own python3 (3.10 on Ubuntu 22.04), where a mapped wildcard
    # such as ::ffff:0.0.0.0 is not "unspecified" to the stdlib; unwrap it so it cannot bypass
    # the wildcard confirmation, whatever the interpreter version.
    if address.version == 6 and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if address.is_unspecified and os.environ.get("STUDIO_WEB_LAN_OK") != "1":
        print("STUDIO_WEB_BIND wildcard requires STUDIO_WEB_LAN_OK=1", file=sys.stderr)
        return 2
    print(f"[{address}]" if address.version == 6 else str(address))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
