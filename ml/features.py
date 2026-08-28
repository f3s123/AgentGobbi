# -*- coding: utf-8 -*-
"""
피처 엔지니어링 (학습 · 추론 공용)
------------------------------------------------
학습 스크립트와 API 가 반드시 동일한 피처를 쓰도록 이 모듈 하나로 통일한다.

두 개의 피처 공간
  SEQ_FEATURES      : LightGBM 용. "이 행동 시퀀스가 위험한가"
                      PaySim + FinDelegationBench 두 데이터셋이 공유한다.
  PERSONAL_FEATURES : IsolationForest 용. "이 사용자가 평소 하는 행동인가"
                      사용자 본인 거래내역만으로 학습한다.
"""
import math

import numpy as np

# --------------------------------------------------------------------------
# 주의: SEQ_FEATURES 는 전부 '스케일 free' 여야 한다.
# 학습은 PaySim 스케일의 가상 사용자로 하고 추론은 원화 실사용자로 하기 때문에,
# 절대 금액(log1p(amount) 등)을 쓰면 학습된 분기 임계값이 전이되지 않는다.
# 금액은 반드시 개인 Baseline · 위임한도 · 잔액에 대한 '비율'로만 넣는다.
SEQ_FEATURES = [
    # 거래 특성
    "amount_to_mean",         # 현재 거래금액 / 개인 평균 거래금액
    "amt_to_auto_limit",      # 현재 거래금액 / 자동송금 한도
    "is_new_recipient",       # 신규 수취인 여부
    "is_transfer",            # 송금(=회수 불가) 여부
    "is_night",               # 심야(00~05시) 여부
    "amt_to_balance",         # 현재 거래금액 / 거래 전 잔액
    "is_balance_drain",       # 잔액을 사실상 전부 소진시키는 거래인지
    # 시퀀스 특성
    "tx_cnt_1h",              # 최근 1시간 거래 횟수
    "tx_cnt_24h",             # 최근 24시간 거래 횟수
    "new_recipient_cnt_1h",   # 최근 1시간 신규 수취인 수
    "distinct_recipient_24h", # 최근 24시간 서로 다른 수취인 수
    "cum1_to_daily_limit",    # 최근 1시간 누적 / 1일 누적한도
    "cum24_to_baseline",      # 최근 24시간 누적 / 개인 하루 평균 지출
    "cum_to_daily_limit",     # 24시간 누적 / 1일 누적한도
    "sec_since_prev_log",     # 직전 거래와의 시간 간격
    "mean_interval_5_log",    # 최근 5건 평균 거래 간격
    "near_limit_repeat",      # 자동송금 한도 90~100% 구간 거래 반복 횟수
    "fail_cnt_1h",            # 최근 1시간 실패 횟수
    "retry_cnt_1h",           # 최근 1시간 동일 수취인 재시도 횟수
    # 개인화 특성
    "amount_z_vs_baseline",   # 개인 평균 거래금액 대비 편차(z)
    "tx_rate_vs_baseline",    # 개인 일평균 거래횟수 대비 증가율
    "unknown_category",       # 평소 쓰지 않던 카테고리 여부
    "unauthorized_tool",      # 위임되지 않은 금융 Tool 호출 여부
]

PERSONAL_FEATURES = [
    "amount_log",
    "hour_sin",
    "hour_cos",
    "is_weekend",
    "is_new_recipient",
    "recipient_freq",         # 해당 수취인의 과거 이용 비율
    "category_freq",          # 해당 카테고리의 과거 이용 비율
    "sec_since_prev_log",
    "tx_cnt_24h",
    "cum_amt_24h_log",
    "amount_z_vs_baseline",
]

LOG = lambda x: math.log1p(max(float(x), 0.0))


def _safe_div(a, b, default=0.0):
    b = float(b)
    return float(a) / b if abs(b) > 1e-9 else default


# --------------------------------------------------------------------------
def sequence_window(history, now_ts, window_sec):
    """now_ts 기준 window_sec 이내의 과거 행동만 추린다."""
    return [h for h in history if 0 <= (now_ts - h["ts"]) <= window_sec]


