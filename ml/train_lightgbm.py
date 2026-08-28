# -*- coding: utf-8 -*-
"""
LightGBM 학습 — Agent 행동 시퀀스 위험도
------------------------------------------------
학습 데이터 2종을 동일한 SEQ_FEATURES 공간으로 합쳐 학습한다.

  (1) data/paysim.csv               PaySim 시뮬레이션 결과. 라벨 = isFraud
      고객(nameOrig)별로 시계열 정렬 후 시퀀스 피처를 계산한다.
      앞 7일(step<=168)은 고객 Baseline 추정에만 쓰고 학습행으로는 내보내지 않아
      미래 정보 누수를 막는다.

  (2) data/findelegation_bench.csv  자체 위임 위험행동 데이터셋. 라벨 = risk_label

출력 : ml/models/lgbm_sequence.pkl
"""
import json
from pathlib import Path

import joblib
import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.metrics import (average_precision_score, classification_report,
                             roc_auc_score)
from sklearn.model_selection import GroupShuffleSplit

from features import SEQ_FEATURES, compute_sequence_features

ROOT = Path(__file__).resolve().parent.parent
MODEL_DIR = Path(__file__).resolve().parent / "models"
MODEL_DIR.mkdir(parents=True, exist_ok=True)

WARMUP_STEP = 168          # 앞 7일은 Baseline 추정 전용
N_FRAUD_CUST = 900         # 사기 관련 고객 표본
N_NORMAL_CUST = 1400       # 정상 고객 표본


