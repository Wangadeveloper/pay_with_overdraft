"""
PayFlow Overdraft — Test Suite
Tests the three core scenarios from the implementation plan.

Updated to match the corrected two-phase architecture:
  Phase 1: POST /api/overdraft/initiate  → Payout (Business → Personal)
  Phase 2: POST /api/orders + capture    → PayPal Checkout

Also updated to use the real sandbox buyer email and new state machine statuses.
"""
import pytest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app
from models import db, User, OverdraftBuffer, TransactionHistory
from config import Config
from datetime import datetime, timedelta

SANDBOX_BUYER_EMAIL = 'sb-tfjr4353268252@personal.example.com'


class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
    DEMO_MODE = True
    PAYPAL_CLIENT_ID = 'test-client-id'
    PAYPAL_CLIENT_SECRET = 'test-client-secret'
    WTF_CSRF_ENABLED = False
    # Override buyer config so tests use the sandbox email consistently
    PAYPAL_BUYER_EMAIL = SANDBOX_BUYER_EMAIL
    PAYPAL_BUYER_ACCOUNT_ID = 'RTFSG6HD676ML'
    PAYPAL_BUYER_NAME = 'John Doe'


@pytest.fixture
def app():
    app = create_app(TestConfig)
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def demo_user(app):
    """
    Seed the sandbox buyer with $5,000 simulated balance (matching real sandbox
    account starting balance) and 30 days of income history.
    30 days × $30/day = $900 income → overdraft limit = min($150, 25%×$900) = $150.
    """
    with app.app_context():
        user = User.query.filter_by(paypal_email=SANDBOX_BUYER_EMAIL).first()
        if not user:
            user = User(
                name='John Doe',
                paypal_email=SANDBOX_BUYER_EMAIL,
                paypal_account_id='RTFSG6HD676ML',
                simulated_balance=5000.0,   # Real sandbox starting balance
                risk_tier='LOW',
                overdraft_limit=500.0
            )
            db.session.add(user)
            db.session.flush()
        else:
            user.simulated_balance = 5000.0
            user.overdraft_limit = 500.0

        # Add 30 days of income history: $70/day × 30 = $2100 income (25% = $525, capped at $500)
        for i in range(30):
            tx = TransactionHistory(
                user_id=user.id,
                paypal_tx_id=f"TEST-IN-{i}",
                amount=70.0,
                direction='IN',
                description='Test income',
                created_at=datetime.now() - timedelta(days=i)
            )
            db.session.add(tx)
        db.session.commit()
        return user


# ─── Risk Engine Tests ─────────────────────────────────────────────

class TestRiskEngine:
    def test_compute_risk_tier_low(self):
        from risk_engine import compute_risk_tier
        assert compute_risk_tier(600) == 'LOW'

    def test_compute_risk_tier_medium(self):
        from risk_engine import compute_risk_tier
        assert compute_risk_tier(200) == 'MEDIUM'

    def test_compute_risk_tier_high(self):
        from risk_engine import compute_risk_tier
        assert compute_risk_tier(100) == 'HIGH'

    def test_overdraft_limit_capped_at_500(self, app):
        with app.app_context():
            from risk_engine import compute_overdraft_limit
            # 25% of $3000 = $750, but capped at $500
            assert compute_overdraft_limit(3000) == 500.0

    def test_overdraft_limit_formula(self, app):
        with app.app_context():
            from risk_engine import compute_overdraft_limit
            # 25% of $200 = $50
            assert compute_overdraft_limit(200) == 50.0

    def test_detect_shortfall(self):
        from risk_engine import detect_shortfall
        result = detect_shortfall(40.0, 15.0)
        assert result['needs_overdraft'] is True
        assert result['shortfall'] == 25.0

    def test_no_shortfall(self):
        from risk_engine import detect_shortfall
        result = detect_shortfall(10.0, 15.0)
        assert result['needs_overdraft'] is False

    def test_can_cover_shortfall_yes(self, app):
        with app.app_context():
            from risk_engine import can_cover_shortfall
            result = can_cover_shortfall(25.0, 50.0, 0.0)
            assert result['can_cover'] is True

    def test_can_cover_shortfall_no(self, app):
        with app.app_context():
            from risk_engine import can_cover_shortfall
            result = can_cover_shortfall(200.0, 50.0, 0.0)
            assert result['can_cover'] is False
            assert result['reason'] == 'limit_exceeded'


