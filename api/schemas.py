# -*- coding: utf-8 -*-
"""API 요청/응답 스키마."""
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class PolicyCompileRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000,
                      description="자연어로 쓴 위임정책")


class PolicyApproveRequest(BaseModel):
    policy: Dict[str, Any]


class SimulateRequest(BaseModel):
    scenario_id: str
    explain: bool = True


class RestoreRequest(BaseModel):
    run_id: str
    target: str = Field("AUTO", description="복원할 권한 등급")
    user_confirmed: bool = False


class EvaluateRequest(BaseModel):
    """단건 평가 (외부 Agent 연동용 엔드포인트)."""
    amount: float = 0
    recipient_id: str = "R-UNKNOWN"
    recipient_name: str = "미확인 수취인"
    category: str = "ETC"
    action_type: str = "TRANSFER"
    tool: Optional[str] = None
    hour: Optional[int] = None
    balance_before: Optional[float] = None
    history: List[Dict[str, Any]] = []
