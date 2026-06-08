from database import db
from datetime import datetime
import pytz
from sqlalchemy import Column, Integer, String, DateTime, Text, ForeignKey, Boolean, Numeric, Date
from sqlalchemy.orm import relationship

# Define Eastern timezone
EASTERN = pytz.timezone('US/Eastern')

def get_eastern_time():
    """Get current time in Eastern timezone as naive datetime"""
    eastern_now = datetime.now(EASTERN)
    # Return naive datetime (no timezone info) but in Eastern time
    return eastern_now.replace(tzinfo=None)

class User(db.Model):
    """Base user class"""
    __tablename__ = 'users'
    
    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    email = Column(String(120), unique=True, nullable=False)
    phone = Column(String(20), nullable=True)  # Phone number field
    pin = Column(String(4), nullable=True)  # 4-digit PIN for phone authentication via Twilio
    pto_balance_hours = Column(Numeric(5,2), default=60.0)  # Current remaining PTO balance in hours
    sick_balance_hours = Column(Numeric(5,2), default=60.0)  # Current remaining sick time balance in hours
    starting_pto_hours = Column(Numeric(5,2), default=60.0)  # Annual PTO allocation (starting balance)
    starting_sick_hours = Column(Numeric(5,2), default=60.0)  # Annual sick time allocation (starting balance)
    pto_refresh_date = Column(Date, default=datetime(2025, 1, 1).date())    # Annual refresh date
    pto_blocked = Column(Boolean, default=False)  # If True, employee can only submit call-outs, not PTO
    created_at = Column(DateTime, default=get_eastern_time)
    
    def __init__(self, name=None, email=None, **kwargs):
        super().__init__(**kwargs)
        if name:
            self.name = name
        if email:
            self.email = email
    
    @property
    def pto_balance_days(self):
        """Convert PTO hours to days (7.5 hours = 1 day)"""
        return round(float(self.pto_balance_hours) / 7.5, 1)

    @property
    def sick_balance_days(self):
        """Convert sick hours to days (7.5 hours = 1 day)"""
        return round(float(self.sick_balance_hours) / 7.5, 1)

    def get_remaining_pto_hours(self):
        """Calculate remaining PTO hours - balance is now deducted immediately upon approval"""
        # PTO balance is now deducted immediately upon approval, so just return current balance
        # Note: Balance already reflects deducted hours from approved/in-progress requests
        return max(0, float(self.pto_balance_hours))

    def get_remaining_pto_days(self):
        """Calculate remaining PTO in days"""
        return round(self.get_remaining_pto_hours() / 7.5, 1)

    def get_remaining_sick_hours(self):
        """Calculate remaining sick time hours"""
        return max(0, float(self.sick_balance_hours))

    def get_remaining_sick_days(self):
        """Calculate remaining sick time in days"""
        return round(self.get_remaining_sick_hours() / 7.5, 1)

    @property
    def starting_pto_days(self):
        """Convert starting PTO hours to days"""
        try:
            hours = getattr(self, 'starting_pto_hours', None) or 60.0
            return round(float(hours) / 7.5, 1)
        except Exception:
            return 8.0

    @property
    def starting_sick_days(self):
        """Convert starting sick hours to days"""
        try:
            hours = getattr(self, 'starting_sick_hours', None) or 60.0
            return round(float(hours) / 7.5, 1)
        except Exception:
            return 8.0

    @property
    def pto_used_hours(self):
        """Calculate PTO hours used (starting - remaining)"""
        try:
            starting = float(getattr(self, 'starting_pto_hours', None) or 60.0)
            remaining = float(self.pto_balance_hours or 0)
            return max(0, starting - remaining)
        except Exception:
            return 0.0

    @property
    def pto_used_days(self):
        """Calculate PTO days used"""
        return round(self.pto_used_hours / 7.5, 1)

    @property
    def sick_used_hours(self):
        """Calculate sick hours used (starting - remaining)"""
        try:
            starting = float(getattr(self, 'starting_sick_hours', None) or 60.0)
            remaining = float(self.sick_balance_hours or 0)
            return max(0, starting - remaining)
        except Exception:
            return 0.0

    @property
    def sick_used_days(self):
        """Calculate sick days used"""
        return round(self.sick_used_hours / 7.5, 1)

    def refresh_pto_balance(self, new_balance_hours=60.0):
        """Reset PTO balance to new amount (usually done annually)"""
        from database import db
        self.pto_balance_hours = new_balance_hours
        db.session.commit()
    
    def __repr__(self):
        return f'<User {self.name}>'

