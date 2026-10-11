"""analyzer 엔트리포인트 — FastAPI.

파이프라인(§2): webhook(즉시 202 + 백그라운드) → G1 → G2 → G4 → Context Pack → one-shot → Slack.
UI 콘솔(§3): 나스닥 히트맵 — /ui 정적 서빙 + /api/incidents 데이터 + /api/incidents/{id} drill-down.
"""
from __future__ import annotations

import pathlib
import time

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import gates, slack
from .analyzer import analyze
from .config import config
from .context_pack import build_context_pack
from .models import AlertmanagerWebhook, AnalysisReport, Incident

app = FastAPI(title="AIOps Analyzer", version="0.1.0")

_WEB_DIR = pathlib.Path(__file__).resolve().parent.parent / "web"


def _now() -> float:
    return time.time()


def _process(incident: Incident) -> None:
    """백그라운드: G4 예산 체크 → Context Pack → 분석 → 저장 → Slack."""
    ok, reason = gates.budget.allow(incident, _now())
    if not ok:
        print(f"[g4-skip] {incident.id}: {reason}")
        return
    pack = build_context_pack(incident)
    try:
        report = analyze(incident, pack)
    except Exception as e:
        # 분석 실패(Bedrock 장애·쿼터·권한 등)는 조용히 묻지 않는다 — degraded 리포트로 가시화
        report = _degraded_report(incident, e)
        print(f"[analyze-error] {incident.id}: {type(e).__name__}: {e}")
    gates.reports.set(incident.id, report.model_dump())      # §3 UI drill-down 소스
    slack.post(incident, report)
    print(f"[done] {incident.id} confidence={report.overall_confidence} "
          f"needs_investigation={report.needs_investigation}")


def _degraded_report(incident: Incident, err: Exception) -> AnalysisReport:
    return AnalysisReport(
        summary=f"⚠️ 자동 분석 실패 — {type(err).__name__}. 인시던트는 접수됨, 수동 조사 필요.",
        overall_confidence="low",
        root_cause_hypotheses=[],
        suggested_actions=["analyzer 로그 확인", "Bedrock 모델 접근/쿼터 점검"],
        data_gaps=[f"LLM 분석 불가: {str(err)[:200]}"],
        needs_investigation=True,
    )


@app.post("/webhook")
async def webhook(payload: AlertmanagerWebhook, bg: BackgroundTasks):
    """Alertmanager webhook. 즉시 202 — 처리는 백그라운드(알림폭풍에 블로킹 금지)."""
    now = _now()
    fresh = gates.g1_filter(payload.alerts, now)             # G1: 중복억제
    firing = [a for a in fresh if a.status == "firing"]
    if not firing:
        return JSONResponse({"status": "accepted", "analyzed": 0,
                             "note": "쿨다운/전부 resolved"}, status_code=202)
    assembled = gates.incidents.assemble(firing, now)        # G2: 인시던트 조립
    queued = 0
    for inc in assembled:
        if inc.status == "firing":
            bg.add_task(_process, inc)
            queued += 1
    return JSONResponse({"status": "accepted", "incidents": queued,
                         "suppressed": gates.suppressed.snapshot()}, status_code=202)


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "llm_mode": config.llm_mode, "model": config.bedrock_model_id}


@app.get("/api/incidents")
async def api_incidents():
    """§3 히트맵 데이터 소스. 타일 크기(영향범위)·색(상태) 계산에 필요한 요약 + 리포트 존재여부."""
    out = []
    for inc in gates.incidents.all():
        rep = gates.reports.get(inc.id)
        out.append({
            "id": inc.id,
            "severity": inc.severity.value,
            "status": inc.status,
            "cluster": inc.cluster,
            "namespace": inc.namespace,
            "alert_count": len(inc.alerts),
            "alert_names": inc.alert_names,
            "storm": inc.storm,
            "created_at": inc.created_at,
            "updated_at": inc.updated_at,
            "has_report": rep is not None,
            "confidence": (rep or {}).get("overall_confidence"),
        })
    return out


@app.get("/api/incidents/{incident_id}")
async def api_incident_detail(incident_id: str):
    """타일 클릭 drill-down — 인시던트 + 전체 분석 리포트(증거·confidence·배제근거)."""
    inc = gates.incidents.get(incident_id)
    if inc is None:
        raise HTTPException(404, "incident not found")
    return {"incident": inc.model_dump(), "report": gates.reports.get(incident_id)}


@app.get("/api/fleet")
async def api_fleet():
    """전체 현황 상황판(finviz 스타일) 데이터 — 전 클러스터 cluster/namespace 헬스(Thanos)."""
    from . import fleet
    return fleet.fleet_snapshot()


@app.get("/metrics", response_class=PlainTextResponse)
async def metrics():
    """억제 계층 관측(§2-1). Phase 1 full 에서 prometheus_client 포맷으로 교체."""
    lines = [f'analyzer_suppressed_total{{layer="{k}"}} {v}'
             for k, v in gates.suppressed.snapshot().items()]
    return "\n".join(lines) + "\n"


@app.get("/")
async def root():
    return RedirectResponse(url="/ui/")


# 나스닥 히트맵 정적 UI (FastAPI StaticFiles — 거의 공짜, 100% 통제, §3)
if _WEB_DIR.is_dir():
    app.mount("/ui", StaticFiles(directory=str(_WEB_DIR), html=True), name="ui")
