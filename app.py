import os
import logging
from flask import Flask, send_from_directory
from werkzeug.middleware.proxy_fix import ProxyFix
from database import db
from models import User, TeamMember, PTORequest, Manager, Position, CallOutRecord, SMSRecipient
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.DEBUG)

# Create the app
app = Flask(__name__)
app.secret_key = os.environ.get("SESSION_SECRET", "default_secret_key_for_development")
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# Configure the database
database_url = os.environ.get("DATABASE_URL", "sqlite:///pto_tracker.db")
# Fix for some providers that use postgres:// instead of postgresql://
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = database_url
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "pool_recycle": 300,
    "pool_pre_ping": True,
}

# Initialize the app with the extension
db.init_app(app)

def run_migrations():
    """Add new columns to existing tables if they don't exist"""
    from sqlalchemy import text, inspect

    # List of migrations: (table, column, type, default)
    # Use None for `default` to skip the DEFAULT clause.
    migrations = [
        ('users', 'starting_pto_hours', 'NUMERIC(5,2)', '60.0'),
        ('users', 'starting_sick_hours', 'NUMERIC(5,2)', '60.0'),
        ('users', 'pto_blocked', 'BOOLEAN', 'FALSE'),
        ('pto_requests', 'callout_classification', 'VARCHAR(10)', None),
    ]

    try:
        inspector = inspect(db.engine)
    except Exception as e:
        print(f'Could not inspect database: {e}')
        return

    # Cache column lookups per table
    columns_by_table = {}
    def cols_for(table_name):
        if table_name not in columns_by_table:
            try:
                columns_by_table[table_name] = {c['name'] for c in inspector.get_columns(table_name)}
            except Exception:
                columns_by_table[table_name] = set()
        return columns_by_table[table_name]

    for table, column, col_type, default in migrations:
        if column in cols_for(table):
            print(f'Column {column} already exists in {table}')
            continue

        default_clause = f' DEFAULT {default}' if default is not None else ''
        sql = f'ALTER TABLE {table} ADD COLUMN {column} {col_type}{default_clause}'
        try:
            db.session.execute(text(sql))
            db.session.commit()
            print(f'Added column {column} to {table}')
            # Invalidate cache for this table so later migrations see the new column
            columns_by_table.pop(table, None)
        except Exception as e:
            db.session.rollback()
            print(f'Could not add column {column} to {table}: {e}')


def backfill_callout_classifications():
    """Classify any existing call-outs that don't have a classification yet."""
    from call_out_classifier import classify_call_out

    try:
        unclassified = PTORequest.query.filter(
            PTORequest.is_call_out == True,  # noqa: E712
            PTORequest.callout_classification.is_(None)
        ).all()
    except Exception as e:
        print(f'Could not query for unclassified call-outs: {e}')
        return

    if not unclassified:
        return

    count = 0
    for req in unclassified:
        # Prefer the SMS message text from CallOutRecord; fall back to PTORequest.reason.
        call_out_record = CallOutRecord.query.filter_by(pto_request_id=req.id).first()
        text = None
        if call_out_record and call_out_record.message_text:
            text = call_out_record.message_text
        elif req.reason:
            text = req.reason

        req.callout_classification = classify_call_out(text)
        count += 1

    try:
        db.session.commit()
        print(f'Backfilled callout_classification for {count} call-out(s)')
    except Exception as e:
        db.session.rollback()
        print(f'Could not backfill call-out classifications: {e}')

