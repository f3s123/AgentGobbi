# -*- coding: utf-8 -*-
"""
생성형 AI 계층
------------------------------------------------
두 가지 역할을 맡는다.

  1) Natural Language Policy Compiler
     사용자가 자연어로 쓴 위임조건 -> 구조화된 Policy JSON
     생성형 AI 의 자유형식 출력을 그대로 정책으로 쓰지 않는다.
     반드시 sanitize_policy() 를 통과한 값만 Policy Engine 입력이 된다.

  2) Risk Explanation Writer
     탐지 시스템이 산출한 8개 분석 항목 -> 사용자용 설명문

ANTHROPIC_API_KEY 가 있으면 Claude 를 호출하고, 없으면 동일한 입력으로
규칙 기반 생성기가 한국어 결과를 만든다(오프라인 MVP 시연 가능).
수치는 두 경로 모두 탐지 엔진이 계산한 값을 그대로 쓴다 — 생성형 AI 는
숫자를 만들어 내지 않고 서술만 담당한다.
"""
import json
import re

from config import (CATEGORY_LABEL, DEFAULT_POLICY, LLM_ENABLED, LLM_MODEL,
                    PERMISSION_DESC, PERMISSION_LABEL, TOOL_LABEL)

# --------------------------------------------------------------------------
# 공통
# --------------------------------------------------------------------------
_CLIENT = None


def _client():
    global _CLIENT
    if _CLIENT is None:
        import google.generativeai as genai
        api_key = __import__('os').environ.get("GOOGLE_API_KEY")
        genai.configure(api_key=api_key)
        _CLIENT = genai.GenerativeModel("gemini-3.6-flash")
    return _CLIENT


def llm_status():
    return {"enabled": LLM_ENABLED, "model": LLM_MODEL if LLM_ENABLED else None,
            "mode": "gemini" if LLM_ENABLED else "rule-based"}


def _won(x):
    return format(int(round(float(x))), ",") + "원"


def _josa(word, with_final, without_final):
    """받침 유무에 따라 조사를 고른다. (예: 유효기간'은' / 한도'는')"""
    ch = word[-1]
    if "가" <= ch <= "힣":
        return with_final if (ord(ch) - 0xAC00) % 28 else without_final
    return without_final


# --------------------------------------------------------------------------
# 1) Natural Language Policy Compiler
# --------------------------------------------------------------------------
ACTION_ENUM = ["BALANCE_READ", "HISTORY_READ", "TRANSFER", "PAYMENT",
               "RECIPIENT_REGISTER", "LIMIT_MODIFY", "INVEST_ORDER", "CARD_ISSUE"]
CATEGORY_ENUM = list(CATEGORY_LABEL.keys())

POLICY_SCHEMA = {
    "type": "object",
    "properties": {
        "auto_limit": {"type": "integer",
                       "description": "건당 자동 실행을 허용할 최대 금액(원)"},
        "daily_limit": {"type": "integer",
                        "description": "1일 누적 거래 한도(원)"},
        "new_recipient_action": {"type": "string",
                                 "enum": ["AUTO", "VERIFY", "BLOCK"]},
        "new_recipient_threshold": {"type": "integer",
                                    "description": "신규 수취인에 대해 승인을 요구하기 시작하는 금액(원). 금액과 무관하게 항상이면 0"},
        "allowed_actions": {"type": "array", "items": {"type": "string", "enum": ACTION_ENUM}},
        "blocked_categories": {"type": "array", "items": {"type": "string", "enum": CATEGORY_ENUM}},
        "time_window_start": {"type": ["integer", "null"], "minimum": 0, "maximum": 23},
        "time_window_end": {"type": ["integer", "null"], "minimum": 0, "maximum": 23},
        "valid_days": {"type": "integer", "description": "위임 유효기간(일)"},
        "verify_channel": {"type": "string", "enum": ["SELF", "TRUSTED_PERSON"]},
        "on_anomaly": {"type": "string", "enum": ["REDUCE_PERMISSION", "NOTIFY_ONLY"]},
        "summary_ko": {"type": "string", "description": "사용자가 읽을 한 문장 요약"},
        "assumptions": {"type": "array", "items": {"type": "string"},
                        "description": "사용자가 말하지 않아 기본값을 적용한 항목"},
    },
    "required": ["auto_limit", "daily_limit", "new_recipient_action",
                 "new_recipient_threshold", "allowed_actions", "blocked_categories",
                 "time_window_start", "time_window_end", "valid_days",
                 "verify_channel", "on_anomaly", "summary_ko", "assumptions"],
    "additionalProperties": False,
}

