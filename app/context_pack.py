"""Context Pack 빌더(§2-2) — 결정론적 코드. LLM 호출 전에 '계산된 사실'만 생성.

덤프 금지: raw 로그/시계열은 넣지 않는다. 상한 초과 시 더 압축하지 더 밀어넣지 않음.

지금(최소 슬라이스)은 (1) 인시던트 메타만 채운다. 나머지 블록은 seam 으로 비워두고
Phase 1 full 에서 결선한다:
  (2) Recent changes  — ArgoCD API sync 이력 + k8s Events (RCA 1순위 신호)
  (3) 메트릭 baseline diff — Thanos 3쿼리(현재/어제/지난주)
  (4) 로그 Drain3 템플릿 압축 — 신규/급증 템플릿만
  (5) 토폴로지 스냅샷 — pod→node→svc→ingress→ALB→RDS 홉별 판정
  (6) 메모리 RAG — 유사 과거 인시던트 + 족보/런북 (Phase 2, S3 Vectors)
"""
from __future__ import annotations

from typing import Any

from .config import config
from .models import Incident


def build_context_pack(incident: Incident) -> dict[str, Any]:
    """인시던트 → LLM 에 줄 압축 컨텍스트(dict). 토큰 상한은 호출측에서 체크."""
    pack: dict[str, Any] = {
        # 1. 인시던트 메타 — '분석 시점 현재 상태' 명시(5분 전 스냅샷 자신만만 RCA 방지)
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
                    "name": a.name,
                    "severity": a.severity,
                    "status": a.status,
                    "startsAt": a.startsAt,
                    "summary": a.annotations.get("summary") or a.annotations.get("description", ""),
                    "runbook": a.annotations.get("runbook_url", ""),
                    "labels": _slim_labels(a.labels),
                }
                for a in incident.alerts
            ],
        },
        # 2~6: seam. ENRICH=true 면 enrich 가 가능한 블록을 채우고 나머지는 data_gap 으로 표기.
        "recent_changes": {"_todo": "ArgoCD sync diff + k8s Events (RCA 1순위)"},
        "metrics_baseline_diff": {"_todo": "Thanos 현재/어제/지난주 3쿼리"},
        "log_templates": {"_todo": "Drain3 신규/급증 템플릿만"},
        "topology": {"_todo": "pod→node→svc→ingress→ALB→RDS 홉별 판정"},
        "memory": {"_todo": "유사 인시던트 + 족보/런북 RAG (Phase 2)"},
    }

    if config.enrich_enabled:
        from . import enrich  # 지연 import — mock/로컬은 enrich 의존성 불필요
        pack["_data_gaps"] = enrich.enrich(incident, pack)
    else:
        pack["_data_gaps"] = ["recent_changes", "metrics_baseline_diff",
                              "log_templates", "topology", "memory"]
    return pack


def _slim_labels(labels: dict[str, str]) -> dict[str, str]:
    """노이즈 라벨(해시·엔드포인트 등) 제거 — 토큰 절약."""
    keep = {"cluster", "namespace", "job", "service", "app", "pod", "node",
            "severity", "alertname", "container", "role", "reason"}
    return {k: v for k, v in labels.items() if k in keep}
