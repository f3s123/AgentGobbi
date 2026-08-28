# -*- coding: utf-8 -*-
"""
사용자 금융행동 Baseline 구축
------------------------------------------------
입력 : data/raw/nh_transactions.xlsx  (농협 입출금거래내역 2026-07, 59건)
출력 : data/user_transactions.csv     (수취인 라벨링 + 12개월 증강, 약 700건)
       data/user_baseline.json        (IsolationForest / 개인화 피처용 통계)

수행 작업
  1) 수취인 라벨링 재작업
     원본의 '거래기록사항'은 가맹점명 · 이체메모 · 사람이름이 섞여 있어
     그대로는 수취인 식별자로 쓸 수 없다. 정규화 사전 + 키워드 규칙으로
     (recipient_id, recipient_name, category, recipient_type) 4개 라벨을 새로 부여한다.
  2) 취소거래 처리 (가승인 후 음수 출금 = 취소쌍) 제거
  3) 12개월 증강
     원본 1개월(59건)만으로는 IsolationForest 학습이 불가능하므로,
     수취인별 월 발생빈도 · 금액 로그정규 · 시간대 분포를 추정한 뒤
     2025-09 ~ 2026-08 구간을 재생성한다.
     고정 지출(월세 · 구독 · 통신비 · 청약)은 실제 주기대로 결정적으로 배치하고,
     실제 2026-07 데이터는 원본 그대로 보존한다.
"""
import hashlib
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import openpyxl
import pandas as pd

RNG = np.random.default_rng(1124)
ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "data" / "raw" / "nh_transactions.xlsx"
OUT_TX = ROOT / "data" / "user_transactions.csv"
OUT_BASE = ROOT / "data" / "user_baseline.json"

USER_ID = "U-NH-475126"

# --------------------------------------------------------------------------
# 1) 수취인 정규화 사전
#    key = 원본 거래기록사항(정확 일치),  value = (정규화명, 카테고리, 수취인유형)
#    recipient_type : MERCHANT(가맹점) / PERSON(개인) / BILL(고정비) / SELF(본인계좌)
# --------------------------------------------------------------------------
RECIPIENT_MAP = {
    "헤드폰": ("전자기기 구매", "SHOPPING", "MERCHANT"),
    "저녁": ("식비 정산", "FOOD", "PERSON"),
    "간식": ("간식 정산", "FOOD", "PERSON"),
    "청약": ("주택청약저축", "SAVINGS", "SELF"),
    "Amazon_WebService": ("AWS", "CLOUD", "MERCHANT"),
    "교통": ("교통비", "TRANSPORT", "BILL"),
    "다이소": ("다이소", "SHOPPING", "MERCHANT"),
    "(주)드림월드플러스": ("드림월드플러스", "ENTERTAIN", "MERCHANT"),
    "네이버페이": ("네이버페이", "SIMPLE_PAY", "MERCHANT"),
    "네이버페이충전": ("네이버페이 충전", "SIMPLE_PAY", "SELF"),
    "카카오페이": ("카카오페이", "SIMPLE_PAY", "MERCHANT"),
    "통신사 요금": ("통신요금", "UTILITY", "BILL"),
    "기차": ("철도 승차권", "TRANSPORT", "MERCHANT"),
    "천막집": ("천막집", "FOOD", "MERCHANT"),
    "NH체크후불": ("NH 후불교통", "TRANSPORT", "BILL"),
    "이재윤": ("이재윤", "P2P", "PERSON"),
    "신이1981": ("신이1981", "P2P", "PERSON"),
    "월세": ("월세", "RENT", "BILL"),
    "구글플레이": ("구글플레이", "SUBSCRIPTION", "MERCHANT"),
    "클로드": ("Claude 구독", "SUBSCRIPTION", "MERCHANT"),
    "（주）예스코０７": ("예스코 도시가스", "UTILITY", "BILL"),
    "닥터브이코인노래연습장": ("코인노래연습장", "ENTERTAIN", "MERCHANT"),
    "유튜브프리미엄": ("유튜브 프리미엄", "SUBSCRIPTION", "MERCHANT"),
    "주식회사밤새출력": ("밤새출력", "SHOPPING", "MERCHANT"),
    "NHNKCP_3": ("NHN KCP 결제", "SHOPPING", "MERCHANT"),
    "워시팡팡무인셀프빨래방고대용두점": ("워시팡팡 빨래방", "LIVING", "MERCHANT"),
}

