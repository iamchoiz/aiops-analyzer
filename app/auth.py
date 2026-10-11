"""GitHub SSO — 네이티브 OAuth(web flow) + HMAC 서명 세션 쿠키. 외부 의존 없음(stdlib).

시크릿: SM tle/platform/hub 의 aiops_client_id/aiops_client_secret → ESO → env GH_CLIENT_ID/GH_CLIENT_SECRET.
auth 는 두 값이 있을 때만 활성(없으면 로컬/개발은 무인증 — 클러스터에선 항상 주입됨).
접근 제어: GH_ALLOWED_USERS(csv) 지정 시 그 로그인만 허용.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.parse
import urllib.request

CLIENT_ID = os.environ.get("GH_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("GH_CLIENT_SECRET", "")
BASE_URL = os.environ.get("BASE_URL", "https://aiops.eks.typelearn.co.kr")
ALLOWED = {u.strip() for u in os.environ.get("GH_ALLOWED_USERS", "").split(",") if u.strip()}
SESSION_SECRET = (os.environ.get("SESSION_SECRET") or CLIENT_SECRET or "dev-secret").encode()
SESSION_TTL = int(os.environ.get("SESSION_TTL", 86400))
COOKIE = "aiops_session"

# 인증 불필요(열린) 경로 — probe·scrape·webhook·OAuth 흐름
OPEN_PREFIXES = ("/healthz", "/metrics", "/webhook", "/login", "/auth/", "/favicon")


def enabled() -> bool:
    return bool(CLIENT_ID and CLIENT_SECRET)


# ──────────────────────────── 서명 쿠키 ────────────────────────────
def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _ub64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def make_session(login: str) -> str:
    payload = json.dumps({"login": login, "exp": int(time.time()) + SESSION_TTL}).encode()
    sig = hmac.new(SESSION_SECRET, payload, hashlib.sha256).digest()
    return f"{_b64(payload)}.{_b64(sig)}"


def verify_session(cookie: str | None) -> str | None:
    if not cookie or "." not in cookie:
        return None
    try:
        p_b64, s_b64 = cookie.split(".", 1)
        payload = _ub64(p_b64)
        expected = hmac.new(SESSION_SECRET, payload, hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _ub64(s_b64)):
            return None
        data = json.loads(payload)
        if data.get("exp", 0) < time.time():
            return None
        return data.get("login")
    except Exception:
        return None


# ──────────────────────────── OAuth 흐름 ────────────────────────────
def login_url(state: str) -> str:
    q = urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "redirect_uri": f"{BASE_URL}/auth/callback",
        "scope": "read:user",
        "state": state,
    })
    return f"https://github.com/login/oauth/authorize?{q}"


def exchange_code(code: str) -> str | None:
    data = urllib.parse.urlencode({
        "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
        "code": code, "redirect_uri": f"{BASE_URL}/auth/callback",
    }).encode()
    req = urllib.request.Request("https://github.com/login/oauth/access_token", data=data,
                                 headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r).get("access_token")


def fetch_login(token: str) -> str | None:
    req = urllib.request.Request("https://api.github.com/user",
                                 headers={"Authorization": f"Bearer {token}",
                                          "Accept": "application/vnd.github+json",
                                          "User-Agent": "aiops-analyzer"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r).get("login")


def is_allowed(login: str | None) -> bool:
    if not login:
        return False
    if "*" in ALLOWED:              # 명시적 전체 허용(opt-in)
        return True
    return login in ALLOWED         # 빈 목록이면 전원 거부(fail-closed) — 허용목록 명시 강제


def is_open_path(path: str) -> bool:
    return any(path.startswith(p) for p in OPEN_PREFIXES)
