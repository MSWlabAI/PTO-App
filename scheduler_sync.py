"""Outgoing sync from PTO app to the MSW Heart Scheduler.

Pushes approved/cancelled PTO requests for Echo techs to the scheduler so
they appear on its Echo grid. Sync is best-effort: failures are logged
and do not block the approval/deletion flow.
"""

import os
from datetime import datetime
from typing import List, Dict, Optional

import requests

from business_days import BusinessDaysCalculator


SYNC_TIMEOUT_SECONDS = 10
SYNC_PATH = "/api/echo-pto/sync"
DISCREPANCIES_PATH = "/api/echo-pto/discrepancies"


def is_echo_position(member) -> bool:
    """Only Echo techs exist in the scheduler today.

    The scheduler endpoint currently rejects any position_group other than
    'echo', so gating here avoids pointless 400s.
    """
    if not member or not getattr(member, "position", None):
        return False
    name = (member.position.name or "")
    return "Echo" in name


def _time_block_for_partial(start_time: str, end_time: str) -> str:
    """Map HH:MM start/end of a partial day to AM / PM / BOTH.

    Noon is the split. A window entirely <12:00 is AM, entirely >=12:00 is PM,
    anything that straddles noon is BOTH.
    """
    def to_minutes(t: str) -> int:
        h, m = t.split(":")
        return int(h) * 60 + int(m)

    try:
        start = to_minutes(start_time)
        end = to_minutes(end_time)
    except (ValueError, AttributeError):
        return "BOTH"

    noon = 12 * 60
    if end <= noon:
        return "AM"
    if start >= noon:
        return "PM"
    return "BOTH"


def build_sync_dates(pto_request) -> List[Dict[str, str]]:
    """Translate a PTORequest into the scheduler's [{date, time_block}] list.

    - Partial-day request: single entry, AM/PM/BOTH derived from times.
    - Multi-day full request: one BOTH entry per business day in range.
    """
    if pto_request.is_partial_day and pto_request.start_time and pto_request.end_time:
        block = _time_block_for_partial(pto_request.start_time, pto_request.end_time)
        return [{"date": pto_request.start_date, "time_block": block}]

    try:
        start = datetime.strptime(pto_request.start_date, "%Y-%m-%d").date()
        end = datetime.strptime(pto_request.end_date, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return []

    business_days = BusinessDaysCalculator.get_business_days_list(start, end)
    return [{"date": d.isoformat(), "time_block": "BOTH"} for d in business_days]


def sync_pto_request(pto_request, action: str) -> Optional[bool]:
    """POST a PTO request to the scheduler. Returns True on success, False on
    handled failure, None when sync was skipped (not configured, not an Echo
    tech, etc.).
    """
    if action not in ("upsert", "delete"):
        print(f"[scheduler_sync] invalid action: {action}")
        return False

    member = pto_request.member
    if not is_echo_position(member):
        return None

    base_url = os.environ.get("MSW_SCHEDULER_URL")
    secret = os.environ.get("PTO_SYNC_SECRET")
    if not base_url or not secret:
        print("[scheduler_sync] MSW_SCHEDULER_URL or PTO_SYNC_SECRET not set; skipping sync")
        return None

    dates = build_sync_dates(pto_request) if action == "upsert" else []
    if action == "upsert" and not dates:
        print(f"[scheduler_sync] no business days for request {pto_request.id}; skipping")
        return None

    payload = {
        "external_tech_id": str(member.id),
        "action": action,
        "source_request_id": str(pto_request.id),
        "position_group": "echo",
        "reason": pto_request.reason or pto_request.pto_type,
        # Endpoint requires non-empty dates even on delete; harmless on that path
        # since the server filters by source_request_id, but we send a minimal
        # placeholder to keep the contract uniform.
        "dates": dates or [{"date": pto_request.start_date, "time_block": "BOTH"}],
    }

    url = base_url.rstrip("/") + SYNC_PATH
    try:
        resp = requests.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {secret}"},
            timeout=SYNC_TIMEOUT_SECONDS,
        )
    except requests.RequestException as e:
        print(f"[scheduler_sync] network error syncing request {pto_request.id}: {e}")
        return False

    if resp.status_code >= 400:
        print(
            f"[scheduler_sync] {action} failed for request {pto_request.id}: "
            f"HTTP {resp.status_code} {resp.text[:300]}"
        )
        return False

    print(f"[scheduler_sync] {action} ok for request {pto_request.id} ({member.name})")
    return True


def _scheduler_request(path: str, method: str = "GET", json_body=None):
    """Internal helper for cross-app calls beyond the upsert/delete sync.
    Returns the parsed JSON body on success, None on configuration or network
    failure. Auth and timeout match sync_pto_request.
    """
    base_url = os.environ.get("MSW_SCHEDULER_URL")
    secret = os.environ.get("PTO_SYNC_SECRET")
    if not base_url or not secret:
        return None
    url = base_url.rstrip("/") + path
    try:
        resp = requests.request(
            method,
            url,
            json=json_body,
            headers={"Authorization": f"Bearer {secret}"},
            timeout=SYNC_TIMEOUT_SECONDS,
        )
    except requests.RequestException as e:
        print(f"[scheduler_sync] network error on {method} {path}: {e}")
        return None
    if resp.status_code >= 400:
        print(f"[scheduler_sync] {method} {path} HTTP {resp.status_code}: {resp.text[:300]}")
        return None
    try:
        return resp.json()
    except ValueError:
        return None


def fetch_open_discrepancies() -> List[Dict]:
    """Return open pto_sync_discrepancies from the scheduler. Empty list on
    any failure — the popup just stays hidden in that case.
    """
    body = _scheduler_request(DISCREPANCIES_PATH)
    if not body:
        return []
    return body.get("discrepancies") or []


def resolve_discrepancy(discrepancy_id: str) -> bool:
    """Ask the scheduler to mark one discrepancy resolved on behalf of the
    PTO-App user. Returns True on success.
    """
    if not discrepancy_id:
        return False
    body = _scheduler_request(
        f"{DISCREPANCIES_PATH}/{discrepancy_id}/resolve",
        method="POST",
        json_body={},
    )
    return body is not None and body.get("ok") is True
