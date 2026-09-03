# -*- coding: utf-8 -*-
"""
에이전트고삐 — API 서버
------------------------------------------------
금융 AI Agent 와 실제 금융 실행 API 사이에 놓이는 권한통제 계층의 MVP.

  POST /api/policy/compile     자연어 위임정책 -> 구조화 Policy (생성형 AI)
  POST /api/policy/approve     사용자가 확인한 정책을 활성화
  POST /api/simulate           시나리오를 흘려보내 권한 조정 과정을 재현
  GET  /api/result/{run_id}    시뮬레이션 결과 조회
  POST /api/permission/restore 축소된 권한 복원 (사용자 승인 필수)
  POST /api/evaluate           단건 평가 (외부 Agent 연동용)
"""
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# .env 파일에서 환경변수 로드
from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, HTTPException                       # noqa: E402
from fastapi.middleware.cors import CORSMiddleware               # noqa: E402
from fastapi.responses import FileResponse                       # noqa: E402
from fastapi.staticfiles import StaticFiles                      # noqa: E402

from config import (CATEGORY_LABEL, DEFAULT_POLICY, PERMISSION_RANK,  # noqa: E402
                    TOOL_LABEL, WEB_DIR)
from engine import get_engine                                    # noqa: E402
from llm import (compile_policy, explain_result, llm_status,      # noqa: E402
                 policy_display, sanitize_policy)
from policy_engine import enforce, permission_view, ratchet       # noqa: E402
from scenarios import (OPENING_BALANCE, SCENARIO_MAP, build_actions,  # noqa: E402
                       list_scenarios)
from schemas import (EvaluateRequest, PolicyApproveRequest,       # noqa: E402
                     PolicyCompileRequest, RestoreRequest, SimulateRequest)

app = FastAPI(title="에이전트고삐 API", version="1.0.0",
              description="금융 AI Agent를 위한 개인 맞춤형 동적 위임 안전장치")
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

# --- 상태 저장소 (MVP: 인메모리) -----------------------------------------
STATE = {
    "policy": None,        # 활성 위임정책
    "runs": {},            # run_id -> 시뮬레이션 결과
}


def active_policy():
    if STATE["policy"] is None:
        p = sanitize_policy({
            "auto_limit": DEFAULT_POLICY["auto_limit"],
            "daily_limit": DEFAULT_POLICY["daily_limit"],
            "new_recipient_action": "VERIFY",
            "new_recipient_threshold": 0,
            "allowed_actions": DEFAULT_POLICY["allowed_actions"],
            "blocked_categories": [],
            "time_window_start": None, "time_window_end": None,
            "valid_days": DEFAULT_POLICY["valid_days"],
            "verify_channel": "SELF", "on_anomaly": "REDUCE_PERMISSION",
            "summary_ko": "", "assumptions": ["아직 위임정책을 설정하지 않아 기본값이 적용되어 있습니다."],
        })
        p["valid_until"] = (datetime.now() + timedelta(days=p["valid_days"])).isoformat()
        p["source"] = "default"
        STATE["policy"] = p
    return STATE["policy"]


def _won(x):
    return format(int(round(float(x))), ",") + "원"


