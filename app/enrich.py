"""Context Pack 보강(§2-2 블록 2·3) — '계산된 사실'만, 결정론적.

핵심 규율: **절대 crash 하지 않는다.** 소스가 죽었거나(hub 장애 시 analyzer 가 자기 기반 분석,
§5 리스크 ⑤) 미설정이면 그 블록을 비우고 data_gap 사유를 남긴다 — LLM 호출 자체는 계속된다.

구현된 것:
  - recent_changes: ArgoCD API 최근 sync 이력 + k8s Events(OOMKilled·FailedScheduling…)
미구현 seam(Phase 1 full):
  - metrics_baseline_diff: Thanos 3쿼리 — 알림별 PromQL 선택 로직이 남음(쿼리 실행 헬퍼는 준비됨)
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from typing import Any

from .models import Incident

# 엔드포인트 — in-cluster 기본값. 미설정/불가 시 degrade.
ARGOCD_API = os.environ.get("ARGOCD_API", "")          # 예: https://argocd-server.argocd.svc
ARGOCD_TOKEN = os.environ.get("ARGOCD_TOKEN", "")
THANOS_API = os.environ.get("THANOS_API", "")          # 예: http://thanos-query.monitoring.svc:9090
ENRICH_TIMEOUT = float(os.environ.get("ENRICH_TIMEOUT", "5"))


def enrich(incident: Incident, pack: dict[str, Any]) -> list[str]:
    """pack 을 제자리 보강. 채우지 못한 블록명을 data_gaps 로 반환."""
    gaps: list[str] = []

    changes = _safe("recent_changes", lambda: _recent_changes(incident))
    if changes.get("_unavailable"):
        gaps.append(f"recent_changes({changes['_unavailable']})")
    pack["recent_changes"] = changes

    # metrics/logs/topology/memory 는 아직 seam — 솔직하게 gap 으로 표기(조용히 빼면 신뢰 파괴, §2-4)
    for seam in ("metrics_baseline_diff", "log_templates", "topology", "memory"):
        gaps.append(f"{seam}(미결선)")
    return gaps


def _safe(name: str, fn) -> dict[str, Any]:
    try:
        return fn()
    except Exception as e:  # 소스 장애는 분석 중단 사유가 아니다
        return {"_unavailable": f"{type(e).__name__}: {e}"}


# ──────────────────────────── recent_changes ────────────────────────────
def _recent_changes(incident: Incident) -> dict[str, Any]:
    out: dict[str, Any] = {}
    out["argocd_syncs"] = _argocd_recent_syncs(incident.namespace)
    out["k8s_events"] = _k8s_recent_events(incident.namespace)
    if out["argocd_syncs"] is None and out["k8s_events"] is None:
        return {"_unavailable": "ArgoCD·k8s 둘 다 미설정/불가"}
    return out


def _argocd_recent_syncs(namespace: str | None) -> list[dict] | None:
    """ArgoCD API 로 최근 sync(이미지 태그 diff·시각). 토큰/주소 없으면 None."""
    if not (ARGOCD_API and ARGOCD_TOKEN):
        return None
    req = urllib.request.Request(
        f"{ARGOCD_API.rstrip('/')}/api/v1/applications",
        headers={"Authorization": f"Bearer {ARGOCD_TOKEN}"},
    )
    with urllib.request.urlopen(req, timeout=ENRICH_TIMEOUT) as r:
        apps = json.load(r).get("items", [])
    syncs = []
    for app in apps:
        if namespace and app.get("spec", {}).get("destination", {}).get("namespace") != namespace:
            continue
        op = app.get("status", {}).get("operationState", {})
        syncs.append({
            "app": app.get("metadata", {}).get("name"),
            "revision": op.get("syncResult", {}).get("revision", "")[:8],
            "phase": op.get("phase"),
            "finished_at": op.get("finishedAt"),
        })
    return syncs


def _k8s_recent_events(namespace: str | None) -> list[dict] | None:
    """k8s Events(OOMKilled·FailedScheduling 등 — 메트릭엔 안 보이는 신호). 클러스터 밖이면 None."""
    try:
        from kubernetes import client, config  # type: ignore
    except ImportError:
        return None
    try:
        config.load_incluster_config()
    except Exception:
        try:
            config.load_kube_config()
        except Exception:
            return None
    v1 = client.CoreV1Api()
    ns = namespace or "default"
    evs = v1.list_namespaced_event(ns, limit=50).items
    interesting = {"OOMKilling", "Failed", "FailedScheduling", "BackOff", "Unhealthy", "Killing"}
    return [
        {"reason": e.reason, "object": f"{e.involved_object.kind}/{e.involved_object.name}",
         "message": (e.message or "")[:200], "count": e.count,
         "last": str(e.last_timestamp)}
        for e in evs if e.reason in interesting
    ][:15]


# ──────────────────────────── Thanos 쿼리 헬퍼(seam) ────────────────────────────
def thanos_query(promql: str, at_time: float | None = None) -> float | None:
    """Thanos instant query. baseline diff 의 '실행' 부분 — '어떤 PromQL' 선택이 Phase 1 full 숙제."""
    if not THANOS_API:
        return None
    url = f"{THANOS_API.rstrip('/')}/api/v1/query?query={urllib.parse.quote(promql)}"
    if at_time:
        url += f"&time={int(at_time)}"
    with urllib.request.urlopen(url, timeout=ENRICH_TIMEOUT) as r:
        result = json.load(r).get("data", {}).get("result", [])
    if not result:
        return None
    return float(result[0]["value"][1])
