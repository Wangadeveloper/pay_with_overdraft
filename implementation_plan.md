# PayFlow: Personal Sandbox Account Integration & Architecture Upgrade

## Background

The PayFlow app already has a solid Flask skeleton: Orders API v2, Payouts API, webhook listener, risk engine, and a demo user. However, the architecture has some gaps relative to the expert advice:

1. **Wrong demo user email** — the app seeds `peterwanga326@gmail.com` everywhere, but the **real personal sandbox buyer** is `sb-tfjr4353268252@personal.example.com` (balance $5,000, Account ID `RTFSG6HD676ML`). This must be updated so live PayPal Sandbox flows use the actual test account.
2. **Payout recipient is wrong** — `send_payout()` currently sends to `user.paypal_email` which is the seeded mock email, not the real sandbox buyer's email.
3. **Missing Transaction Search API** — the income data currently comes from locally-seeded fake transactions. The expert advice says to use PayPal's Transaction Search/Reporting API as the income-data source.
4. **Missing overdraft state machine** — the expert advice recommends a clear `OVERDRAFT_REQUESTED → RISK_APPROVED → PAYOUT_PENDING → PAYOUT_SUCCESS → CHECKOUT_COMPLETED → REPAYMENT_PENDING → REPAYMENT_RECEIVED → CLOSED` state machine, but the current `OverdraftBuffer.status` only has `ACTIVE / REPAID / DEFAULTED / DECLINED`.
5. **Payout flow is tangled with capture** — the Payouts API call happens inside `capture_order()`. The correct flow is: **Payout first → buyer receives funds → buyer then completes PayPal checkout**. Currently the payout and capture happen simultaneously in the same request.
6. **Webhook subscriptions not comprehensive** — only `PAYMENT.CAPTURE.COMPLETED`, `PAYMENT.CAPTURE.DENIED`, and `PAYMENT.PAYOUTS-ITEM.SUCCEEDED` are handled. We should also handle `PAYMENT.PAYOUTS-ITEM.FAILED` for error recovery.
7. **No Transaction Search integration in paypal_client.py** — need to add `GET /v1/reporting/transactions` calls.
8. **Business account config missing** — the `.env` has `PAYPAL_MERCHANT_EMAIL` but no `PAYPAL_BUYER_EMAIL` (the personal sandbox account).

## Key Design Decisions

> [!IMPORTANT]
> **Account Roles:**
> - **Personal sandbox** (`sb-tfjr4353268252@personal.example.com`) = the **Borrower/Buyer** (PayFlow customer)
> - **Business sandbox** (`sb-qjdsl53206694@business.example.com`) = the **Merchant AND Funding Account** (source of payouts + receives checkout payment)
>
> The Business account's API credentials (Client ID + Secret) are what the Flask app uses. The Business account sends payouts TO the Personal account.

> [!WARNING]
> **Payout capability**: The Payouts API requires the business app to have Payouts enabled in the PayPal Developer dashboard. Verify this is checked under your app's "Features" before running live payout calls.

## Proposed Changes

---

### 1. Configuration — `.env` & `config.py`

#### [MODIFY] [.env](file:///home/engineer-wanga/Desktop/Paypal_hackathon/payflow-flask/.env)
- Add `PAYPAL_BUYER_EMAIL=sb-tfjr4353268252@personal.example.com` — the personal sandbox account used as the PayFlow borrower.
- Add `PAYPAL_BUYER_ACCOUNT_ID=RTFSG6HD676ML` — the account ID for reference.

#### [MODIFY] [config.py](file:///home/engineer-wanga/Desktop/Paypal_hackathon/payflow-flask/config.py)
- Expose `PAYPAL_BUYER_EMAIL` and `PAYPAL_BUYER_ACCOUNT_ID` from env.

---

### 2. PayPal Client — `paypal_client.py`

#### [MODIFY] [paypal_client.py](file:///home/engineer-wanga/Desktop/Paypal_hackathon/payflow-flask/paypal_client.py)
Add two new methods:
- **`get_transaction_history(start_date, end_date)`** — calls `GET /v1/reporting/transactions` to fetch real PayPal transaction history for income scoring.
- **`get_payout_item_status(payout_item_id)`** — checks individual payout item status (not just batch).
- Fix `send_payout()` docstring to clarify it sends FROM the business account TO the buyer.

