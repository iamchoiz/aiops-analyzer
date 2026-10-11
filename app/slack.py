"""Slack 리포트 렌더 + 발송(§2-4). 포맷은 코드가 — LLM 출력은 데이터로만 받는다.

구조(상단→하단): 헤더 → 결론+confidence → 증거(≤3) → 배제한 것 → 데이터 공백 → 액션.
스레드 규율: 인시던트당 최상위 메시지 정확히 1개(§2-4). 지금은 webhook 발송이라 ts 추적은
Phase 3(bot 토큰) 에서. 👍/👎·재분석 버튼도 bot 토큰 필요 → Phase 2~3.
"""
from __future__ import annotations

import json
import urllib.request

from .config import config
from .models import AnalysisReport, Incident

_CONF_EMOJI = {"high": "🟢", "medium": "🟡", "low": "🔴"}
_SEV_EMOJI = {"critical": "🔴", "warning": "🟡", "none": "⚪"}


def render_blocks(incident: Incident, report: AnalysisReport) -> list[dict]:
    sev = _SEV_EMOJI.get(incident.severity.value, "⚪")
    conf = _CONF_EMOJI.get(report.overall_confidence, "⚪")
    scope = f"{incident.cluster or '?'}/{incident.namespace or '?'} · 알림 {len(incident.alerts)}건"
    storm = " · ⚠️대규모장애모드" if incident.storm else ""

    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text",
         "text": f"{sev} {incident.id} · {incident.severity.value} · {incident.status}{storm}"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": f"{scope} · 분석 신뢰도 {conf} {report.overall_confidence}"}]},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*결론*\n{report.summary}"}},
    ]

    # 증거 ≤3 (재검증 링크)
    ev_lines: list[str] = []
    for h in report.root_cause_hypotheses:
        if h.ruled_out_reason:
            continue
        for e in h.evidence[:3]:
            link = f" <{e.link}|🔗>" if e.link else ""
            ev_lines.append(f"• [{e.source or '?'}] {e.description}{link}")
        if len(ev_lines) >= 3:
            break
    if ev_lines:
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
                      "text": "*증거*\n" + "\n".join(ev_lines[:3])}})

    if report.checked_and_ruled_out:
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
                      "text": "*확인했고 배제한 것*\n" + "\n".join(f"• {x}" for x in report.checked_and_ruled_out[:5])}})

    if report.data_gaps:
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
                      "text": "*데이터 공백*\n" + ", ".join(report.data_gaps)}})

    if report.suggested_actions:
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
                      "text": "*제안 조치*\n" + "\n".join(f"• {x}" for x in report.suggested_actions[:5])}})

    return blocks


def post(incident: Incident, report: AnalysisReport) -> bool:
    """Slack 발송. webhook 미설정이면 콘솔 출력(로컬 검증)."""
    blocks = render_blocks(incident, report)
    if not config.slack_webhook_url:
        print("[slack:dry-run]\n" + json.dumps(blocks, ensure_ascii=False, indent=2))
        return False
    body = {"blocks": blocks}
    if config.slack_channel:                 # 지정 시 기본 채널 대신 해당 채널로(classic webhook)
        body["channel"] = config.slack_channel
    payload = json.dumps(body).encode()
    req = urllib.request.Request(config.slack_webhook_url, data=payload,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status == 200
