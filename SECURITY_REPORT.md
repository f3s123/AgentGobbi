# AgentGobbi 보안 개선 보고서

작성일: 2026-09-06  
기준: OWASP Top 10:2025  
배포 전제: AWS ALB + ECS Fargate + RDS PostgreSQL, 로그인 기능 없음

## 범위

`api/`, `web/`, 배포 설정, 의존성 관리, 테스트와 운영 문서를 수정했다. 사용자의 요청에 따라 `data/`의 데이터셋 내용, 기존 `ml/models/*.pkl`, Git 이력은 수정하지 않았고 모델 재학습도 하지 않았다.

로그인이 없으므로 웹 사용자는 구분하지 않으며 정책·결과·권한 상태 하나를 서비스 전체가 공유한다. 이 구조에서 `Origin` 검사는 사용자 인증이 아니다. AWS의 `AllowedClientCidr`로 서비스 접근망을 제한하는 것이 필수 배포 조건이다.

## 변경 내역

| 정확한 위치 | 변경 전 상태 | 변경 이유 | 변경 후 상태 | OWASP |
|---|---|---|---|---|
| `api/main.py:63-69` 앱 생성·미들웨어 등록 | CORS가 모든 출처·메서드·헤더를 허용하고 API 문서가 기본 공개됨 | 다른 웹사이트의 API 호출과 불필요한 공격 표면을 줄이기 위해 | `APP_ORIGIN` 한 곳, GET/POST와 필요한 헤더만 허용하고 `/docs`, `/redoc`, `/openapi.json`을 비활성화 | A01, A02 |
| `api/middleware.py:11` `SecurityMiddleware` | 요청 크기·읽기 시간 제한, 공통 보안 헤더와 일반화된 예외 응답이 없음 | 과대 요청, 느린 요청, 클릭재킹·콘텐츠 해석 공격과 내부 오류 노출을 줄이기 위해 | 64KiB 본문 제한, 10초 수신 제한, CSP·nosniff·DENY·Referrer/Permissions Policy·HSTS, 요청 ID, 일반화된 503 응답 적용 | A02, A05, A10 |
| `api/settings.py:10` `Settings.from_env()` | 로컬과 운영 설정 구분 및 운영 필수값 검증이 없음 | AWS에서 안전하지 않은 기본값으로 기동되는 것을 막기 위해 | 운영은 HTTPS Origin, 인증서 검증 PostgreSQL, 32자 이상 Agent API 키가 없으면 기동 실패 | A02, A04, A07, A10 |
| `api/security.py:22` `context()`, `:59` `consume_grant()` | 인증·호출 주체 구분 없이 모든 API 호출 가능 | 로그인은 추가하지 않되 외부 Agent 평가 키와 브라우저 교차 출처 방어가 필요해서 | 브라우저 변경 요청은 정확한 Origin을 요구하고 `/api/evaluate`는 `X-Agent-API-Key`를 상수 시간 비교로 검증. DB 공유 호출 제한 적용 | A01, A06, A07 |
| `api/store.py:31-109` 상태·저장소 메서드 | 전역 메모리 `STATE`라 재시작·다중 ECS 태스크에서 상태가 사라지거나 달라짐 | 권한 축소 상태와 승인 상태가 서버 교체로 풀리지 않게 하기 위해 | 단일 서비스 상태를 SQLite(로컬)/PostgreSQL(AWS)에 저장하고 행 잠금 트랜잭션으로 변경을 직렬화 | A01, A06, A10 |
| `api/main.py:98` `save_draft()`, `:172` `post_policy_approve()` | 승인 API가 클라이언트가 다시 보낸 정책 객체를 그대로 활성화 | 미리 본 정책과 승인되는 정책이 달라지는 변조·경쟁 조건을 막기 위해 | 서버가 초안을 보관하고 대상·상태 버전에 묶인 10분짜리 일회성 토큰으로 정확한 초안만 승인 | A01, A06, A08 |
| `api/main.py:201-347` `post_simulate()`, `:369` `post_restore()` | `user_confirmed: true`라는 클라이언트 boolean만으로 권한 복원 | 임의 요청으로 `STOP`을 해제하는 핵심 통제 우회를 막기 위해 | 시뮬레이션 최초 응답에만 복원 토큰을 전달하고 DB에는 해시·대상·버전·만료만 저장. GET 결과에는 토큰을 노출하지 않고 사용 후 즉시 폐기 | A01, A06, A08 |
| `api/schemas.py:8-98` 요청 모델 | 임의 추가 필드, 음수·NaN·무한대 금액, 무제한 문자열과 조작된 도구/행동 입력 허용 | 엔진 오류와 위험 점수 오염, 정책 메타데이터 위조를 막기 위해 | 추가 필드 금지, 금액·문자열·목록·시간 범위 제한, enum과 행동-도구 일치 검증, 클라이언트 provenance 제거 | A05, A06, A10 |
| `api/main.py:398` `post_evaluate()` | 요청자가 `history`, `hour`, `balance_before`를 보내 판정 근거를 조작할 수 있고 권한 축소가 요청 간 유지되지 않음 | 이력 삭제·시간 조작과 반복 호출로 권한 통제를 우회하지 못하게 하기 위해 | 서버 시간과 서버 저장 이력 사용, 권한 ratchet 유지, `request_id` 멱등성·재사용 변조 차단, 운영에서는 신뢰 데이터 연동 전 503으로 닫힘 | A06, A08, A10 |
| `api/policy_engine.py:36` `check_policy()`, `:166` `enforce()` | 0 한도·빈 허용 목록이 제한 없음으로 해석되고 잘못된 만료일이 무시되며 READ_ONLY 조회 판정에 빈틈이 있음 | 경계값이나 파싱 오류가 권한 확대로 이어지는 fail-open을 방지하기 위해 | 0 한도는 모든 금전 행위 제한, 빈 목록은 모든 행위 미위임, 잘못된 만료는 STOP, 권한과 도구를 함께 확인 | A01, A06, A10 |
| `api/llm.py:119` `compile_policy()`, `:486` `explain_result()` | 사용자 콘텐츠와 지시 경계가 약하고 예외 문자열·traceback이 노출되며 설명 출력 검증이 부족함 | 프롬프트 인젝션 영향과 공급자 오류 정보 노출을 제한하기 위해 | system instruction과 JSON 데이터 분리, 구조화 출력 스키마·길이 검증, 15초 제한·재시도 제한, 오류 내용 없는 규칙 기반 대체 | A02, A05, A09, A10 |
| `api/engine.py:33` `verified_model()`, `api/model_checksums.json` | `joblib.load()` 전에 모델 파일 신뢰 여부를 확인하지 않음 | 변조된 pickle 역직렬화로 코드가 실행될 수 있어서 | SHA-256 체크섬이 일치한 기존 모델만 역직렬화하고 불일치 시 앱 기동 중단 | A08 |
| `api/main.py:332-344`, `api/store.py:70-109` | 결과 저장소가 무한 증가하고 보안 이벤트가 휘발됨 | 메모리·DB 고갈과 사후 추적 불가 문제를 줄이기 위해 | 결과 24시간/50건, 평가 이력·재전송 캐시 상한, DB 기반 호출 제한, 정책 승인·복원·평가 감사 로그와 90일 보관 적용 | A09, A10 |
| `web/static/app.js`, `index.js`, `simulate.js`, `result.js` | HTML에 대형 inline script가 있고 로그인/비밀번호 재입력 흐름에 의존 | 엄격한 CSP를 적용하고 로그인 없는 승인 흐름과 서버 토큰을 연결하기 위해 | 스크립트를 정적 파일로 분리, 출력 escape 유지, 정책 토큰은 메모리에서 사용, 복원 토큰은 해당 탭의 `sessionStorage`에만 보관 후 삭제 | A05, A08 |
| `.gitignore`, `.dockerignore`, `Dockerfile` | `.venv`, 캐시와 런타임 파일의 커밋·이미지 포함 위험, 컨테이너 권한 제한 미정 | 개발 산출물·비밀 유입과 컨테이너 피해 범위를 줄이기 위해 | 가상환경·캐시·환경파일·런타임 DB 제외, 비루트 UID와 읽기 전용 루트 파일시스템, 필요한 앱·모델·baseline만 이미지에 복사 | A02, A03, A04 |
| `requirements.txt`, `requirements.lock`, `.github/workflows/security.yml` | deprecated SDK, 전이 의존성 해시 잠금과 자동 취약점 검사가 없음 | 공급망 변경과 알려진 취약 패키지를 탐지하기 위해 | `google-genai`로 이전, 해시 잠금 설치, CI에서 테스트·pip-audit·비밀 검사 수행 | A03 |
| `deploy/aws.yaml` | AWS 네트워크·TLS·비밀·로그 구성이 코드로 고정되지 않음 | 로그인 없는 서비스를 인터넷 전체에 노출하거나 키를 이미지에 넣는 실수를 막기 위해 | 허용 CIDR만 ALB 80/443 접근, ACM TLS, 비공개 Fargate/RDS 보안 그룹, Secrets Manager 주입, CloudWatch Logs, 배포 롤백 설정 | A01, A02, A04, A07, A09, A10 |

