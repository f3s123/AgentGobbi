# -*- coding: utf-8 -*-
"""
Risk Engine — 두 모델을 묶어 Delegation Risk Score 를 산출한다.

  LightGBM       시퀀스 위험도   "이 행동 흐름이 위험한가"      (0~100, 높을수록 위험)
  IsolationForest 개인 이탈도    "평소 이 사용자가 하는 행동인가" (0~100, 높을수록 낯섦)
  Policy Engine   정책 위험도    "위임 범위를 벗어났는가"        (0~100)

세 점수를 합산해 총 위험도를 만들고, Policy Engine 이 권한 등급을 결정한다.
동시에 사용자 설명 화면에 쓸 8개 근거(factors)를 함께 만들어 돌려준다.
"""
import json
import sys
from pathlib import Path

import joblib
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ml"))

from config import CATEGORY_LABEL, DATA_DIR, MODEL_DIR, TOOL_LABEL  # noqa: E402
from features import (PERSONAL_FEATURES, SEQ_FEATURES,  # noqa: E402
                      compute_personal_features, compute_sequence_features,
                      sequence_window)
from policy_engine import (check_policy, combine, decide_permission,  # noqa: E402
                           risk_band)
from scoring_util import percentile_score  # noqa: E402


def _won(x):
    return "%s원" % format(int(round(float(x))), ",")


