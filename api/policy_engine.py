# -*- coding: utf-8 -*-
"""
Policy Engine — 위임정책 검증 + 권한 결정
------------------------------------------------
머신러닝은 '현재 Agent 행동이 얼마나 위험한가'만 예측한다.
AUTO / VERIFY / READ_ONLY / STOP 이라는 최종 권한 상태는 이 모듈이 정한다.

핵심 원칙 (기획서 4-2)
  - 권한 축소는 위험도에 따라 자동으로 즉시 이루어진다.
  - 권한 복원은 시스템이 스스로 하지 않는다. 반드시 사용자 승인을 거친다.
    -> 세션 안에서 권한은 한 방향(제한 강화)으로만 움직인다(Ratchet).
"""
from datetime import datetime

from config import (ACTION_TO_TOOL, PERMISSION_ALLOWS, PERMISSION_DESC,
                    PERMISSION_LABEL, PERMISSION_RANK, PERMISSIONS,
                    THRESHOLD_READ_ONLY, THRESHOLD_STOP, THRESHOLD_VERIFY,
                    TOOL_LABEL, W_PERSONAL, W_POLICY, W_SEQUENCE)

# 정책 위반 항목: (코드, 라벨, 가중치, 최소 강제 권한)
VIOLATION_SPEC = {
    "POLICY_EXPIRED": ("위임 유효기간 만료", 70, "READ_ONLY"),
    "ACTION_NOT_DELEGATED": ("위임하지 않은 금융행위", 65, "READ_ONLY"),
    "UNAUTHORIZED_TOOL": ("위임하지 않은 금융 Tool 호출", 60, "READ_ONLY"),
    "BLOCKED_CATEGORY": ("차단 카테고리 거래", 50, "VERIFY"),
    "AUTO_LIMIT_EXCEEDED": ("자동송금 한도 초과", 45, "VERIFY"),
    "DAILY_LIMIT_EXCEEDED": ("1일 누적한도 초과", 40, "VERIFY"),
    "NEW_RECIPIENT_APPROVAL": ("신규 수취인 승인 필요", 25, "VERIFY"),
    "OUTSIDE_TIME_WINDOW": ("허용 시간대 외 거래", 20, "VERIFY"),
    "CATEGORY_NOT_ALLOWED": ("허용 목록에 없는 카테고리", 30, "VERIFY"),
}


def check_policy(action, policy, cum_amount_24h, now=None):
    """위임정책 위반 항목을 찾아 (정책위험도, 위반목록, 최소강제권한) 반환."""
    violations = []
    amount = float(action.get("amount", 0))
    tool = action.get("tool")
    category = action.get("category")
    is_new = bool(action.get("is_new_recipient"))
    hour = int(action.get("hour", 12))

    valid_until = policy.get("valid_until")
    if valid_until:
        try:
            if datetime.fromisoformat(str(valid_until)) < (now or datetime.now()):
                violations.append(("POLICY_EXPIRED", "위임 유효기간이 지났습니다."))
        except ValueError:
            pass

    action_type = action.get("action_type")
    allowed_actions = policy.get("allowed_actions") or []
    if action_type and allowed_actions and action_type not in allowed_actions:
        violations.append(("ACTION_NOT_DELEGATED",
                           "'%s' 는 위임 범위에 없는 금융행위입니다." % action_type))

    allowed_tools = policy.get("allowed_tools")
    if allowed_tools and tool and tool not in allowed_tools:
        violations.append(("UNAUTHORIZED_TOOL",
                           "'%s' 기능은 위임하지 않았습니다." % TOOL_LABEL.get(tool, tool)))

    blocked = policy.get("blocked_categories") or []
    if category and category in blocked:
        violations.append(("BLOCKED_CATEGORY", "차단하도록 설정한 거래 유형입니다."))

    allow_cats = policy.get("allowed_categories")
    if allow_cats and category and category not in allow_cats:
        violations.append(("CATEGORY_NOT_ALLOWED", "허용 목록에 없는 거래 유형입니다."))

    auto_limit = float(policy.get("auto_limit") or 0)
    if auto_limit and amount > auto_limit:
        violations.append(("AUTO_LIMIT_EXCEEDED",
                           "건당 자동실행 한도 %s원을 %s원 초과했습니다."
                           % (f"{int(auto_limit):,}", f"{int(amount - auto_limit):,}")))

    daily_limit = float(policy.get("daily_limit") or 0)
    if daily_limit and cum_amount_24h > daily_limit:
        violations.append(("DAILY_LIMIT_EXCEEDED",
                           "1일 누적한도 %s원을 넘어섰습니다 (누적 %s원)."
                           % (f"{int(daily_limit):,}", f"{int(cum_amount_24h):,}")))

    nr = policy.get("new_recipient") or {}
    if is_new and nr.get("action") in ("VERIFY", "BLOCK"):
        if amount >= float(nr.get("amount_threshold") or 0):
            violations.append(("NEW_RECIPIENT_APPROVAL",
                               "처음 보는 수취인입니다. 사전 승인 대상으로 설정되어 있습니다."))

    tw = policy.get("time_window")
    if tw and tw.get("start") is not None and tw.get("end") is not None:
        s, e = int(tw["start"]), int(tw["end"])
        ok = (s <= hour < e) if s < e else (hour >= s or hour < e)
        if not ok:
            violations.append(("OUTSIDE_TIME_WINDOW",
                               "허용 시간대(%02d시~%02d시) 밖의 거래입니다." % (s, e)))

    score = 0.0
    floor = "AUTO"
    detail = []
    for code, msg in violations:
        label, weight, min_perm = VIOLATION_SPEC[code]
        score += weight
        if min_perm and PERMISSION_RANK[min_perm] > PERMISSION_RANK[floor]:
            floor = min_perm
        detail.append({"code": code, "label": label, "message": msg, "weight": weight})

    return min(score, 100.0), detail, floor