class Position(db.Model):
    __tablename__ = 'positions'
    id = Column(Integer, primary_key=True)
    name = Column(String(100), unique=True, nullable=False)
    team = Column(String(20), nullable=False)  # 'admin', 'clinical', 'scribes', or 'research'

    def __repr__(self):
        return f'<Position {self.name}>'

    def __str__(self):
        return self.name

class TeamMember(User):
    """Team member inherits from User"""
    __tablename__ = 'team_members'

    id = Column(Integer, ForeignKey('users.id'), primary_key=True)
    position_id = Column(Integer, ForeignKey('positions.id'), nullable=False)
    position = relationship("Position")

    def __init__(self, name=None, email=None, position_id=None, **kwargs):
        super().__init__(name=name, email=email, **kwargs)
        if position_id:
            self.position_id = position_id
    
    # Relationship to PTO requests
    pto_requests = relationship("PTORequest", back_populates="member")
    
    @property
    def team(self):
        return self.position.team if self.position else None

    @property
    def manager_team(self):
        """Determine which manager should handle this team member's requests"""
        return self.team
    
    def __repr__(self):
        return f'<TeamMember {self.name} - {self.team}>'

class Manager(User):
    """Manager class for approving requests"""
    __tablename__ = 'managers'
    
    id = Column(Integer, ForeignKey('users.id'), primary_key=True)
    role = Column(String(30), nullable=False)  # 'admin', 'clinical', 'superadmin', 'moa_supervisor', 'echo_supervisor', 'scribe_supervisor', 'research_supervisor', 'scribe_research_supervisor'
    password_hash = Column(String(256))  # For authentication
    
    def __init__(self, name=None, email=None, role=None, password_hash=None, **kwargs):
        super().__init__(name=name, email=email, **kwargs)
        if role:
            self.role = role
        if password_hash:
            self.password_hash = password_hash
    
    # Legacy compatibility - keep 'team' as property for existing code
    @property
    def team(self):
        return self.role
    
    def can_approve_position(self, position):
        """Check if this manager can approve requests for a specific position"""
        if self.role == 'superadmin':
            return True
        
        if not isinstance(position, Position):
            return False

        if self.role == 'admin' and position.team == 'admin':
            return True
        if self.role == 'clinical' and position.team == 'clinical':
            return True
        if self.role == 'moa_supervisor' and 'MOA' in position.name:
            return True
        if self.role == 'echo_supervisor' and ('Echo' in position.name or 'Vascular' in position.name):
            return True
        if self.role == 'scribe_supervisor' and position.team == 'scribes':
            return True
        if self.role == 'research_supervisor' and position.team == 'research':
            return True
        if self.role == 'scribe_research_supervisor' and position.team in ('scribes', 'research'):
            return True
        return False
    
    def __repr__(self):
        return f'<Manager {self.name} - {self.role}>'