class RiskEngine:
    def __init__(self):
        lg = joblib.load(MODEL_DIR / "lgbm_sequence.pkl")
        self.lgbm = lg["model"]
        self.lgbm_features = lg.get("features", SEQ_FEATURES)
        self.lgbm_metrics = lg["metrics"]
        self.lgbm_importance = lg["importance"]

        io = joblib.load(MODEL_DIR / "iforest_personal.pkl")
        self.iforest = io["model"]
        self.scaler = io["scaler"]
        self.ref_scores = io["ref_scores"]
        self.iforest_stats = io["stats"]

        self.baseline = json.loads(
            (DATA_DIR / "user_baseline.json").read_text(encoding="utf-8"))

    # ------------------------------------------------------------------
    def sequence_risk(self, action, history, policy):
        f = compute_sequence_features(action, history, policy, self.baseline)
        x = np.array([[f[n] for n in self.lgbm_features]], dtype=np.float64)
        p = float(self.lgbm.predict_proba(x)[0, 1])
        return round(p * 100.0, 1), f

    def personal_deviation(self, action, history):
        f = compute_personal_features(action, history, self.baseline)
        # IsolationForest 는 사용자의 '결제·송금' 내역만으로 학습했다.
        # 잔액·거래내역 조회처럼 금액이 없는 행동은 학습 분포 밖의 값(금액 0)이라
        # 모델에 넣으면 근거 없이 높은 이탈도가 나온다. 소비패턴 평가 대상에서 제외한다.
        if float(action.get("amount", 0)) <= 0:
            return 0.0, f
        x = self.scaler.transform([[f[n] for n in PERSONAL_FEATURES]])
        raw = float(-self.iforest.score_samples(x)[0])
        return percentile_score(raw, self.ref_scores), f

    # ------------------------------------------------------------------
    def evaluate(self, action, history, policy):
        """행동 1건을 평가한다. history 는 같은 세션의 이전 행동 리스트."""
        ts = float(action["ts"])
        h1 = sequence_window(history, ts, 3600)
        h24 = sequence_window(history, ts, 86400)
        cum24 = sum(h["amount"] for h in h24) + float(action["amount"])
        cum1 = sum(h["amount"] for h in h1) + float(action["amount"])

        seq_score, seq_f = self.sequence_risk(action, history, policy)
        pers_score, pers_f = self.personal_deviation(action, history)
        pol_score, violations, floor = check_policy(action, policy, cum24)

        total = combine(seq_score, pers_score, pol_score)
        permission = decide_permission(total, floor)

        factors = self._factors(action, seq_f, pers_score, policy,
                                cum1, cum24, h1, h24, violations)

        return {
            "scores": {
                "sequence_risk": seq_score,
                "personal_deviation": pers_score,
                "policy_risk": round(pol_score, 1),
                "total_risk": total,
                "band": risk_band(total),
            },
            "permission": permission,
            "policy_floor": floor,
            "violations": violations,
            "factors": factors,
            "features": {"sequence": seq_f, "personal": pers_f},
            "aggregates": {
                "cum_amount_1h": cum1, "cum_amount_24h": cum24,
                "tx_count_1h": len(h1) + 1, "tx_count_24h": len(h24) + 1,
            },
        }

    # ------------------------------------------------------------------
    def _factors(self, action, f, pers_score, policy, cum1, cum24, h1, h24, violations):
        """사용자 설명 화면에 쓰는 8개 분석 항목."""
        b = self.baseline
        amount = float(action["amount"])
        mean_amt = b["amount"]["mean"]
        auto_limit = float(policy.get("auto_limit") or 0)
        daily_limit = float(policy.get("daily_limit") or 0)
        median_gap = b["interval_sec"]["median"]
        gap = float(np.expm1(f["sec_since_prev_log"]))
        daily_mean_cnt = b["daily"]["tx_count_mean"]
        daily_mean_sum = b["daily"]["amount_sum_mean"]

        def lvl(a, b_, c):
            return "RISK" if a else ("CAUTION" if b_ else "NORMAL")

        ratio = amount / mean_amt if mean_amt else 0
        limit_ratio = amount / auto_limit if auto_limit else 0

        out = []
        out.append({
            "key": "amount", "label": "현재 거래금액",
            "value": _won(amount),
            "level": lvl(limit_ratio > 1.0 or ratio > 12, ratio > 4, None),
            "note": "평소 평균 %s의 %.1f배%s" % (
                _won(mean_amt), ratio,
                ("이며 자동실행 한도의 %.0f%%" % (limit_ratio * 100)) if auto_limit else ""),
        })

        is_new = bool(f["is_new_recipient"])
        out.append({
            "key": "new_recipient", "label": "신규 수취인 여부",
            "value": "신규 수취인" if is_new else "등록된 수취인",
            "level": lvl(is_new and amount >= mean_amt * 3, is_new, None),
            "note": ("최근 1시간 내 처음 보는 수취인이 %d명 등장했습니다."
                     % int(f["new_recipient_cnt_1h"])) if is_new
                    else "평소 거래하던 수취인입니다.",
        })

        n1, n24 = int(f["tx_cnt_1h"]), int(f["tx_cnt_24h"])
        out.append({
            "key": "tx_count", "label": "최근 거래 횟수",
            "value": "1시간 %d건 / 24시간 %d건" % (n1, n24),
            "level": lvl(n1 >= 6 or n24 > daily_mean_cnt * 4,
                         n1 >= 3 or n24 > daily_mean_cnt * 2, None),
            "note": "평소 하루 평균 %.1f건입니다." % daily_mean_cnt,
        })

        out.append({
            "key": "cumulative", "label": "최근 누적 거래금액",
            "value": "24시간 %s" % _won(cum24),
            "level": lvl(daily_limit and cum24 > daily_limit,
                         daily_limit and cum24 > daily_limit * 0.7, None),
            "note": "평소 하루 지출 %s / 1일 누적한도 %s (%.0f%% 사용)" % (
                _won(daily_mean_sum), _won(daily_limit) if daily_limit else "미설정",
                (cum24 / daily_limit * 100) if daily_limit else 0),
        })

        out.append({
            "key": "interval", "label": "거래 간 시간 간격",
            "value": _fmt_dur(gap),
            "level": lvl(gap < 60, gap < 300, None),
            "note": "평소 거래 간격 중앙값은 %s입니다." % _fmt_dur(median_gap),
        })

        near = int(f["near_limit_repeat"])
        out.append({
            "key": "limit_probing", "label": "자동송금 한도 근접 반복",
            "value": "%d회" % near,
            "level": lvl(near >= 3, near >= 2, None),
            "note": ("한도(%s)의 90~100%% 구간 거래가 1시간 내 %d회 반복됐습니다."
                     % (_won(auto_limit), near)) if near >= 2
                    else "한도 직전 금액을 반복하는 패턴은 확인되지 않았습니다.",
        })

        fails, retries = int(f["fail_cnt_1h"]), int(f["retry_cnt_1h"])
        # 여기서의 '실패'에는 은행이 거절한 건과 에이전트고삐가 차단한 건이 함께 들어간다.
        # 어느 쪽이든 '실행되지 않았는데 Agent가 계속 시도하고 있다'는 같은 신호다.
        if retries >= 2:
            retry_note = "같은 수취인에게 조건을 바꿔가며 반복 시도하고 있습니다. 위임 한도의 경계를 탐색하는 신호입니다."
        elif fails:
            retry_note = "실행되지 않은 요청이 있는데도 Agent가 요청을 이어가고 있습니다."
        else:
            retry_note = "최근 1시간 내 실행되지 않은 요청은 없습니다."
        out.append({
            "key": "retry", "label": "실패 및 재시도",
            "value": "미실행 %d건 / 동일 수취인 재시도 %d회" % (fails, retries),
            "level": lvl(fails >= 3 or retries >= 2, fails >= 1, None),
            "note": retry_note,
        })

        unknown_cat = bool(f["unknown_category"])
        cat = action.get("category", "ETC")
        out.append({
            "key": "personal_gap", "label": "평소 금융행동과의 차이",
            "value": "이탈도 %.0f점" % pers_score,
            "level": lvl(pers_score >= 75, pers_score >= 50, None),
            "note": ("'%s' 는 지난 1년간 이용 기록이 없는 거래 유형입니다."
                     % CATEGORY_LABEL.get(cat, cat)) if unknown_cat
                    else "거래 유형·시간대·금액대를 평소 패턴과 대조한 결과입니다.",
        })
        return out

    # ------------------------------------------------------------------
    def model_info(self):
        return {
            "lightgbm": {
                "name": "LightGBM · Agent 행동 시퀀스 위험도",
                "features": len(self.lgbm_features),
                "roc_auc": self.lgbm_metrics.get("roc_auc", (self.lgbm_metrics.get("session_holdout") or {}).get("roc_auc")),
                "pr_auc": self.lgbm_metrics.get("pr_auc", (self.lgbm_metrics.get("session_holdout") or {}).get("pr_auc")),
                "train_rows": self.lgbm_metrics["n_train"],
                "top_features": sorted(self.lgbm_importance.items(),
                                       key=lambda kv: -kv[1])[:8],
            },
            "isolation_forest": {
                "name": "IsolationForest · 개인 금융행동 이탈도",
                "features": len(PERSONAL_FEATURES),
                "train_rows": self.iforest_stats["n_train"],
            },
            "baseline": {
                "period": self.baseline["period"],
                "n_tx": self.baseline["n_tx"],
                "mean_amount": self.baseline["amount"]["mean"],
                "daily_tx": self.baseline["daily"]["tx_count_mean"],
                "daily_amount": self.baseline["daily"]["amount_sum_mean"],
                "n_recipients": self.baseline["n_known_recipients"],
            },
        }


def _fmt_dur(sec):
    sec = max(float(sec), 0)
    if sec < 60:
        return "%d초" % int(sec)
    if sec < 3600:
        return "%d분" % int(sec // 60)
    if sec < 86400:
        return "%.1f시간" % (sec / 3600)
    return "%.1f일" % (sec / 86400)


ENGINE = None


def get_engine():
    global ENGINE
    if ENGINE is None:
        ENGINE = RiskEngine()
    return ENGINE