POLICY_SYSTEM = """당신은 금융 AI Agent 위임정책 컴파일러입니다.
사용자가 한국어로 말한 위임 조건을 정해진 스키마의 값으로만 변환합니다.

규칙:
- 사용자가 명시하지 않은 값은 임의로 강하게 설정하지 말고 안전한 기본값을 쓰고, assumptions 에 그 사실을 적습니다.
- 기본값: auto_limit 500000, daily_limit 1500000, new_recipient_action VERIFY,
  new_recipient_threshold 0, valid_days 30, verify_channel SELF,
  on_anomaly REDUCE_PERMISSION, allowed_actions 는 조회 2종 + TRANSFER + PAYMENT.
- LIMIT_MODIFY, INVEST_ORDER, CARD_ISSUE, RECIPIENT_REGISTER 는 사용자가 명시적으로
  허용했을 때만 allowed_actions 에 넣습니다.
- "만원"은 10000원, "억"은 100000000원입니다. 금액은 원 단위 정수로 씁니다.
- time_window_start와 time_window_end에는 금지 시간대가 아니라 허용 시간대를 넣습니다.
- 시간 구간은 시작 시각 이상, 종료 시각 미만으로 해석합니다.
- 종료 시각 0은 다음 날 자정을 의미합니다.
- 예: "밤 12시부터 오전 6시까지 하지 마"는
  time_window_start=6, time_window_end=0으로 변환합니다.
- 예: "오전 9시부터 오후 6시까지만 허용"은
  time_window_start=9, time_window_end=18로 변환합니다.
- 사용자가 시간대를 언급하지 않았다면 둘 다 null로 둡니다.
- summary_ko 는 사용자에게 그대로 보여줄 한 문장입니다. 존댓말로 씁니다."""


def compile_policy(text):
    """자연어 위임정책 -> 구조화 Policy. 실패하면 규칙 기반으로 대체한다."""
    raw, source = None, "rule-based"
    if LLM_ENABLED:
        try:
            # Gemini API 호출
            prompt = f"""{POLICY_SYSTEM}

사용자 입력:
{text}

응답은 다음 JSON 스키마를 정확히 따라 JSON으로만 반환하세요:
{json.dumps(POLICY_SCHEMA, ensure_ascii=False)}"""
            
            resp = _client().generate_content(prompt)
            # Gemini 응답에서 JSON 추출
            body = resp.text
            if body.startswith("```json"):
                body = body[7:]  # ```json 제거
            if body.startswith("```"):
                body = body[3:]  # ``` 제거
            if body.endswith("```"):
                body = body[:-3]  # ``` 제거
            body = body.strip()
            raw = json.loads(body)
            source = "gemini"
        except Exception as e:                      # 실패 시 조용히 규칙 기반으로
            import traceback
            error_msg = f"{type(e).__name__}: {str(e)}"
            print(f"[DEBUG] LLM Error: {error_msg}")
            traceback.print_exc()
            raw, source = None, f"rule-based (fallback: {error_msg})"

    if raw is None:
        raw = _rule_compile(text)

    policy = sanitize_policy(raw)
    policy["source"] = source
    policy["input_text"] = text
    return policy


# --- 규칙 기반 파서 -------------------------------------------------------
_NUM = r"([0-9][0-9,\.]*)\s*(억|만)?\s*원?"


def _parse_won(num, unit):
    v = float(num.replace(",", ""))
    if unit == "만":
        v *= 10_000
    elif unit == "억":
        v *= 100_000_000
    return int(round(v))


def _find_amounts(text):
    return [(m.start(), _parse_won(m.group(1), m.group(2)))
            for m in re.finditer(_NUM, text)]


DAILY_KEYS = ["하루", "1일", "일일", "하루에", "당일", "일 누적", "하루 누적"]
PER_TX_KEYS = ["건당", "한 번에", "한번에", "1회", "회당", "한 건", "건별"]
NEW_KEYS = ["신규", "처음", "새로운", "모르는", "낯선", "등록되지", "미등록", "안 해본"]
VERIFY_KEYS = ["물어", "확인", "승인", "허락", "알려", "묻고", "동의"]
BLOCK_KEYS = ["막아", "차단", "금지", "하지 마", "하지마", "안 돼", "안돼", "못 하게"]