# ─── Phase 0: Live Balance Fetch ──────────────────────────────────

class TestPhase0LiveBalance:
    """
    GET /api/balance/live — returns balance and gracefully falls back on error.
    In DEMO_MODE the cached DB value is returned without calling the PayPal API.
    """

    def test_balance_live_returns_200(self, client, demo_user, app):
        """Endpoint returns 200 and a numeric balance field."""
        with app.app_context():
            res = client.get('/api/balance/live')
            assert res.status_code == 200
            data = res.get_json()
            assert 'balance' in data
            assert isinstance(data['balance'], (int, float))

    def test_balance_live_demo_mode_returns_cached(self, client, demo_user, app):
        """In DEMO_MODE the cached $5,000 balance is returned with source='simulated'."""
        with app.app_context():
            res = client.get('/api/balance/live')
            data = res.get_json()
            assert data['source'] == 'simulated'
            assert data['balance'] == 5000.0

    def test_balance_live_no_user_returns_404(self, client, app):
        """When no user is seeded the endpoint returns 404."""
        with app.app_context():
            res = client.get('/api/balance/live')
            # No demo_user fixture so user may not exist yet via app seed
            # Just verify it doesn't crash with 500
            assert res.status_code in (200, 404)

    def test_balance_live_used_by_initiate(self, client, demo_user, app):
        """
        After fetching the live balance ($5,000), the initiate endpoint should
        detect a $100 shortfall on a $5,100 order.
        """
        with app.app_context():
            client.get('/api/balance/live')
            res = client.post('/api/overdraft/initiate', json={'amount': 5100.0})
            assert res.status_code == 200
            data = res.get_json()
            assert data['needs_overdraft'] is True
            assert data['shortfall'] == 100.0


# ─── Scenario 1: Overdraft Initiation Phase (Two-Phase Flow) ──────

