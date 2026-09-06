# AWS 배포 및 운영 절차

이 서비스에는 로그인과 사용자 계정 기능이 없다. 브라우저용 정책·시뮬레이션 상태는 서비스 전체에서 하나를 공유한다. 따라서 공개 인터넷 전체에 열지 말고 `AllowedClientCidr`로 접근 가능한 사내망·VPN·시연장 IP를 제한해야 한다. `Origin` 검사는 브라우저의 교차 출처 요청을 막는 보조 수단이며 사용자 인증을 대신하지 않는다.

권장 구성은 `허용된 IP → ALB(ACM HTTPS) → 비공개 ECS Fargate → 비공개 RDS PostgreSQL`이다. 앱 보안 그룹은 ALB에서 오는 8000 포트만 받고, RDS는 앱 보안 그룹에서 오는 5432 포트만 받는다.

## 로컬 실행

```powershell
.venv/Scripts/python.exe -m pip install --require-hashes -r requirements.lock
.venv/Scripts/python.exe tools/manage.py init-db
.venv/Scripts/python.exe -m uvicorn main:app --app-dir api --host 127.0.0.1 --port 8000 --no-access-log
```

다른 주소나 포트를 사용하면 `APP_ORIGIN`도 정확히 맞춘다. `.env.example`은 형식만 제공하며 실제 키는 Git에 저장하지 않는다.

## 배포 준비

1. 승인된 Python 이미지 digest로 이미지를 빌드하고 ECR 취약점 검사를 통과한 digest URI를 `ImageUri`에 넣는다.
2. DB 관리자 권한으로 일회성 `python tools/manage.py init-db`를 실행한다. 런타임에는 DDL 권한을 주지 않는다.
3. `DATABASE_URL`과 32자 이상의 `AGENT_API_KEY`를 각각 Secrets Manager에 일반 문자열로 저장한다. `AGENT_API_KEY`는 `/api/evaluate` 전용이며 브라우저 코드에 넣지 않는다.
4. `AllowedClientCidr`에 실제 접근망을 지정한다. 템플릿은 `0.0.0.0/0`을 거부한다.
5. Gemini를 사용하면 `GOOGLE_API_KEY`도 Secrets Manager에서 주입한다. 정책 문장이 외부 LLM으로 전송된다는 점을 운영 정책에 반영한다.

운영 PostgreSQL 연결은 `sslmode=verify-full`과 `sslrootcert`가 필수다. ALB는 ACM 인증서와 TLS 1.2 이상 정책을 사용하며 HTTP 요청은 HTTPS로 전환한다.

## 운영 기준

- 결과는 24시간, 최대 50건 보관한다. 정책 초안과 승인 토큰은 10분 뒤 만료된다.
- 정책 승인·권한 복원 토큰은 대상과 상태 버전에 묶이며 한 번 사용하면 폐기된다.
- 외부 Agent 평가에는 `X-Agent-API-Key`를 사용한다. 키는 Secrets Manager에서 교체하고 새 ECS 태스크를 배포한다.
- CloudWatch에는 요청 ID와 허용된 감사 메타데이터만 기록한다. 요청 본문, 승인 토큰, API 키와 원본 예외는 기록하지 않는다.
- `python tools/manage.py cleanup`을 별도 유지보수 작업으로 매일 실행해 만료 상태와 90일이 지난 감사 기록을 정리한다.
- 배포 전 `pytest`, `verify.py`, JavaScript 구문 검사, CloudFormation 검사를 통과시킨다.

로그인이 없으므로 서로 다른 사용자의 데이터·정책을 분리할 수 없다. 여러 사람이 독립적으로 사용하는 서비스로 전환하려면 인증과 사용자별 상태 모델을 별도 기능으로 설계해야 한다.