class PTORequest(db.Model):
    """PTO Request model"""
    __tablename__ = 'pto_requests'
    
    id = Column(Integer, primary_key=True)
    member_id = Column(Integer, ForeignKey('team_members.id'), nullable=False)
    start_date = Column(String(10), nullable=False)  # YYYY-MM-DD format
    end_date = Column(String(10), nullable=False)
    pto_type = Column(String(50), nullable=False)  # vacation, sick, etc.
    status = Column(String(20), default='pending')  # pending, in_progress, approved, denied, completed
    manager_team = Column(String(20), nullable=False)  # which manager should handle this
    denial_reason = Column(Text)
    
    # Partial PTO support
    is_partial_day = Column(Boolean, default=False)  # True for partial day requests
    start_time = Column(String(8))  # HH:MM format for partial days
    end_time = Column(String(8))  # HH:MM format for partial days
    reason = Column(Text)  # Optional reason (especially for partial days)

    # Call Out tracking
    is_call_out = Column(Boolean, default=False)  # True for call-out (today only) requests
    callout_classification = Column(String(10))  # 'sick' or 'fmla' (only meaningful when is_call_out=True)

    # Workflow tracking
    timekeeping_entered = Column(Boolean, default=False)  # Checkbox for timekeeping
    coverage_arranged = Column(Boolean, default=False)  # Checkbox for coverage
    approved_date = Column(DateTime)  # When request was first approved
    completed_date = Column(DateTime)  # When PTO period ended
    
    created_at = Column(DateTime, default=get_eastern_time)
    updated_at = Column(DateTime, default=get_eastern_time, onupdate=get_eastern_time)
    submitted_at = Column(DateTime, default=get_eastern_time)  # For tracking submission time
    
    def __init__(self, member=None, start_date=None, end_date=None, pto_type=None,
                 manager_team=None, status='pending', coverage_arranged=False,
                 timekeeping_entered=False, is_partial_day=False,
                 start_time=None, end_time=None, reason=None, is_call_out=False, **kwargs):
        super().__init__(**kwargs)
        if member:
            self.member = member
        if start_date:
            self.start_date = start_date
        if end_date:
            self.end_date = end_date
        if pto_type:
            self.pto_type = pto_type
        if manager_team:
            self.manager_team = manager_team
        self.status = status
        self.coverage_arranged = coverage_arranged
        self.timekeeping_entered = timekeeping_entered
        self.is_partial_day = is_partial_day
        self.start_time = start_time
        self.end_time = end_time
        self.reason = reason
        self.is_call_out = is_call_out
    
    # Relationships
    member = relationship("TeamMember", back_populates="pto_requests")
    
    @property
    def duration_days(self):
        """Calculate duration in business days (excludes weekends and holidays).
        For partial day requests, returns the fractional equivalent (hours / 7.5)
        so balance deductions and dashboard stats account for partials correctly."""
        if self.is_partial_day and self.start_time and self.end_time:
            try:
                return round(self.duration_hours / 7.5, 2)
            except (ValueError, TypeError, ZeroDivisionError):
                pass
        try:
            from business_days import calculate_pto_days
            return calculate_pto_days(self.start_date, self.end_date)
        except (ValueError, TypeError, ImportError):
            # Fallback to calendar days if business_days module fails
            try:
                start = datetime.strptime(self.start_date, '%Y-%m-%d')
                end = datetime.strptime(self.end_date, '%Y-%m-%d')
                return (end - start).days + 1
            except (ValueError, TypeError):
                return 1
    
    @property
    def duration_hours(self):
        """Calculate duration in hours (7.5 hours = 1 business day)"""
        try:
            if self.is_partial_day and self.start_time and self.end_time:
                # Calculate partial day hours
                start_hour, start_min = map(int, self.start_time.split(':'))
                end_hour, end_min = map(int, self.end_time.split(':'))

                start_minutes = start_hour * 60 + start_min
                end_minutes = end_hour * 60 + end_min

                total_minutes = end_minutes - start_minutes
                return round(total_minutes / 60, 2)
            else:
                # Full day calculation: 7.5 hours per business day
                return self.duration_days * 7.5
        except:
            return 7.5

    def get_pto_breakdown(self):
        """Get detailed breakdown of PTO request including holidays and weekends.
        For partial day requests, returns a single-day breakdown scaled by the
        partial fraction so business_days reflects hours / 7.5."""
        if self.is_partial_day and self.start_time and self.end_time:
            try:
                fraction = round(self.duration_hours / 7.5, 2)
                return {
                    'total_days': fraction,
                    'business_days': fraction,
                    'weekend_days': 0,
                    'holiday_days': 0,
                    'holidays_list': [],
                    'weekends_list': []
                }
            except (ValueError, TypeError, ZeroDivisionError):
                pass
        try:
            from business_days import get_pto_breakdown
            return get_pto_breakdown(self.start_date, self.end_date)
        except (ImportError, ValueError, TypeError):
            # Fallback breakdown
            try:
                start = datetime.strptime(self.start_date, '%Y-%m-%d')
                end = datetime.strptime(self.end_date, '%Y-%m-%d')
                total_days = (end - start).days + 1
                return {
                    'total_days': total_days,
                    'business_days': total_days,  # Fallback assumes all are business days
                    'weekend_days': 0,
                    'holiday_days': 0,
                    'holidays_list': [],
                    'weekends_list': []
                }
            except (ValueError, TypeError):
                return {
                    'total_days': 1,
                    'business_days': 1,
                    'weekend_days': 0,
                    'holiday_days': 0,
                    'holidays_list': [],
                    'weekends_list': []
                }

    def __repr__(self):
        return f'<PTORequest {self.id} - {self.member.name if self.member else "Unknown"} - {self.status}>'

