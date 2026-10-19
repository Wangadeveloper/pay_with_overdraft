"""
Overdraft Blueprint — Overdraft status, limit management, repayment interface,
and Transaction Search income refresh.
"""
from flask import Blueprint, jsonify, render_template, request, current_app
from models import db, User, OverdraftBuffer, TransactionHistory
from risk_engine import compute_overdraft_limit, compute_risk_tier
from datetime import datetime

overdraft_bp = Blueprint('overdraft', __name__)


def get_demo_user():
    """Get the PayFlow buyer user (personal sandbox account)."""
    buyer_email = current_app.config.get('PAYPAL_BUYER_EMAIL', 'sb-tfjr4353268252@personal.example.com')
    return User.query.filter_by(paypal_email=buyer_email).first()


@overdraft_bp.route('/overdraft')
def overdraft_dashboard():
    user = get_demo_user()
    if not user:
        return "User not found", 404

    income = user.thirty_day_income
    od_limit = compute_overdraft_limit(income) if income > 0 else user.overdraft_limit
    risk_tier = compute_risk_tier(income)
    active_od = user.active_overdraft
    history = OverdraftBuffer.query.filter_by(user_id=user.id).order_by(
        OverdraftBuffer.created_at.desc()
    ).limit(10).all()

    return render_template(
        'overdraft.html',
        user=user,
        overdraft_limit=od_limit,
        risk_tier=risk_tier,
        thirty_day_income=income,
        active_overdraft=active_od,
        overdraft_history=history,
        flat_fee=current_app.config.get('FLAT_FEE', 0.50)
    )


@overdraft_bp.route('/api/overdraft/status')
def overdraft_status():
    user = get_demo_user()
    if not user:
        return jsonify({'error': 'user_not_found'}), 404

    income = user.thirty_day_income
    od_limit = compute_overdraft_limit(income) if income > 0 else user.overdraft_limit
    risk_tier = compute_risk_tier(income)
    active_od = user.active_overdraft

    # Fetch Funder Pool balance live from business account (sb-qjdsl53206694@business.example.com)
    funder_balance = 0.0
    funder_email = current_app.config.get('PAYPAL_FUNDER_EMAIL', 'sb-qjdsl53206694@business.example.com')
    try:
        if not current_app.config.get('DEMO_MODE', False):
            from paypal_client import PayPalClient
            client = PayPalClient()
            bal_info = client.get_account_balance()
            funder_balance = bal_info.get('available_usd', 0.0)
        else:
            funder_balance = 10000.00  # demo fallback
    except Exception:
        pass

    return jsonify({
        'user': {
            'email': user.paypal_email,
            'name': user.name,
            'account_id': user.paypal_account_id,
            'risk_tier': risk_tier,
            'wallet_balance': user.simulated_balance,
        },
        'funder': {
            'email': funder_email,
            'pool_balance': funder_balance,
            'mode': current_app.config.get('PAYPAL_MODE', 'sandbox')
        },
        'overdraft': {
            'limit': od_limit,
            'thirty_day_income': income,
            'active': {
                'id': active_od.id,
                'amount_covered': active_od.amount_covered,
                'flat_fee': active_od.flat_fee,
                'total_owed': active_od.total_owed,
                'status': active_od.status,
                'payout_batch_id': active_od.payout_batch_id,
                'payout_item_id': active_od.payout_item_id,
                'due_date': active_od.due_date.isoformat(),
                'days_overdue': active_od.days_overdue,
            } if active_od else None
        }
    })


@overdraft_bp.route('/api/overdraft/repay', methods=['POST'])
def manual_repay():
    """Simulate manual repayment of an overdraft buffer."""
    user = get_demo_user()
    active_od = user.active_overdraft if user else None
    if not active_od:
        return jsonify({'error': 'no_active_overdraft'}), 404

    data = request.get_json() or {}
    repay_amount = float(data.get('amount', active_od.total_owed))

    if user.simulated_balance < repay_amount:
        return jsonify({'error': 'insufficient_balance', 'balance': user.simulated_balance}), 400

    user.simulated_balance = round(user.simulated_balance - repay_amount, 2)
    active_od.advance_state('REPAYMENT_RECEIVED')
    active_od.advance_state('CLOSED')
    active_od.repaid_at = datetime.now()

    from models import RepaymentLog
    log = RepaymentLog(
        overdraft_id=active_od.id,
        webhook_event_id=f"MANUAL-{datetime.now().strftime('%Y%m%d%H%M%S')}",
        repaid_amount=repay_amount,
        trigger='MANUAL'
    )
    db.session.add(log)
    db.session.commit()

    return jsonify({
        'success': True,
        'repaid_amount': repay_amount,
        'wallet_balance': user.simulated_balance,
        'message': f'Overdraft of ${repay_amount:.2f} repaid successfully!'
    })


