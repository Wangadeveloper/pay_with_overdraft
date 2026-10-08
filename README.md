# PayFlow Overdraft — "Fuliza for PayPal"
> **An income-backed instant credit buffer for gig workers when their PayPal wallet falls short at checkout. Modeled on Kenya's Fuliza, built for the global gig economy.**

---

## Inspiration

In emerging markets and gig-economy sectors, freelancers and micro-merchants frequently face short-term wallet liquidity mismatches. When a PayPal balance falls short by a small margin ($5 to $50), essential transactions fail—ranging from inventory restocks and cloud software renewals to utility payments. Inspired by the massive success of Kenya’s **Fuliza**—which provides instant overdrafts at checkout for M-Pesa users—we set out to adapt this continuous liquidity model for PayPal. PayFlow Overdraft bridges the short-term cash flow gap, ensuring gig workers never lose a deal or service due to a transient shortfall.

## What it does

**PayFlow Overdraft** provides an income-backed, instant credit buffer directly at PayPal checkout, accompanied by an AI financial assistant:

* **Gap Detection & Buffer Injection:** Automatically detects wallet shortfalls at checkout and injects the exact missing amount from an approved credit line via PayPal Payouts API v1 so the order completes seamlessly.
* **Micro-Risk Credit Scoring:** Dynamically calculates an overdraft ceiling based on 30-day incoming PayPal transaction history (capped at $150 or 25% of monthly income).
* **Automated Repayment:** Automatically captures outstanding debts plus a flat fee ($0.50 for shortfalls under $50) from the user's next incoming deposit via PayPal Webhooks.
* **Conversational AI Assistant:** Integrated using the PayPal AI Toolkit / MCP Server, allowing users to check limits, review repayment plans, and receive account health advice in natural language.

## How we built it

* **Backend Framework:** Built with Python and Flask using a Blueprint architecture separating payment interception, micro-risk scoring, webhooks, and AI components.
* **Database & ORM:** Managed via SQLAlchemy with core models tracking `User` risk tiers, `OverdraftBuffer` credit lines, `Transaction History`, and `RepaymentLog` audit trails.
* **PayPal Developer Integrations:**
  * **Orders API v2:** Intercepts checkout and handles payment settlement.
  * **Payouts API v1:** Dynamically funds balance gaps into the order pool.
  * **Webhooks API:** Listens for `PAYMENT.CAPTURE.COMPLETED` events to execute auto-repayments.
* **AI & Tooling:** Configured local development environments using the PayPal AI Toolkit, MCP Server, and Cursor plugin to ensure adherence to PayPal REST standards and power the conversational dashboard agent.

## Challenges we ran into

* **Asynchronous Webhook Testing:** Simulating real-time incoming transfer events in sandbox environments required configuring local tunneling tools (ngrok) and verifying webhook payload signatures during auto-recovery execution.
* **Transaction Interception Timing:** Ensuring that the balance gap calculation and Payouts injection occurred synchronously before the Orders API final capture without increasing checkout latency.
* **Responsible Credit Engine Design:** Structuring a zero-interest, flat-fee risk engine that protects users from predatory compounding debt traps while maintaining healthy credit recovery mechanics.

## Accomplishments that we're proud of

* **End-to-End Sandbox Flow:** Successfully executing the complete lifecycle: checkout shortfall detection $\rightarrow$ auto-payout injection $\rightarrow$ order completion $\rightarrow$ automated webhook repayment on subsequent deposit.
* **Ethical Financial Model:** Designing a social-impact-focused fee structure ($0.50 flat fee) with built-in protections like paused credit lines instead of escalating penalties during defaults.
* **Seamless AI Integration:** Connecting natural language assistant tooling directly into the Flask application so gig workers can query complex financial state data effortlessly.

## What we learned

* **PayPal REST & MCP Tooling:** Mastered multi-API orchestrations across Orders, Payouts, Webhooks, and Reporting APIs using standard Python SDKs and PayPal MCP Server tooling.
* **Liquidity & Risk Modeling:** Gained deep insights into micro-credit scoring algorithms tailored for informal and variable-income workers in the global gig economy.

## What's next for pay_with_overdraft

