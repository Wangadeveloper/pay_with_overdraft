"""
AI Agent Blueprint — Gemini-powered conversational assistant for PayFlow.

Uses the google-genai SDK for natural-language understanding and contextual responses when
GEMINI_API_KEY (or GOOGLE_API_KEY) is configured. Falls back to the existing
rule-based responses if Gemini is unavailable or returns an invalid response.
"""
from flask import Blueprint, request, jsonify, render_template, current_app
from models import db, User, OverdraftBuffer
from risk_engine import compute_overdraft_limit, compute_risk_tier
from datetime import datetime
import json
import os
import re

ai_bp = Blueprint("ai", __name__)


def get_demo_user():
    """Get the PayFlow buyer user (personal sandbox account)."""
    buyer_email = current_app.config.get(
        "PAYPAL_BUYER_EMAIL",
        "sb-tfjr4353268252@personal.example.com",
    )
    return User.query.filter_by(paypal_email=buyer_email).first()


# ─── Intent Patterns (fallback) ──────────────────────────────────────────────

INTENTS = [
    {
        "patterns": [r"limit", r"how much.*overdraft", r"overdraft.*limit", r"maximum.*borrow"],
        "handler": "overdraft_limit",
    },
    {
        "patterns": [r"repay", r"pay.*back", r"owe", r"debt", r"when.*due"],
        "handler": "repayment_info",
    },
    {
        "patterns": [r"balance", r"wallet", r"how much.*have", r"funds"],
        "handler": "wallet_balance",
    },
    {
        "patterns": [r"risk", r"tier", r"score", r"rating"],
        "handler": "risk_tier",
    },
    {
        "patterns": [r"income", r"earn", r"revenue", r"payment.*received"],
        "handler": "income_info",
    },
    {
        "patterns": [r"fee", r"charge", r"cost", r"interest"],
        "handler": "fee_info",
    },
    {
        "patterns": [r"how.*work", r"what.*payflow", r"explain", r"help"],
        "handler": "how_it_works",
    },
    {
        "patterns": [r"default", r"late", r"missed", r"overdue"],
        "handler": "default_info",
    },
    {
        "patterns": [r"hello", r"hi", r"hey", r"greet"],
        "handler": "greeting",
    },
]


def detect_intent(message: str) -> str:
    msg_lower = message.lower()
    for intent in INTENTS:
        for pattern in intent["patterns"]:
            if re.search(pattern, msg_lower):
                return intent["handler"]
    return "unknown"


