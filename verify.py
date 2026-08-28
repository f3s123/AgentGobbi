# -*- coding: utf-8 -*-
"""7개 시나리오를 실행해 권한 판정이 의도대로 나오는지 대조한다."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "api"))

from main import post_simulate, active_policy          # noqa: E402
from schemas import SimulateRequest                    # noqa: E402
from scenarios import SCENARIOS                        # noqa: E402

BAR = "─" * 78


def main():
    p = active_policy()
    print("활성 위임정책: 건당 %s / 1일 %s / 신규수취인 %s" % (
        format(p["auto_limit"], ","), format(p["daily_limit"], ","),
        p["new_recipient"]["action"]))
    print(BAR)

    ok = 0
    for scn in SCENARIOS:
        r = post_simulate(SimulateRequest(scenario_id=scn["id"], explain=False))
        got = r["permission"]["permission"]
        exp = scn["expected"]
        mark = "O" if got == exp else "X"
        ok += got == exp
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


if __name__ == "__main__":
    main()