def paysim_to_features(max_rows=None):
    df = pd.read_csv(ROOT / "data" / "paysim.csv")
    rng = np.random.default_rng(5)

    fraud_cust = df.loc[df.isFraud == 1, "nameOrig"].unique()
    fraud_cust = fraud_cust[:N_FRAUD_CUST]
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

        # 고객 Baseline: 앞 7일 거래로 추정 (없으면 전체 분포 대체값)
        if len(warm) >= 2:
            a = warm.amount.values.astype(float)
            mean_amt, std_amt = float(a.mean()), float(a.std(ddof=0)) or 1.0
            daily_tx = float(len(warm) / 7.0)
            daily_spend = float(a.sum() / 7.0)
            auto_limit = float(max(np.percentile(a, 90), 1000.0))
            cat_share = {k: float(v) for k, v in
                         warm.type.value_counts(normalize=True).items()}
            known = {r: {"n": 1} for r in warm.nameDest.unique()}
        else:
            mean_amt, std_amt, daily_tx = 150_000.0, 300_000.0, 1.0
            daily_spend = 150_000.0
            auto_limit = 500_000.0
            cat_share, known = {"PAYMENT": 1.0}, {}

        baseline = {
            "n_tx": max(len(warm), 1),
            "amount": {"mean": mean_amt, "std": std_amt},
            "daily": {"tx_count_mean": max(daily_tx, 0.3),
                      "amount_sum_mean": max(daily_spend, 1.0)},
            "category_share": cat_share,
            "known_recipients": known,
        }
        policy = {"auto_limit": auto_limit, "daily_limit": auto_limit * 3.0,
                  "allowed_tools": None}

        history = [
            {"ts": float(r.ts), "amount": float(r.amount), "recipient_id": r.nameDest,
             "category": r.type, "tx_type": r.type, "status": "SUCCESS",
             "tool": None, "is_new_recipient": 0,
             "balance_before": float(r.oldbalanceOrg)}
            for r in warm.itertuples()
        ]
        for r in live.itertuples():
            act = {"ts": float(r.ts), "amount": float(r.amount),
                   "recipient_id": r.nameDest, "category": r.type,
                   "tx_type": r.type, "status": "SUCCESS", "tool": None,
                   "balance_before": float(r.oldbalanceOrg),
                   "hour": int(r.step % 24), "dow": int((r.step // 24) % 7)}
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


def load_bench():
    df = pd.read_csv(ROOT / "data" / "findelegation_bench.csv")
    df["group"] = "FB_" + df.session_id.astype(str)
    df["source"] = "bench"
    return df[SEQ_FEATURES + ["risk_label", "scenario", "group", "source"]]


def main():
    print("[lgbm] PaySim -> 시퀀스 피처 변환 중 ...")
    ps = paysim_to_features()
    print("      paysim rows=%d  fraud=%d" % (len(ps), int(ps.risk_label.sum())))

    fb = load_bench()
    print("      bench  rows=%d  risky=%d" % (len(fb), int(fb.risk_label.sum())))

    data = pd.concat([fb, ps[SEQ_FEATURES + ["risk_label", "scenario", "group", "source"]]],
                     ignore_index=True)
    data = data.replace([np.inf, -np.inf], np.nan).fillna(0.0)

    X = data[SEQ_FEATURES].values
    y = data["risk_label"].values.astype(int)
    groups = data["group"].values

    gss = GroupShuffleSplit(n_splits=1, test_size=0.25, random_state=42)
    tr, te = next(gss.split(X, y, groups))
    print("[lgbm] train=%d  test=%d  (세션 단위 분할)" % (len(tr), len(te)))

    # PaySim 사기는 표본이 적으므로 가중치를 높여 학습에 반영한다
    w = np.where(data["source"].values == "paysim",
                 np.where(y == 1, 25.0, 1.0), 1.0)

    model = lgb.LGBMClassifier(
        objective="binary",
        n_estimators=600,
        learning_rate=0.05,
        num_leaves=48,
        max_depth=8,
        min_child_samples=40,
        subsample=0.85,
        subsample_freq=1,
        colsample_bytree=0.85,
        reg_lambda=1.0,
        random_state=42,
        n_jobs=-1,
        verbose=-1,
    )
    model.fit(X[tr], y[tr], sample_weight=w[tr],
              eval_set=[(X[te], y[te])], eval_metric="auc",
              callbacks=[lgb.early_stopping(60, verbose=False)])

    p = model.predict_proba(X[te])[:, 1]
    auc = roc_auc_score(y[te], p)
    ap = average_precision_score(y[te], p)
    print("\n[lgbm] ROC-AUC = %.4f   PR-AUC = %.4f" % (auc, ap))
    print(classification_report(y[te], (p >= 0.5).astype(int),
                                target_names=["NORMAL", "RISKY"], digits=3))

    # 시나리오별 재현율
    test_df = data.iloc[te].copy()
    test_df["p"] = p
    print("[lgbm] 시나리오별 탐지율 (임계값 0.5, 위험 라벨 행 기준)")
    for scn, g in test_df[test_df.risk_label == 1].groupby("scenario"):
        print("   %-18s n=%5d  recall=%.3f  mean_score=%5.1f" % (
            scn, len(g), (g.p >= 0.5).mean(), g.p.mean() * 100))
    fp = test_df[(test_df.risk_label == 0)]
    print("   %-18s n=%5d  오탐율=%.3f  mean_score=%5.1f" % (
        "(정상 행동)", len(fp), (fp.p >= 0.5).mean(), fp.p.mean() * 100))

    imp = pd.Series(model.feature_importances_, index=SEQ_FEATURES).sort_values(ascending=False)
    print("\n[lgbm] 피처 중요도 Top 12")
    print(imp.head(12).to_string())

    bundle = {
        "model": model,
        "features": SEQ_FEATURES,
        "metrics": {"roc_auc": float(auc), "pr_auc": float(ap),
                    "n_train": int(len(tr)), "n_test": int(len(te))},
        "importance": imp.to_dict(),
    }
    out = MODEL_DIR / "lgbm_sequence.pkl"
    joblib.dump(bundle, out)
    (MODEL_DIR / "lgbm_metrics.json").write_text(
        json.dumps(bundle["metrics"], indent=2), encoding="utf-8")
    print("\n[lgbm] saved -> %s" % out)


if __name__ == "__main__":
    main()
