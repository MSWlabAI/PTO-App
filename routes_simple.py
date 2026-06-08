from flask import render_template, request, redirect, url_for, flash, jsonify, session
from database import db
from models import PTORequest, TeamMember, Manager, User, PendingEmployee, Position, TardinessRecord, SMSRecipient
from pto_system import PTOTrackerSystem
from auth import roles_required, authenticate_user, login_user, logout_user, get_current_user
from datetime import datetime, timedelta
import pytz
from email_service import EmailService
import scheduler_sync

# Define Eastern timezone
EASTERN = pytz.timezone('US/Eastern')

def get_eastern_time():
    """Get current time in Eastern timezone as naive datetime"""
    eastern_now = datetime.now(EASTERN)
    # Return naive datetime (no timezone info) but in Eastern time
    return eastern_now.replace(tzinfo=None)

def _get_business_day_segments(start_str, end_str):
    """Split a date range into segments of consecutive business days (no weekends).
    Returns list of (segment_start, segment_end_exclusive) string tuples."""
    start = datetime.strptime(start_str, '%Y-%m-%d').date()
    end = datetime.strptime(end_str, '%Y-%m-%d').date()
    segments = []
    current_start = None
    d = start
    while d <= end:
        if d.weekday() < 5:  # Monday-Friday
            if current_start is None:
                current_start = d
        else:
            if current_start is not None:
                # End exclusive for FullCalendar
                segments.append((current_start.strftime('%Y-%m-%d'), (d).strftime('%Y-%m-%d')))
                current_start = None
        d += timedelta(days=1)
    # Close final segment
    if current_start is not None:
        segments.append((current_start.strftime('%Y-%m-%d'), (end + timedelta(days=1)).strftime('%Y-%m-%d')))
    return segments