# 키워드 규칙 (사전에 없을 때 적용, 위에서부터 우선)
KEYWORD_RULES = [
    (r"세븐일레븐|CU|GS25|이마트24|할인마켓", "CONVENIENCE", "MERCHANT"),
    (r"코레일|한국철도|철도공사", "TRANSPORT", "MERCHANT"),
    (r"카카오T|택시", "TRANSPORT", "MERCHANT"),
    (r"카카오페이|네이버페이|토스|페이코", "SIMPLE_PAY", "MERCHANT"),
    (r"구글|애플|유튜브|넷플릭스|스포티파이", "SUBSCRIPTION", "MERCHANT"),
]

# 편의점/역사 지점명 정규화 (지점이 달라도 동일 수취인으로 묶음)
BRANCH_NORMALIZE = [
    (r"^세븐일레븐.*", "세븐일레븐"),
    (r"^CU.*", "CU"),
    (r"^이마트24.*", "이마트24"),
    (r"^할인마켓.*", "할인마켓 SW365"),
    (r"^코레일유통주식회사.*", "코레일유통"),
    (r"^한국철도공사.*", "한국철도공사"),
    (r"^카카오T.*", "카카오T"),
    (r"^샤브온당고.*", "샤브온당고"),
]


def normalize_name(raw):
    s = str(raw).strip()
    for pat, rep in BRANCH_NORMALIZE:
        if re.match(pat, s):
            return rep
    return s


def label_recipient(raw):
    """거래기록사항 -> (recipient_id, recipient_name, category, recipient_type)"""
    s = str(raw).strip()
    if s in RECIPIENT_MAP:
        name, cat, rtype = RECIPIENT_MAP[s]
    else:
        name = normalize_name(s)
        cat, rtype = "ETC", "MERCHANT"
        for pat, c, t in KEYWORD_RULES:
            if re.search(pat, s):
                cat, rtype = c, t
                break
    rid = "R-" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:8].upper()
    return rid, name, cat, rtype


# --------------------------------------------------------------------------
# 2) 원본 로드
# --------------------------------------------------------------------------
def load_raw():
    wb = openpyxl.load_workbook(SRC)
    ws = wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    header_idx = next(i for i, r in enumerate(rows) if r[1] == "순번")
    recs = []
    for r in rows[header_idx + 1:]:
        if r[1] is None:
            continue
        ts = datetime.strptime(str(r[2]).strip(), "%Y/%m/%d  %H:%M:%S")
        out_amt = r[3] or 0
        in_amt = r[4] or 0
        recs.append({
            "seq": int(r[1]),
            "ts": ts,
            "amount": float(out_amt) if out_amt else -float(in_amt),
            "memo": str(r[5]).strip(),
            "branch": str(r[6]).strip() if r[6] else "",
        })
    df = pd.DataFrame(recs)

    # 가승인 취소쌍 제거: 동일 메모 + 금액 부호만 반대 + 5초 이내
    drop = set()
    for i, row in df.iterrows():
        if row["amount"] >= 0:
            continue
        m = df[(df.memo == row.memo) & (df.amount == -row.amount) &
               (abs((df.ts - row.ts).dt.total_seconds()) <= 5)]
        if len(m):
            drop.add(i)
            drop.add(m.index[0])
    df = df.drop(index=list(drop)).reset_index(drop=True)
    df = df[df.amount > 0].reset_index(drop=True)
    return df


# --------------------------------------------------------------------------
# 3) 수취인별 통계 추정
# --------------------------------------------------------------------------
# 고정 주기 지출: (원본메모, 월중 기준일, 금액평균, 금액변동계수, 시각)
FIXED_SCHEDULE = [
    ("월세", 20, 605000, 0.00, (18, 30)),
    ("청약", 1, 50000, 0.00, (18, 20)),
    ("통신사 요금", 9, 12500, 0.02, (7, 25)),
    ("유튜브프리미엄", 30, 14900, 0.00, (17, 50)),
    ("Amazon_WebService", 2, 16900, 0.35, (8, 45)),
    ("클로드", 22, 320000, 0.28, (11, 30)),
    ("NH체크후불", 14, 33000, 0.22, (8, 55)),
    ("（주）예스코０７", 22, 6500, 0.60, (13, 30)),
]
FIXED_MEMOS = {m for m, *_ in FIXED_SCHEDULE}

