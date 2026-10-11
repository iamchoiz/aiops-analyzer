"""스모크 — HTTP 없이 파이프라인 전 단계를 직접 호출해 검증."""
import json
import os
import pathlib

from app import gates, slack
from app.analyzer import analyze
from app.context_pack import build_context_pack
from app.models import AlertmanagerWebhook

NOW = 1_760_000_000.0  # 고정 epoch (결정성)

payload = AlertmanagerWebhook.model_validate(
    json.loads((pathlib.Path(__file__).parent / "samples/alertmanager-webhook.json").read_text())
)

# G1
fresh = gates.g1_filter(payload.alerts, NOW)
firing = [a for a in fresh if a.status == "firing"]
assert len(firing) == 2, firing
print(f"G1: {len(payload.alerts)} alerts → {len(firing)} firing (pass)")

# G1 중복 — 같은 알림 재전송은 쿨다운에 막혀야
again = gates.g1_filter(payload.alerts, NOW + 10)
assert len([a for a in again if a.status == "firing"]) == 0, again
assert gates.suppressed.snapshot().get("g1_cooldown") == 2
print(f"G1 dedup: 재전송 2건 전부 억제 (suppressed={gates.suppressed.snapshot()})")

# G2
incs = gates.incidents.assemble(firing, NOW)
assert len(incs) == 1, incs
inc = incs[0]
assert inc.severity.value == "critical"           # critical+warning → critical
assert set(inc.alert_names) == {"KubePodCrashLooping", "TargetDown"}
print(f"G2: 2 alerts → 1 incident {inc.id} sev={inc.severity.value} names={inc.alert_names}")

# G4
ok, reason = gates.budget.allow(inc, NOW)
assert ok, reason
print(f"G4: critical 자동분석 허용 (ok={ok})")

# Context Pack
pack = build_context_pack(inc)
assert pack["incident"]["alert_count"] == 2
assert "metrics_baseline_diff" in pack  # seam 존재
print(f"ContextPack: incident 메타 {pack['incident']['alert_count']}건 + seam 5블록")

# 분석(mock) + 렌더
report = analyze(inc, pack)
assert report.overall_confidence == "low"
assert report.needs_investigation is True          # critical
blocks = slack.render_blocks(inc, report)
assert blocks[0]["type"] == "header"
print(f"Analyze(mock): confidence={report.overall_confidence} needs_investigation={report.needs_investigation}")
print(f"Slack render: {len(blocks)} blocks")
print(f"  header = {blocks[0]['text']['text']}")

# ── 보강 graceful degradation: ENRICH=on + 엔드포인트 없음 → crash 없이 gap 표기 ──
import importlib
import app.config as cfg_mod
import app.context_pack as cp_mod

os.environ["ENRICH"] = "true"
importlib.reload(cfg_mod)
importlib.reload(cp_mod)
pack2 = cp_mod.build_context_pack(inc)
assert "_data_gaps" in pack2
rc = pack2["recent_changes"]
assert rc.get("_unavailable"), rc        # ArgoCD/k8s 둘 다 미설정 → unavailable
assert any("미결선" in g for g in pack2["_data_gaps"])
print(f"\nEnrich(degraded): crash 없음, recent_changes _unavailable='{rc['_unavailable']}'")
print(f"  data_gaps={pack2['_data_gaps']}")
os.environ["ENRICH"] = "false"
importlib.reload(cfg_mod); importlib.reload(cp_mod)

print("\n✅ 전 파이프라인 통과 (G1→G2→G4→ContextPack→enrich-degrade→analyze→render)")
