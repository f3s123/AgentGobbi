# -*- coding: utf-8 -*-
"""
IsolationForest 학습 — 개인 금융행동 이탈도
------------------------------------------------
"이 사용자가 평소에 하는 행동인가"만 판정한다. 사기 여부는 보지 않는다.
학습 데이터는 사용자 본인 거래내역(data/user_transactions.csv)뿐이며,
전부 정상 행동이라는 전제로 One-Class 이상탐지를 수행한다.

점수 변환
  IsolationForest 의 score_samples 는 스케일이 임의라 그대로 쓸 수 없다.
  학습 데이터의 이상점수 분포를 저장해 두고, 추론 시 그 분포에서의 백분위로
  0~100 점을 매긴다. 학습 분포 최대치를 넘어서면 잔여 구간을 선형 외삽한다.
  -> "평소 행동 대비 상위 몇 %로 낯선 행동인가" 가 그대로 점수가 된다.

출력 : ml/models/iforest_personal.pkl
"""
import json
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler

from features import PERSONAL_FEATURES, compute_personal_features

ROOT = Path(__file__).resolve().parent.parent
MODEL_DIR = Path(__file__).resolve().parent / "models"
MODEL_DIR.mkdir(parents=True, exist_ok=True)


WARMUP = 40      # 확장 Baseline 이 안정될 때까지의 워밍업 구간


def load_actions():
    df = pd.read_csv(ROOT / "data" / "user_transactions.csv", encoding="utf-8-sig")
    df["ts_dt"] = pd.to_datetime(df["ts"])
    df = df.sort_values("ts_dt", kind="stable").reset_index(drop=True)
    # pandas 버전에 따라 datetime 해상도가 ns/us 로 달라지므로 명시적으로 초를 만든다
    df["ts_epoch"] = (df["ts_dt"] - pd.Timestamp("1970-01-01")).dt.total_seconds()
    return df


def build_matrix(df, baseline, expanding=True):
    """
    expanding=True 이면 수취인·카테고리 통계를 시점까지의 이력으로만 계산한다.
    - 미래 정보 누수를 막는다.
    - 전체 Baseline 을 쓰면 학습 데이터의 is_new_recipient 가 전부 0 이 되어
      모델이 '신규 수취인'이라는 개념 자체를 배우지 못한다.
    """
    history, rows = [], []
    run_recipients, run_categories = {}, {}
    for r in df.itertuples():
        act = {
            "ts": float(r.ts_epoch), "amount": float(r.amount),
            "recipient_id": r.recipient_id, "category": r.category,
            "tx_type": r.tx_type, "status": "SUCCESS",
            "hour": int(r.hour), "dow": int(r.dow),
        }
        if expanding:
            n = max(len(history), 1)
            bl = dict(baseline)
            bl["n_tx"] = n
            bl["known_recipients"] = {k: {"n": v} for k, v in run_recipients.items()}
            bl["category_share"] = {k: v / n for k, v in run_categories.items()}
        else:
            bl = baseline
        rows.append(compute_personal_features(act, history, bl))
        history.append(act)
        run_recipients[r.recipient_id] = run_recipients.get(r.recipient_id, 0) + 1
        run_categories[r.category] = run_categories.get(r.category, 0) + 1

    X = pd.DataFrame(rows)[PERSONAL_FEATURES]
    return X.iloc[WARMUP:].reset_index(drop=True) if expanding else X


def main():
    df = load_actions()
    baseline = json.loads((ROOT / "data" / "user_baseline.json").read_text(encoding="utf-8"))

    X = build_matrix(df, baseline)
    scaler = StandardScaler().fit(X.values)
    Xs = scaler.transform(X.values)

    model = IsolationForest(
        n_estimators=400,
        max_samples="auto",
        contamination=0.03,      # 본인 내역에도 소수의 비정상(고액 1회성)이 섞여 있다고 가정
        max_features=1.0,
        bootstrap=False,
        random_state=42,
        n_jobs=-1,
    ).fit(Xs)

    # 학습 분포 (이상점수: 클수록 낯선 행동)
    train_scores = -model.score_samples(Xs)
    ref = np.sort(train_scores)

    bundle = {
        "model": model,
        "scaler": scaler,
        "features": PERSONAL_FEATURES,
        "ref_scores": ref,
        "stats": {
            "n_train": int(len(ref)),
            "mean": float(ref.mean()),
            "p50": float(np.percentile(ref, 50)),
            "p90": float(np.percentile(ref, 90)),
            "p95": float(np.percentile(ref, 95)),
            "p99": float(np.percentile(ref, 99)),
            "max": float(ref.max()),
        },
    }
    out = MODEL_DIR / "iforest_personal.pkl"
    joblib.dump(bundle, out)

    print("[iforest] 학습 %d건 / 피처 %d개" % (len(X), len(PERSONAL_FEATURES)))
    print("[iforest] 이상점수 분포  p50=%.4f  p90=%.4f  p99=%.4f  max=%.4f" % (
        bundle["stats"]["p50"], bundle["stats"]["p90"],
        bundle["stats"]["p99"], bundle["stats"]["max"]))

    # --- 온전성 검사: 명백히 낯선 행동이 높은 점수를 받는지 확인 --------------
    from scoring_util import percentile_score  # noqa: E402  (같은 폴더)

    hist = []
    for r in df.itertuples():
        hist.append({"ts": float(r.ts_epoch), "amount": float(r.amount),
                     "recipient_id": r.recipient_id, "category": r.category,
                     "tx_type": r.tx_type, "status": "SUCCESS"})
    last_ts = hist[-1]["ts"] + 3600

    probes = [
        ("평소 거래 (카카오페이 1.2만원, 20시)",
         dict(amount=12000, recipient_id=df.recipient_id.mode()[0],
              category="SIMPLE_PAY", hour=20)),
        ("월세 60.5만원 (등록 수취인, 정기)",
         dict(amount=605000, recipient_id=df[df.category == "RENT"].recipient_id.iloc[0],
              category="RENT", hour=18)),
        ("신규 계좌 49만원 송금 (14시)",
         dict(amount=490000, recipient_id="R-UNKNOWN1", category="P2P", hour=14)),
        ("신규 계좌 49만원 송금 (새벽 3시)",
         dict(amount=490000, recipient_id="R-UNKNOWN2", category="P2P", hour=3)),
        ("해외송금 320만원 (새벽 3시, 신규)",
         dict(amount=3_200_000, recipient_id="R-UNKNOWN3",
              category="OVERSEAS_REMIT", hour=3)),
    ]
    print("\n[iforest] 온전성 검사")
    for label, kw in probes:
        act = {"ts": last_ts, "status": "SUCCESS", "tx_type": "TRANSFER", "dow": 2, **kw}
        f = compute_personal_features(act, hist, baseline)
        v = scaler.transform([[f[n] for n in PERSONAL_FEATURES]])
        s = float(-model.score_samples(v)[0])
        print("   %-38s 이탈도 %5.1f점" % (label, percentile_score(s, ref)))

    print("\n[iforest] saved -> %s" % out)


if __name__ == "__main__":
    main()
