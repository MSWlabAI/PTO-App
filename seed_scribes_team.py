"""One-shot seed: scribes Position, scribe_supervisor Manager, and the
initial 11 scribe employees. Safe to re-run — skips rows that already exist
by their natural key.

Uses raw SQLAlchemy + DATABASE_URL from .env.local; no Flask context required.

Usage:
    /usr/bin/python3 seed_scribes_team.py
"""

import os
import sys

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from werkzeug.security import generate_password_hash


SCRIBE_MANAGER = {
    "name": "Tzipporah Bergman",
    "email": "Tzipporah.Bergman@mountsinai.org",
    "phone": "+19296975997",
    "role": "scribe_supervisor",
    "initial_password": "Password1",
}

SCRIBE_EMPLOYEES = [
    ("Hillary Okyere", "Hillary.Okyere@mountsinai.org", "+13474952411"),
    ("Sooyong Lee", "Sooyong.Lee@mountsinai.org", "+12148086493"),
    ("Zoe Ozols", "Zoe.Ozols@mountsinai.org", "+19146026354"),
    ("Elizabeth Kershteyn", "Elizabeth.Kershteyn@mountsinai.org", "+19176152029"),
    ("Amaya Rushie", "Amaya.Rushie@mountsinai.org", "+19144132662"),
    ("Brian Asandi", "Brian.Asnadi@mountsinai.org", "+15168506435"),
    ("Sadia Saddika", "Sadia.Saddika@mountsinai.org", "+19296132123"),
    ("Fatima Bogam", "Fatima.Bagom@mountsinai.org", "+19174205666"),
    ("Joanna Kim", "joanna.kim@icahn.mssm.edu", "+17632420316"),
    ("Dan Yun", "dky2011@nyu.edu", "+12674102966"),
    ("Lorena Sinclair", "Lorena.Sinclair@mountsinai.org", "+13475447459"),
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
        {"n": "Scribe", "t": "scribes"},
    ).first()
    if row:
        print(f"  position already exists: id={row.id}")
        return row.id
    new_id = conn.execute(
        text("INSERT INTO positions (name, team) VALUES (:n, :t) RETURNING id"),
        {"n": "Scribe", "t": "scribes"},
    ).scalar()
    print(f"  position created: id={new_id}")
    return new_id


def ensure_user(conn, name, email, phone):
    """Idempotent user insert. Returns (id, was_created)."""
    row = conn.execute(
        text("SELECT id FROM users WHERE email = :e"), {"e": email}
    ).first()
    if row:
        return row.id, False
    new_id = conn.execute(
        text(
            """
            INSERT INTO users (name, email, phone)
            VALUES (:name, :email, :phone)
            RETURNING id
            """
        ),
        {"name": name, "email": email, "phone": phone},
    ).scalar()
    return new_id, True


def ensure_manager(conn):
    user_id, created = ensure_user(
        conn,
        SCRIBE_MANAGER["name"],
        SCRIBE_MANAGER["email"],
        SCRIBE_MANAGER["phone"],
    )
    # Is this user already in managers?
    row = conn.execute(
        text("SELECT id, role FROM managers WHERE id = :id"), {"id": user_id}
    ).first()
    if row:
        if row.role != SCRIBE_MANAGER["role"]:
            conn.execute(
                text("UPDATE managers SET role = :r WHERE id = :id"),
                {"r": SCRIBE_MANAGER["role"], "id": user_id},
            )
            print(f"  manager role updated to {SCRIBE_MANAGER['role']} (id={user_id})")
        else:
            print(f"  manager already exists: id={user_id}, role={row.role}")
        return user_id
    pwd_hash = generate_password_hash(
        SCRIBE_MANAGER["initial_password"], method="pbkdf2:sha256"
    )
    conn.execute(
        text(
            """
            INSERT INTO managers (id, role, password_hash)
            VALUES (:id, :role, :hash)
            """
        ),
        {"id": user_id, "role": SCRIBE_MANAGER["role"], "hash": pwd_hash},
    )
    print(f"  manager created: id={user_id} ({'new user' if created else 'existing user promoted'})")
    return user_id


def ensure_employees(conn, position_id):
    created = 0
    skipped = 0
    for name, email, phone in SCRIBE_EMPLOYEES:
        user_id, was_new = ensure_user(conn, name, email, phone)
        if conn.execute(
            text("SELECT id FROM team_members WHERE id = :id"), {"id": user_id}
        ).first():
            skipped += 1
            continue
        conn.execute(
            text(
                """
                INSERT INTO team_members (id, position_id)
                VALUES (:id, :pid)
                """
            ),
            {"id": user_id, "pid": position_id},
        )
        created += 1
    print(f"  employees created: {created}, skipped (already existed): {skipped}")


def main():
    engine = get_engine()
    with engine.begin() as conn:
        print("== Scribes seed ==")
        print("Step 1: Position")
        position_id = ensure_position(conn)
        print("Step 2: Manager")
        ensure_manager(conn)
        print("Step 3: Employees")
        ensure_employees(conn, position_id)
        print("== Done ==")


if __name__ == "__main__":
    main()
