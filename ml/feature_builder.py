# -*- coding: utf-8 -*-
"""Build leakage-aware model features from unified_events.csv."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from features import SEQ_FEATURES, compute_sequence_features
from synthetic_profiles import baseline_for, build_profiles, policy_for


ROOT = Path(__file__).resolve().parent.parent
IN_EVENTS = ROOT / "data" / "unified_events.csv"
OUT_FEATURES = ROOT / "data" / "unified_features.csv"
OUT_META = ROOT / "data" / "unified_feature_meta.json"

# Deliberately exclude direct policy answer features from the first LGBM pass.
MODEL_FEATURES = [f for f in SEQ_FEATURES if f != "unauthorized_tool"]


def _policy_from_row(row) -> dict:
    allowed_tools = str(row.policy_allowed_tools).split("|") if row.policy_allowed_tools else []
    return {
        "auto_limit": float(row.policy_auto_limit),
        "daily_limit": float(row.policy_daily_limit),
        "allowed_tools": allowed_tools,
        "allowed_actions": [],
        "blocked_categories": [],
        "allowed_categories": None,
    }


def _baseline_from_history(profile, history) -> dict:
    base = baseline_for(profile)
    if not history:
        return base

    amounts = np.array([h["amount"] for h in history if h["amount"] > 0], dtype=float)
    if len(amounts):
        base["amount"]["mean"] = float(amounts.mean())
        base["amount"]["std"] = float(amounts.std(ddof=0) or profile.mean_amount)
        base["daily"]["amount_sum_mean"] = float(max(amounts.sum() / 30.0, 1.0))
    base["n_tx"] = len(history)

    rec_counts = {}
    cat_counts = {}
    for h in history:
        rec_counts[h["recipient_id"]] = rec_counts.get(h["recipient_id"], 0) + 1
        cat_counts[h["category"]] = cat_counts.get(h["category"], 0) + 1
    n = max(len(history), 1)
    base["known_recipients"] = {k: {"n": int(v)} for k, v in rec_counts.items()}
    base["category_share"] = {k: float(v) / n for k, v in cat_counts.items()}
    base["daily"]["tx_count_mean"] = max(float(len(history) / 30.0), 0.3)
    return base


def build() -> pd.DataFrame:
    if not IN_EVENTS.exists():
        raise FileNotFoundError("Run ml/gen_unified_events.py first: %s" % IN_EVENTS)

    df = pd.read_csv(IN_EVENTS)
    df = df.sort_values(["user_id", "timestamp", "session_id"], kind="stable").reset_index(drop=True)
    profiles = {p.user_id: p for p in build_profiles()}

    histories = {uid: [] for uid in profiles}
    rows = []
    for row in df.itertuples(index=False):
        profile = profiles[row.user_id]
        history = histories[row.user_id]
        action = {
            "ts": float(row.ts),
            "amount": float(row.amount),
            "recipient_id": row.recipient_id,
            "category": row.category,
            "tx_type": "TRANSFER" if row.action_type == "TRANSFER" else "PAYMENT",
            "action_type": row.action_type,
            "tool": row.tool,
            # FAILED_BY_BANK is allowed for retry features; decision outcomes stay out.
            "status": "FAILED" if row.request_status != "SUCCESS" else "SUCCESS",
            "hour": int(pd.Timestamp(row.timestamp).hour),
            "dow": int(pd.Timestamp(row.timestamp).dayofweek),
            "balance_before": float(row.balance_before),
            "is_new_recipient": int(row.is_new_recipient),
        }
        feat = compute_sequence_features(
            action,
            history,
            _policy_from_row(row),
            _baseline_from_history(profile, history),
        )
        out = {name: float(feat[name]) for name in SEQ_FEATURES}
        out.update({
            "event_id": row.event_id,
            "session_id": row.session_id,
            "user_id": row.user_id,
            "source": row.source,
            "scenario": row.scenario,
            "risk_label": int(row.risk_label),
            "fraud_label": int(row.fraud_label),
            "policy_violation_label": int(row.policy_violation_label),
        })
        rows.append(out)
        history.append(action)

    features = pd.DataFrame(rows)
    features.to_csv(OUT_FEATURES, index=False, encoding="utf-8")
    OUT_META.write_text(json.dumps({
        "input": str(IN_EVENTS),
        "output": str(OUT_FEATURES),
        "all_sequence_features": SEQ_FEATURES,
        "model_features": MODEL_FEATURES,
        "excluded_model_columns": [
            "event_id", "session_id", "user_id", "source", "scenario",
            "risk_label", "fraud_label", "policy_violation_label",
            "unauthorized_tool",
        ],
    }, indent=2), encoding="utf-8")
    return features


if __name__ == "__main__":
    df = build()
    print("[features] rows=%d features=%d -> %s" % (len(df), len(MODEL_FEATURES), OUT_FEATURES))
    print(df.groupby(["source", "risk_label"]).size().unstack(fill_value=0).to_string())
