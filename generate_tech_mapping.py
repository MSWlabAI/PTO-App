"""Generate SQL to populate echo_techs.external_tech_id from PTO-App.

Reads the PTO-App Postgres (DATABASE_URL from .env.local), finds every
TeamMember whose Position name contains "Echo", and prints SQL UPDATE
statements you can paste into the Supabase SQL editor.

Matching is by LOWER(TRIM(name)). After running the UPDATEs, run the
verification SELECT this script prints and review any echo_techs rows
that still have NULL external_tech_id.

Usage:
    python generate_tech_mapping.py            # prints UPDATE statements
    python generate_tech_mapping.py --csv      # prints id,name CSV instead
"""

import os
import sys

from dotenv import load_dotenv
from sqlalchemy import create_engine, text


def main():
    load_dotenv(".env.local")
    load_dotenv(".env")

    db_url = os.environ.get("DATABASE_URL_UNPOOLED") or os.environ.get("DATABASE_URL")
    if not db_url:
        sys.exit("DATABASE_URL not set in environment or .env.local")
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)

    engine = create_engine(db_url)
    query = text(
        """
        SELECT tm.id, u.name, u.email, p.name AS position_name
        FROM team_members tm
        JOIN users u ON u.id = tm.id
        JOIN positions p ON p.id = tm.position_id
        WHERE p.name ILIKE '%Echo%'
        ORDER BY u.name
        """
    )

    with engine.connect() as conn:
        rows = list(conn.execute(query))

    if not rows:
        sys.exit("No Echo TeamMembers found in PTO-App.")

    as_csv = "--csv" in sys.argv
    if as_csv:
        print("id,name,email,position")
        for r in rows:
            print(f"{r.id},{r.name},{r.email},{r.position_name}")
        return

    print("-- Generated from PTO-App. Paste into Supabase SQL editor.")
    print(f"-- {len(rows)} Echo TeamMembers found.\n")
    for r in rows:
        safe_name = (r.name or "").replace("'", "''")
        print(
            f"UPDATE echo_techs SET external_tech_id = '{r.id}' "
            f"WHERE LOWER(TRIM(name)) = LOWER(TRIM('{safe_name}'));  "
            f"-- {r.position_name}"
        )

    print("\n-- Verification: any row with NULL external_tech_id needs manual mapping.")
    print("SELECT id, name, external_tech_id FROM echo_techs ORDER BY name;")


if __name__ == "__main__":
    main()