CATEGORY_KEYS = {
    "OVERSEAS_REMIT": ["해외송금", "해외 송금", "국외송금"],
    "CRYPTO": ["코인", "가상자산", "암호화폐", "비트코인"],
    "GAMBLING": ["도박", "사행성", "베팅"],
    "GIFT_CARD": ["상품권", "기프트카드", "문화상품권"],
    "SECURITIES": ["주식", "증권", "투자"],
    "PREPAID_CHARGE": ["선불충전", "선불 충전"],
    "ENTERTAIN": ["유흥", "여가"],
}
ACTION_KEYS = {
    "INVEST_ORDER": ["주식", "투자", "매수", "매도", "ETF"],
    "LIMIT_MODIFY": ["한도 변경", "한도변경", "한도 상향"],
    "CARD_ISSUE": ["카드 발급", "카드발급"],
    "RECIPIENT_REGISTER": ["수취인 등록", "계좌 등록", "거래처 등록"],
}


def _rule_compile(text):
    t = text.replace("\n", " ")
    amounts = _find_amounts(t)
    out = {
        "auto_limit": None, "daily_limit": None,
        "new_recipient_action": "VERIFY", "new_recipient_threshold": 0,
        "allowed_actions": ["BALANCE_READ", "HISTORY_READ", "TRANSFER", "PAYMENT"],
        "blocked_categories": [], "time_window_start": None, "time_window_end": None,
        "valid_days": None, "verify_channel": "SELF",
        "on_anomaly": "REDUCE_PERMISSION", "summary_ko": "", "assumptions": [],
    }

    # 금액: 앞쪽 문맥 20자를 보고 '하루 누적' / '건당' 을 판별
    for pos, val in amounts:
        ctx = t[max(0, pos - 22):pos]
        if any(k in ctx for k in DAILY_KEYS):
            out["daily_limit"] = val
        elif any(k in ctx for k in PER_TX_KEYS):
            out["auto_limit"] = val
        elif any(k in ctx for k in NEW_KEYS):
            out["new_recipient_threshold"] = val
        elif out["auto_limit"] is None:
            out["auto_limit"] = val
        elif out["daily_limit"] is None:
            out["daily_limit"] = val

    # 신규 수취인 처리
    for k in NEW_KEYS:
        i = t.find(k)
        if i < 0:
            continue
        seg = t[i:i + 60]
        if any(b in seg for b in BLOCK_KEYS):
            out["new_recipient_action"] = "BLOCK"
        elif any(v in seg for v in VERIFY_KEYS):
            out["new_recipient_action"] = "VERIFY"
        break

    # 차단 카테고리
    for cat, keys in CATEGORY_KEYS.items():
        for k in keys:
            i = t.find(k)
            if i >= 0 and any(b in t[max(0, i - 15):i + 30] for b in BLOCK_KEYS):
                out["blocked_categories"].append(cat)
                break

    # 추가 허용 행위
    for act, keys in ACTION_KEYS.items():
        for k in keys:
            i = t.find(k)
            if i < 0:
                continue
            seg = t[max(0, i - 15):i + 35]
            if any(b in seg for b in BLOCK_KEYS):
                continue
            if any(v in seg for v in ["해도", "허용", "맡기", "알아서", "가능"]):
                out["allowed_actions"].append(act)
            break

    # 시간대
    m = re.search(r"(밤|새벽|심야)", t)
    if m and any(b in t for b in BLOCK_KEYS + VERIFY_KEYS):
        out["time_window_start"], out["time_window_end"] = 7, 23
    m = re.search(r"(\d{1,2})\s*시\s*(?:부터|~|-)\s*(\d{1,2})\s*시", t)
    if m:
        out["time_window_start"], out["time_window_end"] = int(m.group(1)), int(m.group(2))

    # 유효기간
    m = re.search(r"(\d+)\s*(일|주|개월|달)\s*(?:동안|간|까지)?", t)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        out["valid_days"] = n * {"일": 1, "주": 7, "개월": 30, "달": 30}[unit]

    if "가족" in t or "부모" in t or "신뢰" in t:
        out["verify_channel"] = "TRUSTED_PERSON"

    for key, label in [("auto_limit", "건당 자동실행 한도"),
                       ("daily_limit", "1일 누적한도"),
                       ("valid_days", "위임 유효기간")]:
        if out[key] is None:
            out["assumptions"].append(
                "%s%s 지정하지 않아 기본값을 적용했습니다."
                % (label, _josa(label, "은", "는")))
    if out["time_window_start"] is None:
        out["assumptions"].append("허용 시간대는 지정하지 않아 24시간 허용으로 두었습니다.")

    out["summary_ko"] = _summarize_policy_ko(out)
    return out