개발 환경에서는 `localhost`, `127.0.0.1`, `::1` 중 실제 브라우저가 접속한 루프백 Origin과 Host를 허용한다. 최초 구현처럼 `APP_ORIGIN` 문자열 하나와만 비교하면 `localhost`로 연 화면의 정상 POST 요청이 거부되므로 `api/security.py:11`과 `api/settings.py:47`에서 동일 출처·루프백 판정으로 수정했다. 운영 환경은 설정된 HTTPS 도메인 하나만 허용한다.

## 로그인 제외로 변경된 부분

작업 중 추가됐던 `/api/auth/login`, `/api/auth/me`, `/api/auth/logout`, `/api/auth/step-up`, 사용자/비밀번호/세션 테이블, 로그인 화면과 비밀번호 입력 대화상자를 제거했다. `tools/manage.py`도 계정 관리 명령 없이 DB 초기화와 정리만 제공한다.

이 결정으로 로그인 UI와 사용자 개인정보 저장은 없어졌지만, 애플리케이션이 사용자 본인을 식별할 방법도 없다. 현재의 “승인”은 같은 브라우저 흐름에서 서버 발급 토큰을 되돌려 보내는 명시적 확인이며 강한 본인 인증은 아니다. 그래서 AWS ALB 접근 CIDR 제한을 배포 필수 조건으로 지정했다.

