"""관문 — 싼 필터부터(§2-1). G1 중복억제 · G2 인시던트 조립 · G4 예산.

모든 drop 은 analyzer_suppressed_total{layer=} 로 카운트 → 억제 계층 자체를 관측.
상태는 지금 in-memory(파드 재시작에 리셋). Phase 1 full 에서 PVC/Redis 로 영속화 — 영속성 없으면
repeat_interval 재통지에 폭주 재발(§2-1 G1 주석). Store 는 교체 가능하게 인터페이스로 분리.
"""
from __future__ import annotations

import hashlib
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field

from .config import config
from .models import Alert, AlertmanagerWebhook, Incident, Severity


# ──────────────────────────── 억제 메트릭 ────────────────────────────
class SuppressionCounter:
    """analyzer_suppressed_total{layer=} 대용. Phase 1 full 에서 prometheus_client 로 교체."""

    def __init__(self) -> None:
        self._counts: dict[str, int] = defaultdict(int)
        self._lock = threading.Lock()

    def inc(self, layer: str) -> None:
        with self._lock:
            self._counts[layer] += 1

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counts)


suppressed = SuppressionCounter()


# ──────────────────────────── G1 Fingerprint cooldown ────────────────────────────
@dataclass
class _CooldownStore:
    """fingerprint → 마지막 분석 epoch. TTL 지나거나 resolved 면 재분석 허용."""

    _seen: dict[str, float] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def should_analyze(self, fingerprint: str, now: float, ttl: int) -> bool:
        with self._lock:
            last = self._seen.get(fingerprint)
            if last is not None and (now - last) < ttl:
                return False
            self._seen[fingerprint] = now
            return True

    def clear(self, fingerprint: str) -> None:
        """resolved 수신 시 쿨다운 해제 → 재발하면 즉시 재분석."""
        with self._lock:
            self._seen.pop(fingerprint, None)


_cooldown = _CooldownStore()


def _fingerprint(alert: Alert) -> str:
    if alert.fingerprint:
        return alert.fingerprint
    # AM 가 안 줬으면 라벨 해시로 자체 생성(AM fingerprint 와 동일 개념)
    raw = "|".join(f"{k}={v}" for k, v in sorted(alert.labels.items()))
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def g1_filter(alerts: list[Alert], now: float) -> list[Alert]:
    """쿨다운 안 지난 firing 알림은 버린다. resolved 는 통과시키되 쿨다운 해제."""
    passed: list[Alert] = []
    for a in alerts:
        fp = _fingerprint(a)
        if a.status == "resolved":
            _cooldown.clear(fp)
            passed.append(a)          # resolved 는 상태 업데이트 위해 통과
            continue
        if _cooldown.should_analyze(fp, now, config.cooldown_seconds):
            passed.append(a)
        else:
            suppressed.inc("g1_cooldown")
    return passed


