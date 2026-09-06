# -*- coding: utf-8 -*-
"""현재 시나리오의 안전성 기준을 검증한다. 모델·데이터는 수정하지 않는다."""
import sys
from pathlib import Path
import tempfile
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent / "api"))

from main import post_simulate, active_policy          # noqa: E402
from schemas import SimulateRequest                    # noqa: E402
from scenarios import SCENARIOS                        # noqa: E402
from store import Store
from security import Context
from config import PERMISSION_RANK
from datetime import datetime
import main as api_main
from scenarios import build_actions

# Fixed date avoids weekday-dependent model drift in regression runs.
api_main.build_actions = lambda scenario_id, policy: build_actions(scenario_id, policy, datetime(2026, 9, 5))
EXPECTED = {'normal_daily': 'AUTO', 'normal_large_transfer': 'READ_ONLY', 'payday_burst': 'VERIFY',
            'limit_ratcheting': 'STOP', 'recipient_burst_night': 'STOP',
            'cumulative_bypass': 'READ_ONLY', 'retry_probing': 'STOP',
            'account_drain': 'STOP', 'unauthorized_tool': 'STOP'}

BAR = "─" * 78


def verify(ctx):
    p = active_policy(ctx)
    print("활성 위임정책: 건당 %s / 1일 %s / 신규수취인 %s" % (
        format(p["auto_limit"], ","), format(p["daily_limit"], ","),
        p["new_recipient"]["action"]))
    print(BAR)

    ok = 0
    for scn in SCENARIOS:
        r = post_simulate(SimulateRequest(scenario_id=scn["id"], explain=False), ctx)
        got = r["permission"]["permission"]
        exp = EXPECTED[scn['id']]
        safe = got == exp
        ranks = [PERMISSION_RANK[step['permission']] for step in r['steps']]
        safe = safe and ranks == sorted(ranks)
        safe = safe and all(step['outcome'] != 'EXECUTED' for step in r['steps']
                            if step['amount'] > 0 and step['permission'] != 'AUTO')
        mark = 'O' if safe else 'X'
        ok += safe
        s = r["scores"]
        st = r["stats"]
        print("%s  %-22s 기대 %-9s 실제 %-9s  위험도 %5.1f "
              "(seq %5.1f / 이탈 %5.1f / 정책 %5.1f)"
              % (mark, scn["title"], exp, got, s["total_risk"],
                 s["sequence_risk"], s["personal_deviation"], s["policy_risk"]))
        print("      요청 %d건 %s → 실행 %d / 승인대기 %d / 차단 %d / 은행거절 %d"
              % (st["total_actions"], format(int(st["requested_amount"]), ",") + "원",
                 st["executed"], st["pending"], st["blocked"], st["rejected"]))
        for e in r["permission_events"]:
            print("      #%s %s  %s → %s  (위험도 %.1f) %s"
                  % (e["seq"], e["time"], e["from"], e["to"], e["risk"], e["reason"]))
        print(BAR)

    print("일치 %d / %d" % (ok, len(SCENARIOS)))
    return 0 if ok == len(SCENARIOS) else 1


def main():
    # Isolated state; regression checks never alter a real user's policy/results.
    with tempfile.TemporaryDirectory() as directory:
        store = Store('sqlite:///' + (Path(directory) / 'verify.db').as_posix())
        store.initialize()
        try:
            with store.transaction() as conn:
                ctx = Context('service', 'browser', '', store.load(conn, 'service'), conn, store,
                              SimpleNamespace(result_ttl=86400, max_runs=50))
                result = verify(ctx)
        finally:
            store.engine.dispose()
    return result


if __name__ == "__main__":
    sys.exit(main())
