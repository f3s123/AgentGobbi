# -*- coding: utf-8 -*-
"""점수 변환 유틸 (학습 · 추론 공용)."""
import numpy as np

# 학습 분포의 백분위 -> 이탈도 점수 앵커
#   사용자 '본인의 정상 거래'는 낮은 점수대에 머물러야 한다.
#   단순 백분위를 쓰면 정상 거래의 10%가 90점을 넘어 오탐이 된다.
#   학습 분포 전체를 0~60 구간에 눌러 담고, 학습 중 한 번도 본 적 없는
#   영역(분포 최대치 초과)에만 60~100 구간을 배정한다.
ANCHORS = [
    (0.0, 0.0),      # 학습 분포 최솟값
    (50.0, 10.0),
    (75.0, 20.0),
    (90.0, 32.0),
    (97.0, 45.0),
    (99.0, 54.0),
    (100.0, 60.0),   # 학습 분포 최댓값 = 평소 행동 중 가장 낯선 것
]
TAIL_SPAN = 0.35     # (max-min) 의 몇 배까지 벗어나면 100점으로 볼지


def build_anchors(ref_sorted):
    """학습 이상점수 분포에서 (raw_score, mapped_score) 앵커를 만든다."""
    ref = np.asarray(ref_sorted, dtype=float)
    xs = [float(np.percentile(ref, p)) for p, _ in ANCHORS]
    ys = [y for _, y in ANCHORS]
    # 백분위가 같아 x 가 단조증가하지 않는 경우 보정
    for i in range(1, len(xs)):
        if xs[i] <= xs[i - 1]:
            xs[i] = xs[i - 1] + 1e-9
    return np.array(xs), np.array(ys), float(ref[0]), float(ref[-1])


def percentile_score(score, ref_sorted, tail_span=TAIL_SPAN):
    """IsolationForest 이상점수(클수록 낯섦)를 0~100 이탈도로 변환."""
    xs, ys, lo, hi = build_anchors(ref_sorted)
    s = float(score)

    if s <= lo:
        return 0.0
    if s <= hi:
        return round(float(np.interp(s, xs, ys)), 1)

    span = max((hi - lo) * tail_span, 1e-9)
    over = min((s - hi) / span, 1.0)
    return round(60.0 + over * 40.0, 1)