* **Multi-Currency & Cross-Border Overdrafts:** Expand credit risk calculations to support auto-conversion for international freelancers working across diverse currencies.
* **Tiered Financial Wellness Insights:** Enhance the AI assistant to offer personalized cash-flow forecasting and proactive budgeting alerts before shortfalls occur.
* **Merchant Partner Integrations:** Partner directly with gig platforms and digital marketplaces to offer tailored overdraft limits based on verified platform earnings.

---

## 🚀 Quick Start

```bash
# 1. Create virtual environment
python3 -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure environment
cp .env.example .env
# Edit .env with your PayPal sandbox credentials

# 4. Run the app (Demo Mode — no PayPal credentials needed)
DEMO_MODE=true python run.py

# 5. Open in browser
# http://localhost:5000
```

---

## 📋 PayPal Sandbox Setup

1. Go to [developer.paypal.com](https://developer.paypal.com)
2. Create a **Sandbox App** under Apps & Credentials
3. Copy **Client ID** and **Client Secret** into your `.env` file
4. Create a **Sandbox Buyer Account** under Sandbox → Accounts
5. Add a webhook URL (use `ngrok http 5000` for local dev):
   - Event: `PAYMENT.CAPTURE.COMPLETED`
   - Copy the **Webhook ID** into `.env`

---

## 🏗️ Architecture

```
payflow-flask/
├── app.py                  # Flask factory + demo data seeder
├── config.py               # Environment config
├── models.py               # SQLAlchemy: User, OverdraftBuffer, TransactionHistory, RepaymentLog
├── paypal_client.py        # PayPal REST client (OAuth2 + Orders v2 + Payouts v1 + Webhooks)
├── risk_engine.py          # Micro-risk scorer: limit = min($150, 25% × 30d_income)
├── blueprints/
│   ├── checkout.py         # Orders API v2 — gap detection & overdraft injection
│   ├── overdraft.py        # Overdraft status, manual repayment, income simulation
│   ├── webhooks.py         # PAYMENT.CAPTURE.COMPLETED auto-repayment
│   └── ai_agent.py         # Rule-based AI chat assistant
├── templates/              # Jinja2 dark glassmorphism UI
│   ├── base.html
│   ├── dashboard.html
│   ├── checkout.html
│   ├── overdraft.html
│   └── ai_chat.html
├── static/
│   ├── css/styles.css      # Premium CSS design system
│   └── js/main.js
└── tests/
    └── test_overdraft.py   # All 3 hackathon scenarios
```

---

## 💡 Key Features

| Feature | Description |
|---------|-------------|
| **Gap Detector** | Detects wallet shortfall at checkout in real-time |
| **Micro-Risk Engine** | `limit = min($150, 25% × 30d_income)` |
| **Buffer Injection** | Covers shortfall via PayPal Payouts API v1 |
| **Auto-Repayment** | Webhook clears debt on next incoming payment |
| **AI Assistant** | Chat UI for balance, limit, and repayment queries |
| **Demo Mode** | Full offline demo with seeded sandbox data |

---

## 🧪 Running Tests

```bash
# From the project directory
pytest tests/ -v
```

All 3 hackathon scenarios are covered:
1. ✅ Partial balance shortfall ($40 order, $15 balance → $25 injection)
2. ✅ Webhook auto-recovery (incoming $50 → $25.50 debt cleared)
3. ✅ Exceeded limit ($200 order → graceful decline)

---

## 🔧 Environment Variables

| Variable | Description | Required |
|----------|-------------|----------|
| `PAYPAL_CLIENT_ID` | Sandbox app Client ID | Live mode only |
| `PAYPAL_CLIENT_SECRET` | Sandbox app Client Secret | Live mode only |
| `PAYPAL_WEBHOOK_ID` | Webhook ID from dashboard | For webhook verification |
| `PAYPAL_MODE` | `sandbox` or `live` | Default: `sandbox` |
| `DEMO_MODE` | `true` = no PayPal API calls | Default: `false` |
| `FLASK_SECRET_KEY` | Session security key | Yes |

---

*Built with ❤️ for the PayPal DevPost Hackathon 2026 — Financial Inclusion Track*