def _summarize_policy_ko(p):
    auto = p.get("auto_limit") or DEFAULT_POLICY["auto_limit"]
    daily = p.get("daily_limit") or DEFAULT_POLICY["daily_limit"]
    nr = {"AUTO": "신규 수취인도 자동 실행", "VERIFY": "신규 수취인은 본인 승인 후 실행",
          "BLOCK": "신규 수취인 송금은 차단"}[p.get("new_recipient_action", "VERIFY")]
    return ("건당 %s까지, 하루 최대 %s까지 자동으로 실행하고 %s합니다."
            % (_won(auto), _won(daily), nr))


# --- 스키마 강제 ----------------------------------------------------------
def sanitize_policy(raw):
    """생성형 AI 출력에서 스키마에 맞는 값만 뽑아 Policy Engine 입력으로 만든다."""
    def clamp_int(v, lo, hi, default):
        try:
            return int(min(max(int(v), lo), hi))
        except (TypeError, ValueError):
            return default

    auto = clamp_int(raw.get("auto_limit"), 0, 100_000_000, DEFAULT_POLICY["auto_limit"])
    daily = clamp_int(raw.get("daily_limit"), 0, 1_000_000_000, DEFAULT_POLICY["daily_limit"])
    if daily < auto:                      # 논리적으로 일 한도가 건당보다 작을 수 없다
        daily = auto

    raw_new_recipient = raw.get("new_recipient") or {}
    nr_action = raw.get("new_recipient_action", raw_new_recipient.get("action"))
    if nr_action not in ("AUTO", "VERIFY", "BLOCK"):
        nr_action = "VERIFY"

    actions = [a for a in (raw.get("allowed_actions") or []) if a in ACTION_ENUM]
    for must in ("BALANCE_READ", "HISTORY_READ"):
        if must not in actions:
            actions.append(must)
    actions = sorted(set(actions), key=ACTION_ENUM.index)

    blocked = sorted({c for c in (raw.get("blocked_categories") or [])
                      if c in CATEGORY_ENUM})

    raw_time_window = raw.get("time_window") or {}
    ws = raw.get("time_window_start", raw_time_window.get("start"))
    we = raw.get("time_window_end", raw_time_window.get("end"))
    window = None
    if isinstance(ws, int) and isinstance(we, int) and 0 <= ws <= 23 and 0 <= we <= 23 and ws != we:
        window = {"start": ws, "end": we}

    policy = dict(DEFAULT_POLICY)
    policy.update({
        "auto_limit": auto,
        "daily_limit": daily,
        "new_recipient": {
            "action": nr_action,
            "amount_threshold": clamp_int(raw.get("new_recipient_threshold",
                                                  raw_new_recipient.get("amount_threshold")),
                                          0, 100_000_000, 0),
        },
        "allowed_actions": actions,
        "allowed_tools": [TOOL_KEY for TOOL_KEY in
                          [_action_tool(a) for a in actions] if TOOL_KEY],
        "blocked_categories": blocked,
        "allowed_categories": None,
        "time_window": window,
        "valid_days": clamp_int(raw.get("valid_days"), 1, 365,
                                DEFAULT_POLICY["valid_days"]),
        "verify_channel": (raw.get("verify_channel")
                           if raw.get("verify_channel") in ("SELF", "TRUSTED_PERSON")
                           else "SELF"),
        "on_anomaly": (raw.get("on_anomaly")
                       if raw.get("on_anomaly") in ("REDUCE_PERMISSION", "NOTIFY_ONLY")
                       else "REDUCE_PERMISSION"),
        "summary_ko": str(raw.get("summary_ko") or "")[:300],
        "assumptions": [str(a)[:160] for a in (raw.get("assumptions") or [])][:6],
    })
    if not policy["summary_ko"]:
        policy["summary_ko"] = _summarize_policy_ko(
            {"auto_limit": auto, "daily_limit": daily,
             "new_recipient_action": nr_action})
    return policy


def _action_tool(action):
    from config import ACTION_TO_TOOL
    return ACTION_TO_TOOL.get(action)