def build_response(intent: str, user: User) -> dict:
    """Rule-based fallback response using current user data."""
    income = float(user.thirty_day_income or 0)
    od_limit = compute_overdraft_limit(income) if income > 0 else float(user.overdraft_limit or 0)
    risk_tier = compute_risk_tier(income)
    active_od = user.active_overdraft

    responses = {
        "greeting": {
            "message": (
                f"👋 Hi {user.name}! I'm your PayFlow assistant. I can help you check "
                "your overdraft limit, repayment schedule, wallet balance, and more. "
                "What would you like to know?"
            ),
            "suggestions": ["Check my overdraft limit", "When is my repayment due?", "Check my wallet balance"],
        },
        "overdraft_limit": {
            "message": (
                f"💳 Your current overdraft limit is **${od_limit:.2f}**.\n\n"
                f"This is calculated as 25% of your 30-day income of **${income:.2f}**, "
                "capped at a maximum of $150 to protect you from over-borrowing.\n\n"
                f"Risk tier: **{risk_tier}** — "
                f"{'Great standing! Lower risk = easier approval.' if risk_tier == 'LOW' else 'Build more income history to improve your tier.'}"
            ),
            "suggestions": ["How is my income calculated?", "What are the fees?", "Check my wallet balance"],
        },
        "repayment_info": {
            "message": (
                f"📅 **Active Overdraft:** ${active_od.amount_covered:.2f} + "
                f"${active_od.flat_fee:.2f} fee = **${active_od.total_owed:.2f} owed**\n"
                f"Due by: **{active_od.due_date.strftime('%B %d, %Y')}**\n"
                f"{'⚠️ ' + str(active_od.days_overdue) + ' days overdue' if active_od.days_overdue > 0 else '✅ On track'}\n\n"
                "Repayment is **automatic** — when you next receive a PayPal payment, "
                "the overdraft is cleared instantly!"
                if active_od else
                f"✅ You have **no active overdraft**. Your account is clear!\n\n"
                f"Your overdraft limit is **${od_limit:.2f}** — ready to use when you need it."
            ),
            "suggestions": ["How do I repay manually?", "What happens if I default?", "Check my overdraft limit"],
        },
        "wallet_balance": {
            "message": (
                f"💰 Your current PayPal wallet balance is **${float(user.simulated_balance or 0):.2f}**.\n\n"
                + (
                    f"You have an active overdraft of **${active_od.total_owed:.2f}** owed."
                    if active_od else
                    f"No active overdraft. Your net balance is **${float(user.simulated_balance or 0):.2f}**."
                )
            ),
            "suggestions": ["Check my overdraft limit", "Simulate receiving payment", "How does overdraft work?"],
        },
        "risk_tier": {
            "message": (
                f"📊 Your risk tier is **{risk_tier}**.\n\n"
                "- **LOW**: 30-day income ≥ $500 — highest limits, instant approval\n"
                "- **MEDIUM**: 30-day income $150–$499 — moderate limits\n"
                "- **HIGH**: 30-day income < $150 — limited overdraft, build history first\n\n"
                f"Your 30-day income: **${income:.2f}** → Tier: **{risk_tier}**"
            ),
            "suggestions": ["How do I improve my tier?", "What is my overdraft limit?", "How is income calculated?"],
        },
        "income_info": {
            "message": (
                f"📈 Your **30-day PayPal income** is **${income:.2f}**.\n\n"
                "This is calculated from all incoming PayPal payments in the last 30 days. "
                "The more consistent your income, the higher your overdraft limit.\n\n"
                f"Current limit = 25% × ${income:.2f} = **${od_limit:.2f}**"
            ),
            "suggestions": ["What is my risk tier?", "What is my overdraft limit?", "What are the fees?"],
        },
        "fee_info": {
            "message": (
                "💡 **PayFlow Overdraft Pricing — Transparent & Fair**\n\n"
                "- **Flat fee**: Only $0.50 per overdraft event (regardless of amount)\n"
                "- **Zero interest**: We don't charge compounding interest — ever\n"
                "- **No penalty escalation**: If you miss the 30-day window, the overdraft is simply paused — no extra charges\n\n"
                "This design prevents debt traps that harm gig workers."
            ),
            "suggestions": ["What happens if I default?", "What is my overdraft limit?", "How does auto-repayment work?"],
        },
        "how_it_works": {
            "message": (
                "🚀 **How PayFlow Overdraft Works**\n\n"
                "1. **Checkout** — You try to pay for something but your wallet is short\n"
                "2. **Gap Detection** — PayFlow detects the shortfall in milliseconds\n"
                "3. **Risk Check** — Your 30-day income history is scored instantly\n"
                "4. **Buffer Injection** — The shortfall is covered via PayPal Payouts API\n"
                "5. **Auto-Repayment** — Your next incoming PayPal payment automatically clears the debt\n\n"
                "Inspired by Kenya's **Fuliza** (M-Pesa), designed for the global gig economy. 🌍"
            ),
            "suggestions": ["What is my overdraft limit?", "What are the fees?", "Check my wallet balance"],
        },
        "default_info": {
            "message": (
                "🛡️ **Default Protection — No Debt Traps**\n\n"
                "If you don't repay within **30 days**:\n"
                "- ❌ Your overdraft line is **paused** (not penalized)\n"
                "- 💰 Future incoming PayPal payments will be split: part goes to repayment\n"
                "- 🚫 No late fees, no interest escalation, no collections\n\n"
                "PayFlow is built on **responsible micro-credit** principles for gig workers."
            ),
            "suggestions": ["What are the fees?", "How does repayment work?", "Check my risk tier"],
        },
        "unknown": {
            "message": f"I'm not sure I understood that, {user.name}. Here are some things I can help with:",
            "suggestions": [
                "What is my overdraft limit?",
                "When is my repayment due?",
                "Check my wallet balance",
                "How does PayFlow work?",
            ],
        },
    }
    return responses.get(intent, responses["unknown"])


def get_live_user_context(user: User) -> dict:
    """Build the live, minimal context sent to Gemini for this request."""
    income = float(user.thirty_day_income or 0)
    active_od = user.active_overdraft
    od_limit = (
        compute_overdraft_limit(income)
        if income > 0
        else float(user.overdraft_limit or 0)
    )
    return {
        "name": user.name or "PayFlow user",
        "wallet_balance": round(float(user.simulated_balance or 0), 2),
        "thirty_day_income": round(income, 2),
        "overdraft_limit": round(float(od_limit or 0), 2),
        "risk_tier": compute_risk_tier(income),
        "active_overdraft": (
            {
                "amount_covered": round(float(active_od.amount_covered or 0), 2),
                "fee": round(float(active_od.flat_fee or 0), 2),
                "total_owed": round(float(active_od.total_owed or 0), 2),
                "due_date": active_od.due_date.isoformat() if active_od.due_date else None,
                "days_overdue": int(active_od.days_overdue or 0),
            }
            if active_od else None
        ),
    }