def register_routes(app):
    # Initialize the PTO system
    pto_system = PTOTrackerSystem()
    # Initialize the email service
    email_service = EmailService()

    @app.route('/')
    def index():
        """Main page for submitting PTO requests"""
        staff_directory = pto_system.get_staff_directory()
        return render_template('index.html', staff_directory=staff_directory)

    @app.route('/user-manual')
    def user_manual():
        """Display the employee user manual"""
        import os
        import markdown
        manual_path = os.path.join(os.path.dirname(__file__), 'USER_MANUAL.md')
        with open(manual_path, 'r') as f:
            md_content = f.read()
        html_content = markdown.markdown(md_content, extensions=['tables', 'fenced_code'])
        return render_template('user_manual.html', content=html_content, title='Employee Manual')

    @app.route('/manager-manual')
    def manager_manual():
        """Display the manager user manual"""
        import os
        import markdown
        manual_path = os.path.join(os.path.dirname(__file__), 'MANAGER_MANUAL.md')
        with open(manual_path, 'r') as f:
            md_content = f.read()
        html_content = markdown.markdown(md_content, extensions=['tables', 'fenced_code'])
        return render_template('user_manual.html', content=html_content, title='Manager Manual')

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        """Manager login page"""
        if request.method == 'POST':
            email = request.form.get('email')
            password = request.form.get('password')

            user = authenticate_user(email, password)
            if user:
                login_user(user)
                flash('Login successful!', 'success')
                next_page = request.args.get('next')
                return redirect(next_page) if next_page else redirect(url_for('dashboard'))
            else:
                flash('Invalid email or password.', 'error')

        return render_template('login.html')

    # Inject open PTO-sync discrepancies into dashboard templates for the
    # three roles that should see and resolve them.
    DISCREPANCY_DASHBOARDS = {
        'clinical_dashboard',
        'superadmin_dashboard',
        'echo_supervisor_dashboard',
    }
    DISCREPANCY_ROLES = {'superadmin', 'echo_supervisor', 'clinical'}

    @app.context_processor
    def inject_pto_sync_discrepancies():
        if request.endpoint not in DISCREPANCY_DASHBOARDS:
            return {}
        if session.get('user_role') not in DISCREPANCY_ROLES:
            return {}
        try:
            return {'pto_sync_discrepancies': scheduler_sync.fetch_open_discrepancies()}
        except Exception as e:
            print(f"[scheduler_sync] context processor failed: {e}")
            return {'pto_sync_discrepancies': []}

    @app.route('/resolve_discrepancy/<discrepancy_id>', methods=['POST'])
    @roles_required('superadmin', 'echo_supervisor', 'clinical')
    def resolve_discrepancy(discrepancy_id):
        """Proxy a resolve action through to the scheduler."""
        ok = scheduler_sync.resolve_discrepancy(discrepancy_id)
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'ok': ok}), (200 if ok else 502)
        if ok:
            flash('Discrepancy marked resolved.', 'success')
        else:
            flash('Failed to resolve discrepancy.', 'error')
        return redirect(url_for('dashboard'))

    @app.route('/dashboard')
    @roles_required('admin', 'clinical', 'superadmin', 'moa_supervisor', 'echo_supervisor', 'scribe_supervisor')
    def dashboard():
        """Redirect to appropriate dashboard based on user role"""
        user_role = session.get('user_role')
        if user_role == 'admin':
            return redirect(url_for('admin_dashboard'))
        elif user_role == 'clinical':
            return redirect(url_for('clinical_dashboard'))
        elif user_role == 'superadmin':
            return redirect(url_for('superadmin_dashboard'))
        elif user_role == 'moa_supervisor':
            return redirect(url_for('moa_supervisor_dashboard'))
        elif user_role == 'echo_supervisor':
            return redirect(url_for('echo_supervisor_dashboard'))
        elif user_role == 'scribe_supervisor':
            return redirect(url_for('scribe_supervisor_dashboard'))
        else:
            return redirect(url_for('index'))

    @app.route('/dashboard/admin')
    @roles_required('admin', 'superadmin')
    def admin_dashboard():
        """Admin dashboard"""
        from datetime import datetime, timedelta

        # Get pending requests for admin team (using manager_team field)
        pending_requests = PTORequest.query.filter_by(status='pending', manager_team='admin').all()

        # Get approved requests for admin team
        approved_requests = PTORequest.query.filter_by(status='approved', manager_team='admin').all()

        # Get in_progress requests for admin team
        in_progress_requests = PTORequest.query.filter_by(status='in_progress', manager_team='admin').all()

        # Get pending employee registrations for admin team
        pending_employees = PendingEmployee.query.filter_by(status='pending', team='admin').all()

        # Get employees currently on PTO (approved requests where today is within the date range)
        today_str = get_eastern_time().strftime('%Y-%m-%d')
        currently_on_pto = PTORequest.query.filter_by(status='approved', manager_team='admin').filter(
            PTORequest.start_date <= today_str,
            PTORequest.end_date >= today_str
        ).all()

        # Get team employees (admin team only, excluding inactive)
        admin_positions = Position.query.filter_by(team='admin').all()
        admin_pos_ids = [p.id for p in admin_positions]
        team_employees = TeamMember.query.filter(
            TeamMember.position_id.in_(admin_pos_ids),
            ~TeamMember.name.contains('[INACTIVE]')
        ).order_by(TeamMember.name).all()

        # Build stats dictionary for dashboard
        stats = {
            'pending': len(pending_requests),
            'in_progress': len(in_progress_requests),
            'approved': len(approved_requests),
            'total': len(pending_requests) + len(in_progress_requests) + len(approved_requests),
            'team_members': len(team_employees)
        }

        return render_template('dashboard_admin.html',
                               requests=pending_requests,
                               approved_requests=approved_requests,
                               in_progress_requests=in_progress_requests,
                               pending_employees=pending_employees,
                               currently_on_pto=currently_on_pto,
                               team_employees=team_employees,
                               stats=stats,
                               now=get_eastern_time)

    @app.route('/dashboard/clinical')
    @roles_required('clinical', 'superadmin')
    def clinical_dashboard():
        """Clinical dashboard"""
        from datetime import datetime, timedelta

        # Get pending requests for clinical team (using manager_team field)
        pending_requests = PTORequest.query.filter_by(status='pending', manager_team='clinical').all()

        # Get approved requests for clinical team
        approved_requests = PTORequest.query.filter_by(status='approved', manager_team='clinical').all()

        # Get in_progress requests for clinical team
        in_progress_requests = PTORequest.query.filter_by(status='in_progress', manager_team='clinical').all()

        # Get pending employee registrations for clinical team
        pending_employees = PendingEmployee.query.filter_by(status='pending', team='clinical').all()

        # Get employees currently on PTO (approved requests where today is within the date range)
        today_str = get_eastern_time().strftime('%Y-%m-%d')
        currently_on_pto = PTORequest.query.filter_by(status='approved', manager_team='clinical').filter(
            PTORequest.start_date <= today_str,
            PTORequest.end_date >= today_str
        ).all()

        # Get team employees (clinical team only, excluding inactive)
        clinical_positions = Position.query.filter_by(team='clinical').all()
        clinical_pos_ids = [p.id for p in clinical_positions]
        team_employees = TeamMember.query.filter(
            TeamMember.position_id.in_(clinical_pos_ids),
            ~TeamMember.name.contains('[INACTIVE]')
        ).order_by(TeamMember.name).all()

        # Build stats dictionary for dashboard
        stats = {
            'pending': len(pending_requests),
            'in_progress': len(in_progress_requests),
            'approved': len(approved_requests),
            'total': len(pending_requests) + len(in_progress_requests) + len(approved_requests),
            'team_members': len(team_employees)
        }

        return render_template('dashboard_clinical.html',
                               requests=pending_requests,
                               approved_requests=approved_requests,
                               in_progress_requests=in_progress_requests,
                               pending_employees=pending_employees,
                               currently_on_pto=currently_on_pto,
                               team_employees=team_employees,
                               stats=stats,
                               now=get_eastern_time)

    @app.route('/dashboard/superadmin')
    @roles_required('superadmin')
    def superadmin_dashboard():
        """Super admin dashboard"""
        requests = pto_system.get_all_requests()
        team_members = TeamMember.query.all()

        # Build calendar events with weekend splitting
        calendar_events = []
        for req in requests:
            if req.is_call_out:
                color = '#dc3545'
                text_color = '#fff'
            elif req.status == 'approved':
                color = '#28a745'
                text_color = '#fff'
            elif req.status == 'pending':
                color = '#ffc107'
                text_color = '#000'
            else:
                color = '#6c757d'
                text_color = '#fff'

            title = f"Call Out - {req.member.name}" if req.is_call_out else f"{req.member.name} - {req.pto_type}"
            segments = _get_business_day_segments(req.start_date, req.end_date)
            for idx, (seg_start, seg_end) in enumerate(segments):
                calendar_events.append({
                    'title': title,
                    'start': seg_start,
                    'end': seg_end,
                    'backgroundColor': color,
                    'borderColor': color,
                    'textColor': text_color,
                    'extendedProps': {
                        'status': req.status,
                        'is_call_out': req.is_call_out,
                        'type': req.pto_type,
                        'team': req.member.team if req.member else 'unknown',
                        'employee': req.member.name if req.member else 'Unknown',
                    }
                })

        return render_template('dashboard_superadmin.html',
                               requests=requests,
                               team_members=team_members,
                               calendar_events=calendar_events)

    @app.route('/api/staff-directory')
    def api_staff_directory():
        """API endpoint to get current staff directory"""
        staff_directory = pto_system.get_staff_directory()

        # Ensure all team/position combinations exist even if empty
        positions_by_team = {}
        for p in Position.query.all():
            if p.team not in positions_by_team:
                positions_by_team[p.team] = []
            positions_by_team[p.team].append(p.name)

        # Add empty position structures if they don't exist in staff directory
        for team, positions in positions_by_team.items():
            if team not in staff_directory:
                staff_directory[team] = {}
            for position in positions:
                if position not in staff_directory[team]:
                    staff_directory[team][position] = []

        return jsonify(staff_directory)

    @app.route('/api/positions')
    def api_positions():
        """API endpoint to get all available positions"""
        positions = {}
        for p in Position.query.all():
            if p.team not in positions:
                positions[p.team] = []
            positions[p.team].append(p.name)
        return jsonify(positions)

    @app.route('/api/callout-details/<int:request_id>')
    def api_callout_details(request_id):
        """API endpoint to get call-out details for a specific PTO request"""
        try:
            # Get the PTO request
            pto_request = PTORequest.query.get(request_id)
            if not pto_request or not pto_request.is_call_out:
                return jsonify({'success': False, 'message': 'Call-out not found'}), 404

            # Get the associated call-out record
            from models import CallOutRecord
            callout_record = CallOutRecord.query.filter_by(pto_request_id=request_id).first()

            if not callout_record:
                return jsonify({'success': False, 'message': 'Call-out details not found'}), 404

            # Prepare call-out data
            callout_data = {
                'source': callout_record.source,
                'phone_number': callout_record.phone_number_used,
                'authentication_method': callout_record.authentication_method,
                'verified': callout_record.verified,
                'message_text': callout_record.message_text,
                'created_at': callout_record.created_at.strftime('%m/%d/%Y %I:%M %p') if callout_record.created_at else None,
                'call_sid': callout_record.call_sid
            }

            return jsonify({'success': True, 'callout': callout_data})

        except Exception as e:
            return jsonify({'success': False, 'message': str(e)}), 500

    @app.route('/submit_request', methods=['POST'])
    def submit_request():
        """Handle PTO request submission or new employee registration"""
        try:
            # Check if this is a new employee registration
            if request.form.get('name') == 'NOT_LISTED':
                # Handle new employee registration
                new_employee_name = request.form.get('new_employee_name')
                new_employee_email = request.form.get('new_employee_email')
                new_employee_team = request.form.get('new_employee_team')
                new_employee_position = request.form.get('new_employee_position')
                employee_notes = request.form.get('employee_notes', '')

                # Validate required fields
                if not all([new_employee_name, new_employee_email, new_employee_team, new_employee_position]):
                    flash('Please fill in all required fields for employee registration.', 'error')
                    return redirect(url_for('index'))

                # Check if pending employee already exists
                existing_pending = PendingEmployee.query.filter_by(email=new_employee_email).first()
                if existing_pending and existing_pending.status == 'pending':
                    flash('An employee registration with this email is already pending approval.', 'warning')
                    return redirect(url_for('index'))

                # Check if employee already exists
                existing_employee = TeamMember.query.filter_by(email=new_employee_email).first()
                if existing_employee:
                    flash('An employee with this email already exists in the system.', 'error')
                    return redirect(url_for('index'))

                # Create pending employee record
                pending_employee = PendingEmployee(
                    name=new_employee_name,
                    email=new_employee_email,
                    team=new_employee_team,
                    position=new_employee_position,
                    status='pending'
                )

                # Add notes if provided
                if employee_notes:
                    pending_employee.denial_reason = f"Registration Notes: {employee_notes}"  # Using this field for notes

                db.session.add(pending_employee)
                db.session.commit()

                flash(f'Employee registration for {new_employee_name} has been submitted and will be reviewed by the {new_employee_team} team manager.', 'success')
                return redirect(url_for('index'))

            # Regular PTO request handling
            team = request.form.get('team')
            position = request.form.get('position')
            name = request.form.get('name')
            start_date = request.form.get('start_date')
            end_date = request.form.get('end_date')
            pto_type = request.form.get('pto_type')
            reason = request.form.get('reason')

            # Check if this is a call-out request
            call_out_flag = request.form.get('call_out', '0') == '1'

            # Find the team member by name and position
            member = TeamMember.query.join(Position).filter(
                TeamMember.name == name,
                Position.name == position,
                Position.team == team
            ).first()

            if not member:
                flash(f'Employee {name} not found in {team} team', 'error')
                return redirect(url_for('index'))

            # Block RNs from submitting PTO (call-outs still allowed)
            if position == 'RNs' and not call_out_flag:
                flash('Cannot submit PTO request. Please use paper form and submit manually to your nurse manager.', 'error')
                return redirect(url_for('index'))

            # Server-side validation for call-out requests
            if call_out_flag:
                if pto_type != 'Sick':
                    flash('Call-out requests must use PTO Type: Sick.', 'error')
                    return redirect(url_for('index'))

                if not reason or reason.strip() == '':
                    flash('Please provide a reason for calling out sick.', 'error')
                    return redirect(url_for('index'))

            # Classify call-outs as sick or fmla based on the reason text
            callout_classification = None
            if call_out_flag:
                from call_out_classifier import classify_call_out
                callout_classification = classify_call_out(reason)

            # Create PTO request with proper member relationship
            # Call-outs are auto-approved, regular PTO is pending
            pto_request = PTORequest(
                member_id=member.id,
                start_date=start_date,
                end_date=end_date,
                pto_type=pto_type,
                reason=reason,
                manager_team=team,
                is_call_out=call_out_flag,
                status='approved' if call_out_flag else 'pending',
                callout_classification=callout_classification
            )

            db.session.add(pto_request)
            db.session.commit()

            # If call-out, automatically deduct from sick balance
            if call_out_flag:
                hours_to_deduct = pto_request.duration_hours
                current_sick_balance = float(member.sick_balance_hours)
                new_sick_balance = max(0, current_sick_balance - hours_to_deduct)
                member.sick_balance_hours = new_sick_balance
                db.session.commit()

            # Send email notification for PTO submission
            try:
                email_service.send_submission_email(pto_request)
            except Exception as e:
                # Log error but don't fail the request
                print(f"Failed to send submission email: {str(e)}")

            # Auto-approved call-outs need to reach the scheduler too.
            if call_out_flag:
                try:
                    scheduler_sync.sync_pto_request(pto_request, 'upsert')
                except Exception as e:
                    print(f"Failed to sync call-out to scheduler: {str(e)}")

            # Different success message for call-out vs regular PTO
            if call_out_flag:
                flash(f'Call-out submitted successfully for {name}! Request ID: #{pto_request.id}', 'success')
            else:
                flash(f'PTO request submitted successfully for {name}! Request ID: #{pto_request.id}', 'success')
            return redirect(url_for('index'))

        except Exception as e:
            flash(f'Error submitting request: {str(e)}', 'error')
            return redirect(url_for('index'))

    @app.route('/calendar')
    def calendar():
        """Calendar view of PTO requests - viewable by anyone; detail modal requires login.
        Scribes are isolated: their PTO is only visible to scribe_supervisor and
        superadmin. Other viewers (anonymous, admin, clinical, MOA, Echo) never
        see scribe entries here.
        """
        is_logged_in = 'user_id' in session
        user_role = session.get('user_role')
        can_approve = user_role in (
            'admin', 'clinical', 'superadmin', 'moa_supervisor', 'echo_supervisor', 'scribe_supervisor'
        )

        base_query = PTORequest.query.filter(
            PTORequest.status.in_(['approved', 'pending'])
        )
        if user_role == 'scribe_supervisor':
            base_query = base_query.filter(PTORequest.manager_team == 'scribes')
        elif user_role != 'superadmin':
            base_query = base_query.filter(PTORequest.manager_team != 'scribes')
        all_requests = base_query.all()

        # Convert PTO requests to FullCalendar events format
        calendar_events = []
        for request in all_requests:
            # Determine colors based on call-out status first, then regular status
            if request.is_call_out:
                color = '#dc3545'  # Red for call-outs
                text_color = '#fff'
            elif request.status == 'approved':
                color = '#28a745'  # Green for approved PTO
                text_color = '#fff'
            elif request.status == 'pending':
                color = '#ffc107'  # Yellow for pending PTO
                text_color = '#000'
            else:
                color = '#6c757d'  # Gray for other statuses
                text_color = '#fff'

            # Calculate duration — show hours for partial days, days otherwise
            try:
                if request.is_partial_day:
                    duration_label = f"{request.duration_hours:.1f} hrs"
                else:
                    duration_label = f"{request.duration_days} day{'s' if request.duration_days != 1 else ''}"
            except:
                duration_label = "1 day"

            # Determine title based on call-out status
            if request.is_call_out:
                title = f"Call Out - {request.member.name}"
            else:
                title = f'{request.member.name} - {request.pto_type}'

            # Split into business-day segments (skip weekends)
            segments = _get_business_day_segments(request.start_date, request.end_date)
            for idx, (seg_start, seg_end) in enumerate(segments):
                event = {
                    'id': f'pto-{request.id}-{idx}',
                    'title': title,
                    'start': seg_start,
                    'end': seg_end,
                    'backgroundColor': color,
                    'borderColor': color,
                    'textColor': text_color,
                    'extendedProps': {
                        'employee': request.member.name,
                        'employee_position': request.member.position.name if request.member.position else 'Unknown',
                        'team': request.member.position.team if request.member.position else 'unknown',
                        'type': request.pto_type,
                        'status': request.status,
                        'reason': request.reason or '',
                        'duration': duration_label,
                        'is_partial_day': request.is_partial_day,
                        'partial_start_time': request.start_time if request.is_partial_day else None,
                        'partial_end_time': request.end_time if request.is_partial_day else None,
                        'is_call_out': request.is_call_out,
                        'request_id': request.id
                    }
                }
                calendar_events.append(event)

        return render_template('calendar.html',
                               requests=all_requests,
                               calendar_events=calendar_events,
                               is_logged_in=is_logged_in,
                               can_approve=can_approve)

    @app.route('/api/test-business-days')
    def test_business_days():
        """API endpoint to test business days calculations"""
        try:
            from business_days import BusinessDaysCalculator, get_pto_breakdown

            # Test cases demonstrating business days calculation
            test_cases = [
                {
                    'name': 'Thursday to Tuesday (includes weekend)',
                    'start_date': '2025-09-18',
                    'end_date': '2025-09-23',
                    'expected_business_days': 4
                },
                {
                    'name': 'Christmas period (includes holiday)',
                    'start_date': '2025-12-24',
                    'end_date': '2025-12-26',
                    'expected_business_days': 2
                },
                {
                    'name': 'Thanksgiving (includes holiday)',
                    'start_date': '2025-11-27',
                    'end_date': '2025-11-28',
                    'expected_business_days': 1
                },
                {
                    'name': 'July 4th weekend',
                    'start_date': '2025-07-03',
                    'end_date': '2025-07-07',
                    'expected_business_days': 3
                }
            ]

            results = []
            for case in test_cases:
                breakdown = get_pto_breakdown(case['start_date'], case['end_date'])
                results.append({
                    'test_case': case['name'],
                    'date_range': f"{case['start_date']} to {case['end_date']}",
                    'total_days': breakdown['total_days'],
                    'business_days': breakdown['business_days'],
                    'weekend_days': breakdown['weekend_days'],
                    'holiday_days': breakdown['holiday_days'],
                    'holidays': [h.strftime('%Y-%m-%d') for h in breakdown['holidays_list']],
                    'expected_business_days': case['expected_business_days'],
                    'calculation_correct': breakdown['business_days'] == case['expected_business_days']
                })

            return jsonify({
                'success': True,
                'message': 'Business days calculator test results',
                'test_results': results
            })

        except Exception as e:
            return jsonify({
                'success': False,
                'error': str(e)
            })

    @app.route('/api/holidays')
    def get_holidays():
        """API endpoint to get Mount Sinai official holidays for calendar display"""
        try:
            from business_days import get_holidays_for_calendar
            holidays = get_holidays_for_calendar()
            return jsonify(holidays)
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/team-calendar/<team>')
    def get_team_calendar(team):
        """API endpoint to get team-specific calendar events.
        Role-gated to prevent cross-team data leakage: each manager role
        can only fetch its own team. Superadmin sees all.
        """
        user_role = session.get('user_role')
        allowed_team = {
            'admin': 'admin',
            'clinical': 'clinical',
            'scribe_supervisor': 'scribes',
        }.get(user_role)
        if user_role != 'superadmin' and team != allowed_team:
            return jsonify({'error': 'Forbidden'}), 403
        try:
            # Get PTO requests for the specified team
            requests = PTORequest.query.filter(
                PTORequest.manager_team == team,
                PTORequest.status.in_(['approved', 'pending'])
            ).all()

            calendar_events = []
            for request in requests:
                # Determine colors based on call-out status first, then regular status
                if request.is_call_out:
                    color = '#dc3545'  # Red for call-outs
                    text_color = '#fff'
                elif request.status == 'approved':
                    color = '#28a745'  # Green for approved PTO
                    text_color = '#fff'
                elif request.status == 'pending':
                    color = '#ffc107'  # Yellow for pending PTO
                    text_color = '#000'
                else:
                    color = '#6c757d'  # Gray for other statuses
                    text_color = '#fff'

                # Calculate duration label — hours for partial, days otherwise
                try:
                    if request.is_partial_day:
                        duration_label = f"{request.duration_hours:.1f} hrs"
                    else:
                        d = request.duration_days
                        duration_label = f"{d} day{'s' if d != 1 else ''}"
                except:
                    duration_label = "1 day"

                # Build event title
                if request.is_call_out:
                    title = f"CALL OUT - {request.member.name}"
                else:
                    title = request.member.name

                # Split into business-day segments (skip weekends)
                segments = _get_business_day_segments(request.start_date, request.end_date)
                for idx, (seg_start, seg_end) in enumerate(segments):
                    event = {
                        'id': f'{request.id}-{idx}',
                        'title': title,
                        'start': seg_start,
                        'end': seg_end,
                        'color': color,
                        'textColor': text_color,
                        'allDay': True,
                        'extendedProps': {
                            'employee': request.member.name,
                            'employee_position': request.member.position.name if request.member.position else '',
                            'type': request.pto_type,
                            'status': request.status,
                            'is_call_out': request.is_call_out,
                            'is_partial_day': request.is_partial_day,
                            'partial_start_time': request.start_time if request.is_partial_day else None,
                            'partial_end_time': request.end_time if request.is_partial_day else None,
                            'duration': duration_label,
                            'reason': request.reason or '',
                            'team': request.manager_team
                        }
                    }
                    calendar_events.append(event)

            return jsonify(calendar_events)
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/team-calendar/position/<filter_type>')
    def get_position_team_calendar(filter_type):
        """API endpoint to get calendar events filtered by position group (echo/moa)"""
        try:
            if filter_type == 'echo':
                positions = Position.query.filter(
                    db.or_(Position.name.contains('Echo'), Position.name.contains('Vascular'))
                ).all()
            elif filter_type == 'moa':
                positions = Position.query.filter(Position.name.contains('MOA')).all()
            else:
                return jsonify([])

            pos_ids = [p.id for p in positions]
            members = TeamMember.query.filter(TeamMember.position_id.in_(pos_ids)).all()
            member_ids = [m.id for m in members]

            requests = PTORequest.query.filter(
                PTORequest.member_id.in_(member_ids),
                PTORequest.status.in_(['approved', 'pending'])
            ).all()

            calendar_events = []
            for request in requests:
                if request.is_call_out:
                    color = '#dc3545'
                    text_color = '#fff'
                elif request.status == 'approved':
                    color = '#28a745'
                    text_color = '#fff'
                elif request.status == 'pending':
                    color = '#ffc107'
                    text_color = '#000'
                else:
                    color = '#6c757d'
                    text_color = '#fff'

                try:
                    if request.is_partial_day:
                        duration_label = f"{request.duration_hours:.1f} hrs"
                    else:
                        d = request.duration_days
                        duration_label = f"{d} day{'s' if d != 1 else ''}"
                except:
                    duration_label = "1 day"

                title = f"CALL OUT - {request.member.name}" if request.is_call_out else request.member.name

                segments = _get_business_day_segments(request.start_date, request.end_date)
                for idx, (seg_start, seg_end) in enumerate(segments):
                    event = {
                        'id': f'{request.id}-{idx}',
                        'title': title,
                        'start': seg_start,
                        'end': seg_end,
                        'color': color,
                        'textColor': text_color,
                        'allDay': True,
                        'extendedProps': {
                            'employee': request.member.name,
                            'employee_position': request.member.position.name if request.member.position else '',
                            'type': request.pto_type,
                            'status': request.status,
                            'is_call_out': request.is_call_out,
                            'is_partial_day': request.is_partial_day,
                            'partial_start_time': request.start_time if request.is_partial_day else None,
                            'partial_end_time': request.end_time if request.is_partial_day else None,
                            'duration': duration_label,
                            'reason': request.reason or ''
                        }
                    }
                    calendar_events.append(event)

            return jsonify(calendar_events)
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/dashboard/moa_supervisor')
    @roles_required('moa_supervisor', 'superadmin')
    def moa_supervisor_dashboard():
        """MOA Supervisor dashboard"""
        from datetime import datetime, timedelta

        # Get MOA positions (positions with 'MOA' in the name)
        moa_positions = Position.query.filter(Position.name.contains('MOA')).all()
        moa_pos_ids = [p.id for p in moa_positions]

        # Get MOA team members (excluding inactive)
        team_employees = TeamMember.query.filter(
            TeamMember.position_id.in_(moa_pos_ids),
            ~TeamMember.name.contains('[INACTIVE]')
        ).order_by(TeamMember.name).all()
        moa_member_ids = [m.id for m in team_employees]

        # Get pending requests
        pending_requests = PTORequest.query.filter(
            PTORequest.member_id.in_(moa_member_ids),
            PTORequest.status == 'pending'
        ).all()

        # Get approved requests
        approved_requests = PTORequest.query.filter(
            PTORequest.member_id.in_(moa_member_ids),
            PTORequest.status == 'approved'
        ).all()

        # Get in_progress requests
        in_progress_requests = PTORequest.query.filter(
            PTORequest.member_id.in_(moa_member_ids),
            PTORequest.status == 'in_progress'
        ).all()

        # Get currently on PTO
        today_str = get_eastern_time().strftime('%Y-%m-%d')
        currently_on_pto = PTORequest.query.filter(
            PTORequest.member_id.in_(moa_member_ids),
            PTORequest.status == 'approved',
            PTORequest.start_date <= today_str,
            PTORequest.end_date >= today_str
        ).all()

        # Get pending employee registrations for clinical team (MOAs are clinical)
        pending_employees = PendingEmployee.query.filter_by(status='pending', team='clinical').all()

        return render_template('dashboard_moa_supervisor.html',
                               requests=pending_requests,
                               approved_requests=approved_requests,
                               in_progress_requests=in_progress_requests,
                               pending_employees=pending_employees,
                               currently_on_pto=currently_on_pto,
                               team_employees=team_employees,
                               now=get_eastern_time)

    @app.route('/dashboard/echo_supervisor')
    @roles_required('echo_supervisor', 'superadmin')
    def echo_supervisor_dashboard():
        """Echo Supervisor dashboard"""
        from datetime import datetime, timedelta

        # Get Echo Tech and Vascular Tech positions
        echo_positions = Position.query.filter(
            db.or_(Position.name.contains('Echo'), Position.name.contains('Vascular'))
        ).all()
        echo_pos_ids = [p.id for p in echo_positions]

        # Get Echo Tech and Vascular Tech team members (excluding inactive)
        team_employees = TeamMember.query.filter(
            TeamMember.position_id.in_(echo_pos_ids),
            ~TeamMember.name.contains('[INACTIVE]')
        ).order_by(TeamMember.name).all()
        echo_member_ids = [m.id for m in team_employees]

        # Get pending requests
        pending_requests = PTORequest.query.filter(
            PTORequest.member_id.in_(echo_member_ids),
            PTORequest.status == 'pending'
        ).all()

        # Get approved requests
        approved_requests = PTORequest.query.filter(
            PTORequest.member_id.in_(echo_member_ids),
            PTORequest.status == 'approved'
        ).all()

        # Get in_progress requests
        in_progress_requests = PTORequest.query.filter(
            PTORequest.member_id.in_(echo_member_ids),
            PTORequest.status == 'in_progress'
        ).all()

        # Get currently on PTO
        today_str = get_eastern_time().strftime('%Y-%m-%d')
        currently_on_pto = PTORequest.query.filter(
            PTORequest.member_id.in_(echo_member_ids),
            PTORequest.status == 'approved',
            PTORequest.start_date <= today_str,
            PTORequest.end_date >= today_str
        ).all()

        # Get pending employee registrations for clinical team (Echo techs are clinical)
        pending_employees = PendingEmployee.query.filter_by(status='pending', team='clinical').all()

        return render_template('dashboard_echo_supervisor.html',
                               requests=pending_requests,
                               approved_requests=approved_requests,
                               in_progress_requests=in_progress_requests,
                               pending_employees=pending_employees,
                               currently_on_pto=currently_on_pto,
                               team_employees=team_employees,
                               now=get_eastern_time)

    @app.route('/dashboard/scribe_supervisor')
    @roles_required('scribe_supervisor', 'superadmin')
    def scribe_supervisor_dashboard():
        """Scribe Supervisor dashboard — isolated from admin/clinical teams."""
        team_employees = TeamMember.query.join(Position).filter(
            Position.team == 'scribes',
            ~TeamMember.name.contains('[INACTIVE]')
        ).order_by(TeamMember.name).all()

        pending_requests = PTORequest.query.filter_by(
            status='pending', manager_team='scribes'
        ).all()
        approved_requests = PTORequest.query.filter_by(
            status='approved', manager_team='scribes'
        ).all()
        in_progress_requests = PTORequest.query.filter_by(
            status='in_progress', manager_team='scribes'
        ).all()

        today_str = get_eastern_time().strftime('%Y-%m-%d')
        currently_on_pto = PTORequest.query.filter_by(
            status='approved', manager_team='scribes'
        ).filter(
            PTORequest.start_date <= today_str,
            PTORequest.end_date >= today_str
        ).all()

        pending_employees = PendingEmployee.query.filter_by(
            status='pending', team='scribes'
        ).all()

        return render_template('dashboard_scribe_supervisor.html',
                               requests=pending_requests,
                               approved_requests=approved_requests,
                               in_progress_requests=in_progress_requests,
                               pending_employees=pending_employees,
                               currently_on_pto=currently_on_pto,
                               team_employees=team_employees,
                               now=get_eastern_time)

    @app.route('/employees')
    @roles_required('admin', 'clinical', 'superadmin', 'moa_supervisor', 'echo_supervisor', 'scribe_supervisor')
    def employees():
        """Employee management page - filtered by team and position parameters"""
        # Check for query parameters
        team_filter = request.args.get('team', None)
        position_filter = request.args.get('position', None)

        # Get positions and build query based on team filter
        if team_filter in ('admin', 'clinical', 'scribes'):
            positions = Position.query.filter_by(team=team_filter).order_by(Position.name).all()
            pos_ids = [p.id for p in positions]
            query = TeamMember.query.filter(TeamMember.position_id.in_(pos_ids))
        else:
            # No filter or 'all' - show all employees
            positions = Position.query.order_by(Position.name).all()
            query = TeamMember.query

        # Apply position filter if specified
        if position_filter:
            pos = Position.query.filter_by(name=position_filter).first()
            if pos:
                query = query.filter_by(position_id=pos.id)

        team_members = query.all()

        # Calculate comprehensive statistics
        active_employees = [m for m in team_members if '[INACTIVE]' not in m.name]
        total_pto_hours = sum(float(member.pto_balance_hours or 0) for member in team_members)
        total_pto_days = total_pto_hours / 7.5  # Convert hours to days
        avg_pto_days = total_pto_days / len(team_members) if team_members else 0

        stats = {
            'total_employees': len(team_members),
            'active_employees': len(active_employees),
            'total_pto_hours': total_pto_hours,
            'total_pto_days': round(total_pto_days, 1),
            'avg_pto_days': round(avg_pto_days, 1)
        }
        return render_template('employees.html',
                               team_members=team_members,
                               stats=stats,
                               positions=positions,
                               current_team=team_filter,
                               current_position=position_filter)

    @app.route('/pending_employees')
    @roles_required('admin', 'clinical', 'superadmin', 'scribe_supervisor')
    def pending_employees():
        """View pending employee registrations"""
        pending_employees = PendingEmployee.query.all()
        total_pending = len(pending_employees)
        return render_template('pending_employees.html', pending_employees=pending_employees, total_pending=total_pending)

    @app.route('/add_employee', methods=['GET', 'POST'])
    @roles_required('admin', 'clinical', 'superadmin', 'scribe_supervisor')
    def add_employee():
        """Add new employee"""
        print(f"DEBUG: add_employee called with method: {request.method}")
        print(f"DEBUG: Form data: {dict(request.form)}")

        if request.method == 'POST':
            try:
                # Get team first since position depends on it
                team = request.form.get('team')
                position = request.form.get('position')

                print(f"DEBUG: Selected team: {team}, position: {position}")

                employee_data = {
                    'name': request.form.get('name'),
                    'email': request.form.get('email'),
                    'team': team,
                    'position': position,
                    'pto_balance': float(request.form.get('pto_balance', 60.0)),
                    'pto_refresh_date': request.form.get('pto_refresh_date')
                }

                print(f"DEBUG: Employee data: {employee_data}")
                pto_system.add_employee(employee_data)
                flash(f'Employee {employee_data["name"]} added successfully!', 'success')
                return redirect(url_for('employees'))

            except ValueError as e:
                print(f"DEBUG: ValueError: {str(e)}")
                flash(str(e), 'error')
                return render_template('add_employee.html')
            except Exception as e:
                print(f"DEBUG: Exception: {str(e)}")
                flash(f'Error adding employee: {str(e)}', 'error')
                return render_template('add_employee.html')

        return render_template('add_employee.html')

    @app.route('/employee/<int:employee_id>')
    @roles_required('admin', 'clinical', 'superadmin', 'moa_supervisor', 'echo_supervisor', 'scribe_supervisor')
    def employee_detail(employee_id):
        """View employee details"""
        from datetime import datetime, timedelta
        import traceback

        try:
            employee = TeamMember.query.get_or_404(employee_id)

            # Get all PTO requests for this employee
            pto_requests = PTORequest.query.filter_by(member_id=employee_id).order_by(PTORequest.created_at.desc()).all()

            # Calculate statistics
            total_requests = len(pto_requests)
            approved_requests = len([r for r in pto_requests if r.status == 'approved'])
            pending_requests = len([r for r in pto_requests if r.status == 'pending'])
            denied_requests = len([r for r in pto_requests if r.status == 'denied'])

            # Calculate call-out statistics
            total_callouts = len([r for r in pto_requests if r.is_call_out])
            approved_callouts = len([r for r in pto_requests if r.is_call_out and r.status == 'approved'])
            pending_callouts = len([r for r in pto_requests if r.is_call_out and r.status == 'pending'])

            # Split approved requests into completed (past) and all approved
            today_str = get_eastern_time().strftime('%Y-%m-%d')

            # COMPLETED: approved requests where end_date is in the past
            completed_pto = [r for r in pto_requests if r.status == 'approved' and not r.is_call_out and r.pto_type != 'Sick' and r.end_date <= today_str]
            completed_sick = [r for r in pto_requests if r.status == 'approved' and r.pto_type == 'Sick' and not r.is_call_out and r.end_date <= today_str]
            completed_callouts = [r for r in pto_requests if r.status == 'approved' and r.is_call_out and r.end_date <= today_str]

            completed_pto_days = sum(r.duration_days for r in completed_pto)
            completed_pto_hours = completed_pto_days * 7.5
            completed_sick_days = sum(r.duration_days for r in completed_sick)
            completed_sick_hours = completed_sick_days * 7.5
            completed_callout_days = sum(r.duration_days for r in completed_callouts)
            completed_callout_hours = completed_callout_days * 7.5

            # Per-type breakdown for Completed (past) section
            completed_approved = [r for r in pto_requests if r.status == 'approved' and r.end_date <= today_str]

            def _bucket(requests_list):
                count = len(requests_list)
                days = sum(r.duration_days for r in requests_list)
                return {
                    'count': count,
                    'days': round(days, 1),
                    'hours': round(days * 7.5, 1),
                }

            type_breakdown = {
                'vacation': _bucket([r for r in completed_approved if not r.is_call_out and r.pto_type == 'Vacation']),
                'personal': _bucket([r for r in completed_approved if not r.is_call_out and r.pto_type == 'Personal']),
                'sick_regular': _bucket([r for r in completed_approved if not r.is_call_out and r.pto_type == 'Sick']),
                'sick_callout': _bucket([r for r in completed_approved if r.is_call_out and (r.callout_classification or 'sick') == 'sick']),
                'fmla_callout': _bucket([r for r in completed_approved if r.is_call_out and r.callout_classification == 'fmla']),
                'union_business': _bucket([r for r in completed_approved if not r.is_call_out and r.pto_type == 'Union Business']),
            }

            # ALL APPROVED: all approved requests regardless of date
            all_pto = [r for r in pto_requests if r.status == 'approved' and not r.is_call_out and r.pto_type != 'Sick']
            all_sick = [r for r in pto_requests if r.status == 'approved' and r.pto_type == 'Sick' and not r.is_call_out]
            all_callouts = [r for r in pto_requests if r.status == 'approved' and r.is_call_out]

            all_pto_days = sum(r.duration_days for r in all_pto)
            all_pto_hours = all_pto_days * 7.5
            all_sick_days = sum(r.duration_days for r in all_sick)
            all_sick_hours = all_sick_days * 7.5
            all_callout_days = sum(r.duration_days for r in all_callouts)
            all_callout_hours = all_callout_days * 7.5

            # Calculate days until PTO refresh
            if employee.pto_refresh_date:
                today = datetime.now().date()
                refresh_date = employee.pto_refresh_date
                if refresh_date < today:
                    # If refresh date has passed, calculate next year's refresh
                    refresh_date = datetime(today.year + 1, refresh_date.month, refresh_date.day).date()
                days_until_refresh = (refresh_date - today).days
            else:
                days_until_refresh = None

            # Get tardiness records for this employee
            tardiness_records = TardinessRecord.query.filter_by(member_id=employee_id).order_by(TardinessRecord.date.desc()).all()
            total_tardiness = len(tardiness_records)
            total_tardiness_minutes = sum(r.minutes_late for r in tardiness_records)

            stats = {
                'total_requests': total_requests,
                'approved_requests': approved_requests,
                'pending_requests': pending_requests,
                'denied_requests': denied_requests,
                'total_callouts': total_callouts,
                'approved_callouts': approved_callouts,
                'pending_callouts': pending_callouts,
                # Completed (past dates only)
                'completed_pto_days': round(completed_pto_days, 1),
                'completed_pto_hours': round(completed_pto_hours, 1),
                'completed_sick_days': round(completed_sick_days, 1),
                'completed_sick_hours': round(completed_sick_hours, 1),
                'completed_callout_days': round(completed_callout_days, 1),
                'completed_callout_hours': round(completed_callout_hours, 1),
                # All approved (including future)
                'all_pto_days': round(all_pto_days, 1),
                'all_pto_hours': round(all_pto_hours, 1),
                'all_sick_days': round(all_sick_days, 1),
                'all_sick_hours': round(all_sick_hours, 1),
                'all_callout_days': round(all_callout_days, 1),
                'all_callout_hours': round(all_callout_hours, 1),
                'days_until_refresh': days_until_refresh,
                'total_tardiness': total_tardiness,
                'total_tardiness_minutes': total_tardiness_minutes,
                'type_breakdown': type_breakdown,
            }

            return render_template('employee_detail.html', employee=employee, pto_requests=pto_requests, stats=stats, tardiness_records=tardiness_records)
        except Exception as e:
            print(f"ERROR in employee_detail for ID {employee_id}: {str(e)}")
            traceback.print_exc()
            return f"Error loading employee details: {str(e)}", 500

    @app.route('/api/employee/<int:employee_id>/pto-events')
    @roles_required('admin', 'clinical', 'superadmin', 'moa_supervisor', 'echo_supervisor', 'scribe_supervisor')
    def get_employee_pto_events(employee_id):
        """API endpoint to get PTO events for a specific employee (for calendar view)"""
        from datetime import timedelta

        employee = TeamMember.query.get_or_404(employee_id)
        requests = PTORequest.query.filter_by(member_id=employee_id).all()

        events = []
        for req in requests:
            # Determine event color based on status
            if req.is_call_out:
                color = '#dc3545'  # Red for call-outs
            elif req.status == 'approved':
                color = '#28a745'  # Green
            elif req.status == 'pending':
                color = '#ffc107'  # Yellow
            elif req.status == 'denied':
                color = '#6c757d'  # Gray
            else:
                color = '#17a2b8'  # Info blue for other statuses

            # Determine title
            if req.is_call_out:
                title = f"Call Out - {req.pto_type}"
            else:
                title = req.pto_type

            # Calculate end date (FullCalendar uses exclusive end dates, so add 1 day)
            try:
                end_date = datetime.strptime(str(req.end_date), '%Y-%m-%d')
                end_date_exclusive = (end_date + timedelta(days=1)).strftime('%Y-%m-%d')
            except:
                end_date_exclusive = str(req.end_date)

            event = {
                'id': req.id,
                'title': title,
                'start': str(req.start_date),
                'end': end_date_exclusive,
                'color': color,
                'allDay': not req.is_partial_day,
                'extendedProps': {
                    'type': req.pto_type,
                    'status': req.status,
                    'duration': req.duration_days if not req.is_partial_day else f"{req.duration_hours} hours",
                    'is_partial_day': req.is_partial_day,
                    'is_call_out': req.is_call_out,
                    'reason': req.reason or 'Not specified',
                    'submitted': req.submitted_at.strftime('%m/%d/%Y') if req.submitted_at else 'N/A',
                    'start_date': str(req.start_date),
                    'end_date': str(req.end_date)
                }
            }

            # For partial day requests, add time information
            if req.is_partial_day and req.start_time and req.end_time:
                event['start'] = f"{req.start_date}T{req.start_time}"
                event['end'] = f"{req.end_date}T{req.end_time}"
                event['title'] = f"{title} (Partial)"
                event['allDay'] = False

            events.append(event)

        # Add tardiness events to the calendar
        tardiness_records = TardinessRecord.query.filter_by(member_id=employee_id).all()
        for record in tardiness_records:
            # Convert date to string for JSON serialization
            record_date_str = str(record.date) if record.date else None
            event = {
                'id': f'tardiness_{record.id}',
                'title': f'Late ({record.minutes_late} min)',
                'start': record_date_str,
                'end': record_date_str,
                'color': '#fd7e14',  # Orange for tardiness
                'allDay': True,
                'extendedProps': {
                    'type': 'Tardiness',
                    'status': 'recorded',
                    'duration': f'{record.minutes_late} minutes',
                    'is_partial_day': False,
                    'is_call_out': False,
                    'is_tardiness': True,
                    'reason': record.reason or 'Not specified',
                    'submitted': record.created_at.strftime('%m/%d/%Y') if record.created_at else 'N/A',
                    'start_date': record_date_str,
                    'end_date': record_date_str
                }
            }
            events.append(event)

        return jsonify(events)

    @app.route('/api/employee/<int:employee_id>/tardiness', methods=['POST'])
    @roles_required('admin', 'clinical', 'superadmin', 'moa_supervisor', 'echo_supervisor', 'scribe_supervisor')
    def add_tardiness(employee_id):
        """API endpoint to add a tardiness record for an employee"""
        employee = TeamMember.query.get_or_404(employee_id)

        data = request.get_json() if request.is_json else request.form

        date = data.get('date')
        minutes_late = data.get('minutes_late')
        reason = data.get('reason', '')

        if not date or not minutes_late:
            return jsonify({'error': 'Date and minutes late are required'}), 400

        try:
            minutes_late = int(minutes_late)
        except ValueError:
            return jsonify({'error': 'Minutes late must be a number'}), 400

        # Get current manager ID if logged in
        manager_id = session.get('user_id')

        tardiness = TardinessRecord(
            member_id=employee_id,
            date=date,
            minutes_late=minutes_late,
            reason=reason,
            recorded_by_id=manager_id
        )

        db.session.add(tardiness)
        db.session.commit()

        return jsonify({
            'success': True,
            'message': f'Tardiness recorded for {employee.name}',
            'id': tardiness.id
        })

    @app.route('/api/employee/<int:employee_id>/tardiness/<int:tardiness_id>', methods=['DELETE'])
    @roles_required('admin', 'clinical', 'superadmin', 'moa_supervisor', 'echo_supervisor', 'scribe_supervisor')
    def delete_tardiness(employee_id, tardiness_id):
        """API endpoint to delete a tardiness record"""
        tardiness = TardinessRecord.query.filter_by(id=tardiness_id, member_id=employee_id).first_or_404()

        db.session.delete(tardiness)
        db.session.commit()

        return jsonify({'success': True, 'message': 'Tardiness record deleted'})

    @app.route('/api/request/<int:request_id>/classify-callout', methods=['POST'])
    @roles_required('admin', 'clinical', 'superadmin', 'moa_supervisor', 'echo_supervisor', 'scribe_supervisor')
    def classify_callout(request_id):
        """Manager-only: update a call-out's classification (sick or fmla)."""
        data = request.get_json() or {}
        classification = data.get('classification')

        if classification not in ('sick', 'fmla'):
            return jsonify({'success': False, 'message': 'Invalid classification'}), 400

        pto_request = PTORequest.query.get(request_id)
        if not pto_request:
            return jsonify({'success': False, 'message': 'Request not found'}), 404

        if not pto_request.is_call_out:
            return jsonify({'success': False, 'message': 'Not a call-out request'}), 400

        pto_request.callout_classification = classification
        pto_request.updated_at = get_eastern_time()
        db.session.commit()

        return jsonify({'success': True, 'classification': classification})

    @app.route('/employee/edit/<int:employee_id>', methods=['GET', 'POST'])
    @roles_required('admin', 'clinical', 'superadmin', 'scribe_supervisor')
    def edit_employee(employee_id):
        """Edit employee details"""
        employee = TeamMember.query.get_or_404(employee_id)

        if request.method == 'POST':
            try:
                employee.name = request.form.get('name')
                employee.email = request.form.get('email')
                employee.phone = request.form.get('phone')

                # Update PTO balance
                pto_balance = float(request.form.get('pto_balance', employee.pto_balance_hours))
                employee.pto_balance_hours = pto_balance

                # Update sick balance
                sick_balance = float(request.form.get('sick_balance', employee.sick_balance_hours))
                employee.sick_balance_hours = sick_balance

                db.session.commit()
                flash(f'Employee {employee.name} updated successfully!', 'success')
                return redirect(url_for('employees'))
            except Exception as e:
                flash(f'Error updating employee: {str(e)}', 'error')

        return render_template('edit_employee.html', employee=employee)

    @app.route('/api/positions-list')
    @roles_required('admin', 'clinical', 'superadmin', 'scribe_supervisor')
    def api_positions_list():
        """API endpoint to get all positions with IDs for dropdowns"""
        positions = Position.query.order_by(Position.team, Position.name).all()
        return jsonify({
            'positions': [{'id': p.id, 'name': p.name, 'team': p.team} for p in positions]
        })

    @app.route('/api/employee/<int:employee_id>/update', methods=['POST'])
    @roles_required('admin', 'clinical', 'superadmin', 'scribe_supervisor')
    def update_employee_field(employee_id):
        """API endpoint to update a single employee field inline"""
        employee = TeamMember.query.get_or_404(employee_id)
        data = request.get_json()

        field = data.get('field')
        value = data.get('value')

        if not field:
            return jsonify({'success': False, 'error': 'Field name is required'}), 400

        try:
            display_value = value
            extra_data = {}

            if field == 'name':
                employee.name = value
                display_value = value
            elif field == 'email':
                employee.email = value
                display_value = value
            elif field == 'phone':
                employee.phone = value if value else None
                display_value = value if value else 'Not provided'
            elif field == 'pin':
                if value and len(value) != 4:
                    return jsonify({'success': False, 'error': 'PIN must be exactly 4 digits'}), 400
                if value and not value.isdigit():
                    return jsonify({'success': False, 'error': 'PIN must contain only digits'}), 400
                employee.pin = value if value else None
                display_value = '****' if value else 'Not set'
            elif field == 'position_id':
                position = Position.query.get(int(value))
                if not position:
                    return jsonify({'success': False, 'error': 'Invalid position'}), 400
                employee.position_id = position.id
                display_value = position.name
                extra_data['team'] = position.team
            elif field == 'pto_balance_hours':
                employee.pto_balance_hours = float(value)
                display_value = str(float(value))
            elif field == 'sick_balance_hours':
                employee.sick_balance_hours = float(value)
                display_value = str(float(value))
            elif field == 'pto_refresh_date':
                if value:
                    from datetime import datetime
                    employee.pto_refresh_date = datetime.strptime(value, '%Y-%m-%d').date()
                    display_value = employee.pto_refresh_date.strftime('%m/%d/%Y')
                else:
                    employee.pto_refresh_date = None
                    display_value = 'Not set'
            else:
                return jsonify({'success': False, 'error': f'Unknown field: {field}'}), 400

            db.session.commit()

            response = {'success': True, 'display_value': display_value}
            response.update(extra_data)
            return jsonify(response)

        except Exception as e:
            db.session.rollback()
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/api/employee/<int:employee_id>/add-pto', methods=['POST'])
    @roles_required('admin', 'clinical', 'superadmin', 'moa_supervisor', 'echo_supervisor', 'scribe_supervisor')
    def add_pto_for_employee(employee_id):
        """API endpoint to create a PTO request on behalf of an employee"""
        employee = TeamMember.query.get_or_404(employee_id)
        data = request.get_json()

        start_date = data.get('start_date')
        end_date = data.get('end_date')
        pto_type = data.get('pto_type')
        is_partial_day = data.get('is_partial_day', False)
        start_time = data.get('start_time')
        end_time = data.get('end_time')
        is_call_out = data.get('is_call_out', False)
        reason = data.get('reason', '')

        # Validation
        if not start_date or not end_date or not pto_type:
            return jsonify({'success': False, 'error': 'Start date, end date, and PTO type are required'}), 400

        # Call-out validation
        if is_call_out and pto_type != 'Sick':
            return jsonify({'success': False, 'error': 'Call-out requests must use Sick type'}), 400

        try:
            # Determine manager team from employee's position
            manager_team = employee.team or 'admin'

            # Classify call-outs as sick or fmla based on the reason text
            callout_classification = None
            if is_call_out:
                from call_out_classifier import classify_call_out
                callout_classification = classify_call_out(reason)

            # Create PTO request
            # Call-outs are auto-approved, regular PTO is pending
            pto_request = PTORequest(
                member_id=employee.id,
                start_date=start_date,
                end_date=end_date,
                pto_type=pto_type,
                reason=reason,
                manager_team=manager_team,
                is_call_out=is_call_out,
                is_partial_day=is_partial_day,
                start_time=start_time if is_partial_day else None,
                end_time=end_time if is_partial_day else None,
                status='approved' if is_call_out else 'pending',
                callout_classification=callout_classification
            )

            db.session.add(pto_request)
            db.session.commit()

            # If call-out, automatically deduct from sick balance
            if is_call_out:
                hours_to_deduct = pto_request.duration_hours
                current_sick_balance = float(employee.sick_balance_hours or 0)
                new_sick_balance = max(0, current_sick_balance - hours_to_deduct)
                employee.sick_balance_hours = new_sick_balance
                db.session.commit()

            # Send email notification
            try:
                email_service.send_submission_email(pto_request)
            except Exception as e:
                print(f"Failed to send submission email: {str(e)}")

            # Auto-approved call-outs need to reach the scheduler too.
            if is_call_out:
                try:
                    scheduler_sync.sync_pto_request(pto_request, 'upsert')
                except Exception as e:
                    print(f"Failed to sync call-out to scheduler: {str(e)}")

            # Different success message for call-out vs regular PTO
            if is_call_out:
                message = f'Call-out submitted and auto-approved for {employee.name}. Sick time deducted: {pto_request.duration_hours} hrs'
            else:
                message = f'PTO request submitted for {employee.name}. Request ID: #{pto_request.id}'

            return jsonify({
                'success': True,
                'message': message,
                'request_id': pto_request.id
            })

        except Exception as e:
            db.session.rollback()
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/employee/delete/<int:employee_id>', methods=['POST'])
    @roles_required('admin', 'clinical', 'superadmin', 'scribe_supervisor')
    def delete_employee(employee_id):
        """Delete or deactivate employee"""
        employee = TeamMember.query.get_or_404(employee_id)
        employee_name = employee.name.replace('[INACTIVE] ', '')

        try:
            # If employee is already inactive, delete them and all their PTO history
            if '[INACTIVE]' in employee.name:
                # Delete all PTO requests for this employee
                PTORequest.query.filter_by(member_id=employee.id).delete()
                # Delete the employee
                db.session.delete(employee)
                db.session.commit()
                flash(f'Inactive employee {employee_name} and all PTO history deleted permanently.', 'success')
            else:
                # Check if active employee has PTO requests
                has_pto_history = PTORequest.query.filter_by(member_id=employee.id).first() is not None

                if has_pto_history:
                    # Mark as inactive instead of deleting
                    employee.name = f'[INACTIVE] {employee.name}'
                    db.session.commit()
                    flash(f'Employee {employee_name} marked as inactive due to PTO history.', 'info')
                else:
                    # Safe to delete (no PTO history)
                    db.session.delete(employee)
                    db.session.commit()
                    flash(f'Employee {employee_name} deleted successfully.', 'success')

        except Exception as e:
            flash(f'Error deleting employee: {str(e)}', 'error')

        return redirect(url_for('employees'))

    @app.route('/approve_request/<int:request_id>')
    @roles_required('admin', 'clinical', 'superadmin', 'moa_supervisor', 'echo_supervisor', 'scribe_supervisor')
    def approve_request(request_id):
        """Approve a PTO request and deduct hours from balance"""
        next_view = 'calendar' if request.args.get('next') == 'calendar' else 'dashboard'
        try:
            pto_request = PTORequest.query.get_or_404(request_id)

            if pto_request.status != 'pending':
                flash('This request has already been processed.', 'warning')
                return redirect(url_for(next_view))

            pto_request.status = 'approved'
            pto_request.approved_date = get_eastern_time()
            pto_request.updated_at = get_eastern_time()

            # Deduct hours from employee balance
            member = pto_request.member
            hours_to_deduct = pto_request.duration_hours

            if pto_request.pto_type == 'Sick':
                current_balance = float(member.sick_balance_hours or 0)
                member.sick_balance_hours = max(0, current_balance - hours_to_deduct)
                print(f"Deducted {hours_to_deduct} sick hrs from {member.name}. Balance: {member.sick_balance_hours}")
            else:
                current_balance = float(member.pto_balance_hours or 0)
                member.pto_balance_hours = max(0, current_balance - hours_to_deduct)
                print(f"Deducted {hours_to_deduct} PTO hrs from {member.name}. Balance: {member.pto_balance_hours}")

            db.session.commit()

            # Send email notification for approval
            try:
                email_service.send_approval_email(pto_request)
            except Exception as e:
                print(f"Failed to send approval email: {str(e)}")

            # Push to MSW Heart Scheduler (Echo techs only; best-effort).
            try:
                scheduler_sync.sync_pto_request(pto_request, 'upsert')
            except Exception as e:
                print(f"Failed to sync to scheduler: {str(e)}")

            flash(f'PTO request for {member.name} approved! {hours_to_deduct} hours deducted.', 'success')

        except Exception as e:
            flash(f'Error approving request: {str(e)}', 'error')

        return redirect(url_for(next_view))

    @app.route('/deny_request/<int:request_id>', methods=['POST'])
    @roles_required('admin', 'clinical', 'superadmin', 'moa_supervisor', 'echo_supervisor', 'scribe_supervisor')
    def deny_request(request_id):
        """Deny a PTO request"""
        next_view = 'calendar' if request.form.get('next') == 'calendar' else 'dashboard'
        try:
            pto_request = PTORequest.query.get_or_404(request_id)
            denial_reason = request.form.get('denial_reason', 'No reason provided')

            pto_request.status = 'denied'
            pto_request.updated_at = get_eastern_time()
            pto_request.denial_reason = denial_reason

            db.session.commit()

            # Send email notification for denial
            try:
                email_service.send_denial_email(pto_request, denial_reason)
            except Exception as e:
                # Log error but don't fail the request
                print(f"Failed to send denial email: {str(e)}")

            flash(f'PTO request for {pto_request.member.name} has been denied.', 'warning')

        except Exception as e:
            flash(f'Error denying request: {str(e)}', 'error')

        return redirect(url_for(next_view))

    @app.route('/approve_employee/<int:employee_id>')
    @roles_required('admin', 'clinical', 'superadmin', 'scribe_supervisor')
    def approve_employee(employee_id):
        """Approve a pending employee registration"""
        pending_employee = PendingEmployee.query.get_or_404(employee_id)
        user_role = session.get('user_role', '')
        user_id = session.get('user_id')

        # Check permissions
        can_approve = False
        if user_role == 'superadmin':
            can_approve = True
        elif user_role == 'admin' and pending_employee.team == 'admin':
            can_approve = True
        elif user_role == 'clinical' and pending_employee.team == 'clinical':
            can_approve = True
        elif user_role == 'scribe_supervisor' and pending_employee.team == 'scribes':
            can_approve = True

        if not can_approve:
            flash('You do not have permission to approve this employee.', 'error')
            return redirect(url_for('dashboard'))

        # Find or create the position
        position = Position.query.filter_by(name=pending_employee.position, team=pending_employee.team).first()
        if not position:
            position = Position(name=pending_employee.position, team=pending_employee.team)
            db.session.add(position)
            db.session.flush()

        # Create the new team member
        new_member = TeamMember(
            name=pending_employee.name,
            email=pending_employee.email,
            position_id=position.id,
            pto_balance_hours=60.0  # Default PTO balance
        )

        # Update pending employee status
        pending_employee.status = 'approved'
        pending_employee.approved_at = get_eastern_time()
        pending_employee.approved_by_id = user_id

        db.session.add(new_member)
        db.session.commit()

        flash(f'Employee {pending_employee.name} has been approved and added to the {pending_employee.team} team.', 'success')
        return redirect(url_for('dashboard'))

    @app.route('/deny_employee/<int:employee_id>', methods=['POST'])
    @roles_required('admin', 'clinical', 'superadmin', 'scribe_supervisor')
    def deny_employee(employee_id):
        """Deny a pending employee registration"""
        pending_employee = PendingEmployee.query.get_or_404(employee_id)
        user_role = session.get('user_role', '')
        user_id = session.get('user_id')

        # Check permissions
        can_deny = False
        if user_role == 'superadmin':
            can_deny = True
        elif user_role == 'admin' and pending_employee.team == 'admin':
            can_deny = True
        elif user_role == 'clinical' and pending_employee.team == 'clinical':
            can_deny = True
        elif user_role == 'scribe_supervisor' and pending_employee.team == 'scribes':
            can_deny = True

        if not can_deny:
            flash('You do not have permission to deny this employee.', 'error')
            return redirect(url_for('dashboard'))

        # Update pending employee status
        pending_employee.status = 'denied'
        pending_employee.approved_at = get_eastern_time()
        pending_employee.approved_by_id = user_id

        # Add denial reason if provided
        denial_reason = request.form.get('denial_reason', '')
        if denial_reason:
            pending_employee.denial_reason = denial_reason

        db.session.commit()

        flash(f'Employee registration for {pending_employee.name} has been denied.', 'info')
        return redirect(url_for('dashboard'))

    @app.route('/workqueue/in_progress')
    @roles_required('admin', 'clinical', 'superadmin', 'scribe_supervisor')
    def workqueue_in_progress():
        """View in-progress PTO requests with checklist"""
        user_role = session.get('user_role', '')

        # Get in-progress requests based on role
        if user_role == 'superadmin':
            in_progress_requests = PTORequest.query.filter_by(status='in_progress').all()
        elif user_role == 'admin':
            in_progress_requests = PTORequest.query.filter_by(status='in_progress', manager_team='admin').all()
        elif user_role == 'clinical':
            in_progress_requests = PTORequest.query.filter_by(status='in_progress', manager_team='clinical').all()
        elif user_role == 'scribe_supervisor':
            in_progress_requests = PTORequest.query.filter_by(status='in_progress', manager_team='scribes').all()
        else:
            in_progress_requests = []

        return render_template('workqueue_in_progress.html', requests=in_progress_requests, now=get_eastern_time)

    @app.route('/workqueue/approved')
    @roles_required('admin', 'clinical', 'superadmin', 'scribe_supervisor')
    def workqueue_approved():
        """View approved PTO requests"""
        user_role = session.get('user_role', '')

        # Get approved requests based on role
        if user_role == 'superadmin':
            approved_requests = PTORequest.query.filter_by(status='approved').all()
        elif user_role == 'admin':
            approved_requests = PTORequest.query.filter_by(status='approved', manager_team='admin').all()
        elif user_role == 'clinical':
            approved_requests = PTORequest.query.filter_by(status='approved', manager_team='clinical').all()
        elif user_role == 'scribe_supervisor':
            approved_requests = PTORequest.query.filter_by(status='approved', manager_team='scribes').all()
        else:
            approved_requests = []

        from datetime import datetime
        return render_template('workqueue_approved.html', requests=approved_requests, now=get_eastern_time, datetime=datetime)

    @app.route('/workqueue/completed')
    @roles_required('admin', 'clinical', 'superadmin', 'scribe_supervisor')
    def workqueue_completed():
        """View completed PTO requests"""
        user_role = session.get('user_role', '')

        # Get completed requests based on role
        if user_role == 'superadmin':
            completed_requests = PTORequest.query.filter_by(status='completed').all()
        elif user_role == 'admin':
            completed_requests = PTORequest.query.filter_by(status='completed', manager_team='admin').all()
        elif user_role == 'clinical':
            completed_requests = PTORequest.query.filter_by(status='completed', manager_team='clinical').all()
        elif user_role == 'scribe_supervisor':
            completed_requests = PTORequest.query.filter_by(status='completed', manager_team='scribes').all()
        else:
            completed_requests = []

        return render_template('workqueue_completed.html', requests=completed_requests, now=get_eastern_time)

    @app.route('/update_checklist/<int:request_id>', methods=['POST'])
    @roles_required('admin', 'clinical', 'superadmin', 'scribe_supervisor')
    def update_checklist(request_id):
        """Update checklist items for in-progress request"""
        pto_request = PTORequest.query.get_or_404(request_id)

        # Update checklist items
        pto_request.timekeeping_entered = request.form.get('timekeeping_entered') == 'on'
        pto_request.coverage_arranged = request.form.get('coverage_arranged') == 'on'

        # If both are complete, move to approved status
        newly_approved = False
        if pto_request.timekeeping_entered and pto_request.coverage_arranged:
            pto_request.status = 'approved'
            newly_approved = True
            flash(f'PTO request for {pto_request.member.name} is now fully approved!', 'success')

            # No email sent when checklist is completed (per updated requirements)

        pto_request.updated_at = get_eastern_time()
        db.session.commit()

        if newly_approved:
            try:
                scheduler_sync.sync_pto_request(pto_request, 'upsert')
            except Exception as e:
                print(f"Failed to sync to scheduler: {str(e)}")

        return redirect(url_for('workqueue_in_progress'))

    @app.route('/delete_request/<int:request_id>', methods=['POST'])
    @roles_required('admin', 'clinical', 'superadmin', 'scribe_supervisor')
    def delete_request(request_id):
        """Delete a PTO request (admin only) - restores balance if approved/in_progress/completed"""
        try:
            pto_request = PTORequest.query.get_or_404(request_id)
            member = pto_request.member
            employee_name = member.name if member else 'Unknown'

            was_approved = pto_request.status in ['approved', 'in_progress', 'completed']

            # If the request was approved/in_progress/completed, restore the balance
            if was_approved:
                hours_to_restore = pto_request.duration_hours

                if pto_request.pto_type == 'Sick':
                    member.sick_balance_hours = float(member.sick_balance_hours or 0) + hours_to_restore
                else:
                    member.pto_balance_hours = float(member.pto_balance_hours or 0) + hours_to_restore

            # Remove from MSW Heart Scheduler before we lose the row. Only
            # meaningful if it was ever synced (approved+ status, Echo tech).
            if was_approved:
                try:
                    scheduler_sync.sync_pto_request(pto_request, 'delete')
                except Exception as e:
                    print(f"Failed to sync delete to scheduler: {str(e)}")

            # Delete associated CallOutRecord if exists
            from models import CallOutRecord
            CallOutRecord.query.filter_by(pto_request_id=request_id).delete()

            # Delete the PTO request
            db.session.delete(pto_request)
            db.session.commit()

            flash(f'PTO request for {employee_name} has been deleted. Balance restored if applicable.', 'success')

        except Exception as e:
            db.session.rollback()
            flash(f'Error deleting request: {str(e)}', 'error')

        return redirect(url_for('dashboard'))

    @app.route('/edit_request/<int:request_id>', methods=['GET', 'POST'])
    @roles_required('admin', 'clinical', 'superadmin', 'scribe_supervisor')
    def edit_request(request_id):
        """Edit a PTO request (admin only) - adjusts balance if dates/type change"""
        pto_request = PTORequest.query.get_or_404(request_id)
        member = pto_request.member

        if request.method == 'POST':
            try:
                # Store old values for balance adjustment
                old_hours = pto_request.duration_hours
                old_type = pto_request.pto_type
                was_approved = pto_request.status in ['approved', 'in_progress', 'completed']

                # Get new values from form
                new_start_date = request.form.get('start_date')
                new_end_date = request.form.get('end_date')
                new_pto_type = request.form.get('pto_type')
                new_reason = request.form.get('reason', '')

                # If request was approved, we need to adjust balance
                if was_approved:
                    # Restore old balance first
                    if old_type == 'Sick':
                        member.sick_balance_hours = float(member.sick_balance_hours or 0) + old_hours
                    else:
                        member.pto_balance_hours = float(member.pto_balance_hours or 0) + old_hours

                # Update request fields
                pto_request.start_date = new_start_date
                pto_request.end_date = new_end_date
                pto_request.pto_type = new_pto_type
                pto_request.reason = new_reason
                pto_request.updated_at = get_eastern_time()

                # If request was approved, deduct new balance
                if was_approved:
                    new_hours = pto_request.duration_hours  # Recalculated based on new dates
                    if new_pto_type == 'Sick':
                        member.sick_balance_hours = float(member.sick_balance_hours or 0) - new_hours
                    else:
                        member.pto_balance_hours = float(member.pto_balance_hours or 0) - new_hours

                db.session.commit()

                # If this was an approved request, the scheduler has stale rows
                # for the old dates. Delete and re-upsert so it reflects the
                # edit. Delete is keyed on source_request_id, not on dates, so
                # it correctly removes the prior rows regardless of new dates.
                if was_approved:
                    try:
                        scheduler_sync.sync_pto_request(pto_request, 'delete')
                        scheduler_sync.sync_pto_request(pto_request, 'upsert')
                    except Exception as e:
                        print(f"Failed to sync edit to scheduler: {str(e)}")

                flash(f'PTO request for {member.name} has been updated successfully.', 'success')
                return redirect(url_for('dashboard'))

            except Exception as e:
                db.session.rollback()
                flash(f'Error updating request: {str(e)}', 'error')

        # GET request - show edit form
        return render_template('edit_request.html', request=pto_request, member=member)

    @app.route('/check_and_complete_requests')
    def check_and_complete_requests():
        """Check for PTO requests that should be marked as completed based on end date"""
        from datetime import datetime, date

        today = date.today()

        # Find approved requests where end date has passed
        approved_requests = PTORequest.query.filter_by(status='approved').all()

        completed_count = 0
        for request in approved_requests:
            try:
                end_date = datetime.strptime(request.end_date, '%Y-%m-%d').date()
                if end_date < today:
                    request.status = 'completed'
                    request.completed_date = get_eastern_time()
                    completed_count += 1
            except:
                pass

        if completed_count > 0:
            db.session.commit()
            flash(f'{completed_count} PTO requests marked as completed.', 'info')

        return redirect(url_for('dashboard'))

    @app.route('/logout')
    def logout():
        """Logout current user"""
        from auth import logout_user
        logout_user()
        flash('You have been logged out.', 'success')
        return redirect(url_for('index'))

    @app.route('/change-password', methods=['GET', 'POST'])
    @roles_required('admin', 'clinical', 'superadmin', 'moa_supervisor', 'echo_supervisor', 'scribe_supervisor')
    def change_password():
        """Allow managers to change their password"""
        from werkzeug.security import check_password_hash, generate_password_hash
        from database import db

        if request.method == 'POST':
            current_password = request.form.get('current_password')
            new_password = request.form.get('new_password')
            confirm_password = request.form.get('confirm_password')

            # Get current manager from session
            user_id = session.get('user_id')
            user = Manager.query.get(user_id)
            if not user:
                flash('User not found.', 'error')
                return redirect(url_for('change_password'))

            # Validate current password
            if not check_password_hash(user.password_hash, current_password):
                flash('Current password is incorrect.', 'error')
                return redirect(url_for('change_password'))

            # Validate new password length
            if len(new_password) < 8:
                flash('New password must be at least 8 characters long.', 'error')
                return redirect(url_for('change_password'))

            # Validate passwords match
            if new_password != confirm_password:
                flash('New passwords do not match.', 'error')
                return redirect(url_for('change_password'))

            # Update password
            user.password_hash = generate_password_hash(new_password)
            db.session.commit()

            flash('Password changed successfully!', 'success')
            return redirect(url_for('dashboard'))

        return render_template('change_password.html')

    # ========================================
    # SMS RECIPIENTS MANAGEMENT
    # ========================================

    @app.route('/sms-recipients')
    @roles_required('superadmin')
    def sms_recipients():
        """List all SMS recipients for call-out notifications"""
        recipients = SMSRecipient.query.order_by(
            SMSRecipient.team, SMSRecipient.name
        ).all()
        return render_template('sms_recipients.html', recipients=recipients)

    @app.route('/sms-recipients/add', methods=['POST'])
    @roles_required('superadmin')
    def add_sms_recipient():
        """Add a new SMS recipient"""
        name = (request.form.get('name') or '').strip()
        phone_raw = (request.form.get('phone') or '').strip()
        team = (request.form.get('team') or '').strip()

        if not name or not phone_raw or team not in ('admin', 'clinical', 'both'):
            flash('Name, phone, and team are all required.', 'error')
            return redirect(url_for('sms_recipients'))

        phone = SMSRecipient.normalize_phone(phone_raw)
        digits = ''.join(ch for ch in phone if ch.isdigit())
        if len(digits) < 10:
            flash('Phone number must be at least 10 digits.', 'error')
            return redirect(url_for('sms_recipients'))

        existing = SMSRecipient.query.filter_by(phone=phone, team=team).first()
        if existing:
            flash(f'{phone} is already listed for the {team} team.', 'error')
            return redirect(url_for('sms_recipients'))

        recipient = SMSRecipient(name=name, phone=phone, team=team, active=True)
        db.session.add(recipient)
        db.session.commit()
        flash(f'Added {name} ({phone}) to {team} notifications.', 'success')
        return redirect(url_for('sms_recipients'))

    @app.route('/sms-recipients/<int:recipient_id>/toggle', methods=['POST'])
    @roles_required('superadmin')
    def toggle_sms_recipient(recipient_id):
        """Toggle active status (pause/resume) for a recipient"""
        recipient = SMSRecipient.query.get_or_404(recipient_id)
        recipient.active = not recipient.active
        db.session.commit()
        state = 'resumed' if recipient.active else 'paused'
        flash(f'{recipient.name} {state}.', 'success')
        return redirect(url_for('sms_recipients'))

    @app.route('/sms-recipients/<int:recipient_id>/delete', methods=['POST'])
    @roles_required('superadmin')
    def delete_sms_recipient(recipient_id):
        """Permanently remove a recipient"""
        recipient = SMSRecipient.query.get_or_404(recipient_id)
        name = recipient.name
        db.session.delete(recipient)
        db.session.commit()
        flash(f'Removed {name} from SMS notifications.', 'success')
        return redirect(url_for('sms_recipients'))

    @app.errorhandler(404)
    def not_found_error(error):
        return render_template('404.html'), 404

    @app.errorhandler(500)
    def internal_error(error):
        db.session.rollback()
        return render_template('500.html'), 500