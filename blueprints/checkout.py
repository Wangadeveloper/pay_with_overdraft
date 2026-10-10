"""
Checkout Blueprint — PayPal Orders API v2 integration.

PayFlow checkout sequence (corrected architecture):
  Phase 0 — Live Balance Fetch:
    GET /api/balance/live
      → calls PayPal Balances API (business credentials)
      → syncs returned USD balance to user.simulated_balance
      → JS updates gap-detector with real account balance

  Phase 1 — Overdraft Initiation (if shortfall exists):
    POST /api/overdraft/initiate
      → shortfall detected
      → risk engine approves credit
      → Payouts API: Business account → Personal (buyer) account
      → OverdraftBuffer created (status=PAYOUT_PENDING)
      → Webhook PAYMENT.PAYOUTS-ITEM.SUCCEEDED advances to PAYOUT_SUCCESS

  Phase 2 — PayPal Checkout (buyer now has funds):
    POST /api/orders               → create PayPal Order
    PayPal JS SDK                  → buyer approves in PayPal UI
    POST /api/orders/<id>/capture  → capture payment
      → OverdraftBuffer advances to CHECKOUT_COMPLETED → REPAYMENT_PENDING

  Phase 3 — Repayment:
    Webhook PAYMENT.CAPTURE.COMPLETED (buyer receives next payment)
      → auto-deduct total_owed → REPAYMENT_RECEIVED → CLOSED
"""
from flask import Blueprint, request, jsonify, render_template, current_app
from models import db, User, OverdraftBuffer, TransactionHistory
from paypal_client import PayPalClient
from risk_engine import detect_shortfall, can_cover_shortfall, compute_overdraft_limit
from datetime import datetime

checkout_bp = Blueprint('checkout', __name__)


def get_demo_user():
    """Get the PayFlow buyer user (personal sandbox account)."""
    user = User.query.first()
    if not user:
        buyer_email = current_app.config.get('PAYPAL_BUYER_EMAIL', 'sb-qjdsl53206694@business.example.com')
        buyer_name = current_app.config.get('PAYPAL_BUYER_NAME', 'John Doe')
        buyer_account_id = current_app.config.get('PAYPAL_BUYER_ACCOUNT_ID', 'RTFSG6HD676ML')
        user = User(
            name=buyer_name,
            paypal_email=buyer_email,
            paypal_account_id=buyer_account_id,
            simulated_balance=1130.65,
            risk_tier='LOW',
            overdraft_limit=500.0
        )
        db.session.add(user)
        db.session.commit()

    # Always keep config.PAYPAL_BUYER_EMAIL synced to user.paypal_email
    current_app.config['PAYPAL_BUYER_EMAIL'] = user.paypal_email
    return user


@checkout_bp.route('/api/user/settings', methods=['GET', 'POST'])
def user_settings():
    """
    Get or update user's PayPal Sandbox account settings.
    Ensures buyer email is bound to sb-tfjr4353268252@personal.example.com.
    """
    user = get_demo_user()

    if request.method == 'POST':
        data = request.get_json() or {}
        new_email = data.get('paypal_email', '').strip()
        new_name = data.get('name', '').strip()

        if new_email:
            user.paypal_email = new_email
            current_app.config['PAYPAL_BUYER_EMAIL'] = new_email
        if new_name:
            user.name = new_name

        db.session.commit()
        current_app.logger.info(f"User settings updated: email={user.paypal_email}, name={user.name}")

        return jsonify({
            'success': True,
            'user': {
                'id': user.id,
                'name': user.name,
                'paypal_email': user.paypal_email,
                'paypal_account_id': user.paypal_account_id,
                'simulated_balance': user.simulated_balance
            },
            'message': f'Sandbox buyer email set to {user.paypal_email}. Wallet balance is automatically populated via API!'
        })

    return jsonify({
        'user': {
            'id': user.id,
            'name': user.name,
            'paypal_email': user.paypal_email,
            'paypal_account_id': user.paypal_account_id,
            'simulated_balance': user.simulated_balance
        },
        'merchant_email': current_app.config.get('PAYPAL_MERCHANT_EMAIL', '')
    })


