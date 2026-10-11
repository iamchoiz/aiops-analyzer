"""전체 현황 상황판(fleet heatmap) 데이터 — Thanos 로 전 클러스터 kube-state-metrics 집계.

finviz.com/map 스타일: 클러스터(sector) → 네임스페이스(stock) 그룹 treemap.
크기 = 파드 수, 색 = 헬스(not-ready·restart·firing 알림). 전부 결정론적 코드, LLM 무관.

소스 장애 시 crash 하지 않고 빈 결과/에러 플래그 반환(§5 리스크 ⑤ — hub 장애 자기관측).
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from collections import defaultdict
from typing import Any

# Thanos query API — query-frontend(캐싱) http 포트 10902
THANOS_API = os.environ.get("THANOS_API", "http://thanos-query-frontend.monitoring.svc:10902")
FLEET_TIMEOUT = float(os.environ.get("FLEET_TIMEOUT", "10"))
RESTART_WINDOW = os.environ.get("FLEET_RESTART_WINDOW", "15m")


def _q(expr: str) -> list[dict]:
    url = f"{THANOS_API.rstrip('/')}/api/v1/query?query=" + urllib.parse.quote(expr)
    with urllib.request.urlopen(url, timeout=FLEET_TIMEOUT) as r:
        return json.load(r)["data"]["result"]


def _by_ns(result: list[dict]) -> dict[tuple[str, str], float]:
    """(cluster, namespace) → value. cluster/namespace 라벨 없는 시리즈는 버린다."""
    out: dict[tuple[str, str], float] = {}
    for s in result:
        m = s["metric"]
        c, n = m.get("cluster"), m.get("namespace")
        if not c or not n:
            continue
        out[(c, n)] = float(s["value"][1])
    return out


def fleet_snapshot() -> dict[str, Any]:
    """전 클러스터 cluster/namespace 헬스 스냅샷. 색/크기 계산은 프론트가."""
    try:
        pods = _by_ns(_q("count by (cluster,namespace) (kube_pod_info)"))
        ready = _by_ns(_q('count by (cluster,namespace) (kube_pod_status_ready{condition="true"} == 1)'))
        restarts = _by_ns(_q(
            f"sum by (cluster,namespace) (increase(kube_pod_container_status_restarts_total[{RESTART_WINDOW}]))"))
        alerts = _by_ns(_q('count by (cluster,namespace) (ALERTS{alertstate="firing"})'))
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}", "clusters": [], "namespaces": []}

    tiles = []
    for (cluster, ns), pod_count in sorted(pods.items()):
        r = int(ready.get((cluster, ns), 0))
        tiles.append({
            "cluster": cluster,
            "namespace": ns,
            "pods": int(pod_count),
            "ready": r,
            "not_ready": max(0, int(pod_count) - r),
            "restarts": round(restarts.get((cluster, ns), 0.0), 1),
            "alerts": int(alerts.get((cluster, ns), 0)),
        })

    clusters = sorted({t["cluster"] for t in tiles})
    return {
        "clusters": [
            {"cluster": c,
             "pods": sum(t["pods"] for t in tiles if t["cluster"] == c),
             "namespaces": sum(1 for t in tiles if t["cluster"] == c)}
            for c in clusters
        ],
        "namespaces": tiles,
    }
