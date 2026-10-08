"""
PayPal REST API HTTP client.
Handles OAuth2 token caching, Orders API v2, Payouts API v1,
Transaction Search/Reporting API v1, and Webhook signature verification.
"""
import time
import uuid
import requests
from datetime import datetime, timedelta
from flask import current_app


class PayPalClient:
    _token_cache = {}  # class-level cache: {client_id: {token, expires_at}}

    def __init__(self):
        self.client_id = current_app.config['PAYPAL_CLIENT_ID']
        self.client_secret = current_app.config['PAYPAL_CLIENT_SECRET']
        self.base_url = current_app.config.get('PAYPAL_BASE_URL', 'https://api-m.sandbox.paypal.com')

    def get_access_token(self) -> str:
        """Fetch and cache OAuth2 client-credentials token."""
        cache = PayPalClient._token_cache
        cached = cache.get(self.client_id)
        if cached and time.time() < cached['expires_at'] - 60:
            return cached['token']

        url = f"{self.base_url}/v1/oauth2/token"
        resp = requests.post(
            url,
            auth=(self.client_id, self.client_secret),
            data={'grant_type': 'client_credentials'},
            timeout=30
        )
        resp.raise_for_status()
        data = resp.json()
        token = data['access_token']
        expires_in = data.get('expires_in', 32400)  # 9 hours default

        cache[self.client_id] = {
            'token': token,
            'expires_at': time.time() + expires_in
        }
        return token

    def _headers(self) -> dict:
        return {
            'Authorization': f'Bearer {self.get_access_token()}',
            'Content-Type': 'application/json',
            'PayPal-Request-Id': str(uuid.uuid4()),
        }

    # ─── Orders API v2 ─────────────────────────────────────────────────────────

    def create_order(self, amount: float, currency: str = 'USD') -> dict:
        """Create a PayPal order. Returns full response dict."""
        url = f"{self.base_url}/v2/checkout/orders"
        payload = {
            'intent': 'CAPTURE',
            'purchase_units': [{
                'amount': {
                    'currency_code': currency,
                    'value': f"{amount:.2f}"
                },
                'description': 'PayFlow Overdraft Protected Checkout'
            }]
        }
        resp = requests.post(url, json=payload, headers=self._headers(), timeout=15)
        resp.raise_for_status()
        return resp.json()

    def capture_order(self, order_id: str) -> dict:
        """Capture an approved PayPal order."""
        url = f"{self.base_url}/v2/checkout/orders/{order_id}/capture"
        resp = requests.post(url, json={}, headers=self._headers(), timeout=15)
        resp.raise_for_status()
        return resp.json()

    def get_order(self, order_id: str) -> dict:
        """Get order details."""
        url = f"{self.base_url}/v2/checkout/orders/{order_id}"
        resp = requests.get(url, headers=self._headers(), timeout=10)
        resp.raise_for_status()
        return resp.json()

    # ─── Payouts API v1 ────────────────────────────────────────────────────────

    def send_payout(self, receiver_email: str, amount: float, note: str = '', currency: str = 'USD') -> dict:
        """
        Disburse approved credit buffer via Payouts API v1.

        Flow: Business Sandbox account (funding source) → Personal Sandbox account (buyer/borrower).
        The buyer then has sufficient funds to complete the PayPal checkout.

        Args:
            receiver_email: The personal sandbox buyer's PayPal email
                            (sb-tfjr4353268252@personal.example.com)
            amount: Shortfall amount to cover (e.g. $25.00)
            note: Human-readable note on the payout item
            currency: ISO currency code, default 'USD'

        Returns:
            PayPal Payouts API response dict including batch_header.payout_batch_id
        """
        url = f"{self.base_url}/v1/payments/payouts"
        batch_id = f"PAYFLOW-{uuid.uuid4().hex[:12].upper()}"
        payload = {
            'sender_batch_header': {
                'sender_batch_id': batch_id,
                'email_subject': 'PayFlow Overdraft Credit',
                'email_message': (
                    f'Your PayFlow overdraft buffer of ${amount:.2f} has been activated. '
                    'These funds have been added to your PayPal balance so you can complete your purchase.'
                )
            },
            'items': [{
                'recipient_type': 'EMAIL',
                'amount': {
                    'value': f"{amount:.2f}",
                    'currency': currency
                },
                'note': note or f'PayFlow overdraft buffer: ${amount:.2f} credit disbursement',
                'receiver': receiver_email,
                'sender_item_id': f"ITEM-{uuid.uuid4().hex[:8].upper()}"
            }]
        }
        resp = requests.post(url, json=payload, headers=self._headers(), timeout=15)
        resp.raise_for_status()
        return resp.json()

    def get_payout_batch_status(self, batch_id: str) -> dict:
        """Check status of a payout batch."""
        url = f"{self.base_url}/v1/payments/payouts/{batch_id}"
        resp = requests.get(url, headers=self._headers(), timeout=10)
        resp.raise_for_status()
        return resp.json()

    def get_payout_item_status(self, payout_item_id: str) -> dict:
        """
        Check status of an individual payout item.
        Returns transaction_status: PENDING | UNCLAIMED | RETURNED | ONHOLD | BLOCKED | SUCCESS | FAILED
        """
        url = f"{self.base_url}/v1/payments/payouts-item/{payout_item_id}"
        resp = requests.get(url, headers=self._headers(), timeout=10)
        resp.raise_for_status()
        return resp.json()

    # ─── Transaction Search / Reporting API v1 ─────────────────────────────────

    def get_transaction_history(
        self,
        start_date: datetime = None,
        end_date: datetime = None,
        transaction_type: str = None,
        page_size: int = 100
    ) -> dict:
        """
        Fetch transaction history via Transaction Search API v1.
        Used for income scoring: sums all incoming payments over the past 30 days.

        Args:
            start_date: Start of date range (default: 31 days ago)
            end_date: End of date range (default: now)
            transaction_type: Optional PayPal transaction type filter (e.g. 'T0007' = payment received)
            page_size: Number of results per page (max 500)

        Returns:
            PayPal Transaction Search response with 'transaction_details' list

        Note:
            Requires the 'https://uri.paypal.com/services/reporting/search/read' scope.
            This is typically included for business accounts with reporting enabled.
        """
        if end_date is None:
            end_date = datetime.utcnow()
        if start_date is None:
            start_date = end_date - timedelta(days=31)

        # PayPal requires ISO 8601 format with timezone offset
        start_str = start_date.strftime('%Y-%m-%dT%H:%M:%S-0000')
        end_str = end_date.strftime('%Y-%m-%dT%H:%M:%S-0000')

        url = f"{self.base_url}/v1/reporting/transactions"
        params = {
            'start_date': start_str,
            'end_date': end_str,
            'page_size': page_size,
            'page': 1,
            'fields': 'all',
        }
        if transaction_type:
            params['transaction_type'] = transaction_type

        # Remove Content-Type for GET requests, keep auth
        headers = {
            'Authorization': f'Bearer {self.get_access_token()}',
        }

        resp = requests.get(url, params=params, headers=headers, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def get_account_balance(self, currency: str = 'USD') -> dict:
        """
        Fetch current account balance via Balances API v1.

        Uses the client-credentials (business) OAuth token, so this returns
        the BUSINESS sandbox account's balance.  For the demo the personal
        buyer's sandbox account also starts at $5,000, so the same figure
        is displayed as the buyer's wallet balance.

        Returns a dict that includes:
          - 'available_usd'  float — the primary available balance in USD
          - 'total_usd'      float — the total (including withheld) balance
          - 'currency'       str   — the queried currency code
          - 'raw'            dict  — the raw API response
        """
        url = f"{self.base_url}/v1/reporting/balances"
        params = {
            'as_of_time': datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S+0000'),
            'currency_code': currency
        }
        headers = {
            'Authorization': f'Bearer {self.get_access_token()}',
        }
        resp = requests.get(url, params=params, headers=headers, timeout=15)
        resp.raise_for_status()
        raw = resp.json()

        # Parse the balances list for the requested currency
        balances = raw.get('balances', [])
        available_usd = 0.0
        total_usd = 0.0
        for entry in balances:
            if entry.get('currency_code', entry.get('currency', '')) == currency:
                available_usd = float(
                    entry.get('available_balance', {}).get('value', 0) or
                    entry.get('available_amount', {}).get('value', 0) or 0
                )
                total_usd = float(
                    entry.get('total_balance', {}).get('value', 0) or
                    entry.get('total_amount', {}).get('value', 0) or 0
                )
                break

        return {
            'available_usd': available_usd,
            'total_usd': total_usd,
            'currency': currency,
            'raw': raw
        }

    # ─── Webhook Verification ──────────────────────────────────────────────────

    def verify_webhook_signature(self, headers: dict, raw_body: bytes) -> bool:
        """
        Verify PayPal webhook signature using PayPal's verification API.
        https://developer.paypal.com/docs/api/webhooks/v1/#verify-webhook-signature
        """
        webhook_id = current_app.config.get('PAYPAL_WEBHOOK_ID', '')
        if not webhook_id:
            # In demo mode, skip verification
            return True

        url = f"{self.base_url}/v1/notifications/verify-webhook-signature"
        payload = {
            'auth_algo': headers.get('PAYPAL-AUTH-ALGO', ''),
            'cert_url': headers.get('PAYPAL-CERT-URL', ''),
            'transmission_id': headers.get('PAYPAL-TRANSMISSION-ID', ''),
            'transmission_sig': headers.get('PAYPAL-TRANSMISSION-SIG', ''),
            'transmission_time': headers.get('PAYPAL-TRANSMISSION-TIME', ''),
            'webhook_id': webhook_id,
            'webhook_event': raw_body.decode('utf-8')
        }
        try:
            resp = requests.post(url, json=payload, headers=self._headers(), timeout=10)
            data = resp.json()
            return data.get('verification_status') == 'SUCCESS'
        except Exception:
            return False
