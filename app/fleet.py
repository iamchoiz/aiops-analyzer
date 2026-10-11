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
import re
import time
import urllib.parse
import urllib.request
from typing import Any

_ANSI = re.compile(r"\x1b\[[0-9;]*m")           # 터미널 색코드 제거
# PromQL/LogQL 셀렉터에 들어가는 라벨 값 검증 — 인젝션/쿼리깨짐 방지(webhook 은 비인증 경로)
_LABEL_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,253}$")


def _safe_label(s: str) -> bool:
    return isinstance(s, str) and bool(_LABEL_RE.match(s))
# DB/연결 장애는 err/fail 단어가 없는 경우 多 → 패턴 대폭 확대
_ERR_PAT = (r"(?i)(err|error|exception|fatal|panic|fail|warn|refused|timeout|timed out|"
            r"unable|cannot|could not|econnrefused|enotfound|unavailable|denied|"
            r"connection|connect|dial|rds|postgres|mysql|sql|database|reset|crash|"
            r"exit|terminat|killed|oom|unhealthy|probe|retry|retrying)")

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
_KIND = {"deployment": "Deployment", "statefulset": "StatefulSet", "daemonset": "DaemonSet",
         "replicaset": "ReplicaSet", "job": "Job", "rollout": "Rollout"}
_REL = "namespace_workload_pod:kube_pod_owner:relabel"   # pod→workload (Deployment/STS/DS/Rollout 전부 커버)


def fleet_snapshot() -> dict[str, Any]:
    # 타일 소스 = relabel 룰(모든 워크로드 타입 포함 — Argo Rollout 도). desired=현재 파드수, ready=ready 파드수.
    try:
        total = _by(_q(f"count by (cluster,namespace,workload,workload_type) ({_REL})"),
                    "cluster", "namespace", "workload", "workload_type")
        ready = _by(_q(f'count by (cluster,namespace,workload,workload_type) '
                       f'((kube_pod_status_ready{{condition="true"}} == 1) '
                       f'* on(cluster,namespace,pod) group_left(workload,workload_type) {_REL})'),
                    "cluster", "namespace", "workload", "workload_type")
        alerts = _by(_q('count by (cluster,namespace) (ALERTS{alertstate="firing"})'), "cluster", "namespace")
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}", "clusters": [], "tiles": []}

    tiles: list[dict] = []
    for (cluster, ns, wl, wtype), tot in total.items():
        tiles.append({"cluster": cluster, "namespace": ns,
                      "kind": _KIND.get((wtype or "").lower(), wtype or "Workload"), "name": wl,
                      "desired": int(tot), "ready": int(ready.get((cluster, ns, wl, wtype), 0))})
    tiles.sort(key=lambda t: (t["cluster"], t["namespace"], t["name"]))

    # 워크로드별 CPU/Mem (pod→workload join 레코딩룰). best-effort — 실패해도 헬스 타일은 유지.
    cpu = mem = cpu_req = mem_req = {}
    try:
        join = "* on(cluster,namespace,pod) group_left(workload) namespace_workload_pod:kube_pod_owner:relabel"
        cpu = _by(_q(f'sum by (cluster,namespace,workload) (rate(container_cpu_usage_seconds_total{{container!="",container!="POD"}}[5m]) {join})'), "cluster", "namespace", "workload")
        mem = _by(_q(f'sum by (cluster,namespace,workload) (container_memory_working_set_bytes{{container!="",container!="POD"}} {join})'), "cluster", "namespace", "workload")
        cpu_req = _by(_q(f'sum by (cluster,namespace,workload) (kube_pod_container_resource_requests{{resource="cpu"}} {join})'), "cluster", "namespace", "workload")
        mem_req = _by(_q(f'sum by (cluster,namespace,workload) (kube_pod_container_resource_requests{{resource="memory"}} {join})'), "cluster", "namespace", "workload")
    except Exception:
        pass
    for t in tiles:
        k = (t["cluster"], t["namespace"], t["name"])
        t["cpu"] = round(cpu.get(k, 0.0), 4)          # cores
        t["mem"] = int(mem.get(k, 0))                  # bytes
        t["cpu_req"] = round(cpu_req.get(k, 0.0), 4)
        t["mem_req"] = int(mem_req.get(k, 0))

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
    out: dict[str, Any] = {"cluster": cluster, "namespace": namespace}
    if not (_safe_label(cluster) and _safe_label(namespace)):
        out["error"] = "invalid cluster/namespace label"
        out["logs"] = {"lines": []}
        return out
    f = f'cluster="{cluster}",namespace="{namespace}"'
    try:
        out["workloads"] = _workloads(f)
        out["problem_pods"] = _problem_pods(f)
        out["firing_alerts"] = _firing_alerts(f)
        out["pods"] = _all_pods(f)
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    # 로그는 별도 소스(Loki) — 실패해도 나머지는 유지
    out["logs"] = _safe_logs(cluster, namespace)
    return out


