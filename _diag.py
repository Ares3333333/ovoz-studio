import json
import re
import sqlite3
import sys
from pathlib import Path

out = []
db = sqlite3.connect("file:data/ovoz.db?mode=ro", uri=True, timeout=5)
db.row_factory = sqlite3.Row
out.append("== recent jobs ==")
for r in db.execute("SELECT id,user_id,type,status,minutes,substr(coalesce(error,''),1,90) err,"
                    " created_at,updated_at FROM jobs ORDER BY rowid DESC LIMIT 10"):
    out.append(json.dumps(dict(r), ensure_ascii=False))
out.append("== users seen today ==")
for r in db.execute("SELECT id,substr(contact,1,30) c,name,plan,created_at FROM users"
                    " ORDER BY rowid DESC LIMIT 6"):
    out.append(json.dumps(dict(r), ensure_ascii=False))

for logname in ("_go37_err.log", "_go37_out.log"):
    p = Path(logname)
    if not p.exists():
        continue
    out.append(f"== {logname}: requests/errors (last 40) ==")
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    hits = [ln for ln in lines if re.search(r'"(POST|GET|DELETE) |Traceback|Error|error', ln)]
    out.extend(hits[-40:])

Path("_diag.txt").write_text("\n".join(out), encoding="utf-8")
print("DIAG_WRITTEN", len(out))
