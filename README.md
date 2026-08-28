# 에이전트고삐

**금융 AI Agent를 위한 개인 맞춤형 동적 위임 안전장치**
_"AI에게 맡기되, 통제는 내가."_

금융 AI Agent에게 부여한 권한을 고정된 값으로 두지 않고, 사용자가 설정한 위임정책과
Agent의 실시간 행동 위험도를 함께 분석해 자율 권한을 **AUTO / VERIFY / READ ONLY / STOP**
4단계로 동적 조정하는 권한통제 계층입니다.

---

## 1. 빠른 실행

> **전용 가상환경에 설치하세요.** 이 프로젝트는 pandas 3.x / numpy 2.4 를 요구합니다.
> conda `base` 처럼 다른 패키지가 함께 사는 공용 환경에 설치하면
> numba(`numpy<2.4`), streamlit(`pandas<3`) 같은 기존 패키지가 깨집니다.

```bash
python -m venv .venv
```

```bash
.venv\Scripts\activate
```

```bash
pip install -r requirements.txt
```

```bash
cd api && python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

브라우저에서 <http://127.0.0.1:8000> 접속.

모델 파일(`ml/models/*.pkl`)과 데이터셋이 저장소에 포함되어 있어 바로 실행됩니다.
데이터셋과 모델을 처음부터 다시 만들려면:

```bash
cd ml && python gen_paysim.py && python build_user_profile.py && python gen_bench.py && python train_lightgbm.py && python train_isolation_forest.py
```

7개 시나리오의 권한 판정을 한 번에 검증하려면:

```bash
python verify.py
```

---

## 2. 화면 구성

| 페이지 | 경로 | 내용 |
|---|---|---|
| 위임정책 설정 | `/index.html` | 자연어로 위임 조건 입력 → 생성형 AI가 구조화 → **사용자가 변환 결과를 확인한 뒤 승인** |
| 시뮬레이션 | `/simulate.html` | 시나리오 7종 중 선택 → 시뮬레이션 시작 |
| 결과 | `/result.html` | 상단 행위 요약 / 좌하단 권한 · 위험도 점수 / 우하단 자세한 설명 · 분석 항목 8가지 · 행동 타임라인 |

---

## 3. 시스템 구조

```
사용자
  ↓  자연어 위임정책
금융 AI Agent
  ↓  거래 요청 (송금 / 결제 / 조회)
┌──────────────── 에이전트고삐 ────────────────┐
│  ① LightGBM        행동 시퀀스 위험도  0~100  │
│  ② IsolationForest 개인 행동 이탈도    0~100  │
│  ③ Policy Engine   위임정책 위반       0~100  │
│         ↓ 0.5 : 0.3 : 0.2 로 합산             │
│     Delegation Risk Score → 권한 등급 결정    │
│  ④ 생성형 AI       사용자용 설명 생성         │
└───────────────────────────────────────────────┘
  ↓  AUTO(실행) / VERIFY(승인 후) / READ ONLY(차단) / STOP(전면 중단)
은행 · 카드 · 결제 API
```

### 권한 등급

| 등급 | 조회 | 송금·결제 | 위험도 구간 |
|---|---|---|---|
| `AUTO` | 가능 | 자동 실행 | 0 – 29 |
| `VERIFY` | 가능 | 본인 승인 후 실행 | 30 – 54 |
| `READ ONLY` | 가능 | 금지 (실행권한 일시 회수) | 55 – 77 |
| `STOP` | 금지 | 금지 (Kill Switch) | 78 – 100 |

**권한 축소는 자동, 복원은 사용자 승인 필수.**
세션 안에서 권한은 제한이 강해지는 방향으로만 움직입니다(Ratchet).
되돌리려면 `POST /api/permission/restore` 에 `user_confirmed: true` 가 있어야 하고,
현재보다 넓은 등급으로만 복원할 수 있습니다.

정책 위반은 점수와 별개로 **최소 강제 권한**을 갖습니다.
예를 들어 위임하지 않은 Tool 호출은 위험도가 20점이어도 즉시 READ ONLY로 내려갑니다.

---

## 4. 탐지 시스템

### 4-1. LightGBM — 행동 시퀀스 위험도

"이 행동 **흐름**이 위험한가"를 판정합니다. 데이터셋 2종을 같은 피처 공간에서 함께 학습합니다.

| 데이터셋 | 규모 | 라벨 |
|---|---|---|
| `data/paysim.csv` | 274,204건 (사기 0.129%) | `isFraud` |
| `data/findelegation_bench.csv` | 51,961행 / 3,400세션 | `risk_label` |

**PaySim** — 원본이 에이전트 기반 시뮬레이터이므로, 동일한 스키마·거래유형 비율
(CASH_OUT 35.2 / PAYMENT 33.8 / CASH_IN 21.9 / TRANSFER 8.4 / DEBIT 0.7 %)·
사기 시나리오(계좌 탈취 후 전액 이체 → 즉시 인출)를 재현하는 시뮬레이터를 구현했습니다
(`ml/gen_paysim.py`). 원본과 달리 고객 에이전트를 재사용해 한 고객이 여러 건을 만들도록 했고,
step 내부에 분 단위 타임스탬프를 부여해 시퀀스 피처를 뽑을 수 있게 했습니다.

**FinDelegationBench** — 공개 데이터에는 'AI Agent의 연속 행동 + 사용자 위임정책'을 함께 담은 것이 없어
직접 만들었습니다(`ml/gen_bench.py`). PaySim에서 추출한 금액 분포를 토대로
가상 사용자 Baseline + 위임정책 + Agent 행동 시퀀스를 결합하고, 위험 시나리오 9종을 생성합니다.

> Limit Ratcheting · Velocity Attack · Recipient Burst · Cumulative Bypass · Retry Pattern
> · Category Drift · Boundary Probing · Unauthorized Tool · Combined

**성능** (세션 단위 25% 홀드아웃)

```
ROC-AUC 0.9998   PR-AUC 0.9988   정상 행동 오탐율 0.2%

BOUNDARY_PROBING  0.991    RECIPIENT_BURST   1.000
CATEGORY_DRIFT    1.000    RETRY_PATTERN     1.000
COMBINED          0.960    UNAUTHORIZED_TOOL 0.885
CUMULATIVE_BYPASS 0.970    VELOCITY_ATTACK   0.989
LIMIT_RATCHETING  1.000    PAYSIM_FRAUD      1.000
```

### 4-2. IsolationForest — 개인 금융행동 이탈도

"평소 **이 사용자가** 하는 행동인가"만 판정합니다. 사기 여부는 보지 않습니다.
사용자 본인의 거래내역만으로 One-Class 학습합니다.

원본 데이터는 농협 입출금거래내역 1개월 59건입니다. 그대로는 학습이 불가능하므로
`ml/build_user_profile.py` 가 두 가지를 수행합니다.

1. **수취인 라벨링 재작업** — 원본의 `거래기록사항`은 가맹점명·이체메모·사람이름이 섞여 있어
   수취인 식별자로 쓸 수 없습니다. 정규화 사전 + 키워드 규칙으로
   `(recipient_id, recipient_name, category, recipient_type)` 4개 라벨을 새로 부여합니다.
   지점명이 다른 편의점·역사는 같은 수취인으로 묶고, 가승인 취소쌍은 제거합니다.
2. **12개월 증강** — 수취인별 월 발생빈도·금액 로그정규·시간대 분포를 추정해
   2025-09 ~ 2026-08 구간을 재생성합니다. 고정 지출(월세·구독·통신비·청약)은 실제 주기대로
   결정적으로 배치하고, 실제 2026-07 데이터는 원본 그대로 보존합니다.

결과: **615건 / 수취인 34명 / 카테고리 14종**, 평균 거래금액 37,055원, 일평균 2.17건.

**점수 보정** — 백분위를 그대로 점수로 쓰면 사용자 본인의 정상 거래 10%가 90점을 넘어 오탐이 됩니다.
학습 분포 전체를 0~60 구간에 눌러 담고, 학습 중 한 번도 본 적 없는 영역에만 60~100을 배정합니다.

```
평소 거래 (카카오페이 1.2만원, 20시)      12.0점
월세 60.5만원 (등록 수취인, 정기)         45.6점
신규 계좌 49만원 송금 (14시)              81.2점
신규 계좌 49만원 송금 (새벽 3시)          89.1점
해외송금 320만원 (새벽 3시, 신규)         97.8점
```

### 4-3. 피처 설계

두 모델이 `ml/features.py` 한 곳을 공유해 학습과 추론이 어긋나지 않게 했습니다.

- `SEQ_FEATURES` 23개 (LightGBM) — 거래 특성 / 시퀀스 특성 / 개인화 특성
- `PERSONAL_FEATURES` 11개 (IsolationForest)

> **SEQ_FEATURES 는 전부 스케일 free 입니다.**
> 학습은 PaySim 스케일의 가상 사용자로 하고 추론은 원화 실사용자로 하기 때문에,
> 절대 금액(`log1p(amount)` 등)을 넣으면 학습된 분기 임계값이 전이되지 않습니다.
> 금액은 반드시 개인 Baseline · 위임한도 · 잔액에 대한 **비율**로만 넣습니다.

---

## 5. 생성형 AI

`ANTHROPIC_API_KEY` 가 설정되어 있으면 **Claude(`claude-opus-5`)** 를 호출하고,
없으면 같은 입력으로 규칙 기반 생성기가 한국어 결과를 만듭니다.
따라서 API 키 없이도 MVP 전체가 완전히 동작합니다.

**두 경로 모두 수치는 탐지 엔진이 계산한 값을 그대로 씁니다. 생성형 AI는 숫자를 만들지 않고 서술만 담당합니다.**

### 5-1. Natural Language Policy Compiler

```
입력  "해외송금이랑 상품권 결제는 절대 하지 마. 나머지는 20만원까지 알아서 해."

출력  등록 수취인 자동송금 한도  200,000원
      1일 누적 한도            1,500,000원
      신규 수취인              본인 승인 필요
      차단 거래 유형           상품권 · 해외송금
      ...
      말씀하지 않으신 부분은 이렇게 채웠습니다
        · 1일 누적한도는 지정하지 않아 기본값을 적용했습니다.
        · 위임 유효기간은 지정하지 않아 기본값을 적용했습니다.
```

생성형 AI의 자유형식 출력을 그대로 정책으로 쓰지 않습니다.
`sanitize_policy()` 를 통과한 값만 Policy Engine 입력이 됩니다 — enum 화이트리스트,
금액 범위 clamp, `daily_limit < auto_limit` 같은 논리 모순 보정, 조회 권한 강제 포함.

### 5-2. Risk Explanation Writer

탐지 결과를 근거로 사용자용 설명(headline / summary / detail / recommendation)을 작성합니다.
분석 항목 8가지를 그대로 근거로 씁니다.

1. 현재 거래금액 · 2. 신규 수취인 여부 · 3. 최근 일정 시간 동안 거래 횟수 · 4. 최근 누적 거래금액
5. 거래 간 시간 간격 · 6. 자동송금 한도 근접 반복 여부 · 7. 실패 및 재시도 횟수 · 8. 평소 금융행동과의 차이

---

## 6. 시뮬레이션 시나리오

실제 AI Agent를 연결할 수 없으므로, Agent가 보낼 법한 거래 요청 시퀀스를 정의해
위험평가 엔진에 흘려보냅니다. 금액·수취인·시간대는 실제 사용자 Baseline에 맞췄습니다.

| 시나리오 | 도달 권한 | 내용 |
|---|---|---|
| 평상시 하루 | `AUTO` | 교통비·간편결제·지인 정산. 위험도 7점 |
| 한도 직전 금액 반복 송금 | `AUTO → VERIFY → READ ONLY → STOP` | 50만 한도를 지키며 49만원씩 신규계좌 5곳에 반복 |
| 심야 신규 수취인 연속 송금 | `AUTO → READ ONLY → STOP` | 새벽에 처음 보는 계좌 7곳으로 연속 송금 |
| 누적 한도 우회 | `AUTO → VERIFY` | 건당 한도는 지키며 하루 누적 200만원 초과 |
| 실패 후 금액 낮춰 재시도 | `AUTO → VERIFY → STOP` | 거절되자 금액을 낮춰가며 한도 경계 탐색 |
| 잔액 전액 이체 | `AUTO → READ ONLY → STOP` | PaySim 사기와 동일 구조의 계좌 비우기 |
| 위임하지 않은 기능 호출 | `AUTO → READ ONLY → STOP` | 이체한도 변경 → 상품권 대량결제 → 해외송금 |

대표 사례인 **한도 직전 금액 반복 송금**의 실제 실행 결과:

```
#1 14:05 잔액 조회      -           위험도  0  AUTO       실행
#2 14:10 결제 실행      15,200원    위험도  8  AUTO       실행
#3 14:35 송금 신규계좌A 490,000원   위험도 48  VERIFY     승인 대기
#4 14:51 송금 신규계좌B 487,000원   위험도 77  READ_ONLY  차단
#5 15:04 송금 신규계좌C 492,000원   위험도 78  READ_ONLY  차단
#6 15:18 송금 신규계좌D 485,000원   위험도 87  STOP       차단
#7 15:30 송금 신규계좌E 495,000원   위험도 88  STOP       차단

요청 2,464,200원 중 실행 15,200원 / 차단·보류 2,449,000원
```

개별 거래는 모두 50만원 한도 이내여서 정적 규칙으로는 전부 통과합니다.
에이전트고삐는 흐름 전체를 보고 3번째 거래에서 승인 절차를 요구하고, 6번째에서 실행권한을 끊습니다.

---

## 7. API

| 메서드 | 경로 | 설명 |
|---|---|---|
| `GET` | `/api/health` | 상태 + 생성형 AI 모드 |
| `GET` | `/api/models` | 모델 성능 · 사용자 Baseline 요약 |
| `GET` | `/api/policy` | 현재 적용 중인 위임정책 |
| `POST` | `/api/policy/compile` | 자연어 → 구조화 정책 (미리보기, 아직 적용 안 함) |
| `POST` | `/api/policy/approve` | 사용자 확인 후 정책 활성화 |
| `GET` | `/api/scenarios` | 시나리오 목록 |
| `POST` | `/api/simulate` | 시뮬레이션 실행 |
| `GET` | `/api/result/{run_id}` | 결과 조회 |
| `POST` | `/api/permission/restore` | 권한 복원 (`user_confirmed: true` 필수) |
| `POST` | `/api/evaluate` | 단건 평가 — 외부 Agent 연동용 |

`/api/evaluate` 로 연동할 때는 **직전 행동 이력을 `history` 로 함께 넘겨야** 시퀀스 위험도가 산출됩니다.
이력 없이 1건만 보내면 시퀀스 축은 0이 되고 개인 이탈도와 정책 검증만 반영됩니다
(단건 판정만으로는 연속 이상행동을 잡을 수 없다는 것이 이 서비스의 출발점입니다).

---

## 8. 프로젝트 구조

```
제작/
├── data/
│   ├── raw/nh_transactions.xlsx      농협 입출금거래내역 원본 (59건)
│   ├── paysim.csv                    PaySim 시뮬레이션 결과 (274,204건)
│   ├── paysim_profile.json           금액 분포 통계
│   ├── findelegation_bench.csv       자체 위험행동 데이터셋 (51,961행)
│   ├── user_transactions.csv         수취인 라벨링 + 12개월 증강 (615건)
│   └── user_baseline.json            개인 금융행동 Baseline
├── ml/
│   ├── features.py                   공용 피처 (학습·추론 단일 소스)
│   ├── scoring_util.py               이탈도 점수 앵커 매핑
│   ├── gen_paysim.py                 PaySim 시뮬레이터
│   ├── build_user_profile.py         수취인 라벨링 + 증강
│   ├── gen_bench.py                  FinDelegationBench 생성
│   ├── train_lightgbm.py             시퀀스 위험도 학습
│   ├── train_isolation_forest.py     개인 이탈도 학습
│   └── models/                       학습된 모델
├── api/
│   ├── config.py                     가중치 · 임계값 · 권한 정의
│   ├── policy_engine.py              정책 검증 · 권한 결정 · Ratchet
│   ├── engine.py                     두 모델 결합 + 분석 항목 8가지
│   ├── llm.py                        정책 컴파일러 + 설명 생성기
│   ├── scenarios.py                  시뮬레이션 시나리오 7종
│   ├── schemas.py                    요청·응답 스키마
│   └── main.py                       FastAPI 앱
├── web/
│   ├── index.html                    위임정책 설정
│   ├── simulate.html                 시뮬레이션
│   ├── result.html                   결과
│   └── static/app.css, app.js
├── verify.py                         시나리오 7종 권한 판정 검증
└── requirements.txt
```

---

## 9. MVP 단계의 제한사항

- 실제 금융 API·AI Agent와 연결되어 있지 않습니다. 시나리오 시뮬레이션으로 동작을 재현합니다.
- 사용자 Baseline은 1인분(농협 입출금내역 1개월 → 12개월 증강)입니다. 다중 사용자는 미지원입니다.
- 위임정책과 시뮬레이션 결과는 서버 메모리에 저장되며 재시작 시 초기화됩니다.
- FinDelegationBench와 PaySim은 모두 합성 데이터입니다. 실계좌 데이터로 재학습이 필요합니다.
- `ANTHROPIC_API_KEY` 미설정 시 생성형 AI 두 기능은 규칙 기반 경로로 동작합니다.
- VERIFY 단계의 실제 본인인증(생체·PIN) 연동은 구현하지 않았습니다. 승인 여부만 상태로 다룹니다.