class PendingEmployee(db.Model):
    """Model for employees pending manager approval"""
    __tablename__ = 'pending_employees'
    
    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    email = Column(String(120), nullable=False)
    team = Column(String(20), nullable=False)  # 'admin', 'clinical', 'scribes', or 'research'
    position = Column(String(50), nullable=False)
    status = Column(String(20), default='pending')  # pending, approved, denied
    submitted_at = Column(DateTime, default=get_eastern_time)
    approved_at = Column(DateTime)
    approved_by_id = Column(Integer, ForeignKey('managers.id'))
    denial_reason = Column(Text)
    
    # Initial PTO balance requested (default 60 hours)
    requested_pto_balance = Column(Numeric(5,2), default=60.0)
    
    def __init__(self, name=None, email=None, team=None, position=None, **kwargs):
        super().__init__(**kwargs)
        if name:
            self.name = name
        if email:
            self.email = email
        if team:
            self.team = team
        if position:
            self.position = position
    
    # Relationship to approving manager
    approved_by = relationship("Manager", foreign_keys=[approved_by_id])

    def __repr__(self):
        return f'<PendingEmployee {self.name} - {self.status}>'

class CallOutRecord(db.Model):
    """Model for tracking SMS call-out submissions via Twilio"""
    __tablename__ = 'call_out_records'

    id = Column(Integer, primary_key=True)
    member_id = Column(Integer, ForeignKey('team_members.id'), nullable=False)
    pto_request_id = Column(Integer, ForeignKey('pto_requests.id'), nullable=True)

    # Twilio tracking information
    call_sid = Column(String(100), nullable=True)  # Twilio message SID

    # Call-out metadata
    source = Column(String(10), nullable=False)  # 'sms'
    phone_number_used = Column(String(20), nullable=False)  # Sender's phone number
    verified = Column(Boolean, default=False)  # Whether authentication succeeded
    authentication_method = Column(String(20), nullable=True)  # 'phone_match' or 'manual'

    # Message content
    message_text = Column(Text, nullable=True)  # SMS text message

    # Timestamps
    created_at = Column(DateTime, default=get_eastern_time)
    processed_at = Column(DateTime, nullable=True)

    def __init__(self, member_id=None, source=None, phone_number_used=None, **kwargs):
        super().__init__(**kwargs)
        if member_id:
            self.member_id = member_id
        if source:
            self.source = source
        if phone_number_used:
            self.phone_number_used = phone_number_used

    # Relationships
    member = relationship("TeamMember", backref="call_out_records")
    pto_request = relationship("PTORequest", backref="call_out_record", uselist=False)

    def __repr__(self):
        return f'<CallOutRecord {self.id} - {self.source} - {self.member.name if self.member else "Unknown"}>'

class TardinessRecord(db.Model):
    """Model for tracking employee tardiness"""
    __tablename__ = 'tardiness_records'

    id = Column(Integer, primary_key=True)
    member_id = Column(Integer, ForeignKey('team_members.id'), nullable=False)

    # Tardiness details
    date = Column(String(10), nullable=False)  # YYYY-MM-DD format
    minutes_late = Column(Integer, nullable=False)  # How many minutes late
    reason = Column(Text, nullable=True)  # Optional reason/notes

    # Who recorded it
    recorded_by_id = Column(Integer, ForeignKey('managers.id'), nullable=True)

    # Timestamps
    created_at = Column(DateTime, default=get_eastern_time)

    def __init__(self, member_id=None, date=None, minutes_late=None, reason=None, recorded_by_id=None, **kwargs):
        super().__init__(**kwargs)
        if member_id:
            self.member_id = member_id
        if date:
            self.date = date
        if minutes_late:
            self.minutes_late = minutes_late
        if reason:
            self.reason = reason
        if recorded_by_id:
            self.recorded_by_id = recorded_by_id

    # Relationships
    member = relationship("TeamMember", backref="tardiness_records")
    recorded_by = relationship("Manager", foreign_keys=[recorded_by_id])

    def __repr__(self):
        return f'<TardinessRecord {self.id} - {self.member.name if self.member else "Unknown"} - {self.date}>'

class SMSRecipient(db.Model):
    """Phone numbers that receive call-out SMS notifications"""
    __tablename__ = 'sms_recipients'

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    phone = Column(String(20), nullable=False)
    team = Column(String(20), nullable=False)  # 'admin', 'clinical', or 'both'
    active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=get_eastern_time)

    def __repr__(self):
        return f'<SMSRecipient {self.name} {self.phone} team={self.team} active={self.active}>'

    @staticmethod
    def normalize_phone(raw):
        """Normalize a phone number to +1XXXXXXXXXX format"""
        if not raw:
            return ''
        digits = ''.join(ch for ch in raw if ch.isdigit())
        if len(digits) == 10:
            return f'+1{digits}'
        if len(digits) == 11 and digits.startswith('1'):
            return f'+{digits}'
        # Fall back to original if unexpected length
        return raw.strip()