---

### 3. Database Models — `models.py`

#### [MODIFY] [models.py](file:///home/engineer-wanga/Desktop/Paypal_hackathon/payflow-flask/models.py)
- **Expand `OverdraftBuffer.status`** to full state machine values:
  `OVERDRAFT_REQUESTED → RISK_APPROVED → PAYOUT_PENDING → PAYOUT_SUCCESS → CHECKOUT_COMPLETED → REPAYMENT_PENDING → REPAYMENT_RECEIVED → CLOSED`
  (Keep backward compat: `ACTIVE` = `PAYOUT_SUCCESS`, `REPAID` = `REPAYMENT_RECEIVED`)
- Add `payout_item_id` column to `OverdraftBuffer` for tracking individual payout item status.
- Update `active_overdraft` property to match the new in-flight states.

---

### 4. App Seed Data — `app.py`

#### [MODIFY] [app.py](file:///home/engineer-wanga/Desktop/Paypal_hackathon/payflow-flask/app.py)
- Change seeded user email from `peterwanga326@gmail.com` → `sb-tfjr4353268252@personal.example.com`
- Change seeded user name from `Peter Wanga` → `John Doe` (matching sandbox account)
- Set `paypal_account_id = 'RTFSG6HD676ML'`
- Set `simulated_balance = 15.00` (matching the demo scenario: buyer has $15, wants to buy $40 item)

---

### 5. All Blueprints — `get_demo_user()` email update

#### [MODIFY] [checkout.py](file:///home/engineer-wanga/Desktop/Paypal_hackathon/payflow-flask/blueprints/checkout.py)
#### [MODIFY] [overdraft.py](file:///home/engineer-wanga/Desktop/Paypal_hackathon/payflow-flask/blueprints/overdraft.py)
#### [MODIFY] [webhooks.py](file:///home/engineer-wanga/Desktop/Paypal_hackathon/payflow-flask/blueprints/webhooks.py)
#### [MODIFY] [ai_agent.py](file:///home/engineer-wanga/Desktop/Paypal_hackathon/payflow-flask/blueprints/ai_agent.py)

Update all `get_demo_user()` references from `peterwanga326@gmail.com` → read from `current_app.config['PAYPAL_BUYER_EMAIL']` (or use the sandbox email directly).

---

### 6. Checkout Flow — `checkout.py` (Core Architecture Fix)

#### [MODIFY] [checkout.py](file:///home/engineer-wanga/Desktop/Paypal_hackathon/payflow-flask/blueprints/checkout.py)

**Correct the PayFlow sequence:**

```
Step 1: POST /api/overdraft/initiate
  → shortfall detected
  → risk engine evaluates
  → if approved: send Payout (Business → Personal)
  → record buffer with status=PAYOUT_PENDING
  → return payout_batch_id

Step 2: GET /api/overdraft/payout-status/<batch_id>
  → poll/check payout landed (PAYOUT_SUCCESS)
  → UI shows "✅ $25 added to your PayPal wallet"

Step 3: POST /api/orders  (create PayPal order)
Step 4: PayPal JS SDK → buyer approves checkout
Step 5: POST /api/orders/<id>/capture
  → record status=CHECKOUT_COMPLETED
  → begin repayment tracking (REPAYMENT_PENDING)
```

Currently steps 1-5 are collapsed into a single capture endpoint, which is architecturally wrong.

Add new endpoints:
- `POST /api/overdraft/initiate` — gap detection + risk check + Payout disbursement
- `GET /api/overdraft/payout-status/<batch_id>` — check payout landed in buyer wallet

---

### 7. Transaction Search Integration — New endpoint in `overdraft.py`

#### [MODIFY] [overdraft.py](file:///home/engineer-wanga/Desktop/Paypal_hackathon/payflow-flask/blueprints/overdraft.py)

Add `GET /api/income/refresh` endpoint that:
1. Calls `paypal_client.get_transaction_history()` for the last 30 days
2. Syncs real PayPal transaction data into `TransactionHistory` table
3. Returns computed 30-day income and updated overdraft limit

This replaces the reliance on manually-seeded fake data with real Sandbox transaction data.

---

### 8. Webhooks — `webhooks.py`

