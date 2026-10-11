"""데이터 모델 — Alertmanager 입력 / 인시던트 / Bedrock 출력 스키마(§2-4).

출력 스키마는 '신뢰는 조사 과정에서' 원칙을 그대로 타입으로 강제한다:
confidence·evidence·ruled_out·data_gaps 가 모두 1급 필드.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


# ──────────────────────────── Alertmanager 입력 ────────────────────────────
class Alert(BaseModel):
    """Alertmanager webhook payload 의 개별 alert."""

    status: Literal["firing", "resolved"] = "firing"
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    startsAt: str | None = None
    endsAt: str | None = None
    fingerprint: str | None = None

    @property
    def name(self) -> str:
        return self.labels.get("alertname", "unknown")

    @property
    def severity(self) -> str:
        return self.labels.get("severity", "none")


class AlertmanagerWebhook(BaseModel):
    """https://prometheus.io/docs/alerting/latest/configuration/#webhook_config"""

    version: str = "4"
    groupKey: str = ""
    status: Literal["firing", "resolved"] = "firing"
    receiver: str = ""
    groupLabels: dict[str, str] = Field(default_factory=dict)
    commonLabels: dict[str, str] = Field(default_factory=dict)
    commonAnnotations: dict[str, str] = Field(default_factory=dict)
    externalURL: str = ""
    alerts: list[Alert] = Field(default_factory=list)


# ──────────────────────────── 인시던트 ────────────────────────────
class Severity(str, Enum):
    critical = "critical"
    warning = "warning"
    none = "none"


class Incident(BaseModel):
    """G2 가 조립한 분석 단위. 알림 N개 → 인시던트 1개."""

    id: str
    signature: str                      # G3 유사도/캐시 키의 기반
    severity: Severity = Severity.none
    status: Literal["firing", "resolved"] = "firing"
    created_at: float                   # epoch seconds (주입받음 — 테스트 결정성)
    updated_at: float
    cluster: str | None = None
    namespace: str | None = None
    alerts: list[Alert] = Field(default_factory=list)
    alert_names: list[str] = Field(default_factory=list)
    storm: bool = False                 # G2 대규모 장애 모드

    def touch(self, now: float) -> None:
        self.updated_at = now


# ──────────────────────────── Bedrock 출력 스키마(§2-4) ────────────────────────────
class Evidence(BaseModel):
    """증거 1개 = 재검증 가능한 클릭 링크 1개가 이상적."""

    description: str
    source: str = ""          # grafana | loki | argocd | cloudwatch | k8s
    link: str | None = None


class RootCauseHypothesis(BaseModel):
    cause: str
    confidence: Literal["high", "medium", "low"]
    evidence: list[Evidence] = Field(default_factory=list)
    ruled_out_reason: str | None = None   # 배제됐다면 왜


class AnalysisReport(BaseModel):
    """1단 one-shot 의 구조화 출력. Slack 렌더는 코드가 — LLM 에 포맷팅 금지(§2-3)."""

    summary: str
    overall_confidence: Literal["high", "medium", "low"]
    timeline: list[str] = Field(default_factory=list)
    root_cause_hypotheses: list[RootCauseHypothesis] = Field(default_factory=list)
    checked_and_ruled_out: list[str] = Field(default_factory=list)
    suggested_actions: list[str] = Field(default_factory=list)
    data_gaps: list[str] = Field(default_factory=list)
    needs_investigation: bool = False     # True → 2단 agent loop 승격 신호(Phase 3)

    @staticmethod
    def json_schema_for_llm() -> dict[str, Any]:
        """Bedrock tool-use / JSON 강제용 스키마."""
        return AnalysisReport.model_json_schema()