# 시간대 분포 (원본 59건에서 관측된 형태를 카테고리별로 반영)
CATEGORY_HOURS = {
    "FOOD": [11, 12, 18, 19, 20, 21, 22],
    "CONVENIENCE": [8, 9, 12, 18, 19, 20, 21, 22, 23],
    "TRANSPORT": [7, 8, 9, 10, 12, 17, 18, 21, 22],
    "SIMPLE_PAY": [10, 11, 13, 15, 17, 19, 20, 21, 22],
    "SHOPPING": [11, 14, 16, 19, 20, 21],
    "ENTERTAIN": [19, 20, 21, 22, 23],
    "P2P": [10, 12, 16, 18, 20],
    "LIVING": [10, 14, 16, 19],
    "SUBSCRIPTION": [8, 11, 17, 21],
    "CLOUD": [8, 9],
    "ETC": [10, 12, 15, 18, 20],
}


def estimate_recipients(df):
    """수취인별 월 빈도 / 금액 로그정규 파라미터 추정."""
    stats = {}
    for memo, g in df.groupby("memo"):
        rid, name, cat, rtype = label_recipient(memo)
        key = rid
        amts = g["amount"].values.astype(float)
        entry = stats.setdefault(key, {
            "recipient_id": rid, "recipient_name": name, "category": cat,
            "recipient_type": rtype, "memos": [], "amounts": [],
        })
        entry["memos"].append(memo)
        entry["amounts"].extend(amts.tolist())

    for key, e in stats.items():
        a = np.array(e["amounts"], dtype=float)
        e["monthly_freq"] = float(len(a))              # 원본이 정확히 1개월
        e["log_mu"] = float(np.log(a).mean())
        # 표본이 적으므로 변동계수 하한을 둔다
        e["log_sigma"] = float(max(np.log(a).std(ddof=0), 0.22))
        e["amounts"] = a.tolist()
    return stats


def sample_hour(cat):
    hours = CATEGORY_HOURS.get(cat, CATEGORY_HOURS["ETC"])
    h = int(RNG.choice(hours))
    return h, int(RNG.integers(0, 60)), int(RNG.integers(0, 60))


def generate_month(year, month, stats, seasonal=1.0):
    """한 달치 거래 생성."""
    out = []
    days_in_month = (datetime(year + (month == 12), (month % 12) + 1, 1)
                     - datetime(year, month, 1)).days

    # 고정 주기 지출
    for memo, day, mean_amt, cv, (hh, mm) in FIXED_SCHEDULE:
        d = min(day + int(RNG.integers(-1, 2)), days_in_month)
        d = max(d, 1)
        amt = mean_amt if cv == 0 else float(np.round(mean_amt * np.exp(RNG.normal(0, cv)), -1))
        rid, name, cat, rtype = label_recipient(memo)
        out.append({
            "ts": datetime(year, month, d, hh, mm, int(RNG.integers(0, 60))),
            "amount": round(float(amt)),
            "memo": memo, "recipient_id": rid, "recipient_name": name,
            "category": cat, "recipient_type": rtype, "is_recurring": 1,
        })

    # 변동 지출
    for key, e in stats.items():
        if any(m in FIXED_MEMOS for m in e["memos"]):
            continue
        lam = e["monthly_freq"] * seasonal
        n = int(RNG.poisson(lam))
        for _ in range(n):
            d = int(RNG.integers(1, days_in_month + 1))
            hh, mm, ss = sample_hour(e["category"])
            amt = float(np.exp(RNG.normal(e["log_mu"], e["log_sigma"])))
            amt = round(amt, -1) if amt > 1000 else round(amt, -1)
            out.append({
                "ts": datetime(year, month, d, hh, mm, ss),
                "amount": max(int(amt), 500),
                "memo": e["memos"][0], "recipient_id": e["recipient_id"],
                "recipient_name": e["recipient_name"], "category": e["category"],
                "recipient_type": e["recipient_type"], "is_recurring": 0,
            })
    return out


