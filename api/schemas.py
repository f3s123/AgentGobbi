# -*- coding: utf-8 -*-
"""API 요청/응답 스키마."""
from typing import Any, Dict, List, Optional, Literal, Annotated

from pydantic import BaseModel, Field, ConfigDict, model_validator, field_validator
from config import ACTION_TO_TOOL, CATEGORY_LABEL

class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False, str_max_length=2000)

Short = Annotated[str, Field(min_length=1, max_length=128)]
Money = Annotated[float, Field(ge=0, le=1_000_000_000, allow_inf_nan=False)]
Permission = Literal['AUTO', 'VERIFY', 'READ_ONLY', 'STOP']


class PolicyCompileRequest(StrictModel):
    text: str = Field(..., min_length=1, max_length=2000,
                      description="자연어로 쓴 위임정책")


class PolicyPreviewRequest(StrictModel):
    policy: Dict[str, Any]

    @field_validator('policy')
    @classmethod
    def validate_policy(cls, p):
        required = {'auto_limit': 100_000_000, 'daily_limit': 1_000_000_000, 'valid_days': 365}
        for key, maximum in required.items():
            value = p.get(key)
            if type(value) is not int or not (1 if key == 'valid_days' else 0) <= value <= maximum:
                raise ValueError('invalid policy number')
        if p['daily_limit'] < p['auto_limit']:
            raise ValueError('daily limit below auto limit')
        for key, allowed, limit in [('allowed_actions', ACTION_TO_TOOL, 8),
                                    ('blocked_categories', CATEGORY_LABEL, 30)]:
            values = p.get(key, [])
            if not isinstance(values, list) or len(values) > limit or any(not isinstance(x, str) or x not in allowed for x in values):
                raise ValueError('invalid policy list')
        for key in ('new_recipient', 'time_window'):
            if p.get(key) is not None and not isinstance(p[key], dict):
                raise ValueError('invalid nested policy')
        nr = p.get('new_recipient') or {}
        action = p.get('new_recipient_action', nr.get('action', 'VERIFY'))
        threshold = p.get('new_recipient_threshold', nr.get('amount_threshold', 0))
        if action not in ('AUTO', 'VERIFY', 'BLOCK') or type(threshold) is not int or not 0 <= threshold <= 100_000_000:
            raise ValueError('invalid recipient rule')
        window = p.get('time_window') or {}
        s, e = p.get('time_window_start', window.get('start')), p.get('time_window_end', window.get('end'))
        if (s is None) != (e is None) or (s is not None and (type(s) is not int or type(e) is not int or not 0 <= s <= 23 or not 0 <= e <= 23 or s == e)):
            raise ValueError('invalid time window')
        if not isinstance(p.get('assumptions', []), list) or len(p.get('assumptions', [])) > 6:
            raise ValueError('invalid assumptions')
        if any(not isinstance(x, str) or len(x) > 160 for x in p.get('assumptions', [])):
            raise ValueError('invalid assumption text')
        # Strip metadata and unknown properties; never trust client provenance or tool lists.
        keys = set(required) | {'allowed_actions', 'blocked_categories', 'new_recipient',
            'new_recipient_action', 'new_recipient_threshold', 'time_window', 'time_window_start',
            'time_window_end', 'verify_channel', 'on_anomaly'}
        return {k: v for k, v in p.items() if k in keys}


class PolicyApproveRequest(StrictModel):
    draft_id: Short
    approval_token: Short


class SimulateRequest(StrictModel):
    scenario_id: Short
    explain: bool = True


class RestoreRequest(StrictModel):
    run_id: Short
    target: Permission = 'AUTO'
    approval_token: Short


class EvaluateRequest(StrictModel):
    """단건 평가 (외부 Agent 연동용 엔드포인트)."""
    request_id: Short
    amount: Money = 0
    recipient_id: Short = 'R-UNKNOWN'
    recipient_name: Short = '미확인 수취인'
    category: Short = 'ETC'
    action_type: Short = 'TRANSFER'
    tool: Short | None = None

    @model_validator(mode='after')
    def consistent(self):
        if self.category not in CATEGORY_LABEL or self.action_type not in ACTION_TO_TOOL:
            raise ValueError('unknown action/category')
        expected = ACTION_TO_TOOL[self.action_type]
        if self.tool is not None and self.tool != expected:
            raise ValueError('action/tool mismatch')
        self.tool = expected
        if self.action_type in ('BALANCE_READ', 'HISTORY_READ') and self.amount != 0:
            raise ValueError('read actions must have zero amount')
        return self
