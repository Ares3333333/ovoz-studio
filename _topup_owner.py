"""Owner convenience: top up the user's own Telegram account in the local dev DB."""
import sys

sys.path.insert(0, ".")
from app import billing, db  # noqa: E402

con = db.get_conn()
row = con.execute("SELECT id, name, contact FROM users WHERE contact LIKE 'tg:249741377'").fetchone()
if not row:
    print("USER_NOT_FOUND")
    raise SystemExit(1)
uid, name = row["id"], row["name"]
before = billing.balance(uid)
billing.credit(uid, 60.0, "owner:demo-topup")
print("TOPUP", name, "before=", before, "after=", billing.balance(uid))