@overdraft_bp.route('/api/overdraft/simulate-income', methods=['POST'])
def simulate_income():
    """Simulate an incoming PayPal payment (for demo purposes)."""
    data = request.get_json() or {}
    amount = float(data.get('amount', 50.0))
    user = get_demo_user()
    if not user:
        return jsonify({'error': 'user_not_found'}), 404

    # Add to simulated balance
    user.simulated_balance = round(user.simulated_balance + amount, 2)

    # Record incoming transaction
    tx = TransactionHistory(
        user_id=user.id,
        paypal_tx_id=f"SIM-{datetime.now().strftime('%H%M%S%f')}",
        amount=amount,
        currency='USD',
        direction='IN',
        description='Simulated gig payment received'
    )
    db.session.add(tx)

    # Auto-repay active overdraft if balance covers it
    auto_repaid = None
    active_od = user.active_overdraft
    if active_od and user.simulated_balance >= active_od.total_owed:
        user.simulated_balance = round(user.simulated_balance - active_od.total_owed, 2)
        active_od.advance_state('REPAYMENT_RECEIVED')
        active_od.advance_state('CLOSED')
        active_od.repaid_at = datetime.now()
        from models import RepaymentLog
        log = RepaymentLog(
            overdraft_id=active_od.id,
            webhook_event_id=f"AUTO-{datetime.now().strftime('%Y%m%d%H%M%S')}",
            repaid_amount=active_od.total_owed,
            trigger='WEBHOOK'
        )
        db.session.add(log)
        auto_repaid = active_od.total_owed

    db.session.commit()

    return jsonify({
        'success': True,
        'income_received': amount,
        'wallet_balance': user.simulated_balance,
        'auto_repaid': auto_repaid,
        'message': (
            f'${amount:.2f} received!'
            + (f' Overdraft of ${auto_repaid:.2f} auto-repaid!' if auto_repaid else '')
        )
    })


# ─── Transaction Search Income Refresh ─────────────────────────────────────────

@overdraft_bp.route('/api/activity')
def financial_activity():
    """
    Unified financial activity feed for the AG Grid table.
    Merges TransactionHistory and OverdraftBuffer into a single
    normalised list, sorted newest-first.
    """
    user = get_demo_user()
    if not user:
        return jsonify({'error': 'user_not_found'}), 404

    rows = []

    # ── Overdraft records ──────────────────────────────────────────────
    overdrafts = OverdraftBuffer.query.filter_by(user_id=user.id).all()
    for od in overdrafts:
        # Map internal state machine status to a simple display status
        if od.status in ('REPAYMENT_RECEIVED', 'CLOSED', 'REPAID'):
            display_status = 'REPAID'
        elif od.status in ('DECLINED', 'PAYOUT_FAILED', 'DEFAULTED'):
            display_status = 'FAILED'
        elif od.status in ('OVERDRAFT_REQUESTED', 'RISK_APPROVED',
                           'PAYOUT_PENDING'):
            display_status = 'PENDING'
        else:
            display_status = 'ACTIVE'

        rows.append({
            'id': f'OD-{od.id}',
            'date': od.created_at.isoformat(),
            'type': 'overdraft',
            'reference': f'PF-OD-{od.id:04d}',
            'amount': round(od.amount_covered, 2),
            'credit_used': round(od.amount_covered, 2),
            'fee': round(od.flat_fee, 2),
            'status': display_status,
            'detail': {
                'payout_batch_id': od.payout_batch_id or '—',
                'order_id': od.order_id or '—',
                'due_date': od.due_date.isoformat() if od.due_date else None,
                'repaid_at': od.repaid_at.isoformat() if od.repaid_at else None,
                'days_overdue': od.days_overdue,
                'internal_status': od.status,
                'total_owed': round(od.total_owed, 2),
            }
        })

    # ── Transaction history records ────────────────────────────────────
    transactions = TransactionHistory.query.filter_by(user_id=user.id).all()
    for tx in transactions:
        activity_type = 'incoming' if tx.direction == 'IN' else 'payment'
        rows.append({
            'id': f'TX-{tx.id}',
            'date': tx.created_at.isoformat(),
            'type': activity_type,
            'reference': tx.paypal_tx_id or f'TX-{tx.id:04d}',
            'amount': round(tx.amount, 2),
            'credit_used': 0.0,
            'fee': 0.0,
            'status': 'COMPLETED',
            'detail': {
                'description': tx.description or '—',
                'currency': tx.currency,
                'direction': tx.direction,
                'payout_batch_id': '—',
                'order_id': '—',
                'due_date': None,
                'repaid_at': None,
                'days_overdue': 0,
                'total_owed': 0.0,
            }
        })

    # Sort newest-first
    rows.sort(key=lambda r: r['date'], reverse=True)

    return jsonify({'success': True, 'rows': rows, 'count': len(rows)})