def migrate_admin_positions():
    """Migrate old admin positions (Front Desk, CT Desk, etc.) to Secretary II"""
    # Get Secretary II position
    secretary_pos = Position.query.filter_by(name='Secretary II', team='admin').first()
    if not secretary_pos:
        print('Secretary II position not found, skipping migration')
        return

    # Old admin positions to migrate
    old_positions = ['Front Desk/Admin', 'CT Desk', 'Front Desk Coordinator', 'Admin Assistant']

    for old_name in old_positions:
        old_pos = Position.query.filter_by(name=old_name).first()
        if old_pos:
            # Update all team members with this position to Secretary II
            members_updated = TeamMember.query.filter_by(position_id=old_pos.id).update(
                {'position_id': secretary_pos.id}
            )
            if members_updated > 0:
                print(f'Migrated {members_updated} employees from {old_name} to Secretary II')

            # Delete the old position
            db.session.delete(old_pos)
            print(f'Deleted old position: {old_name}')

    db.session.commit()

def seed_sms_recipients_from_env():
    """Seed the SMS recipients table from env vars on first deploy, then
    run a one-time reconciliation to set the correct state:
    - Stephen Handzel, Ashley Stark, and Lauryn Padron → team 'both'
    - Remove 6465565559 and Sukhjeet Dhaliwal entirely."""
    excluded_digits = {'6465565559'}

    def _digits(s):
        return ''.join(ch for ch in (s or '') if ch.isdigit())

    try:
        count = SMSRecipient.query.count()
    except Exception as e:
        print(f'Could not check sms_recipients: {e}')
        return

    # --- First-time seed from env vars ---
    if count == 0:
        admin_raw = os.environ.get('MANAGER_ADMIN_SMS', '')
        clinical_raw = os.environ.get('MANAGER_CLINICAL_SMS', '')

        seen_digits = set()
        entries = []
        for phone in [p.strip() for p in admin_raw.split(',') if p.strip()]:
            d = _digits(phone)
            if d in excluded_digits or d in seen_digits:
                continue
            seen_digits.add(d)
            entries.append(('Manager', phone, 'both'))
        for phone in [p.strip() for p in clinical_raw.split(',') if p.strip()]:
            d = _digits(phone)
            if d in excluded_digits or d in seen_digits:
                continue
            seen_digits.add(d)
            entries.append(('Manager', phone, 'both'))

        for name, phone, team in entries:
            normalized = SMSRecipient.normalize_phone(phone)
            db.session.add(SMSRecipient(name=name, phone=normalized, team=team, active=True))
            print(f'Seeded SMS recipient: {normalized} (both teams)')
        db.session.commit()

    # --- Reconciliation (runs every deploy until conditions are met) ---
    changed = False

    # Remove 6465565559
    for r in SMSRecipient.query.all():
        if _digits(r.phone) in excluded_digits:
            print(f'Removing excluded SMS recipient: {r.phone}')
            db.session.delete(r)
            changed = True

    # Remove Sukhjeet
    for r in SMSRecipient.query.filter(
        SMSRecipient.name.ilike('%sukhjeet%')
    ).all():
        print(f'Removing Sukhjeet: {r.phone}')
        db.session.delete(r)
        changed = True

    # Supervisors of the isolated teams must only receive call-outs for their
    # own team, never admin/clinical. Derive them from the manager role rather
    # than hardcoding phone numbers, and exclude them from the 'both' reset.
    # Phones are stored normalized (+1XXXXXXXXXX), so compare on trailing
    # digits: a bare 10-digit number never equals its 11-digit stored form.
    ISOLATED_ROLE_TEAMS = {
        'scribe_supervisor': ('scribes',),
        'research_supervisor': ('research',),
        'scribe_research_supervisor': ('scribes', 'research'),
    }

    isolated = []  # (name, normalized_phone, digits, teams)
    for mgr in Manager.query.filter(
        Manager.role.in_(ISOLATED_ROLE_TEAMS.keys())
    ).all():
        if not _digits(mgr.phone):
            print(f'Skipping {mgr.name} ({mgr.role}): no phone on record')
            continue
        phone = SMSRecipient.normalize_phone(mgr.phone)
        isolated.append((mgr.name, phone, _digits(phone), ISOLATED_ROLE_TEAMS[mgr.role]))

    def _is_isolated(phone):
        d = _digits(phone)
        return any(d.endswith(sd[-10:]) for _, _, sd, _ in isolated)

    for name, phone, sup_digits, teams in isolated:
        rows = [
            r for r in SMSRecipient.query.all()
            if _digits(r.phone).endswith(sup_digits[-10:])
        ]
        # Keep at most one row per allowed team; drop off-team rows and dupes.
        keep = {}
        for r in rows:
            if r.team in teams and r.team not in keep:
                keep[r.team] = r
                continue
            print(f'Removing off-team/duplicate SMS row for {r.name}: team={r.team}')
            db.session.delete(r)
            changed = True
        for team in teams:
            if team not in keep:
                print(f'Adding {team} SMS row for {name}')
                db.session.add(SMSRecipient(name=name, phone=phone, team=team, active=True))
                changed = True
            elif not keep[team].active:
                print(f'Reactivating {team} SMS row for {name}')
                keep[team].active = True
                changed = True

    # Ensure everyone else gets both teams (admin + clinical)
    for r in SMSRecipient.query.filter(SMSRecipient.team != 'both').all():
        if _is_isolated(r.phone):
            continue
        print(f'Setting {r.name} ({r.phone}) to both teams')
        r.team = 'both'
        changed = True

    if changed:
        db.session.commit()