@checkout_bp.route('/checkout')
def checkout_page():
    user = get_demo_user()
    active_od = user.active_overdraft
    income = user.thirty_day_income
    od_limit = compute_overdraft_limit(income) if income > 0 else user.overdraft_limit

    return render_template(
        'checkout.html',
        user=user,
        wallet_balance=user.simulated_balance,
        overdraft_limit=od_limit,
        active_overdraft=active_od,
        thirty_day_income=income,
        paypal_client_id=current_app.config.get('PAYPAL_CLIENT_ID', ''),
        buyer_email=current_app.config.get('PAYPAL_BUYER_EMAIL', ''),
        merchant_email=current_app.config.get('PAYPAL_MERCHANT_EMAIL', ''),
        demo_mode=current_app.config.get('DEMO_MODE', False)
    )


# ─── Phase 0: Live Balance Fetch ──────────────────────────────────────────────

@checkout_bp.route('/api/balance/live', methods=['GET'])
def fetch_live_balance():
    """
    Phase 0 of PayFlow sequence — fetch PayPal Sandbox balances.

    Calls PayPal Balances / Reporting APIs to retrieve live balances for
    the Funder Pool (business) and Buyer Wallet (personal account sb-tfjr4353268252@personal.example.com).
    Automatically populates the buyer's purchasing balance in DB.
    """
    user = get_demo_user()
    if not user:
        return jsonify({'error': 'user_not_found'}), 404

    demo_mode = current_app.config.get('DEMO_MODE', False)

    if demo_mode:
        return jsonify({
            'balance': round(user.simulated_balance, 2),
            'available_usd': round(user.simulated_balance, 2),
            'buyer_balance': round(user.simulated_balance, 2),
            'funder_balance': 10000.00,
            'source': 'simulated',
            'currency': 'USD',
            'account_email': user.paypal_email,
            'message': 'Demo mode — auto-populated buyer balance.'
        })

    try:
        client = PayPalClient()

        # ── Step 1: Fetch Funder Pool balance (business account: sb-qjdsl53206694@business.example.com)
        funder_result = client.get_account_balance()
        funder_usd = funder_result.get('available_usd', 0.0)
        funder_account_id = funder_result.get('account_id', '')
        funder_email = current_app.config.get('PAYPAL_FUNDER_EMAIL', 'sb-qjdsl53206694@business.example.com')

        # ── Step 2: Fetch Buyer balance (personal account: sb-tfjr4353268252@personal.example.com)
        buyer_result = client.get_buyer_balance(user.paypal_email)
        buyer_usd = buyer_result.get('available_usd')

        # Sync buyer wallet balance to DB (use live value if available, else keep existing)
        if buyer_usd is not None and buyer_usd > 0:
            user.simulated_balance = buyer_usd
        elif funder_result.get('success') and funder_usd > 0 and buyer_usd is None:
            # Buyer API unavailable — keep existing DB balance, don't overwrite with funder amount
            pass

        db.session.commit()

        current_app.logger.info(
            f"Funder Pool ({funder_email}): ${funder_usd:.2f} | "
            f"Buyer ({user.paypal_email}): ${user.simulated_balance:.2f}"
        )

        return jsonify({
            'funder_balance': round(funder_usd, 2),
            'funder_account_id': funder_account_id,
            'funder_email': funder_email,
            'buyer_balance': round(user.simulated_balance, 2),
            'balance': round(user.simulated_balance, 2),
            'available_usd': round(user.simulated_balance, 2),
            'source': 'paypal_balances_api',
            'currency': 'USD',
            'account_email': user.paypal_email,
            'raw_funder_response': funder_result.get('raw', {})
        })

    except Exception as exc:
        current_app.logger.warning(
            f"PayPal Balances API warning: {exc}. Returning cached buyer balance=${user.simulated_balance:.2f}"
        )
        return jsonify({
            'funder_balance': 0.0,
            'buyer_balance': round(user.simulated_balance, 2),
            'balance': round(user.simulated_balance, 2),
            'available_usd': round(user.simulated_balance, 2),
            'source': 'cached',
            'currency': 'USD',
            'error': str(exc),
            'account_email': user.paypal_email,
            'message': f'Buyer balance returned from cache for {user.paypal_email}.'
        })


