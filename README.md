# AIOps Analyzer

> Bedrock 기반 **자동 장애 분석 엔진** — Alertmanager 알림이 뜨면, 온콜이 Grafana 를 열기 전에
> **1차 원인 분석 리포트**를 Slack 에 먼저 붙인다. LLM 은 조치를 실행하지 않는다 (영구 read-only 조사자).

EKS 허브-스포크 플랫폼의 관측 스택(Prometheus/Thanos·Loki·CloudWatch) 위에서 동작하는 self-hosted 분석기.

---

## 설계 원칙

| 항목 | 결정 |
|---|---|
| **LLM 역할** | **합성기(synthesizer) only** — 압축된 컨텍스트 위에서 분석·판단만. 조치 실행 0 |
| **트리거 단위** | 알림(alert) 아님 → **인시던트(incident)**. 노드 1대 다운 = 알림 35개지만 원인 1개 |
| **데이터 투입** | 덤프 금지 → **"계산된 사실"만**. raw 로그/시계열 덤프는 컨텍스트 폭발 + lost-in-the-middle |
| **비용 통제** | 싼 결정론적 필터 4층을 전부 통과한 것만 Bedrock 호출. 폭주는 *장애 중*(알림폭풍)에 터진다 |
| **신뢰 원천** | 결론이 아니라 **조사 과정** — 증거 링크·배제 근거·confidence·데이터 공백을 전부 표시 |

> 핵심: *LLM 이 아니라 LLM 앞단의 결정론적 파이프라인(Incident Gate)이 설계의 80%.*

---

## 파이프라인

```
Alertmanager ──webhook(즉시 202 + 백그라운드)──▶
  [G1 Fingerprint cooldown] ─▶ [G2 Incident 조립] ─▶ [G4 예산]
  ─▶ [Context Pack 빌더 (LLM 없음)] ─▶ [분석: one-shot(JSON 스키마 강제)]
  ─▶ Slack 리포트 ─▶ 나스닥 히트맵 UI 콘솔
```

### 관문 (싼 필터부터)

| 관문 | 역할 | 막는 실패모드 |
|---|---|---|
| **G1** Fingerprint cooldown | 같은 알림은 resolved 전까지 재분석 0 | `repeat_interval` 재통지 = 재분석 폭주 |
| **G2** Incident 조립 | 토폴로지(cluster/ns) 상관창으로 알림→인시던트 묶음 + storm 모드 | 노드 다운 시 알림 N개를 N번 분석 |
| **G4** 예산 서킷브레이커 | 인시던트당 토큰 캡·일일 상한, critical 자동/warning on-demand | 장애 중 비용 폭주 |

모든 drop 은 `analyzer_suppressed_total{layer=}` 로 노출 → 억제 계층 자체를 관측.

> G3(과거 인시던트 유사도 캐시, S3 Vectors)는 로드맵. 상태 저장은 현재 in-memory → PVC/Redis 로 영속화 예정.

### Context Pack (덤프 금지, 계산된 사실만)

결정론적 코드가 LLM 호출 전에 생성 — 소스가 죽어도 **crash 하지 않고** 해당 블록을 `data_gap` 으로 남긴다:
1. 인시던트 메타 (현재 상태 명시 — "5분 전 스냅샷 자신만만 RCA" 방지)
2. **Recent changes** — ArgoCD sync 이력 + k8s Events (RCA 1순위 신호)
3. 메트릭 baseline diff (Thanos) · 4. 로그 Drain3 템플릿 · 5. 토폴로지 · 6. 메모리 RAG *(3~6 로드맵)*

### 출력 스키마 (신뢰는 조사 과정에서)

`summary` · `overall_confidence` · `timeline` · `root_cause_hypotheses[{cause, confidence, evidence[], ruled_out_reason?}]`
· `checked_and_ruled_out[]` · `suggested_actions[]` · `data_gaps[]`

Slack/UI 렌더링은 **코드가** 담당 — LLM 에 포맷팅을 맡기지 않는다. 로그·이벤트 블록은 untrusted 데이터로 격리(prompt injection 방어).

---

## 나스닥 히트맵 UI 콘솔

analyzer 가 FastAPI StaticFiles 로 직접 서빙하는 treemap — Grafana 패널이 아니라 엔진 소유 데이터를 엔진이 그린다.

