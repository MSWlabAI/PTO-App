"""One-shot seed: Research position, promote Tzipporah Bergman to
combined scribe_research_supervisor role, and insert the 5 research
coordinators. Idempotent.

Usage:
    /usr/bin/python3 seed_research_team.py
"""

import os
import sys

from dotenv import load_dotenv
from sqlalchemy import create_engine, text


COMBINED_MANAGER_EMAIL = "Tzipporah.Bergman@mountsinai.org"
COMBINED_ROLE = "scribe_research_supervisor"

RESEARCH_EMPLOYEES = [
    ("Joslin Jose Plathottam", "JoslinJose.Plathottam@mountsinai.org", "+16822909592"),
    ("Noor Nouaili", "Noor.Nouaili@mountsinai.org", "+13104301519"),
    ("Mikayla Fuchs", "mikaylarfuchs@gmail.com", "+15167847857"),
    ("Ping Ting Yan", "PingTing.Yan@mountsinai.org", "+16179595288"),
    ("Fatmata Barry", "Fatmata.Barry@mountsinai.org", "+19176009387"),
]


def get_engine():
    load_dotenv(".env.local")
    load_dotenv(".env")
    db_url = os.environ.get("DATABASE_URL_UNPOOLED") or os.environ.get("DATABASE_URL")
    if not db_url:
        sys.exit("DATABASE_URL not set")
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    return create_engine(db_url)


def ensure_position(conn):
    row = conn.execute(
        text("SELECT id FROM positions WHERE name = :n AND team = :t"),
        {"n": "Research Coordinator", "t": "research"},
    ).first()
    if row:
        print(f"  position already exists: id={row.id}")
        return row.id
    new_id = conn.execute(
        text("INSERT INTO positions (name, team) VALUES (:n, :t) RETURNING id"),
        {"n": "Research Coordinator", "t": "research"},
    ).scalar()
    print(f"  position created: id={new_id}")
    return new_id


def promote_manager(conn):
    row = conn.execute(
        text(
            """
            SELECT m.id, m.role, u.name
            FROM managers m JOIN users u ON u.id = m.id
            WHERE u.email = :e
            """
        ),
        {"e": COMBINED_MANAGER_EMAIL},
    ).first()
    if not row:
        sys.exit(
            f"Expected existing manager {COMBINED_MANAGER_EMAIL} (scribes setup). "
            "Run seed_scribes_team.py first."
        )
    if row.role == COMBINED_ROLE:
        print(f"  manager already has combined role (id={row.id})")
        return row.id
    conn.execute(
        text("UPDATE managers SET role = :r WHERE id = :id"),
        {"r": COMBINED_ROLE, "id": row.id},
    )
    print(f"  manager role changed: {row.name} {row.role} → {COMBINED_ROLE} (id={row.id})")
    return row.id


def ensure_user(conn, name, email, phone):
    row = conn.execute(
        text("SELECT id FROM users WHERE email = :e"), {"e": email}
    ).first()
    if row:
        return row.id, False
    new_id = conn.execute(
        text(
            "INSERT INTO users (name, email, phone) VALUES (:n, :e, :p) RETURNING id"
        ),
        {"n": name, "e": email, "p": phone},
    ).scalar()
    return new_id, True


def ensure_employees(conn, position_id):
    created = 0
    skipped = 0
    for name, email, phone in RESEARCH_EMPLOYEES:
        user_id, _ = ensure_user(conn, name, email, phone)
        if conn.execute(
            text("SELECT id FROM team_members WHERE id = :id"), {"id": user_id}
        ).first():
            skipped += 1
            continue
        conn.execute(
            text("INSERT INTO team_members (id, position_id) VALUES (:id, :pid)"),
            {"id": user_id, "pid": position_id},
        )
        created += 1
    print(f"  employees created: {created}, skipped (already existed): {skipped}")


def main():
    engine = get_engine()
    with engine.begin() as conn:
        print("== Research seed ==")
        print("Step 1: Position")
        position_id = ensure_position(conn)
        print("Step 2: Promote manager")
        promote_manager(conn)
        print("Step 3: Employees")
        ensure_employees(conn, position_id)
        print("== Done ==")


if __name__ == "__main__":
    main()
