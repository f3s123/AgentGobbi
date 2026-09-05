# -*- coding: utf-8 -*-
"""Train the LightGBM sequence-risk model.

Preferred input:
  data/unified_features.csv produced by:
    python ml/gen_unified_events.py
    python ml/feature_builder.py

Fallback input:
  the original PaySim + FinDelegationBench feature pipeline.
"""
from __future__ import annotations

import json
from pathlib import Path

import joblib
import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score, classification_report, roc_auc_score
from sklearn.model_selection import GroupShuffleSplit

from feature_builder import MODEL_FEATURES
from features import SEQ_FEATURES, compute_sequence_features


ROOT = Path(__file__).resolve().parent.parent
MODEL_DIR = Path(__file__).resolve().parent / "models"
MODEL_DIR.mkdir(parents=True, exist_ok=True)

UNIFIED_FEATURES = ROOT / "data" / "unified_features.csv"
WARMUP_STEP = 168
N_FRAUD_CUST = 900
N_NORMAL_CUST = 1400
UNSEEN_SCENARIO = "LOW_AND_SLOW_ATTACK"


def paysim_to_features(max_rows=None):
    df = pd.read_csv(ROOT / "data" / "paysim.csv")
    rng = np.random.default_rng(5)

    fraud_cust = df.loc[df.isFraud == 1, "nameOrig"].unique()[:N_FRAUD_CUST]
    others = df.loc[~df.nameOrig.isin(fraud_cust), "nameOrig"].unique()
    normal_cust = rng.choice(others, size=min(N_NORMAL_CUST, len(others)), replace=False)
    keep = set(fraud_cust) | set(normal_cust)
    sub = df[df.nameOrig.isin(keep)].sort_values(["nameOrig", "ts"], kind="stable")

    rows = []
    for cust, g in sub.groupby("nameOrig", sort=False):
        g = g.reset_index(drop=True)
        warm = g[g.step <= WARMUP_STEP]
        live = g[g.step > WARMUP_STEP]
        if len(live) == 0:
            continue

        if len(warm) >= 2:
            a = warm.amount.values.astype(float)
            mean_amt, std_amt = float(a.mean()), float(a.std(ddof=0)) or 1.0
            daily_tx = float(len(warm) / 7.0)
            daily_spend = float(a.sum() / 7.0)
            auto_limit = float(max(np.percentile(a, 90), 1000.0))
            cat_share = {k: float(v) for k, v in warm.type.value_counts(normalize=True).items()}
            known = {r: {"n": 1} for r in warm.nameDest.unique()}
        else:
            mean_amt, std_amt, daily_tx = 150_000.0, 300_000.0, 1.0
            daily_spend = 150_000.0
            auto_limit = 500_000.0
            cat_share, known = {"PAYMENT": 1.0}, {}

        baseline = {
            "n_tx": max(len(warm), 1),
            "amount": {"mean": mean_amt, "std": std_amt},
            "daily": {"tx_count_mean": max(daily_tx, 0.3), "amount_sum_mean": max(daily_spend, 1.0)},
            "category_share": cat_share,
            "known_recipients": known,
        }
        policy = {"auto_limit": auto_limit, "daily_limit": auto_limit * 3.0, "allowed_tools": None}
        history = [
            {"ts": float(r.ts), "amount": float(r.amount), "recipient_id": r.nameDest,
             "category": r.type, "tx_type": r.type, "status": "SUCCESS", "tool": None,
             "is_new_recipient": 0, "balance_before": float(r.oldbalanceOrg)}
            for r in warm.itertuples()
        ]
        for r in live.itertuples():
            act = {"ts": float(r.ts), "amount": float(r.amount), "recipient_id": r.nameDest,
                   "category": r.type, "tx_type": r.type, "status": "SUCCESS", "tool": None,
                   "balance_before": float(r.oldbalanceOrg), "hour": int(r.step % 24),
                   "dow": int((r.step // 24) % 7)}
            seen = set(known) | {h["recipient_id"] for h in history}
            act["is_new_recipient"] = 0 if act["recipient_id"] in seen else 1
            f = compute_sequence_features(act, history, policy, baseline)
            f["risk_label"] = int(r.isFraud)
            f["scenario"] = "PAYSIM_FRAUD" if r.isFraud else "PAYSIM_NORMAL"
            f["group"] = "PS_" + cust
            f["source"] = "paysim"
            rows.append(f)
            history.append(act)
            if max_rows and len(rows) >= max_rows:
                break
        if max_rows and len(rows) >= max_rows:
            break
    return pd.DataFrame(rows)


def load_old_training_data():
    ps = paysim_to_features()
    fb = pd.read_csv(ROOT / "data" / "findelegation_bench.csv")
    fb["group"] = "FB_" + fb.session_id.astype(str)
    fb["source"] = "bench"
    data = pd.concat([
        fb[SEQ_FEATURES + ["risk_label", "scenario", "group", "source"]],
        ps[SEQ_FEATURES + ["risk_label", "scenario", "group", "source"]],
    ], ignore_index=True)
    return data, SEQ_FEATURES, "legacy"


def load_unified_training_data():
    data = pd.read_csv(UNIFIED_FEATURES)
    data["group"] = data["session_id"].astype(str)
    return data, MODEL_FEATURES, "unified"


def load_training_data():
    if UNIFIED_FEATURES.exists():
        return load_unified_training_data()
    return load_old_training_data()


def fit_model(data: pd.DataFrame, features: list[str], train_idx, test_idx):
    X = data[features].replace([np.inf, -np.inf], np.nan).fillna(0.0).values
    y = data["risk_label"].values.astype(int)
    weights = np.where(data["source"].isin(["paysim", "paysim_fraud"]), np.where(y == 1, 18.0, 1.0), 1.0)
    weights = np.where(data["source"].eq("hard_negative"), 2.5, weights)

    model = lgb.LGBMClassifier(
        objective="binary",
        n_estimators=700,
        learning_rate=0.045,
        num_leaves=48,
        max_depth=8,
        min_child_samples=45,
        subsample=0.85,
        subsample_freq=1,
        colsample_bytree=0.85,
        reg_lambda=1.2,
        random_state=42,
        n_jobs=-1,
        verbose=-1,
    )
    model.fit(
        X[train_idx], y[train_idx],
        sample_weight=weights[train_idx],
        eval_set=[(X[test_idx], y[test_idx])],
        eval_metric="auc",
        callbacks=[lgb.early_stopping(70, verbose=False)],
    )
    return model


def report_split(name: str, model, data: pd.DataFrame, features: list[str], idx):
    if len(idx) == 0:
        return None
    X = data.iloc[idx][features].replace([np.inf, -np.inf], np.nan).fillna(0.0)
    y = data.iloc[idx]["risk_label"].values.astype(int)
    p = model.predict_proba(X)[:, 1]
    if len(np.unique(y)) < 2:
        hit_rate = float((p >= 0.5).mean())
        label = "RISKY" if int(y[0]) else "NORMAL"
        print("\n[%s] single-class=%s rows=%d predicted_risky_rate=%.3f mean_score=%.1f" % (
            name, label, len(idx), hit_rate, float(p.mean() * 100)))
        return {
            "single_class": label,
            "rows": int(len(idx)),
            "predicted_risky_rate": hit_rate,
            "mean_score": float(p.mean() * 100),
        }
    auc = roc_auc_score(y, p)
    ap = average_precision_score(y, p)
    print("\n[%s] ROC-AUC=%.4f PR-AUC=%.4f rows=%d positives=%d" % (name, auc, ap, len(idx), int(y.sum())))
    print(classification_report(y, (p >= 0.5).astype(int), target_names=["NORMAL", "RISKY"], digits=3))
    return {"roc_auc": float(auc), "pr_auc": float(ap), "rows": int(len(idx)), "positives": int(y.sum())}


def main():
    data, features, mode = load_training_data()
    data = data.replace([np.inf, -np.inf], np.nan).fillna(0.0)
    print("[lgbm] mode=%s rows=%d features=%d risky=%d" % (
        mode, len(data), len(features), int(data.risk_label.sum())))

    groups = data["group"].values
    y = data["risk_label"].values.astype(int)
    gss = GroupShuffleSplit(n_splits=1, test_size=0.25, random_state=42)
    tr, te = next(gss.split(data[features].values, y, groups))

    if mode == "unified" and UNSEEN_SCENARIO in set(data["scenario"]):
        unseen = data["scenario"].eq(UNSEEN_SCENARIO).values
        train_pool = np.where(~unseen)[0]
        gss2 = GroupShuffleSplit(n_splits=1, test_size=0.25, random_state=42)
        tr_inner, te_inner = next(gss2.split(
            data.iloc[train_pool][features].values,
            data.iloc[train_pool]["risk_label"].values,
            data.iloc[train_pool]["group"].values,
        ))
        tr = train_pool[tr_inner]
        te = train_pool[te_inner]
        unseen_idx = np.where(unseen)[0]
    else:
        unseen_idx = np.array([], dtype=int)

    print("[lgbm] train=%d test=%d split=session%s" % (
        len(tr), len(te), " + unseen scenario holdout" if len(unseen_idx) else ""))
    model = fit_model(data, features, tr, te)

    metrics = {"mode": mode, "features": features, "n_train": int(len(tr)), "n_test": int(len(te))}
    metrics["session_holdout"] = report_split("session_holdout", model, data, features, te)
    if len(unseen_idx):
        metrics["unseen_%s" % UNSEEN_SCENARIO.lower()] = report_split(
            "unseen_%s" % UNSEEN_SCENARIO.lower(), model, data, features, unseen_idx)
    if mode == "unified" and "user_id" in data:
        hold_user = sorted(data.user_id.unique())[-1]
        user_idx = np.where(data.user_id.eq(hold_user).values)[0]
        metrics["user_holdout_%s" % hold_user.lower()] = report_split(
            "user_holdout_%s" % hold_user, model, data, features, user_idx)

    imp = pd.Series(model.feature_importances_, index=features).sort_values(ascending=False)
    print("\n[lgbm] top features")
    print(imp.head(12).to_string())

    bundle = {
        "model": model,
        "features": features,
        "metrics": metrics,
        "importance": imp.to_dict(),
    }
    out = MODEL_DIR / "lgbm_sequence.pkl"
    joblib.dump(bundle, out)
    (MODEL_DIR / "lgbm_metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    print("\n[lgbm] saved -> %s" % out)


if __name__ == "__main__":
    main()
