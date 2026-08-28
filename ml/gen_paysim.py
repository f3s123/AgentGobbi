# -*- coding: utf-8 -*-
"""
PaySim 스타일 모바일머니 거래 시뮬레이터
------------------------------------------------
PaySim(Lopez-Rojas et al., 2016)은 원본 자체가 에이전트 기반 시뮬레이터이며,
공개 CSV(PS_20174392719_1491204439457_log.csv)는 그 실행 결과물이다.
본 모듈은 동일한 스키마 / 거래유형 비율 / 사기 시나리오(계좌 탈취 후 전액 이체 -> 인출)를
재현하는 시뮬레이터를 구현하여 data/paysim.csv 를 생성한다.

원본과 맞춘 항목
  - 컬럼: step, type, amount, nameOrig, oldbalanceOrg, newbalanceOrig,
          nameDest, oldbalanceDest, newbalanceDest, isFraud, isFlaggedFraud
  - step = 1시간 단위 (30일 = 720 step)
  - 유형 비율: CASH_OUT 35.2 / PAYMENT 33.8 / CASH_IN 21.9 / TRANSFER 8.4 / DEBIT 0.7 (%)
  - 사기는 TRANSFER, CASH_OUT 에서만 발생하며 잔액을 전액 빼내는 패턴
  - isFlaggedFraud 는 단건 200만 초과 TRANSFER 에 대한 정적 룰(원본 규칙 재현)

원본과 의도적으로 다르게 한 항목
  - 원본은 nameOrig 가 거의 1회성이라 시퀀스 분석이 불가능하다.
    본 시뮬레이터는 고객 에이전트를 재사용하여 한 고객이 여러 건을 발생시키고,
    step 내부에 분 단위 타임스탬프를 부여해 행동 시퀀스 피처를 뽑을 수 있게 한다.
"""
import json
from pathlib import Path

import numpy as np
import pandas as pd

RNG = np.random.default_rng(20260827)

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "paysim.csv"
PROFILE_OUT = ROOT / "data" / "paysim_profile.json"

N_STEPS = 720            # 30일 * 24시간
N_CUSTOMERS = 6000
N_MERCHANTS = 1200
FRAUD_RATE = 0.0013      # 원본 약 0.129%

TYPE_P = {
    "CASH_OUT": 0.352,
    "PAYMENT": 0.338,
    "CASH_IN": 0.219,
    "TRANSFER": 0.084,
    "DEBIT": 0.007,
}

# 원본 PaySim 유형별 금액 분포 근사 (로그정규)
AMOUNT_LOGN = {
    "PAYMENT": (9.05, 1.05),
    "CASH_OUT": (11.35, 1.30),
    "CASH_IN": (11.20, 1.25),
    "TRANSFER": (12.10, 1.85),
    "DEBIT": (8.60, 1.10),
}

# 시간대별 거래 강도 (0~23시). 새벽 한산, 낮 피크
HOUR_WEIGHT = np.array([
    0.20, 0.12, 0.08, 0.07, 0.08, 0.15, 0.35, 0.70,
    1.00, 1.25, 1.35, 1.30, 1.20, 1.30, 1.35, 1.30,
    1.20, 1.15, 1.05, 0.95, 0.85, 0.70, 0.50, 0.32,
])
HOUR_WEIGHT = HOUR_WEIGHT / HOUR_WEIGHT.sum()


def _amount(tx_type):
    mu, sigma = AMOUNT_LOGN[tx_type]
    return float(np.round(RNG.lognormal(mu, sigma), 2))


