"""
PayPal REST API HTTP client.
Handles OAuth2 token caching, Orders API v2, Payouts API v1,
Transaction Search/Reporting API v1, Balances API, and Webhook signature verification.

Two client classes:
  - PayPalClient       : Uses the main (business/funder) app credentials.
  - BuyerPayPalClient  : Uses dedicated buyer app credentials (PAYPAL_BUYER_CLIENT_ID /
                         PAYPAL_BUYER_CLIENT_SECRET) to call the Balances API on behalf
                         of the personal buyer account.
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
        Fetch current PayFlow Funder Pool balance via PayPal Balances API v1.

        Queries the BUSINESS sandbox account (sb-qjdsl53206694@business.example.com)
        which is the PayFlow liquidity pool — its available balance is the
        funding source for all overdraft payouts disbursed to buyers.
        Returns diagnostic details including HTTP status code, available USD, account ID, and raw payload.
        """
        url = f"{self.base_url}/v1/reporting/balances"
        params = {
            'as_of_time': datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S+0000'),
            'currency_code': currency
        }
        try:
            headers = {
                'Authorization': f'Bearer {self.get_access_token()}',
                'Accept': 'application/json',
                'PayPal-Enforce-ISO8601-Format': 'true',
            }
            resp = requests.get(url, params=params, headers=headers, timeout=15)
            status_code = resp.status_code
            debug_id = resp.headers.get('PayPal-Debug-Id', '')
            
            if not resp.ok:
                return {
                    'success': False,
                    'status_code': status_code,
                    'debug_id': debug_id,
                    'error': f"PayPal API returned status {status_code}",
                    'raw': resp.json() if resp.headers.get('Content-Type', '').startswith('application/json') else resp.text[:500],
                    'available_usd': 0.0,
                    'total_usd': 0.0,
                    'account_id': '',
                    'currency': currency
                }

            raw = resp.json()
            balances = raw.get('balances', [])
            available_usd = 0.0
            total_usd = 0.0
            account_id = raw.get('account_id', '')

            for entry in balances:
                curr = entry.get('currency_code') or entry.get('currency') or ''
                if curr == currency or not curr:
                    avail_dict = entry.get('available_balance') or entry.get('available_amount') or {}
                    total_dict = entry.get('total_balance') or entry.get('total_amount') or {}
                    available_usd = float(avail_dict.get('value', 0) or 0)
                    total_usd = float(total_dict.get('value', 0) or 0)
                    break

            return {
                'success': True,
                'status_code': status_code,
                'debug_id': debug_id,
                'available_usd': available_usd,
                'total_usd': total_usd,
                'currency': currency,
                'account_id': account_id,
                'raw': raw
            }
        except Exception as exc:
            return {
                'success': False,
                'status_code': 500,
                'debug_id': '',
                'error': str(exc),
                'available_usd': 0.0,
                'total_usd': 0.0,
                'account_id': '',
                'currency': currency,
                'raw': {}
            }

    def get_buyer_balance(self, buyer_email: str = None, currency: str = 'USD') -> dict:
        """
        Fetch the live wallet balance for the personal buyer account
        (sb-tfjr4353268252@personal.example.com).

        Strategy (in priority order):
          1. BuyerPayPalClient — if PAYPAL_BUYER_CLIENT_ID + PAYPAL_BUYER_CLIENT_SECRET
             are set, authenticate as the buyer's own sandbox app and call
             GET /v1/reporting/balances. This is the only true way to read the
             personal account's real balance.
          2. Identity/UserInfo fallback — call GET /v1/identity/oauth2/userinfo
             to confirm account linkage; useful for diagnostics when buyer
             credentials aren't configured yet.

        Returns a dict with:
          available_usd  : float balance (or None on error)
          source         : 'paypal_buyer_balances_api' | 'buyer_credentials_missing' | 'error'
          success        : bool
        """
        if not buyer_email:
            buyer_email = current_app.config.get(
                'PAYPAL_BUYER_EMAIL', 'sb-tfjr4353268252@personal.example.com'
            )

        buyer_client_id = current_app.config.get('PAYPAL_BUYER_CLIENT_ID', '').strip()
        buyer_client_secret = current_app.config.get('PAYPAL_BUYER_CLIENT_SECRET', '').strip()

        # ── Strategy 1: Dedicated buyer app credentials ──────────────────────────
        if buyer_client_id and buyer_client_secret:
            try:
                buyer_client = BuyerPayPalClient(
                    client_id=buyer_client_id,
                    client_secret=buyer_client_secret,
                    base_url=self.base_url
                )
                result = buyer_client.get_balance(currency=currency)
                result['buyer_email'] = buyer_email
                result['source'] = 'paypal_buyer_balances_api'
                current_app.logger.info(
                    f"[BuyerBalance] Live fetch via buyer credentials → "
                    f"${result.get('available_usd', 0.0):.2f} {currency} "
                    f"for {buyer_email}"
                )
                return result
            except Exception as exc:
                current_app.logger.warning(
                    f"[BuyerBalance] Buyer credentials auth failed for {buyer_email}: {exc}"
                )
                return {
                    'buyer_email': buyer_email,
                    'available_usd': None,
                    'currency': currency,
                    'source': 'error',
                    'success': False,
                    'error': str(exc)
                }

        # ── Strategy 2: No buyer credentials configured ───────────────────────────
        # We cannot read a personal account's balance using business credentials.
        # Return explicit advisory so the UI/logs surface the gap.
        current_app.logger.warning(
            f"[BuyerBalance] PAYPAL_BUYER_CLIENT_ID not configured — cannot fetch "
            f"live balance for personal account {buyer_email}. "
            "Set PAYPAL_BUYER_CLIENT_ID and PAYPAL_BUYER_CLIENT_SECRET in .env to enable."
        )
        return {
            'buyer_email': buyer_email,
            'available_usd': None,
            'currency': currency,
            'source': 'buyer_credentials_missing',
            'success': False,
            'error': (
                'Buyer app credentials (PAYPAL_BUYER_CLIENT_ID / PAYPAL_BUYER_CLIENT_SECRET) '
                'are not configured. Add them to .env to fetch live personal account balance.'
            )
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


# ─── Buyer PayPal Client ───────────────────────────────────────────────────────

class BuyerPayPalClient:
    """
    Lightweight PayPal OAuth2 client for the personal buyer sandbox account.

    PayPal's Balances API (GET /v1/reporting/balances) is account-scoped:
    it returns the balance of the account whose credentials were used to obtain
    the access token.  To read sb-tfjr4353268252@personal.example.com's real
    wallet balance we must authenticate using an App that was created *under*
    that personal sandbox account, not the business/funder app.

    Setup (one-time, in PayPal Developer Dashboard):
      1. Log in to developer.paypal.com with the SANDBOX PERSONAL account.
      2. Apps & Credentials → Create App (sandbox).
      3. Copy Client ID + Secret → paste into .env as:
             PAYPAL_BUYER_CLIENT_ID=<id>
             PAYPAL_BUYER_CLIENT_SECRET=<secret>
      4. Ensure the app has the "Transaction Search" and "Reporting" scopes.
    """

    _token_cache: dict = {}  # {client_id: {token, expires_at}}

    def __init__(self, client_id: str, client_secret: str, base_url: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.base_url = base_url.rstrip('/')

    # ── OAuth2 token (client_credentials, personal account app) ─────────────

    def _get_access_token(self) -> str:
        """Fetch and cache OAuth2 bearer token for the buyer's sandbox app."""
        cache = BuyerPayPalClient._token_cache
        cached = cache.get(self.client_id)
        if cached and time.time() < cached['expires_at'] - 60:
            return cached['token']

        resp = requests.post(
            f"{self.base_url}/v1/oauth2/token",
            auth=(self.client_id, self.client_secret),
            data={'grant_type': 'client_credentials'},
            timeout=30
        )
        resp.raise_for_status()
        data = resp.json()
        token = data['access_token']
        expires_in = data.get('expires_in', 32400)
        cache[self.client_id] = {
            'token': token,
            'expires_at': time.time() + expires_in
        }
        return token

    def _headers(self) -> dict:
        return {
            'Authorization': f'Bearer {self._get_access_token()}',
            'Accept': 'application/json',
            'PayPal-Enforce-ISO8601-Format': 'true',
        }

    # ── Balances API ─────────────────────────────────────────────────────────

    def get_balance(self, currency: str = 'USD') -> dict:
        """
        Call GET /v1/reporting/balances authenticated as the personal buyer account.

        Returns:
            {
              'success'       : bool,
              'available_usd' : float,
              'total_usd'     : float,
              'currency'      : str,
              'account_id'    : str,
              'status_code'   : int,
              'debug_id'      : str,
              'raw'           : dict
            }
        """
        url = f"{self.base_url}/v1/reporting/balances"
        params = {
            'as_of_time': datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S+0000'),
            'currency_code': currency
        }
        try:
            resp = requests.get(url, params=params, headers=self._headers(), timeout=15)
            status_code = resp.status_code
            debug_id = resp.headers.get('PayPal-Debug-Id', '')

            if not resp.ok:
                raw_err = {}
                try:
                    raw_err = resp.json()
                except Exception:
                    raw_err = {'text': resp.text[:500]}
                return {
                    'success': False,
                    'status_code': status_code,
                    'debug_id': debug_id,
                    'error': f"PayPal Buyer Balances API returned HTTP {status_code}",
                    'available_usd': None,
                    'total_usd': 0.0,
                    'account_id': '',
                    'currency': currency,
                    'raw': raw_err
                }

            raw = resp.json()
            balances = raw.get('balances', [])
            available_usd = 0.0
            total_usd = 0.0
            account_id = raw.get('account_id', '')

            for entry in balances:
                curr = entry.get('currency_code') or entry.get('currency') or ''
                if curr == currency or not curr:
                    avail_dict = (
                        entry.get('available_balance')
                        or entry.get('available_amount')
                        or {}
                    )
                    total_dict = (
                        entry.get('total_balance')
                        or entry.get('total_amount')
                        or {}
                    )
                    available_usd = float(avail_dict.get('value', 0) or 0)
                    total_usd = float(total_dict.get('value', 0) or 0)
                    break

            return {
                'success': True,
                'status_code': status_code,
                'debug_id': debug_id,
                'available_usd': available_usd,
                'total_usd': total_usd,
                'currency': currency,
                'account_id': account_id,
                'raw': raw
            }

        except requests.exceptions.RequestException as exc:
            return {
                'success': False,
                'status_code': 502,
                'debug_id': '',
                'error': f"Network error fetching buyer balance: {exc}",
                'available_usd': None,
                'total_usd': 0.0,
                'account_id': '',
                'currency': currency,
                'raw': {}
            }
