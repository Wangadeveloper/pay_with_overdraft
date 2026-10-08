"""
Webhooks Blueprint — PayPal event handlers with full state machine support.

Subscribed events:
  PAYMENT.CAPTURE.COMPLETED       → buyer received money → auto-repay overdraft
  PAYMENT.CAPTURE.DENIED          → log failed capture
  PAYMENT.PAYOUTS-ITEM.SUCCEEDED  → payout landed → advance PAYOUT_PENDING → PAYOUT_SUCCESS
  PAYMENT.PAYOUTS-ITEM.FAILED     → payout failed → mark buffer PAYOUT_FAILED
"""
from flask import Blueprint, request, jsonify, current_app
from models import db, User, OverdraftBuffer, RepaymentLog, TransactionHistory
from paypal_client import PayPalClient
from datetime import datetime

webhooks_bp = Blueprint('webhooks', __name__)


def get_buyer_user():
    """Resolve the PayFlow buyer from the configured sandbox email."""
    buyer_email = current_app.config.get('PAYPAL_BUYER_EMAIL', 'sb-tfjr4353268252@personal.example.com')
    return User.query.filter_by(paypal_email=buyer_email).first()


@webhooks_bp.route('/webhooks/paypal', methods=['POST'])
def paypal_webhook():
    """
    Main webhook receiver.
    Acknowledges immediately with 200, then routes to the appropriate handler.
    """
    raw_body = request.get_data()
    headers = {k: v for k, v in request.headers.items()}

    # Verify signature (skip in demo mode or if WEBHOOK_ID not configured)
    demo_mode = current_app.config.get('DEMO_MODE', False)
    webhook_id = current_app.config.get('PAYPAL_WEBHOOK_ID', '')
    if not demo_mode and webhook_id and 'your_webhook_id' not in webhook_id:
        try:
            client = PayPalClient()
            verified = client.verify_webhook_signature(headers, raw_body)
            if not verified:
                current_app.logger.warning("Webhook signature verification FAILED")
                return jsonify({'error': 'invalid_signature'}), 401
        except Exception as e:
            current_app.logger.error(f"Signature verification error: {e}")

    data = request.get_json(silent=True) or {}
    event_type = data.get('event_type', '')
    event_id = data.get('id', '')

    current_app.logger.info(f"PayPal Webhook received: {event_type} (id={event_id})")

    # Route to the appropriate handler
    handlers = {
        'PAYMENT.CAPTURE.COMPLETED': handle_payment_capture_completed,
        'PAYMENT.CAPTURE.DENIED': handle_payment_capture_denied,
        'PAYMENT.PAYOUTS-ITEM.SUCCEEDED': handle_payout_item_succeeded,
        'PAYMENT.PAYOUTS-ITEM.FAILED': handle_payout_item_failed,
    }

    handler = handlers.get(event_type)
    if handler:
        try:
            handler(data, event_id)
        except Exception as e:
            current_app.logger.error(f"Webhook handler error for {event_type}: {e}")

    # Always return 200 to acknowledge receipt
    return jsonify({'status': 'received', 'event_type': event_type}), 200


# ─── Event Handlers ────────────────────────────────────────────────────────────