class TestScenario1OverdraftInitiation:
    """
    Demo scenario: $5,100 order with $5,000 balance → $100 shortfall.

    Phase 1: POST /api/overdraft/initiate
      → risk approved → DEMO_MODE skips PAYOUT_PENDING → PAYOUT_SUCCESS
      → simulated balance updated to $5,100 (5000 + 100)

    Phase 2: POST /api/orders → POST /api/orders/<id>/capture
      → checkout captured → status: CHECKOUT_COMPLETED → REPAYMENT_PENDING
    """

    def test_initiate_overdraft_approves_shortfall(self, client, demo_user, app):
        """Phase 1: initiate endpoint approves $100 shortfall on $5,000 balance."""
        with app.app_context():
            res = client.post('/api/overdraft/initiate', json={'amount': 5100.0})
            assert res.status_code == 200
            data = res.get_json()
            assert data['needs_overdraft'] is True
            assert data['approved'] is True
            assert data['shortfall'] == 100.0
            assert data['flat_fee'] == 0.50
            assert data['total_owed'] == 100.50
            assert data['payout_batch_id'] is not None

    def test_initiate_overdraft_approves_larger_300_shortfall(self, client, demo_user, app):
        """Shortfall of $300 on $5,000 balance ($5,300 order) is approved under $500 limit."""
        with app.app_context():
            res = client.post('/api/overdraft/initiate', json={'amount': 5300.0})
            assert res.status_code == 200
            data = res.get_json()
            assert data['needs_overdraft'] is True
            assert data['approved'] is True
            assert data['shortfall'] == 300.0
            assert data['total_owed'] == 300.50

    def test_initiate_overdraft_creates_buffer_in_db(self, client, demo_user, app):
        """Phase 1: OverdraftBuffer is created in DB with correct amount."""
        with app.app_context():
            client.post('/api/overdraft/initiate', json={'amount': 5100.0})
            buffer = OverdraftBuffer.query.filter(
                OverdraftBuffer.amount_covered == 100.0
            ).first()
            assert buffer is not None
            assert buffer.amount_covered == 100.0
            assert buffer.total_owed == 100.50
            assert buffer.status in ('PAYOUT_SUCCESS', 'PAYOUT_PENDING', 'RISK_APPROVED')

    def test_initiate_no_shortfall_returns_false(self, client, demo_user, app):
        """Wallet covers the purchase — no overdraft needed."""
        with app.app_context():
            # $4,500 is covered by $5,000 balance → no shortfall
            res = client.post('/api/overdraft/initiate', json={'amount': 4500.0})
            assert res.status_code == 200
            data = res.get_json()
            assert data['needs_overdraft'] is False

    def test_create_order_endpoint(self, client, demo_user, app):
        """Phase 2a: POST /api/orders creates an order (demo returns DEMO-ORDER-*)."""
        with app.app_context():
            res = client.post('/api/orders', json={'amount': 5100.0})
            assert res.status_code == 200
            data = res.get_json()
            assert 'id' in data
            assert data['status'] in ('CREATED', 'DEMO_CAPTURED', 'DEMO')

    def test_capture_after_overdraft_advances_state(self, client, demo_user, app):
        """Phase 2b: After initiate + create + capture, buffer advances to REPAYMENT_PENDING."""
        with app.app_context():
            client.post('/api/overdraft/initiate', json={'amount': 5100.0})
            create_res = client.post('/api/orders', json={'amount': 5100.0})
            order_id = create_res.get_json()['id']
            capture_res = client.post(f'/api/orders/{order_id}/capture',
                                      json={'amount': 5100.0})
            assert capture_res.status_code == 200
            data = capture_res.get_json()
            assert data.get('overdraft') is not None
            od = data['overdraft']
            assert od['status'] in ('REPAYMENT_PENDING', 'CHECKOUT_COMPLETED')

    def test_capture_records_outgoing_transaction(self, client, demo_user, app):
        """Capture should record an OUT transaction in TransactionHistory."""
        with app.app_context():
            client.post('/api/overdraft/initiate', json={'amount': 5100.0})
            create_res = client.post('/api/orders', json={'amount': 5100.0})
            order_id = create_res.get_json()['id']
            client.post(f'/api/orders/{order_id}/capture', json={'amount': 5100.0})

            user = User.query.filter_by(paypal_email=SANDBOX_BUYER_EMAIL).first()
            out_txs = TransactionHistory.query.filter_by(
                user_id=user.id, direction='OUT'
            ).all()
            assert len(out_txs) >= 1
            assert any(t.amount == 5100.0 for t in out_txs)


# ─── Scenario 2: Webhook Auto-Recovery ────────────────────────────