def policy_display(policy):
    """정책을 화면에 뿌릴 항목 리스트로 변환."""
    nr = policy.get("new_recipient", {})
    nr_txt = {"AUTO": "자동 실행 허용", "VERIFY": "본인 승인 필요",
              "BLOCK": "송금 차단"}[nr.get("action", "VERIFY")]
    if nr.get("action") == "VERIFY" and nr.get("amount_threshold"):
        nr_txt += " (%s 이상)" % _won(nr["amount_threshold"])

    tw = policy.get("time_window")
    rows = [
        ("등록 수취인 자동송금 한도", _won(policy["auto_limit"]), "건당 이 금액까지는 승인 없이 실행합니다."),
        ("1일 누적 한도", _won(policy["daily_limit"]), "24시간 누적 거래금액이 이 값을 넘으면 승인을 요구합니다."),
        ("신규 수취인", nr_txt, "처음 보는 계좌·가맹점에 대한 처리 방식입니다."),
        ("위임한 금융행위", " · ".join(TOOL_LABEL.get(_action_tool(a), a)
                                 for a in policy["allowed_actions"]),
         "목록에 없는 기능을 호출하면 즉시 권한을 회수합니다."),
        ("차단 거래 유형",
         " · ".join(CATEGORY_LABEL.get(c, c) for c in policy["blocked_categories"])
         or "없음", "이 유형의 거래는 실행하지 않습니다."),
        ("허용 시간대",
         ("%02d시 ~ %02d시" % (tw["start"], tw["end"])) if tw else "24시간",
         "시간대 밖의 거래는 위험도에 가중됩니다."),
        ("위임 유효기간", "%d일" % policy["valid_days"], "기간이 지나면 권한이 자동 회수됩니다."),
        ("추가 승인 주체",
         "본인" if policy["verify_channel"] == "SELF" else "지정 신뢰인",
         "VERIFY 단계에서 승인을 요청할 대상입니다."),
        ("이상행동 발생 시",
         "권한 자동 축소" if policy["on_anomaly"] == "REDUCE_PERMISSION" else "알림만 발송",
         "축소된 권한의 복원에는 반드시 본인 승인이 필요합니다."),
    ]
    return [{"label": a, "value": b, "hint": c} for a, b, c in rows]


# --------------------------------------------------------------------------
# 2) Risk Explanation Writer
# --------------------------------------------------------------------------
EXPLAIN_SYSTEM = """당신은 금융 AI Agent 권한통제 서비스 '에이전트고삐'의 설명 작성자입니다.
탐지 시스템이 계산한 수치를 근거로, 사용자에게 왜 이런 판단이 내려졌는지 설명합니다.

규칙:
- 주어진 수치만 사용합니다. 새로운 숫자를 만들어 내지 마세요.
- 사용자는 금융 전문가가 아닙니다. 쉬운 한국어 존댓말로 씁니다.
- headline: 무슨 일이 있었는지 한 문장 (40자 이내).
- summary: 상황 요약 2~3문장.
- detail: 왜 위험하다고 판단했는지 근거를 문단으로 설명. 3~5문장.
  개별 거래는 정상으로 보여도 흐름 전체가 왜 문제인지 짚어 주세요.
- recommendation: 사용자가 지금 무엇을 하면 되는지 1~2문장.
- 권한이 AUTO 면 안심시키되 무엇을 계속 지켜보는지 알려 주세요."""

EXPLAIN_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "summary": {"type": "string"},
        "detail": {"type": "string"},
        "recommendation": {"type": "string"},
    },
    "required": ["headline", "summary", "detail", "recommendation"],
    "additionalProperties": False,
}


def explain_result(ctx):
    """
    ctx: {scenario, permission, scores, factors, violations, timeline_stats, policy}
    """
    if LLM_ENABLED:
        try:
            prompt = f"""{EXPLAIN_SYSTEM}

다음 JSON 분석 결과를 바탕으로 설명을 작성해주세요:
{json.dumps(ctx, ensure_ascii=False, indent=2)}

응답은 다음 JSON 스키마를 정확히 따라야 합니다:
{json.dumps(EXPLAIN_SCHEMA, ensure_ascii=False)}"""
            
            resp = _client().generate_content(prompt)
            body = resp.text
            # Gemini 응답에서 JSON 추출
            if body.startswith("```json"):
                body = body[7:]
            if body.startswith("```"):
                body = body[3:]
            if body.endswith("```"):
                body = body[:-3]
            body = body.strip()
            out = json.loads(body)
            out["source"] = "gemini"
            return out
        except Exception as e:
            pass
    out = _rule_explain(ctx)
    out["source"] = "rule-based"
    return out