def handle_payment_capture_completed(data: dict, event_id: str):
    """
    PAYMENT.CAPTURE.COMPLETED — Buyer received an incoming payment.
    Auto-repay any active overdraft buffer + flat fee.

    State machine: REPAYMENT_PENDING → REPAYMENT_RECEIVED → CLOSED
    """
    # Idempotency check
    existing = RepaymentLog.query.filter_by(webhook_event_id=event_id).first()
    if existing:
        current_app.logger.info(f"Duplicate webhook {event_id}, skipping")
        return

    resource = data.get('resource', {})
    amount_str = resource.get('amount', {}).get('value', '0')
    payee_email = resource.get('payee', {}).get('email_address', '')
    payer_email = resource.get('payer', {}).get('email_address', '')

    # Try to find user by email in the webhook payload first, then fall back to configured buyer
    user = User.query.filter(
        (User.paypal_email == payee_email) | (User.paypal_email == payer_email)
    ).first()

    if not user:
        user = get_buyer_user()

    if not user:
        current_app.logger.warning(f"No user found for webhook event {event_id}")
        return

    received_amount = float(amount_str)

    # Credit the incoming payment to simulated balance
    user.simulated_balance = round(user.simulated_balance + received_amount, 2)

    # Record incoming transaction
    tx = TransactionHistory(
        user_id=user.id,
        paypal_tx_id=event_id,
        amount=received_amount,
        currency='USD',
        direction='IN',
        description='PayPal payment received (webhook)'
    )
    db.session.add(tx)

    # Auto-repay active overdraft
    active_od = user.active_overdraft
    if active_od and user.simulated_balance >= active_od.total_owed:
        repay_amount = active_od.total_owed
        user.simulated_balance = round(user.simulated_balance - repay_amount, 2)
        active_od.advance_state('REPAYMENT_RECEIVED')
        active_od.advance_state('CLOSED')
        active_od.repaid_at = datetime.now()

        repay_log = RepaymentLog(
            overdraft_id=active_od.id,
            webhook_event_id=event_id,
            repaid_amount=repay_amount,
            trigger='WEBHOOK'
        )
        db.session.add(repay_log)
        current_app.logger.info(
            f"Auto-repaid overdraft #{active_od.id} — ${repay_amount:.2f} for user {user.paypal_email}"
        )
    elif active_od:
        # Balance came in but not enough to fully repay — just log the event
        current_app.logger.info(
            f"Incoming ${received_amount:.2f} for {user.paypal_email} — "
            f"overdraft #{active_od.id} still outstanding (${active_od.total_owed:.2f} owed)"
        )
        repay_log = RepaymentLog(
            overdraft_id=active_od.id,
            webhook_event_id=event_id,
            repaid_amount=0.0,
            trigger='WEBHOOK'
        )
        db.session.add(repay_log)

    db.session.commit()


def handle_payment_capture_denied(data: dict, event_id: str):
    """PAYMENT.CAPTURE.DENIED — Log failed capture."""
    current_app.logger.warning(f"Payment capture DENIED: event_id={event_id}")


def handle_payout_item_succeeded(data: dict, event_id: str):
    """
    PAYMENT.PAYOUTS-ITEM.SUCCEEDED — Overdraft payout landed in buyer's wallet.

    State machine: PAYOUT_PENDING → PAYOUT_SUCCESS
    The buyer now has sufficient funds to complete checkout.
    """
    resource = data.get('resource', {})
    batch_id = resource.get('payout_batch_id', '')
    payout_item_id = resource.get('payout_item_id', '')
    amount_str = resource.get('amount', {}).get('value', '0')
    payout_amount = float(amount_str)

    current_app.logger.info(
        f"Payout item succeeded: batch_id={batch_id}, item_id={payout_item_id}, amount=${payout_amount:.2f}"
    )

    # Find the corresponding OverdraftBuffer
    buffer = OverdraftBuffer.query.filter_by(payout_batch_id=batch_id).first()
    if not buffer:
        if payout_item_id:
            buffer = OverdraftBuffer.query.filter_by(payout_item_id=payout_item_id).first()

    if buffer and buffer.status == 'PAYOUT_PENDING':
        buffer.advance_state('PAYOUT_SUCCESS')
        # Credit the payout amount to buyer's simulated balance
        if buffer.user:
            buffer.user.simulated_balance = round(
                buffer.user.simulated_balance + buffer.amount_covered, 2
            )
        db.session.commit()
        current_app.logger.info(
            f"OverdraftBuffer #{buffer.id} advanced to PAYOUT_SUCCESS — "
            f"buyer wallet credited ${buffer.amount_covered:.2f}"
        )
    elif buffer:
        current_app.logger.info(
            f"Payout succeeded webhook for buffer #{buffer.id} (status={buffer.status}) — no state change needed"
        )