- 타일 **크기 = 영향범위**(묶인 알림 수), **색 = 상태**(critical 빨강 — 지속시간 길수록 진해짐 / warning 노랑 / resolved 초록)
- 호버 = 요약, **타일 클릭 = Bedrock 리포트 슬라이드인**(증거 링크·confidence·배제 근거), `#inc=ID` 공유 딥링크
- CDN 의존 없는 바닐라 SVG squarified treemap (CSP 안전). 3s 폴링(색 전환은 CSS transition)

---

## 로컬 실행

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python smoke_test.py                       # HTTP 없이 파이프라인 전 단계 검증

LLM_MODE=mock .venv/bin/uvicorn app.main:app --port 8000
curl -XPOST localhost:8000/webhook -H 'Content-Type: application/json' -d @samples/alertmanager-webhook.json
# 히트맵 UI: http://localhost:8000/ui/
```

`LLM_MODE=mock` 은 Bedrock 접근 없이 전 체인(게이트→컨텍스트→리포트→Slack)을 돌린다. Slack webhook 미설정 시 콘솔 dry-run.

### Bedrock 실분석

```bash
LLM_MODE=bedrock BEDROCK_MODEL_ID=global.anthropic.claude-haiku-4-5-20251001-v1:0 \
AWS_REGION=ap-northeast-2 .venv/bin/uvicorn app.main:app --port 8000
```

자격증명: 로컬 `~/.aws` / 클러스터 EKS Pod Identity. 1단 one-shot=Haiku, 2단 agent loop=Sonnet 분리 후보.

---

## 설정 (env)

| 키 | 기본 | 설명 |
|---|---|---|
| `LLM_MODE` | `mock` | `mock` \| `bedrock` |
| `BEDROCK_MODEL_ID` | global Haiku 4.5 | inference profile id |
| `ENRICH` | `false` | Context Pack 보강(ArgoCD·k8s Events) on/off |
| `ARGOCD_API` / `THANOS_API` | — | in-cluster 엔드포인트 (미설정 시 graceful degrade) |
| `G1_COOLDOWN_SECONDS` | `3600` | fingerprint 쿨다운 |
| `G2_WINDOW_SECONDS` / `G2_STORM_THRESHOLD` | `300` / `15` | 상관창 / 대규모 장애 모드 임계 |
| `G4_DAILY_CAP` / `G4_AUTO_WARNING` | `200` / `false` | 일일 분석 상한 / warning 자동분석 |
| `SLACK_WEBHOOK_URL` | — | 미설정 시 콘솔 dry-run |

## API

| 엔드포인트 | 설명 |
|---|---|
| `POST /webhook` | Alertmanager webhook (즉시 202, 백그라운드 처리) |
| `GET /api/incidents` | 히트맵 데이터 소스 |
| `GET /api/incidents/{id}` | 인시던트 + 전체 분석 리포트 (UI drill-down) |
| `GET /metrics` | 억제 계층 메트릭 |
| `GET /healthz` · `GET /ui/` | 헬스체크 · 히트맵 콘솔 |

---

## 배포

- **CI** (`.github/workflows/build-and-deploy.yml`): self-hosted 러너가 arm64 이미지 빌드 → ECR `tle/aiops-analyzer` push → gitops `platform/hub/aiops/deployment.yaml` 이미지 태그 bump. 시크릿 `GITOPS_TOKEN` 필요.
- **k8s 배포**: gitops 레포 `platform/hub/aiops/` (ArgoCD App). Bedrock 권한은 EKS Pod Identity(terraform).
- **런타임**: 허브 EKS self-host (Thanos Query·Loki in-cluster), 허브 공유 ALB. Alertmanager receiver → `http://aiops-analyzer.aiops.svc/webhook`.

## 로드맵

G3 유사도 캐시(S3 Vectors) · 피드백 루프 · Context Pack 2~6 완결선(Thanos/Drain3/토폴로지/RAG) ·
2단 bounded agent loop(MCP 도구, read-only) · 상태 영속화 · UI 실시간 SSE.

## 스택

Python · FastAPI · Pydantic · AWS Bedrock (Converse + tool-use + prompt caching) · boto3 · kubernetes-client
