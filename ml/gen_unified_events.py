# -*- coding: utf-8 -*-
"""Generate a unified event dataset for delegation-risk training.

The output keeps raw identifiers and labels for analysis, but these columns are
not intended to be model inputs. Feature building happens in feature_builder.py.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from labeling import is_policy_violation, labels_for
from synthetic_profiles import ACTION_TO_TOOL, baseline_for, build_profiles, policy_for


ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "unified_events.csv"
START = datetime(2026, 5, 1, 0, 0, 0)
RNG = np.random.default_rng(20260901)
DAYS = 90

HARD_NEGATIVE_SCENARIOS = [
    "PAYDAY_BURST", "TRAVEL_MODE", "NEW_RECIPIENT_NORMAL",
    "SUBSCRIPTION_BURST", "EMERGENCY_LARGE_TRANSFER", "BUSINESS_BATCH",
]
MISUSE_SCENARIOS = [
    "LIMIT_RATCHETING", "VELOCITY_ATTACK", "RECIPIENT_BURST",
    "CUMULATIVE_BYPASS", "RETRY_PATTERN", "CATEGORY_DRIFT",
    "UNAUTHORIZED_TOOL", "POLICY_CONFLICT", "LOW_AND_SLOW_ATTACK",
]


def _choice_weighted(weights: dict):
    keys = list(weights)
    probs = np.array([weights[k] for k in keys], dtype=float)
    probs = probs / probs.sum()
    return keys[int(RNG.choice(len(keys), p=probs))]


def _ts(day: int, hour_weights: dict, jitter_minutes: int = 60) -> datetime:
    hour = int(_choice_weighted(hour_weights))
    minute = int(RNG.integers(0, jitter_minutes))
    second = int(RNG.integers(0, 60))
    return START + timedelta(days=int(day), hours=hour, minutes=minute, seconds=second)


def _amount(profile, multiplier: float = 1.0, cap: float | None = None) -> float:
    mu = np.log(profile.mean_amount) - 0.5 * profile.amount_sigma ** 2
    val = float(RNG.lognormal(mu, profile.amount_sigma) * multiplier)
    if cap is not None:
        val = min(val, cap)
    return float(max(round(val, -2), 500.0))


def _recipient(profile, category: str | None = None, known: bool = True) -> dict:
    pool = profile.recipients
    if category:
        matched = [r for r in pool if r["category"] == category]
        if matched:
            pool = matched
    if known:
        return dict(pool[int(RNG.integers(0, len(pool)))])
    rid = f"{profile.user_id}-N{int(RNG.integers(100000, 999999))}"
    cat = category or str(_choice_weighted(profile.categories))
    rtype = "PERSON" if cat in {"P2P", "OVERSEAS_REMIT"} else "MERCHANT"
    return {"recipient_id": rid, "recipient_type": rtype, "category": cat}


def _event(profile, session_id: str, ts: datetime, action_type: str, amount: float,
           recipient: dict, source: str, scenario: str, status: str = "SUCCESS",
           policy_violation: bool = False, fraud: bool = False) -> dict:
    tool = ACTION_TO_TOOL[action_type]
    out = {
        "event_id": "",
        "session_id": session_id,
        "user_id": profile.user_id,
        "timestamp": ts.isoformat(),
        "ts": ts.timestamp(),
        "action_type": action_type,
        "tool": tool,
        "amount": float(amount),
        "recipient_id": recipient["recipient_id"],
        "recipient_type": recipient["recipient_type"],
        "category": recipient["category"],
        "request_status": status,
        "decision_status": "NOT_EVALUATED",
        "balance_before": 0.0,
        "policy_auto_limit": profile.auto_limit,
        "policy_daily_limit": profile.daily_limit,
        "policy_allowed_tools": "|".join(policy_for(profile)["allowed_tools"]),
        "source": source,
        "scenario": scenario,
    }
    out.update(labels_for(source, scenario, policy_violation, fraud))
    return out


def normal_user_events(profile) -> list[dict]:
    rows = []
    sid_base = f"{profile.user_id}-normal"
    for day in range(DAYS):
        for rec in profile.recurring:
            current = START + timedelta(days=day)
            if current.day == min(rec["day"], 28):
                recipient = _recipient(profile, rec["category"], known=True)
                ts = current.replace(hour=18, minute=int(RNG.integers(0, 50)), second=0)
                rows.append(_event(profile, f"{sid_base}-{day:03d}", ts, rec["action_type"],
                                   rec["amount"], recipient, "normal_user", "RECURRING_NORMAL"))
        n = int(RNG.poisson(profile.daily_tx_mean))
        for i in range(n):
            cat = str(_choice_weighted(profile.categories))
            recipient = _recipient(profile, cat, known=True)
            action_type = "TRANSFER" if recipient["recipient_type"] in {"PERSON", "SELF"} else "PAYMENT"
            rows.append(_event(profile, f"{sid_base}-{day:03d}", _ts(day, profile.hour_weights),
                               action_type, _amount(profile, cap=profile.auto_limit * 0.75),
                               recipient, "normal_user", "DAILY_NORMAL"))
    return rows


def normal_agent_events(profile) -> list[dict]:
    rows = []
    for i in range(35):
        day = int(RNG.integers(0, DAYS))
        action = str(RNG.choice(["BALANCE_READ", "HISTORY_READ", "PAYMENT", "TRANSFER"],
                                p=[0.25, 0.25, 0.35, 0.15]))
        amount = 0.0 if action in {"BALANCE_READ", "HISTORY_READ"} else _amount(profile, 0.75, profile.auto_limit * 0.65)
        rec = _recipient(profile, known=True)
        rows.append(_event(profile, f"{profile.user_id}-agent-normal-{i:03d}",
                           _ts(day, profile.hour_weights), action, amount, rec,
                           "normal_agent", "DELEGATED_NORMAL"))
    return rows


def hard_negative_events(profile) -> list[dict]:
    rows = []
    for i, scn in enumerate(HARD_NEGATIVE_SCENARIOS):
        day = int(RNG.integers(5, DAYS - 5))
        sid = f"{profile.user_id}-hard-{scn}-{i:03d}"
        if scn == "PAYDAY_BURST":
            for j in range(6):
                rows.append(_event(profile, sid, START + timedelta(days=day, hours=18, minutes=j * 7),
                                   "PAYMENT", _amount(profile, 0.9, profile.auto_limit * 0.5),
                                   _recipient(profile, known=True), "hard_negative", scn))
        elif scn == "TRAVEL_MODE":
            for j in range(8):
                rec = _recipient(profile, "OVERSEAS_REMIT", known=(profile.persona == "travel_overseas"))
                rows.append(_event(profile, sid, START + timedelta(days=day + j // 3, hours=12 + j % 6, minutes=13),
                                   "PAYMENT", _amount(profile, 1.25, profile.auto_limit * 0.8),
                                   rec, "hard_negative", scn))
        elif scn == "NEW_RECIPIENT_NORMAL":
            rec = _recipient(profile, "P2P", known=False)
            rows.append(_event(profile, sid, START + timedelta(days=day, hours=14, minutes=20),
                               "TRANSFER", min(profile.auto_limit * 0.82, profile.mean_amount * 8),
                               rec, "hard_negative", scn))
        elif scn == "SUBSCRIPTION_BURST":
            for j in range(5):
                rows.append(_event(profile, sid, START + timedelta(days=day, hours=9, minutes=j * 11),
                                   "PAYMENT", float(RNG.choice([9900, 14900, 29000, 49000])),
                                   _recipient(profile, "SUBSCRIPTION", known=True),
                                   "hard_negative", scn))
        elif scn == "EMERGENCY_LARGE_TRANSFER":
            rec = _recipient(profile, "P2P", known=False)
            rows.append(_event(profile, sid, START + timedelta(days=day, hours=11, minutes=40),
                               "TRANSFER", min(profile.auto_limit * 0.88, profile.mean_amount * 15),
                               rec, "hard_negative", scn))
        elif scn == "BUSINESS_BATCH":
            for j in range(7):
                rec = _recipient(profile, "P2P", known=profile.persona == "small_business")
                rows.append(_event(profile, sid, START + timedelta(days=day, hours=10, minutes=j * 8),
                                   "TRANSFER", min(profile.auto_limit * 0.55, profile.mean_amount * 4),
                                   rec, "hard_negative", scn))
    return rows


def misuse_events(profile) -> list[dict]:
    rows = []
    for i, scn in enumerate(MISUSE_SCENARIOS):
        day = int(RNG.integers(7, DAYS - 2))
        sid = f"{profile.user_id}-misuse-{scn}-{i:03d}"
        A = profile.auto_limit
        D = profile.daily_limit
        base = START + timedelta(days=day, hours=int(RNG.integers(1, 22)), minutes=int(RNG.integers(0, 30)))
        if scn == "LIMIT_RATCHETING":
            ratio_cases = [[0.98, 0.96, 0.99], [0.84, 0.92, 0.88, 0.95], [0.35, 0.42, 0.94, 0.97]]
            ratios = ratio_cases[int(RNG.integers(0, len(ratio_cases)))]
            for j, ratio in enumerate(ratios):
                rows.append(_event(profile, sid, base + timedelta(minutes=12 * j), "TRANSFER",
                                   A * float(ratio), _recipient(profile, "P2P", known=j < 1),
                                   "agent_misuse", scn))
        elif scn == "VELOCITY_ATTACK":
            for j in range(int(RNG.integers(8, 16))):
                rows.append(_event(profile, sid, base + timedelta(seconds=int(25 + j * RNG.integers(30, 95))),
                                   "TRANSFER", A * float(RNG.uniform(0.12, 0.48)),
                                   _recipient(profile, "P2P", known=RNG.random() < 0.55),
                                   "agent_misuse", scn))
        elif scn == "RECIPIENT_BURST":
            for j in range(int(RNG.integers(5, 11))):
                rows.append(_event(profile, sid, base + timedelta(minutes=5 * j), "TRANSFER",
                                   A * float(RNG.uniform(0.18, 0.72)),
                                   _recipient(profile, "P2P", known=False),
                                   "agent_misuse", scn))
        elif scn == "CUMULATIVE_BYPASS":
            n = int(RNG.integers(5, 10))
            for j in range(n):
                rows.append(_event(profile, sid, base + timedelta(minutes=28 * j), "TRANSFER",
                                   min(A * 0.72, D / n * float(RNG.uniform(1.05, 1.35))),
                                   _recipient(profile, "P2P", known=True),
                                   "agent_misuse", scn))
        elif scn == "RETRY_PATTERN":
            rec = _recipient(profile, "P2P", known=False)
            amt = A * float(RNG.uniform(1.15, 1.75))
            for j in range(6):
                rows.append(_event(profile, sid, base + timedelta(minutes=3 * j), "TRANSFER",
                                   amt, rec, "agent_misuse", scn,
                                   status="FAILED" if j < 3 else "SUCCESS"))
                amt *= float(RNG.uniform(0.72, 0.9))
        elif scn == "CATEGORY_DRIFT":
            for j in range(7):
                cat = str(RNG.choice(["GIFT_CARD", "OVERSEAS_REMIT", "SECURITIES"]))
                rows.append(_event(profile, sid, base + timedelta(minutes=17 * j), "PAYMENT",
                                   A * float(RNG.uniform(0.18, 0.65)),
                                   _recipient(profile, cat, known=False),
                                   "agent_misuse", scn))
        elif scn == "UNAUTHORIZED_TOOL":
            for j, action in enumerate(["LIMIT_MODIFY", "INVEST_ORDER", "CARD_ISSUE"]):
                rows.append(_event(profile, sid, base + timedelta(minutes=9 * j), action,
                                   0.0 if action != "INVEST_ORDER" else A * 0.6,
                                   _recipient(profile, "SECURITIES", known=False),
                                   "agent_misuse", scn, policy_violation=True))
        elif scn == "POLICY_CONFLICT":
            rec = _recipient(profile, "P2P", known=False)
            rows.append(_event(profile, sid, base, "TRANSFER", A * 1.18, rec,
                               "agent_misuse", scn, policy_violation=True))
        elif scn == "LOW_AND_SLOW_ATTACK":
            for j in range(12):
                known = j % 3 != 0
                rows.append(_event(profile, sid, base + timedelta(hours=2 * j), "TRANSFER",
                                   A * float(RNG.uniform(0.22, 0.55)),
                                   _recipient(profile, "P2P", known=known),
                                   "agent_misuse", scn))
    return rows


def paysim_fraud_events(profile) -> list[dict]:
    rows = []
    for i in range(8):
        day = int(RNG.integers(10, DAYS - 1))
        sid = f"{profile.user_id}-paysim-fraud-{i:03d}"
        base = START + timedelta(days=day, hours=int(RNG.integers(0, 5)), minutes=int(RNG.integers(0, 40)))
        balance = profile.opening_balance * float(RNG.uniform(0.45, 1.35))
        first = balance * float(RNG.uniform(0.6, 0.98))
        rec = _recipient(profile, "P2P", known=False)
        rows.append(_event(profile, sid, base, "TRANSFER", first, rec,
                           "paysim_fraud", "ACCOUNT_DRAIN_TRANSFER", fraud=True))
        rows.append(_event(profile, sid, base + timedelta(minutes=int(RNG.integers(1, 5))),
                           "TRANSFER", max(balance - first, profile.mean_amount), rec,
                           "paysim_fraud", "FOLLOW_UP_CASHOUT_PROXY", fraud=True))
    return rows


def assign_running_fields(rows: list[dict], profiles_by_id: dict) -> list[dict]:
    rows = sorted(rows, key=lambda r: (r["user_id"], r["timestamp"], r["session_id"]))
    balances = {uid: p.opening_balance * 2 for uid, p in profiles_by_id.items()}
    seen = {uid: set(r["recipient_id"] for r in p.recipients) for uid, p in profiles_by_id.items()}
    by_user_day = {}
    out = []
    for idx, r in enumerate(rows):
        uid = r["user_id"]
        day_key = (uid, r["timestamp"][:10])
        cum = by_user_day.get(day_key, 0.0) + float(r["amount"])
        policy = policy_for(profiles_by_id[uid])
        if r["source"] in {"normal_user", "normal_agent", "hard_negative"}:
            r["risk_label"] = 0
            r["fraud_label"] = 0
            r["policy_violation_label"] = 0
        elif is_policy_violation(r, policy, cum):
            r["policy_violation_label"] = 1
            r["risk_label"] = 1
        r["balance_before"] = max(float(balances[uid]), float(r["amount"]) + profiles_by_id[uid].mean_amount)
        r["is_new_recipient"] = int(r["recipient_id"] not in seen[uid])
        seen[uid].add(r["recipient_id"])
        by_user_day[day_key] = cum
        balances[uid] = max(r["balance_before"] - float(r["amount"]), profiles_by_id[uid].opening_balance * 0.25)
        r["event_id"] = f"E{idx + 1:07d}"
        out.append(r)
    return out


def build() -> pd.DataFrame:
    profiles = build_profiles()
    by_id = {p.user_id: p for p in profiles}
    rows = []
    for profile in profiles:
        rows.extend(normal_user_events(profile))
        rows.extend(normal_agent_events(profile))
        rows.extend(hard_negative_events(profile))
        rows.extend(misuse_events(profile))
        rows.extend(paysim_fraud_events(profile))
    rows = assign_running_fields(rows, by_id)
    df = pd.DataFrame(rows)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUT, index=False, encoding="utf-8")
    return df


if __name__ == "__main__":
    df = build()
    print("[unified-events] rows=%d users=%d sessions=%d -> %s" % (
        len(df), df.user_id.nunique(), df.session_id.nunique(), OUT))
    print(df.groupby(["source", "risk_label"]).size().unstack(fill_value=0).to_string())