## 검증 결과

- `pytest -q`: 17개 보안·기능 테스트 통과
- `verify.py`: 9개 시나리오의 최종 권한과 단방향 권한 축소 판정 통과
- `python -m py_compile`: 변경한 Python 모듈 구문 검사 통과
- `node --check`: `app.js`, `index.js`, `simulate.js`, `result.js` 구문 검사 통과
- `tools/browser_smoke.py`: 로그인 없는 정책 작성 → 승인 → 시뮬레이션 → 일회성 권한 복원 흐름 통과
- `cfn-lint deploy/aws.yaml`, `pip check`, 데이터셋·모델 diff 검사 통과

실제 AWS DNS/ACM 인증서, RDS TLS, Secrets Manager 주입, ALB CIDR 접근 차단, CloudWatch 경보 수신은 AWS 리소스가 준비된 뒤 스테이징 환경에서 별도로 검증해야 한다.

## 남은 위험과 제외 항목

- 데이터셋과 Git 이력은 요청에 따라 수정하지 않았다. 저장소 자체가 공개라면 데이터 노출 문제는 이 변경으로 해결되지 않는다.
- 로그인과 사용자 구분이 없으므로 허용 네트워크 안의 사용자들은 하나의 정책과 결과를 공유한다.
- 실제 금융 실행 연동은 없다. `/api/evaluate`는 운영 모드에서 신뢰된 금융 데이터 연동 전까지 차단한다.
- 기존 모델 파일 내용은 바꾸지 않았으며 체크섬 검증만 추가했다.
