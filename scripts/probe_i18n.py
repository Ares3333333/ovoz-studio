"""Throwaway probe: understand i18n.js shape before writing the real test."""
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parents[1]
src = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")

starts = [(m.group(1), m.end() - 1) for m in re.finditer(r"(?m)^\s*(uz|ru|en)\s*:\s*\{", src)]
print("locales found:", [s[0] for s in starts])


def body_at(text, open_idx):
    """Return the substring inside the object literal starting at open_idx (a '{')."""
    depth, i, in_str, esc_, q = 0, open_idx, False, False, ""
    while i < len(text):
        c = text[i]
        if in_str:
            if esc_:
                esc_ = False
            elif c == "\\":
                esc_ = True
            elif c == q:
                in_str = False
        else:
            if c in "\"'":
                in_str, q = True, c
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return text[open_idx + 1:i]
        i += 1
    raise AssertionError("unbalanced braces")


bounds = [(name, body_at(src, pos)) for name, pos in starts]

KEY_RE = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)\s*:\s*"((?:[^"\\]|\\.)*)"')
sets = {}
for name, body in bounds:
    pairs = KEY_RE.findall(body)
    sets[name] = dict(pairs)
    print(name, "keys:", len(pairs), "unique:", len(sets[name]), "chars:", len(body))

names = list(sets)
for a in names:
    for b in names:
        if a < b:
            only_a = set(sets[a]) - set(sets[b])
            only_b = set(sets[b]) - set(sets[a])
            if only_a or only_b:
                print("PARITY", a, "only:", sorted(only_a)[:10], b, "only:", sorted(only_b)[:10])
empty = {n: [k for k, v in sets[n].items() if not v.strip()] for n in names}
print("EMPTY:", empty)

html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
used = set()
for attr in ("data-i18n", "data-i18n-ph", "data-i18n-aria"):
    used |= set(re.findall(r'%s="([A-Za-z0-9_]+)"' % attr, html))
    used |= set(re.findall(r'%s="([A-Za-z0-9_]+)"' % attr, js))
tlit = set(re.findall(r'\bt\(\s*"([A-Za-z0-9_]+)"', js))
print("html/attr keys:", len(used), "t() literal keys:", len(tlit))
missing = sorted(k for k in used | tlit if k not in sets.get("uz", {}))
print("MISSING FROM uz:", missing)
dyn = re.findall(r'\bt\(\s*[^"\)]', js)
print("dynamic t() call sites:", len(dyn))
