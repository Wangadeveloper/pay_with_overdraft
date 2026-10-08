# PayFlow Overdraft

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