def compute_sequence_features(action, history, policy, baseline):
    """
    action   : dict(ts, amount, recipient_id, category, tx_type, status, tool)
    history  : action 이전까지의 행동 리스트 (같은 형식, ts 오름차순)
    policy   : dict(auto_limit, daily_limit, allowed_tools, ...)
    baseline : dict — build_user_profile 이 만든 사용자 Baseline (또는 PaySim 고객 통계)
    """
    ts = float(action["ts"])
    amt = float(action["amount"])
    auto_limit = float(policy.get("auto_limit") or 500_000)
    daily_limit = float(policy.get("daily_limit") or 1_500_000)

    h1 = sequence_window(history, ts, 3600)
    h24 = sequence_window(history, ts, 86400)

    known = set(baseline.get("known_recipients", {}).keys())
    seen = known | {h["recipient_id"] for h in history}
    is_new = 0 if action.get("recipient_id") in seen else 1

    new_in_1h = sum(1 for i, h in enumerate(h1) if h.get("is_new_recipient")) + is_new

    prev_ts = history[-1]["ts"] if history else ts - 86400
    gaps = []
    recent = history[-5:]
    for a, b in zip(recent, recent[1:] + [action]):
        g = b["ts"] - a["ts"]
        if g >= 0:
            gaps.append(g)
    mean_gap = float(np.mean(gaps)) if gaps else 86400.0

    # 한도 근접 반복: 자동송금 한도의 90~100% 구간 거래
    near = sum(1 for h in h1 if 0.90 * auto_limit <= h["amount"] <= auto_limit)
    if 0.90 * auto_limit <= amt <= auto_limit:
        near += 1

    fails = sum(1 for h in h1 if h.get("status") == "FAILED")
    retries = sum(1 for h in h1
                  if h.get("status") == "FAILED"
                  and h.get("recipient_id") == action.get("recipient_id"))

    b_amt = baseline.get("amount", {})
    mu, sd = float(b_amt.get("mean", amt)), float(b_amt.get("std", 1.0)) or 1.0
    b_daily = baseline.get("daily", {})
    daily_mean = float(b_daily.get("tx_count_mean", 2.0)) or 2.0
    daily_spend = float(b_daily.get("amount_sum_mean", mu * daily_mean)) or (mu * daily_mean)

    cat_share = baseline.get("category_share", {})
    unknown_cat = 0 if cat_share.get(action.get("category", "ETC"), 0.0) > 0.005 else 1

    allowed = policy.get("allowed_tools")
    tool = action.get("tool")
    unauth = 1 if (allowed and tool and tool not in allowed) else 0

    cum1 = sum(h["amount"] for h in h1) + amt
    cum24 = sum(h["amount"] for h in h24) + amt

    # 잔액 대비 소진율. PaySim 사기 시나리오(계좌 탈취 후 전액 이체)의 핵심 신호이며,
    # AI Agent 가 잔액을 통째로 빼내는 행위를 잡는 데도 그대로 쓰인다.
    bal = action.get("balance_before")
    if bal is None or float(bal) <= 0:
        amt_to_balance, drain = 0.0, 0.0
    else:
        bal = float(bal)
        amt_to_balance = min(amt / bal, 5.0)
        drain = 1.0 if (bal - amt) <= max(bal * 0.005, 1.0) else 0.0

    tx_type = action.get("tx_type") or action.get("action_type")

    return {
        "amount_to_mean": min(_safe_div(amt, mu), 300.0),
        "amt_to_auto_limit": min(_safe_div(amt, auto_limit), 50.0),
        "is_new_recipient": float(is_new),
        "is_transfer": 1.0 if tx_type in ("TRANSFER", "CASH_OUT") else 0.0,
        "is_night": 1.0 if action.get("hour", 12) in (0, 1, 2, 3, 4, 5) else 0.0,
        "amt_to_balance": amt_to_balance,
        "is_balance_drain": drain,
        "tx_cnt_1h": float(len(h1) + 1),
        "tx_cnt_24h": float(len(h24) + 1),
        "new_recipient_cnt_1h": float(new_in_1h),
        "distinct_recipient_24h": float(len({h["recipient_id"] for h in h24} |
                                            {action.get("recipient_id")})),
        "cum1_to_daily_limit": min(_safe_div(cum1, daily_limit), 50.0),
        "cum24_to_baseline": min(_safe_div(cum24, daily_spend), 300.0),
        "cum_to_daily_limit": min(_safe_div(cum24, daily_limit), 50.0),
        "sec_since_prev_log": LOG(max(ts - prev_ts, 0)),
        "mean_interval_5_log": LOG(mean_gap),
        "near_limit_repeat": float(near),
        "fail_cnt_1h": float(fails),
        "retry_cnt_1h": float(retries),
        "amount_z_vs_baseline": float(min(max((amt - mu) / sd, -10.0), 60.0)),
        "tx_rate_vs_baseline": min(_safe_div(len(h24) + 1, daily_mean), 100.0),
        "unknown_category": float(unknown_cat),
        "unauthorized_tool": float(unauth),
    }


def compute_personal_features(action, history, baseline):
    """사용자 개인 행동 이탈도용 피처."""
    ts = float(action["ts"])
    amt = float(action["amount"])
    hour = int(action.get("hour", 12))
    dow = int(action.get("dow", 0))

    h24 = sequence_window(history, ts, 86400)
    prev_ts = history[-1]["ts"] if history else ts - 86400

    known = baseline.get("known_recipients", {})
    n_total = max(int(baseline.get("n_tx", 1)), 1)
    rid = action.get("recipient_id")
    rec_n = known.get(rid, {}).get("n", 0) if rid in known else 0
    rec_freq = rec_n / n_total

    cat_freq = float(baseline.get("category_share", {}).get(action.get("category", "ETC"), 0.0))

    b_amt = baseline.get("amount", {})
    mu, sd = float(b_amt.get("mean", amt)), float(b_amt.get("std", 1.0)) or 1.0

    return {
        "amount_log": LOG(amt),
        "hour_sin": math.sin(2 * math.pi * hour / 24),
        "hour_cos": math.cos(2 * math.pi * hour / 24),
        "is_weekend": 1.0 if dow >= 5 else 0.0,
        "is_new_recipient": 0.0 if rec_n > 0 else 1.0,
        "recipient_freq": float(rec_freq),
        "category_freq": cat_freq,
        "sec_since_prev_log": LOG(max(ts - prev_ts, 0)),
        "tx_cnt_24h": float(len(h24) + 1),
        "cum_amt_24h_log": LOG(sum(h["amount"] for h in h24) + amt),
        "amount_z_vs_baseline": float((amt - mu) / sd),
    }


def to_vector(feat_dict, names):
    return np.array([float(feat_dict[n]) for n in names], dtype=np.float64)
