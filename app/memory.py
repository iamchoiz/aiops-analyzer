"""G3 유사도 캐시 — 인시던트 메모리(S3 Vectors + Titan 임베딩).

새 인시던트의 '시그니처'를 임베딩해 과거 인시던트와 코사인 유사도 비교 →
충분히 유사하면(재발) 풀 분석(Bedrock) 없이 과거 결론으로 즉답 → 비용·시간 절감.
항상 '풀 재분석'(온디맨드 분석 버튼)으로 덮어쓸 수 있다.

전부 best-effort: 비활성/장애 시 no-op(분석은 그대로 진행). read-only 아닌 유일한 쓰기지만
메모리 적재일 뿐 운영 리소스는 건드리지 않는다.
"""
from __future__ import annotations

import json
from typing import Any

from .config import config

_s3v = None
_br = None


def _clients():
    global _s3v, _br
    if _s3v is None:
        import boto3
        _s3v = boto3.client("s3vectors", region_name=config.aws_region)
        _br = boto3.client("bedrock-runtime", region_name=config.aws_region)
    return _s3v, _br


def enabled() -> bool:
    return config.memory_enabled


def embed(text: str) -> list[float] | None:
    """Titan v2 임베딩(1024차원). 실패 시 None."""
    if not enabled():
        return None
    try:
        _, br = _clients()
        r = br.invoke_model(modelId=config.embed_model,
                            body=json.dumps({"inputText": text[:8000]}))
        return json.loads(r["body"].read())["embedding"]
    except Exception as e:
        print(f"[memory] embed 실패: {type(e).__name__}: {e}")
        return None


def signature_text(incident, pack: dict[str, Any]) -> str:
    """인시던트 지문 — alertname 집합 + 토폴로지 + 핵심 로그 라인. 임베딩 입력."""
    alerts = ",".join(sorted(incident.alert_names))
    ns_state = pack.get("namespace_state", {}) or {}
    probs = ns_state.get("problem_pods") or pack.get("incident", {}).get("alerts", [])
    reasons = ",".join(sorted({p.get("last_reason") or p.get("reason", "") for p in probs if isinstance(p, dict)}))
    logs = pack.get("logs") or []
    log_hint = " ".join(l.get("line", "")[:120] for l in logs[:4]) if isinstance(logs, list) else ""
    return f"cluster={incident.cluster} ns={incident.namespace} alerts=[{alerts}] reasons=[{reasons}] logs={log_hint}"[:2000]


def find_similar(vector: list[float] | None, exclude_id: str | None = None) -> dict | None:
    """가장 유사한 과거 인시던트(자기 자신 제외). 유사도 < 임계값이면 None."""
    if not (enabled() and vector):
        return None
    try:
        s3v, _ = _clients()
        r = s3v.query_vectors(vectorBucketName=config.s3vectors_bucket, indexName=config.s3vectors_index,
                              queryVector={"float32": vector}, topK=3,
                              returnDistance=True, returnMetadata=True)
        for h in r.get("vectors", []):            # distance 오름차순(가까운 것 먼저)
            if exclude_id and h.get("key") == exclude_id:
                continue
            sim = 1.0 - float(h.get("distance", 1.0))
            if sim < config.similarity_threshold:
                return None                        # 가장 가까운 것도 임계 미달 → 나머지도 미달
            m = h.get("metadata", {}) or {}
            return {"incident_id": h.get("key"), "similarity": round(sim, 3),
                    "summary": m.get("summary", ""), "confidence": m.get("confidence", "low"),
                    "when": m.get("ts", ""), "cluster": m.get("cluster", ""), "namespace": m.get("namespace", "")}
        return None
    except Exception as e:
        print(f"[memory] query 실패: {type(e).__name__}: {e}")
        return None


def remember(incident, report, vector: list[float] | None, now: float) -> None:
    """인시던트 벡터 + 결론 메타 적재. 다음 재발 때 이걸로 매칭."""
    if not (enabled() and vector):
        return
    try:
        s3v, _ = _clients()
        s3v.put_vectors(vectorBucketName=config.s3vectors_bucket, indexName=config.s3vectors_index,
                        vectors=[{"key": incident.id, "data": {"float32": vector},
                                  "metadata": {"summary": report.summary[:900],
                                               "confidence": report.overall_confidence,
                                               "cluster": incident.cluster or "", "namespace": incident.namespace or "",
                                               "ts": str(int(now))}}])
    except Exception as e:
        print(f"[memory] put 실패: {type(e).__name__}: {e}")
