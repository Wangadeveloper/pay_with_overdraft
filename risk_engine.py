"""
Micro-Risk Scoring Engine.
Calculates overdraft limit tiers based on 30-day income history.
"""
from flask import current_app


def compute_risk_tier(thirty_day_income: float) -> str:
    """Assign risk tier based on monthly income."""
    if thirty_day_income >= 500:
        return 'LOW'
    elif thirty_day_income >= 150:
        return 'MEDIUM'
    else:
        return 'HIGH'


def compute_overdraft_limit(thirty_day_income: float) -> float:
    """
    Overdraft ceiling formula:
        limit = min($500, 25% × 30d_income)
    Social-safety cap at $500 to prevent debt traps.
    """
    max_limit = current_app.config.get('MAX_OVERDRAFT_LIMIT', 500.0)
    ratio = current_app.config.get('OVERDRAFT_INCOME_RATIO', 0.25)
    raw = thirty_day_income * ratio
    return round(min(max_limit, raw), 2)


def detect_shortfall(order_amount: float, wallet_balance: float) -> dict:
    """
    Gap detector engine.
    Returns a dict with shortfall amount and whether overdraft is needed.
    """
    shortfall = round(order_amount - wallet_balance, 2)
    if shortfall <= 0:
        return {'needs_overdraft': False, 'shortfall': 0.0}
    return {
        'needs_overdraft': True,
        'shortfall': shortfall,
        'wallet_balance': wallet_balance,
        'order_amount': order_amount,
    }


def can_cover_shortfall(shortfall: float, overdraft_limit: float, active_owed: float = 0.0) -> dict:
    """
    Check if user's overdraft limit can cover the shortfall.
    Considers already-owed amounts (active overdraft buffers).
    """
    available = overdraft_limit - active_owed
    if shortfall <= 0:
        return {'can_cover': True, 'reason': 'no_shortfall', 'available': available}
    if shortfall > available:
        return {
            'can_cover': False,
            'reason': 'limit_exceeded',
            'shortfall': shortfall,
            'available': available,
            'overdraft_limit': overdraft_limit
        }
    return {
        'can_cover': True,
        'shortfall': shortfall,
        'available': available,
        'remaining_after': round(available - shortfall, 2)
    }
