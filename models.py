from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timedelta

db = SQLAlchemy()

# ─── Overdraft State Machine ───────────────────────────────────────────────────
#
# OVERDRAFT_REQUESTED  →  Initial request recorded, risk check pending
# RISK_APPROVED        →  Risk engine approved the credit limit
# PAYOUT_PENDING       →  Payouts API call dispatched, waiting for confirmation
# PAYOUT_SUCCESS       →  Funds landed in buyer's PayPal wallet (webhook confirmed)
# CHECKOUT_COMPLETED   →  Buyer completed PayPal checkout, merchant received payment
# REPAYMENT_PENDING    →  Awaiting next incoming payment to auto-repay
# REPAYMENT_RECEIVED   →  Auto-repayment processed (webhook triggered)
# CLOSED               →  Fully settled
#
# Terminal failure states:
# DECLINED             →  Risk engine or limit check declined
# PAYOUT_FAILED        →  Payouts API returned a failure
# DEFAULTED            →  Overdraft not repaid within DEFAULT_DAYS
#
# Legacy aliases (kept for backward compat):
#   ACTIVE   ≈  PAYOUT_SUCCESS
#   REPAID   ≈  REPAYMENT_RECEIVED

# States that count as "active overdraft" (buyer still owes PayFlow)
ACTIVE_OVERDRAFT_STATES = {
    'OVERDRAFT_REQUESTED',
    'RISK_APPROVED',
    'PAYOUT_PENDING',
    'PAYOUT_SUCCESS',
    'ACTIVE',           # legacy
    'CHECKOUT_COMPLETED',
    'REPAYMENT_PENDING',
}


class User(db.Model):
    """Gig worker / micro-merchant PayPal account."""
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, default='Gig Worker')
    paypal_email = db.Column(db.String(200), unique=True, nullable=False)
    paypal_account_id = db.Column(db.String(200), unique=True, nullable=True)
    risk_tier = db.Column(db.String(10), default='LOW')   # LOW / MEDIUM / HIGH
    overdraft_limit = db.Column(db.Float, default=0.0)
    simulated_balance = db.Column(db.Float, default=15.0)  # Demo wallet balance ($15 for the demo scenario)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.now)

    overdraft_buffers = db.relationship('OverdraftBuffer', backref='user', lazy=True)
    transaction_history = db.relationship('TransactionHistory', backref='user', lazy=True)

    @property
    def thirty_day_income(self):
        """Sum of all incoming transactions in the past 30 days."""
        cutoff = datetime.now() - timedelta(days=30)
        total = db.session.query(
            db.func.sum(TransactionHistory.amount)
        ).filter(
            TransactionHistory.user_id == self.id,
            TransactionHistory.direction == 'IN',
            TransactionHistory.created_at >= cutoff
        ).scalar()
        return float(total or 0.0)

    @property
    def computed_overdraft_limit(self):
        """Micro-risk formula: min($500, 25% × 30d_income)."""
        from flask import current_app
        max_limit = current_app.config.get('MAX_OVERDRAFT_LIMIT', 500.0)
        ratio = current_app.config.get('OVERDRAFT_INCOME_RATIO', 0.25)
        return min(max_limit, self.thirty_day_income * ratio)

    @property
    def active_overdraft(self):
        """Return the first in-flight overdraft buffer, if any."""
        return OverdraftBuffer.query.filter(
            OverdraftBuffer.user_id == self.id,
            OverdraftBuffer.status.in_(ACTIVE_OVERDRAFT_STATES)
        ).first()

    def __repr__(self):
        return f'<User {self.paypal_email} tier={self.risk_tier}>'


class OverdraftBuffer(db.Model):
    """
    Tracks a single overdraft credit event from request through repayment.
    Implements the PayFlow state machine:
        OVERDRAFT_REQUESTED → RISK_APPROVED → PAYOUT_PENDING → PAYOUT_SUCCESS
        → CHECKOUT_COMPLETED → REPAYMENT_PENDING → REPAYMENT_RECEIVED → CLOSED
    """
    __tablename__ = 'overdraft_buffers'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    order_id = db.Column(db.String(200), nullable=True)           # PayPal Order ID (set at checkout)
    payout_batch_id = db.Column(db.String(200), nullable=True)    # PayPal Payout Batch ID
    payout_item_id = db.Column(db.String(200), nullable=True)     # Individual payout item ID (for status polling)
    amount_covered = db.Column(db.Float, nullable=False)           # Shortfall injected ($25)
    flat_fee = db.Column(db.Float, default=0.50)                  # Service fee ($0.50)
    total_owed = db.Column(db.Float, nullable=False)               # amount_covered + flat_fee ($25.50)
    status = db.Column(db.String(30), default='OVERDRAFT_REQUESTED')  # See state machine above
    created_at = db.Column(db.DateTime, default=datetime.now)
    due_date = db.Column(db.DateTime, default=lambda: datetime.now() + timedelta(days=30))
    repaid_at = db.Column(db.DateTime, nullable=True)

    repayment_logs = db.relationship('RepaymentLog', backref='overdraft', lazy=True)

    @property
    def days_overdue(self):
        if self.status not in ('REPAYMENT_PENDING', 'CHECKOUT_COMPLETED', 'ACTIVE', 'PAYOUT_SUCCESS'):
            return 0
        delta = datetime.now() - self.due_date
        return max(0, delta.days)

    @property
    def is_settled(self):
        """True if the overdraft is fully resolved (no longer owed)."""
        return self.status in ('REPAYMENT_RECEIVED', 'CLOSED', 'REPAID', 'DEFAULTED')

    def advance_state(self, new_status: str):
        """Advance the state machine, logging the transition."""
        from flask import current_app
        current_app.logger.info(
            f"OverdraftBuffer #{self.id}: {self.status} → {new_status}"
        )
        self.status = new_status

    def __repr__(self):
        return f'<OverdraftBuffer ${self.amount_covered} status={self.status}>'


class TransactionHistory(db.Model):
    """Historical PayPal transactions used for income scoring."""
    __tablename__ = 'transaction_history'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    paypal_tx_id = db.Column(db.String(200), unique=True, nullable=True)
    amount = db.Column(db.Float, nullable=False)
    currency = db.Column(db.String(10), default='USD')
    direction = db.Column(db.String(5), nullable=False)  # IN / OUT
    description = db.Column(db.String(300), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.now)

    def __repr__(self):
        return f'<Transaction {self.direction} ${self.amount}>'


class RepaymentLog(db.Model):
    """Audit trail of webhook-triggered overdraft repayments."""
    __tablename__ = 'repayment_logs'

    id = db.Column(db.Integer, primary_key=True)
    overdraft_id = db.Column(db.Integer, db.ForeignKey('overdraft_buffers.id'), nullable=False)
    webhook_event_id = db.Column(db.String(200), unique=True, nullable=True)  # Idempotency key
    repaid_amount = db.Column(db.Float, nullable=False)
    trigger = db.Column(db.String(50), default='WEBHOOK')  # WEBHOOK / MANUAL
    repaid_at = db.Column(db.DateTime, default=datetime.now)

    def __repr__(self):
        return f'<RepaymentLog ${self.repaid_amount} at {self.repaid_at}>'
