"""Feature flags: gradual rollout + A/B testing infrastructure.

Flags are stored in-memory with a simple JSON file backing store.
Each flag can be:
  - globally enabled/disabled
  - gated by plan level (free/pro/studio)
  - percentage roll-out (0-100, deterministic by user_id hash)

Usage:
    from .flags import is_enabled
    if is_enabled("new_pricing", user_id=uid, plan="pro"):
        ...
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import threading
from pathlib import Path
from typing import Any

from .config import settings

_FLAGS_FILE: Path | None = None
_lock = threading.Lock()
_flags: dict[str, dict[str, Any]] = {}
log = logging.getLogger(__name__)


def _flags_path() -> Path:
    global _FLAGS_FILE
    if _FLAGS_FILE is None:
        _FLAGS_FILE = settings.data_dir / "feature_flags.json"
    return _FLAGS_FILE


def load() -> None:
    """Load flags from disk (called at startup). Robust: a corrupt or hand-
    mangled file must never stop the whole API from booting (fail-open)."""
    global _flags
    path = _flags_path()
    with _lock:
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                log.warning("feature_flags.json unreadable — resetting to defaults")
                loaded = None
            _flags = loaded if isinstance(loaded, dict) else {}
            # Backfill any flags introduced since the file was written, so new
            # defaults (and their kill-switch semantics) apply on existing installs.
            changed = False
            for k, v in _DEFAULTS.items():
                if k not in _flags or not isinstance(_flags[k], dict):
                    _flags[k] = copy.deepcopy(v)
                    changed = True
            if changed:
                _save()
        else:
            _flags = copy.deepcopy(_DEFAULTS)
            _save()


def _save() -> None:
    """Persist to disk. Call within _lock."""
    path = _flags_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(_flags, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


_DEFAULTS: dict[str, dict[str, Any]] = {
    "job_sharing": {"enabled": True, "min_plan": "free", "pct": 100},
    "api_keys": {"enabled": True, "min_plan": "pro", "pct": 100},
    "webhook_out": {"enabled": False, "min_plan": "studio", "pct": 0},
    "batch_jobs": {"enabled": False, "min_plan": "studio", "pct": 10},
    "ai_suggestions": {"enabled": False, "min_plan": "pro", "pct": 0},
    "dark_light_toggle": {"enabled": False, "min_plan": "free", "pct": 5},
    "uzbek_language_engine": {"enabled": True, "min_plan": "free", "pct": 100},
}

_PLAN_LEVEL = {"free": 0, "pro": 1, "studio": 2}


def is_enabled(flag: str, *, user_id: str = "", plan: str = "free") -> bool:
    """Check if a feature flag is enabled for the given user/plan."""
    with _lock:
        f = _flags.get(flag)
    if not f:
        return False
    if not f.get("enabled", False):
        return False
    # plan gate
    min_plan = f.get("min_plan", "free")
    if _PLAN_LEVEL.get(plan, 0) < _PLAN_LEVEL.get(min_plan, 0):
        return False
    # percentage rollout
    pct = f.get("pct", 100)
    if pct >= 100:
        return True
    if pct <= 0:
        return False
    if not user_id:
        return False
    # deterministic hash → bucket 0-99
    h = int(hashlib.md5(f"{flag}:{user_id}".encode()).hexdigest(), 16)
    return (h % 100) < pct


def get_all() -> dict[str, dict[str, Any]]:
    """Return copy of all flags (for admin panel)."""
    with _lock:
        return dict(_flags)


def set_flag(flag: str, **kwargs) -> None:
    """Update a flag's properties (admin only)."""
    with _lock:
        if flag not in _flags:
            _flags[flag] = {}
        _flags[flag].update(kwargs)
        _save()


def delete_flag(flag: str) -> bool:
    with _lock:
        if flag in _flags:
            del _flags[flag]
            _save()
            return True
    return False
