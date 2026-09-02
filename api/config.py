# -*- coding: utf-8 -*-
"""에이전트고삐 — 설정값."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
MODEL_DIR = ROOT / "ml" / "models"
WEB_DIR = ROOT / "web"

# --- 위험도 합산 가중치 --------------------------------------------------
# 기획서의 4개 평가축을 3개 산출물로 묶는다.
#   Transaction Risk + Cumulative Risk -> LightGBM 시퀀스 위험도
#   Behavior Risk                      -> IsolationForest 개인 이탈도
#   Policy Risk                        -> 위임정책 규칙 검증
W_SEQUENCE = 0.50
W_PERSONAL = 0.30
W_POLICY = 0.20

# --- 권한 결정 임계값 ----------------------------------------------------
# 0 ────── AUTO ──── 30 ──── VERIFY ──── 55 ──── READ_ONLY ──── 78 ──── STOP ── 100
THRESHOLD_VERIFY = 30.0
THRESHOLD_READ_ONLY = 55.0
THRESHOLD_STOP = 78.0

PERMISSIONS = ["AUTO", "VERIFY", "READ_ONLY", "STOP"]
PERMISSION_RANK = {p: i for i, p in enumerate(PERMISSIONS)}   # 클수록 제한적

PERMISSION_LABEL = {
    "AUTO": "자동 실행",
    "VERIFY": "추가 승인 필요",
    "READ_ONLY": "조회만 허용",
    "STOP": "실행권한 중단",
}
PERMISSION_DESC = {
    "AUTO": "잔액·거래내역 조회, 송금, 결제를 모두 자동으로 수행할 수 있습니다.",
    "VERIFY": "잔액·거래내역 조회는 가능하지만, 송금과 결제는 본인 승인 후에만 실행됩니다.",
    "READ_ONLY": "잔액·거래내역 조회만 가능합니다. 송금과 결제 실행 권한을 일시 회수했습니다.",
    "STOP": "Kill Switch가 발동되어 조회를 포함한 모든 금융 실행 권한이 중단되었습니다.",
}
PERMISSION_ALLOWS = {
    "AUTO": {"read": True, "transfer": True, "payment": True, "approval_required": False},
    "VERIFY": {"read": True, "transfer": True, "payment": True, "approval_required": True},
    "READ_ONLY": {"read": True, "transfer": False, "payment": False, "approval_required": False},
    "STOP": {"read": False, "transfer": False, "payment": False, "approval_required": False},
}

# --- 위임정책 기본값 -----------------------------------------------------
DEFAULT_POLICY = {
    "version": "1.0",
    "allowed_actions": ["BALANCE_READ", "HISTORY_READ", "TRANSFER", "PAYMENT"],
    "auto_limit": 500_000,
    "daily_limit": 1_500_000,
    "new_recipient": {"action": "VERIFY", "amount_threshold": 0},
    "blocked_categories": [],
    "allowed_categories": None,
    "time_window": None,
    "valid_days": 30,
    "on_anomaly": "REDUCE_PERMISSION",
    "verify_channel": "SELF",
    "allowed_tools": ["balance.read", "history.read",
                      "transfer.execute", "payment.execute"],
    "notes": "",
}

ACTION_TO_TOOL = {
    "BALANCE_READ": "balance.read",
    "HISTORY_READ": "history.read",
    "TRANSFER": "transfer.execute",
    "PAYMENT": "payment.execute",
    "RECIPIENT_REGISTER": "recipient.register",
    "LIMIT_MODIFY": "limit.modify",
    "INVEST_ORDER": "invest.order",
    "CARD_ISSUE": "card.issue",
}
TOOL_LABEL = {
    "balance.read": "잔액 조회",
    "history.read": "거래내역 조회",
    "transfer.execute": "송금 실행",
    "payment.execute": "결제 실행",
    "recipient.register": "수취인 등록",
    "limit.modify": "이체한도 변경",
    "invest.order": "투자 주문",
    "card.issue": "카드 발급",
}

CATEGORY_LABEL = {
    "FOOD": "식비", "CONVENIENCE": "편의점", "TRANSPORT": "교통",
    "SIMPLE_PAY": "간편결제", "SHOPPING": "쇼핑", "SUBSCRIPTION": "구독",
    "P2P": "개인송금", "RENT": "주거비", "UTILITY": "공과금",
    "SAVINGS": "저축", "ENTERTAIN": "여가", "LIVING": "생활",
    "CLOUD": "클라우드", "ETC": "기타",
    "CRYPTO": "가상자산", "GAMBLING": "사행성", "OVERSEAS_REMIT": "해외송금",
    "GIFT_CARD": "상품권", "PREPAID_CHARGE": "선불충전", "SECURITIES": "증권",
}

# --- 생성형 AI -----------------------------------------------------------
LLM_MODEL = "gemini-2.0-flash"  # Gemini 모델
LLM_ENABLED = bool(os.environ.get("GOOGLE_API_KEY"))