def combine(sequence_risk, personal_deviation, policy_risk):
    """세 축의 점수를 하나의 Delegation Risk Score(0~100)로 합산."""
    total = (W_SEQUENCE * sequence_risk
             + W_PERSONAL * personal_deviation
             + W_POLICY * policy_risk)
    return round(min(max(total, 0.0), 100.0), 1)


def decide_permission(total_risk, policy_floor="AUTO"):
    """위험도 + 정책 최소강제권한 -> 권한 등급."""
    if total_risk >= THRESHOLD_STOP:
        by_score = "STOP"
    elif total_risk >= THRESHOLD_READ_ONLY:
        by_score = "READ_ONLY"
    elif total_risk >= THRESHOLD_VERIFY:
        by_score = "VERIFY"
    else:
        by_score = "AUTO"

    # 더 제한적인 쪽을 채택
    return (by_score if PERMISSION_RANK[by_score] >= PERMISSION_RANK[policy_floor]
            else policy_floor)


def ratchet(previous, proposed):
    """
    권한은 세션 안에서 축소 방향으로만 움직인다.
    복원은 사용자 승인 API 를 통해서만 가능하다.
    """
    if previous is None:
        return proposed
    return proposed if PERMISSION_RANK[proposed] > PERMISSION_RANK[previous] else previous


def risk_band(total_risk):
    if total_risk >= THRESHOLD_STOP:
        return "매우 높음"
    if total_risk >= THRESHOLD_READ_ONLY:
        return "높음"
    if total_risk >= THRESHOLD_VERIFY:
        return "주의"
    return "낮음"


def permission_view(permission):
    return {
        "permission": permission,
        "label": PERMISSION_LABEL[permission],
        "description": PERMISSION_DESC[permission],
        "allows": PERMISSION_ALLOWS[permission],
        "rank": PERMISSION_RANK[permission],
        "levels": PERMISSIONS,
    }


def enforce(permission, action):
    """
    현재 권한으로 이 행동을 실행할 수 있는지 판정한다.
    반환: EXECUTED / PENDING_APPROVAL / BLOCKED
    """
    allows = PERMISSION_ALLOWS[permission]
    tool = action.get("tool") or ACTION_TO_TOOL.get(action.get("action_type", ""), "")
    is_read = tool in ("balance.read", "history.read")

    if is_read:
        return "EXECUTED" if allows["read"] else "BLOCKED"

    is_transfer = tool in ("transfer.execute",)
    can = allows["transfer"] if is_transfer else allows["payment"]
    if not can:
        return "BLOCKED"
    return "PENDING_APPROVAL" if allows["approval_required"] else "EXECUTED"