def initialize_database():
    with app.app_context():
        # Create tables if they don't exist (but don't drop existing data)
        # WARNING: Only use db.drop_all() during development if you need to reset schema
        # db.drop_all()  # COMMENTED OUT - Enable ONLY to update schema during development
        db.create_all()

        # Run migrations to add new columns to existing tables
        run_migrations()

        # Initialize positions if they don't exist
        positions_to_create = [
            {'name': 'RNs', 'team': 'clinical'},
            {'name': 'CVI MOAs', 'team': 'clinical'},
            {'name': 'Echo Techs', 'team': 'clinical'},
            {'name': 'Secretary II', 'team': 'admin'},
            {'name': 'Leadership', 'team': 'admin'},
            {'name': 'Other', 'team': 'admin'},
        ]

        for pos_data in positions_to_create:
            existing_pos = Position.query.filter_by(name=pos_data['name']).first()
            if not existing_pos:
                new_pos = Position(name=pos_data['name'], team=pos_data['team'])
                db.session.add(new_pos)
        db.session.commit()

        # Migrate old admin positions to Secretary II
        migrate_admin_positions()

        # Migrate deprecated positions to their correct names
        position_renames = {
            'CVI Echo Techs': 'Echo Techs',
            'CVI RNs': 'RNs',
        }
        for old_name, new_name in position_renames.items():
            old_pos = Position.query.filter_by(name=old_name).first()
            if old_pos:
                new_pos = Position.query.filter_by(name=new_name).first()
                if new_pos:
                    # Move employees to the existing correct position
                    moved = TeamMember.query.filter_by(position_id=old_pos.id).update({'position_id': new_pos.id})
                    db.session.delete(old_pos)
                    print(f'Migrated {moved} employees from {old_name} to {new_name}')
                else:
                    # Just rename the position
                    old_pos.name = new_name
                    print(f'Renamed position {old_name} to {new_name}')

        # Remove APP position and its employees
        app_pos = Position.query.filter_by(name='APP').first()
        if app_pos:
            members = TeamMember.query.filter_by(position_id=app_pos.id).all()
            for member in members:
                PTORequest.query.filter_by(member_id=member.id).delete()
                db.session.delete(member)
            db.session.delete(app_pos)
            print(f'Removed APP position ({len(members)} employees)')
        db.session.commit()

        # Initialize default managers in Manager table
        from werkzeug.security import generate_password_hash

        managers_to_create = [
            {
                'name': 'Lauryn Padron',
                'email': 'lauryn.padron@mountsinai.org',
                'role': 'superadmin',
                'password': 'Carlostylermila5!'
            },
            {
                'name': 'Ashley Stark',
                'email': 'ashley.stark@mountsinai.org',
                'role': 'clinical',
                'password': 'Heart123!'
            },
            {
                'name': 'Samantha Zakow',
                'email': 'htn.prevention@mountsinai.org',
                'role': 'superadmin',
                'password': 'Password123'
            },
            {
                'name': 'Darline Saint-Victor',
                'email': 'darline.saintvictor@mountsinai.org',
                'role': 'clinical',
                'password': 'password#1'
            },
            {
                'name': 'Stephen Handzel',
                'email': 'Stephen.Handzel@mountsinai.org',
                'role': 'clinical',
                'password': 'password#1'
            },
            {
                'name': 'Samantha Mason',
                'email': 'samantha.mason@mountsinai.org',
                'role': 'admin',
                'password': 'password#1'
            }
        ]

        for manager_data in managers_to_create:
            # Check if manager already exists by email
            existing_manager = Manager.query.filter_by(email=manager_data['email']).first()

            if existing_manager:
                # Update existing manager
                existing_manager.name = manager_data['name']
                existing_manager.password_hash = generate_password_hash(manager_data['password'])
                existing_manager.role = manager_data['role']
                print(f"Updated Manager {manager_data['name']}")
            else:
                # Check if email exists as any other user type
                existing_user = User.query.filter_by(email=manager_data['email']).first()
                if existing_user:
                    print(f"Warning: Cannot create manager - email {manager_data['email']} already in use")
                    continue

                # Create new manager
                new_manager = Manager(
                    name=manager_data['name'],
                    email=manager_data['email'],
                    role=manager_data['role'],
                    password_hash=generate_password_hash(manager_data['password'])
                )
                db.session.add(new_manager)
                print(f"Created Manager {manager_data['name']} as {manager_data['role']}")

        db.session.commit()

        # Clean up any non-Mount Sinai test accounts on every deploy
        non_ms_members = TeamMember.query.filter(~TeamMember.email.ilike('%@mountsinai.org')).all()
        if non_ms_members:
            for m in non_ms_members:
                PTORequest.query.filter_by(member_id=m.id).delete()
                db.session.delete(m)
            print(f"Cleaned up {len(non_ms_members)} non-Mount Sinai test accounts")

        non_ms_managers = Manager.query.filter(~Manager.email.ilike('%@mountsinai.org')).all()
        if non_ms_managers:
            for m in non_ms_managers:
                db.session.delete(m)
            print(f"Cleaned up {len(non_ms_managers)} non-Mount Sinai manager accounts")

        # Hard delete Sukhjeet Dhaliwal: null out FK references then drop the row.
        # Idempotent — no-op once the row is gone.
        sukhjeet = Manager.query.filter(Manager.email.ilike('Sukhjeet.Dhaliwal@mountsinai.org')).first()
        if sukhjeet:
            from models import TardinessRecord
            TardinessRecord.query.filter_by(recorded_by_id=sukhjeet.id).update({'recorded_by_id': None})
            db.session.delete(sukhjeet)
            print(f"Hard-deleted Manager Sukhjeet Dhaliwal (id={sukhjeet.id})")

        db.session.commit()

        # Classify any call-outs missing a classification (runs once on first deploy,
        # then no-ops on subsequent restarts since rows already have a value).
        backfill_callout_classifications()

        # Seed SMS recipients from env vars on first deploy of the new system.
        # After this runs once, managing recipients is done entirely via the /sms-recipients UI.
        seed_sms_recipients_from_env()

        print("Database initialization complete")

# Import and register routes
from routes_simple import register_routes
register_routes(app)

# Register Twilio routes for call-out feature
from routes_twilio import register_twilio_routes
register_twilio_routes(app)

# Register scheduled-job routes (daily summary email)
from routes_cron import register_cron_routes
register_cron_routes(app)

with app.app_context():
    initialize_database()

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5000, debug=True)

@app.route('/static/<path:path>')
def send_static(path):
    return send_from_directory('static', path)