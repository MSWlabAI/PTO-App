"""Morning summary of today's call-outs and who is on PTO.

Covers the admin and clinical teams only; scribes and research are isolated
teams and are reported to their own supervisor, not in this digest.

Triggered by a Vercel cron job (see vercel.json) which fires at both 12:30
and 13:30 UTC so that exactly one of them lands at 8:30 AM Eastern on either
side of daylight saving. should_send_now() gates on Eastern wall-clock time.
"""

import logging
import os
from datetime import date, datetime

import pytz

from business_days import BusinessDaysCalculator
from models import PTORequest, TeamMember

logger = logging.getLogger(__name__)

EASTERN = pytz.timezone('US/Eastern')

SUMMARY_TEAMS = ('admin', 'clinical')

# A request counts as "out today" once a manager has approved it. Denied and
# still-pending requests are not time off yet.
ON_PTO_STATUSES = ('approved', 'in_progress', 'completed')

# A call-out is real unless it was denied outright.
CALL_OUT_EXCLUDED_STATUSES = ('denied',)

SEND_HOUR_EASTERN = 8
SEND_MINUTE_EASTERN = 30


def eastern_now():
    return datetime.now(EASTERN)


def should_send_now(now=None):
    """True only on a business day, in the 8:30 AM Eastern hour.

    Two UTC cron entries fire each day; the one that is not 8:30 Eastern is a
    no-op. Holidays and weekends are skipped via BusinessDaysCalculator.
    """
    now = now or eastern_now()
    if not BusinessDaysCalculator.is_business_day(now.date()):
        return False
    if now.hour != SEND_HOUR_EASTERN:
        return False
    # Cron delivery can drift a few minutes; accept anything from :30 on.
    return now.minute >= SEND_MINUTE_EASTERN - 15


def _describe_dates(req):
    if req.start_date == req.end_date:
        if req.is_partial_day and req.start_time and req.end_time:
            return f'{req.start_date} ({req.start_time}–{req.end_time})'
        return req.start_date
    return f'{req.start_date} → {req.end_date}'


def collect_summary(session, today=None):
    """Gather today's call-outs and today's PTO for admin + clinical.

    Takes a session rather than importing the Flask app so this can be run
    read-only without triggering app startup side effects.
    """
    today = today or eastern_now().date()
    today_str = today.isoformat()

    # TeamMember inherits User (joined-table), so its .name comes from users.
    base = (
        session.query(PTORequest, TeamMember.name)
        .join(TeamMember, TeamMember.id == PTORequest.member_id)
        .filter(PTORequest.manager_team.in_(SUMMARY_TEAMS))
    )

    call_outs = [
        {'name': name, 'team': r.manager_team, 'request': r,
         'classification': r.callout_classification or 'sick'}
        for r, name in base.filter(
            PTORequest.is_call_out.is_(True),
            PTORequest.start_date == today_str,
            PTORequest.status.notin_(CALL_OUT_EXCLUDED_STATUSES),
        ).all()
    ]

    # Dates are stored as YYYY-MM-DD strings, which compare correctly.
    on_pto = [
        {'name': name, 'team': r.manager_team, 'request': r,
         'pto_type': r.pto_type}
        for r, name in base.filter(
            PTORequest.is_call_out.isnot(True),
            PTORequest.status.in_(ON_PTO_STATUSES),
            PTORequest.start_date <= today_str,
            PTORequest.end_date >= today_str,
        ).all()
    ]

    call_outs.sort(key=lambda e: (e['team'], e['name']))
    on_pto.sort(key=lambda e: (e['team'], e['name']))
    return {'date': today, 'call_outs': call_outs, 'on_pto': on_pto}


def _rows_html(entries, kind):
    if not entries:
        return '<tr><td colspan="3" style="padding:8px;color:#666;">None</td></tr>'
    rows = []
    for e in entries:
        detail = (e['classification'].upper() if kind == 'call_out'
                  else e['pto_type'])
        rows.append(
            '<tr>'
            f'<td style="padding:8px;border-top:1px solid #eee;">{e["name"]}</td>'
            f'<td style="padding:8px;border-top:1px solid #eee;">{e["team"].title()}</td>'
            f'<td style="padding:8px;border-top:1px solid #eee;">{detail} '
            f'&middot; {_describe_dates(e["request"])}</td>'
            '</tr>'
        )
    return ''.join(rows)


def render_html(summary):
    pretty = summary['date'].strftime('%A, %B %-d, %Y')
    return f'''<html><body style="font-family:Arial,sans-serif;color:#222;">
  <h2 style="margin-bottom:4px;">Daily Summary &mdash; {pretty}</h2>
  <p style="color:#666;margin-top:0;">Admin &amp; Clinical teams</p>

  <h3>Called out today ({len(summary['call_outs'])})</h3>
  <table style="border-collapse:collapse;width:100%;max-width:640px;">
    {_rows_html(summary['call_outs'], 'call_out')}
  </table>

  <h3>On PTO today ({len(summary['on_pto'])})</h3>
  <table style="border-collapse:collapse;width:100%;max-width:640px;">
    {_rows_html(summary['on_pto'], 'pto')}
  </table>

  <p style="color:#888;font-size:12px;margin-top:24px;">
    Automated summary from the MSW CVI PTO Tracker.
  </p>
</body></html>'''


def render_text(summary):
    lines = [f'Daily Summary - {summary["date"].isoformat()} (Admin & Clinical)', '']
    for title, entries, kind in (
        ('Called out today', summary['call_outs'], 'call_out'),
        ('On PTO today', summary['on_pto'], 'pto'),
    ):
        lines.append(f'{title} ({len(entries)}):')
        if not entries:
            lines.append('  None')
        for e in entries:
            detail = (e['classification'].upper() if kind == 'call_out'
                      else e['pto_type'])
            lines.append(
                f'  - {e["name"]} ({e["team"]}) - {detail} - {_describe_dates(e["request"])}'
            )
        lines.append('')
    return '\n'.join(lines)


def send_daily_summary(session, email_service, force=False):
    """Build and send the summary. Returns a result dict for the cron route."""
    if not force and not should_send_now():
        return {'sent': False, 'reason': 'outside send window'}

    summary = collect_summary(session)
    recipients = [
        e for e in {email_service.admin_email, email_service.clinical_email} if e
    ]
    if not recipients:
        logger.error('Daily summary: no recipients configured')
        return {'sent': False, 'reason': 'no recipients'}

    subject = (
        f'Daily Summary {summary["date"].isoformat()}: '
        f'{len(summary["call_outs"])} called out, {len(summary["on_pto"])} on PTO'
    )
    html, text = render_html(summary), render_text(summary)

    delivered = []
    for to_email in recipients:
        if email_service.send_email(to_email, subject, body_html=html, body_text=text):
            delivered.append(to_email)

    return {
        'sent': bool(delivered),
        'date': summary['date'].isoformat(),
        'recipients': delivered,
        'call_outs': len(summary['call_outs']),
        'on_pto': len(summary['on_pto']),
    }
