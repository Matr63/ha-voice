#!/usr/bin/env python3
"""Copy custom_components/ha_voice into addon/integration/ (the add-on build
context only sees addon/, and the add-on installs the integration into HA).

    python scripts/sync_integration.py          # copy
    python scripts/sync_integration.py --check  # CI: fail when out of sync
"""
import filecmp
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "custom_components" / "ha_voice"
DST = ROOT / "addon" / "integration" / "ha_voice"


def differs(a: Path, b: Path) -> bool:
    if not b.is_dir():
        return True
    cmp = filecmp.dircmp(a, b, ignore=["__pycache__"])
    if cmp.left_only or cmp.right_only or cmp.diff_files or cmp.funny_files:
        return True
    return any(differs(a / d, b / d) for d in cmp.common_dirs)


if "--check" in sys.argv:
    if differs(SRC, DST):
        sys.exit("addon/integration/ha_voice is out of sync: run python scripts/sync_integration.py")
    print("integration copy in sync")
else:
    shutil.rmtree(DST, ignore_errors=True)
    shutil.copytree(SRC, DST, ignore=shutil.ignore_patterns("__pycache__"))
    print(f"copied {SRC} -> {DST}")
