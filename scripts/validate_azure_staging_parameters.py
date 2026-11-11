#!/usr/bin/env python3
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Final


PLACEHOLDER: Final = re.compile(r"<[A-Z0-9_]+>|TODO|example\.com", re.IGNORECASE)


def main() -> int:
    parameter_file = Path(sys.argv[1])
    matches = sorted(set(PLACEHOLDER.findall(parameter_file.read_text(encoding="utf-8"))))
    if matches:
        print(f"ERROR: placeholder values must be replaced: {', '.join(matches)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
