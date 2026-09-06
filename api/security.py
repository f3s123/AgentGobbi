"""Single-service state access, origin checks, API-key scope, and one-time grants."""
from dataclasses import dataclass
import hmac
import time
from urllib.parse import urlparse

from fastapi import HTTPException, Request

from store import canonical, digest


def origin_allowed(request, origin):
    """Require the configured origin in production; allow the actual local origin in development."""
    if origin == request.app.state.settings.origin:
        return True
    if request.app.state.settings.production or not origin:
        return False
    parsed = urlparse(origin)
    host = request.headers.get("host", "").lower()
    return (parsed.scheme in ("http", "https") and parsed.netloc.lower() == host
            and parsed.hostname in ("localhost", "127.0.0.1", "::1"))


@dataclass
class Context:
    owner: str
    kind: str
    token: str
    state: dict
    conn: object
    store: object
    settings: object


def context(request: Request):
    store, settings = request.app.state.store, request.app.state.settings
    path = request.url.path
    kind = "browser"

    if path == "/api/evaluate":
        supplied = request.headers.get("x-agent-api-key", "")
        if not settings.agent_api_key or not hmac.compare_digest(supplied, settings.agent_api_key):
            raise HTTPException(401, "Agent API 인증이 필요합니다.")
        kind = "agent"
    elif request.method not in ("GET", "HEAD"):
        # 로그인 없는 단일 서비스이므로 브라우저 변경 요청은 동일 출처만 허용한다.
        # 실제 접근 주체 제한은 AWS WAF/ALB 정책에서 적용해야 한다.
        if not origin_allowed(request, request.headers.get("origin")):
            raise HTTPException(403, "요청 출처를 확인할 수 없습니다.")

    rate_key = "agent" if kind == "agent" else "browser"
    if not store.rate("service:" + rate_key, 120):
        raise HTTPException(429, "요청이 너무 많습니다.", headers={"Retry-After": "60"})
    if path in ("/api/policy/compile", "/api/simulate") and not store.rate("expensive", 10):
        raise HTTPException(429, "분석 요청 한도를 초과했습니다.")

    with store.transaction() as conn:
        state = store.load(conn, "service")
        now = time.time()
        state["runs"] = {k: v for k, v in state["runs"].items() if v.get("expires_at", 0) > now}
        for field in ("drafts", "grants"):
            state[field] = {k: v for k, v in state[field].items() if v["expires"] > now}
        state["evaluation"]["requests"] = {
            k: v for k, v in state["evaluation"]["requests"].items() if v.get("expires", 0) > now
        }
        ctx = Context("service", kind, "", state, conn, store, settings)
        request.state.owner = "service"
        yield ctx
        store.save(conn, "service", state)


def consume_grant(ctx, token, operation, payload):
    grant = ctx.state["grants"].pop(digest(token), None)
    if (not grant or grant.get("operation") != operation
            or grant.get("payload") != digest(canonical(payload))
            or grant.get("revision") != ctx.state["revision"]
            or grant.get("expires", 0) <= time.time()):
        raise HTTPException(403, "승인이 만료되었거나 대상이 변경되었습니다. 다시 확인해 주세요.")
    ctx.state["revision"] += 1