def build():
    raw = load_raw()
    stats = estimate_recipients(raw)

    # --- 실제 2026-07 데이터는 원본 그대로 보존 ---
    real = []
    for _, r in raw.iterrows():
        rid, name, cat, rtype = label_recipient(r["memo"])
        real.append({
            "ts": r["ts"], "amount": int(r["amount"]), "memo": r["memo"],
            "recipient_id": rid, "recipient_name": name, "category": cat,
            "recipient_type": rtype,
            "is_recurring": 1 if r["memo"] in FIXED_MEMOS else 0,
        })

    # --- 2025-09 ~ 2026-08 증강 (2026-07 은 실제 데이터로 대체) ---
    rows = list(real)
    months = [(2025, m) for m in range(9, 13)] + [(2026, m) for m in range(1, 9)]
    for (y, m) in months:
        if (y, m) == (2026, 7):
            continue
        # 방학/학기 계절성 (1~2월, 7~8월 지출 빈도 소폭 감소)
        seasonal = 0.85 if m in (1, 2, 8) else 1.0
        rows.extend(generate_month(y, m, stats, seasonal))

    df = pd.DataFrame(rows)
    # 오늘(2026-08-27) 이후 거래 제거
    df = df[df.ts < datetime(2026, 8, 27)].reset_index(drop=True)
    df = df.sort_values("ts", kind="stable").reset_index(drop=True)

    df["user_id"] = USER_ID
    df["tx_type"] = np.where(df.recipient_type.isin(["PERSON", "SELF"]), "TRANSFER", "PAYMENT")
    df["hour"] = df.ts.dt.hour
    df["dow"] = df.ts.dt.dayofweek
    df["date"] = df.ts.dt.date

    # 수취인 최초 등장 여부 -> 신규 수취인 라벨
    first_seen = {}
    is_new = []
    for _, r in df.iterrows():
        rid = r["recipient_id"]
        is_new.append(0 if rid in first_seen else 1)
        first_seen.setdefault(rid, r["ts"])
    df["is_new_recipient"] = is_new

    df.to_csv(OUT_TX, index=False, encoding="utf-8-sig")
    return df


def build_baseline(df):
    """개인화 피처 · IsolationForest 정규화에 쓰는 Baseline 통계."""
    a = df["amount"].values.astype(float)
    daily = df.groupby("date")["amount"].agg(["count", "sum"])
    gaps = df["ts"].diff().dt.total_seconds().dropna()
    gaps = gaps[gaps > 0]

    known = (df.groupby("recipient_id")
               .agg(recipient_name=("recipient_name", "first"),
                    category=("category", "first"),
                    recipient_type=("recipient_type", "first"),
                    n=("amount", "size"),
                    mean_amount=("amount", "mean"),
                    max_amount=("amount", "max"))
               .sort_values("n", ascending=False))

    baseline = {
        "user_id": USER_ID,
        "period": {"from": str(df.ts.min()), "to": str(df.ts.max())},
        "n_tx": int(len(df)),
        "amount": {
            "mean": float(a.mean()), "std": float(a.std(ddof=0)),
            "median": float(np.median(a)),
            "log_mu": float(np.log(a).mean()), "log_sigma": float(np.log(a).std(ddof=0)),
            "p90": float(np.percentile(a, 90)), "p95": float(np.percentile(a, 95)),
            "p99": float(np.percentile(a, 99)), "max": float(a.max()),
        },
        "daily": {
            "tx_count_mean": float(daily["count"].mean()),
            "tx_count_std": float(daily["count"].std(ddof=0)),
            "tx_count_p95": float(np.percentile(daily["count"], 95)),
            "amount_sum_mean": float(daily["sum"].mean()),
            "amount_sum_std": float(daily["sum"].std(ddof=0)),
            "amount_sum_p95": float(np.percentile(daily["sum"], 95)),
            "active_days": int(len(daily)),
        },
        "interval_sec": {
            "mean": float(gaps.mean()), "median": float(gaps.median()),
            "p05": float(np.percentile(gaps, 5)), "p25": float(np.percentile(gaps, 25)),
        },
        "hour_hist": {str(h): int(v) for h, v in
                      df.hour.value_counts().sort_index().items()},
        "category_share": {k: float(v) for k, v in
                           df.category.value_counts(normalize=True).items()},
        "new_recipient_rate": float(df.is_new_recipient.mean()),
        "n_known_recipients": int(df.recipient_id.nunique()),
        "known_recipients": {
            rid: {
                "name": r.recipient_name, "category": r.category,
                "recipient_type": r.recipient_type, "n": int(r.n),
                "mean_amount": float(r.mean_amount), "max_amount": float(r.max_amount),
            } for rid, r in known.iterrows()
        },
    }
    OUT_BASE.write_text(json.dumps(baseline, indent=2, ensure_ascii=False), encoding="utf-8")
    return baseline


if __name__ == "__main__":
    df = build()
    b = build_baseline(df)
    print("[user] 증강 거래 %d건  (%s ~ %s)" % (
        len(df), df.ts.min().date(), df.ts.max().date()))
    print("[user] 수취인 %d명 / 카테고리 %d종" % (
        df.recipient_id.nunique(), df.category.nunique()))
    print("[user] 평균 거래금액 %,.0f원 / 일평균 %.2f건 / 일평균 지출 %,.0f원"
          .replace(",", "") % (b["amount"]["mean"], b["daily"]["tx_count_mean"],
                               b["daily"]["amount_sum_mean"]))
    print(df.category.value_counts().to_string())
    print("[user] saved -> %s" % OUT_TX)
    print("[user] baseline -> %s" % OUT_BASE)
