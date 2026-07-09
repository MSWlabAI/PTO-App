"""Read-only check: print the sms_recipients table and flag Bergman's rows.
Uses the same DB config as the seed scripts. Makes no changes.

Usage:
    python3 verify_bergman_sms.py
"""

import os
import sys

from dotenv import load_dotenv
from sqlalchemy import create_engine, text


def get_engine():
    load_dotenv(".env.local")
    load_dotenv(".env")
    db_url = os.environ.get("DATABASE_URL_UNPOOLED") or os.environ.get("DATABASE_URL")
    if not db_url:
        sys.exit("DATABASE_URL not set")
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    return create_engine(db_url)


def main():
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT name, phone, team, active FROM sms_recipients ORDER BY team, name")
        ).all()

    print(f"{'NAME':<24} {'PHONE':<16} {'TEAM':<10} ACTIVE")
    print("-" * 62)
    for name, phone, team, active in rows:
        digits = "".join(c for c in (phone or "") if c.isdigit())
        flag = "  <-- BERGMAN" if digits.endswith("9296975997") else ""
        print(f"{(name or ''):<24} {(phone or ''):<16} {(team or ''):<10} {active}{flag}")
    print(f"\nTotal recipients: {len(rows)}")

    bergman = [r for r in rows if "".join(c for c in (r[1] or '') if c.isdigit()).endswith("9296975997")]
    bteams = sorted(t for _, _, t, _ in bergman)
    print(f"Bergman teams: {bteams or '(none — not in table)'}")
    if bteams == ["research", "scribes"]:
        print("RESULT: ✅ correctly scoped to scribes + research only")
    elif any(t in ("both", "admin", "clinical") for t in bteams):
        print("RESULT: ❌ still has admin/clinical/both — not fixed")
    else:
        print("RESULT: ⚠️  unexpected state, review above")


if __name__ == "__main__":
    main()