def generate_gemini_response(message: str, context: dict) -> dict | None:
    """
    Ask Gemini for a natural-language response using the supported google-genai SDK.

    Returns a validated {message, suggestions} dict, or None if Gemini is
    unavailable so the caller can use the rule-based fallback.
    """
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        current_app.logger.info("Gemini API key not configured; using rule-based fallback.")
        return None

    try:
        # google-genai is the current SDK. Install with:
        # python -m pip install -U google-genai
        from google import genai
        from google.genai import types

        client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=60_000),
        )

        prompt = f"""
You are PayFlow's friendly financial product assistant.
Answer the user's question using the supplied current account context and the
PayFlow product facts below. Be concise, helpful, and clear. Use Markdown in
the message field when useful.

Rules:
- Treat the account context as the source of truth for personal account values.
- Never invent balances, dates, fees, eligibility, or transactions.
- If a needed fact is not provided, say you cannot confirm it.
- Do not claim to perform payments, repayments, or account changes.
- Do not promise credit approval. Explain limits as estimates where appropriate.
- Keep the answer about PayFlow, overdrafts, repayment, income, risk tiers, fees,
  and general financial-product help.
- Return ONLY valid JSON with exactly these keys:
  {{"message": "string", "suggestions": ["string", "string", "string"]}}
- Suggestions must be 2 to 4 short follow-up questions/actions.

PayFlow product facts (from the existing app's fallback responses):
- The app describes the overdraft limit as 25% of 30-day income, capped at $150.
- The app describes a flat $0.50 fee per overdraft event and no compounding interest.
- The app describes a 30-day repayment window and automatic repayment from a
  future incoming PayPal payment. Do not imply this is guaranteed if the context
  does not confirm it.

Current user context:
{json.dumps(context, ensure_ascii=False)}

User message:
{message}
"""

        model_name = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
        result = client.models.generate_content(
            model=model_name,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.3,
                max_output_tokens=1050,
                response_mime_type="application/json",
            ),
        )
        raw = (getattr(result, "text", None) or "").strip()
        if not raw:
            current_app.logger.warning("Gemini returned an empty response.")
            return None

        # Tolerate JSON fenced by Markdown, even if the model ignores JSON mode.
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE).strip()
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            return None

        answer = parsed.get("message")
        suggestions = parsed.get("suggestions", [])

        if not isinstance(answer, str) or not answer.strip():
            return None
        if not isinstance(suggestions, list):
            suggestions = []
        suggestions = [
            str(item).strip()
            for item in suggestions
            if isinstance(item, (str, int, float)) and str(item).strip()
        ][:4]

        return {
            "message": answer.strip(),
            "suggestions": suggestions or [
                "What is my overdraft limit?",
                "When is my repayment due?",
                "How can I improve my risk tier?",
            ],
        }
    except Exception as exc:
        # Keep chat available when the provider is slow, unreachable, misconfigured,
        # or returns invalid JSON. Do not expose API credentials or provider errors.
        current_app.logger.warning(
            "Gemini request failed (%s); using rule-based fallback.",
            type(exc).__name__,
        )
        current_app.logger.debug("Gemini failure details", exc_info=True)
        return None


@ai_bp.route("/ai")
def ai_chat_page():
    user = get_demo_user()
    return render_template("ai_chat.html", user=user)


@ai_bp.route("/ai/chat", methods=["POST"])
def chat():
    data = request.get_json(silent=True) or {}
    message = data.get("message", "")
    if not isinstance(message, str):
        return jsonify({"error": "invalid_message"}), 400
    message = message.strip()

    if not message:
        return jsonify({"error": "empty_message"}), 400

    user = get_demo_user()
    if not user:
        return jsonify({"error": "user_not_found"}), 404

    context = get_live_user_context(user)
    intent = detect_intent(message)

    # Gemini handles natural-language questions; rule-based answers remain the
    # reliable fallback if Gemini is not configured or any API step fails.
    response = generate_gemini_response(message, context)
    ai_provider = "gemini" if response else "rule_based"
    if response is None:
        response = build_response(intent, user)

    return jsonify({
        "intent": intent,
        "message": response["message"],
        "suggestions": response.get("suggestions", []),
        "provider": ai_provider,
        "timestamp": datetime.now().isoformat(),
    })
