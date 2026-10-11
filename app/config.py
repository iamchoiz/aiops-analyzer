"""환경변수 설정. 전부 env 로 주입 — 시크릿은 git 평문 금지(ESO/SM 경유)."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass(frozen=True)
class Config:
    # LLM — mock 면 Bedrock 접근 없이 전 체인 검증 가능(모델 활성화 전 기본값)
    llm_mode: str = os.environ.get("LLM_MODE", "mock")            # mock | bedrock
    bedrock_model_id: str = os.environ.get(
        "BEDROCK_MODEL_ID",
        # 계정에 활성화된 실제 프로파일(global). 1단 one-shot=Haiku, 2단 루프=Sonnet 분리 후보(§6).
        "global.anthropic.claude-haiku-4-5-20251001-v1:0",
    )
    aws_region: str = os.environ.get("AWS_REGION", "ap-northeast-2")
    max_context_tokens: int = _int("MAX_CONTEXT_TOKENS", 40_000)
    # Context Pack 보강(ArgoCD·k8s Events). 로컬/mock 은 off, 클러스터 배포 시 on.
    enrich_enabled: bool = os.environ.get("ENRICH", "false").lower() == "true"

    # G1 fingerprint cooldown — 영속 스토리지 필수(§2-1). 지금은 in-memory(Phase 1 full 에서 PVC/Redis).
    cooldown_seconds: int = _int("G1_COOLDOWN_SECONDS", 3600)

    # G2 incident 조립
    correlation_window_seconds: int = _int("G2_WINDOW_SECONDS", 300)
    storm_threshold: int = _int("G2_STORM_THRESHOLD", 15)         # 창에 N개↑ = 대규모 장애 모드

    # G4 예산 서킷브레이커 (앱 내부 1차 — 계정 AWS Budgets 가 2차)
    daily_analysis_cap: int = _int("G4_DAILY_CAP", 200)
    auto_analyze_warning: bool = os.environ.get("G4_AUTO_WARNING", "false").lower() == "true"

    # Slack
    slack_webhook_url: str = os.environ.get("SLACK_WEBHOOK_URL", "")

    # 관측/링크 생성용 베이스 URL
    grafana_base_url: str = os.environ.get("GRAFANA_BASE_URL", "https://grafana.eks.typelearn.co.kr")
    argocd_base_url: str = os.environ.get("ARGOCD_BASE_URL", "https://argocd.eks.typelearn.co.kr")


config = Config()