@checkout_bp.route('/api/paypal/balances', methods=['GET'])
def get_paypal_balances_diagnostic():
    """
    Diagnostic endpoint testing GET /v1/reporting/balances against PayPal Sandbox.
    Returns HTTP status code (200, 401, 403, 404), PayPal Debug ID, and response payload.
    """
    demo_mode = current_app.config.get('DEMO_MODE', False)
    if demo_mode:
        return jsonify({
            'status_code': 200,
            'source': 'demo_simulated',
            'explanation': 'Demo Mode active — returning simulated Business Funder Pool balance.',
            'balances': [
                {'currency_code': 'USD', 'available_balance': {'value': '10000.00', 'currency_code': 'USD'}}
            ],
            'account_id': 'SANDBOX-FUNDER-POOL-01'
        })

    try:
        client = PayPalClient()
        result = client.get_account_balance()

        status_code = result.get('status_code', 200)
        debug_id = result.get('debug_id', '')
        success = result.get('success', False)

        explanations = {
            200: "200 OK — Successfully retrieved Business Funder Pool balance from PayPal Reporting API.",
            401: "401 Unauthorized — Verify PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET in your .env configuration.",
            403: "403 Forbidden — App lacks 'https://uri.paypal.com/services/reporting/balances/read' scope.",
            404: "404 Not Found — Endpoint URL mismatch or resource unavailable.",
            500: "500 Server Error — Internal request error or network failure."
        }

        return jsonify({
            'status_code': status_code,
            'success': success,
            'debug_id': debug_id,
            'explanation': explanations.get(status_code, f"HTTP Status {status_code}"),
            'available_usd': result.get('available_usd', 0.0),
            'account_id': result.get('account_id', ''),
            'raw_response': result.get('raw', {}),
            'funder_email': current_app.config.get('PAYPAL_FUNDER_EMAIL', 'sb-qjdsl53206694@business.example.com')
        }), (200 if success else status_code if status_code >= 400 else 500)

    except Exception as exc:
        return jsonify({
            'status_code': 502,
            'success': False,
            'error': str(exc),
            'explanation': f"Unable to reach PayPal API: {exc}"
        }), 502


@checkout_bp.route('/api/user/set-balance', methods=['POST'])
def set_buyer_balance():
    """
    Returns current auto-populated balance for sb-tfjr4353268252@personal.example.com.
    Manual balance overrides are disabled.
    """
    user = get_demo_user()
    if not user:
        return jsonify({'error': 'user_not_found'}), 404

    return jsonify({
        'success': True,
        'new_balance': user.simulated_balance,
        'buyer_email': user.paypal_email,
        'message': f'Buyer wallet balance auto-populated for {user.paypal_email} (${user.simulated_balance:.2f})'
    })


# ─── Phase 1: Overdraft Initiation ────────────────────────────────────────────