# ──────────────────────────── G2 인시던트 조립 ────────────────────────────
class IncidentStore:
    """5분 상관창에서 cluster/namespace 로 알림을 인시던트로 묶는다."""

    def __init__(self) -> None:
        self._incidents: dict[str, Incident] = {}
        self._lock = threading.Lock()
        self._seq = 0

    def _key(self, alert: Alert) -> str:
        # 토폴로지 기반 묶음: 같은 cluster+namespace 는 하나의 사건일 가능성 높음
        cluster = alert.labels.get("cluster", "?")
        ns = alert.labels.get("namespace", alert.labels.get("job", "?"))
        return f"{cluster}/{ns}"

    def _new_id(self, now: float) -> str:
        # Date.now 금지 환경 대비 — now 는 주입. 날짜는 호출측에서 포맷.
        self._seq += 1
        return f"INC-{int(now)}-{self._seq:03d}"

    def assemble(self, alerts: list[Alert], now: float) -> list[tuple[Incident, bool]]:
        """firing 알림을 토폴로지 창으로 묶어 인시던트 생성/갱신.
        반환: [(incident, is_new)] — is_new=이번 호출에서 처음 생성됨(= 분석 큐잉 대상)."""
        grouped: dict[str, list[Alert]] = defaultdict(list)
        for a in alerts:
            grouped[self._key(a)].append(a)

        out: list[tuple[Incident, bool]] = []
        with self._lock:
            for key, group in grouped.items():
                inc = self._find_open(key, now)
                is_new = inc is None
                if inc is None:
                    cluster, ns = (key.split("/", 1) + ["?"])[:2]
                    inc = Incident(
                        id=self._new_id(now),
                        signature=_signature(group),
                        severity=_max_severity(group),
                        created_at=now,
                        updated_at=now,
                        cluster=cluster if cluster != "?" else None,
                        namespace=ns if ns != "?" else None,
                    )
                    self._incidents[inc.id] = inc
                # 병합
                inc.alerts.extend(group)
                inc.alert_names = sorted({a.name for a in inc.alerts})
                inc.severity = _max_severity(inc.alerts)
                inc.status = "resolved" if all(a.status == "resolved" for a in inc.alerts) else "firing"
                inc.storm = len(inc.alerts) >= config.storm_threshold
                inc.signature = _signature(inc.alerts)
                inc.touch(now)
                out.append((inc, is_new))
        return out

    def _find_open(self, key: str, now: float) -> Incident | None:
        for inc in self._incidents.values():
            same = f"{inc.cluster or '?'}/{inc.namespace or '?'}" == key
            if same and inc.status == "firing" and (now - inc.created_at) <= config.correlation_window_seconds:
                return inc
        return None

    def apply_resolved(self, resolved: list[Alert], now: float) -> int:
        """AM resolved 알림 → 해당 fingerprint 알림을 resolved 로, 전부 resolved 면 인시던트 resolved."""
        fps = {_fingerprint(a) for a in resolved}
        n = 0
        with self._lock:
            for inc in self._incidents.values():
                if inc.status != "firing":
                    continue
                changed = False
                for a in inc.alerts:
                    if a.status == "firing" and _fingerprint(a) in fps:
                        a.status = "resolved"
                        changed = True
                if changed:
                    inc.status = "resolved" if all(x.status == "resolved" for x in inc.alerts) else "firing"
                    inc.touch(now)
                    if inc.status == "resolved":
                        n += 1
        return n

    def _sweep_stale(self, now: float) -> None:
        """안전망 — 오래된 firing 자동 해소 + 오래된 resolved 제거(스토어 무한증식 방지)."""
        dead = []
        for iid, inc in self._incidents.items():
            if inc.status == "firing" and (now - inc.updated_at) > config.incident_stale_ttl:
                inc.status = "resolved"
            if inc.status == "resolved" and (now - inc.updated_at) > config.incident_evict_ttl:
                dead.append(iid)
        for iid in dead:
            del self._incidents[iid]

    def all(self) -> list[Incident]:
        with self._lock:
            self._sweep_stale(time.time())
            return list(self._incidents.values())

    def get(self, incident_id: str) -> Incident | None:
        with self._lock:
            return self._incidents.get(incident_id)


incidents = IncidentStore()


class ReportStore:
    """인시던트 → 최근 분석 리포트. §3 UI 타일 클릭 drill-down 의 소스."""

    def __init__(self) -> None:
        self._reports: dict[str, dict] = {}
        self._lock = threading.Lock()

    def set(self, incident_id: str, report: dict) -> None:
        with self._lock:
            self._reports[incident_id] = report

    def get(self, incident_id: str) -> dict | None:
        with self._lock:
            return self._reports.get(incident_id)


reports = ReportStore()


def _max_severity(alerts: list[Alert]) -> Severity:
    sevs = {a.severity for a in alerts}
    if "critical" in sevs:
        return Severity.critical
    if "warning" in sevs:
        return Severity.warning
    return Severity.none


def _signature(alerts: list[Alert]) -> str:
    """G3 유사도/캐시의 기반. 지금은 alertname 집합 해시(Phase 2 에서 임베딩)."""
    names = "|".join(sorted({a.name for a in alerts}))
    return hashlib.sha1(names.encode()).hexdigest()[:16]


# ──────────────────────────── G4 예산 서킷브레이커 ────────────────────────────
class Budget:
    """앱 내부 1차 가드. 계정 레벨 AWS Budgets 가 2차(이중화, §2-1 G4)."""

    def __init__(self) -> None:
        self._count = 0
        self._day = -1
        self._lock = threading.Lock()

    def allow(self, incident: Incident, now: float) -> tuple[bool, str]:
        day = int(now // 86400)
        with self._lock:
            if day != self._day:
                self._day, self._count = day, 0
            # 기본 정책: critical 만 자동, warning 은 on-demand(§2-1 G4)
            if incident.severity != Severity.critical and not config.auto_analyze_warning:
                suppressed.inc("g4_warning_manual")
                return False, "warning 은 on-demand(자동분석 비활성)"
            if self._count >= config.daily_analysis_cap:
                suppressed.inc("g4_daily_cap")
                return False, f"일일 분석 상한 도달({config.daily_analysis_cap})"
            self._count += 1
            return True, ""


budget = Budget()