@overdraft_bp.route('/api/income/refresh', methods=['POST'])
def refresh_income_from_paypal():
    """
    Fetch real PayPal transaction history via Transaction Search API
    and sync it into the local TransactionHistory table for income scoring.

    This replaces reliance on manually-seeded data with real Sandbox data.
    The endpoint pulls the last 30 days of transactions and upserts them locally.

    Note: Requires the reporting scope on the business OAuth token.
    Falls back gracefully if the API is unavailable.
    """
    user = get_demo_user()
    if not user:
        return jsonify({'error': 'user_not_found'}), 404

    demo_mode = current_app.config.get('DEMO_MODE', False)
    if demo_mode:
        # In demo mode, just return the existing seeded data stats
        income = user.thirty_day_income
        od_limit = compute_overdraft_limit(income) if income > 0 else user.overdraft_limit
        return jsonify({
            'success': True,
            'source': 'seeded_demo_data',
            'thirty_day_income': income,
            'overdraft_limit': od_limit,
            'message': 'Demo mode: using seeded transaction history.'
        })

    try:
        from paypal_client import PayPalClient
        from datetime import timedelta

        client = PayPalClient()
        result = client.get_transaction_history()

        transactions = result.get('transaction_details', [])
        synced_count = 0
        skipped_count = 0

        for tx_detail in transactions:
            tx_info = tx_detail.get('transaction_info', {})
            tx_id = tx_info.get('transaction_id')
            if not tx_id:
                continue

            # Skip if already recorded
            existing = TransactionHistory.query.filter_by(paypal_tx_id=tx_id).first()
            if existing:
                skipped_count += 1
                continue

            amount_str = tx_info.get('transaction_amount', {}).get('value', '0')
            amount = float(amount_str)

            # Positive = incoming, negative = outgoing
            direction = 'IN' if amount > 0 else 'OUT'
            amount = abs(amount)

            # Parse transaction date
            tx_date_str = tx_info.get('transaction_initiation_date', '')
            try:
                tx_date = datetime.strptime(tx_date_str[:19], '%Y-%m-%dT%H:%M:%S')
            except Exception:
                tx_date = datetime.now()

            description = tx_info.get('transaction_subject', '') or tx_info.get('transaction_note', '') or 'PayPal transaction'

            tx_record = TransactionHistory(
                user_id=user.id,
                paypal_tx_id=tx_id,
                amount=amount,
                currency=tx_info.get('transaction_amount', {}).get('currency_code', 'USD'),
                direction=direction,
                description=description[:300],
                created_at=tx_date
            )
            db.session.add(tx_record)
            synced_count += 1

        db.session.commit()

        income = user.thirty_day_income
        od_limit = compute_overdraft_limit(income) if income > 0 else user.overdraft_limit
        risk_tier = compute_risk_tier(income)

        return jsonify({
            'success': True,
            'source': 'paypal_transaction_search',
            'synced': synced_count,
            'skipped': skipped_count,
            'thirty_day_income': income,
            'overdraft_limit': od_limit,
            'risk_tier': risk_tier,
            'message': (
                f'Synced {synced_count} transactions from PayPal. '
                f'30-day income: ${income:.2f} → Limit: ${od_limit:.2f}'
            )
        })

    except Exception as e:
        current_app.logger.error(f"Transaction Search API error: {e}")
        # Graceful fallback
        income = user.thirty_day_income
        od_limit = compute_overdraft_limit(income) if income > 0 else user.overdraft_limit
        return jsonify({
            'success': False,
            'source': 'fallback_seeded_data',
            'error': str(e),
            'thirty_day_income': income,
            'overdraft_limit': od_limit,
            'message': (
                f'Transaction Search unavailable (check reporting scope). '
                f'Using seeded data: 30-day income ${income:.2f}'
            )
        }), 207  # 207 Multi-Status: partial success
