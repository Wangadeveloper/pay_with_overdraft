"""
PayFlow Overdraft — Flask Application Factory
"""
from flask import Flask, redirect, url_for
from models import db
from config import Config


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    # Expose computed properties on app.config
    cfg = config_class()
    app.config['DEMO_MODE'] = cfg.DEMO_MODE
    app.config['PAYPAL_BASE_URL'] = cfg.PAYPAL_BASE_URL

    # Initialize extensions
    db.init_app(app)

    # Register blueprints
    from blueprints.checkout import checkout_bp
    from blueprints.overdraft import overdraft_bp
    from blueprints.webhooks import webhooks_bp
    from blueprints.ai_agent import ai_bp

    app.register_blueprint(checkout_bp)
    app.register_blueprint(overdraft_bp)
    app.register_blueprint(webhooks_bp)
    app.register_blueprint(ai_bp)

    # Root redirect to dashboard
    @app.route('/')
    def index():
        return redirect(url_for('checkout.checkout_page'))

    @app.route('/dashboard')
    def dashboard():
        from flask import render_template
        from models import User, TransactionHistory
        from risk_engine import compute_overdraft_limit, compute_risk_tier

        buyer_email = app.config.get('PAYPAL_BUYER_EMAIL')
        user = User.query.filter_by(paypal_email=buyer_email).first()
        if not user:
            return redirect(url_for('checkout.checkout_page'))

        income = user.thirty_day_income
        od_limit = compute_overdraft_limit(income) if income > 0 else user.overdraft_limit
        risk_tier = compute_risk_tier(income)
        transactions = TransactionHistory.query.filter_by(user_id=user.id).order_by(
            TransactionHistory.created_at.desc()
        ).limit(20).all()

        from models import OverdraftBuffer
        od_history = OverdraftBuffer.query.filter_by(user_id=user.id).order_by(
            OverdraftBuffer.created_at.desc()
        ).all()

        # Build chart data for past 7 days
        from datetime import datetime, timedelta
        chart_labels = []
        chart_income = []
        chart_expenses = []
        for i in range(6, -1, -1):
            day = datetime.now() - timedelta(days=i)
            label = day.strftime('%b %d')
            chart_labels.append(label)
            day_start = day.replace(hour=0, minute=0, second=0, microsecond=0)
            day_end = day.replace(hour=23, minute=59, second=59)
            day_in = sum(
                t.amount for t in transactions
                if t.direction == 'IN' and day_start <= t.created_at <= day_end
            )
            day_out = sum(
                t.amount for t in transactions
                if t.direction == 'OUT' and day_start <= t.created_at <= day_end
            )
            chart_income.append(round(day_in, 2))
            chart_expenses.append(round(day_out, 2))

        return render_template(
            'dashboard.html',
            user=user,
            wallet_balance=user.simulated_balance,
            overdraft_limit=od_limit,
            risk_tier=risk_tier,
            thirty_day_income=income,
            active_overdraft=user.active_overdraft,
            transactions=transactions,
            od_history=od_history,
            chart_labels=chart_labels,
            chart_income=chart_income,
            chart_expenses=chart_expenses,
            demo_mode=app.config.get('DEMO_MODE', False)
        )

    # Create DB tables on first run
    with app.app_context():
        db.create_all()
        _seed_demo_data(app)

    return app


def _seed_demo_data(app):
    """
    Seed the sandbox buyer (John Doe / sb-tfjr4353268252@personal.example.com)
    and 30 days of realistic gig-worker income history.

    Starting balance is set to $5,000.00 to match the real PayPal sandbox
    account default.  The demo scenario is:
        Buyer has $5,000 → wants to buy $5,200 item → $200 shortfall
        → PayFlow covers the $200 via Payouts API
        → Total owed: $200.50 (buffer + flat fee)

    When DEMO_MODE=false the checkout page fetches the live balance via
    GET /api/balance/live, which overwrites this value with the real API figure.
    """
    from models import User, TransactionHistory, OverdraftBuffer
    from datetime import datetime, timedelta
    import random

    # If user exists, ensure limit is updated to 500.0
    existing_user = User.query.first()
    if existing_user:
        existing_user.overdraft_limit = 500.0
        db.session.commit()
        return

    buyer_email = app.config.get('PAYPAL_BUYER_EMAIL', 'sb-qjdsl53206694@business.example.com')
    buyer_name = app.config.get('PAYPAL_BUYER_NAME', 'John Doe')
    buyer_account_id = app.config.get('PAYPAL_BUYER_ACCOUNT_ID', 'RTFSG6HD676ML')

    user = User(
        name=buyer_name,
        paypal_email=buyer_email,
        paypal_account_id=buyer_account_id,
        risk_tier='LOW',
        overdraft_limit=500.0,
        simulated_balance=1130.65   # Matches real PayPal sandbox account balance ($1,130.65)
    )
    db.session.add(user)
    db.session.flush()

    # Seed 30 days of realistic gig worker income history
    gig_descriptions = [
        'Freelance design project', 'Upwork payout', 'Fiverr gig payment',
        'Client invoice payment', 'Shopify store sale', 'Consulting fee',
        'Digital product sale', 'Affiliate commission', 'Tutoring session',
        'Content writing payment'
    ]

    for i in range(30):
        day = datetime.now() - timedelta(days=i)

        # 80% chance of income on any given day
        if random.random() < 0.8:
            income_amount = round(random.uniform(80.0, 200.0), 2)
            tx = TransactionHistory(
                user_id=user.id,
                paypal_tx_id=f"SEED-IN-{i:03d}",
                amount=income_amount,
                currency='USD',
                direction='IN',
                description=random.choice(gig_descriptions),
                created_at=day - timedelta(hours=random.randint(0, 8))
            )
            db.session.add(tx)

        # 40% chance of expense
        if random.random() < 0.4:
            expense_amount = round(random.uniform(5.0, 30.0), 2)
            tx = TransactionHistory(
                user_id=user.id,
                paypal_tx_id=f"SEED-OUT-{i:03d}",
                amount=expense_amount,
                currency='USD',
                direction='OUT',
                description='Business expense',
                created_at=day - timedelta(hours=random.randint(9, 20))
            )
            db.session.add(tx)

    db.session.commit()
    app.logger.info(
        f"✅ Demo data seeded: {buyer_name} ({buyer_email}) "
        f"with 30 days of transaction history | balance=$5,000.00 "
        f"(set DEMO_MODE=false to fetch live balance from PayPal API)"
    )