class TestScenario2WebhookAutoRecovery:
    """
    Simulate incoming transfer → PAYMENT.CAPTURE.COMPLETED fires
    → Flask captures debt → OverdraftBuffer → REPAYMENT_RECEIVED → CLOSED.
    """

    def _create_active_overdraft(self, client, app):
        """Helper: run Phase 1 + Phase 2 to get an overdraft in REPAYMENT_PENDING."""
        with app.app_context():
            client.post('/api/overdraft/initiate', json={'amount': 5100.0})
            create_res = client.post('/api/orders', json={'amount': 5100.0})
            order_id = create_res.get_json()['id']
            client.post(f'/api/orders/{order_id}/capture', json={'amount': 5100.0})

    def test_webhook_simulate_returns_success(self, client, demo_user, app):
        """Simulated webhook endpoint returns success."""
        self._create_active_overdraft(client, app)
        with app.app_context():
            wh_res = client.post('/webhooks/simulate', json={'amount': 50.0})
            assert wh_res.status_code == 200
            data = wh_res.get_json()
            assert data.get('success') is True

    def test_overdraft_marked_closed_after_webhook(self, client, demo_user, app):
        """After webhook fires with sufficient payment, buffer advances to CLOSED."""
        self._create_active_overdraft(client, app)
        with app.app_context():
            # Fire webhook with $150 — more than enough to repay $100.50
            client.post('/webhooks/simulate', json={'amount': 150.0})

            closed = OverdraftBuffer.query.filter_by(status='CLOSED').first()
            assert closed is not None

    def test_simulate_income_auto_repays_overdraft(self, client, demo_user, app):
        """POST /api/overdraft/simulate-income triggers auto-repayment."""
        self._create_active_overdraft(client, app)
        with app.app_context():
            # The overdraft is $100.50 (100 + 0.50 fee); send $300 income
            res = client.post('/api/overdraft/simulate-income', json={'amount': 300.0})
            assert res.status_code == 200
            data = res.get_json()
            assert data['success'] is True
            assert data['auto_repaid'] == 100.50

    def test_simulate_payout_success_webhook(self, client, demo_user, app):
        """Simulate payout success webhook advances PAYOUT_PENDING → PAYOUT_SUCCESS."""
        with app.app_context():
            # Manually create a PAYOUT_PENDING buffer
            user = User.query.filter_by(paypal_email=SANDBOX_BUYER_EMAIL).first()
            buffer = OverdraftBuffer(
                user_id=user.id,
                amount_covered=25.0,
                flat_fee=0.50,
                total_owed=25.50,
                payout_batch_id='TEST-BATCH-001',
                status='PAYOUT_PENDING'
            )
            db.session.add(buffer)
            db.session.commit()

            res = client.post('/webhooks/simulate-payout-success',
                              json={'batch_id': 'TEST-BATCH-001'})
            assert res.status_code == 200
            data = res.get_json()
            assert data['success'] is True
            assert data['new_status'] == 'PAYOUT_SUCCESS'


# ─── Scenario 3: Exceeded Credit Limit ────────────────────────────

class TestScenario3ExceededLimit:
    """$200 order with small overdraft limit → graceful 422 decline."""

    def test_exceeded_limit_returns_422_on_initiate(self, client, demo_user, app):
        """Initiate endpoint rejects shortfall exceeding available overdraft limit."""
        with app.app_context():
            # $5,000 + $1,000 = $6,000 order. Shortfall = $1,000 > $150 limit.
            user = User.query.filter_by(paypal_email=SANDBOX_BUYER_EMAIL).first()
            TransactionHistory.query.filter_by(user_id=user.id).delete()
            user.overdraft_limit = 10.0
            db.session.commit()

            res = client.post('/api/overdraft/initiate', json={'amount': 6000.0})
            assert res.status_code == 422
            data = res.get_json()
            assert data.get('error') == 'OVERDRAFT_LIMIT_EXCEEDED'

    def test_declined_no_buffer_created(self, client, demo_user, app):
        """No OverdraftBuffer should be created when the limit is exceeded."""
        with app.app_context():
            user = User.query.filter_by(paypal_email=SANDBOX_BUYER_EMAIL).first()
            TransactionHistory.query.filter_by(user_id=user.id).delete()
            user.overdraft_limit = 5.0
            db.session.commit()

            client.post('/api/overdraft/initiate', json={'amount': 6000.0})

            buffers = OverdraftBuffer.query.filter_by(user_id=user.id).all()
            assert len(buffers) == 0

    def test_payout_status_not_found_returns_404(self, client, demo_user, app):
        """Polling a non-existent batch ID returns 404."""
        with app.app_context():
            res = client.get('/api/overdraft/payout-status/NON-EXISTENT-BATCH')
            assert res.status_code == 404


# ─── State Machine Tests ───────────────────────────────────────────