def simulate():
    cust_ids = np.array(["C%d" % (1000000000 + int(i)) for i in
                         RNG.choice(900000000, size=N_CUSTOMERS, replace=False)])
    merch_ids = np.array(["M%d" % (1000000000 + int(i)) for i in
                          RNG.choice(900000000, size=N_MERCHANTS, replace=False)])

    balances = np.round(RNG.lognormal(9.6, 1.35, N_CUSTOMERS), 2)
    dest_balances = {}

    # 소수의 헤비유저가 다수 거래를 만든다 (현실 반영)
    activity = RNG.pareto(1.6, N_CUSTOMERS) + 1.0
    activity = activity / activity.sum()

    step_hours = np.arange(N_STEPS) % 24
    step_lambda = 380 * HOUR_WEIGHT[step_hours] * 24

    rows = []
    fraud_plan = {}

    n_tx_est = int(step_lambda.sum())
    n_fraud_sessions = max(1, int(n_tx_est * FRAUD_RATE / 2))
    for _ in range(n_fraud_sessions):
        s = int(RNG.integers(1, N_STEPS))
        victim = int(RNG.integers(0, N_CUSTOMERS))
        mule = int(RNG.integers(0, N_CUSTOMERS))
        fraud_plan.setdefault(s, []).append((victim, mule))

    types = list(TYPE_P.keys())
    type_p = np.array([TYPE_P[t] for t in types])

    for step in range(1, N_STEPS + 1):
        n_tx = int(RNG.poisson(step_lambda[step - 1]))
        if n_tx > 0:
            actors = RNG.choice(N_CUSTOMERS, size=n_tx, p=activity)
            kinds = RNG.choice(types, size=n_tx, p=type_p)
            minutes = np.sort(RNG.integers(0, 60, size=n_tx))

            for actor, tx_type, minute in zip(actors, kinds, minutes):
                orig = cust_ids[actor]
                # 잔액이 마르면 급여 등 유입으로 채워진다. 이렇게 해야 정상 거래가
                # '잔액 전액 소진'으로 기록되지 않고, 전액 소진이 사기의 시그니처로 남는다.
                if balances[actor] < 1000:
                    balances[actor] = float(np.round(RNG.lognormal(9.6, 1.35), 2))
                old_o = float(balances[actor])
                amt = _amount(tx_type)

                if tx_type in ("PAYMENT", "DEBIT"):
                    dest = merch_ids[RNG.integers(0, N_MERCHANTS)]
                    if amt > old_o:
                        amt = float(np.round(old_o * RNG.uniform(0.05, 0.75), 2))
                    new_o = round(max(old_o - amt, 0.0), 2)
                    old_d, new_d = 0.0, 0.0      # 원본에서 가맹점 잔액은 0으로 기록
                elif tx_type == "CASH_IN":
                    dest = merch_ids[RNG.integers(0, N_MERCHANTS)]
                    new_o = round(old_o + amt, 2)
                    old_d, new_d = 0.0, 0.0
                else:  # TRANSFER / CASH_OUT
                    didx = int(RNG.integers(0, N_CUSTOMERS))
                    dest = cust_ids[didx]
                    if amt > old_o:
                        amt = float(np.round(old_o * RNG.uniform(0.05, 0.75), 2))
                    new_o = round(max(old_o - amt, 0.0), 2)
                    old_d = float(dest_balances.get(dest, balances[didx]))
                    new_d = round(old_d + amt, 2)
                    dest_balances[dest] = new_d

                balances[actor] = new_o
                flagged = 1 if (tx_type == "TRANSFER" and amt > 2_000_000) else 0
                rows.append((step, int(minute), tx_type, amt, orig, old_o, new_o,
                             dest, old_d, new_d, 0, flagged))

        # 해당 step 의 사기 세션 실행
        for victim, mule in fraud_plan.get(step, []):
            orig = cust_ids[victim]
            bal = float(balances[victim])
            if bal < 1000:
                bal = float(np.round(RNG.lognormal(10.5, 0.8), 2))
            mule_id = cust_ids[mule]
            m = int(RNG.integers(0, 55))

            # 1) 탈취 계좌 -> 뮬 계좌 전액 이체
            old_d = float(dest_balances.get(mule_id, 0.0))
            rows.append((step, m, "TRANSFER", bal, orig, bal, 0.0,
                         mule_id, old_d, 0.0, 1,
                         1 if bal > 2_000_000 else 0))
            balances[victim] = 0.0

            # 2) 뮬 계좌에서 즉시 전액 인출
            rows.append((step, min(m + int(RNG.integers(1, 4)), 59), "CASH_OUT", bal,
                         mule_id, bal, 0.0,
                         "C%d" % RNG.integers(1000000000, 1999999999), 0.0, 0.0, 1, 0))

    df = pd.DataFrame(rows, columns=[
        "step", "minute", "type", "amount", "nameOrig", "oldbalanceOrg",
        "newbalanceOrig", "nameDest", "oldbalanceDest", "newbalanceDest",
        "isFraud", "isFlaggedFraud"])
    df = df.sort_values(["step", "minute"], kind="stable").reset_index(drop=True)
    df["ts"] = (df["step"] - 1) * 3600 + df["minute"] * 60
    return df


def build_profile(df):
    """FinDelegationBench 생성 시 참조할 금액/행동 분포 통계."""
    prof = {"n_rows": int(len(df)), "fraud_rate": float(df.isFraud.mean()), "by_type": {}}
    for t, g in df.groupby("type"):
        a = g["amount"].values
        pos = np.log(a[a > 0])
        prof["by_type"][t] = {
            "share": float(len(g) / len(df)),
            "log_mu": float(pos.mean()),
            "log_sigma": float(pos.std()),
            "p50": float(np.percentile(a, 50)),
            "p90": float(np.percentile(a, 90)),
            "p99": float(np.percentile(a, 99)),
        }
    f = df[df.isFraud == 1]
    prof["fraud"] = {
        "n": int(len(f)),
        "mean_amount": float(f.amount.mean()) if len(f) else 0.0,
        "drain_ratio": float((f.newbalanceOrig == 0).mean()) if len(f) else 0.0,
        "type_share": {k: float(v) for k, v in f.type.value_counts(normalize=True).items()},
    }
    return prof


if __name__ == "__main__":
    df = simulate()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUT, index=False)
    prof = build_profile(df)
    PROFILE_OUT.write_text(json.dumps(prof, indent=2, ensure_ascii=False), encoding="utf-8")

    print("[paysim] rows=%d  fraud=%d (%.3f%%)  flagged=%d" % (
        len(df), int(df.isFraud.sum()), df.isFraud.mean() * 100,
        int(df.isFlaggedFraud.sum())))
    print(df.type.value_counts(normalize=True).round(4).to_string())
    print("[paysim] saved -> %s" % OUT)
    print("[paysim] profile -> %s" % PROFILE_OUT)