def _workloads(f: str) -> list[dict]:
    # relabel 룰 기반 — Deployment/STS/DS/Rollout 전부 커버.
    total = _by(_q(f"count by (workload,workload_type) ({_REL}{{{f}}})"), "workload", "workload_type")
    ready = _by(_q(f'count by (workload,workload_type) '
                   f'((kube_pod_status_ready{{{f},condition="true"}} == 1) '
                   f'* on(cluster,namespace,pod) group_left(workload,workload_type) {_REL}{{{f}}})'),
                "workload", "workload_type")
    items = []
    for (name, wtype), tot in sorted(total.items()):
        items.append(_wl(_KIND.get((wtype or "").lower(), wtype or "Workload"), name,
                         int(ready.get((name, wtype), 0)), int(tot)))
    return items


def _wl(kind: str, name: str, ready: int, desired: int) -> dict:
    return {"kind": kind, "name": name, "ready": ready, "desired": desired,
            "healthy": ready >= desired and desired > 0 or (desired == 0)}


def _problem_pods(f: str) -> list[dict]:
    """현재 문제만 — not-ready 이거나 최근 15분 재시작. 누적 재시작/과거 last_reason 만으로는 플래그 안 함
    (회복된 파드가 과거 흔적으로 계속 남는 문제 방지)."""
    ready = _by(_q(f'kube_pod_status_ready{{{f},condition="true"}}'), "pod")        # pod→0/1
    recent = _by(_q(f"sum by (pod) (increase(kube_pod_container_status_restarts_total{{{f}}}[15m]))"), "pod")
    total = _by(_q(f"sum by (pod) (kube_pod_container_status_restarts_total{{{f}}})"), "pod")  # 참고용
    term = {}
    for s in _q(f"kube_pod_container_status_last_terminated_reason{{{f}}} == 1"):
        term[s["metric"].get("pod")] = s["metric"].get("reason")

    out = []
    for key in set(ready) | {(p,) for p in recent}:
        pod = key[0]
        rd = int(ready.get((pod,), 1))
        rr = round(recent.get((pod,), 0.0), 1)
        bad = rd == 0 or rr >= 1          # 현재 not-ready 또는 최근 재시작
        if not bad:
            continue
        out.append({"pod": pod, "ready": rd == 1, "recent_restarts": rr,
                    "restarts": int(total.get((pod,), 0)), "last_reason": term.get(pod)})
    out.sort(key=lambda p: (p["ready"], -p["recent_restarts"]))
    return out[:20]


def _all_pods(f: str) -> list[dict]:
    """ns 전체 파드 — 상태·재시작·CPU·Mem (Phase D 파드 레벨)."""
    ready = _by(_q(f'kube_pod_status_ready{{{f},condition="true"}}'), "pod")
    restarts = _by(_q(f"sum by (pod) (kube_pod_container_status_restarts_total{{{f}}})"), "pod")
    phase = {}
    for s in _q(f"kube_pod_status_phase{{{f}}} == 1"):
        phase[s["metric"].get("pod")] = s["metric"].get("phase")
    cpu = _by(_q(f'sum by (pod) (rate(container_cpu_usage_seconds_total{{{f},container!="",container!="POD"}}[5m]))'), "pod")
    mem = _by(_q(f'sum by (pod) (container_memory_working_set_bytes{{{f},container!="",container!="POD"}})'), "pod")
    out = []
    for key in sorted(set(ready) | {(p,) for p in phase if p}):
        pod = key[0]
        out.append({"pod": pod, "ready": int(ready.get((pod,), 0)) == 1,
                    "phase": phase.get(pod, "?"), "restarts": int(restarts.get((pod,), 0)),
                    "cpu": round(cpu.get((pod,), 0.0), 3), "mem": int(mem.get((pod,), 0))})
    out.sort(key=lambda p: (p["ready"], -p["restarts"]))
    return out[:60]


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


def loki_recent_logs(cluster: str, namespace: str, limit: int = 30, hours: float = 6) -> list[dict]:
    """에러성 로그 — 넓은 패턴. 없으면 일반 최근 로그 폴백(빈 패널 방지). ANSI 제거."""
    if not (_safe_label(cluster) and _safe_label(namespace)):
        return []
    now = time.time()
    start = int((now - hours * 3600) * 1e9)
    end = int(now * 1e9)
    sel = '{cluster="%s",namespace="%s"}' % (cluster, namespace)
    lines = _loki_query(f"{sel} |~ `{_ERR_PAT}`", start, end, limit)
    if not lines:
        lines = _loki_query(sel, start, end, limit)
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
            clean = _ANSI.sub("", line).strip()
            if clean:
                rows.append({"ts": int(ts), "app": app, "line": clean[:500]})
    rows.sort(key=lambda r: r["ts"], reverse=True)
    return rows[:limit]
