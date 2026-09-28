"""Применение пользовательского глоссария до и после машинного перевода."""
from __future__ import annotations

import re


def _single_pass(text: str, terms: dict[str, str], max_out: int) -> str:
    """One alternation pass with a *callable* replacement so substitute values
    are treated literally (no re-template escapes) and never re-scanned.

    Callable + longest-first means an output of rule N can't be re-hit by rule
    N+1 — this closes the chained-amplification / exponential-output vector a
    public caller could otherwise trigger via /api/v1/ling terms."""
    items = sorted(((s, t) for s, t in terms.items() if s),
                   key=lambda kv: (-len(kv[0]), kv[0].lower(), kv[0]))
    if not items:
        return text
    pattern = re.compile(
        "(?<!\\w)(" + "|".join(re.escape(s) for s, _ in items) + ")(?!\\w)",
        re.IGNORECASE,
    )
    # Case-only-different keys would silently overwrite each other in a plain
    # dict comprehension; the explicit sorted order + setdefault keeps the
    # winner deterministic instead of "whoever iterated last".
    table: dict[str, str] = {}
    for s, t in items:
        table.setdefault(s.lower(), t)
    out = pattern.sub(lambda m: table.get(m.group(0).lower(), m.group(0)), text)
    # refuse pathological expansion rather than OOM the worker
    if len(out) > max_out or len(out) > len(text) * 4 + 4096:
        return text
    return out


def apply_terms(text: str, terms: dict[str, str]) -> str:
    """Заменяет термины src→tgt за один проход (длинное раньше короткого)."""
    if not terms:
        return text
    return _single_pass(text, terms, max_out=200_000)


def mask_terms(text: str, terms: dict[str, str]) -> tuple[str, dict[str, str]]:
    """Маскирует термины плейсхолдерами ⟦k⟧ перед переводом, возвращает карту восстановления."""
    mapping: dict[str, str] = {}
    if not terms:
        return text, mapping
    ordered = sorted(((s, t) for s, t in terms.items() if s), key=lambda kv: -len(kv[0]))
    for i, (src, tgt) in enumerate(ordered):
        key = f"\u27e6T{i}\u27e7"
        pattern = re.compile("(?<!\\w)" + re.escape(src) + "(?!\\w)", re.IGNORECASE)
        if pattern.search(text):
            text = pattern.sub(lambda m, _k=key: _k, text)
            mapping[key] = tgt
    return text, mapping


def unmask_terms(text: str, mapping: dict[str, str]) -> str:
    for key, tgt in mapping.items():
        text = text.replace(key, tgt)
    return text