#### [MODIFY] [webhooks.py](file:///home/engineer-wanga/Desktop/Paypal_hackathon/payflow-flask/blueprints/webhooks.py)

Add handler for `PAYMENT.PAYOUTS-ITEM.FAILED`:
- Sets `OverdraftBuffer.status = 'DECLINED'`
- Logs the failure reason

Update `handle_payout_succeeded()` to properly advance state from `PAYOUT_PENDING → PAYOUT_SUCCESS`.

---

### 9. Checkout UI — `checkout.html`

#### [MODIFY] [checkout.html](file:///home/engineer-wanga/Desktop/Paypal_hackathon/payflow-flask/templates/checkout.html)

Update the sandbox testing tip to correctly name the personal buyer account:
```
Personal Buyer: sb-tfjr4353268252@personal.example.com
Balance: $5,000 USD (sandbox)
```

Update JavaScript flow to implement the correct two-phase sequence:
1. **Phase 1**: Call `/api/overdraft/initiate` → show payout confirmation → poll for payout success
2. **Phase 2**: Then render PayPal checkout button for buyer to approve

---

### 10. `.env` — Add buyer email

#### [MODIFY] [.env](file:///home/engineer-wanga/Desktop/Paypal_hackathon/payflow-flask/.env)
```
PAYPAL_BUYER_EMAIL=sb-tfjr4353268252@personal.example.com
PAYPAL_BUYER_ACCOUNT_ID=RTFSG6HD676ML
```

---

## Demo Scenario After Changes

```
Starting state:
  Personal buyer (sb-tfjr4353268252@personal.example.com):
    PayPal balance: $5,000 (sandbox) — simulated as $15.00 in app
  Business merchant (sb-qjdsl53206694@business.example.com):
    PayPal balance: $5,000 (sandbox)

Step 1: Buyer wants $40 item
  → App detects $25 shortfall (wallet=$15, order=$40)
  → Risk engine: income=$100/30d → limit=$25 ✅

Step 2: PayFlow initiates payout
  → Business acct sends $25 to Personal acct via Payouts API
  → OverdraftBuffer.status = PAYOUT_PENDING

Step 3: Payout lands (webhook: PAYMENT.PAYOUTS-ITEM.SUCCEEDED)
  → OverdraftBuffer.status = PAYOUT_SUCCESS
  → Buyer balance now: $5,025 (sandbox) / $40.00 (simulated)

Step 4: Buyer completes PayPal checkout
  → $40 captured → Business merchant receives $40
  → OverdraftBuffer.status = CHECKOUT_COMPLETED → REPAYMENT_PENDING

Step 5: Next incoming payment to buyer triggers repayment
  → webhook: PAYMENT.CAPTURE.COMPLETED
  → $25.50 auto-deducted
  → OverdraftBuffer.status = REPAYMENT_RECEIVED → CLOSED
```

## Verification Plan

### Automated
- Run `pytest tests/` to ensure existing unit tests still pass after email/config changes.

### Manual Sandbox Verification
1. **API auth**: `curl -X POST ... /v1/oauth2/token` returns valid token ✅
2. **Payout test**: Call `/api/overdraft/initiate` with amount=$40 → business acct sends $25 to `sb-tfjr4353268252@personal.example.com`
3. **Sandbox dashboard**: Log into PayPal Sandbox as personal buyer → verify $25 credit appears in balance
4. **Checkout**: Use PayPal JS SDK button → log in as personal buyer → approve $40 payment
5. **Webhook simulation**: Use PayPal Sandbox webhook simulator or `/webhooks/simulate` to trigger repayment

### Open Questions for User

> [!IMPORTANT]
> **Q1: Is Payouts enabled on your Developer App?**
> In PayPal Developer Dashboard → Your App → Features, check that "Payouts" is ticked. Without this, `POST /v1/payments/payouts` will return 403.

> [!IMPORTANT]
> **Q2: Do you want to keep the $15 simulated balance for the demo scenario?**
> The sandbox personal account has $5,000 but the app simulates $15 to demonstrate the overdraft. Should we keep this simulation, or attempt to read the real sandbox balance via the Balances API?

> [!NOTE]
> **Q3: Transaction Search Scope**
> Transaction Search requires the `reporting` scope. This should be automatically included in the client-credentials token for a business account, but confirm by checking the token's scope. If unavailable, we keep using seeded data for income scoring.
