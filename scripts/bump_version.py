# -*- coding: utf-8 -*-
"""Atomically stamp the release version everywhere it must agree.

The version lives in three places on purpose:
  app/__init__.py  — what the API reports (/healthz, /api/v1/info, /metrics)
  static/sw.js     — the service worker cache name (BUILD)
  static/index.html— the ?v= build stamp on code assets

They must never disagree: a stale cache key is how a returning visitor runs the
previous release's client (Round 13.7 browser QA), and tests/test_frontend_invariants.py
fails the build when they drift. This script is the only sanctioned way to move
all three at once.

Usage: python scripts/bump_version.py 0.11.9
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:[-.][0-9A-Za-z.-]+)?$")


def bump(version: str) -> list[str]:
    """Rewrite every version-bearing file; return the paths that changed."""
    changed: list[str] = []

    init = ROOT / "app" / "__init__.py"
    text = init.read_text(encoding="utf-8")
    new, n = re.subn(r'__version__ = "[^"]*"', f'__version__ = "{version}"', text, count=1)
    assert n == 1, "app/__init__.py has no __version__ assignment"
    if new != text:
        init.write_text(new, encoding="utf-8")
        changed.append(str(init.relative_to(ROOT)))

    sw = ROOT / "static" / "sw.js"
    text = sw.read_text(encoding="utf-8")
    new, n = re.subn(r"const BUILD = '[^']*'", f"const BUILD = '{version}'", text, count=1)
    assert n == 1, "static/sw.js has no BUILD stamp"
    if new != text:
        sw.write_text(new, encoding="utf-8")
        changed.append(str(sw.relative_to(ROOT)))

    html = ROOT / "static" / "index.html"
    text = html.read_text(encoding="utf-8")
    # every asset URL that already carries a stamp, whatever the old value was
    new, n = re.subn(r'((?:styles\.css|i18n\.js|app\.js|tg-boot\.js|sw-boot\.js)\?v=)[\d.]+',
                     r'\g<1>' + version, text)
    assert n >= 5, f"expected 5 stamped asset URLs in index.html, found {n}"
    if new != text:
        html.write_text(new, encoding="utf-8")
        changed.append(str(html.relative_to(ROOT)))

    return changed


def main() -> int:
    if len(sys.argv) != 2 or not SEMVER.match(sys.argv[1]):
        print(__doc__)
        print("ERROR: give one semver, e.g. 0.11.9")
        return 2
    version = sys.argv[1]
    changed = bump(version)
    print(f"version -> {version}")
    for path in changed or ():
        print(f"  stamped {path}")
    if not changed:
        print("  already at this version")
    print("next: python -m pytest -q  (invariant tests enforce the lockstep)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
