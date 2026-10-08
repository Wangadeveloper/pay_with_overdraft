import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    SECRET_KEY = os.environ.get('FLASK_SECRET_KEY', 'dev-secret-change-me')
    SQLALCHEMY_DATABASE_URI = 'sqlite:///payflow.db'
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # PayPal API
    PAYPAL_CLIENT_ID = os.environ.get('PAYPAL_CLIENT_ID', '')
    PAYPAL_CLIENT_SECRET = os.environ.get('PAYPAL_CLIENT_SECRET', '')
    PAYPAL_WEBHOOK_ID = os.environ.get('PAYPAL_WEBHOOK_ID', '')
    PAYPAL_MODE = os.environ.get('PAYPAL_MODE', 'sandbox')
    PAYPAL_MERCHANT_EMAIL = os.environ.get('PAYPAL_MERCHANT_EMAIL', 'sb-qjdsl53206694@business.example.com')

    # Personal sandbox account — the PayFlow borrower/buyer
    PAYPAL_BUYER_EMAIL = os.environ.get('PAYPAL_BUYER_EMAIL', 'sb-tfjr4353268252@personal.example.com')
    PAYPAL_BUYER_ACCOUNT_ID = os.environ.get('PAYPAL_BUYER_ACCOUNT_ID', 'RTFSG6HD676ML')
    PAYPAL_BUYER_NAME = os.environ.get('PAYPAL_BUYER_NAME', 'John Doe')

    @property
    def DEMO_MODE(self):
        env_demo = os.environ.get('DEMO_MODE')
        client_id = os.environ.get('PAYPAL_CLIENT_ID', '')
        has_real_creds = bool(client_id and 'your_sandbox' not in client_id and 'here' not in client_id)
        if env_demo is not None:
            if env_demo.lower() == 'true':
                return True
            if env_demo.lower() == 'false' and has_real_creds:
                return False
        return not has_real_creds

    @property
    def PAYPAL_BASE_URL(self):
        if self.PAYPAL_MODE == 'sandbox':
            return 'https://api-m.sandbox.paypal.com'
        return 'https://api-m.paypal.com'

    # Overdraft rules
    MAX_OVERDRAFT_LIMIT = 500.0   # $500 hard cap
    OVERDRAFT_INCOME_RATIO = 0.25  # 25% of 30-day income
    FLAT_FEE = 0.50               # $0.50 per overdraft event
    DEFAULT_DAYS = 30             # Days until overdraft is paused
