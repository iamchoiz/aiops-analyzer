"""전체 현황 상황판(fleet heatmap) 데이터 — Thanos/Loki 로 전 클러스터 상태 집계.

finviz.com/map 스타일: 클러스터(sector) → 네임스페이스(stock) 그룹 treemap + ns 클릭 drill-down.
크기 = 파드 수, 색 = 헬스. 전부 결정론적 코드, LLM 무관.

멀티클러스터 제약: analyzer 는 허브에 있어 스포크 k8s API 직접 접근 불가 →
크로스클러스터로 이미 모이는 소스만 사용(Thanos remote-write · Loki 중앙 집계).

소스 장애 시 crash 하지 않고 빈 결과/에러 플래그 반환(§5 리스크 ⑤).
"""
from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from typing import Any

THANOS_API = os.environ.get("THANOS_API", "http://thanos-query-frontend.monitoring.svc:10902")
LOKI_API = os.environ.get("LOKI_API", "http://loki-gateway.monitoring.svc")
FLEET_TIMEOUT = float(os.environ.get("FLEET_TIMEOUT", "10"))
RESTART_WINDOW = os.environ.get("FLEET_RESTART_WINDOW", "15m")

_BAD_TERM_REASONS = {"OOMKilled", "Error", "ContainerCannotRun", "DeadlineExceeded"}


# ──────────────────────────── Thanos ────────────────────────────
def _q(expr: str) -> list[dict]:
    url = f"{THANOS_API.rstrip('/')}/api/v1/query?query=" + urllib.parse.quote(expr)
    with urllib.request.urlopen(url, timeout=FLEET_TIMEOUT) as r:
        return json.load(r)["data"]["result"]


def _by(result: list[dict], *labels: str) -> dict[tuple, float]:
    """결과를 지정 라벨 튜플 → value 로. 라벨 하나라도 없으면 버린다."""
    out: dict[tuple, float] = {}
    for s in result:
        m = s["metric"]
        key = tuple(m.get(l) for l in labels)
        if any(k is None for k in key):
            continue
        out[key] = float(s["value"][1])
    return out


# ──────────────────────────── L0 플릿 스냅샷 (워크로드 타일) ────────────────────────────
# finviz 구조: 클러스터(섹터) → 네임스페이스(산업) → 워크로드 타일(종목).
# 타일 = 워크로드 1개(replica 여러개여도 하나). 크기 = replica 수, 색 = ready/desired 헬스.
def fleet_snapshot() -> dict[str, Any]:
    try:
        dep_d = _by(_q("kube_deployment_spec_replicas"), "cluster", "namespace", "deployment")
        dep_a = _by(_q("kube_deployment_status_replicas_available"), "cluster", "namespace", "deployment")
        sts_d = _by(_q("kube_statefulset_replicas"), "cluster", "namespace", "statefulset")
        sts_r = _by(_q("kube_statefulset_status_replicas_ready"), "cluster", "namespace", "statefulset")
        ds_d = _by(_q("kube_daemonset_status_desired_number_scheduled"), "cluster", "namespace", "daemonset")
        ds_r = _by(_q("kube_daemonset_status_number_ready"), "cluster", "namespace", "daemonset")
        alerts = _by(_q('count by (cluster,namespace) (ALERTS{alertstate="firing"})'), "cluster", "namespace")
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}", "clusters": [], "tiles": []}

    tiles: list[dict] = []

    def add(kind, src_d, src_r):
        for (cluster, ns, name), desired in src_d.items():
            ready = int(src_r.get((cluster, ns, name), 0))
            tiles.append({"cluster": cluster, "namespace": ns, "kind": kind, "name": name,
                          "desired": int(desired), "ready": ready})

    add("Deployment", dep_d, dep_a)
    add("StatefulSet", sts_d, sts_r)
    add("DaemonSet", ds_d, ds_r)
    tiles.sort(key=lambda t: (t["cluster"], t["namespace"], t["name"]))

    clusters = sorted({t["cluster"] for t in tiles})
    ns_alerts = {f"{c}/{n}": int(v) for (c, n), v in alerts.items()}
    return {
        "clusters": [{"cluster": c,
                      "namespaces": len({t["namespace"] for t in tiles if t["cluster"] == c}),
                      "workloads": sum(1 for t in tiles if t["cluster"] == c),
                      "replicas": sum(t["desired"] for t in tiles if t["cluster"] == c)}
                     for c in clusters],
        "ns_alerts": ns_alerts,
        "tiles": tiles,
    }


# ──────────────────────────── L1 네임스페이스 상세 ────────────────────────────
def namespace_detail(cluster: str, namespace: str) -> dict[str, Any]:
    f = f'cluster="{cluster}",namespace="{namespace}"'
    out: dict[str, Any] = {"cluster": cluster, "namespace": namespace}
    try:
        out["workloads"] = _workloads(f)
        out["problem_pods"] = _problem_pods(f)
        out["firing_alerts"] = _firing_alerts(f)
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    # 로그는 별도 소스(Loki) — 실패해도 나머지는 유지
    out["logs"] = _safe_logs(cluster, namespace)
    return out