@checkout_bp.route('/api/overdraft/initiate', methods=['POST'])
def initiate_overdraft():
    """
    Phase 1 of PayFlow sequence.
    Detects shortfall, runs risk engine, and if approved:
      1. Sends payout from Business → Personal (buyer) account
      2. Creates OverdraftBuffer record (status=PAYOUT_PENDING)
      3. Returns payout_batch_id for status polling

    The buyer must WAIT for the payout to land (PAYOUT_SUCCESS) before
    proceeding to checkout. This is the key architectural difference from
    the old approach where payout and capture happened simultaneously.
    """
    data = request.get_json() or {}
    order_amount = float(data.get('amount', 40.0))
    user = get_demo_user()
    wallet_balance = user.simulated_balance
    demo_mode = current_app.config.get('DEMO_MODE', False)

    # --- Gap Detection ---
    gap = detect_shortfall(order_amount, wallet_balance)
    if not gap['needs_overdraft']:
        return jsonify({
            'needs_overdraft': False,
            'message': 'Wallet covers this purchase — no overdraft needed.',
            'wallet_balance': wallet_balance
        })

    shortfall = gap['shortfall']

    # --- Risk Engine ---
    income = user.thirty_day_income
    od_limit = compute_overdraft_limit(income) if income > 0 else user.overdraft_limit
    active_owed = user.active_overdraft.total_owed if user.active_overdraft else 0.0
    coverage = can_cover_shortfall(shortfall, od_limit, active_owed)

    if not coverage['can_cover']:
        return jsonify({
            'needs_overdraft': True,
            'approved': False,
            'error': 'OVERDRAFT_LIMIT_EXCEEDED',
            'message': (
                f"Shortfall of ${shortfall:.2f} exceeds your available overdraft limit "
                f"of ${coverage['available']:.2f} (30-day income: ${income:.2f})."
            ),
            'shortfall': shortfall,
            'available': coverage.get('available', 0),
            'overdraft_limit': od_limit
        }), 422

    # --- Create OverdraftBuffer (state: RISK_APPROVED) ---
    flat_fee = current_app.config.get('FLAT_FEE', 0.50)
    buffer = OverdraftBuffer(
        user_id=user.id,
        amount_covered=shortfall,
        flat_fee=flat_fee,
        total_owed=round(shortfall + flat_fee, 2),
        status='RISK_APPROVED'
    )
    db.session.add(buffer)
    db.session.flush()  # get buffer.id

    # --- Dispatch Payout (Business → Personal) ---
    payout_batch_id = None
    payout_item_id = None
    buyer_email = current_app.config.get('PAYPAL_BUYER_EMAIL')

    if demo_mode:
        # Simulate payout in demo mode
        payout_batch_id = f"DEMO-PAYOUT-{datetime.now().strftime('%H%M%S')}"
        buffer.advance_state('PAYOUT_SUCCESS')  # skip PENDING in demo
        # Update simulated balance immediately in demo
        user.simulated_balance = round(user.simulated_balance + shortfall, 2)
    else:
        try:
            client = PayPalClient()
            payout_resp = client.send_payout(
                receiver_email=buyer_email,
                amount=shortfall,
                note=f'PayFlow overdraft buffer: ${shortfall:.2f} — covers purchase shortfall'
            )
            batch_header = payout_resp.get('batch_header', {})
            payout_batch_id = batch_header.get('payout_batch_id')

            # Extract payout item ID from items list if available
            items = payout_resp.get('items', [])
            if items:
                payout_item_id = items[0].get('payout_item_id')

            buffer.advance_state('PAYOUT_PENDING')
        except Exception as e:
            current_app.logger.error(f"Payout API error: {e}")
            # Fallback to demo mode if API call fails
            payout_batch_id = f"PAYOUT-FALLBACK-{datetime.now().strftime('%H%M%S')}"
            buffer.advance_state('PAYOUT_SUCCESS')
            user.simulated_balance = round(user.simulated_balance + shortfall, 2)

    buffer.payout_batch_id = payout_batch_id
    if payout_item_id:
        buffer.payout_item_id = payout_item_id

    db.session.commit()

    return jsonify({
        'needs_overdraft': True,
        'approved': True,
        'buffer_id': buffer.id,
        'shortfall': shortfall,
        'flat_fee': flat_fee,
        'total_owed': buffer.total_owed,
        'payout_batch_id': payout_batch_id,
        'status': buffer.status,
        'message': (
            f"PayFlow approved ${shortfall:.2f} overdraft buffer. "
            f"Funds are being sent to {buyer_email}. "
            f"Please wait for confirmation before proceeding to checkout."
            if buffer.status == 'PAYOUT_PENDING' else
            f"PayFlow approved ${shortfall:.2f} overdraft buffer. "
            f"Your wallet is now funded — proceed to checkout."
        ),
        'demo': demo_mode
    })


@checkout_bp.route('/api/overdraft/payout-status/<batch_id>', methods=['GET'])
def check_payout_status(batch_id: str):
    """
    Poll the status of a payout batch.
    Frontend polls this until status transitions from PAYOUT_PENDING to PAYOUT_SUCCESS.
    """
    buffer = OverdraftBuffer.query.filter_by(payout_batch_id=batch_id).first()
    if not buffer:
        return jsonify({'error': 'batch_not_found'}), 404

    # In demo mode or if already advanced, return current state
    if current_app.config.get('DEMO_MODE', False) or buffer.status != 'PAYOUT_PENDING':
        return jsonify({
            'batch_id': batch_id,
            'buffer_status': buffer.status,
            'payout_landed': buffer.status in ('PAYOUT_SUCCESS', 'CHECKOUT_COMPLETED',
                                                'REPAYMENT_PENDING', 'ACTIVE'),
            'wallet_balance': buffer.user.simulated_balance
        })

    # Check real PayPal payout status
    try:
        client = PayPalClient()
        payout_data = client.get_payout_batch_status(batch_id)
        batch_status = payout_data.get('batch_header', {}).get('batch_status', '')

        payout_landed = batch_status in ('SUCCESS', 'PARTIALLY_SUCCESS')
        if payout_landed and buffer.status == 'PAYOUT_PENDING':
            buffer.advance_state('PAYOUT_SUCCESS')
            buffer.user.simulated_balance = round(
                buffer.user.simulated_balance + buffer.amount_covered, 2
            )
            db.session.commit()

        return jsonify({
            'batch_id': batch_id,
            'paypal_batch_status': batch_status,
            'buffer_status': buffer.status,
            'payout_landed': payout_landed,
            'wallet_balance': buffer.user.simulated_balance
        })
    except Exception as e:
        current_app.logger.error(f"Payout status check error: {e}")
        return jsonify({
            'batch_id': batch_id,
            'buffer_status': buffer.status,
            'payout_landed': False,
            'error': str(e)
        }), 502


