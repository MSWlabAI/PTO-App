"""Scheduled-job endpoints, invoked by Vercel cron (see vercel.json)."""

import logging
import os

from flask import jsonify, request

from database import db
from daily_summary import send_daily_summary
from email_service import EmailService

logger = logging.getLogger(__name__)


def _authorized():
    """Vercel sends `Authorization: Bearer $CRON_SECRET` when CRON_SECRET is set.

    If no secret is configured the endpoint stays closed rather than open, so a
    missing env var cannot expose a mail-sending route to the internet.
    """
    secret = os.environ.get('CRON_SECRET', '')
    if not secret:
        return False
    return request.headers.get('Authorization', '') == f'Bearer {secret}'


def register_cron_routes(app):

    @app.route('/api/cron/daily-summary', methods=['GET', 'POST'])
    def cron_daily_summary():
        if not _authorized():
            return jsonify({'error': 'unauthorized'}), 401

        # `force=1` bypasses the time/business-day gate for manual testing.
        force = request.args.get('force') == '1'
        try:
            result = send_daily_summary(db.session, EmailService(), force=force)
        except Exception as exc:
            logger.exception('Daily summary failed')
            return jsonify({'error': str(exc)}), 500

        logger.info(f'Daily summary result: {result}')
        return jsonify(result), 200
