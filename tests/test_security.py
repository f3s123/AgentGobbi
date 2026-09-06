import concurrent.futures
import os
import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import main
from policy_engine import check_policy, enforce
from schemas import EvaluateRequest
from settings import Settings
from store import Store, audit, digest


@pytest.fixture
def service(tmp_path, monkeypatch):
    store = Store("sqlite:///" + (tmp_path / "state.db").as_posix())
    store.initialize()
    monkeypatch.setattr(main, "store", store)
    main.app.state.store = store
    main.app.middleware_stack = None
    for item in main.app.user_middleware:
        if item.cls.__name__ == "SecurityMiddleware":
            monkeypatch.setitem(item.kwargs, "store", store)
    client = TestClient(main.app, headers={"Origin": "http://testserver"},
                        raise_server_exceptions=False)
    yield client, store
    client.close()
    store.engine.dispose()


def draft(client):
    response = client.post("/api/policy/compile", json={"text": "건당 10만원 하루 30만원"})
    assert response.status_code == 200, response.text
    return response.json()


def test_no_login_routes_and_same_origin_protection(service):
    client, _ = service
    assert client.get("/api/auth/me").status_code == 404
    assert client.post("/api/auth/login", json={}).status_code in (404, 405)
    assert client.get("/api/policy").status_code == 200
    assert client.post("/api/policy/compile", json={"text": "test"},
                       headers={"Origin": "https://evil.example"}).status_code == 403
    local = TestClient(main.app, headers={"Origin": "http://localhost:8000",
                                          "Host": "localhost:8000"},
                       raise_server_exceptions=False)
    try:
        assert local.post("/api/policy/compile", json={"text": "test"}).status_code == 200
    finally:
        local.close()


def test_policy_grant_is_bound_single_use_and_expires(service):
    client, store = service
    item = draft(client)
    payload = {"draft_id": item["draft_id"], "approval_token": item["approval_token"]}
    assert client.post("/api/policy/approve", json=payload).status_code == 200
    assert client.post("/api/policy/approve", json=payload).status_code == 403

    item = draft(client)
    with store.transaction() as conn:
        state = store.load(conn, "service")
        state["grants"][digest(item["approval_token"])]["expires"] = time.time() - 1
        store.save(conn, "service", state)
    assert client.post("/api/policy/approve", json={
        "draft_id": item["draft_id"], "approval_token": item["approval_token"]
    }).status_code == 403


def test_restore_grant_is_not_exposed_by_get_and_is_target_bound(service):
    client, _ = service
    response = client.post("/api/simulate", json={"scenario_id": "limit_ratcheting", "explain": False})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result.get("restore_approval_token")
    assert "restore_approval_token" not in client.get("/api/result/" + result["run_id"]).json()
    wrong = {"run_id": result["run_id"], "target": "VERIFY",
             "approval_token": result["restore_approval_token"]}
    assert client.post("/api/permission/restore", json=wrong).status_code == 403
    right = {**wrong, "target": "AUTO"}
    assert client.post("/api/permission/restore", json=right).status_code == 200
    assert client.post("/api/permission/restore", json=right).status_code == 403


@pytest.mark.parametrize("change", [
    {"amount": -1}, {"amount": float("inf")}, {"amount": float("nan")},
    {"hour": 12}, {"history": [{}]}, {"balance_before": 1000},
    {"recipient_name": "x" * 129}, {"action_type": "UNKNOWN"},
    {"tool": "balance.read"}, {"category": "UNKNOWN"},
])
def test_invalid_evaluation(change):
    with pytest.raises(ValueError):
        EvaluateRequest.model_validate({"request_id": "test", **change})


def test_agent_key_and_production_fail_closed(service, monkeypatch):
    client, _ = service
    assert client.post("/api/evaluate", json={"request_id": "one", "amount": 1}).status_code == 401
    monkeypatch.setattr(main.app.state, "settings",
                        replace(main.settings, agent_api_key="a" * 32))
    assert client.post("/api/evaluate", json={"request_id": "one", "amount": 1},
                       headers={"X-Agent-API-Key": "wrong"}).status_code == 401
    response = client.post("/api/evaluate", json={"request_id": "one", "amount": 1},
                           headers={"X-Agent-API-Key": "a" * 32})
    assert response.status_code == 200, response.text

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("APP_ORIGIN", "https://app.example.com")
    monkeypatch.setenv("DATABASE_URL",
                       "postgresql+psycopg://u:p@db/app?sslmode=verify-full&sslrootcert=/cert.pem")
    monkeypatch.delenv("AGENT_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        Settings.from_env()


def test_headers_limits_and_private_files(service):
    client, _ = service
    assert client.post("/api/policy/compile", content="x" * 70000,
                       headers={"Content-Type": "application/json"}).status_code == 413
    response = client.get("/")
    assert "script-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
    for path in ("/data/user_baseline.json", "/ml/models/lgbm_sequence.pkl", "/.env", "/docs"):
        assert client.get(path).status_code == 404
    assert "baseline" not in client.get("/api/models").json()


def test_concurrent_approval_and_audit(service):
    client, store = service
    item = draft(client)
    payload = {"draft_id": item["draft_id"], "approval_token": item["approval_token"]}

    def send(_):
        local = TestClient(main.app, headers={"Origin": "http://testserver"},
                           raise_server_exceptions=False)
        try:
            return local.post("/api/policy/approve", json=payload).status_code
        finally:
            local.close()

    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(send, range(2))) == [200, 403]
    with store.transaction() as conn:
        records = conn.execute(select(audit)).mappings().all()
        assert item["approval_token"] not in str(records)


def test_policy_fail_closed_edges(service):
    client, _ = service
    policy = client.get("/api/policy").json()["policy"]
    action = {"amount": 1, "action_type": "TRANSFER", "tool": "transfer.execute", "hour": 12}
    _, violations, _ = check_policy(action, {**policy, "auto_limit": 0, "daily_limit": 0}, 1)
    assert {"AUTO_LIMIT_EXCEEDED", "DAILY_LIMIT_EXCEEDED"} <= {v["code"] for v in violations}
    assert check_policy(action, {**policy, "valid_until": "invalid"}, 1)[2] == "STOP"
    assert enforce("READ_ONLY", {**action, "tool": "balance.read"}) == "BLOCKED"