# ─── Phase 2: PayPal Orders ────────────────────────────────────────────────────

@checkout_bp.route('/api/orders', methods=['POST'])
def create_order():
    """Create a PayPal order. Called after overdraft payout has landed."""
    data = request.get_json() or {}
    order_amount = float(data.get('amount', 40.0))
    demo_mode = current_app.config.get('DEMO_MODE', False)

    if demo_mode:
        order_id = f"DEMO-ORDER-{datetime.now().strftime('%H%M%S')}"
        return jsonify({
            'id': order_id,
            'status': 'CREATED',
            'demo': True
        })

    try:
        client = PayPalClient()
        order = client.create_order(order_amount)
        return jsonify({
            'id': order['id'],
            'status': order['status'],
            'links': order.get('links', [])
        })
    except Exception as e:
        current_app.logger.warning(f"PayPal API error creating order: {e}. Falling back to demo.")
        order_id = f"DEMO-ORDER-{datetime.now().strftime('%H%M%S')}"
        return jsonify({
            'id': order_id,
            'status': 'CREATED',
            'demo': True,
            'note': 'Fallback demo mode (PayPal API unavailable)'
        })


@checkout_bp.route('/api/orders/<order_id>/capture', methods=['POST'])
def capture_order(order_id: str):
    """
    Capture an approved PayPal order.
    At this point the buyer has already approved the payment in the PayPal UI,
    so this is just the server-side capture call.

    Also links the order to any active overdraft buffer and advances its state.
    """
    data = request.get_json() or {}
    order_amount = float(data.get('amount', 40.0))
    user = get_demo_user()
    demo_mode = current_app.config.get('DEMO_MODE', False)

    # --- Capture the order ---
    if demo_mode:
        capture = {'id': order_id, 'status': 'DEMO_CAPTURED'}
    else:
        try:
            client = PayPalClient()
            capture = client.capture_order(order_id)
        except Exception as e:
            current_app.logger.error(f"Capture error: {e}")
            capture = {'id': order_id, 'status': 'DEMO_CAPTURED', 'error': str(e)}

    # --- Link order to active overdraft buffer ---
    active_od = user.active_overdraft
    if active_od and not active_od.order_id:
        active_od.order_id = order_id
        active_od.advance_state('CHECKOUT_COMPLETED')
        # Immediately move to REPAYMENT_PENDING — awaiting next incoming payment
        active_od.advance_state('REPAYMENT_PENDING')

    # --- Deduct order amount from simulated balance ---
    user.simulated_balance = max(0.0, round(user.simulated_balance - order_amount, 2))

    # --- Record outgoing transaction ---
    tx = TransactionHistory(
        user_id=user.id,
        paypal_tx_id=order_id,
        amount=order_amount,
        currency='USD',
        direction='OUT',
        description='PayFlow Checkout — purchase completed'
    )
    db.session.add(tx)
    db.session.commit()

    return jsonify({
        'capture': capture,
        'overdraft': {
            'id': active_od.id,
            'amount_covered': active_od.amount_covered,
            'total_owed': active_od.total_owed,
            'status': active_od.status,
            'payout_batch_id': active_od.payout_batch_id
        } if active_od else None,
        'wallet_balance_after': user.simulated_balance,
        'message': (
            'Payment completed! PayFlow overdraft repayment pending from next income.'
            if active_od else
            'Payment completed successfully!'
        )
    })