def _workloads(f: str) -> list[dict]:
    dep_d = _by(_q(f"kube_deployment_spec_replicas{{{f}}}"), "deployment")
    dep_a = _by(_q(f"kube_deployment_status_replicas_available{{{f}}}"), "deployment")
    sts_d = _by(_q(f"kube_statefulset_replicas{{{f}}}"), "statefulset")
    sts_r = _by(_q(f"kube_statefulset_status_replicas_ready{{{f}}}"), "statefulset")
    ds_d = _by(_q(f"kube_daemonset_status_desired_number_scheduled{{{f}}}"), "daemonset")
    ds_r = _by(_q(f"kube_daemonset_status_number_ready{{{f}}}"), "daemonset")

    items = []
    for (name,), desired in sorted(dep_d.items()):
        items.append(_wl("Deployment", name, int(dep_a.get((name,), 0)), int(desired)))
    for (name,), desired in sorted(sts_d.items()):
        items.append(_wl("StatefulSet", name, int(sts_r.get((name,), 0)), int(desired)))
    for (name,), desired in sorted(ds_d.items()):
        items.append(_wl("DaemonSet", name, int(ds_r.get((name,), 0)), int(desired)))
    return items


def _wl(kind: str, name: str, ready: int, desired: int) -> dict:
    return {"kind": kind, "name": name, "ready": ready, "desired": desired,
            "healthy": ready >= desired and desired > 0 or (desired == 0)}


def _problem_pods(f: str) -> list[dict]:
    ready = _by(_q(f'kube_pod_status_ready{{{f},condition="true"}}'), "pod")        # pod→0/1
    restarts = _by(_q(f"sum by (pod) (kube_pod_container_status_restarts_total{{{f}}})"), "pod")
    term = {}
    for s in _q(f"kube_pod_container_status_last_terminated_reason{{{f}}} == 1"):
        term[s["metric"].get("pod")] = s["metric"].get("reason")

    pods = set(ready) | {(p,) for p in restarts} | {(p,) for p in term if p}
    out = []
    for key in pods:
        pod = key[0]
        rd = int(ready.get((pod,), 1))
        rs = int(restarts.get((pod,), 0))
        reason = term.get(pod)
        bad = rd == 0 or rs >= 3 or (reason in _BAD_TERM_REASONS)
        if not bad:
            continue
        out.append({"pod": pod, "ready": rd == 1, "restarts": rs, "last_reason": reason})
    out.sort(key=lambda p: (p["ready"], -p["restarts"]))   # not-ready 먼저, 재시작 많은 순
    return out[:20]


def _firing_alerts(f: str) -> list[dict]:
    res = _q(f'ALERTS{{{f},alertstate="firing"}}')
    return sorted(({"alertname": s["metric"].get("alertname"),
                    "severity": s["metric"].get("severity", "none")} for s in res),
                  key=lambda a: a["severity"])


# ──────────────────────────── Loki 로그 ────────────────────────────
def _safe_logs(cluster: str, namespace: str) -> dict[str, Any]:
    try:
        return {"lines": loki_recent_logs(cluster, namespace)}
    except Exception as e:
        return {"_unavailable": f"{type(e).__name__}: {e}", "lines": []}


def loki_recent_logs(cluster: str, namespace: str, limit: int = 30) -> list[dict]:
    """최근 에러성 로그 — 없으면 전체 최근 로그로 폴백(빈 패널 방지)."""
    now = time.time()
    start = int((now - 3600) * 1e9)      # 1h 전 (ns)
    end = int(now * 1e9)
    err = r'{cluster="%s",namespace="%s"} |~ `(?i)(err|error|exception|fatal|panic|fail|warn)`' % (cluster, namespace)
    lines = _loki_query(err, start, end, limit)
    if not lines:                         # 에러 없으면 일반 최근 로그
        lines = _loki_query('{cluster="%s",namespace="%s"}' % (cluster, namespace), start, end, limit)
    return lines


def _loki_query(expr: str, start: int, end: int, limit: int) -> list[dict]:
    qs = urllib.parse.urlencode({"query": expr, "start": start, "end": end,
                                 "limit": limit, "direction": "backward"})
    url = f"{LOKI_API.rstrip('/')}/loki/api/v1/query_range?{qs}"
    with urllib.request.urlopen(url, timeout=FLEET_TIMEOUT) as r:
        streams = json.load(r)["data"]["result"]
    rows = []
    for s in streams:
        lbl = s["stream"]
        app = lbl.get("app") or lbl.get("pod") or lbl.get("container", "")
        for ts, line in s["values"]:
            rows.append({"ts": int(ts), "app": app, "line": line[:400]})
    rows.sort(key=lambda r: r["ts"], reverse=True)
    return rows[:limit]