def _fmt_span(seconds):
    if seconds < 60:
        return "%d초" % int(seconds)
    if seconds < 3600:
        return "%d분" % int(seconds // 60)
    return "%.1f시간" % (seconds / 3600)


# ==========================================================================
# 기본 정보
# ==========================================================================
@app.get("/api/health")
def health():
    return {"status": "ok", "llm": llm_status(),
            "policy_configured": STATE["policy"] is not None}


@app.get("/api/models")
def models():
    return {**get_engine().model_info(), "llm": llm_status()}


# ==========================================================================
# 1) 위임정책
# ==========================================================================
@app.get("/api/policy")
def get_policy():
    p = active_policy()
    return {"policy": p, "display": policy_display(p)}


@app.post("/api/policy/compile")
def post_policy_compile(req: PolicyCompileRequest):
    """자연어 -> 구조화 Policy. 아직 활성화하지 않고 미리보기만 반환한다."""
    policy = compile_policy(req.text)
    policy["valid_until"] = (datetime.now()
                             + timedelta(days=policy["valid_days"])).isoformat()
    return {"policy": policy, "display": policy_display(policy),
            "llm": llm_status()}


@app.post("/api/policy/preview")
def post_policy_preview(req: PolicyApproveRequest):
    """사용자가 수정한 Policy를 서버 기준으로 정리한 뒤 미리보기만 반환한다."""
    policy = sanitize_policy(req.policy)
    policy["valid_until"] = (datetime.now()
                             + timedelta(days=policy["valid_days"])).isoformat()
    policy["source"] = req.policy.get("source", "user-edited")
    policy["input_text"] = req.policy.get("input_text", "")
    return {"policy": policy, "display": policy_display(policy),
            "llm": llm_status()}


@app.post("/api/policy/approve")
def post_policy_approve(req: PolicyApproveRequest):
    """사용자가 변환 결과를 확인한 뒤 최종 승인한다."""
    policy = sanitize_policy(req.policy)
    policy["valid_until"] = (datetime.now()
                             + timedelta(days=policy["valid_days"])).isoformat()
    policy["source"] = req.policy.get("source", "user-approved")
    policy["input_text"] = req.policy.get("input_text", "")
    policy["approved_at"] = datetime.now().isoformat()
    STATE["policy"] = policy
    return {"ok": True, "policy": policy, "display": policy_display(policy)}


# ==========================================================================
# 2) 시뮬레이션
# ==========================================================================
@app.get("/api/scenarios")
def get_scenarios():
    policy = active_policy()
    return {"scenarios": list_scenarios(policy),
            "policy_summary": {
                "auto_limit": policy["auto_limit"],
                "daily_limit": policy["daily_limit"],
            }}


@app.post("/api/simulate")
def post_simulate(req: SimulateRequest):
    if req.scenario_id not in SCENARIO_MAP:
        raise HTTPException(404, "알 수 없는 시나리오입니다: %s" % req.scenario_id)

    engine = get_engine()
    policy = active_policy()
    scn, actions = build_actions(req.scenario_id, policy)

    history = []
    balance = float(OPENING_BALANCE)
    permission = "AUTO"
    steps = []
    permission_events = []
    peak = None

    for act in actions:
        act = dict(act)
        act["balance_before"] = balance

        ev = engine.evaluate(act, history, policy)
        proposed = ev["permission"]
        before = permission
        permission = ratchet(permission, proposed)

        outcome = enforce(permission, act)
        # 우리 계층을 통과한 뒤 은행 API 가 거절한 경우 (시나리오가 선언)
        if outcome == "EXECUTED" and act.get("status") == "FAILED":
            outcome = "REJECTED_BY_BANK"

        executed = outcome == "EXECUTED"
        if executed:
            balance = max(balance - float(act["amount"]), 0.0)

        if permission != before:
            permission_events.append({
                "seq": act["seq"],
                "time": act["datetime"].strftime("%H:%M:%S"),
                "from": before, "to": permission,
                "risk": ev["scores"]["total_risk"],
                "reason": (ev["violations"][0]["message"] if ev["violations"]
                           else "행동 시퀀스 위험도가 임계값을 넘었습니다."),
            })

        step = {
            "seq": act["seq"],
            "time": act["datetime"].strftime("%H:%M:%S"),
            "action_type": act["action_type"],
            "action_label": TOOL_LABEL.get(act["tool"], act["action_type"]),
            "amount": float(act["amount"]),
            "amount_text": _won(act["amount"]) if act["amount"] else "-",
            "recipient_name": act["recipient_name"],
            "is_new_recipient": bool(ev["features"]["sequence"]["is_new_recipient"]),
            "category": act["category"],
            "category_label": CATEGORY_LABEL.get(act["category"], act["category"]),
            "memo": act.get("memo", ""),
            "scores": ev["scores"],
            "permission_before": before,
            "permission": permission,
            "outcome": outcome,
            "violations": [v["label"] for v in ev["violations"]],
            "balance_after": balance,
        }
        steps.append(step)

        if peak is None or ev["scores"]["total_risk"] >= peak["ev"]["scores"]["total_risk"]:
            peak = {"ev": ev, "step": step}

        history.append({
            "ts": act["ts"], "amount": float(act["amount"]),
            "recipient_id": act["recipient_id"], "category": act["category"],
            "tx_type": "TRANSFER" if act["action_type"] == "TRANSFER" else "PAYMENT",
            "status": "SUCCESS" if executed else "FAILED",
            "tool": act["tool"],
            "is_new_recipient": int(ev["features"]["sequence"]["is_new_recipient"]),
            "balance_before": act["balance_before"],
        })

    # --- 집계 -----------------------------------------------------------
    span = actions[-1]["ts"] - actions[0]["ts"]
    money_actions = [s for s in steps if s["amount"] > 0]
    stats = {
        "total_actions": len(steps),
        "money_actions": len(money_actions),
        "requested_amount": sum(s["amount"] for s in steps),
        "executed_amount": sum(s["amount"] for s in steps if s["outcome"] == "EXECUTED"),
        "blocked_amount": sum(s["amount"] for s in steps
                              if s["outcome"] in ("BLOCKED", "PENDING_APPROVAL")),
        "executed": sum(1 for s in steps if s["outcome"] == "EXECUTED"),
        "pending": sum(1 for s in steps if s["outcome"] == "PENDING_APPROVAL"),
        "blocked": sum(1 for s in steps if s["outcome"] == "BLOCKED"),
        "rejected": sum(1 for s in steps if s["outcome"] == "REJECTED_BY_BANK"),
        "new_recipients": len({s["recipient_name"] for s in steps if s["is_new_recipient"]}),
        "span_sec": span,
        "span_text": _fmt_span(span),
        "start_time": actions[0]["datetime"].strftime("%m월 %d일 %H:%M"),
        "end_time": actions[-1]["datetime"].strftime("%H:%M"),
        "peak_seq": peak["step"]["seq"],
        "opening_balance": OPENING_BALANCE,
        "closing_balance": balance,
    }

    ctx = {
        "scenario": {"title": scn["title"], "summary": scn["summary"],
                     "detail": scn["detail"], "tag": scn["tag"]},
        "permission": permission,
        "scores": peak["ev"]["scores"],
        "factors": peak["ev"]["factors"],
        "violations": peak["ev"]["violations"],
        "timeline_stats": stats,
        "policy": {"auto_limit": policy["auto_limit"],
                   "daily_limit": policy["daily_limit"]},
    }
    explanation = explain_result(ctx) if req.explain else None

    run_id = uuid.uuid4().hex[:12]
    result = {
        "run_id": run_id,
        "created_at": datetime.now().isoformat(),
        "scenario": {k: v for k, v in scn.items() if k != "builder"},
        "policy": policy,
        "policy_display": policy_display(policy),
        "permission": permission_view(permission),
        "permission_events": permission_events,
        "scores": peak["ev"]["scores"],
        "factors": peak["ev"]["factors"],
        "violations": peak["ev"]["violations"],
        "stats": stats,
        "steps": steps,
        "explanation": explanation,
        "restored": False,
        "llm": llm_status(),
    }
    STATE["runs"][run_id] = result
    return result


@app.get("/api/result/{run_id}")
def get_result(run_id: str):
    r = STATE["runs"].get(run_id)
    if not r:
        raise HTTPException(404, "결과를 찾을 수 없습니다.")
    return r


@app.get("/api/result")
def get_latest_result():
    if not STATE["runs"]:
        raise HTTPException(404, "아직 실행한 시뮬레이션이 없습니다.")
    return list(STATE["runs"].values())[-1]


# ==========================================================================
# 3) 권한 복원 — 시스템이 스스로 하지 않는다
# ==========================================================================
@app.post("/api/permission/restore")
def post_restore(req: RestoreRequest):
    r = STATE["runs"].get(req.run_id)
    if not r:
        raise HTTPException(404, "결과를 찾을 수 없습니다.")
    if not req.user_confirmed:
        raise HTTPException(
            400, "권한 복원에는 사용자 본인의 명시적 승인이 필요합니다.")
    if req.target not in PERMISSION_RANK:
        raise HTTPException(400, "알 수 없는 권한 등급입니다.")

    current = r["permission"]["permission"]
    if PERMISSION_RANK[req.target] >= PERMISSION_RANK[current]:
        raise HTTPException(400, "현재보다 넓은 권한만 복원할 수 있습니다.")

    r["permission"] = permission_view(req.target)
    r["restored"] = True
    r["permission_events"].append({
        "seq": None, "time": datetime.now().strftime("%H:%M:%S"),
        "from": current, "to": req.target, "risk": r["scores"]["total_risk"],
        "reason": "사용자 본인 승인에 의한 권한 복원",
    })
    return {"ok": True, "permission": r["permission"],
            "events": r["permission_events"]}


# ==========================================================================
# 4) 단건 평가 (외부 Agent 연동용)
# ==========================================================================
@app.post("/api/evaluate")
def post_evaluate(req: EvaluateRequest):
    engine = get_engine()
    policy = active_policy()
    now = datetime.now()
    action = {
        "ts": now.timestamp(),
        "amount": req.amount,
        "recipient_id": req.recipient_id,
        "recipient_name": req.recipient_name,
        "category": req.category,
        "action_type": req.action_type,
        "tool": req.tool,
        "status": "SUCCESS",
        "hour": req.hour if req.hour is not None else now.hour,
        "dow": now.weekday(),
        "balance_before": req.balance_before,
    }
    ev = engine.evaluate(action, req.history, policy)
    perm = ev["permission"]
    return {
        "scores": ev["scores"],
        "permission": permission_view(perm),
        "decision": enforce(perm, action),
        "violations": ev["violations"],
        "factors": ev["factors"],
    }


# ==========================================================================
# 정적 페이지
# ==========================================================================
@app.get("/")
def index():
    return FileResponse(WEB_DIR / "index.html")


app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
