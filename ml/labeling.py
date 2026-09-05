# -*- coding: utf-8 -*-
"""Label helpers for unified event generation."""


def labels_for(source: str, scenario: str, policy_violation: bool = False, fraud: bool = False) -> dict:
    risk = source in {"paysim_fraud", "agent_misuse"} or fraud or policy_violation
    if source == "hard_negative":
        risk = False
    return {
        "risk_label": int(risk),
        "fraud_label": int(fraud),
        "policy_violation_label": int(policy_violation),
    }


def is_policy_violation(event: dict, policy: dict, cum_24h: float = 0.0) -> bool:
    amount = float(event.get("amount", 0.0))
    if amount > float(policy.get("auto_limit") or 0):
        return True
    if cum_24h > float(policy.get("daily_limit") or 0):
        return True
    if event.get("tool") and event["tool"] not in (policy.get("allowed_tools") or []):
        return True
    if event.get("category") in (policy.get("blocked_categories") or []):
        return True
    return False
