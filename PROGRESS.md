# AIOps Analyzer — 진행 현황

> Bedrock 기반 자동 장애 분석 엔진. 알림 → 게이트 → 수집 → 분석 → Slack·상황판.
> 전체 설계: 인프라 레포 `docs/AIOPS-PLAN.md`. 발표자료: `docs/architecture-deck.html`.
> 런타임: 허브 EKS self-host(Graviton/arm64) · 모델 **Bedrock Claude Sonnet 4.6** · 현재 이미지 `v0.11.0`.

---

## ✅ 완료 — 동작·실클러스터 검증됨

| 영역 | 내용 | 검증 |
|---|---|---|
| **파이프라인** | `webhook(202+백그라운드) → G1 → G2 → G4 → Context Pack → (유사도\|분석) → Slack` | E2E ✅ |
| **게이트** | G1 fingerprint 쿨다운(중복억제) · G2 5분창 cluster/ns 인시던트 조립(+storm) · G4 예산(critical 자동/warning 보류·일일캡) | 중복방지 ✅ |
| **Context Pack** | Loki 로그(에러패턴·ANSI제거·시간창) + Thanos(워크로드·CPU/Mem·문제파드·ALERTS) + ArgoCD/k8s Events. webhook·on-demand **공통 리치 팩** | ✅ |
| **분석** | Bedrock Sonnet 4.6 Converse+tool-use(JSON 스키마) · 인젝션 방어 프롬프트 · 실패 시 degraded 리포트 | 실 RDS 케이스 high ✅ |
| **유사도 캐시 (G3)** | 시그니처 → Titan v2 임베딩(1024d) → **S3 Vectors** 코사인 유사도 ≥0.90 → Bedrock 스킵·과거결론 즉답 | sim 0.96 재발감지 ✅ |
| **인시던트 해소** | AM resolved 웹훅 → `apply_resolved` 상태 전환 + staleness 안전망 + resolved 6h 후 제거 | resolved ✅ |
| **상황판 (finviz)** | 클러스터→ns→워크로드 3단 treemap · 색(CPU/Mem heatmap·헬스) / 크기(replica) 토글 · 클러스터/ns 멀티필터 · 애드온 축소·서비스 강조 · 드릴다운(워크로드·파드·로그·알림·인시던트) · **온디맨드 분석 버튼** | 렌더 ✅ |
| **인시던트 히트맵** | 나스닥 스타일 · 타일 클릭 → RCA 패널 · `#inc=ID` 딥링크 | ✅ |
| **접근/보안** | GitHub SSO(네이티브 OAuth, fail-closed 허용목록) · webhook/healthz/metrics 는 열림 · 데이터 read-only | ✅ |
| **배포** | ECR `tle/aiops-analyzer` · Pod Identity(Bedrock InvokeModel·Titan·s3vectors) · GitOps(ArgoCD) · CI(빌드→ECR→태그 bump) | ✅ |
| **코드리뷰 픽스** | 중복분석·레이스·쿼리인젝션·_process 미보호·실패캐싱·자기매칭·auth fail-open·스토어증식 (8건) | v0.11.0 ✅ |

**AWS 리소스:** ECR `tle/aiops-analyzer` · S3 Vectors 버킷 `tle-aiops-memory`/인덱스 `incidents`(1024·cosine) · IAM `tle-hub-01-aiops-analyzer`(Pod Identity).

---

## ⬜ 남은 것 — 로드맵 (우선순위 순)

1. **Context Pack 완결** *(분석 정확도 영향 최대)* — 메트릭 baseline diff(어제/지난주 대비 n배) · Drain3 로그 템플릿 압축(신규/급증만) · 토폴로지 홉별 판정(pod→node→svc→ingress→ALB→RDS).
2. **상태 영속화** — G1/G2/인시던트/억제 메트릭이 in-memory → 파드 재시작 시 리셋. PVC 또는 Redis 로 이전.
3. **피드백 루프** — Slack 👍/👎 → 메모리에 "이 패턴 과거 분석 틀림" 반영(품질 자가개선).
4. **족보/런북 RAG** — 과거 트러블슈팅 문서 임베딩 → S3 Vectors → Context Pack 에 관련 런북 주입.
5. **심층 조사 에이전트 (2단)** — confidence low 시 MCP 도구(read-only, 8-call 캡)로 bounded 심층 조사(Strands).
6. **평가·트레이싱** — LLM-as-judge 품질 평가 + OTel GenAI 트레이싱(토큰·지연·비용).
7. **prometheus_client 메트릭** — 현재 간이 `/metrics` → 정식 포맷(호출·토큰·비용·억제·실패율) + 자체 알림.

---

## 🔧 하드닝 백로그 — 코드리뷰 유보(무조건 고칠 건 아님, 드문 조건)

- **G1 쿨다운이 G4 판정 전 기록** — 일일캡 도달로 드롭된 critical 이 쿨다운에 갇힘(엣지).
- **G4 스킵 warning 인시던트 잔존** — 분석 안 하는 warning 이 firing 으로 남아 히트맵 노이즈(3h 후 해소).
- **장기 firing 의 stale-resolve/중복 churn** — 5분창 밖 재발은 새 인시던트 생성 + 원본은 3h 후 ghost resolve.
- **라벨 없는 알림 과병합** — cluster/ns 둘 다 없으면 `?/?` 로 뭉침(AM 알림엔 거의 라벨 있음).

---

## 운영 메모

- **배포**: 앱 수정 → `docker buildx build --platform linux/arm64 --push`(ECR) → gitops `deployment.yaml` 이미지 태그 → ArgoCD. (CI 로도 가능 — GITOPS_TOKEN 필요)
- **Alertmanager 연결**: 허브 kps AM config 에 `aiops-analyzer` webhook receiver + severity 라우트(`continue:true`) → `http://aiops-analyzer.aiops.svc/webhook`.
- **로컬 검증**: `LLM_MODE=mock uvicorn app.main:app` + `smoke_test.py`. Bedrock/Thanos/Loki 는 `LLM_MODE=bedrock` + 포트포워드.
- **주의**: 스포크 kps 는 `alertmanager.enabled=false`(설계) → `PrometheusNotConnectedToAlertmanagers` 룰 비활성해둠.