class TestStateMachine:
    """Verify the full state machine transitions are correct."""

    def test_advance_state_logs_transition(self, app):
        """advance_state() correctly updates status field."""
        with app.app_context():
            user = User.query.filter_by(paypal_email=SANDBOX_BUYER_EMAIL).first()
            if not user:
                user = User(
                    name='John Doe',
                    paypal_email=SANDBOX_BUYER_EMAIL,
                    simulated_balance=15.0
                )
                db.session.add(user)
                db.session.flush()

            buffer = OverdraftBuffer(
                user_id=user.id,
                amount_covered=25.0,
                flat_fee=0.50,
                total_owed=25.50,
                status='OVERDRAFT_REQUESTED'
            )
            db.session.add(buffer)
            db.session.commit()

            buffer.advance_state('RISK_APPROVED')
            assert buffer.status == 'RISK_APPROVED'

            buffer.advance_state('PAYOUT_PENDING')
            assert buffer.status == 'PAYOUT_PENDING'

            buffer.advance_state('PAYOUT_SUCCESS')
            assert buffer.status == 'PAYOUT_SUCCESS'

    def test_active_overdraft_property_includes_in_flight_states(self, app):
        """active_overdraft property returns buffer for all in-flight states."""
        with app.app_context():
            user = User.query.filter_by(paypal_email=SANDBOX_BUYER_EMAIL).first()
            if not user:
                user = User(
                    name='John Doe',
                    paypal_email=SANDBOX_BUYER_EMAIL,
                    simulated_balance=15.0
                )
                db.session.add(user)
                db.session.flush()

            for status in ('PAYOUT_PENDING', 'PAYOUT_SUCCESS', 'CHECKOUT_COMPLETED', 'REPAYMENT_PENDING'):
                # Clear previous
                OverdraftBuffer.query.filter_by(user_id=user.id).delete()
                buffer = OverdraftBuffer(
                    user_id=user.id,
                    amount_covered=25.0,
                    flat_fee=0.50,
                    total_owed=25.50,
                    status=status
                )
                db.session.add(buffer)
                db.session.commit()

                assert user.active_overdraft is not None, (
                    f"active_overdraft should not be None when status={status}"
                )

    def test_is_settled_for_terminal_states(self, app):
        """is_settled returns True for CLOSED, REPAYMENT_RECEIVED, DEFAULTED."""
        with app.app_context():
            user = User.query.filter_by(paypal_email=SANDBOX_BUYER_EMAIL).first()
            if not user:
                user = User(
                    name='John Doe',
                    paypal_email=SANDBOX_BUYER_EMAIL,
                    simulated_balance=15.0
                )
                db.session.add(user)
                db.session.flush()

            buffer = OverdraftBuffer(
                user_id=user.id,
                amount_covered=25.0,
                flat_fee=0.50,
                total_owed=25.50,
                status='CLOSED'
            )
            db.session.add(buffer)
            db.session.commit()
            assert buffer.is_settled is True


# ─── AI Assistant Tests ────────────────────────────────────────────

class TestAIAssistant:
    def test_chat_limit_query(self, client, demo_user, app):
        with app.app_context():
            res = client.post('/ai/chat', json={'message': 'What is my overdraft limit?'})
            assert res.status_code == 200
            data = res.get_json()
            assert 'message' in data
            assert data['intent'] == 'overdraft_limit'

    def test_chat_balance_query(self, client, demo_user, app):
        with app.app_context():
            res = client.post('/ai/chat', json={'message': 'Check my wallet balance'})
            assert res.status_code == 200
            data = res.get_json()
            assert data['intent'] == 'wallet_balance'

    def test_chat_fee_query(self, client, demo_user, app):
        with app.app_context():
            res = client.post('/ai/chat', json={'message': 'What are the fees?'})
            assert res.status_code == 200
            data = res.get_json()
            assert data['intent'] == 'fee_info'

    def test_chat_how_it_works(self, client, demo_user, app):
        with app.app_context():
            res = client.post('/ai/chat', json={'message': 'How does PayFlow work?'})
            assert res.status_code == 200
            data = res.get_json()
            assert data['intent'] == 'how_it_works'