def handle_payout_item_failed(data: dict, event_id: str):
    """
    PAYMENT.PAYOUTS-ITEM.FAILED — Overdraft payout failed.

    State machine: PAYOUT_PENDING → PAYOUT_FAILED
    The checkout cannot proceed. User should be notified.
    """
    resource = data.get('resource', {})
    batch_id = resource.get('payout_batch_id', '')
    payout_item_id = resource.get('payout_item_id', '')
    error_info = resource.get('errors', {})

    current_app.logger.error(
        f"Payout item FAILED: batch_id={batch_id}, item_id={payout_item_id}, error={error_info}"
    )

    # Find the corresponding OverdraftBuffer
    buffer = OverdraftBuffer.query.filter_by(payout_batch_id=batch_id).first()
    if not buffer and payout_item_id:
        buffer = OverdraftBuffer.query.filter_by(payout_item_id=payout_item_id).first()

    if buffer and buffer.status == 'PAYOUT_PENDING':
        buffer.advance_state('PAYOUT_FAILED')
        db.session.commit()
        current_app.logger.error(
            f"OverdraftBuffer #{buffer.id} marked PAYOUT_FAILED. "
            f"Buyer checkout blocked. Error: {error_info}"
        )


# ─── Development / Testing Endpoints ──────────────────────────────────────────

@webhooks_bp.route('/webhooks/simulate', methods=['POST'])
def simulate_webhook():
    """
    Development endpoint: simulate a PAYMENT.CAPTURE.COMPLETED webhook.
    Useful for testing auto-repayment without ngrok.
    """
    data = request.get_json() or {}
    amount = float(data.get('amount', 50.0))
    buyer_email = current_app.config.get('PAYPAL_BUYER_EMAIL', 'sb-tfjr4353268252@personal.example.com')

    fake_event_id = f"SIMWH-{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
    fake_payload = {
        'event_type': 'PAYMENT.CAPTURE.COMPLETED',
        'id': fake_event_id,
        'resource': {
            'amount': {'value': str(amount), 'currency_code': 'USD'},
            'payee': {'email_address': buyer_email},
            'payer': {'email_address': 'gig-client@example.com'}
        }
    }

    try:
        handle_payment_capture_completed(fake_payload, fake_event_id)
        db.session.commit()
        user = User.query.filter_by(paypal_email=buyer_email).first()
        return jsonify({
            'success': True,
            'event_id': fake_event_id,
            'amount_received': amount,
            'wallet_balance': user.simulated_balance if user else 0,
            'message': f'Simulated webhook: ${amount:.2f} received, auto-repayment triggered if overdraft active'
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@webhooks_bp.route('/webhooks/simulate-payout-success', methods=['POST'])
def simulate_payout_success():
    """
    Development endpoint: simulate a PAYMENT.PAYOUTS-ITEM.SUCCEEDED webhook.
    Use this to advance a PAYOUT_PENDING buffer to PAYOUT_SUCCESS in testing.
    """
    data = request.get_json() or {}
    batch_id = data.get('batch_id')

    buffer = OverdraftBuffer.query.filter_by(payout_batch_id=batch_id).first() if batch_id else None
    if not buffer:
        # Try the most recent PAYOUT_PENDING buffer
        buffer = OverdraftBuffer.query.filter_by(status='PAYOUT_PENDING').order_by(
            OverdraftBuffer.created_at.desc()
        ).first()

    if not buffer:
        return jsonify({'error': 'no_pending_payout_found'}), 404

    fake_event_id = f"SIMPY-{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
    fake_payload = {
        'event_type': 'PAYMENT.PAYOUTS-ITEM.SUCCEEDED',
        'id': fake_event_id,
        'resource': {
            'payout_batch_id': buffer.payout_batch_id or 'UNKNOWN',
            'payout_item_id': buffer.payout_item_id or 'UNKNOWN',
            'amount': {'value': str(buffer.amount_covered), 'currency_code': 'USD'},
        }
    }

    try:
        handle_payout_item_succeeded(fake_payload, fake_event_id)
        db.session.commit()
        user = buffer.user
        return jsonify({
            'success': True,
            'buffer_id': buffer.id,
            'new_status': buffer.status,
            'wallet_balance': user.simulated_balance if user else 0,
            'message': f'Simulated payout success: buffer #{buffer.id} advanced to {buffer.status}'
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500
