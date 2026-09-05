# -*- coding: utf-8 -*-
"""Synthetic user profiles for the unified delegation-risk dataset."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


CATEGORIES = [
    "FOOD", "CONVENIENCE", "TRANSPORT", "SIMPLE_PAY", "SHOPPING",
    "SUBSCRIPTION", "P2P", "RENT", "UTILITY", "SAVINGS",
    "ENTERTAIN", "LIVING", "CLOUD", "ETC", "OVERSEAS_REMIT",
    "GIFT_CARD", "SECURITIES",
]

ACTION_TO_TOOL = {
    "BALANCE_READ": "balance.read",
    "HISTORY_READ": "history.read",
    "TRANSFER": "transfer.execute",
    "PAYMENT": "payment.execute",
    "RECIPIENT_REGISTER": "recipient.register",
    "LIMIT_MODIFY": "limit.modify",
    "INVEST_ORDER": "invest.order",
    "CARD_ISSUE": "card.issue",
}


@dataclass(frozen=True)
class UserProfile:
    user_id: str
    persona: str
    mean_amount: float
    amount_sigma: float
    daily_tx_mean: float
    opening_balance: float
    auto_limit: float
    daily_limit: float
    categories: dict
    hour_weights: dict
    recurring: list
    recipients: list
    allowed_actions: list


def _normalize(weights: dict) -> dict:
    total = float(sum(weights.values()))
    return {k: float(v) / total for k, v in weights.items()}


def _recipients(prefix: str, categories: dict, n: int) -> list:
    cats = list(categories)
    probs = np.array([categories[c] for c in cats], dtype=float)
    probs = probs / probs.sum()
    out = []
    for i in range(n):
        cat = str(np.random.choice(cats, p=probs))
        rtype = "PERSON" if cat == "P2P" else "BILL" if cat in {"RENT", "UTILITY", "SAVINGS"} else "MERCHANT"
        out.append({
            "recipient_id": f"{prefix}-R{i:03d}",
            "recipient_type": rtype,
            "category": cat,
        })
    return out


def build_profiles(seed: int = 20260901) -> list[UserProfile]:
    rng = np.random.default_rng(seed)
    np.random.seed(seed)
    specs = [
        (
            "U001", "salary_worker",
            {"FOOD": 22, "SIMPLE_PAY": 18, "TRANSPORT": 14, "SHOPPING": 12,
             "SUBSCRIPTION": 8, "P2P": 8, "RENT": 8, "UTILITY": 6, "ETC": 4},
            42000, 1.0, 2.4, 680000, 1800000,
        ),
        (
            "U002", "shopping_heavy",
            {"SHOPPING": 30, "SIMPLE_PAY": 22, "FOOD": 14, "CONVENIENCE": 10,
             "SUBSCRIPTION": 8, "P2P": 6, "TRANSPORT": 6, "ETC": 4},
            65000, 1.15, 3.8, 900000, 2600000,
        ),
        (
            "U003", "p2p_student",
            {"P2P": 26, "FOOD": 20, "CONVENIENCE": 16, "SIMPLE_PAY": 14,
             "TRANSPORT": 12, "ENTERTAIN": 8, "ETC": 4},
            28000, 0.95, 3.2, 350000, 950000,
        ),
        (
            "U004", "travel_overseas",
            {"FOOD": 17, "SHOPPING": 14, "SIMPLE_PAY": 14, "OVERSEAS_REMIT": 8,
             "TRANSPORT": 13, "SUBSCRIPTION": 8, "P2P": 8, "LIVING": 8, "ETC": 10},
            82000, 1.25, 2.6, 1200000, 3200000,
        ),
        (
            "U005", "small_business",
            {"P2P": 24, "CLOUD": 12, "SHOPPING": 14, "UTILITY": 10,
             "FOOD": 10, "TRANSPORT": 8, "SIMPLE_PAY": 8, "SUBSCRIPTION": 6, "ETC": 8},
            185000, 1.05, 5.4, 2200000, 8500000,
        ),
        (
            "U006", "investor",
            {"SECURITIES": 20, "P2P": 14, "FOOD": 12, "SHOPPING": 12,
             "SIMPLE_PAY": 12, "SUBSCRIPTION": 8, "TRANSPORT": 8, "ETC": 14},
            110000, 1.35, 2.1, 1600000, 4500000,
        ),
    ]
    profiles = []
    for uid, persona, cats, mean, sigma, daily_tx, auto, daily in specs:
        cats = _normalize(cats)
        hours = _normalize({
            0: 1, 1: 1, 2: 0.6, 3: 0.4, 4: 0.3, 5: 0.5,
            6: 1.2, 7: 2.2, 8: 2.8, 9: 2.0, 10: 2.2, 11: 3.0,
            12: 3.6, 13: 2.8, 14: 2.4, 15: 2.3, 16: 2.5, 17: 3.0,
            18: 3.8, 19: 3.5, 20: 3.1, 21: 2.6, 22: 1.8, 23: 1.2,
        })
        recipients = _recipients(uid, cats, int(rng.integers(18, 44)))
        recurring = [
            {"name": "rent", "category": "RENT", "amount": 605000, "day": 25, "action_type": "TRANSFER"},
            {"name": "phone", "category": "UTILITY", "amount": 63000, "day": 9, "action_type": "PAYMENT"},
            {"name": "subscription", "category": "SUBSCRIPTION", "amount": 14900, "day": 30, "action_type": "PAYMENT"},
        ]
        if persona == "small_business":
            recurring.append({"name": "cloud", "category": "CLOUD", "amount": 320000, "day": 3, "action_type": "PAYMENT"})
        allowed = ["BALANCE_READ", "HISTORY_READ", "TRANSFER", "PAYMENT"]
        profiles.append(UserProfile(
            user_id=uid,
            persona=persona,
            mean_amount=float(mean),
            amount_sigma=float(sigma),
            daily_tx_mean=float(daily_tx),
            opening_balance=float(mean * rng.uniform(35, 95)),
            auto_limit=float(auto),
            daily_limit=float(daily),
            categories=cats,
            hour_weights=hours,
            recurring=recurring,
            recipients=recipients,
            allowed_actions=allowed,
        ))
    return profiles


def policy_for(profile: UserProfile) -> dict:
    return {
        "auto_limit": profile.auto_limit,
        "daily_limit": profile.daily_limit,
        "allowed_actions": list(profile.allowed_actions),
        "allowed_tools": [ACTION_TO_TOOL[a] for a in profile.allowed_actions],
        "blocked_categories": [],
        "allowed_categories": None,
        "new_recipient": {"action": "VERIFY", "amount_threshold": 0},
    }


def baseline_for(profile: UserProfile) -> dict:
    known = {r["recipient_id"]: {"n": 4, "category": r["category"]} for r in profile.recipients}
    return {
        "n_tx": int(profile.daily_tx_mean * 90),
        "amount": {
            "mean": profile.mean_amount,
            "std": max(profile.mean_amount * profile.amount_sigma, 1.0),
            "log_mu": float(np.log(profile.mean_amount) - 0.5 * profile.amount_sigma ** 2),
            "log_sigma": profile.amount_sigma,
        },
        "daily": {
            "tx_count_mean": profile.daily_tx_mean,
            "amount_sum_mean": profile.mean_amount * profile.daily_tx_mean,
        },
        "category_share": dict(profile.categories),
        "known_recipients": known,
        "opening_balance": profile.opening_balance,
    }
