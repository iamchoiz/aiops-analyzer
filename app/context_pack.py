"""Context Pack 빌더(§2-2) — 결정론적 코드. LLM 호출 전에 '계산된 사실'만 생성.

덤프 금지: raw 로그/시계열은 넣지 않는다. 상한 초과 시 더 압축하지 더 밀어넣지 않음.
ENRICH=true(클러스터)면 on-demand 분석과 동일한 리치 소스를 채운다:
  - recent_changes: ArgoCD sync + k8s Events (RCA 1순위)
  - namespace_state: 워크로드·문제파드·파드·발화알림 (Thanos)
  - logs: Loki 에러 로그 (ANSI 제거)
아직 미수집(로드맵): metric baseline(어제/지난주 대비), topology 홉별 판정, Drain3 템플릿.
"""
from __future__ import annotations

from typing import Any

from .config import config
from .models import Incident


def build_context_pack(incident: Incident) -> dict[str, Any]:
    """인시던트 → LLM 에 줄 압축 컨텍스트(dict). webhook·on-demand 공통 리치 팩."""
    pack: dict[str, Any] = {
        "incident": {
            "id": incident.id,
            "severity": incident.severity.value,
            "status_at_analysis": incident.status,     # 이미 resolved 면 LLM 이 알게
            "cluster": incident.cluster,
            "namespace": incident.namespace,
            "alert_count": len(incident.alerts),
            "storm_mode": incident.storm,
            "alerts": [
                {
                    "name": a.name, "severity": a.severity, "status": a.status, "startsAt": a.startsAt,
                    "summary": a.annotations.get("summary") or a.annotations.get("description", ""),
                    "runbook": a.annotations.get("runbook_url", ""),
                    "labels": _slim_labels(a.labels),
                }
                for a in incident.alerts
            ],
        },
    }

    if not config.enrich_enabled:
        pack["namespace_state"] = {}
        pack["logs"] = []
        pack["_data_gaps"] = ["enrich 비활성(로컬/mock)"]
        return pack

    from . import enrich, fleet
    gaps: list[str] = []

    # 최근 변경 — ArgoCD sync + k8s Events
    try:
        pack["recent_changes"] = enrich._recent_changes(incident)
    except Exception as e:
        pack["recent_changes"] = {"_unavailable": f"{type(e).__name__}: {e}"}
        gaps.append("recent_changes")

    # ns 상태 + 로그 (on-demand 와 동일 소스 → 분석 품질·재발 시그니처 일치)
    if incident.cluster and incident.namespace:
        try:
            d = fleet.namespace_detail(incident.cluster, incident.namespace)
            pack["namespace_state"] = {k: d.get(k) for k in ("workloads", "problem_pods", "pods", "firing_alerts")}
            logs = d.get("logs") or {}
            pack["logs"] = logs.get("lines", [])
            if logs.get("_unavailable"):
                gaps.append("logs(Loki 불가)")
        except Exception as e:
            pack["namespace_state"] = {"_unavailable": str(e)[:120]}
            pack["logs"] = []
            gaps.append("namespace_state")
    else:
        pack["namespace_state"] = {}
        pack["logs"] = []

    gaps += ["metric baseline(어제/지난주 대비)", "topology 홉별 판정"]   # 아직 미수집(로드맵)
    pack["_data_gaps"] = gaps
    return pack


def _slim_labels(labels: dict[str, str]) -> dict[str, str]:
    """노이즈 라벨(해시·엔드포인트 등) 제거 — 토큰 절약."""
    keep = {"cluster", "namespace", "job", "service", "app", "pod", "node",
            "severity", "alertname", "container", "role", "reason"}
    return {k: v for k, v in labels.items() if k in keep}