def _rule_explain(ctx):
    perm = ctx["permission"]
    sc = ctx["scores"]
    factors = ctx["factors"]
    violations = ctx.get("violations", [])
    st = ctx.get("timeline_stats", {})
    scn = ctx.get("scenario", {})

    risky = [f for f in factors if f["level"] == "RISK"]
    caution = [f for f in factors if f["level"] == "CAUTION"]

    # --- headline ---
    if perm == "AUTO":
        headline = "평소 패턴과 일치합니다. 자동 실행 권한을 유지합니다."
    elif perm == "VERIFY":
        headline = "평소와 다른 흐름이 보여 승인 절차를 추가했습니다."
    elif perm == "READ_ONLY":
        headline = "실행 권한을 회수하고 조회만 허용합니다."
    else:
        headline = "Kill Switch를 발동해 금융 실행을 전면 중단했습니다."

    # --- summary ---
    parts = []
    n = st.get("total_actions", 0)
    span = st.get("span_text", "")
    moved = st.get("requested_amount", 0)
    parts.append("AI Agent가 %s 동안 %d건의 금융 요청을 보냈고, 요청한 금액은 모두 %s입니다."
                 % (span, n, _won(moved)))
    if perm == "AUTO":
        parts.append("금액대·수취인·시간대가 모두 평소 범위 안에 있어 위험도 %.0f점(낮음)으로 평가했습니다."
                     % sc["total_risk"])
    else:
        top = risky[:2] or caution[:2]
        if top:
            parts.append("특히 %s에서 이상 신호가 나타났습니다."
                         % ", ".join("'%s'" % f["label"] for f in top))
        parts.append("행동 시퀀스 위험도 %.0f점, 평소 행동 대비 이탈도 %.0f점, 위임정책 위반 %.0f점을 합산해 "
                     "총 위험도는 %.0f점(%s)입니다."
                     % (sc["sequence_risk"], sc["personal_deviation"],
                        sc["policy_risk"], sc["total_risk"], sc["band"]))
    summary = " ".join(parts)

    # --- detail ---
    d = []
    if scn.get("detail"):
        d.append(scn["detail"])
    for f in (risky + caution)[:5]:
        note = f["note"].rstrip()
        if not note.endswith((".", "다", "요")):
            note += "입니다."
        elif not note.endswith("."):
            note += "."
        d.append("%s %s — %s" % (f["label"], f["value"], note))
    if violations:
        d.append("위임정책 위반 항목은 %s입니다."
                 % ", ".join("%s(%s)" % (v["label"], v["message"]) for v in violations[:3]))
    if perm != "AUTO":
        d.append("개별 거래만 보면 한도를 지킨 것처럼 보일 수 있지만, "
                 "에이전트고삐는 거래 한 건이 아니라 연속된 행동 흐름 전체를 함께 평가합니다. "
                 "그래서 각각은 정책 위반이 아니어도 흐름이 평소와 다르면 권한을 조정합니다.")
    else:
        d.append("현재는 거래금액, 신규 수취인 여부, 최근 거래 횟수와 누적금액, 거래 간격, "
                 "한도 근접 반복, 실패·재시도, 평소 행동과의 차이를 계속 지켜보고 있습니다.")
    detail = " ".join(d)

    # --- recommendation ---
    if perm == "AUTO":
        rec = "지금은 따로 하실 일이 없습니다. 위임정책을 바꾸고 싶으시면 정책 설정 화면에서 수정하실 수 있습니다."
    elif perm == "VERIFY":
        rec = "대기 중인 요청 내역을 확인하시고, 본인이 의도한 거래가 맞으면 승인해 주세요. 아니라면 거절하시면 즉시 차단됩니다."
    elif perm == "READ_ONLY":
        rec = "송금·결제 실행 권한을 회수했습니다. 거래 목록을 확인하신 뒤, 문제가 없다면 본인 인증을 거쳐 권한을 복원해 주세요."
    else:
        rec = "모든 금융 실행이 중단됐습니다. 계좌와 최근 거래를 확인하시고, 의심스러운 거래가 있으면 금융회사에 즉시 신고해 주세요. 권한 복원은 본인 확인 후에만 가능합니다."

    return {"headline": headline, "summary": summary,
            "detail": detail, "recommendation": rec}
