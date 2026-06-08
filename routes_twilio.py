"""
Twilio Webhook Routes for SMS Call-Out Feature
Handles incoming SMS messages from Twilio
"""

from flask import request
from twilio_service import TwilioSMSService
from email_service import EmailService
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def register_twilio_routes(app):
    """Register Twilio SMS webhook routes with the Flask app"""

    # Initialize services
    sms_service = TwilioSMSService()
    email_service = EmailService()

    # ========================================
    # SMS ROUTES
    # ========================================

    @app.route('/twilio/sms/incoming', methods=['POST'])
    def twilio_sms_incoming():
        """
        Handle incoming SMS messages for call-outs
        Twilio calls this webhook when someone texts the call-out line
        """
        logger.info("Incoming SMS received")

        # Get SMS information from Twilio request
        from_number = request.form.get('From', '')
        message_body = request.form.get('Body', '')
        message_sid = request.form.get('MessageSid', '')

        logger.info(f"SMS from: {from_number}")
        logger.info(f"Message: {message_body}")
        logger.info(f"SID: {message_sid}")

        try:
            # Authenticate sender by phone number
            authenticated, member = sms_service.authenticate_sender(from_number)

            if not authenticated:
                logger.warning(f"SMS authentication failed for {from_number}")
                twiml_response = sms_service.generate_sms_response(
                    authenticated=False
                )
                return twiml_response, 200, {'Content-Type': 'text/xml'}

            # Create PTO request and CallOutRecord
            pto_request = sms_service.create_call_out_request(
                member=member,
                message_sid=message_sid,
                message_body=message_body,
                from_number=from_number
            )

            # Send email notification to manager
            try:
                email_service.send_submission_email(pto_request)
            except Exception as e:
                logger.error(f"Failed to send email notification: {str(e)}")

            # Send SMS confirmation to employee using Twilio API
            sms_service.send_employee_confirmation_sms(
                to_number=from_number,
                employee_name=member.name,
                request_id=pto_request.id
            )

            # Look up active SMS recipients for this employee's team from the DB
            from models import SMSRecipient
            manager_team = member.position.team if member.position else None
            recipients = []
            if manager_team in ('admin', 'clinical', 'scribes'):
                # Scribes are intentionally isolated: 'both' targets only the
                # admin+clinical pair, not scribes.
                team_filter = [manager_team] if manager_team == 'scribes' else [manager_team, 'both']
                recipients = SMSRecipient.query.filter(
                    SMSRecipient.active == True,
                    SMSRecipient.team.in_(team_filter)
                ).all()

            logger.info(
                f"Manager team: {manager_team}, "
                f"recipients: {[r.phone for r in recipients]}"
            )

            for recipient in recipients:
                sms_service.send_manager_notification_sms(
                    recipient.phone,
                    member.name,
                    pto_request.id
                )

            # Generate empty TwiML response (SMS already sent via API)
            twiml_response = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'

            return twiml_response, 200, {'Content-Type': 'text/xml'}

        except Exception as e:
            logger.error(f"Error processing SMS: {str(e)}")
            twiml_response = sms_service.generate_sms_response(
                authenticated=False
            )
            return twiml_response, 200, {'Content-Type': 'text/xml'}

    # ========================================
    # TEST/DEBUG ROUTES (optional)
    # ========================================

    @app.route('/twilio/test/sms')
    def test_sms_twiml():
        """Test endpoint to see what SMS response looks like"""
        twiml = sms_service.generate_sms_response(
            authenticated=True,
            member=type('obj', (object,), {'name': 'Test Employee'}),
            request_id=123
        )
        return f"<pre>{twiml}</pre>", 200

    logger.info("Twilio SMS routes registered successfully")
