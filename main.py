from http.server import HTTPServer, SimpleHTTPRequestHandler
import json
import hashlib
import os
import re
import secrets
import random
import threading
import time
import ccxt
import numpy as np
import pandas as pd
import requests
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import backtest_framework

# ============================================================
# 거래소 인식 — 검증된 3개로 고정 (V003 신뢰성 검증 결과 반영)
# 바이낸스·바이비트·쿠코인 등은 Render(AWS US) 환경에서 미국IP 지오블록에 걸려
# 에러가 발생하므로 지원 대상에서 제외한다. 아래 3개는 지오블록 없음이 확인됨.
# 코인베이스는 USDT가 아닌 USD가 표준 페어이므로 quote를 USD로 고정한다.
# ============================================================
SUPPORTED_EXCHANGES = {
    # 1w는 거래소 API 지원여부와 무관하게 항상 일봉 리샘플링으로 생성(resample_daily_to_weekly)하므로
    # 3개 거래소 모두 동일 패턴(1w,1d,X,1h)을 안전하게 사용할 수 있다.
    # 빗썸 4h: ccxt 정적목록엔 없지만 실사용으로 정상 동작 확인됨 → 포함.
    # 코인베이스 4h: 실제 API 호출 시 확정 오류("granularity 4h is not a valid value") 확인됨 → 제외, 6h로 대체.
    "upbit":    {"quote": "KRW", "kr_name": "업비트",   "default_tfs": ["1w", "1d", "4h", "1h"]},
    "bithumb":  {"quote": "KRW", "kr_name": "빗썸",     "default_tfs": ["1w", "1d", "4h", "1h"]},
    "coinbase": {"quote": "USD", "kr_name": "코인베이스", "default_tfs": ["1w", "1d", "6h", "1h"]},
}

EXCHANGE_KR_MAP = {"업비트": "upbit", "빗썸": "bithumb", "코인베이스": "coinbase"}

# ============================================================
# 비인가 입력(슬래시 명령이 아닌 임의 입력) 안내 문구
# ============================================================
UNAUTHORIZED_INPUT_GUIDE = (
    "본 시스템은 분석입력, 출력 외 다른 기능은 제공되지 않습니다.\n\n"
    "<b>▶️ 지정코인 분석 명령어</b>\n"
    "/거래소 + 코인명 을 입력해주세요. (한글·영문 모두 가능)\n\n"
    "예시) /업비트 비트코인   또는   /coinbase btc\n\n"
    "시간대(TF)는 자동으로 적용됩니다.\n"
    "• 업비트·빗썸: 1주, 1일, 4시간, 1시간\n"
    "• 코인베이스: 1주, 1일, 6시간, 1시간\n"
    "필요시 직접 지정도 가능합니다: /업비트 비트코인 1d 4h 1h\n\n"
    "<b>한글 코인명 안내</b>\n"
    "업비트·빗썸은 각 거래소 상장목록 기준 한글 인식됩니다.\n"
    "코인베이스는 자체 한글명이 없어 업비트·빗썸에도 상장된 코인만\n"
    "한글 인식되며, 코인베이스 전용 코인은 영문 심볼로 입력해 주세요.\n\n"
    "<b>지원 거래소</b>\n"
    "업비트(KRW) · 빗썸(KRW) · 코인베이스(USD)\n\n"
    "<b>🔎 FindCoin</b>\n"
    "현재 시세분출 가능성이 높은 코인을 분석해서 Top3를 알려드립니다.\n"
    "/거래소 만 입력하면 실행됩니다. (예: /업비트)\n"
    "(전종목 스캔이라 1~3분 정도 소요될 수 있습니다.)"
)


def friendly_error_message(exc, ex_display, symbol_display):
    """기술적 예외를 사용자 친화적 한글 메시지로 변환한다.
    원본 예외(ccxt/Python 메시지)는 서버 로그에만 남기고 사용자에게는 노출하지 않는다.
    [결함수정-Context7검증] ccxt 공식문서(llms.txt)가 "에러 메시지 문자열 파싱은 비권장,
    구조화된 예외 클래스(isinstance)로 판별하라"고 명시. 42번(or/and 우선순위) 수정은
    문자열매칭 내부의 논리버그만 고쳤을 뿐, 방식 자체가 비권장 패턴이었던 근본원인은
    남아있었다. ccxt 예외타입 판별을 1순위로 추가하고, 기존 문자열매칭은 (a)ccxt가 아닌
    일반 예외 (b)예상과 다른 타입이 온 경우를 위한 2순위 안전망으로 그대로 유지한다
    (완전 재작성이 아닌 최소침습적 계층추가 — 기존 폴백을 지우지 않아 리스크 최소화)."""
    # 1순위: ccxt 구조화된 예외타입 판별 (공식 권장 방식, Context7로 계층구조 검증됨)
    if isinstance(exc, ccxt.RequestTimeout):
        return (
            f"⏱️ {ex_display} 서버 응답이 지연되고 있습니다.\n"
            f"잠시 후 다시 시도해 주세요."
        )
    if isinstance(exc, ccxt.NotSupported):
        return (
            f"⏰ {ex_display}에서 지원하지 않는 시간대(TF)입니다.\n"
            f"다른 시간대로 다시 시도해 주세요."
        )
    if isinstance(exc, ccxt.RateLimitExceeded):
        return (
            f"⏳ {ex_display} 요청이 일시적으로 많습니다.\n"
            f"잠시 후 다시 시도해 주세요."
        )
    if isinstance(exc, ccxt.NetworkError):
        return (
            f"🔌 {ex_display} 서버에 연결할 수 없습니다.\n"
            f"잠시 후 다시 시도해 주세요."
        )

    # 2순위(안전망): ccxt 예외타입이 아니거나 위에서 못 걸린 경우 문자열매칭으로 폴백
    err = str(exc).lower()

    if "timeout" in err or "timed out" in err:
        return (
            f"⏱️ {ex_display} 서버 응답이 지연되고 있습니다.\n"
            f"잠시 후 다시 시도해 주세요."
        )
    if ("granularity" in err or "timeframe" in err or "interval" in err) and "not a valid" in err:
        return (
            f"⏰ {ex_display}에서 지원하지 않는 시간대(TF)입니다.\n"
            f"다른 시간대로 다시 시도해 주세요."
        )
    if "rate limit" in err or "429" in err or "too many requests" in err:
        return (
            f"⏳ {ex_display} 요청이 일시적으로 많습니다.\n"
            f"잠시 후 다시 시도해 주세요."
        )
    if "connection" in err or "network" in err or "getaddrinfo" in err or "resolve" in err:
        return (
            f"🔌 {ex_display} 서버에 연결할 수 없습니다.\n"
            f"잠시 후 다시 시도해 주세요."
        )
    return (
        f"⚠️ '{symbol_display}' 분석 중 문제가 발생했습니다.\n"
        f"코인명과 거래소를 다시 확인하신 뒤 시도해 주세요.\n"
        f"문제가 계속되면 잠시 후 다시 시도해 주세요."
    )


def resolve_exchange(name: str):
    """한글명 또는 영문 id를 지원 거래소 id로 변환. 미지원 시 None."""
    if not name:
        return None
    raw = name.strip()
    if raw in EXCHANGE_KR_MAP:
        return EXCHANGE_KR_MAP[raw]
    key = raw.lower()
    if key in SUPPORTED_EXCHANGES:
        return key
    return None


# 수동 TF는 엔진 명세의 분 단위 표준표로 한 번만 정규화한다.
# 기본 TF는 이미 같은 표준표의 부분집합이므로 동일 함수를 거쳐도 값이 변하지 않는다.
TF_STANDARD_MINUTES = {
    "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "2h": 120, "4h": 240, "6h": 360, "8h": 480,
    "12h": 720, "1d": 1440, "3d": 4320, "1w": 10080, "1M": 43200,
}


def normalize_timeframes(raw_tfs):
    """수동 TF를 상위 표준 TF로 수렴·중복제거·내림차순 정렬한다.
    반환: (정규화 목록, 오류 메시지). 표준표 초과값은 임의 축소하지 않고 거부한다."""
    if not isinstance(raw_tfs, (list, tuple)):
        return None, "시간대(TF) 입력 형식이 올바르지 않습니다."
    normalized = []
    for raw_tf in raw_tfs:
        token = str(raw_tf).strip()
        match = re.fullmatch(r"(\d+)(m|h|d|w|M)", token)
        if not match or int(match.group(1)) <= 0:
            return None, f"지원하지 않는 시간대(TF)입니다: {token}"
        number, unit = int(match.group(1)), match.group(2)
        raw_minutes = number * {"m": 1, "h": 60, "d": 1440, "w": 10080, "M": 43200}[unit]
        upper = [name for name, minutes in TF_STANDARD_MINUTES.items() if minutes >= raw_minutes]
        if not upper:
            return None, f"표준 범위를 초과하는 시간대(TF)입니다: {token}"
        canonical = min(upper, key=lambda name: TF_STANDARD_MINUTES[name])
        if canonical not in normalized:
            normalized.append(canonical)
    if not (2 <= len(normalized) <= 4):
        return None, "시간대(TF)는 중복 제거 후 최소 2개, 최대 4개를 지정해 주세요."
    normalized.sort(key=lambda name: TF_STANDARD_MINUTES[name], reverse=True)
    return normalized, None

# ============================================================
# Render 포트 바인딩 (헬스체크용)
# ============================================================
def run_port_server():
    try:
        port = int(os.environ.get("PORT", 10000))
        server = HTTPServer(("0.0.0.0", port), SimpleHTTPRequestHandler)
        print(f"포트 {port} 오픈 완료. Render 수신 대기 중...")
        server.serve_forever()
    except Exception as e:
        print(f"포트 서버 스레드 오류: {e}")


threading.Thread(target=run_port_server, daemon=True).start()

# ============================================================
# 환경변수 로드
# ============================================================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

if not TELEGRAM_BOT_TOKEN or not GEMINI_API_KEY:
    print("⚠️ 경고: TELEGRAM_BOT_TOKEN 또는 GEMINI_API_KEY 환경변수가 비어 있습니다.")

# ============================================================
# 엔진 파일 로드 (운영 고정명 우선)
# 권장 파일명: CandleView_API.txt  (버전 번호는 파일명에 넣지 않음)
# 개정 시 같은 이름으로 덮어쓰면 되고, main.py는 수정할 필요 없음.
# ============================================================
def get_latest_candleview_file():
    """운영 고정명 우선. 버전 숫자는 파일명에 넣지 않는 것을 원칙으로 한다.
    우선순위:
      1) CandleView_API.txt  (권장 운영본)
      2) CandleView.txt
      3) CandleView로 시작하는 기타 .txt (하위 호환, 이름순 마지막 수단)
    """
    preferred = ["CandleView_API.txt", "CandleView.txt"]
    for name in preferred:
        if os.path.isfile(name):
            return name
    files = [f for f in os.listdir(".") if f.startswith("CandleView") and f.endswith(".txt")]
    if not files:
        return None
    files.sort()
    return files[0]

TARGET_FILE = get_latest_candleview_file()
CANDLEVIEW_PROMPT_FULL = ""

if TARGET_FILE:
    try:
        with open(TARGET_FILE, "r", encoding="utf-8") as file:
            CANDLEVIEW_PROMPT_FULL = file.read()
            print(f"[INFO] 엔진 파일({TARGET_FILE}) 로드 성공! 문자수: {len(CANDLEVIEW_PROMPT_FULL):,}자")
    except Exception as e:
        print(f"[ERROR] 엔진 파일 읽기 실패: {e}")
else:
    print(f"[ERROR] 엔진 파일을 찾을 수 없습니다. (CandleView_Engine.txt 또는 CandleView_API_*.txt 파일이 필요합니다.)")
    for f in os.listdir("."):
        if f.endswith(".txt"):
            print(f"  - {f} ({os.path.getsize(f):,} bytes)")

if not CANDLEVIEW_PROMPT_FULL:
    CANDLEVIEW_PROMPT_FULL = "CandleView 정밀 연산 엔진"
    print("[WARN] 엔진 파일을 찾지 못해 기본 문자열로 대체합니다.")


def build_standard_analysis_runtime_prompt(full_prompt):
    """일반 지정코인 분석의 활성 엔진 규칙을 보존하고 history·FindCoin만 제외한다.

    PHASE 1/2의 역할 분리는 사양의 실행 규칙과 각 호출의 추가 지시가 담당한다.
    Layer 0~5를 PHASE별로 다시 잘라 Gemini의 정의·수식·서술 문맥이 누락될 위험은 만들지 않는다.
    사양 표식이 누락·재정렬된 경우 전체 사양을 유지한다.
    """
    if not isinstance(full_prompt, str) or not full_prompt.strip():
        return full_prompt
    history_marker = "📋 [결함 판정 이력 대장]"
    runtime_marker = "(PA-VSA 전용 API 데이터 정밀 연산 엔진"
    findcoin_marker = "=== 14. FindCoin 플러그인 모듈"
    history = full_prompt.find(history_marker)
    runtime = full_prompt.find(runtime_marker)
    findcoin = full_prompt.find(findcoin_marker)
    if not (0 <= history < runtime < findcoin):
        print("[WARN] 표준 runtime profile 경계를 확인하지 못해 전체 엔진 사양을 유지합니다.")
        return full_prompt
    preamble = full_prompt[:history].strip()
    active_runtime = full_prompt[runtime:findcoin].strip()
    standard_prompt = "\n\n".join((preamble, active_runtime))
    print(
        f"[INFO] 일반 분석 표준 runtime profile: {len(standard_prompt):,}자 / "
        f"전체 {len(full_prompt):,}자"
    )
    return standard_prompt


CANDLEVIEW_PROMPT_STANDARD_ANALYSIS = build_standard_analysis_runtime_prompt(CANDLEVIEW_PROMPT_FULL)
CANDLEVIEW_PROMPT_PHASE1 = CANDLEVIEW_PROMPT_STANDARD_ANALYSIS
CANDLEVIEW_PROMPT_PHASE2 = CANDLEVIEW_PROMPT_STANDARD_ANALYSIS

# ============================================================
# 분석 세션 임시 저장소
# - 인라인 callback_data에는 사용자 입력·심볼·TF를 넣지 않는다. Telegram의 UTF-8 64바이트
#   제약과 TF 2~4개 계약을 동시에 지키기 위해 서버 발급 세션 토큰만 사용한다.
# - 동일 chat의 새 분석이 기존 세션을 덮어쓰지 않는다. 만료 전의 이전 버튼도 자기 세션만 참조한다.
# - 프로세스 재시작 또는 TTL 만료 뒤에는 과거 시장 상태를 자동 재수집하지 않고 명시 재명령을 요구한다.
# ============================================================
analysis_sessions = {}
active_session_by_chat = {}
CACHE_TTL_MINUTES = 30
SESSION_TOKEN_BYTES = 12
CALLBACK_PROTOCOL = "cv1"
PHASE2_RETRY_MAX_PER_SESSION = 1


def _session_is_expired(data, now=None):
    if not isinstance(data, dict) or not isinstance(data.get("created_at"), datetime):
        return True
    now = now or datetime.now()
    return now - data["created_at"] > timedelta(minutes=CACHE_TTL_MINUTES)


def _drop_analysis_session(session_id):
    data = analysis_sessions.pop(session_id, None)
    if data is not None and active_session_by_chat.get(data.get("chat_id")) == session_id:
        del active_session_by_chat[data.get("chat_id")]


def create_analysis_session(chat_id, session_data):
    """검증된 분석 상태를 독립 세션으로 보존하고 고정 길이 토큰을 반환한다."""
    if not isinstance(session_data, dict):
        raise TypeError("analysis session data must be a dict")
    session_id = secrets.token_urlsafe(SESSION_TOKEN_BYTES)
    while session_id in analysis_sessions:
        session_id = secrets.token_urlsafe(SESSION_TOKEN_BYTES)
    record = dict(session_data)
    record["chat_id"] = chat_id
    record["session_id"] = session_id
    record["created_at"] = datetime.now()
    # PHASE 2는 사용자 승인 1회와 P2M02/P2M03 동일 세션 재시도 1회만 허용한다.
    # 이 상태는 cache/session 안에만 존재하며, PHASE 1 원천·모델 결과를 변경하지 않는다.
    record["phase2_state"] = "ready"
    record["phase2_retry_count"] = 0
    analysis_sessions[session_id] = record
    active_session_by_chat[chat_id] = session_id
    return session_id


def get_analysis_session(chat_id, session_id, now=None):
    """chat 바인딩·TTL을 함께 확인한다. 만료·타인 세션은 자동 재분석하지 않는다."""
    if not isinstance(session_id, str) or not session_id:
        return None
    data = analysis_sessions.get(session_id)
    if data is None or data.get("chat_id") != chat_id:
        return None
    if _session_is_expired(data, now=now):
        _drop_analysis_session(session_id)
        return None
    return data


def clean_expired_cache():
    """하위 호환 함수명은 유지하되, TTL 정리는 session 단위로 수행한다."""
    now = datetime.now()
    expired = [sid for sid, data in analysis_sessions.items() if _session_is_expired(data, now=now)]
    for session_id in expired:
        _drop_analysis_session(session_id)


def begin_phase2_execution(session_data, action, now=None):
    """PHASE 2 최초 승인 또는 P2M02/P2M03 재시도의 단일 실행권을 원자적으로 취득한다."""
    if not isinstance(session_data, dict):
        return False, "분석 세션을 찾을 수 없습니다."
    state = session_data.get("phase2_state", "ready")
    retries = int(session_data.get("phase2_retry_count", 0) or 0)
    if state == "in_progress":
        return False, "동일 세션의 Phase2 분석이 이미 진행 중입니다."
    if action == "phase2_run":
        if state == "ready":
            session_data["phase2_state"] = "in_progress"
            return True, ""
        if state == "retry_available":
            return False, "PHASE 2 보류 후 재시도 버튼을 사용해 주세요."
        return False, "이 세션의 Phase2 실행은 이미 종료되었습니다. 새 분석 명령을 사용해 주세요."
    if action == "phase2_retry":
        if state != "retry_available" or retries >= PHASE2_RETRY_MAX_PER_SESSION:
            return False, "Phase2 재시도 가능 시간이 종료되었습니다. 새 분석 명령을 사용해 주세요."
        retry_not_before = session_data.get("phase2_retry_not_before")
        current_time = now or datetime.now()
        if isinstance(retry_not_before, datetime) and current_time < retry_not_before:
            remaining_seconds = max(1, int((retry_not_before - current_time).total_seconds() + 0.999))
            return False, f"고정 모델 할당량 재시도 대기 중입니다. 약 {remaining_seconds}초 후 다시 눌러 주세요."
        session_data["phase2_retry_count"] = retries + 1
        session_data["phase2_state"] = "in_progress"
        session_data.pop("phase2_retry_not_before", None)
        return True, ""
    return False, "지원하지 않는 Phase2 실행 요청입니다."


def finish_phase2_execution(session_data, outcome, retry_after_seconds=None, now=None):
    """P2M02/P2M03만 같은 session의 PHASE 2 재시도를 한 번 열고, 나머지는 종료한다."""
    if not isinstance(session_data, dict):
        return "terminal"
    retries = int(session_data.get("phase2_retry_count", 0) or 0)
    if outcome in ("P2M02", "P2M03") and retries < PHASE2_RETRY_MAX_PER_SESSION:
        session_data["phase2_state"] = "retry_available"
        session_data["phase2_retry_reason"] = outcome
        if outcome == "P2M02":
            try:
                delay_seconds = max(0, min(int(retry_after_seconds or 0), CACHE_TTL_MINUTES * 60))
            except (TypeError, ValueError):
                delay_seconds = 0
            if delay_seconds:
                session_data["phase2_retry_not_before"] = (now or datetime.now()) + timedelta(seconds=delay_seconds)
            else:
                session_data.pop("phase2_retry_not_before", None)
        else:
            session_data.pop("phase2_retry_not_before", None)
    elif outcome in ("P2M02", "P2M03"):
        session_data["phase2_state"] = "retry_exhausted"
        session_data.pop("phase2_retry_not_before", None)
    else:
        session_data["phase2_state"] = "completed"
        session_data.pop("phase2_retry_not_before", None)
        session_data.pop("phase2_retry_reason", None)
    return session_data["phase2_state"]


def classify_phase2_retryable_result(phase2_result):
    """명시적 P2M02/P2M03 hold만 같은 session 재시도 대상으로 인정한다."""
    if not isinstance(phase2_result, str):
        return None
    if phase2_result.startswith("[검증보류 — PHASE 2 승인 모델 할당량 도달]"):
        return "P2M02"
    if phase2_result.startswith("[검증보류 — PHASE 2 승인 모델 호출 실패]"):
        return "P2M03"
    return None


def is_p2m03_retryable_result(phase2_result):
    """run_phase2의 명시적 P2M03 hold만 동일 세션 재시도 대상으로 인정한다."""
    return isinstance(phase2_result, str) and phase2_result.startswith("[검증보류 — PHASE 2 승인 모델 호출 실패]")


def extract_p2m02_retry_after_seconds(phase2_result):
    """P2M02의 시스템 생성 안내문에서 TTL 이내 provider 재시도 대기만 추출한다."""
    if classify_phase2_retryable_result(phase2_result) != "P2M02":
        return None
    matched = re.search(r"약\s+(\d+)\s*초 후", phase2_result)
    if not matched:
        return None
    return max(0, min(int(matched.group(1)), CACHE_TTL_MINUTES * 60))


def parse_phase_callback(data):
    """현재 프로토콜의 세션 콜백만 해석한다. 구형 입력형 콜백은 의도적으로 거부한다."""
    if not isinstance(data, str):
        return None, None
    parts = data.split("|")
    if len(parts) != 3 or parts[0] != CALLBACK_PROTOCOL:
        return None, None
    action, session_id = parts[1], parts[2]
    if action not in ("phase1_view", "supplement_view", "phase2_run", "phase2_retry", "fractal_view"):
        return None, None
    return action, session_id


# ============================================================
# 업비트 한글 코인 맵
# ============================================================
def fetch_upbit_korean_map():
    try:
        url = "https://api.upbit.com/v1/market/all?isDetails=false"
        res = requests.get(url, timeout=10).json()
        k_map = {}
        for item in res:
            if item["market"].startswith("KRW-"):
                sym = item["market"].replace("KRW-", "")
                k_name = item["korean_name"].replace(" ", "").strip()
                k_map[k_name] = sym
        return k_map
    except Exception:
        return {}


def fetch_bithumb_korean_map():
    """빗썸 자체 API에서 한글명 조회 시도. 필드 부재·API 실패 시 빈 dict로 안전 폴백
    (업비트 매핑을 우연히 재사용하지 않고, 빗썸 자체 소스가 없으면 명확히 비워둔다)."""
    try:
        url = "https://api.bithumb.com/v1/market/all?isDetails=false"
        res = requests.get(url, timeout=10).json()
        k_map = {}
        for item in res:
            market = item.get("market", "")
            k_name = (item.get("korean_name") or "").replace(" ", "").strip()
            if market.startswith("KRW-") and k_name:
                sym = market.replace("KRW-", "")
                k_map[k_name] = sym
        return k_map
    except Exception:
        return {}


UPBIT_KOREAN_MAP = fetch_upbit_korean_map()
print(f"[INFO] 업비트 한글 맵 로드: {len(UPBIT_KOREAN_MAP)}개 코인")

BITHUMB_KOREAN_MAP = fetch_bithumb_korean_map()
print(f"[INFO] 빗썸 한글 맵 로드: {len(BITHUMB_KOREAN_MAP)}개 코인"
      + ("" if BITHUMB_KOREAN_MAP else " (자체 한글명 미제공 — 업비트 매핑을 보조로 사용)"))


def resolve_korean_symbol(clean_name, ex_name):
    """거래소별 한글명 해석 우선순위(명확한 소스 계층화):
    1) 분석대상 거래소 자체의 한글맵(가장 정확)
    2) 없으면 국내 자매거래소(업비트↔빗썸) 매핑을 보조로 재사용(실용적 타협)
    3) 코인베이스 등 해외거래소는 자체 한글소스가 없으므로 국내맵만 참고 시도
    어디에도 없으면 원문을 그대로 대문자 심볼로 간주(영문 입력으로 처리)."""
    if ex_name == "upbit":
        primary, secondary = UPBIT_KOREAN_MAP, BITHUMB_KOREAN_MAP
    elif ex_name == "bithumb":
        primary, secondary = BITHUMB_KOREAN_MAP, UPBIT_KOREAN_MAP
    else:
        primary, secondary = {}, UPBIT_KOREAN_MAP

    if clean_name in primary:
        return primary[clean_name]
    if clean_name in secondary:
        return secondary[clean_name]
    return clean_name.upper()


# [결함수정] 심볼→한글명 역방향 조회 (FindCoin 한글명 오매칭 근본해결용)
# 근거: 실사용 감사에서 FindCoin이 "CBK"를 "무비블록"(실제로는 MBL)으로, "THETA"를
# "테타토큰"(실제로는 "세타토큰")으로 오기하는 사례 확인. 원인: main.py가 정확한 한글명
# 데이터(UPBIT_KOREAN_MAP 등)를 이미 보유하고도 FindCoin payload에 심볼(티커)만 넘기고
# 한글명은 넘기지 않아, Gemini가 자기 기억으로 창작하게 방치했던 구조적 결함.
# 원본 맵은 {한글명: 심볼} 방향이므로, 최초 1회만 역맵을 구축해 재사용한다(SSOT, 매회 순회 방지).
_UPBIT_SYMBOL_TO_KOREAN = {v: k for k, v in UPBIT_KOREAN_MAP.items()}
_BITHUMB_SYMBOL_TO_KOREAN = {v: k for k, v in BITHUMB_KOREAN_MAP.items()}


def resolve_symbol_korean_name(base_symbol, ex_name):
    """심볼(예: 'CBK')을 정확한 한글명으로 변환. resolve_korean_symbol과 동일한
    소스 우선순위(분석대상 거래소 자체 맵 → 국내 자매거래소 보조)를 역방향으로 적용.
    어디에도 없으면 None(호출측에서 안전 폴백 처리)."""
    if ex_name == "upbit":
        primary, secondary = _UPBIT_SYMBOL_TO_KOREAN, _BITHUMB_SYMBOL_TO_KOREAN
    elif ex_name == "bithumb":
        primary, secondary = _BITHUMB_SYMBOL_TO_KOREAN, _UPBIT_SYMBOL_TO_KOREAN
    else:
        primary, secondary = {}, _UPBIT_SYMBOL_TO_KOREAN
    return primary.get(base_symbol) or secondary.get(base_symbol)


# ============================================================
# RMA-RSI 14
# ============================================================
def calculate_rma_rsi(prices, period=14):
    delta = prices.diff()
    gain = (delta.where(delta > 0, 0)).fillna(0)
    loss = (-delta.where(delta < 0, 0)).fillna(0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    # [결함수정-Grok42] avg_gain==0 AND avg_loss==0(완전 무변동, 예: 저유동성 코인의
    # 거래정지·동일가 반복)이면 rs=0/0=NaN이 되어 RSI 전체가 오염된다("RSI: nan"이
    # 그대로 프롬프트에 노출되던 실사례 재현 확인). 가격변동 정보 자체가 없는 상태이므로
    # RSI척도의 완전균형점(50.0, 중립)으로 명시 처리한다. 순수상승(→100)/순수하락(→0)은
    # 기존 로직이 이미 자연스럽게(0으로나눔=inf 경로) 정상 산출하므로 영향 없음.
    return rsi.where(~((avg_gain == 0) & (avg_loss == 0)), 50.0)


# ============================================================
# [결함수정] Regular RSI 다이버전스 순수 기하학 사전계산 + 가점 수치 확정
# - 근거: 가격 HH + RSI LH / 가격 LL + RSI HL이 명확해도 Gemini 다중 스윙 추론
#   실패로 감지·가점이 누락되는 사례 확인.
# - 범위: Regular만. Hidden·Exaggerated는 기존 STAGE 0·Module B 경로 유지.
#   RSI_DELTA_MIN=3.0, RSI_DISTANCE_MAX=30 재사용. 신규 상수 없음.
# - 가격 피벗: 좌우 ≥2봉 + 주요 스윙 진폭 필터(직전 파동 평균의 60%, 스펙 SSOT).
# - RSI 피벗: 가격 피벗 ±±2봉 이내만 매칭(벗어나면 해당 가격 피벗 폐기).
# - 복수 후보가 성립하면 |ΔRSI| 크기와 무관하게 종료 피벗이 가장 최근인 1개만 확정한다.
# - Python이 관측 + Regular 가점(+0.3) 수치를 확정해 주입한다.
# ============================================================
RSI_DELTA_MIN_DIV = 3.0
RSI_DISTANCE_MAX_DIV = 30
REGULAR_DIV_POINTS = 0.3  # Module B 일반 다이버전스 가점 (SSOT와 동일)
MAJOR_SWING_AMP_RATIO = 0.60  # 주요 스윙 진폭 ≥ 직전 파동 평균의 60% (스펙 SSOT)


def _find_pivots(series_high, series_low, left=2, right=2):
    """좌우 left/right봉 극값 피벗 인덱스 목록 반환. 완성봉 구간만 전달할 것."""
    n = len(series_high)
    highs, lows = [], []
    for i in range(left, n - right):
        window_h = series_high[i - left : i + right + 1]
        window_l = series_low[i - left : i + right + 1]
        if series_high[i] >= window_h.max() and list(window_h).count(series_high[i]) == 1:
            highs.append(i)
        if series_low[i] <= window_l.min() and list(window_l).count(series_low[i]) == 1:
            lows.append(i)
    return highs, lows


def _filter_major_price_pivots(highs, lows, high_arr, low_arr):
    """주요 스윙 진폭 필터: 파동 진폭 ≥ 직전 최대 3개 파동 평균의 60%.
    콜드스타트(이전 파동 < 3)는 확보된 파동 평균을 사용(스펙과 동일)."""
    events = sorted([(i, "H", float(high_arr[i])) for i in highs] +
                    [(i, "L", float(low_arr[i])) for i in lows])
    if len(events) < 2:
        return highs, lows

    amps = []
    keep_H, keep_L = set(), set()
    # 첫 마디는 기준점 — 진폭 비교 대상이 없으므로 통과
    keep_H.add(events[0][0]) if events[0][1] == "H" else keep_L.add(events[0][0])

    for k in range(1, len(events)):
        i_prev, t_prev, v_prev = events[k - 1]
        i_cur, t_cur, v_cur = events[k]
        amp = abs(v_cur - v_prev)
        if not amps:
            # 첫 파동: 평균 없음 → 통과 후 기록
            (keep_H if t_cur == "H" else keep_L).add(i_cur)
            amps.append(amp)
            continue
        ref = sum(amps[-3:]) / len(amps[-3:])
        if amp >= ref * MAJOR_SWING_AMP_RATIO:
            (keep_H if t_cur == "H" else keep_L).add(i_cur)
            amps.append(amp)
        # 미달 시 해당 마디는 주요 스윙에서 제외(amps에 넣지 않음)

    return sorted(keep_H), sorted(keep_L)


def detect_regular_divergence(df):
    """완성봉 기준 Regular 다이버전스 1건 탐지.
    반환: 관측 태그 + 확정 가점 수치. 가점 미해당 시 가점 0.
    df: open/high/low/close/rsi. 진행봉(마지막 행) 내부 제외."""
    if df is None or len(df) < 10:
        return "다이버전스: 데이터부족 | 가점: 0"
    work = df.iloc[:-1].reset_index(drop=True)
    if len(work) < 10:
        return "다이버전스: 데이터부족 | 가점: 0"

    high_arr = work["high"].values
    low_arr = work["low"].values
    rsi_vals = work["rsi"].values

    highs_raw, lows_raw = _find_pivots(high_arr, low_arr, left=2, right=2)
    highs_px, lows_px = _filter_major_price_pivots(highs_raw, lows_raw, high_arr, low_arr)
    highs_rsi, lows_rsi = _find_pivots(rsi_vals, rsi_vals, left=2, right=2)

    def _rsi_pivot_at(price_idx, rsi_pivots):
        """가격 피벗 ±2봉 이내 RSI 피벗만 허용. 없으면 None(해당 가격 피벗 폐기)."""
        if not rsi_pivots:
            return None
        candidates = [r for r in rsi_pivots if abs(r - price_idx) <= 2]
        if not candidates:
            return None
        return min(candidates, key=lambda r: abs(r - price_idx))

    candidates = []  # (abs_delta, end_idx, tag_string); 최종 선택은 end_idx(최신 확정)만 사용

    # Regular Bearish: 가격 HH + RSI LH
    if len(highs_px) >= 2:
        for a in range(len(highs_px) - 1):
            i1, i2 = highs_px[a], highs_px[a + 1]
            dist = i2 - i1
            if not (1 <= dist <= RSI_DISTANCE_MAX_DIV):
                continue
            p1, p2 = float(high_arr[i1]), float(high_arr[i2])
            if p2 <= p1:
                continue
            r1_idx = _rsi_pivot_at(i1, highs_rsi)
            r2_idx = _rsi_pivot_at(i2, highs_rsi)
            if r1_idx is None or r2_idx is None or r2_idx == r1_idx:
                continue
            r1, r2 = float(rsi_vals[r1_idx]), float(rsi_vals[r2_idx])
            delta = r2 - r1
            if r2 < r1 and abs(delta) >= RSI_DELTA_MIN_DIV:
                tag = (
                    f"다이버전스: 일반하락 | 가격 {p1:.4g}→{p2:.4g} | "
                    f"RSI {r1:.1f}→{r2:.1f} (Δ{delta:+.1f}) | 간격 {dist}봉 | "
                    f"선정: 최신 확정 페어 | 가점: +{REGULAR_DIV_POINTS} (필수적용)"
                )
                candidates.append((abs(delta), i2, tag))

    # Regular Bullish: 가격 LL + RSI HL
    if len(lows_px) >= 2:
        for a in range(len(lows_px) - 1):
            i1, i2 = lows_px[a], lows_px[a + 1]
            dist = i2 - i1
            if not (1 <= dist <= RSI_DISTANCE_MAX_DIV):
                continue
            p1, p2 = float(low_arr[i1]), float(low_arr[i2])
            if p2 >= p1:
                continue
            r1_idx = _rsi_pivot_at(i1, lows_rsi)
            r2_idx = _rsi_pivot_at(i2, lows_rsi)
            if r1_idx is None or r2_idx is None or r2_idx == r1_idx:
                continue
            r1, r2 = float(rsi_vals[r1_idx]), float(rsi_vals[r2_idx])
            delta = r2 - r1
            if r2 > r1 and abs(delta) >= RSI_DELTA_MIN_DIV:
                tag = (
                    f"다이버전스: 일반상승 | 가격 {p1:.4g}→{p2:.4g} | "
                    f"RSI {r1:.1f}→{r2:.1f} (Δ{delta:+.1f}) | 간격 {dist}봉 | "
                    f"선정: 최신 확정 페어 | 가점: +{REGULAR_DIV_POINTS} (필수적용)"
                )
                candidates.append((abs(delta), i2, tag))

    if not candidates:
        return "다이버전스: 미검출 | 가점: 0"

    # 최신 완료 피벗(end_idx)이 현재 시장 국면을 대표한다. |ΔRSI| 크기는 유효성(RSI_DELTA_MIN)만
    # 판정하며, 더 오래된 큰 편차가 최신 신호를 덮지 못하게 한다.
    return max(candidates, key=lambda x: x[1])[2]


def is_subday_tf(tf: str) -> bool:
    """TF 문자열이 1일 미만(HOTD/LOTD 일중 스윕 대상)인지 판별한다.
    ccxt 표준 표기(예: '15m','1h','4h','6h','1d','1w') 기준, 접미사로 단위를 판단한다."""
    if tf.endswith("w") or tf.endswith("d"):
        return False
    if tf.endswith("h"):
        try:
            return int(tf[:-1]) < 24
        except ValueError:
            return True
    if tf.endswith("m"):
        return True
    return False


def _spec_number(name, integer=False):
    """수집 품질 기준도 CandleView 명세 Layer 1 SSOT에서만 읽는다."""
    match = re.search(rf"{re.escape(name)}\s*=\s*([0-9]+(?:\.[0-9]+)?)", CANDLEVIEW_PROMPT_FULL)
    if not match:
        return None
    value = float(match.group(1))
    return int(value) if integer else value


DATA_MIN_COMPLETED_BARS = _spec_number("DATA_MIN_COMPLETED_BARS", integer=True)
WEEKLY_HISTORY_DAYS = _spec_number("WEEKLY_HISTORY_DAYS", integer=True)
OHLCV_STALE_INTERVALS = _spec_number("OHLCV_STALE_INTERVALS", integer=True)


def timeframe_seconds(tf):
    """TF의 대표 구간 초를 반환한다. 월봉은 최신성 계산용 30일 대표값만 사용한다."""
    match = re.fullmatch(r"(\d+)(m|h|d|w|M)", tf or "")
    if not match:
        return None
    value, unit = int(match.group(1)), match.group(2)
    return value * {"m": 60, "h": 3600, "d": 86400, "w": 604800, "M": 2592000}[unit]


def _calendar_month_delta(timestamp_a_ms, timestamp_b_ms):
    a = datetime.fromtimestamp(timestamp_a_ms / 1000, timezone.utc)
    b = datetime.fromtimestamp(timestamp_b_ms / 1000, timezone.utc)
    return (b.year - a.year) * 12 + (b.month - a.month)


def validate_ohlcv_quality(ohlcv, tf, collected_at_utc):
    """원천 배열을 바꾸지 않고 적격/결손 상태와 근거만 반환한다."""
    reasons = []
    expected_seconds = timeframe_seconds(tf)
    quality = {
        "tf": tf,
        "received_bars": len(ohlcv) if ohlcv else 0,
        "completed_bars": max((len(ohlcv) if ohlcv else 0) - 1, 0),
        "last_source_timestamp_ms": None,
        "expected_interval_seconds": expected_seconds,
        "standard_target_completed_bars": DATA_MIN_COMPLETED_BARS,
        "status": "적격",
        "history_note": "",
        "reasons": reasons,
    }
    if DATA_MIN_COMPLETED_BARS is None or OHLCV_STALE_INTERVALS is None or expected_seconds is None:
        reasons.append("품질 SSOT 또는 TF 구간 해석 불가")
        quality["status"] = "데이터결손/판정불가"
        return quality
    if not ohlcv:
        reasons.append("OHLCV 배열 비어 있음")
        quality["status"] = "데이터결손/판정불가"
        return quality

    try:
        frame = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
        numeric = frame.astype(float)
    except Exception:
        reasons.append("OHLCV 6필드 숫자 배열 파싱 실패")
        quality["status"] = "데이터결손/판정불가"
        return quality

    if not np.isfinite(numeric.to_numpy()).all():
        reasons.append("비유한 OHLCV 값 존재")
    timestamps = numeric["timestamp"].to_numpy()
    # timestamp가 NaN/±inf이면 int()·날짜 변환이 불가능하다. 이후 예외로 전체 PHASE 1을 중단하지 않고
    # 해당 TF만 품질결손으로 확정한다. 가격·거래량의 비유한값은 아래 reasons로 동일하게 결손 처리된다.
    if not np.isfinite(timestamps).all():
        quality["status"] = "데이터결손/판정불가"
        return quality
    diffs = np.diff(timestamps)
    if np.any(diffs <= 0):
        reasons.append("timestamp 중복 또는 역행")
    expected_ms = expected_seconds * 1000
    if tf.endswith("M"):
        expected_months = int(tf[:-1])
        if len(timestamps) > 1 and any(
            _calendar_month_delta(timestamps[i - 1], timestamps[i]) != expected_months
            for i in range(1, len(timestamps))
        ):
            reasons.append("기대 월봉 캘린더 간격 결손 또는 불일치")
    elif len(diffs) and np.any(diffs != expected_ms):
        reasons.append("기대 TF 간격 결손 또는 불일치")
    if (numeric["volume"] < 0).any():
        reasons.append("음수 거래량 존재")
    if (numeric["high"] < numeric[["open", "close", "low"]].max(axis=1)).any() or (numeric["low"] > numeric[["open", "close", "high"]].min(axis=1)).any():
        reasons.append("OHLC 고저가 논리 오류")

    last_timestamp = int(timestamps[-1])
    quality["last_source_timestamp_ms"] = last_timestamp
    age_ms = int(collected_at_utc.timestamp() * 1000) - last_timestamp
    quality["latest_age_seconds"] = age_ms / 1000.0
    if age_ms > OHLCV_STALE_INTERVALS * expected_ms:
        reasons.append("마지막 원천봉 최신성 결손")
    # DATA_MIN_COMPLETED_BARS는 표준 수집 목표값이다. 신규 상장처럼 정상·연속·최신인
    # 가용 완성봉이 목표보다 적어도 거래 시작 후 확보된 전량으로 동일 분석을 진행한다.
    if quality["completed_bars"] < DATA_MIN_COMPLETED_BARS:
        quality["status"] = "가용봉 분석"
        quality["history_note"] = (
            f"표준 수집 목표 {DATA_MIN_COMPLETED_BARS}봉 미달; "
            f"거래 시작 후 확보 가능한 완성봉 {quality['completed_bars']}개 전체 사용"
        )

    # 실제 원천 무결성 오류만 분석 제외 상태로 전환한다.
    if reasons:
        quality["status"] = "데이터결손/판정불가"
    return quality


def format_ohlcv_quality(quality):
    detail = "; ".join(quality["reasons"]) if quality["reasons"] else (quality.get("history_note") or "검사 통과")
    last_ts = quality["last_source_timestamp_ms"]
    last_text = datetime.fromtimestamp(last_ts / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC") if last_ts else "없음"
    return (
        f"상태: {quality['status']} | 수신봉: {quality['received_bars']} | 완성봉: {quality['completed_bars']} | "
        f"마지막 원천봉: {last_text} | 근거: {detail}"
    )


def build_phase1_canonical(exchange, symbol, raw_payload, tf_quality, snapshot_status, snapshot_span_seconds, plugins):
    """PHASE 1의 관측 사실만 고정한다. 해석 점수·방향·가격은 포함하지 않는다."""
    canonical = {
        "schema_version": "PHASE1_CANONICAL_V1",
        "provenance": {
            "exchange": exchange,
            "symbol": symbol,
            "captured_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "raw_payload_sha256": hashlib.sha256(raw_payload.encode("utf-8")).hexdigest(),
        },
        "data_quality": {"timeframes": tf_quality, "snapshot_status": snapshot_status, "snapshot_span_seconds": snapshot_span_seconds},
        "plugins": plugins,
        "observations_payload": raw_payload,
    }
    return canonical


PHASE1_FACT_LABELS = (
    "현재가", "구조 상태", "현재 진행 봉", "구조 돌파", "최근 3봉 기하학 및 시퀀스",
    "단일/연속 캔들 패턴", "다중 스윙 및 채널 패턴", "거래량 배율 및 감속 추세",
    "가격 공백대", "세력 매물대", "중첩 매물대", "박스 / 수렴 여부",
    "다이버전스 / 추세 건전성", "RSI 및 모멘텀", "F1~F4 역학 코드",
)


def build_phase1_fact_registry(phase1_result):
    """이미 표시할 PHASE 1 카드에서 TF별 핵심 사실을 추가 모델 호출 없이 추출한다."""
    registry = {}
    text = str(phase1_result or "")
    section_pattern = r"(?ms)^🔹\s*([^\n]+)\n(.*?)(?=^🔹\s*|\Z)"
    for section_match in re.finditer(section_pattern, text):
        tf = section_match.group(1).strip()
        body = section_match.group(2)
        for label in PHASE1_FACT_LABELS:
            fact_match = re.search(rf"(?m)^{re.escape(label)}\s*\n([^\n]+)", body)
            if not fact_match:
                continue
            fact_ref = f"{tf}:{label}"
            registry[fact_ref] = {"fact_ref": fact_ref, "tf": tf, "label": label, "value": fact_match.group(1).strip()}
    return registry


def validate_phase1_fact_registry(registry):
    if not isinstance(registry, dict) or not registry:
        return ["PHASE1 사실 registry 누락"]
    for fact_ref, item in registry.items():
        if not isinstance(item, dict) or item.get("fact_ref") != fact_ref or not all(item.get(key) for key in ("tf", "label", "value")):
            return ["PHASE1 사실 registry 형식 오류"]
    return []


def validate_phase1_canonical(canonical):
    required = {"schema_version", "provenance", "data_quality", "plugins", "observations_payload"}
    if not isinstance(canonical, dict) or set(canonical) != required:
        return ["PHASE1 canonical 스키마 오류"]
    provenance = canonical.get("provenance", {})
    if not all(provenance.get(field) for field in ("exchange", "symbol", "captured_at_utc", "raw_payload_sha256")):
        return ["PHASE1 canonical provenance 누락"]
    if hashlib.sha256(canonical["observations_payload"].encode("utf-8")).hexdigest() != provenance["raw_payload_sha256"]:
        return ["PHASE1 canonical 원천해시 불일치"]
    return []


# ============================================================
# PHASE 2 입력 provenance
# - 카드 자연어와 STAGE 0 canonical은 역할이 다르다. 이 record는 원천 관측을 모델에
#   재전송하지 않으며, 어떤 PHASE 1 결과·원천해시·품질상태가 PHASE 2에 연결됐는지만 고정한다.
# - raw payload 전체를 다시 모델에 넣으면 token budget과 계산 계약이 달라지므로, 그 변경은
#   별도 동등성 검증 전까지 금지한다.
# ============================================================
PHASE2_INPUT_PROVENANCE_SCHEMA = "PHASE2_INPUT_PROVENANCE_V2"


def build_phase2_input_provenance(phase1_result, phase1_canonical):
    """PHASE 2가 참조하는 PHASE 1 결과·원천 관측의 비식별 immutable fingerprint를 만든다."""
    canonical = phase1_canonical or {}
    provenance = canonical.get("provenance", {}) if isinstance(canonical, dict) else {}
    quality = canonical.get("data_quality", {}) if isinstance(canonical, dict) else {}
    tf_quality = quality.get("timeframes", []) if isinstance(quality, dict) else []
    quality_summary = []
    for item in tf_quality if isinstance(tf_quality, list) else []:
        if isinstance(item, dict):
            quality_summary.append({"tf": str(item.get("tf", "")), "status": str(item.get("status", ""))})
    fact_registry = build_phase1_fact_registry(phase1_result)
    fact_registry_json = json.dumps(fact_registry, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "schema_version": PHASE2_INPUT_PROVENANCE_SCHEMA,
        "phase1_result_sha256": hashlib.sha256(str(phase1_result or "").encode("utf-8")).hexdigest(),
        "phase1_result_chars": len(str(phase1_result or "")),
        "canonical_schema_version": str(canonical.get("schema_version", "")) if isinstance(canonical, dict) else "",
        "raw_payload_sha256": str(provenance.get("raw_payload_sha256", "")),
        "exchange": str(provenance.get("exchange", "")),
        "symbol": str(provenance.get("symbol", "")),
        "tf_quality": quality_summary,
        "fact_registry_sha256": hashlib.sha256(fact_registry_json.encode("utf-8")).hexdigest(),
        "fact_registry_count": len(fact_registry),
        "model_input_mode": "phase1_result_plus_canonical_provenance_only",
    }


def validate_phase2_input_provenance(record, phase1_result, phase1_canonical):
    """저장된 provenance가 현재 PHASE 1 결과·canonical과 정확히 같은지 확인한다."""
    expected = build_phase2_input_provenance(phase1_result, phase1_canonical)
    if not isinstance(record, dict):
        return ["P2I01 입력 provenance 누락"]
    if set(record) != set(expected):
        return ["P2I02 입력 provenance 스키마 불일치"]
    mismatches = [key for key, expected_value in expected.items() if record.get(key) != expected_value]
    if mismatches:
        return ["P2I03 입력 provenance 불일치: " + ", ".join(sorted(mismatches))]
    return []


def _phase2_session_hash(session_id):
    if not session_id:
        return ""
    return hashlib.sha256(str(session_id).encode("utf-8")).hexdigest()[:12]


def log_phase2_observation(event, input_provenance=None, session_id=None, **details):
    """원천 데이터·브리핑을 복제하지 않는 PHASE 2 운영 관측 로그."""
    record = {
        "event": str(event),
        "session": _phase2_session_hash(session_id),
        "input_schema": (input_provenance or {}).get("schema_version", ""),
        "raw_payload_sha256": (input_provenance or {}).get("raw_payload_sha256", ""),
    }
    record.update(details)
    print("[PHASE2_OBS] " + json.dumps(record, ensure_ascii=False, sort_keys=True))


def resample_daily_to_weekly(ohlcv_1d):
    """일봉 OHLCV 배열을 주봉으로 직접 집계한다.
    많은 거래소가 REST API로는 1w 캔들을 따로 제공하지 않고(앱 차트는 프론트엔드가
    일봉을 묶어서 그림), ccxt의 exchange.timeframes에도 1w가 빠져있는 경우가 많다.
    동일한 방식(일봉 7개 묶음)으로 직접 재현하여 앱에서 보는 주봉과 일치시킨다."""
    if not ohlcv_1d:
        return []
    df = pd.DataFrame(ohlcv_1d, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["dt"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.set_index("dt")
    weekly = df.resample("W-MON", label="left", closed="left").agg({
        "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum",
    }).dropna()
    source_days = df["close"].resample("W-MON", label="left", closed="left").count()
    # 수집 시작점이 주중이면 첫 버킷은 7일을 대표하지 못한다. 마지막 버킷은 진행 주봉으로 유지한다.
    if not weekly.empty and source_days.loc[weekly.index[0]] < 7:
        weekly = weekly.iloc[1:]
    if weekly.empty:
        return []
    weekly["timestamp"] = weekly.index.map(lambda x: int(x.timestamp() * 1000))
    return weekly.reset_index(drop=True)[["timestamp", "open", "high", "low", "close", "volume"]].values.tolist()


# ============================================================
# [결함수정] 캔들 형태(도지/망치형/유성형/장악형/관통형·먹구름형) 순수 기하학 사전계산
# - 근거: 실사용 감사(STORJ/KRW 분석)에서 Gemini가 이미 주어진 고가/저가/현재가만으로
#   추론 가능한 꼬리방향을 반대로 서술하는 오류가 반복 확인됨(1w·1d 동일 오류 재현).
#   단일비교(평형가/Premium-Discount/RSI임계값)는 12/12 정확했으나, 다중요소 종합판단
#   (캔들형태)만 반복 실패 — 계산 자체를 Python으로 사전확정하여 서술오류를 원천 차단한다.
# - 스펙(CandleView_API_V003.txt 7장 "캔들 기하학" + 13장 용어사전) 수치기준 그대로 반영.
# - 여기서는 순수 형태(모양) 사실만 판정한다. 그 형태가 스윙 고점/저점 중 어디 근처인지에
#   따른 컨텍스트 라벨링(망치형=저점 매수신호 vs 교수형=고점 매도신호)은 스윙판단이 필요하므로
#   Gemini의 몫으로 남겨둔다(Python은 사실만, 해석은 Gemini — 관측/해석 분리 원칙 준수).
# ============================================================
BODY_RATIO_DOJI = 10.0
BODY_RATIO_HAMMER_STAR = 30.0
WICK_DOMINANT_RATIO = 200.0 / 3.0  # 2/3 = 66.67%
PENETRATION_MIN = 54.0


def classify_candle_shape(o, h, l, c, prev_o=None, prev_h=None, prev_l=None, prev_c=None):
    """캔들 1~2개의 O/H/L/C만으로 판정 가능한 순수 기하학적 형태를 반환한다.
    반환값은 태그 문자열 리스트(복수 매칭 가능, 예: 장악형+망치형 동시 성립)."
    """
    rng = h - l
    if rng <= 0:
        return []
    body = abs(c - o)
    body_pct = body / rng * 100.0
    body_low = min(o, c)
    body_high = max(o, c)
    lower_wick_pct = (body_low - l) / rng * 100.0
    upper_wick_pct = (h - body_high) / rng * 100.0
    body_in_upper_third = body_low >= (l + rng * 2.0 / 3.0)
    body_in_lower_third = body_high <= (l + rng / 3.0)

    is_hammer_shape = (body_pct <= BODY_RATIO_HAMMER_STAR and body_in_upper_third
                        and lower_wick_pct >= WICK_DOMINANT_RATIO)
    is_star_shape = (body_pct <= BODY_RATIO_HAMMER_STAR and body_in_lower_third
                      and upper_wick_pct >= WICK_DOMINANT_RATIO)
    is_doji = body_pct <= BODY_RATIO_DOJI

    tags = []
    # [SSOT] 스펙 중복매칭 우선순위(구체성 원칙): 망치형/유성형(조건3개)이 도지(조건1개)보다 우선
    if is_hammer_shape:
        tags.append(f"형태:망치형/교수형계열(몸통{body_pct:.1f}%,아래꼬리{lower_wick_pct:.1f}%,고점/저점근접여부는별도판단)")
    elif is_star_shape:
        tags.append(f"형태:유성형계열(몸통{body_pct:.1f}%,윗꼬리{upper_wick_pct:.1f}%)")
    elif is_doji:
        tags.append(f"형태:도지(몸통{body_pct:.1f}%)")
    else:
        # 꼬리방향 자체는 항상 명시(형태분류에 못 미쳐도 서술오류 방지에 유용)
        if upper_wick_pct > lower_wick_pct * 1.5 and upper_wick_pct >= 40.0:
            tags.append(f"꼬리:윗꼬리우세({upper_wick_pct:.1f}%)")
        elif lower_wick_pct > upper_wick_pct * 1.5 and lower_wick_pct >= 40.0:
            tags.append(f"꼬리:아래꼬리우세({lower_wick_pct:.1f}%)")

    if prev_o is not None and prev_c is not None:
        prev_body = abs(prev_c - prev_o)
        if prev_body > 0:
            # 장악형(Engulfing): 몸통≥직전100% + 반대방향 + 직전몸통을 완전히 감쌈(13장 용어사전 정의 결합)
            if body >= prev_body:
                bullish_engulf = (c > o and prev_c < prev_o and o <= prev_c and c >= prev_o)
                bearish_engulf = (c < o and prev_c > prev_o and o >= prev_c and c <= prev_o)
                if bullish_engulf or bearish_engulf:
                    tags.append(f"형태:장악형(직전몸통대비{body/prev_body*100:.0f}%)")
            # 관통형(Piercing, 직전음봉->현재양봉) / 먹구름형(Dark Cloud, 직전양봉->현재음봉)
            if prev_c < prev_o and c > o:
                pen = (c - prev_c) / prev_body * 100.0
                if pen > PENETRATION_MIN:
                    tags.append(f"형태:관통형(직전몸통{pen:.1f}%침투)")
            elif prev_c > prev_o and c < o:
                pen = (prev_c - c) / prev_body * 100.0
                if pen > PENETRATION_MIN:
                    tags.append(f"형태:먹구름형(직전몸통{pen:.1f}%침투)")
    return tags



# ============================================================
# V002-15 Plugin 7/8/9 보조 데이터 수집
# - 미지원·오류 시 조용히 생략 (데이터결손 태그로 엔진에 전달)
# - 원본 배열 전체 주입 금지: 요약 스칼라·소량 태그만 payload에 추가
# ============================================================
WALL_RANGE_PCT = 5.0
WALL_MAX_COUNT = 2
WALL_MIN_SIZE_RATIO = 2.0
OI_HISTORY_LIMIT = 30


def _safe_float(x, default=None):
    try:
        if x is None:
            return default
        return float(x)
    except Exception:
        return default


def fetch_volume_delta_summary(exchange, symbol, timeframe, limit=120):
    """Plugin 7: Taker Buy/Sell → Delta 요약.
    우선순위: (1) 거래소 kline 확장 필드 (2) 최근 체결 집계 근사 (3) 결손
    반환 dict 또는 None
    """
    ex_id = getattr(exchange, "id", "")

    # [결함수정 — V003[C] 8항 소급감사] 스펙 5장 Plugin7 "5. 데이터 가용성 구분(V002-16 신설)"이
    # 업비트/빗썸을 Taker Buy/Sell 분리거래량 미제공 거래소로 명시하여 [거래소 미지원] 자동
    # 비활성 처리를 규정하는데, 기존 코드는 이 예외를 두지 않고 아래 제네릭 체결근사 분기로
    # 빠져 신뢰할 수 없는 근사값을 "정상(status=ok)"으로 그대로 내보내고 있었다(실사용에서
    # 효과가 기대에 못 미친다는 지적으로 확인됨). 여기서 명시적으로 조기 반환한다.
    if ex_id not in ("binance", "binanceusdm", "binancecoinm"):
        return None

    try:
        # --- Binance spot/usdm: kline에 taker buy base volume 포함 ---
        if ex_id in ("binance", "binanceusdm", "binancecoinm"):
            market = exchange.market(symbol)
            raw_symbol = market.get("id", symbol.replace("/", ""))
            if ex_id == "binance":
                rows = exchange.publicGetKlines({"symbol": raw_symbol, "interval": exchange.timeframes.get(timeframe, timeframe), "limit": limit})
            else:
                # futures
                rows = exchange.fapiPublicGetKlines({"symbol": raw_symbol, "interval": exchange.timeframes.get(timeframe, timeframe), "limit": limit}) if hasattr(exchange, "fapiPublicGetKlines") else None
                if rows is None and hasattr(exchange, "publicGetKlines"):
                    rows = exchange.publicGetKlines({"symbol": raw_symbol, "interval": exchange.timeframes.get(timeframe, timeframe), "limit": limit})
            if not rows:
                return None
            # binance kline: [0]open time [1]o [2]h [3]l [4]c [5]vol [6]close time [7]quote vol [8]trades [9]taker buy base [10]taker buy quote
            recent = rows[-min(5, len(rows)):]
            lines = []
            last_ratio = None
            for r in recent:
                vol = _safe_float(r[5], 0.0) or 0.0
                tb = _safe_float(r[9], 0.0) or 0.0
                sell = max(vol - tb, 0.0)
                delta = tb - sell
                ratio = delta / (vol + 1e-8) if vol > 0 else 0.0
                last_ratio = ratio
                lines.append(f"Delta={delta:.4f} Ratio={ratio:+.4f} (Buy={tb:.4f} Sell={sell:.4f} Vol={vol:.4f})")
            return {
                "status": "ok",
                "source": "binance_kline_taker",
                "last_delta_ratio": last_ratio,
                "lines": lines,
            }

        # --- Bybit linear: try v5 kline + optional buy/sell if present in info ---
        if ex_id in ("bybit",):
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
            # bybit unified often lacks taker split in OHLCV; approximate via recent trades window
            trades = exchange.fetch_trades(symbol, limit=200)
            buy_v = sell_v = 0.0
            for t in trades:
                amt = _safe_float(t.get("amount"), 0.0) or 0.0
                side = (t.get("side") or "").lower()
                if side in ("buy", "b"):
                    buy_v += amt
                else:
                    sell_v += amt
            total = buy_v + sell_v
            if total <= 0:
                return None
            delta = buy_v - sell_v
            ratio = delta / (total + 1e-8)
            return {
                "status": "ok",
                "source": "bybit_trades_approx",
                "last_delta_ratio": ratio,
                "lines": [f"Delta={delta:.4f} Ratio={ratio:+.4f} (Buy={buy_v:.4f} Sell={sell_v:.4f} Vol={total:.4f}) [최근체결근사]"],
            }

        # --- Generic: trades approximation ---
        if exchange.has.get("fetchTrades"):
            trades = exchange.fetch_trades(symbol, limit=150)
            buy_v = sell_v = 0.0
            for t in trades:
                amt = _safe_float(t.get("amount"), 0.0) or 0.0
                side = (t.get("side") or "").lower()
                if side in ("buy", "b"):
                    buy_v += amt
                else:
                    sell_v += amt
            total = buy_v + sell_v
            if total <= 0:
                return None
            delta = buy_v - sell_v
            ratio = delta / (total + 1e-8)
            return {
                "status": "ok",
                "source": f"{ex_id}_trades_approx",
                "last_delta_ratio": ratio,
                "lines": [f"Delta={delta:.4f} Ratio={ratio:+.4f} (Buy={buy_v:.4f} Sell={sell_v:.4f}) [최근체결근사]"],
            }
    except Exception as e:
        print(f"[WARN] Volume Delta 수집 실패 ({ex_id} {symbol}): {e}")
    return None


# ============================================================
# OI 교차거래소 조회 기능 정지 스위치
# 사유: 현재 지원 3개 거래소(업비트/빗썸/코인베이스)는 전부 순수 현물마켓이라
# 자체 OI가 원천적으로 존재하지 않는다(미결제약정은 선물/무기한계약 전용 개념).
# 유일한 경로였던 교차거래소 조회(바이낸스/바이비트/OKX)도 Render(AWS US)
# 환경에서 전부 미국IP 지오블록 대상이라 매 시도가 실패하며 최대 8초
# (2곳×4초) 불필요한 지연만 유발한다. 코드(V003 Plugin8 설계)는 그대로
# 보존하고 실행만 정지한다 — 프록시 구축 등으로 지오블록이 해소되면
# 아래 값을 True로 바꾸는 것만으로 즉시 재사용 가능하다.
# ============================================================
OI_CROSS_EXCHANGE_ENABLED = False


def fetch_oi_summary(exchange, symbol, primary_ex_id):
    """Plugin 8: OI 변동률 스칼라 3~4개만.
    현물 전용 거래소는 선물 심볼로 교차 조회 시도.
    폴백 순서: primary → 심볼변형 → Binance USDM → Bybit → OKX
    (교차거래소 조회 단계는 OI_CROSS_EXCHANGE_ENABLED 스위치로 정지 가능)
    """
    tried = []

    def _oi_from(ex, sym):
        try:
            if not ex.has.get("fetchOpenInterest") and not hasattr(ex, "fetch_open_interest"):
                if hasattr(ex, "fetch_open_interest_history"):
                    hist = ex.fetch_open_interest_history(sym, timeframe="1h", limit=OI_HISTORY_LIMIT)
                    if not hist or len(hist) < 2:
                        return None
                    vals = []
                    for h in hist[-5:]:
                        v = _safe_float(h.get("openInterestAmount") or h.get("openInterestValue") or h.get("openInterest"))
                        if v is not None:
                            vals.append(v)
                    if len(vals) < 2:
                        return None
                    changes = []
                    for i in range(1, len(vals)):
                        prev = vals[i - 1]
                        cur = vals[i]
                        pct = ((cur - prev) / prev * 100.0) if prev else 0.0
                        changes.append(pct)
                    return {
                        "source": getattr(ex, "id", "?"),
                        "last_oi": vals[-1],
                        "change_pcts": changes[-4:],
                        "latest_change_pct": changes[-1] if changes else None,
                    }
                return None

            oi = ex.fetch_open_interest(sym)
            val = _safe_float(
                oi.get("openInterestAmount")
                or oi.get("openInterestValue")
                or oi.get("openInterest")
                or (oi.get("info") or {}).get("openInterest")
            )
            change_pcts = []
            if hasattr(ex, "fetch_open_interest_history"):
                try:
                    hist = ex.fetch_open_interest_history(sym, timeframe="1h", limit=OI_HISTORY_LIMIT)
                    vals = []
                    for h in hist[-6:]:
                        v = _safe_float(h.get("openInterestAmount") or h.get("openInterestValue") or h.get("openInterest"))
                        if v is not None:
                            vals.append(v)
                    for i in range(1, len(vals)):
                        prev = vals[i - 1]
                        if prev:
                            change_pcts.append((vals[i] - prev) / prev * 100.0)
                except Exception:
                    pass
            return {
                "source": getattr(ex, "id", "?"),
                "last_oi": val,
                "change_pcts": change_pcts[-4:],
                "latest_change_pct": change_pcts[-1] if change_pcts else None,
            }
        except Exception as e:
            print(f"[WARN] OI 조회 실패 ({getattr(ex, 'id', '?')} {sym}): {e}")
            return None

    # 현재 지원 거래소는 현물 전용이며 OI는 원천적으로 존재하지 않는다. 교차거래소 경로가
    # 운영상 정지된 경우에도 원거래소의 미지원 fetchOpenInterest를 호출하면 반복 경고와
    # 불필요한 예외만 생긴다. Layer 5 출력에는 기존 missing 상태만 전달한다.
    if not OI_CROSS_EXCHANGE_ENABLED:
        return {"status": "missing", "tried": [], "reason": "cross_exchange_paused"}

    base = symbol.split("/")[0] if "/" in symbol else symbol

    # 1) primary exchange
    tried.append(primary_ex_id)
    data = _oi_from(exchange, symbol)
    if data and (data.get("latest_change_pct") is not None or data.get("last_oi") is not None):
        return {"status": "ok", **data}

    # 2) 심볼 변형 시도 (primary)
    for alt in (f"{base}/USDT:USDT", f"{base}/USDT:USDT-USDT", f"{base}/USDT"):
        if alt == symbol:
            continue
        data = _oi_from(exchange, alt)
        if data and data.get("latest_change_pct") is not None:
            return {"status": "ok", **data, "symbol_used": alt}

    # 3) Binance USDM 교차
    if OI_CROSS_EXCHANGE_ENABLED and primary_ex_id not in ("binanceusdm", "binance"):
        try:
            bx = ccxt.binanceusdm({"enableRateLimit": True, "timeout": 4000})
            bx.load_markets()
            tried.append("binanceusdm")
            for sym in (f"{base}/USDT:USDT", f"{base}/USDT"):
                data = _oi_from(bx, sym)
                if data and (data.get("latest_change_pct") is not None or data.get("last_oi") is not None):
                    return {"status": "ok", **data, "symbol_used": sym, "cross_exchange": True}
        except Exception as e:
            print(f"[WARN] Binance OI 교차조회 실패: {e}")

    # 4) Bybit linear 교차
    if OI_CROSS_EXCHANGE_ENABLED and primary_ex_id not in ("bybit",):
        try:
            by = ccxt.bybit({
                "enableRateLimit": True,
                "timeout": 4000,
                "options": {"defaultType": "linear"}
            })
            by.load_markets()
            tried.append("bybit")
            for sym in (f"{base}/USDT:USDT", f"{base}/USDT"):
                data = _oi_from(by, sym)
                if data and (data.get("latest_change_pct") is not None or data.get("last_oi") is not None):
                    return {"status": "ok", **data, "symbol_used": sym, "cross_exchange": True}
        except Exception as e:
            print(f"[WARN] Bybit OI 교차조회 실패: {e}")

    # 5) OKX 교차
    if OI_CROSS_EXCHANGE_ENABLED and primary_ex_id not in ("okx",):
        try:
            ox = ccxt.okx({
                "enableRateLimit": True,
                "timeout": 4000,
                "options": {"defaultType": "swap"}
            })
            ox.load_markets()
            tried.append("okx")
            for sym in (f"{base}/USDT:USDT", f"{base}/USDT"):
                data = _oi_from(ox, sym)
                if data and (data.get("latest_change_pct") is not None or data.get("last_oi") is not None):
                    return {"status": "ok", **data, "symbol_used": sym, "cross_exchange": True}
        except Exception as e:
            print(f"[WARN] OKX OI 교차조회 실패: {e}")

    return {"status": "missing", "tried": tried}


def fetch_whale_wall_summary(exchange, symbol, last_price):
    """Plugin 9: 호가 요약 — 현재가 ±WALL_RANGE_PCT 내 상위 벽 소수만."""
    try:
        if not exchange.has.get("fetchOrderBook"):
            return None
        book = exchange.fetch_order_book(symbol, limit=50)
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        if not last_price or last_price <= 0:
            last_price = bids[0][0] if bids else (asks[0][0] if asks else None)
        if not last_price:
            return None

        lo = last_price * (1 - WALL_RANGE_PCT / 100.0)
        hi = last_price * (1 + WALL_RANGE_PCT / 100.0)

        def walls(levels, side):
            """동일 방향·탐색 범위 호가 평균 대비 상대강도(R_wall)를 계산한다.
            기존 WALL_MIN_SIZE_RATIO는 유의미 벽의 최소 통과선으로 유지하고,
            통과 후보는 절대 잔량이 아닌 R_wall 우선으로 정렬한다."""
            sized = []
            sizes = []
            for price, amt in levels:
                p = _safe_float(price)
                a = _safe_float(amt, 0.0) or 0.0
                if p is None:
                    continue
                if side == "bid" and lo <= p <= last_price:
                    sized.append((p, a))
                    sizes.append(a)
                if side == "ask" and last_price <= p <= hi:
                    sized.append((p, a))
                    sizes.append(a)
            if not sized:
                return []
            avg = sum(sizes) / len(sizes) if sizes else 0.0
            if avg <= 0:
                return []
            strong = [
                {"price": p, "amount": a, "relative_strength": a / avg}
                for p, a in sized
                if a >= avg * WALL_MIN_SIZE_RATIO
            ]
            # 동일한 최소 통과선 안에서는 평균 대비 비정상성(R_wall)을 우선한다.
            # R_wall 동률일 때만 절대 잔량이 큰 후보를 먼저 둔다.
            strong.sort(key=lambda x: (x["relative_strength"], x["amount"]), reverse=True)
            return strong[:WALL_MAX_COUNT]

        bid_walls = walls(bids, "bid")
        ask_walls = walls(asks, "ask")
        if not bid_walls and not ask_walls:
            return {"status": "ok", "lines": ["범위 내 유의미 Whale Wall 없음"], "last_price": last_price}

        lines = []
        for wall in bid_walls:
            p, a, r = wall["price"], wall["amount"], wall["relative_strength"]
            lines.append(
                f"매수벽 {p} 잔량={a:.4f} | R_wall={r:.2f}x(동일방향 평균 대비, "
                f"최소 {WALL_MIN_SIZE_RATIO:.1f}x 통과) | 현재가 대비 {(p/last_price-1)*100:+.2f}%"
            )
        for wall in ask_walls:
            p, a, r = wall["price"], wall["amount"], wall["relative_strength"]
            lines.append(
                f"매도벽 {p} 잔량={a:.4f} | R_wall={r:.2f}x(동일방향 평균 대비, "
                f"최소 {WALL_MIN_SIZE_RATIO:.1f}x 통과) | 현재가 대비 {(p/last_price-1)*100:+.2f}%"
            )
        return {"status": "ok", "lines": lines, "last_price": last_price, "bid_walls": bid_walls, "ask_walls": ask_walls}
    except Exception as e:
        print(f"[WARN] Whale Wall 수집 실패 ({symbol}): {e}")
        return None


def format_plugin_payload(oi_info, wall_info):
    """엔진 STAGE 0 / Plugin 8·9(TF무관 1회성)가 파싱하기 쉬운 요약 블록.
    Volume Delta(Plugin 7)는 TF별로 다르므로 run_phase1의 TF 루프 내에서 개별 첨부한다."""
    parts = ["\n[API 플러그인 보조 데이터 — V003 Plugin 8/9, TF무관 1회성 스냅샷]"]

    # OI
    parts.append("• Open Interest (Plugin 8):")
    if oi_info and oi_info.get("status") == "ok":
        src = oi_info.get("source", "?")
        if oi_info.get("cross_exchange"):
            src += " (교차거래소)"
        parts.append(f"  - source: {src}")
        if oi_info.get("symbol_used"):
            parts.append(f"  - symbol_used: {oi_info['symbol_used']}")
        if oi_info.get("last_oi") is not None:
            parts.append(f"  - last_OI: {oi_info['last_oi']}")
        pcts = oi_info.get("change_pcts") or []
        if pcts:
            parts.append("  - OI_change_pct(최근→과거순 최대4개): " + ", ".join(f"{p:+.2f}%" for p in pcts))
        if oi_info.get("latest_change_pct") is not None:
            parts.append(f"  - latest_OI_change_pct: {oi_info['latest_change_pct']:+.2f}%")
    else:
        reason = (oi_info or {}).get("reason")
        if reason == "cross_exchange_paused":
            parts.append("  - [해당없음/기능정지] 현물마켓 자체 OI 없음, 교차거래소 조회는 지오블록으로 정지 상태")
        else:
            tried = (oi_info or {}).get("tried") or []
            parts.append("  - [데이터결손] Open Interest 미지원 또는 수집 실패" + (f" tried={tried}" if tried else ""))

    # Wall
    parts.append("• Whale Wall (Plugin 9):")
    if wall_info and wall_info.get("status") == "ok":
        parts.append(f"  - ref_price: {wall_info.get('last_price')}")
        parts.append(f"  - range: ±{WALL_RANGE_PCT}%")
        for ln in wall_info.get("lines") or []:
            parts.append(f"  - {ln}")
    else:
        parts.append("  - [데이터결손] Order book 미지원 또는 수집 실패")

    parts.append("")
    return "\n".join(parts)


def format_supplement_display(supplement, symbol, exchange_display):
    """보간지표(Plugin 7/8/9 원시데이터) 텔레그램 표시용 포맷.
    Gemini를 거치지 않고 main.py가 수집한 원시데이터를 그대로 정돈해 보여준다
    (환각 위험 없음, 별도 API 호출 없어 즉시 응답)."""
    if not supplement:
        return "보간지표 데이터가 없습니다."

    lines = [f"<b>CandleView — 보간지표 (Plugin 7·8·9 원시데이터)</b>", f"{exchange_display} {symbol}", ""]

    lines.append("<b>📶 체결강도 Volume Delta (TF별)</b>")
    lines.append("<pre>")
    for item in supplement.get("delta_list", []):
        tf = item.get("tf", "?")
        info = item.get("info")
        if info and info.get("status") == "ok":
            lines.append(f"{tf:>5} | Ratio {info.get('last_delta_ratio'):+.4f} | {info.get('source')}")
        else:
            lines.append(f"{tf:>5} | 거래소 미지원 또는 데이터결손")
    lines.append("</pre>")

    lines.append("")
    lines.append("<b>📊 미결제약정 Open Interest</b>")
    lines.append("<pre>")
    oi = supplement.get("oi")
    if oi and oi.get("status") == "ok":
        src = oi.get("source", "?")
        if oi.get("cross_exchange"):
            src += " (교차거래소 참고)"
        lines.append(f"출처: {src}")
        if oi.get("symbol_used"):
            lines.append(f"조회심볼: {oi['symbol_used']}")
        if oi.get("latest_change_pct") is not None:
            lines.append(f"최근 변동률: {oi['latest_change_pct']:+.2f}%")
        pcts = oi.get("change_pcts") or []
        if pcts:
            lines.append("변동추이: " + ", ".join(f"{p:+.2f}%" for p in pcts))
    else:
        if oi and oi.get("reason") == "cross_exchange_paused":
            lines.append("[해당없음/기능정지]")
            lines.append("현물마켓 자체 OI 없음, 교차거래소 조회는 지오블록으로 정지 상태")
            lines.append("(추후 프록시 구축시 재개 예정)")
        else:
            lines.append("해당없음 (현물마켓 또는 조회 실패)")
    lines.append("</pre>")

    lines.append("")
    lines.append("<b>🧱 호가 매물벽 Whale Wall</b>")
    lines.append("<pre>")
    wall = supplement.get("wall")
    if wall and wall.get("status") == "ok":
        for ln in wall.get("lines", []):
            lines.append(ln)
    else:
        lines.append("데이터결손 또는 호가조회 실패")
    lines.append("</pre>")

    return "\n".join(lines)


# ============================================================
# 고정 승인 모델 운영 — 분석 수식·STAGE 0·Layer 5와 분리된 실행 인프라
# ============================================================
# 자동 탐색·자동 승인·상태 캐시를 사용하지 않는다. 아래 순서는 수동 승인된 실행 순서다.
STATIC_APPROVED_MODELS = [
    {"model_id": "gemini-3.7-flash", "selection_source": "고정 승인 1차", "rank": 0},
    {"model_id": "gemini-3.6-flash", "selection_source": "고정 승인 fallback", "rank": 1},
]


def _model_url(model_id):
    return f"https://generativelanguage.googleapis.com/v1beta/models/{model_id}:generateContent"


def _model_id_from_url(url):
    match = re.search(r"/models/([^:/]+):generateContent$", url or "")
    return match.group(1) if match else ""


def _friendly_model_name(model_id):
    match = re.fullmatch(r"gemini-(\d+(?:\.\d+)+)-(flash(?:-lite)?)", model_id or "")
    if not match:
        return "검증된 Gemini 모델"
    version, family = match.groups()
    return f"Gemini {version} {family.replace('-', ' ').title()}"


def format_model_provenance(metadata, python_only=False):
    """사용자 출력에는 실제 실패 유형을 숨기지 않는 안전한 표시용 모델 상태만 노출한다."""
    if python_only:
        return "분석 모델: 미사용 (Python 사전계산)"
    metadata = metadata or {}
    if metadata.get("failed"):
        kind = metadata.get("failure_kind", "model_call")
        label = {
            "model_selection": "선택 실패",
            "quota_exhausted": "할당량 제한",
            "service_unavailable": "일시 과부하",
            "network_exception": "연결 실패",
        }.get(kind, "응답 실패")
        return f"분석 모델: {label}"
    model_id = metadata.get("model_id") or _model_id_from_url(metadata.get("model_url", ""))
    suffix = "자동 fallback" if metadata.get("fallback_used") else metadata.get("selection_source", "고정 승인 모델")
    safe_suffix = re.sub(r"[^가-힣A-Za-z0-9 ._-]", "", str(suffix))[:48] or "승인 roster"
    return f"분석 모델: {_friendly_model_name(model_id)} · {safe_suffix}"


def model_execution_hold_message(metadata):
    metadata = metadata or {}
    kind = metadata.get("failure_kind")
    if kind == "model_selection":
        return ("⚠️ <b>분석 실행 보류</b>\n"
                "현재 고정 승인 Gemini 모델 목록을 사용할 수 없습니다.\n"
                "분석 결과는 생성되지 않았습니다. 잠시 후 동일 명령으로 다시 실행해 주세요.")
    if kind == "quota_exhausted":
        wait = metadata.get("retry_after_seconds")
        wait_hint = f" 약 {wait}초 후" if wait else " 잠시 후"
        return ("⚠️ <b>분석 실행 보류 — Gemini 할당량 제한</b>\n"
                f"승인된 분석 모델의 현재 할당량이 부족합니다.{wait_hint} 같은 명령으로 다시 시도해 주세요.\n"
                "원천 수집 데이터는 유지되지만 AI 해석·가격 경로·확률은 생성되지 않았습니다.")
    return ("⚠️ <b>분석 실행 실패</b>\n"
            "현재 분석 모델에 연결할 수 없어 결과를 생성하지 않았습니다.\n"
            "시장 분석·가격 경로·투자 판단은 발행되지 않았습니다. 잠시 후 다시 실행해 주세요.")


def get_approved_model_roster():
    """분석 실행에는 수동 승인된 두 고정 모델만 순서대로 사용한다.
    모델 목록 탐색·상태 캐시·fixture 호출은 일반 분석 경로에 존재하지 않는다."""
    selected = []
    for item in sorted(STATIC_APPROVED_MODELS, key=lambda row: (int(row["rank"]), row["model_id"])):
        model_id = item["model_id"]
        selected.append({
            "model_id": model_id,
            "model_url": _model_url(model_id),
            "selection_source": item["selection_source"],
            "roster_updated_at_utc": "fixed-static",
        })
    return selected


# ============================================================
# Gemini API 호출
# ============================================================
def _response_retry_after_seconds(response):
    """Gemini RetryInfo를 안전하게 초 단위로 읽는다. 원문 오류는 기록하지 않는다."""
    try:
        detail = response.json()
        retry_info = next((item for item in detail.get("error", {}).get("details", [])
                           if item.get("@type", "").endswith("RetryInfo")), {})
        delay_text = str(retry_info.get("retryDelay", "")).rstrip("s")
        return int(float(delay_text)) if delay_text else None
    except Exception:
        return None


def _bounded_retry_delay_seconds(attempt_index, retry_after_seconds=None, jitter=None):
    """provider delay를 우선하고, 없으면 1·2·4…초 bounded backoff에 작은 jitter를 더한다."""
    if retry_after_seconds is not None:
        try:
            return max(0, min(int(retry_after_seconds), 60))
        except (TypeError, ValueError):
            pass
    base = min(30, 2 ** max(0, int(attempt_index)))
    if jitter is None:
        jitter = random.uniform(0.0, min(1.0, base * 0.25))
    return base + max(0.0, float(jitter))


def call_gemini_api_with_retry(full_prompt, max_tokens=16384, preferred_url=None, return_metadata=False,
                               response_json_schema=None, allow_preferred_fallback=False,
                               allow_short_quota_retry=False, request_timeout_seconds=120,
                               max_transport_attempts=2):
    headers = {
        "Content-Type": "application/json",
        "X-goog-api-key": GEMINI_API_KEY,
    }
    payload = {
        "contents": [{"parts": [{"text": full_prompt}]}],
        "generationConfig": {
            "maxOutputTokens": max_tokens,
            # gemini-3.x 계열은 기본적으로 thinking이 켜져 있고 완전 비활성화가 불가능하다.
            # 미설정 시 기본값(medium)이 max_tokens 예산을 사고과정에 소비해, 답변이 잘리거나
            # 사고과정 원문이 그대로 노출되는 사례가 실사용에서 확인됨 → low로 최소화한다.
            "thinkingConfig": {"thinkingLevel": "low"},
        },
    }

    if response_json_schema is not None:
        payload["generationConfig"]["responseMimeType"] = "application/json"
        payload["generationConfig"]["responseJsonSchema"] = response_json_schema

    roster = get_approved_model_roster()
    if preferred_url:
        # PHASE 2는 PHASE 1 성공 모델을 첫 후보로 고정한다. 승인된 fallback은 명시적 호출자만 허용한다.
        candidates = [{"model_url": preferred_url, "model_id": _model_id_from_url(preferred_url), "selection_source": "PHASE 1 성공 모델"}]
        if allow_preferred_fallback:
            preferred_rank = next((index for index, approved in enumerate(roster)
                                   if approved["model_url"] == preferred_url), None)
            # 승인 roster의 뒤 순위로만 전환한다. 3.6으로 성공한 PHASE 1을 3.7로 역방향 재시도하지 않는다.
            if preferred_rank is not None:
                for approved in roster[preferred_rank + 1:]:
                    fallback_candidate = dict(approved)
                    fallback_candidate["selection_source"] = "PHASE 2 승인 fallback"
                    candidates.append(fallback_candidate)
    else:
        candidates = roster
    if not candidates:
        failure_text = "AI 분석 모델을 선택하지 못했습니다. 분석 결과는 생성되지 않았습니다."
        failure_metadata = {"model_url": "", "response_model_version": "", "failed": True, "failure_kind": "model_selection"}
        return (failure_text, failure_metadata) if return_metadata else failure_text

    try:
        request_timeout_seconds = max(5, int(request_timeout_seconds))
    except (TypeError, ValueError):
        request_timeout_seconds = 120
    try:
        max_transport_attempts = max(1, int(max_transport_attempts))
    except (TypeError, ValueError):
        max_transport_attempts = 2

    last_failure_kind = "model_call"
    last_retry_after_seconds = None
    last_model_url = ""
    short_quota_retry_consumed = False
    for candidate_index, candidate in enumerate(candidates):
        url = candidate["model_url"]
        last_model_url = url
        for transport_attempt in range(max_transport_attempts):
            try:
                res = requests.post(url, headers=headers, json=payload, timeout=request_timeout_seconds)
                if res.status_code == 200:
                    data = res.json()
                    parts = data["candidates"][0]["content"]["parts"]
                    # thought=true 파트(사고과정)는 제외하고 실제 답변 파트만 이어붙인다
                    # (parts[0]만 읽으면 사고과정 파트가 답변 대신 잡힐 수 있음 — 실사용 확인됨).
                    answer_text = "".join(
                        p.get("text", "") for p in parts if p.get("text") and not p.get("thought")
                    )
                    if answer_text:
                        # [호출량 관측] Gemini 2.5+ 계열은 암묵적 캐싱이 기본 활성화된다.
                        # 일반 분석은 PHASE별 runtime profile이 각 phase 안에서 동일 prefix로 들어가므로,
                        # 비용·quota 보장은 하지 않고 실제 cache hit만 관측한다. 신규 호출·과금·분기는 없다.
                        usage = data.get("usageMetadata", {})
                        cached_tok = usage.get("cachedContentTokenCount", 0)
                        prompt_tok = usage.get("promptTokenCount", 0)
                        if prompt_tok:
                            pct = cached_tok / prompt_tok * 100
                            print(f"[INFO] Gemini 캐시히트(암묵적, 무료): {cached_tok:,}/{prompt_tok:,}"
                                  f"토큰 ({pct:.0f}%)")
                        metadata = {
                            "model_url": url,
                            "model_id": candidate.get("model_id") or _model_id_from_url(url),
                            "response_model_version": data.get("modelVersion", ""),
                            "selection_source": candidate.get("selection_source", "고정 승인 모델"),
                            "roster_updated_at_utc": candidate.get("roster_updated_at_utc", ""),
                            "fallback_used": candidate is not candidates[0],
                            "prompt_token_count": int(usage.get("promptTokenCount", 0) or 0),
                            "output_token_count": int(usage.get("candidatesTokenCount", 0) or 0),
                            "total_token_count": int(usage.get("totalTokenCount", 0) or 0),
                        }
                        return (answer_text, metadata) if return_metadata else answer_text
                    # 사고과정만 오고 최종 답변 파트가 비어있는 경우(토큰 예산 소진 등) 재시도로 넘긴다
                    print(f"[WARN] Gemini 응답에 최종 답변 파트 없음(사고과정만 수신, parts={len(parts)}개), 재시도")
                    time.sleep(_bounded_retry_delay_seconds(transport_attempt))
                elif res.status_code == 429:
                    # provider가 짧고 명시적인 RetryInfo를 준 PHASE 1 요청에 한해, 전체 roster에서 한 번만 같은 입력을 기다려 재시도한다.
                    # RetryInfo가 없거나 장기 대기면 즉시 승인된 다음 후보로 넘어가 불필요한 대형 호출·대기를 만들지 않는다.
                    last_failure_kind = "quota_exhausted"
                    last_retry_after_seconds = _response_retry_after_seconds(res)
                    if (
                        allow_short_quota_retry
                        and not short_quota_retry_consumed
                        and last_retry_after_seconds is not None
                        and 0 < last_retry_after_seconds <= 15
                    ):
                        short_quota_retry_consumed = True
                        print(f"[WARN] Gemini 할당량 제한(429): provider RetryInfo {last_retry_after_seconds}초 후 동일 요청을 한 번 재시도합니다")
                        time.sleep(last_retry_after_seconds)
                        continue
                    print("[WARN] Gemini 할당량 제한(429): 승인 roster의 허용된 다음 후보를 확인합니다")
                    break
                elif res.status_code == 503:
                    last_failure_kind = "service_unavailable"
                    time.sleep(_bounded_retry_delay_seconds(transport_attempt, _response_retry_after_seconds(res)))
                else:
                    last_failure_kind = f"http_{res.status_code}"
                    print(f"[WARN] Gemini 응답 코드: {res.status_code}")
                    time.sleep(1)
            except Exception as e:
                last_failure_kind = "network_exception"
                print(f"[WARN] Gemini 호출 예외: {e}")
                time.sleep(_bounded_retry_delay_seconds(transport_attempt))
        # PHASE 2 fallback은 1차 성공 모델이 quota·503으로 실제 응답하지 못한 경우에만 허용한다.
        # 다른 형태의 실패는 PHASE 1→PHASE 2 모델 결속을 임의로 바꾸지 않고 보류한다.
        if preferred_url and allow_preferred_fallback and candidate_index == 0:
            if last_failure_kind not in ("quota_exhausted", "service_unavailable"):
                break
    failure_text = "AI 서버 일시적 과부하 또는 모델 접근 불가 상태입니다. 잠시 후 다시 시도해 주세요."
    failure_metadata = {"model_url": last_model_url, "response_model_version": "", "failed": True,
                        "failure_kind": last_failure_kind, "retry_after_seconds": last_retry_after_seconds}
    return (failure_text, failure_metadata) if return_metadata else failure_text


# ============================================================
# 가독성 보강 유틸
# - sanitize_html: Gemini 원문의 마크다운 잔재를 HTML로 치환하고,
#   <,>,& 로 인한 Telegram HTML 파싱 오류를 방지한다.
# - smart_chunk: 4000자 단위 강제절단 대신, 카드/섹션 경계(🔹, 1️⃣~6️⃣)에서만
#   끊어 문단이 중간에 잘리지 않도록 청크를 나눈다.
# ============================================================
def sanitize_html(text: str) -> str:
    if not text:
        return text
    # 1) 원문에 섞인 <,>,& 이스케이프 (HTML 파싱 오류 방지, 반드시 최우선 처리)
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    # 2) 마크다운 굵게(**text**) -> <b>text</b>
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    # 3) 마크다운 헤더(### 제목) -> 굵게 처리
    text = re.sub(r"^#{1,6}\s*(.+)$", r"<b>\1</b>", text, flags=re.MULTILINE)
    # 4) 표 구분선 등 잔여 파이프/대시 라인 제거 (V002-3부터 표 미사용, 방어용)
    text = re.sub(r"^\s*\|?[-:]{3,}\|?[-:|]*\s*$", "", text, flags=re.MULTILINE)
    return text


def smart_chunk(text: str, boundary_markers, max_len=4000):
    """boundary_markers 앞에서만 끊어 청크를 구성한다.
    단일 구간이 max_len을 넘으면 그 구간만 부득이하게 강제 절단한다."""
    if not text:
        return [text]

    positions = sorted({
        m.start()
        for marker in boundary_markers
        for m in re.finditer(re.escape(marker), text)
    })
    if not positions or positions[0] != 0:
        positions = [0] + positions

    segments = [
        text[positions[i]:(positions[i + 1] if i + 1 < len(positions) else len(text))]
        for i in range(len(positions))
    ]

    chunks, current = [], ""
    for seg in segments:
        if len(seg) > max_len:
            if current:
                chunks.append(current)
                current = ""
            for i in range(0, len(seg), max_len):
                chunks.append(seg[i:i + max_len])
            continue
        if len(current) + len(seg) > max_len:
            chunks.append(current)
            current = seg
        else:
            current += seg
    if current:
        chunks.append(current)
    return chunks


PHASE1_BOUNDARY_MARKERS = ["🔹"]
PHASE2_BOUNDARY_MARKERS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣"]
FINDCOIN_BOUNDARY_MARKERS = ["🥇", "🥈", "🥉"]


# ============================================================
# 텔레그램 메시지 전송 (일반 + 인라인 버튼 지원)
# ============================================================
def _telegram_response_ok(response):
    try:
        return bool(response.json().get("ok", False))
    except Exception:
        return bool(getattr(response, "status_code", 0) == 200)


def send_telegram_message(chat_id, text, reply_markup=None, timeout=15):
    """Telegram 전송 실패를 호출자 예외로 전파하지 않고 HTML→plain 1회 fallback의 성공 여부를 반환한다."""
    send_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup

    try:
        if _telegram_response_ok(requests.post(send_url, json=payload, timeout=timeout)):
            return True
    except Exception as exc:
        print(f"[WARN] Telegram sendMessage HTML 전송 실패: {type(exc).__name__}")

    payload.pop("parse_mode", None)
    try:
        if _telegram_response_ok(requests.post(send_url, json=payload, timeout=timeout)):
            return True
    except Exception as exc:
        print(f"[WARN] Telegram sendMessage plain fallback 실패: {type(exc).__name__}")
    return False


def answer_callback_query(callback_query_id, text=None):
    """콜백 응답 전송 실패가 분석 세션 상태를 고착시키지 않도록 성공 여부만 반환한다."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery"
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    try:
        return _telegram_response_ok(requests.post(url, json=payload, timeout=10))
    except Exception as exc:
        print(f"[WARN] Telegram answerCallbackQuery 실패: {type(exc).__name__}")
        return False


# ============================================================
# PHASE 1 전용 실행 (데이터 수집 + 표 작성만)
# ============================================================
def run_phase1(symbol_input, exchange_name, custom_tfs):
    ex_name = resolve_exchange(exchange_name)
    ex_display = SUPPORTED_EXCHANGES.get(ex_name, {}).get("kr_name", exchange_name)
    normalized_tfs, tf_error = normalize_timeframes(custom_tfs)
    if tf_error:
        return tf_error, None, None, None
    custom_tfs = normalized_tfs
    try:
        if ex_name is None:
            return (
                "지원하지 않는 거래소입니다.\n"
                "현재 지원 거래소: 업비트 · 빗썸 · 코인베이스",
                None, None, None
            )

        quote = SUPPORTED_EXCHANGES[ex_name]["quote"]
        clean = symbol_input.replace("/", "").replace(":", "").replace(" ", "")
        symbol_upper = resolve_korean_symbol(clean, ex_name)

        try:
            exchange_class = getattr(ccxt, ex_name)({"enableRateLimit": True, "timeout": 8000})
            exchange_class.load_markets()
        except Exception as e:
            print(f"[WARN] load_markets 실패({ex_name}): {e}")
            return friendly_error_message(e, ex_display, symbol_input), None, None, None

        symbol = f"{symbol_upper}/{quote}"

        # 코인 존재여부 사전검증 — fetch_ohlcv 예외를 기다리지 않고 즉시 명확한 안내
        if symbol not in exchange_class.markets:
            is_korean_input = bool(re.search(r"[가-힣]", symbol_input))
            hint = (
                f"\n한글 코인명이 인식되지 않았습니다. 코인명은 영문을 사용해주세요. (예: BTC, XRP)"
                if is_korean_input else
                f"\n({ex_display}에 상장되지 않은 코인일 수 있습니다)"
            )
            return (
                f"'{symbol_input}' 코인을 {ex_display}에서 찾을 수 없습니다.\n"
                f"코인명을 다시 확인하시거나 다른 거래소를 시도해 주세요."
                f"{hint}",
                None, None, None
            )

        # [수정] ccxt의 정적 timeframes 목록은 실제 거래소 API 능력과 다를 수 있음이
        # 실사용으로 확인되어(예: 빗썸은 목록에 4h가 없지만 실제로는 정상 동작) 사전차단을
        # 제거한다. 실제 미지원 TF는 fetch_ohlcv 실패 시 friendly_error_message가 안전하게 안내한다.

        # [결함수정] HOTD/LOTD(당일 고/저) 판정용 KST 기준 '오늘' 경계를 미리 계산한다.
        # ccxt는 업비트·빗썸·코인베이스 전부 동일한 UTC epoch 타임스탬프로 표준화하여 반환하므로,
        # 거래소와 무관하게 KST 기준 하나로 통일 적용한다(실사용 확인: 이 기준이 없어 Gemini가
        # "오늘"에 해당하는 봉을 추측해야 했음 — Plugin5 HOTD/LOTD Sweep 채점 항목에 직접 영향).
        now_kst = datetime.now(timezone.utc) + timedelta(hours=9)
        today_kst_str = now_kst.strftime("%Y-%m-%d")

        payload = (
            f"[STAGE 0 사전 환경 점검]\n"
            f"• 수집 거래소: {ex_name.upper()}\n"
            f"• 수집 방식: ■ API Direct Data Stream\n"
            f"• 현재 시각(KST): {now_kst.strftime('%Y-%m-%d %H:%M')} — 아래 각 봉의 [오늘] 태그는\n"
            f"  이 시각 기준 KST 당일(00:00~현재)에 해당함을 의미하며, HOTD/LOTD 일중 스윕 판정의\n"
            f"  유일한 근거로 사용한다(임의 추정 금지).\n\n"
            f"=== 코인명: {symbol} ===\n"
        )

        last_close = None
        tf_delta_list = []
        tf_quality_list = []
        snapshot_events = []
        # TF 루프: OHLCV+RSI + TF별 Volume Delta(Plugin7, V003은 TF마다 다른 값을 요구)
        # 한 TF의 수집 예외는 그 TF의 데이터결손으로만 국소화한다. 다른 정상 TF의 PHASE 1/2 경로는 계속 유지한다.
        for i, tf in enumerate(custom_tfs):
            tf_fetch_started = datetime.now(timezone.utc)
            try:
                if tf == "1w":
                    # 주봉은 거래소 API 지원여부와 무관하게 항상 일봉을 직접 묶어 생성한다.
                    # 100주 의존 규칙에 필요한 Layer 1 SSOT 이력만큼 요청하고, 부족 반환은 품질 게이트가 격리한다.
                    if WEEKLY_HISTORY_DAYS is None:
                        raise RuntimeError("WEEKLY_HISTORY_DAYS SSOT를 읽지 못해 주봉 수집을 시작할 수 없습니다")
                    daily_ohlcv = exchange_class.fetch_ohlcv(symbol, timeframe="1d", limit=WEEKLY_HISTORY_DAYS)
                    tf_fetch_finished = datetime.now(timezone.utc)
                    daily_quality = validate_ohlcv_quality(daily_ohlcv, "1d", tf_fetch_finished)
                    ohlcv = resample_daily_to_weekly(daily_ohlcv)
                else:
                    daily_quality = None
                    ohlcv = exchange_class.fetch_ohlcv(symbol, timeframe=tf, limit=120)
                    tf_fetch_finished = datetime.now(timezone.utc)
                quality = validate_ohlcv_quality(ohlcv, tf, tf_fetch_finished)
                if daily_quality and daily_quality["status"] == "데이터결손/판정불가":
                    quality["status"] = "데이터결손/판정불가"
                    quality["reasons"].append("주봉 파생 원천 1d " + "; ".join(daily_quality["reasons"]))
            except Exception as tf_error:
                tf_fetch_finished = datetime.now(timezone.utc)
                quality = {
                    "tf": tf,
                    "received_bars": 0,
                    "completed_bars": 0,
                    "last_source_timestamp_ms": None,
                    "expected_interval_seconds": timeframe_seconds(tf),
                    "standard_target_completed_bars": DATA_MIN_COMPLETED_BARS,
                    "status": "데이터결손/판정불가",
                    "history_note": "",
                    "reasons": [f"OHLCV 수집 실패: {type(tf_error).__name__}"],
                }
            quality["fetch_started_utc"] = tf_fetch_started.isoformat(timespec="seconds") + "Z"
            quality["fetch_finished_utc"] = tf_fetch_finished.isoformat(timespec="seconds") + "Z"
            tf_quality_list.append(quality)
            snapshot_events.extend((tf_fetch_started, tf_fetch_finished))
            payload += f"\n[{tf} 원천 데이터 품질 감사]\n{format_ohlcv_quality(quality)}\n"

            # 실제 원천 무결성 결손은 OHLCV 및 그 파생값을 분석 입력에서 제외한다.
            # 가용 이력 부족(가용봉 분석)은 이 분기에 들어오지 않으며 전체 봉을 그대로 사용한다.
            if quality["status"] == "데이터결손/판정불가":
                payload += f"[{tf} 분석 입력 제외] 원천 무결성 결손으로 OHLCV·RSI·캔들·다이버전스·구조 데이터를 사용하지 않습니다.\n"
                tf_delta_info = fetch_volume_delta_summary(exchange_class, symbol, tf, limit=120)
                tf_delta_list.append({"tf": tf, "info": tf_delta_info})
                payload += f"\n[{tf} Volume Delta (Plugin 7)]\n"
                if tf_delta_info and tf_delta_info.get("status") == "ok":
                    payload += f"source: {tf_delta_info.get('source')} | last_Delta_Ratio: {tf_delta_info.get('last_delta_ratio'):+.4f}\n"
                else:
                    payload += "[거래소 미지원 또는 데이터결손] Volume Delta 수집 불가\n"
                continue

            df = pd.DataFrame(
                ohlcv,
                columns=["timestamp", "open", "high", "low", "close", "volume"],
            )
            df["rsi"] = calculate_rma_rsi(df["close"])
            recent = df.tail(100)
            # [결함수정-Cowork39] recent(최근100봉)의 첫 봉은 원본 df(최대120봉)에서는
            # df_offset번째 위치이며, 그 직전봉(df_offset-1)은 df에 존재하지만 tail(100)
            # 으로 잘려나가 recent에는 없다 — 장악형/관통형/먹구름형 태그가 원천 생략되던
            # 원인. df_offset==0(신규상장 데이터부족, 1w 리샘플 등 df 자체가 100봉 이하)
            # 인 경우는 직전봉이 실제로 없는 정상 상황이므로 그대로 둔다.
            df_offset = len(df) - len(recent)
            if len(recent) > 0:
                last_close = float(recent.iloc[-1]["close"])

            payload += f"\n[{tf} 타임프레임 API 수신 배열 (최근 {len(recent)}봉)]\n"
            n_rows = len(recent)
            check_today = is_subday_tf(tf)
            for j, (_, row) in enumerate(recent.iterrows()):
                # [결함수정] 마지막 봉은 아직 마감되지 않은 진행봉이라 종가가 계속 변한다.
                # 태그 없이 다른 확정봉과 동일하게 넘기면 Gemini가 진행봉 종가로 BOS/CHoCH를
                # 확정 판정해버리는 사례가 실사용에서 확인됨(엔진 규정 "진행봉 처리 통일 규칙" 위반).
                is_last = (j == n_rows - 1)
                tags = []
                if is_last:
                    tags.append("진행봉 — 미종료, 구조돌파(BOS/CHoCH) 확정판정 사용금지")
                if check_today:
                    bar_kst = datetime.fromtimestamp(row["timestamp"] / 1000, timezone.utc) + timedelta(hours=9)
                    if bar_kst.strftime("%Y-%m-%d") == today_kst_str:
                        tags.append("오늘")
                # [결함수정] 캔들 형태(꼬리방향 등) 순수 기하학 사전계산 — Gemini 서술오류 방지
                if j > 0:
                    prev_row = recent.iloc[j - 1]
                    shape_tags = classify_candle_shape(
                        row["open"], row["high"], row["low"], row["close"],
                        prev_row["open"], prev_row["high"], prev_row["low"], prev_row["close"],
                    )
                elif df_offset > 0:
                    # [결함수정-Cowork39] recent의 첫 봉(j==0) — df에서 실제 직전봉을 조회
                    prev_row = df.iloc[df_offset - 1]
                    shape_tags = classify_candle_shape(
                        row["open"], row["high"], row["low"], row["close"],
                        prev_row["open"], prev_row["high"], prev_row["low"], prev_row["close"],
                    )
                else:
                    shape_tags = classify_candle_shape(row["open"], row["high"], row["low"], row["close"])
                tags.extend(shape_tags)
                tag = f" [{', '.join(tags)}]" if tags else ""
                payload += (
                    f"O: {row['open']} | H: {row['high']} | L: {row['low']} | "
                    f"C: {row['close']} | V: {row['volume']:.2f} | RSI: {row['rsi']:.2f}{tag}\n"
                )

            # [결함수정] Regular RSI 다이버전스 사전계산 — Gemini 다중스윙 추론 누락 차단
            div_tag = detect_regular_divergence(recent)
            payload += f"\n[{tf} RSI 다이버전스 사전계산]\n{div_tag}\n"

            # Volume Delta: TF마다 개별 수집 (Layer3.5 Plugin 7 — PHASE1 카드15개와는 별개, TF별로 값이 달라야 함)
            tf_delta_info = fetch_volume_delta_summary(exchange_class, symbol, tf, limit=120)
            tf_delta_list.append({"tf": tf, "info": tf_delta_info})
            payload += f"\n[{tf} Volume Delta (Plugin 7)]\n"
            if tf_delta_info and tf_delta_info.get("status") == "ok":
                payload += f"source: {tf_delta_info.get('source')} | last_Delta_Ratio: {tf_delta_info.get('last_delta_ratio'):+.4f}\n"
            else:
                payload += "[거래소 미지원 또는 데이터결손] Volume Delta 수집 불가\n"

        # 다중 TF 분석의 최소 계약(2~4개)상 정상 OHLCV가 2개 미만이면 방향·가격경로를 구성하지 않는다.
        # 단일 TF API 예외 때문에 정상 TF를 버리지 않으며, 2개 이상이면 아래 PHASE 1/2 경로를 그대로 계속한다.
        usable_tf_count = sum(quality.get("status") != "데이터결손/판정불가" for quality in tf_quality_list)
        if usable_tf_count < 2:
            unavailable_lines = [
                f"• {quality.get('tf')}: " + "; ".join(quality.get("reasons") or ["정상 OHLCV 확보 실패"])
                for quality in tf_quality_list
                if quality.get("status") == "데이터결손/판정불가"
            ]
            return (
                "[다중 TF 원천 수집 미완료]\n"
                f"정상 OHLCV가 {usable_tf_count}개여서 최소 2개 TF 분석 계약을 충족하지 못했습니다.\n"
                + "\n".join(unavailable_lines)
                + "\n정상 TF는 버리지 않았습니다. 잠시 후 같은 명령으로 다시 수집해 주세요.",
                None, None, None,
            )

        # OI / Whale Wall — 심볼 단위 1회 (TF 무관 1회성 스냅샷, V003 정식모드 보조지표 블록 뒤 배치 대상)
        oi_fetch_started = datetime.now(timezone.utc)
        oi_info = fetch_oi_summary(exchange_class, symbol, ex_name)
        oi_fetch_finished = datetime.now(timezone.utc)
        wall_fetch_started = datetime.now(timezone.utc)
        wall_info = fetch_whale_wall_summary(exchange_class, symbol, last_close)
        wall_fetch_finished = datetime.now(timezone.utc)
        snapshot_events.extend((oi_fetch_started, oi_fetch_finished, wall_fetch_started, wall_fetch_finished))
        min_tf_seconds = min((timeframe_seconds(tf) for tf in custom_tfs if timeframe_seconds(tf)), default=None)
        snapshot_span_seconds = (max(snapshot_events) - min(snapshot_events)).total_seconds() if snapshot_events else 0.0
        snapshot_status = "시간동기"
        if min_tf_seconds is None:
            snapshot_status = "시간비동기/TF해석불가"
        elif snapshot_span_seconds >= min_tf_seconds:
            snapshot_status = "시간비동기"
        payload += (
            "\n[STAGE 0 수집 스냅샷 감사]\n"
            f"상태: {snapshot_status} | 전체 수집 시간차: {snapshot_span_seconds:.3f}초 | "
            f"최단 TF 구간: {min_tf_seconds if min_tf_seconds is not None else '해석불가'}초\n"
        )
        payload += format_plugin_payload(oi_info, wall_info)

        phase1_prompt = (
            f"{CANDLEVIEW_PROMPT_PHASE1}\n\n"
            f"[API 수신 원천 데이터]\n{payload}\n\n"
            f"[STAGE 0 품질 게이트 실행 지시]\n"
            f"각 TF의 '원천 데이터 품질 감사' 상태가 데이터결손/판정불가이면, PHASE 1의 해당 카드 항목을 생략하지 말고 "
            f"필요한 곳에 [데이터결손/판정불가]를 그대로 기록하십시오. 해당 TF의 결손 데이터를 구조·RSI·VSA·가격 판단으로 보완·추정하지 마십시오. "
            f"수집 스냅샷 감사가 시간비동기이면 OI/Whale Wall은 동시 스냅샷 사실로 과장하지 말고 상태만 보존하십시오.\n\n"
            f"지금은 PHASE 1만 수행하십시오.\n"
            f"PHASE 1 표 작성을 완료한 뒤, 엔진에 규정된 PHASE 1 최종 종료 고정 문구를 출력하고 멈추십시오.\n"
            f"PHASE 2 관련 서술·해석·전략은 절대 출력하지 마십시오."
        )

        # Gemini 해석과 무관하게 Python 원천 수집·가공 결과를 먼저 확정한다.
        phase1_canonical = build_phase1_canonical(
            ex_name.upper(), symbol, payload, tf_quality_list, snapshot_status, snapshot_span_seconds,
            {"volume_delta": tf_delta_list, "oi": oi_info, "whale_wall": wall_info},
        )
        canonical_warnings = validate_phase1_canonical(phase1_canonical)
        phase1_result, phase1_model_meta = call_gemini_api_with_retry(
            phase1_prompt, max_tokens=12000, return_metadata=True,
            allow_short_quota_retry=True,
            # PHASE 1 read timeout은 같은 대형 요청을 같은 모델에 120초씩 반복하지 않는다.
            # 60초 무응답이면 승인 roster의 다음 모델로 즉시 순방향 fallback해 전체 명령의 장기 무응답을 막는다.
            request_timeout_seconds=60, max_transport_attempts=1,
        )
        supplement = {
            "delta_list": tf_delta_list,
            "oi": oi_info,
            "wall": wall_info,
            "tf_quality": tf_quality_list,
            "snapshot_status": snapshot_status,
            "snapshot_span_seconds": snapshot_span_seconds,
            "phase1_canonical": phase1_canonical,
            "phase1_model": phase1_model_meta,
            "python_stage0_payload": payload,
        }
        if phase1_model_meta.get("failed"):
            # 수집데이터·보간지표는 Gemini 실패와 독립적으로 열람 가능해야 한다.
            failure_kind = phase1_model_meta.get("failure_kind")
            if failure_kind == "quota_exhausted":
                retry_after_seconds = phase1_model_meta.get("retry_after_seconds")
                retry_hint = f" 약 {retry_after_seconds}초 후" if retry_after_seconds else " 잠시 후"
                failure_line = f"승인된 Gemini 모델의 현재 할당량이 부족합니다.{retry_hint} 같은 명령으로 다시 시도해 주세요."
            else:
                failure_line = "모델 연결 또는 응답 실패로 PHASE 1 표 해석은 생성하지 않았습니다."
            phase1_result = (
                "[PHASE 1 AI 해석 보류 — Python 원천 수집은 완료]\n"
                + failure_line + "\n"
                "아래는 분석 추정 없이 Python이 수집·가공한 STAGE 0 원천 데이터입니다.\n\n"
                + payload
            )
        if canonical_warnings:
            phase1_result += "\n\n[자동검증 로그 — Python 사후검증]\n" + "\n".join(f"• {w}" for w in canonical_warnings)
        # PHASE 1 최종 표시본에서 compact 사실 registry를 만들고 immutable fingerprint를 만든다.
        # registry는 새 분석이 아니라 이미 표시된 TF별 사실의 참조 목록이며, PHASE 2 Bundle의 출처 검증에만 사용한다.
        supplement["phase1_fact_registry"] = build_phase1_fact_registry(phase1_result)
        supplement["phase1_fact_registry_warnings"] = validate_phase1_fact_registry(supplement["phase1_fact_registry"])
        # 이 값은 원천·카드가 이후 바뀌었을 때 PHASE 2 실행을 차단하는 용도이며, 모델 입력을 축약·변형하지 않는다.
        supplement["phase2_input_provenance"] = build_phase2_input_provenance(phase1_result, phase1_canonical)
        return phase1_result, symbol, ex_name.upper(), supplement

    except Exception as e:
        print(f"[ERROR] run_phase1 예외 ({ex_name} {symbol_input}): {e}")
        return friendly_error_message(e, ex_display, symbol_input), None, None, None


# ============================================================
# [V005] Phase2 내부 근거원장 검증 — 점수·가격 비개입형 설명 추적성 보강
# - 3-C의 S_1~S_4를 재계산하지 않는다. 모델이 작성한 내부 원장의 필드·배정·중복·서술 경계만 검증한다.
# - [INTERNAL_EVIDENCE_LEDGER]는 최종 사용자 출력 인터페이스에 속하지 않으므로 검증 직후 제거한다.
# - 파싱 실패 시 자연어를 임의 재작성하지 않고 [검증보류-확인필요] 로그만 남긴다.
# ============================================================
LEDGER_START = "[INTERNAL_EVIDENCE_LEDGER]"
LEDGER_END = "[/INTERNAL_EVIDENCE_LEDGER]"
LEDGER_FIELDS = ("결론", "등급", "등급보정", "축점수", "순합방향", "가격경로", "최종신뢰도점수", "지지축", "반대축", "상충축", "중립축", "축내상쇄", "진행국면")


def _load_core_score_cap_from_spec():
    """점수 범위는 코드에 재정의하지 않고 CandleView 명세의 SSOT에서만 읽는다."""
    match = re.search(r"CORE_SCORE_CAP\s*=\s*±\s*([0-9]+(?:\.[0-9]+)?)", CANDLEVIEW_PROMPT_FULL)
    return float(match.group(1)) if match else None


LEDGER_SCORE_LIMIT = _load_core_score_cap_from_spec()
LEDGER_ZERO_EPS = 1e-12
LEDGER_AXIS_FIELDS = ("지지축", "반대축", "상충축", "중립축")
LEDGER_CONDITIONAL_WORDS = ("다만", "반면", "확인 전", "리테스트", "조건부")
LEDGER_FORBIDDEN_WORDS = ("완벽한 정합", "즉시 돌파 확정", "무조건 지속")


def _extract_ledger_value(ledger_text, label):
    """동일 줄의 label:value만 읽고, axis의 빈 값은 정상으로 허용한다."""
    match = re.search(rf"^{re.escape(label)}[ \t]*:[ \t]*(.*)$", ledger_text or "", re.MULTILINE)
    return match.group(1).strip() if match else ""


def _extract_ledger_axes(value):
    return re.findall(r"S_[1-4]", value or "")


def _extract_signed_axis_scores(value):
    """INTERNAL_EVIDENCE_LEDGER의 S_1~S_4 부호 포함 수치를 중복 없이 읽는다."""
    matches = re.findall(
        r"\bS_([1-4])\s*=\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))",
        value or "",
    )
    scores = {}
    duplicates = []
    for axis_no, raw_value in matches:
        axis = f"S_{axis_no}"
        if axis in scores:
            duplicates.append(axis)
            continue
        try:
            scores[axis] = float(raw_value)
        except ValueError:
            return {}, [f"축점수 숫자 파싱 오류: {axis}={raw_value}"]
    warnings = []
    if duplicates:
        warnings.append("축점수 중복 기록: " + ", ".join(sorted(set(duplicates))))
    return scores, warnings


def _direction_from_net(net_value):
    if net_value > LEDGER_ZERO_EPS:
        return "상방"
    if net_value < -LEDGER_ZERO_EPS:
        return "하방"
    return "횡보"


def _extract_price_path(value):
    """내부 가격경로의 고정 P_* 필드를 수치로 파싱한다. 통화 쉼표는 제거한다."""
    expected_keys = ("P_entry", "P_inv", "P_target_1", "P_target_2")
    prices = {}
    duplicates = []
    for key in expected_keys:
        matches = re.findall(rf"\b{re.escape(key)}\s*=\s*([0-9][0-9,]*(?:\.[0-9]+)?)", value or "")
        if len(matches) > 1:
            duplicates.append(key)
        if matches:
            try:
                prices[key] = float(matches[0].replace(",", ""))
            except ValueError:
                return {}, [f"가격경로 숫자 파싱 오류: {key}={matches[0]}"]
    warnings = []
    if duplicates:
        warnings.append("가격경로 중복 기록: " + ", ".join(duplicates))
    return prices, warnings


def _load_phase2_probability_params_from_spec():
    """확률 표시식의 상수는 코드에 재정의하지 않고 명세 SSOT에서만 읽는다."""
    match = re.search(
        r"Final_신뢰도점수\s*/\s*([0-9]+(?:\.[0-9]+)?).*?Main Path 확률%\s*=\s*([0-9]+)%.*?×\s*([0-9]+)%",
        CANDLEVIEW_PROMPT_FULL,
        re.DOTALL,
    )
    if not match:
        return None
    try:
        denominator, base_pct, multiplier_pct = (Decimal(value) for value in match.groups())
    except (InvalidOperation, ValueError):
        return None
    if denominator <= 0 or multiplier_pct < 0:
        return None
    return {"confidence_denominator": denominator, "base_pct": base_pct, "multiplier_pct": multiplier_pct}


PHASE2_PROBABILITY_PARAMS = _load_phase2_probability_params_from_spec()


def _parse_decimal(value):
    try:
        return Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError, AttributeError):
        return None


def _format_display_number(value):
    decimal_value = _parse_decimal(value)
    if decimal_value is None or not decimal_value.is_finite():
        return None
    rendered = format(decimal_value.normalize(), "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def _verified_probability_pair(scores, confidence_value):
    """명세의 기존 3-C 확률식을 계산하고 표시용 소수점 첫째자리만 결정한다."""
    params = PHASE2_PROBABILITY_PARAMS
    confidence = _parse_decimal(confidence_value)
    if not params or confidence is None or not confidence.is_finite():
        return None, "확률 검증보류: 신뢰도 점수 또는 확률 SSOT를 읽을 수 없음"
    if confidence < 0 or confidence > params["confidence_denominator"]:
        return None, "확률 검증보류: 최종 신뢰도점수가 허용 범위를 벗어남"
    if LEDGER_SCORE_LIMIT is None:
        return None, "확률 검증보류: CORE_SCORE_CAP SSOT를 읽을 수 없음"
    score_total = sum(Decimal(str(value)) for value in scores.values())
    maximum = Decimal(str(len(scores))) * Decimal(str(LEDGER_SCORE_LIMIT))
    if maximum <= 0:
        return None, "확률 검증보류: 축점수 최대범위가 유효하지 않음"
    evidence_strength = min(Decimal("1"), abs(score_total) / maximum)
    confidence_ratio = confidence / params["confidence_denominator"]
    main_raw = params["base_pct"] + (evidence_strength * confidence_ratio * params["multiplier_pct"])
    if main_raw < Decimal("50") or main_raw > Decimal("90"):
        return None, "확률 검증보류: 명세 산식 결과가 50~90% 범위를 벗어남"
    main_display = main_raw.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    alternative_display = Decimal("100.0") - main_display
    return {"main": main_display, "alternative": alternative_display}, None


def _candidate_grade_from_scores(scores):
    """명세의 기존 81조합 규칙을 등급 후보로만 재현한다."""
    up_axes = [axis for axis, value in scores.items() if value > LEDGER_ZERO_EPS]
    down_axes = [axis for axis, value in scores.items() if value < -LEDGER_ZERO_EPS]
    neutral_count = 4 - len(up_axes) - len(down_axes)
    n_up, n_down = len(up_axes), len(down_axes)

    if n_up == n_down or max(n_up, n_down) == 0 or neutral_count >= 3:
        return "횡보", "횡보"

    majority_direction = "상방" if n_up > n_down else "하방"
    majority_axes = up_axes if majority_direction == "상방" else down_axes
    opposite_axes = down_axes if majority_direction == "상방" else up_axes

    if len(majority_axes) >= 3:
        return majority_direction, "강"
    if len(majority_axes) == 2 and not opposite_axes:
        return majority_direction, "보통"
    if len(majority_axes) == 2 and len(opposite_axes) == 1 and neutral_count == 1:
        opposite_abs = abs(scores[opposite_axes[0]])
        majority_abs = sorted((abs(scores[axis]) for axis in majority_axes), reverse=True)
        if opposite_abs > majority_abs[0] and opposite_abs > majority_abs[1]:
            return majority_direction, "횡보"
        return majority_direction, "약함"
    return "횡보", "횡보"


def _expected_final_grade(scores, net_direction, adjustment):
    majority_direction, candidate_grade = _candidate_grade_from_scores(scores)
    if net_direction == "횡보":
        return "횡보"

    # 순합 부호와 표수 후보가 다르면 약한 방향성만 허용한다.
    if majority_direction != net_direction or candidate_grade == "횡보":
        return "약함"

    expected_grade = candidate_grade
    if adjustment == "대형임펄스반전" and candidate_grade == "보통":
        expected_grade = "강"
    return expected_grade


def verify_and_strip_evidence_ledger(text):
    """내부 근거원장의 구조적 완전성만 검사하고 사용자 출력에서는 제거한다."""
    warnings = []
    match = re.search(r"\[INTERNAL_EVIDENCE_LEDGER\](.*?)\[/INTERNAL_EVIDENCE_LEDGER\]", text, re.DOTALL)
    if not match:
        return text, ["근거원장 누락: INTERNAL_EVIDENCE_LEDGER 블록이 없어 전수 축·반대근거 추적성을 검증할 수 없음"]

    ledger = match.group(1)
    for field in LEDGER_FIELDS:
        # axis의 빈 값·생략은 실제 점수 부호 배정이 판단한다. 이 단계는 비축 필수값만 관측한다.
        if field in LEDGER_AXIS_FIELDS:
            continue
        value = _extract_ledger_value(ledger, field)
        if not value:
            warnings.append(f"근거원장 필드 누락: {field}")

    axes = []
    field_axes = {}
    for field in LEDGER_AXIS_FIELDS:
        field_axes[field] = _extract_ledger_axes(_extract_ledger_value(ledger, field))
        axes.extend(field_axes[field])

    expected_axes = {"S_1", "S_2", "S_3", "S_4"}
    if set(axes) != expected_axes or len(axes) != 4:
        warnings.append("근거원장 축 배정 오류: S_1~S_4는 지지·반대·상충·중립축에 각각 정확히 한 번 배정돼야 함")

    score_value = _extract_ledger_value(ledger, "축점수")
    scores, score_warnings = _extract_signed_axis_scores(score_value)
    warnings.extend(score_warnings)
    conclusion_value = _extract_ledger_value(ledger, "결론")
    net_direction_value = _extract_ledger_value(ledger, "순합방향")
    grade_value = _extract_ledger_value(ledger, "등급")
    adjustment_value = _extract_ledger_value(ledger, "등급보정")

    if set(scores) != expected_axes or len(scores) != 4:
        warnings.append("축점수 완전성 오류: S_1~S_4의 부호 포함 수치를 각각 한 번씩 기록해야 함")
    elif LEDGER_SCORE_LIMIT is None:
        warnings.append("축점수 범위 검증 불가: CandleView 명세에서 CORE_SCORE_CAP SSOT를 읽지 못함")
    elif not all(np.isfinite(value) and -LEDGER_SCORE_LIMIT <= value <= LEDGER_SCORE_LIMIT for value in scores.values()):
        warnings.append(
            f"축점수 범위 오류: S_1~S_4는 유한수이며 [-{LEDGER_SCORE_LIMIT},+{LEDGER_SCORE_LIMIT}] 범위여야 함"
        )
    else:
        net_direction = _direction_from_net(sum(scores.values()))
        if net_direction_value != net_direction:
            warnings.append(
                f"3-C 순합방향 불일치: sign(S_1+S_2+S_3+S_4)={net_direction}, 기록={net_direction_value}"
            )
        if conclusion_value != net_direction:
            warnings.append(
                f"3-C 결론 방향 불일치: sign(S_1+S_2+S_3+S_4)={net_direction}, 결론={conclusion_value}"
            )

        if adjustment_value not in ("없음", "대형임펄스반전"):
            warnings.append("등급보정 오류: 없음 또는 대형임펄스반전만 허용")
        else:
            expected_grade = _expected_final_grade(scores, net_direction, adjustment_value)
            if grade_value != expected_grade:
                warnings.append(
                    f"3-C 우세등급 불일치: 점수·81조합·보정 기준={expected_grade}, 기록={grade_value}"
                )

        price_path_value = _extract_ledger_value(ledger, "가격경로")
        prices, price_warnings = _extract_price_path(price_path_value)
        warnings.extend(price_warnings)
        expected_price_keys = {"P_entry", "P_inv", "P_target_1", "P_target_2"}
        if net_direction == "횡보":
            if "횡보" not in price_path_value or prices:
                warnings.append("3-C 횡보 가격경로 오류: 방향성 목표가 없이 '횡보 — 방향성 목표가 미확정'이어야 함")
        elif set(prices) != expected_price_keys:
            warnings.append("가격경로 완전성 오류: P_entry·P_inv·P_target_1·P_target_2를 각각 한 번 기록해야 함")
        else:
            entry = prices["P_entry"]
            invalidation = prices["P_inv"]
            target_1 = prices["P_target_1"]
            target_2 = prices["P_target_2"]
            if net_direction == "상방" and not (invalidation < entry < target_1 < target_2):
                warnings.append("3-C 상방 가격경로 불일치: P_inv < P_entry < P_target_1 < P_target_2가 필요")
            if net_direction == "하방" and not (target_2 < target_1 < entry < invalidation):
                warnings.append("3-C 하방 가격경로 불일치: P_target_2 < P_target_1 < P_entry < P_inv가 필요")

        expected_axis_sets = {
            "지지축": {axis for axis, value in scores.items() if net_direction != "횡보" and ((net_direction == "상방" and value > LEDGER_ZERO_EPS) or (net_direction == "하방" and value < -LEDGER_ZERO_EPS))},
            "반대축": {axis for axis, value in scores.items() if net_direction != "횡보" and ((net_direction == "상방" and value < -LEDGER_ZERO_EPS) or (net_direction == "하방" and value > LEDGER_ZERO_EPS))},
            "상충축": {axis for axis, value in scores.items() if net_direction == "횡보" and abs(value) > LEDGER_ZERO_EPS},
            "중립축": {axis for axis, value in scores.items() if abs(value) <= LEDGER_ZERO_EPS},
        }
        for field in LEDGER_AXIS_FIELDS:
            if set(field_axes[field]) != expected_axis_sets[field]:
                warnings.append(f"근거원장 축 부호 배정 불일치: {field} 기록이 S_1~S_4 실제 부호와 다름")

    bundle_start = ledger.find("Bundle:")
    bundle_keys = []
    if bundle_start >= 0:
        bundle_text = ledger[bundle_start + len("Bundle:"):]
        bundle_lines = [line.strip()[1:].strip() for line in bundle_text.splitlines() if line.strip().startswith("-")]
        for line in bundle_lines:
            parts = [part.strip() for part in line.split("|")]
            if len(parts) != 7 or not all(parts[:7]):
                warnings.append(f"Source Bundle 형식 오류: {line}")
                continue
            if parts[5] not in ("결정", "국면", "보조", "가격경로"):
                warnings.append(f"Source Bundle 1차 역할 오류: {line}")
                continue
            bundle_keys.append("|".join((parts[0], parts[1], parts[3], parts[4])))
    else:
        warnings.append("근거원장 Bundle 항목 누락")

    if not bundle_keys:
        warnings.append("근거원장 Bundle 비어 있음")
    elif len(bundle_keys) != len(set(bundle_keys)):
        warnings.append("Source Bundle 중복: 같은 TF·관측창·원시사실군·방향은 한 번만 기록해야 함")

    cleaned = text[:match.start()] + text[match.end():]
    section_one = re.search(r"1️⃣.*?(?=\n2️⃣|\Z)", cleaned, re.DOTALL)
    section_one_text = section_one.group(0) if section_one else ""
    conclusion = _extract_ledger_value(ledger, "결론")
    grade = _extract_ledger_value(ledger, "등급")
    visible_decision = re.search(r"(상방|하방|횡보)\s*우세\s*\(우세\s*등급\s*:\s*(강|보통|약함|횡보)", section_one_text)
    if not visible_decision:
        warnings.append("메인 시나리오 방향·우세등급 표기 누락")
    elif visible_decision.group(1) != conclusion or visible_decision.group(2) != grade:
        warnings.append("근거원장과 메인 시나리오의 방향·우세등급 불일치")
    nested_counter = "있음" in _extract_ledger_value(ledger, "축내상쇄")
    needs_conditional = bool(field_axes["반대축"] or field_axes["상충축"] or nested_counter or grade in ("보통", "약함"))
    if needs_conditional:
        if not any(word in section_one_text for word in LEDGER_CONDITIONAL_WORDS):
            warnings.append("반대·상쇄 서술 누락: 1️⃣에 조건부 연결어 또는 경로상 조건이 없음")
        forbidden = [word for word in LEDGER_FORBIDDEN_WORDS if word in section_one_text]
        if forbidden:
            warnings.append("우세등급/반대근거와 충돌하는 확정 과장 표현: " + ", ".join(forbidden))

    return cleaned, warnings


# ============================================================
# [결함수정] Phase2 출력 사후검증 — FVG/OB 중첩서술 검증
# - 근거: 실사용 감사(STORJ/KRW)에서 OB-FVG "상호중첩" 서술이 실제로는 겹침폭 0
#   (경계접촉)인데 과장 서술된 사례 확인.
# - FVG/OB는 자연어 문장 재작성 대신 검증실패 시 경고 로그만 부착
#   (자동 재작성은 새 왜곡 위험이 더 큼).
# - 파싱 실패(정규식 불일치) 시 아무것도 건드리지 않고 원문 그대로 반환(보수적 동작).
# - [V003[C] 정리] 손익비(R:R)/추가진입시나리오가 Phase2 브리핑 표시항목에서
#   삭제됨(사용자 요청, 저부가가치 판단 + 리스크 재검토 완료)에 따라, 해당 항목을
#   재계산·치환하던 사후검증 로직도 함께 제거 — 더 이상 출력에 나오지 않는 문구를
#   정규식으로 찾는 죽은 코드를 방지한다. R:R 계산공식 자체(9장)와 FindCoin의
#   R:R≥2.0 게이트(14장)는 이 삭제와 무관하게 그대로 유지된다.
# ============================================================
def verify_and_fix_phase2(text):
    fixed, warnings = verify_and_strip_evidence_ledger(text)

    # FVG/OB "상호중첩" 서술 검증(수정은 안 함, 경고만 — 자연어 재작성 리스크 회피)
    # [결함수정-Cowork40] 숫자 패턴에 천단위 콤마 허용(예: "68,500,000") — 콤마 미대응 시
    # 정규식이 "68"에서 절단매칭되어 안전폴백(파싱실패시 원문유지) 없이 잘못된 값으로
    # 오매칭되는 것을 방지. 소수점만 있는 기존케이스도 동일 패턴으로 계속 정상 매칭됨.
    # [결함수정-Cowork37] re.search(단일매칭)는 4️⃣개별TF분석에서 TF마다 OB-FVG 중첩을
    # 각각 서술할 수 있는 구조상, 첫 매치 이후의 과장서술을 놓친다 — re.finditer로 전수
    # 검사한다. 경고문구에 어느 OB-FVG 조합인지 값을 명시해 여러 건이 섞여도 구분 가능하게 함.
    overlap_pattern = (
        r'\[OB\]\s*\(\s*([\d,]+(?:\.\d+)?)\s*~\s*([\d,]+(?:\.\d+)?)[^)]*\)[^가-힣]*(?:과|와)\s*가격\s*공백대\s*'
        r'\[FVG\]\s*\(\s*([\d,]+(?:\.\d+)?)\s*~\s*([\d,]+(?:\.\d+)?)[^)]*\)\s*가\s*상호\s*중첩'
    )
    for m_overlap in re.finditer(overlap_pattern, fixed):
        ob_lo, ob_hi, fvg_lo, fvg_hi = (float(g.replace(",", "")) for g in m_overlap.groups())
        overlap_width = min(ob_hi, fvg_hi) - max(ob_lo, fvg_lo)
        if overlap_width <= 0:
            warnings.append(
                f"OB({ob_lo}~{ob_hi})-FVG({fvg_lo}~{fvg_hi}) '상호중첩' 서술 검증실패: "
                f"실제 겹침폭={overlap_width:.2f}(0 이하=경계접촉·인접일 뿐 중첩 아님)"
            )

    if warnings:
        fixed += "\n\n[자동검증 로그 — Python 사후검증]\n" + "\n".join(f"• {w}" for w in warnings)
    return fixed


def classify_phase2_verification_warnings(warnings, structured=False):
    """검증 경고를 안정적인 운영 rule ID로 분류한다. 수식·결론·경고 원문은 변경하지 않는다."""
    if structured:
        return ["P2S01"]
    rule_ids = set()
    for warning in warnings or []:
        text = str(warning)
        if "근거원장" in text or "Source Bundle" in text:
            rule_ids.add("P2V01")
        if "축점수" in text or "순합방향" in text or "결론 방향" in text or "우세등급" in text or "축 부호" in text:
            rule_ids.add("P2V02")
        if "가격경로" in text:
            rule_ids.add("P2V03")
        if "메인 시나리오" in text or "반대·상쇄" in text or "과장 표현" in text:
            rule_ids.add("P2V04")
        if "OB(" in text or "상호중첩" in text:
            rule_ids.add("P2V05")
        if not any(rule_id in rule_ids for rule_id in ("P2V01", "P2V02", "P2V03", "P2V04", "P2V05")):
            rule_ids.add("P2V99")
    return sorted(rule_ids) or ["P2V99"]


def extract_phase2_validation_warnings(verified_text):
    marker = "[자동검증 로그 — Python 사후검증]"
    if marker not in (verified_text or ""):
        return []
    tail = verified_text.split(marker, 1)[1]
    return [line.strip().lstrip("•").strip() for line in tail.splitlines() if line.strip().lstrip("•").strip()]


# ============================================================
# PHASE 2 구조화 응답 렌더링 — 자연어 브리핑은 보존하고 검증 원장만 고정한다.
# ============================================================
# 배포 단위는 CandleView_API.txt + main.py 두 파일로 고정한다.
# 구조화 응답 스키마는 외부 파일이 아니라 main.py 내부 상수로 보존한다.
def phase2_validation_subcodes(warnings):
    """P2V 원문을 저장하지 않고, 반복 원인만 안정적인 세부 code로 관측한다."""
    subcodes = set()
    for warning in warnings or []:
        text = str(warning)
        if "3-C 순합방향 불일치" in text:
            subcodes.add("P2V02_NET_DIRECTION")
        if "3-C 결론 방향 불일치" in text:
            subcodes.add("P2V02_CONCLUSION")
        if "3-C 우세등급 불일치" in text:
            subcodes.add("P2V02_GRADE")
        if "근거원장 축 부호 배정 불일치" in text:
            subcodes.add("P2V02_AXIS_ASSIGNMENT")
        if "근거원장과 메인 시나리오의 방향·우세등급 불일치" in text:
            subcodes.add("P2V02_VISIBLE_DECISION")
        if "축점수 범위 오류" in text or "축점수 완전성 오류" in text or "축점수 숫자 파싱 오류" in text:
            subcodes.add("P2V02_SCORE_RANGE")
    return sorted(subcodes)


def _derived_axis_sets(scores, net_direction):
    return {
        "지지축": [axis for axis, value in scores.items() if net_direction != "횡보" and ((net_direction == "상방" and value > LEDGER_ZERO_EPS) or (net_direction == "하방" and value < -LEDGER_ZERO_EPS))],
        "반대축": [axis for axis, value in scores.items() if net_direction != "횡보" and ((net_direction == "상방" and value < -LEDGER_ZERO_EPS) or (net_direction == "하방" and value > LEDGER_ZERO_EPS))],
        "상충축": [axis for axis, value in scores.items() if net_direction == "횡보" and abs(value) > LEDGER_ZERO_EPS],
        "중립축": [axis for axis, value in scores.items() if abs(value) <= LEDGER_ZERO_EPS],
    }


def _replace_ledger_line(text, label, value):
    """동일 줄 label을 교체한다. 누락 label은 이 함수에서 새로 만들지 않는다."""
    return re.sub(
        rf"(?m)^{re.escape(label)}[ \t]*:[^\r\n]*$",
        f"{label}: {value}", text or "", count=1,
    )


def _upsert_ledger_line(text, label, value):
    """기존 label은 교체하고, 없으면 Bundle 직전의 동일 내부 ledger에만 삽입한다."""
    replaced = _replace_ledger_line(text, label, value)
    if replaced != (text or ""):
        return replaced
    bundle_at = (text or "").find("Bundle:")
    line = f"{label}: {value}\n"
    return (text or "") + "\n" + line if bundle_at < 0 else text[:bundle_at] + line + text[bundle_at:]


def normalize_evidence_ledger_for_publication(text):
    """파생 ledger 결손만 기존 수식으로 보완하고, 나머지는 비차단 관측으로 남긴다."""
    metadata = {"normalized_fields": [], "observation_codes": []}
    match = re.search(r"\[INTERNAL_EVIDENCE_LEDGER\](.*?)\[/INTERNAL_EVIDENCE_LEDGER\]", text or "", re.DOTALL)
    if not match:
        metadata["observation_codes"].append("LEDGER_ABSENT")
        return text, metadata

    ledger = match.group(1)
    scores, score_warnings = _extract_signed_axis_scores(_extract_ledger_value(ledger, "축점수"))
    expected_axes = {"S_1", "S_2", "S_3", "S_4"}
    score_is_usable = (
        not score_warnings and set(scores) == expected_axes and LEDGER_SCORE_LIMIT is not None
        and all(np.isfinite(value) and -LEDGER_SCORE_LIMIT <= value <= LEDGER_SCORE_LIMIT for value in scores.values())
    )
    if not score_is_usable:
        metadata["observation_codes"].append("LEDGER_CORE_UNAVAILABLE")
        return text, metadata

    net_direction = _direction_from_net(sum(scores.values()))
    adjustment = _extract_ledger_value(ledger, "등급보정")
    briefing = text[:match.start()]
    visible = re.search(r"(상방|하방|횡보)\s*우세\s*\(우세\s*등급\s*:\s*(강|보통|약함|횡보)", briefing)
    expected_grade = _expected_final_grade(scores, net_direction, adjustment) if adjustment in ("없음", "대형임펄스반전") else None
    axis_sets = _derived_axis_sets(scores, net_direction)
    replacements = {
        "결론": net_direction,
        "순합방향": net_direction,
        "지지축": ", ".join(axis_sets["지지축"]) if axis_sets["지지축"] else "없음",
        "반대축": ", ".join(axis_sets["반대축"]) if axis_sets["반대축"] else "없음",
        "상충축": ", ".join(axis_sets["상충축"]) if axis_sets["상충축"] else "없음",
        "중립축": ", ".join(axis_sets["중립축"]) if axis_sets["중립축"] else "없음",
    }
    if expected_grade is not None:
        replacements["등급"] = expected_grade
    elif visible:
        replacements["등급"] = visible.group(2)
        metadata["observation_codes"].append("LEDGER_GRADE_FROM_VISIBLE")

    repaired_ledger = ledger
    for label, value in replacements.items():
        current = _extract_ledger_value(repaired_ledger, label)
        if current != value:
            repaired_ledger = _upsert_ledger_line(repaired_ledger, label, value)
            metadata["normalized_fields"].append(label)

    repaired_briefing = briefing
    if visible and expected_grade is not None:
        expected_decision = f"{net_direction} 우세 (우세 등급: {expected_grade})"
        if visible.group(0) != expected_decision:
            repaired_briefing = re.sub(
                r"(상방|하방|횡보)\s*우세\s*\(우세\s*등급\s*:\s*(강|보통|약함|횡보)\)",
                expected_decision, briefing, count=1,
            )
            metadata["normalized_fields"].append("visible_decision")
    elif not visible:
        metadata["observation_codes"].append("LEDGER_VISIBLE_DECISION_UNLOCATABLE")

    if metadata["normalized_fields"]:
        metadata["observation_codes"].append("LEDGER_DERIVED_NORMALIZED")
    return repaired_briefing + LEDGER_START + repaired_ledger + LEDGER_END + text[match.end():], metadata


def strip_phase2_validation_log(text):
    """내부 검증 로그는 관측으로만 보존하고 사용자 브리핑에는 붙이지 않는다."""
    return (text or "").split("[자동검증 로그 — Python 사후검증]", 1)[0].rstrip()


def build_phase2_display_contract(text):
    """내부 ledger에서 Telegram 표시 전용 결정값을 만든다. 분석 산식은 수정하지 않는다."""
    match = re.search(r"\[INTERNAL_EVIDENCE_LEDGER\](.*?)\[/INTERNAL_EVIDENCE_LEDGER\]", text or "", re.DOTALL)
    if not match:
        return None, ["결정값 표시 보류: 내부 ledger 없음"]
    ledger = match.group(1)
    scores, score_warnings = _extract_signed_axis_scores(_extract_ledger_value(ledger, "축점수"))
    expected_axes = {"S_1", "S_2", "S_3", "S_4"}
    if score_warnings or set(scores) != expected_axes or LEDGER_SCORE_LIMIT is None:
        return None, ["결정값 표시 보류: 축점수 검증 불가"]
    if not all(np.isfinite(value) and -LEDGER_SCORE_LIMIT <= value <= LEDGER_SCORE_LIMIT for value in scores.values()):
        return None, ["결정값 표시 보류: 축점수 범위 오류"]
    direction = _direction_from_net(sum(scores.values()))
    prices, price_warnings = _extract_price_path(_extract_ledger_value(ledger, "가격경로"))
    if price_warnings:
        return None, ["결정값 표시 보류: 가격경로 파싱 오류"]
    if direction == "횡보":
        return {"direction": direction, "prices": {}, "probabilities": None}, []
    if set(prices) != {"P_entry", "P_inv", "P_target_1", "P_target_2"}:
        return None, ["결정값 표시 보류: 가격경로 필수값 누락"]
    entry, invalidation = prices["P_entry"], prices["P_inv"]
    target_1, target_2 = prices["P_target_1"], prices["P_target_2"]
    ordered = (invalidation < entry < target_1 < target_2) if direction == "상방" else (target_2 < target_1 < entry < invalidation)
    if not ordered:
        return None, ["결정값 표시 보류: 가격경로 방향 순서 불일치"]
    probabilities, probability_warning = _verified_probability_pair(scores, _extract_ledger_value(ledger, "최종신뢰도점수"))
    warnings = [probability_warning] if probability_warning else []
    return {"direction": direction, "prices": prices, "probabilities": probabilities}, warnings


def _strip_model_phase2_success_tag(text):
    """성공 태그는 Gemini 문장이 아니라 Python 검증 결과에서만 표시한다."""
    return re.sub(
        r"(?im)^\s*(?:(?:<[^>\n]+>|\*\*|__)\s*)?(?:[✅■]\s*)?(?:시스템 무결성 검증 완료|API Direct Data Parsing 완료|Layer 5-B 인라인 검증 100% 통과)(?:\s*(?:</[^>\n]+>|\*\*|__))?\s*$\n?",
        "",
        text or "",
    ).rstrip()


MAIN_PATH_SECTION_PATTERN = (
    r"(?ms)^(?P<header>[^\n]*📈\s*메인 시나리오 파동 경로[^\n]*)\n"
    r"(?P<body>.*?)(?=^[^\n]*📉\s*대체 시나리오 파동 경로[^\n]*|\Z)"
)
ALT_PATH_SECTION_PATTERN = (
    r"(?ms)^(?P<header>[^\n]*📉\s*대체 시나리오 파동 경로[^\n]*)\n"
    r"(?P<body>.*?)(?=^[^\n]*(?:⏰️?\s*예상 소요기간|2️⃣)[^\n]*|\Z)"
)


def _is_unverified_decision_value_line(line):
    """핵심 결정값 키워드와 수치가 함께 있는 행만 제거해 일반 설명 문장은 보존한다."""
    text = str(line or "")
    return bool(re.search(r"(확률|현재가|진입|목표|무효화|손절)", text) and re.search(r"\d", text))


def _retain_route_context(body):
    """핵심 결정 수치는 제거하고 FVG·Role Reversal 등 보조 설명은 그대로 유지한다."""
    lines = [line for line in (body or "").splitlines() if not _is_unverified_decision_value_line(line)]
    return "\n".join(lines).strip()


def _render_path_section(match, card):
    context = _retain_route_context(match.group("body"))
    return match.group("header") + "\n" + card + ("\n" + context if context else "") + "\n\n"


def _redact_unverified_decision_lines(text):
    """필수 경로 섹션이 없을 때도 검증되지 않은 핵심 가격·확률 라인을 직접 표시하지 않는다."""
    lines = [line for line in (text or "").splitlines() if not _is_unverified_decision_value_line(line)]
    return "\n".join(lines).rstrip()


def _strip_nonroute_probability_lines(text, main_match, alt_match):
    """메인·대체 경로 밖의 확률 줄은 결정값 혼선을 막기 위해 표시하지 않는다."""
    probability_line = r"(?m)^\s*\(확률[^\n]*\)\s*$\n?"
    spans = sorted((main_match.span(), alt_match.span()))
    pieces, cursor, removed = [], 0, False
    for start, end in spans:
        outside = text[cursor:start]
        cleaned = re.sub(probability_line, "", outside)
        removed = removed or cleaned != outside
        pieces.append(cleaned)
        pieces.append(text[start:end])
        cursor = end
    outside = text[cursor:]
    cleaned = re.sub(probability_line, "", outside)
    removed = removed or cleaned != outside
    pieces.append(cleaned)
    return "".join(pieces), removed


def render_verified_phase2_decision_blocks(text, contract, quote, all_validation_warnings=None, display_warnings=None):
    """메인·대체 경로 전체를 단일 출처로 렌더링해 자연어 라벨 변형과 전역 치환을 차단한다."""
    rendered = _strip_model_phase2_success_tag(text)
    display_warnings = list(display_warnings or [])
    main_match = re.search(MAIN_PATH_SECTION_PATTERN, rendered)
    alt_match = re.search(ALT_PATH_SECTION_PATTERN, rendered)
    if not (main_match and alt_match):
        display_warnings.append("결정값 경로 섹션 누락")
        return _redact_unverified_decision_lines(rendered), display_warnings
    rendered, nonroute_probability_removed = _strip_nonroute_probability_lines(rendered, main_match, alt_match)
    if nonroute_probability_removed:
        display_warnings.append("비경로 확률 표기 제거")
    main_match = re.search(MAIN_PATH_SECTION_PATTERN, rendered)
    alt_match = re.search(ALT_PATH_SECTION_PATTERN, rendered)

    probabilities = contract.get("probabilities") if contract else None
    if contract and contract.get("direction") != "횡보":
        prices = contract["prices"]
        entry = _format_display_number(prices["P_entry"])
        invalidation = _format_display_number(prices["P_inv"])
        target_1 = _format_display_number(prices["P_target_1"])
        target_2 = _format_display_number(prices["P_target_2"])
        main_probability = (
            f"(확률 {_format_display_number(probabilities['main'])}% — 참고용, 백테스트 검증치 아님)"
            if probabilities else "(확률 검증보류 — 신뢰도 점수 확인 필요)"
        )
        alt_probability = (
            f"(확률 {_format_display_number(probabilities['alternative'])}% — 참고용, 백테스트 검증치 아님)"
            if probabilities else "(확률 검증보류 — 신뢰도 점수 확인 필요)"
        )
        main_card = (
            f"{main_probability}\n"
            f"➔ 검증된 진입 예상가 ({entry} {quote})\n"
            f"➔ 검증된 1차 목표가 ({target_1} {quote})\n"
            f"➔ 검증된 2차 목표가 ({target_2} {quote})"
        )
        alt_card = f"{alt_probability}\n➔ 검증된 무효화선 ({invalidation} {quote}) 이탈"
    else:
        display_warnings.append("결정값 표시 보류")
        main_card = "(결정값 검증보류 — 가격경로 확인 필요)"
        alt_card = "(결정값 검증보류 — 가격경로 확인 필요)"

    rendered = re.sub(MAIN_PATH_SECTION_PATTERN, lambda match: _render_path_section(match, main_card), rendered, count=1)
    rendered = re.sub(ALT_PATH_SECTION_PATTERN, lambda match: _render_path_section(match, alt_card), rendered, count=1)
    if not all_validation_warnings and not display_warnings:
        rendered = rendered.rstrip() + "\n\n시스템 무결성 검증 완료\n■ API Direct Data Parsing 완료\n■ Layer 5-B 인라인 검증 100% 통과"
    return rendered.rstrip(), display_warnings


def try_deterministic_phase2_repair(text, warnings):
    """모델의 중복 ledger 파생 필드만 기존 Python 수식으로 맞춘다. 점수·가격·원천은 수정하지 않는다."""
    allowed_prefixes = (
        "3-C 순합방향 불일치", "3-C 결론 방향 불일치", "3-C 우세등급 불일치",
        "근거원장 축 부호 배정 불일치", "근거원장과 메인 시나리오의 방향·우세등급 불일치",
    )
    warnings = [str(warning) for warning in warnings or []]
    metadata = {"repaired": False, "model_calls": 0, "subcodes": phase2_validation_subcodes(warnings), "changed_fields": 0}
    if not warnings or any(not warning.startswith(allowed_prefixes) for warning in warnings):
        return None, metadata
    ledger_match = re.search(r"\[INTERNAL_EVIDENCE_LEDGER\](.*?)\[/INTERNAL_EVIDENCE_LEDGER\]", text or "", re.DOTALL)
    if not ledger_match:
        return None, metadata
    ledger = ledger_match.group(1)
    scores, score_warnings = _extract_signed_axis_scores(_extract_ledger_value(ledger, "축점수"))
    adjustment = _extract_ledger_value(ledger, "등급보정")
    expected_axes = {"S_1", "S_2", "S_3", "S_4"}
    if score_warnings or set(scores) != expected_axes or LEDGER_SCORE_LIMIT is None:
        return None, metadata
    if not all(np.isfinite(value) and -LEDGER_SCORE_LIMIT <= value <= LEDGER_SCORE_LIMIT for value in scores.values()):
        return None, metadata
    if adjustment not in ("없음", "대형임펄스반전"):
        return None, metadata
    decision_pattern = r"(상방|하방|횡보)\s*우세\s*\(우세\s*등급\s*:\s*(강|보통|약함|횡보)\)"
    briefing = text[:ledger_match.start()]
    if len(re.findall(decision_pattern, briefing)) != 1:
        return None, metadata
    net_direction = _direction_from_net(sum(scores.values()))
    grade = _expected_final_grade(scores, net_direction, adjustment)
    axis_sets = _derived_axis_sets(scores, net_direction)
    repaired_ledger = ledger
    replacements = {
        "결론": net_direction, "등급": grade, "순합방향": net_direction,
        "지지축": ", ".join(axis_sets["지지축"]) if axis_sets["지지축"] else "없음",
        "반대축": ", ".join(axis_sets["반대축"]) if axis_sets["반대축"] else "없음",
        "상충축": ", ".join(axis_sets["상충축"]) if axis_sets["상충축"] else "없음",
        "중립축": ", ".join(axis_sets["중립축"]) if axis_sets["중립축"] else "없음",
    }
    for label, value in replacements.items():
        repaired_ledger = _replace_ledger_line(repaired_ledger, label, value)
    repaired_briefing = re.sub(decision_pattern, f"{net_direction} 우세 (우세 등급: {grade})", briefing, count=1)
    repaired = repaired_briefing + LEDGER_START + repaired_ledger + LEDGER_END + text[ledger_match.end():]
    metadata.update({"repaired": True, "changed_fields": len(replacements) + 1})
    return repaired, metadata


def build_phase2_lightweight_repair_prompt(provisional_json, warnings, phase1_fact_refs=None,
                                               phase1_result="", phase1_canonical=None):
    """기존 자연어를 Python이 보존하고, 모델은 ledger 객체만 제한적으로 복구하게 한다."""
    safe_warnings = "\n".join(f"- {str(warning)[:240]}" for warning in (warnings or [])[:12])
    safe_fact_refs = json.dumps(sorted(str(item) for item in (phase1_fact_refs or [])), ensure_ascii=False)
    safe_phase1 = str(phase1_result or "").strip()
    safe_canonical = json.dumps((phase1_canonical or {}).get("provenance", {}), ensure_ascii=False, sort_keys=True)
    try:
        provisional_briefing = extract_phase2_briefing_fallback(provisional_json)
    except Exception:
        provisional_briefing = ""
    return (
        "[PHASE 2 ledger 전용 검증 repair]\n"
        "아래 provisional user_briefing은 Python이 그대로 보존·발행하므로 절대로 재작성하거나 출력하지 마십시오. "
        "새 원천 수집·새 시장 사실·새 시나리오는 금지합니다. 동일 PHASE 1 데이터와 canonical provenance에서 이미 도출 가능한 값만 사용해 ledger를 완성하십시오. "
        "가격경로·축점수·신뢰도·Bundle은 PHASE 1 근거에만 결속하고, 허용 fact_ref 목록 밖의 참조는 쓰지 마십시오. "
        "응답은 반드시 ledger-repair JSON schema에 맞는 {\"ledger\": {...}} 하나여야 하며, user_briefing·설명문·마크다운은 절대 포함하지 마십시오. "
        "ledger는 모든 필수 필드와 최소 한 개 Bundle을 포함해야 합니다.\n\n"
        "[검증오류]\n" + safe_warnings + "\n\n"
        "[보존할 provisional user_briefing — 출력 금지]\n" + provisional_briefing + "\n\n"
        "[PHASE 1 canonical provenance]\n" + safe_canonical + "\n\n"
        "[허용 PHASE 1 fact_ref]\n" + safe_fact_refs + "\n\n"
        "[PHASE 1 완성 결과]\n" + safe_phase1
    )


PHASE2_RESPONSE_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object", "additionalProperties": False,
    "required": ["user_briefing", "ledger"],
    "properties": {
        "user_briefing": {"type": "string", "description": "사용자에게 표시할 기존 PHASE 2 자연어 브리핑 전체. 1️⃣~6️⃣의 기존 순서·맥락 흐름·조건부 반대근거 서술을 충분히 포함한다. 내부 원장 블록은 포함하지 않는다."},
        "ledger": {
            "type": "object", "additionalProperties": False,
            "required": ["conclusion", "grade", "grade_adjustment", "axis_scores", "net_direction", "price_path", "final_confidence_score", "support_axes", "opposition_axes", "contested_axes", "neutral_axes", "nested_offset", "regime", "bundles"],
            "properties": {
                "conclusion": {"type": "string", "enum": ["상방", "하방", "횡보"]},
                "grade": {"type": "string", "enum": ["강", "보통", "약함", "횡보"]},
                "grade_adjustment": {"type": "string", "enum": ["없음", "대형임펄스반전"]},
                "axis_scores": {"type": "object", "additionalProperties": False, "required": ["S_1", "S_2", "S_3", "S_4"], "properties": {"S_1": {"type": "number"}, "S_2": {"type": "number"}, "S_3": {"type": "number"}, "S_4": {"type": "number"}}},
                "net_direction": {"type": "string", "enum": ["상방", "하방", "횡보"]},
                "price_path": {"type": "string"},
                "final_confidence_score": {"type": "number", "minimum": 0, "maximum": 5.9},
                "support_axes": {"type": "array", "items": {"type": "string", "enum": ["S_1", "S_2", "S_3", "S_4"]}},
                "opposition_axes": {"type": "array", "items": {"type": "string", "enum": ["S_1", "S_2", "S_3", "S_4"]}},
                "contested_axes": {"type": "array", "items": {"type": "string", "enum": ["S_1", "S_2", "S_3", "S_4"]}},
                "neutral_axes": {"type": "array", "items": {"type": "string", "enum": ["S_1", "S_2", "S_3", "S_4"]}},
                "nested_offset": {"type": "string"}, "regime": {"type": "string"},
                "bundles": {
                    "type": "array", "minItems": 1,
                    "items": {
                        "type": "object", "additionalProperties": False,
                        "required": ["tf", "window", "fact", "fact_ref", "direction", "role", "axis"],
                        "properties": {
                            "tf": {"type": "string", "minLength": 1},
                            "window": {"type": "string", "minLength": 1},
                            "fact": {"type": "string", "minLength": 1},
                            "fact_ref": {"type": "string", "minLength": 1},
                            "direction": {"type": "string", "enum": ["상방", "하방", "중립"]},
                            "role": {"type": "string", "enum": ["결정", "국면", "보조", "가격경로"]},
                            "axis": {"type": "string", "enum": ["S_1", "S_2", "S_3", "S_4"]},
                        },
                    },
                },
            },
        },
    },
}


PHASE2_LEDGER_REPAIR_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object", "additionalProperties": False,
    "required": ["ledger"],
    "properties": {"ledger": PHASE2_RESPONSE_SCHEMA["properties"]["ledger"]},
}


def _load_phase2_response_schema():
    return PHASE2_RESPONSE_SCHEMA


def _load_phase2_ledger_repair_schema():
    return PHASE2_LEDGER_REPAIR_SCHEMA


def split_phase2_provenance_warnings(warnings):
    """내부 fact_ref 결속 관측과 카드 수학을 막는 구조 오류를 분리한다."""
    provenance, blocking = [], []
    for warning in warnings or []:
        text = str(warning)
        if "Source Bundle PHASE1 사실 참조 오류" in text:
            provenance.append(text)
        else:
            blocking.append(text)
    return blocking, provenance


def render_phase2_structured_response(raw_json, phase1_fact_registry=None):
    """JSON의 user_briefing을 보존하고, fact_ref 결속 오류는 비차단 provenance 관측으로 격리한다."""
    try:
        response = json.loads(raw_json)
        briefing = response["user_briefing"]
        ledger = response["ledger"]
        scores = ledger["axis_scores"]
        required_scores = ("S_1", "S_2", "S_3", "S_4")
        if not isinstance(briefing, str) or not briefing.strip() or not all(key in scores for key in required_scores):
            raise ValueError("user_briefing 또는 4축 점수 누락")
        def axis_text(values):
            return ", ".join(values) if values else "없음"

        lines = [
            LEDGER_START,
            f"결론: {ledger['conclusion']}", f"등급: {ledger['grade']}",
            f"등급보정: {ledger['grade_adjustment']}",
            "축점수: " + ", ".join(f"{key}={scores[key]}" for key in required_scores),
            f"순합방향: {ledger['net_direction']}", f"가격경로: {ledger['price_path']}",
            f"최종신뢰도점수: {ledger['final_confidence_score']}",
            "지지축: " + axis_text(ledger["support_axes"]),
            "반대축: " + axis_text(ledger["opposition_axes"]),
            "상충축: " + axis_text(ledger["contested_axes"]),
            "중립축: " + axis_text(ledger["neutral_axes"]),
            f"축내상쇄: {ledger['nested_offset']}", f"진행국면: {ledger['regime']}",
            "Bundle:",
        ]
        provenance_warnings = []
        for bundle in ledger["bundles"]:
            if not isinstance(bundle, dict):
                raise ValueError("Source Bundle 객체 형식 오류")
            fact_ref = str(bundle["fact_ref"])
            source_fact = (phase1_fact_registry or {}).get(fact_ref)
            if not isinstance(source_fact, dict) or source_fact.get("tf") != str(bundle["tf"]):
                # 내부 식별자 표기만의 오류가 이미 유효한 점수·가격경로·확률 카드 전체를 차단하지 않게 한다.
                provenance_warnings.append(f"Source Bundle PHASE1 사실 참조 오류: {fact_ref}")
                source_value = "PHASE1 registry 결속 미확인"
            else:
                source_value = str(source_fact["value"])
            lines.append("- " + "|".join([
                str(bundle["tf"]), str(bundle["window"]), source_value, fact_ref,
                str(bundle["direction"]), str(bundle["role"]), str(bundle["axis"]),
            ]))
        lines.append(LEDGER_END)
        return briefing.strip() + "\n\n" + "\n".join(lines), provenance_warnings
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        return "", [f"PHASE2 구조화 JSON 파싱 실패: {exc}"]


def render_phase2_ledger_repair_response(raw_json, preserved_briefing, phase1_fact_registry=None):
    """ledger 전용 repair 응답을 최초 자연어와 결합해 동일 renderer 계약으로 검증한다."""
    try:
        response = json.loads(raw_json)
        if not isinstance(response, dict):
            raise ValueError("ledger 객체 누락")
        # schema 준수 wrapper를 우선하고, provider가 wrapper만 생략한 순수 ledger 객체도 같은 검증으로 수용한다.
        ledger = response.get("ledger", response)
        if not isinstance(ledger, dict) or "axis_scores" not in ledger:
            raise ValueError("ledger 객체 누락")
        combined = json.dumps({"user_briefing": str(preserved_briefing or ""), "ledger": ledger}, ensure_ascii=False)
        return render_phase2_structured_response(combined, phase1_fact_registry)
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        return "", [f"PHASE2 ledger 전용 repair 파싱 실패: {exc}"]


def phase2_structured_warning_codes(warnings):
    """원시 브리핑·시장값 없이 구조화 실패 종류만 운영 로그에 남긴다."""
    codes = []
    for warning in (warnings or []):
        text = str(warning)
        if "'ledger'" in text or "ledger 객체 누락" in text:
            codes.append("MISSING_LEDGER")
        elif "Source Bundle PHASE1 사실 참조 오류" in text:
            codes.append("FACT_REF_MISMATCH")
        elif "4축 점수 누락" in text:
            codes.append("AXIS_SCORES_MISSING")
        elif "JSON" in text or "Expecting" in text:
            codes.append("MALFORMED_JSON")
        else:
            codes.append("OTHER_STRUCTURED_FAILURE")
    return sorted(set(codes)) or ["OTHER_STRUCTURED_FAILURE"]


def extract_phase2_briefing_fallback(raw_json):
    """ledger 결손과 사용자 브리핑 부재를 구분한다. 새 내용은 만들지 않는다."""
    try:
        response = json.loads(raw_json)
        briefing = response.get("user_briefing") if isinstance(response, dict) else None
        return briefing.strip() if isinstance(briefing, str) and briefing.strip() else ""
    except (json.JSONDecodeError, TypeError):
        return ""


# ============================================================
# PHASE 2 전용 실행 (이미 완성된 PHASE 1을 재료로 사용)
# ============================================================
def run_phase2(phase1_result, symbol, exchange_name, phase1_canonical=None, phase1_model=None,
               phase2_input_provenance=None, session_id=None, session_execution_ordinal=1,
               trigger_action="phase2_run", phase2_execution_metadata=None):
    """PHASE 2 실행. attempt는 내부 재질의 순번, execution ordinal은 세션 실행 순번이다."""
    try:
        session_execution_ordinal = int(session_execution_ordinal)
    except (TypeError, ValueError):
        session_execution_ordinal = 1
    session_execution_ordinal = max(1, session_execution_ordinal)
    if trigger_action not in ("phase2_run", "phase2_retry"):
        trigger_action = "phase2_run"

    def observe(event, **details):
        details.setdefault("session_execution_ordinal", session_execution_ordinal)
        details.setdefault("trigger_action", trigger_action)
        log_phase2_observation(event, phase2_input_provenance, session_id, **details)

    if (phase1_model or {}).get("failed"):
        observe("PRECHECK_HELD", rule_ids=["P2M01"])
        return (
            "[검증보류 — PHASE 1 AI 해석 미완료]\n"
            "원천 수집 데이터는 열람할 수 있으나 PHASE 1 해석이 완료되지 않아 최종 분석을 생성하지 않았습니다.\n"
            "가격·방향·확률·목표가는 제공되지 않습니다. 다시 분석을 실행해 주세요."
        )
    canonical_warnings = validate_phase1_canonical(phase1_canonical) if phase1_canonical else ["PHASE1 canonical 누락"]
    input_warnings = validate_phase2_input_provenance(phase2_input_provenance, phase1_result, phase1_canonical)
    model_url = (phase1_model or {}).get("model_url")
    repair_model_url = model_url
    precheck_warnings = list(canonical_warnings) + list(input_warnings)
    if not model_url:
        precheck_warnings.append("P2I04 PHASE1 성공 모델 provenance 누락")
    if precheck_warnings:
        rule_ids = sorted({warning.split()[0] if warning.startswith("P2I") else "P2I05" for warning in precheck_warnings})
        observe("PRECHECK_HELD", rule_ids=rule_ids)
        return "[검증보류 — PHASE 2 실행 차단]\n" + "\n".join(f"• {w}" for w in precheck_warnings)

    phase1_fact_registry = build_phase1_fact_registry(phase1_result)
    fact_registry_warnings = validate_phase1_fact_registry(phase1_fact_registry)
    fact_refs_for_prompt = sorted(phase1_fact_registry)

    base_prompt = (
        f"{CANDLEVIEW_PROMPT_PHASE2}\n\n"
        f"아래는 이미 완성된 PHASE 1 결과입니다. 사용자는 PHASE 2 진행을 명시적으로 승인하였습니다.\n\n"
        f"[PHASE 1 canonical provenance]\n{json.dumps(phase1_canonical['provenance'], ensure_ascii=False, sort_keys=True)}\n\n"
        f"[PHASE 1 완성 결과]\n{phase1_result}\n\n"
        f"[PHASE 1 사실 참조 목록]\n{json.dumps(fact_refs_for_prompt, ensure_ascii=False)}\n\n"
        f"이제 PHASE 2 통합 브리핑을 엔진 규칙에 따라 완제 출력하십시오. 새로운 수치나 판단을 임의로 추가하지 말고, PHASE 1에서 도출된 데이터만을 근거로 사용하십시오.\n"
        f"반드시 JSON으로 응답하십시오. user_briefing에는 기존 1️⃣~6️⃣ 자연어 브리핑 전체를 충분한 문맥으로 작성하고, INTERNAL_EVIDENCE_LEDGER 표식은 넣지 마십시오. "
        f"다만 1️⃣의 메인·대체 시나리오 경로에서는 가격·확률 숫자를 직접 쓰지 말고, 각 라벨과 보조 레벨(FVG·Role Reversal·돌파선·다음 매물대)만 작성하십시오. "
        f"Python이 검증 완료된 ledger 값으로 진입·무효화·목표가·확률을 표시합니다. "
        f"'시스템 무결성 검증 완료', 'API Direct Data Parsing 완료', 'Layer 5-B 인라인 검증 100% 통과' 태그는 절대 작성하지 마십시오. "
        f"ledger에는 같은 결론의 검증용 원장과 기존 Final_신뢰도점수(0~5.9)를 final_confidence_score로 작성하십시오. ledger.bundles의 각 항목은 문자열이 아니라 "
        f"tf·window·fact·fact_ref·direction·role·axis 일곱 필드를 모두 가진 JSON 객체여야 합니다. "
        f"fact_ref는 반드시 위 PHASE 1 사실 참조 목록의 값 하나를 사용하고 tf와 일치해야 합니다. fact는 해당 참조의 설명용 복사본입니다. "
        f"direction은 상방·하방·중립, role은 결정·국면·보조·가격경로, axis는 S_1~S_4 중 하나만 사용하십시오."
    )
    lightweight_repair_prompt = None
    provisional_fallback_briefing = ""
    last_verified = "[검증보류 — PHASE 2 결과 없음]"
    last_rule_ids = ["P2V99"]
    for attempt in range(2):
        is_lightweight_repair = lightweight_repair_prompt is not None
        request_prompt = lightweight_repair_prompt if is_lightweight_repair else base_prompt
        request_max_tokens = 3500 if is_lightweight_repair else 12000
        if is_lightweight_repair:
            observe(
                "LIGHTWEIGHT_REPAIR_ATTEMPT", attempt=attempt + 1,
                input_bytes=len(request_prompt.encode("utf-8")), max_output_tokens=request_max_tokens,
            )
        observe("CALL_ATTEMPT", attempt=attempt + 1, request_kind="lightweight_repair" if is_lightweight_repair else "full_phase2")
        raw_json, response_meta = call_gemini_api_with_retry(
            request_prompt, max_tokens=request_max_tokens,
            preferred_url=model_url if not is_lightweight_repair else repair_model_url, return_metadata=True,
            response_json_schema=(
                _load_phase2_ledger_repair_schema()
                if is_lightweight_repair else _load_phase2_response_schema()
            ), allow_preferred_fallback=True,
        )
        if isinstance(phase2_execution_metadata, dict):
            phase2_execution_metadata.clear()
            phase2_execution_metadata.update(response_meta)
        if response_meta.get("failed"):
            failure_kind = response_meta.get("failure_kind", "model_call")
            rule_id = "P2M02" if failure_kind == "quota_exhausted" else "P2M03"
            observe("MODEL_HELD", attempt=attempt + 1, rule_ids=[rule_id], failure_kind=failure_kind,
                    retry_after_seconds=response_meta.get("retry_after_seconds"))
            # 첫 응답의 자연어가 정상이라면 ledger repair 호출 실패가 그 분석 전체를 지우면 안 된다.
            if is_lightweight_repair and provisional_fallback_briefing:
                fallback_display, fallback_display_warnings = render_verified_phase2_decision_blocks(
                    provisional_fallback_briefing,
                    None,
                    symbol.rsplit("/", 1)[-1] if "/" in symbol else "",
                    all_validation_warnings=[f"P2S01 ledger repair 호출 실패: {failure_kind}"],
                    display_warnings=["구조화 ledger 복구 미완료"],
                )
                observe(
                    "STRUCTURED_LEDGER_OBSERVED", attempt=attempt + 1, rule_ids=["P2S01", rule_id],
                    observation_codes=["LEDGER_REPAIR_CALL_FAILED"] + sorted(set(fallback_display_warnings)),
                )
                observe("PUBLISHED", attempt=attempt + 1)
                return fallback_display
            if failure_kind == "quota_exhausted":
                wait_seconds = response_meta.get("retry_after_seconds")
                wait_hint = f" 약 {wait_seconds}초 후" if wait_seconds else " 잠시 후"
                return "[검증보류 — PHASE 2 승인 모델 할당량 도달]" + wait_hint + " 재시도해 주세요."
            return "[검증보류 — PHASE 2 승인 모델 호출 실패]"
        # P2S01 repair는 첫 응답을 실제로 만든 모델을 우선 사용해 불필요한 roster 재탐색을 피한다.
        repair_model_url = response_meta.get("model_url") or repair_model_url
        observe("MODEL_RESPONSE", attempt=attempt + 1, model_id=response_meta.get("model_id", ""),
                fallback_used=bool(response_meta.get("fallback_used")),
                selection_source=response_meta.get("selection_source", ""),
                prompt_tokens=response_meta.get("prompt_token_count", 0),
                output_tokens=response_meta.get("output_token_count", 0),
                total_tokens=response_meta.get("total_token_count", 0))
        raw_result, structured_warnings = (
            render_phase2_ledger_repair_response(raw_json, provisional_fallback_briefing, phase1_fact_registry)
            if is_lightweight_repair else render_phase2_structured_response(raw_json, phase1_fact_registry)
        )
        blocking_structured_warnings, provenance_warnings = split_phase2_provenance_warnings(structured_warnings)
        if provenance_warnings:
            observe(
                "PROVENANCE_OBSERVED", attempt=attempt + 1, rule_ids=["P2V01"],
                observation_codes=["FACT_REF_UNBOUND_NONBLOCKING"], provenance_warning_count=len(provenance_warnings),
            )
        if blocking_structured_warnings:
            # user_briefing이 있어도 ledger가 없거나 불완전하면, 이미 생성된 JSON만 재료로 한 번의 경량 repair를 먼저 시도한다.
            # 이 repair는 새 시장 판단을 만들지 않고 ledger·fact_ref 계약만 완성해 정상 결정값 카드를 복구한다.
            if not is_lightweight_repair:
                last_rule_ids = classify_phase2_verification_warnings(blocking_structured_warnings, structured=True)
                observe(
                    "STRUCTURED_LEDGER_REPAIR_SCHEDULED", attempt=attempt + 1, rule_ids=last_rule_ids,
                    rule_subcodes=["P2S01"], has_user_briefing=bool(extract_phase2_briefing_fallback(raw_json)),
                    structured_warning_codes=phase2_structured_warning_codes(blocking_structured_warnings),
                )
                provisional_fallback_briefing = extract_phase2_briefing_fallback(raw_json)
                lightweight_repair_prompt = build_phase2_lightweight_repair_prompt(
                    raw_json, blocking_structured_warnings, fact_refs_for_prompt,
                    phase1_result=phase1_result, phase1_canonical=phase1_canonical,
                )
                continue
            # 경량 repair까지 실패했을 때만 자연어를 보존하는 기존 fallback을 사용한다.
            fallback_briefing = extract_phase2_briefing_fallback(raw_json) or provisional_fallback_briefing
            if fallback_briefing:
                fallback_display, fallback_display_warnings = render_verified_phase2_decision_blocks(
                    fallback_briefing,
                    None,
                    symbol.rsplit("/", 1)[-1] if "/" in symbol else "",
                    all_validation_warnings=blocking_structured_warnings,
                    display_warnings=["구조화 ledger 복구 미완료"],
                )
                observe(
                    "STRUCTURED_LEDGER_OBSERVED", attempt=attempt + 1, rule_ids=["P2S01"],
                    observation_codes=["LEDGER_REPAIR_EXHAUSTED"] + sorted(set(fallback_display_warnings)),
                    structured_warning_codes=phase2_structured_warning_codes(blocking_structured_warnings),
                )
                observe("PUBLISHED", attempt=attempt + 1)
                return fallback_display
            last_rule_ids = classify_phase2_verification_warnings(blocking_structured_warnings, structured=True)
            observe("STRUCTURED_BRIEFING_HELD", attempt=attempt + 1, rule_ids=last_rule_ids, rule_subcodes=["P2S01"])
            last_verified = "[검증보류 — 사용자 브리핑 및 구조화 ledger 누락]\n" + "\n".join(f"• {warning}" for warning in blocking_structured_warnings)
            break

        normalized_result, normalization_meta = normalize_evidence_ledger_for_publication(raw_result)
        last_verified = verify_and_fix_phase2(normalized_result)
        validation_warnings = extract_phase2_validation_warnings(last_verified)
        if normalization_meta.get("normalized_fields"):
            observe(
                "LEDGER_NORMALIZED", attempt=attempt + 1, rule_ids=["P2V02"],
                normalized_fields=normalization_meta["normalized_fields"],
                observation_codes=normalization_meta.get("observation_codes", []),
            )
        if validation_warnings:
            last_rule_ids = classify_phase2_verification_warnings(validation_warnings)
            observe(
                "VALIDATION_OBSERVED", attempt=attempt + 1, rule_ids=last_rule_ids,
                rule_subcodes=phase2_validation_subcodes(validation_warnings),
                observation_codes=normalization_meta.get("observation_codes", []),
            )
        elif normalization_meta.get("observation_codes"):
            observe(
                "LEDGER_OBSERVED", attempt=attempt + 1, rule_ids=["P2V01"],
                observation_codes=normalization_meta["observation_codes"] + fact_registry_warnings,
            )
        clean_briefing = strip_phase2_validation_log(last_verified)
        display_contract, display_warnings = build_phase2_display_contract(normalized_result)
        published_briefing, display_warnings = render_verified_phase2_decision_blocks(
            clean_briefing,
            display_contract,
            symbol.rsplit("/", 1)[-1] if "/" in symbol else "",
            all_validation_warnings=list(validation_warnings) + list(provenance_warnings),
            display_warnings=display_warnings,
        )
        if display_warnings:
            observe(
                "DISPLAY_CONTRACT_OBSERVED", attempt=attempt + 1, rule_ids=["P2D01"],
                observation_codes=sorted(set(display_warnings)),
            )
        observe("PUBLISHED", attempt=attempt + 1)
        return published_briefing
    observe("RETRY_EXHAUSTED", rule_ids=last_rule_ids)
    return (
        "[검증보류 — 최대 2회 재질의 후 무결성 미통과]\n"
        "근거원장 또는 3-C 검증을 통과하지 못해 이번 분석 결과를 발행하지 않았습니다.\n"
        "사유 코드: " + ", ".join(last_rule_ids) + "\n"
        "가격·방향·확률·목표가는 제공되지 않습니다. 잠시 후 다시 실행해 주세요."
    )



def run_fractal_supplement(phase1_result, symbol, exchange_name):
    """PHASE2 전체가 아닌 [정식 모드 보조 지표] 블록(TF별 Zone/Kz/Kp/게이트 통과 여부)만
    별도로 요청한다. 버튼 클릭 시에만 호출되며, PHASE2 완제 출력에는 포함되지 않는다
    (V003[C] 8항 소급감사 반영 — 팀 결정에 따른 버튼 분리)."""
    fractal_prompt = (
        f"{CANDLEVIEW_PROMPT_FULL}\n\n"
        f"아래는 이미 완성된 PHASE 1 결과입니다.\n\n"
        f"[PHASE 1 완성 결과]\n{phase1_result}\n\n"
        f"PHASE 2 메인 분석은 생성하지 말고, 오직 [정식 모드 보조 지표] 출력 블록(MASTER_SWITCH=3 전용,\n"
        f"선택된 각 TF별 상위 컨테이너 위치·연쇄 전파 단계·계산 적용 계수·계산 보정 점수·게이트 통과 여부)만\n"
        f"엔진 규정의 고정 서식대로 완제 출력하십시오. PHASE 1에서 도출된 데이터만을 근거로 사용하고 새로운\n"
        f"수치를 임의로 추가하지 마십시오."
    )
    return call_gemini_api_with_retry(fractal_prompt, max_tokens=4000)


# ============================================================
# FindCoin — Layer0~1 (Python 선필터, V003[C] 챕터14 FC-0/FC-1)
# 전종목 정직 스캔(절대값 사전압축 없음 — 저가 알트가 상대적 급등을 해도
# 배제되지 않도록 RTM/Percentile은 항상 전체 유효종목 기준으로 계산한다).
# [V003[C] 이중경로] 경로A(RTM+Percentile+Liquidity, 3조건AND)와 경로B(Liquidity+압축조건,
# RTM/Percentile 면제, State1 전용)를 병렬 운영한다. RTM은 이미 발생한 수급폭발만 측정하는
# 후행지표라 경로A만으로는 "아직 조용한 매집" 국면(State1)을 원천적으로 잡을 수 없기 때문이다.
# 가벼운 계산(RTM/압축폭, 일봉만 필요)을 전종목에 먼저 돌리고 무거운 계산(Liquidity_Ratio,
# 호가창조회)은 각 경로 통과자에게만 나중에 적용해 결과 손상 없이 소요시간을 단축한다.
# ============================================================
FC_RTM_MIN = 3.0
FC_PERCENTILE_MIN = 85.0
FC_LIQUIDITY_MIN = 1.0
FC_COMPRESSION_RANGE_MAX = 5.0  # 경로B: 최근 FC_MIN_BARS_STANDARD봉 최고가 대비 변동폭 상한(%) — V003[C] 신설
# [lookback 확장 — 판정47번] 20→40. 근거: 표본 20개에서 단일 이상치 가중치(1/N)=5.0%였던 것을
# 40개로 확대해 2.5%로 완화(통계적 안정성). 40~50 구간 중 40을 채택한 이유는 한계이득(1/(N×(N+1)))이
# 20→40 구간(5.0%→2.5%, -2.5%p)에서 대부분 실현되고 40→50 구간(2.5%→2.0%, -0.5%p)은 체감이 급격히
# 줄어드는 반면, narrow-scope 상태(State1 옥석검증A 등이 여전히 원시봉을 "최근 N봉 내"로 참조)에서는
# 토큰비용이 N에 거의 선형이라 비용 대비 효율이 40 근방에서 꺾이기 때문(사용자 승인, 판정47번).
FC_MIN_BARS_STANDARD = 40
FC_MIN_BARS_REDUCED = 8
FC_NEWCOIN_CONFIDENCE = 0.80
FC_AVG_VOLUME_LOOKBACK_SHORT = 3

# [V003[C] Cowork 재설계 — 진입 타이밍 품질 게이트, 스펙 45번/[FC-1 공통 게이트] 참조]
# 경로A·경로B 공통 적용: 가장 최근 완성봉 레인지가 ATR_STD(TR의 FC_MIN_BARS_STANDARD봉 평균) 대비
# 과도하게 확장됐으면 탈락. 임의 신설 상수 아님 — 본 챕터 State2 RV_t≥2.0, VROC_ACCEL_MAX(2.0)과
# 동일한 "평소 대비 2배=눈에 띄게 발화" SSOT 값 재사용.
# [판정47번 명칭 변경] 기존 'atr_20'/'ATR_20' 표기는 FC_MIN_BARS_STANDARD 값(20)을 이름에 그대로
# 박아넣어, 창 크기가 바뀌면(이번 40) 이름과 실제 계산창이 어긋나는 문제가 있었다. 'ATR_STD'로
# 개명해 이후 FC_MIN_BARS_STANDARD가 다시 바뀌어도 이름 재변경이 불필요하도록 한다.
FC_RANGE_EXPANSION_MAX = 2.0
# [판정50번 — 사용자 승인, 관측모드 종료·하드게이트 전환] 원래 계획("실거래 탈락률을 먼저 관측한 뒤
# 전환")은 Render 무료플랜(로그 영구저장 없음)·캘리브레이션 루프 미구현으로 관측 자체가 불가능해
# 좌초됨 — 이 상태로 무기한 대기하는 건 "판단 보류"가 아니라 "계속 미적용"을 능동 선택하는 것과
# 같음. 재검토 결과 전환 근거: ① 2.0배 임계값은 VROC_ACCEL_MAX(2.0)·State2 RV_t≥2.0과 동일한
# SSOT로, 시스템 다른 곳에서는 이미 라이브로 신뢰 중 — 유독 이 게이트만 보류할 근거가 약함.
# ② 판정45 잔여우려(저유동성 코인 ATR 노이즈발 오탈락)는 lookback 20→40(판정47) 확대로 단일
# 이상치 가중치가 5.0%→2.5%로 줄어 일부 완화됨. 저유동성 코인 오탈락 실제 비율은 여전히 관측
# 불가하므로 완전 해소는 아니며, 이후 인프라가 생기면 재검토 대상(판정이력 대장 원칙상 [해석]
# 등급 재검증 가능).
FC_RANGE_EXPANSION_HARD_GATE = True

# [판정47번 신설 — 모듈1(S_vol_squeeze) 리터럴→SSOT 승격] 스펙 5.[FC-3] 모듈1 공식의 리터럴
# 0.40/0.60을 그대로 승격. Gemini가 원시봉에서 직접 재추정하던 값을 Python이 사전계산해
# 라벨숫자로 전달 — atr_20/range_expansion_ratio와 동일 패턴(재계산 후 치환), 신규 판단기준
# 발명 아님. ratio=최근3봉평균Bar_Range/(ATR_STD+EPSILON): ratio≤LOW→1.0(완전압축),
# ratio≥HIGH→0.0(비압축), 구간내 선형.
FC_VOL_SQUEEZE_RATIO_LOW = 0.40
FC_VOL_SQUEEZE_RATIO_HIGH = 0.60

# [판정52번 신설 — 모듈8(S_boxrange) 박스권 구조 확정] 외부검토(Cowork) 3차례 왕복 후 통합설계.
# FC_BOX_MIN_TOUCHES=2는 신규값 아님 — 13장 용어사전 박스권 정의("SH 2개 이상 및 SL 2개 이상")를
# 그대로 재사용. FC_BOX_TOUCH_BAND_RATIO=0.20(상단/하단 각 20% 밴드)은 이 모듈 고유의 신규 도입값 —
# 표준 지지/저항 zone 폭 관행과 정합, 임의성 최소화를 위해 명시적으로 밝힘(허위 SSOT 주장 금지).
# [설계 결정 — 스윙탐지 대신 밴드접촉횟수 채택] 1차안(스윙포인트 좌우2봉 극값)은 극단압축(레인지가
# 거의 안 움직이는 State1의 이상적 타깃)에서 연속봉이 전부 서로 인접해 스윙이 1개로 뭉개져 탐지
# 실패하는 결함이 시뮬레이션으로 확인됨(가장 타이트한 박스일수록 오히려 못 잡는 역설). 2차 수정안
# (compression≤5% 통과시 자동 신뢰도 부여)은 경로B 게이트 조건과 사실상 동일해 판별력이 사라지는
# 결함이 재발함(진짜 형태 검증이 아니라 이미 아는 정보 재확인에 불과). 밴드접촉횟수 방식은 극단압축
# 에서도 안 죽으면서(모든 봉이 밴드 안에 있으면 접촉조건 자연충족) "몇 번 왕복했는지"라는 새 정보를
# 여전히 판별하므로 두 결함을 동시에 회피한다.
FC_BOX_TOUCH_BAND_RATIO = 0.20
FC_BOX_MIN_TOUCHES = 2

# 시세분출 개념이 적용되지 않는 스테이블코인(quote가 아닌 base가 스테이블인 경우 제외)
FC_STABLECOIN_BASES = {"USDT", "USDC", "DAI", "TUSD", "PAX", "GUSD", "PYUSD", "BUSD", "USDP", "FDUSD"}



def fc_prefilter_universe(exchange, quote):
    """FC-0: 스캔 대상 사전 정제. [결함수정] fetch_tickers()를 인자 없이 호출하면
    일부 거래소(업비트 등, 마켓코드를 명시해야 하는 REST 구조)에서 극소수 결과만
    반환하는 문제가 실사용으로 확인되었다. load_markets()로 이미 확보한 신뢰 가능한
    전체 심볼목록에서 quote 통화에 해당하는 심볼만 명시적으로 골라 fetch_tickers에
    전달하여 전종목이 실제로 조회되도록 한다."""
    try:
        target_symbols = [s for s in exchange.markets if s.endswith(f"/{quote}")]
        if not target_symbols:
            return []
        tickers = exchange.fetch_tickers(target_symbols)
    except Exception as e:
        print(f"[WARN] fetch_tickers 실패: {e}")
        return []

    universe = []
    for symbol, t in tickers.items():
        if not symbol.endswith(f"/{quote}"):
            continue
        base = symbol.split("/")[0]
        if base in FC_STABLECOIN_BASES:
            continue
        market = exchange.markets.get(symbol, {})
        if market.get("active") is False:
            continue
        qv = _safe_float(t.get("quoteVolume"))
        if qv is None or qv <= 0:
            continue
        universe.append({"symbol": symbol, "base": base, "quote_volume_24h": qv, "last": _safe_float(t.get("last"))})
    return universe


def fc_compute_rtm(exchange, item):
    """FC-1[1]: RTM = 오늘 24h 거래대금 / 직전 FC_MIN_BARS_STANDARD일(또는 확보 가능한 만큼) 평균
    24h 거래대금(판정47번: 20→40).
    적응형 룩백(FC-0)도 함께 처리: 확보 봉수에 따라 표준/축소/관측대기로 분류.
    [결함수정] 분자(티커 quoteVolume, 롤링24h·정확값)와 분모(일봉 close×volume 근사,
    캘린더일·근사값)의 측정기준이 서로 달라 RTM이 왜곡되던 문제를 해소한다 — 분자·분모를
    동일 방법론(일봉 기반 close×volume)으로 통일한다. 이 경우 '오늘 진행 중인 봉'은 하루가
    아직 안 끝났으므로 거래량이 항상 과소평가되는 편향이 생기므로, 경과시간 비율로 정규화
    (외삽)한다. 단, 자정 직후처럼 극초반에는 과도한 외삽(수십 배 부풀림)을 막기 위해 경과비율
    하한(10%)을 적용한다."""
    symbol = item["symbol"]
    try:
        # [설계최적화, 판정47번: 30→50] FC_MIN_BARS_STANDARD(40)+여유버퍼(절대량 10, 기존
        # 20봉창/30봉fetch와 동일 버퍼 유지 — limit/N 비율 자체는 1.5→1.25로 달라지지만
        # TR계산 첫 봉이 진짜 prev_close를 참조하도록 보장하는 실질 여유폭(9)은 동일하게 보존됨)를
        # 한 번만 조회해 item에 캐싱한다 — RTM(41봉 필요)뿐 아니라 이후 fc_compute_liquidity_ratio
        # (4봉)·fc_build_candidate_payload(50봉)에서 동일 심볼을 재조회하지 않고 이 캐시를
        # 재사용해 API 호출 중복을 없앤다.
        daily = exchange.fetch_ohlcv(symbol, timeframe="1d", limit=50)
    except Exception as e:
        print(f"[WARN] FindCoin 일봉조회 실패({symbol}): {e}")
        return None

    if not daily or len(daily) < 2:
        item["mode"] = "watch_only"
        item["bar_count"] = len(daily) if daily else 0
        return item

    item["daily_ohlcv_cache"] = daily
    today_bar = daily[-1]
    # RTM 평균은 스펙대로 최근 FC_MIN_BARS_STANDARD영업일(가용한 만큼)만 사용 — 50봉을 조회했다고
    # 평균 기간이 늘어나지 않도록 슬라이싱(판정47번: 20→40)
    past = daily[:-1][-FC_MIN_BARS_STANDARD:]
    bar_count = len(past)

    if bar_count < FC_MIN_BARS_REDUCED:
        item["mode"] = "watch_only"
        item["bar_count"] = bar_count
        return item

    avg_quote_volume = sum(row[4] * row[5] for row in past) / bar_count  # close*volume 근사(거래대금)
    if avg_quote_volume <= 0:
        return None

    # 오늘 진행 중인 봉 시간외삽 (경과비율 하한 10% 적용)
    now_ms = int(time.time() * 1000)
    elapsed_ms = now_ms - today_bar[0]
    elapsed_ratio = max(0.10, min(1.0, elapsed_ms / 86_400_000))
    today_qv_raw = today_bar[4] * today_bar[5]
    today_qv_normalized = today_qv_raw / elapsed_ratio

    rtm = today_qv_normalized / avg_quote_volume
    item["rtm"] = rtm
    item["bar_count"] = bar_count
    item["mode"] = "standard" if bar_count >= FC_MIN_BARS_STANDARD else "reduced"
    item["confidence"] = 1.0 if item["mode"] == "standard" else FC_NEWCOIN_CONFIDENCE
    return item


def fc_compute_compression(item):
    """FC-1 경로B[신규 — V003[C]]: 최근 FC_MIN_BARS_STANDARD봉(또는 확보 봉수)의 레인지 폭
    (고가~저가)을 계산한다(판정47번: 20→40). fc_compute_rtm에서 이미 캐싱한 daily_ohlcv_cache를
    재사용 — 신규 API 호출 없음. State1(분출 전 매집) 진단의 핵심 조건이며, RTM/Percentile과
    무관하게 독립 판정한다.
    [재정의-Grok검토42] 기존에는 "최고가 대비 현재가 거리"를 측정했으나, 이는 VCP(변동성수축
    패턴)가 실제로 의미하는 "레인지 폭 자체의 수축"과 다른 개념이었다(반례: 고100/저50/현재95는
    거리 5%로 통과하지만 실제 레인지는 50%로 전혀 압축 아님). 레인지 폭(고가~저가)/고가로
    재정의하여 설계근거(미네르비니 VCP 최종수축단계)와 실제 측정대상을 일치시킨다."""
    daily = item.get("daily_ohlcv_cache")
    if not daily or len(daily) < 2:
        item["compression_pct"] = None
        return item
    past = daily[:-1][-FC_MIN_BARS_STANDARD:]  # RTM과 동일한 "최근 FC_MIN_BARS_STANDARD봉(또는 확보 봉수)" 윈도우
    if not past:
        item["compression_pct"] = None
        return item
    period_high = max(row[2] for row in past)  # OHLCV 인덱스: [ts,o,h,l,c,v] → high=2
    period_low = min(row[3] for row in past)   # low=3
    current_price = daily[-1][4]  # 오늘 진행봉 종가(현재가)
    if period_high <= 0:
        item["compression_pct"] = None
        return item
    if current_price > period_high:
        # [결함수정-Cowork35] State1은 정의상 "분출 전(Pre-Breakout)" 상태여야 한다.
        # 이미 고점을 돌파한 코인은 압축후보에서 명시적으로 제외한다.
        item["compression_pct"] = None
        return item
    item["compression_pct"] = (period_high - period_low) / period_high * 100.0
    return item


def fc_compute_range_expansion(item):
    """FC-1 공통 게이트[신규 — V003[C] Cowork 재설계]: 가장 최근 완성봉(전일 종가 확정봉,
    당일 진행봉의 직전 봉)의 레인지(고가-저가)가 ATR_STD(TR의 FC_MIN_BARS_STANDARD봉 평균) 대비
    과도하게 확장됐는지 판정한다. [판정47번] 기존 'atr_20' 명칭은 FC_MIN_BARS_STANDARD 값(20)을
    이름에 내장해, 창 크기가 바뀌면(이번 40) 이름과 실제 계산창이 어긋났다 — 'atr_std'로 개명해
    이후 재변경 시에도 이름 재수정이 불필요하도록 한다.
    경로A·경로B 공통 적용. fc_compute_rtm에서 이미 캐싱한 daily_ohlcv_cache 재사용 —
    신규 API 호출 없음.
    [설계근거] 당일 진행봉(미종료 봉)을 검사 대상으로 쓰면 스캔 시각(예: 자정 직후 vs 늦은 밤)에
    따라 결과가 달라지는 시간대 편향이 생긴다. RTM(거래대금, 합 통계량)은 진행봉을 경과시간
    비율로 외삽하지만, 레인지는 극값(max-min) 통계량이라 단발성 점프에 지배되고 이 게이트가
    감지하려는 현상(시세분출) 자체가 그 점프이므로 시간비례 외삽의 전제와 모순된다. 대신
    9대 예외규칙 3항(몸통 마감 우선의 원칙)·[진행봉 처리 통일 규칙]을 그대로 원용해 당일
    진행봉 대신 가장 최근 완성봉만을 검사 대상으로 삼는다(신규 원칙 발명 아님).
    R:R(FC-4~FC-5, 구조적 여력)과는 다른 축(진입 타이밍 품질)이라 병존하며, 서로 대체하지 않는다.
    [판정47번 신설] ATR_STD 계산 김에 모듈1(S_vol_squeeze, 스펙 5.[FC-3] 참조)도 함께
    사전계산해 item에 저장한다 — Gemini가 동일 값을 원시봉에서 재추정하며 생길 수 있는
    산술불일치를 없앤다(compression_pct/range_expansion_ratio와 동일한 재계산-치환 패턴,
    신규 판단기준 발명 아님, 스펙 리터럴 0.40/0.60 → FC_VOL_SQUEEZE_RATIO_LOW/HIGH SSOT 재사용).
    """
    daily = item.get("daily_ohlcv_cache")
    if not daily or len(daily) < 3:
        item["range_expansion_ok"] = True  # 데이터 부족 시 게이트 미적용(안전 폴백, 탈락시키지 않음)
        item["s_vol_squeeze"] = None  # 데이터부족 — 억지로 만들지 않고 미산출 명시
        return item

    completed = daily[:-1]  # 오늘 진행봉 제외, 전체 완성봉
    past = completed[-FC_MIN_BARS_STANDARD:]  # RTM/압축과 동일한 "최근 FC_MIN_BARS_STANDARD봉(또는 확보 봉수)" 윈도우
    if len(past) < 2:
        item["range_expansion_ok"] = True
        item["s_vol_squeeze"] = None
        return item

    start_idx = len(completed) - len(past)
    trs = []
    for i in range(start_idx, len(completed)):
        h, l = completed[i][2], completed[i][3]
        # 직전 종가 참조 불가(윈도우 최초 봉이면서 그 이전 데이터도 없는 경우)면 자기 종가로
        # 대체해 TR이 단순 H-L로 축소되도록 안전 폴백한다(크래시 방지, 결측 처리 관행과 동일).
        prev_c = completed[i - 1][4] if i - 1 >= 0 else completed[i][4]
        trs.append(max(h - l, abs(h - prev_c), abs(l - prev_c)))

    if not trs:
        item["range_expansion_ok"] = True
        item["s_vol_squeeze"] = None
        return item

    atr_std = sum(trs) / len(trs)
    last_bar = past[-1]  # 가장 최근 완성봉
    r_last = last_bar[2] - last_bar[3]

    item["atr_std"] = atr_std
    item["atr_std_n"] = len(past)  # [판정48번] 실제 사용된 봉수(축소모드에선 FC_MIN_BARS_STANDARD보다 작을 수 있음) — payload 라벨 정확도용
    item["range_expansion_ratio"] = r_last / (atr_std + 1e-8)
    item["range_expansion_ok"] = item["range_expansion_ratio"] < FC_RANGE_EXPANSION_MAX

    # [모듈1 S_vol_squeeze — 판정47번 신규] 최근 3완성봉 평균 Bar_Range / ATR_STD.
    # 스펙 5.[FC-3] 모듈1 공식 그대로 이식(리터럴→SSOT는 FC_VOL_SQUEEZE_RATIO_LOW/HIGH 재사용).
    if len(past) < 3:
        item["s_vol_squeeze"] = None  # 데이터부족(3봉 미만) — 억지로 만들지 않고 미산출 명시
    else:
        recent3 = past[-3:]
        avg_range_recent3 = sum(row[2] - row[3] for row in recent3) / 3
        vol_squeeze_ratio = avg_range_recent3 / (atr_std + 1e-8)
        item["vol_squeeze_ratio"] = vol_squeeze_ratio
        if vol_squeeze_ratio <= FC_VOL_SQUEEZE_RATIO_LOW:
            item["s_vol_squeeze"] = 1.0
        elif vol_squeeze_ratio >= FC_VOL_SQUEEZE_RATIO_HIGH:
            item["s_vol_squeeze"] = 0.0
        else:
            item["s_vol_squeeze"] = (
                (FC_VOL_SQUEEZE_RATIO_HIGH - vol_squeeze_ratio)
                / (FC_VOL_SQUEEZE_RATIO_HIGH - FC_VOL_SQUEEZE_RATIO_LOW)
            )
    return item


def fc_compute_box_range(item):
    """FC-3 [모듈8] 박스권 구조 확정(S_boxrange) — 판정52번 신설, 외부검토(Cowork) 3차 왕복 후
    통합설계 채택. fc_compute_rtm이 이미 캐싱한 daily_ohlcv_cache를 그대로 재사용한다 — 신규 API
    호출·별도 캐시 키 없음(1차 외부제안이었던 100봉 별도fetch는, 신규fetch 결과를 daily_ohlcv_cache에
    합치면 fc_build_candidate_payload가 그 캐시를 그대로 원시봉 dump하는 지점(판정47/48 라벨정확도
    문제와 동일 구조)과 충돌해 토큰비용이 재발할 위험이 있어 채택하지 않음 — 기존 FC_MIN_BARS_STANDARD
    창을 그대로 씀).
    [설계] box_high/box_low = 창 내 최고가/최저가(단순 max/min, 스윙탐지 불필요 — 평탄 데이터에서도
    안 죽음). box_range_pct는 compression_pct와 동일하게 고가 기준 분모로 통일(1차 검토에서 mid
    기준으로 설계하면 같은 이름의 COMPRESSION_RANGE_MAX 상수를 다른 기준으로 재사용하게 되는
    구문-의미 불일치를 자체발견해 정정). 상단/하단 각 FC_BOX_TOUCH_BAND_RATIO(20%) 밴드에 각각
    FC_BOX_MIN_TOUCHES(2)회 이상 접촉해야 확정 — "몇 번 왕복했는지"를 재는 게 핵심이라 compression_pct
    (그냥 지금 폭이 좁은지)와는 다른 새 정보를 제공한다.
    """
    daily = item.get("daily_ohlcv_cache")
    if not daily or len(daily) < 2:
        item["s_boxrange"] = None
        item["box_status"] = "insufficient_data"
        return item

    completed = daily[:-1]  # 오늘 진행봉 제외 — compression_pct/range_expansion과 동일 원칙
    past = completed[-FC_MIN_BARS_STANDARD:]
    if len(past) < 2:
        item["s_boxrange"] = None
        item["box_status"] = "insufficient_data"
        return item

    box_high = max(row[2] for row in past)  # OHLCV 인덱스: high=2
    box_low = min(row[3] for row in past)   # low=3
    if box_high <= 0:
        item["s_boxrange"] = None
        item["box_status"] = "insufficient_data"
        return item

    box_range_pct = (box_high - box_low) / box_high * 100.0  # compression_pct와 동일 분모(고가) 기준

    current_price = daily[-1][4]  # 오늘 진행봉 종가 — compression_pct의 이미돌파 체크와 동일 패턴
    if current_price > box_high:
        item["s_boxrange"] = 0.0
        item["box_status"] = "already_broken"
        return item

    band_width = (box_high - box_low) * FC_BOX_TOUCH_BAND_RATIO
    upper_band = box_high - band_width
    lower_band = box_low + band_width

    def _count_touch_episodes(flags):
        # [적대적 자체테스트로 발견·수정] 단순 봉수 합산은 단조추세가 밴드를 "왕복"이 아니라
        # "한 번 통과"만 해도 여러 봉이 연속으로 카운트돼 오확정을 유발함(실제 시뮬레이션으로
        # 확인). 연속된 True 구간을 1회 방문(에피소드)으로만 세어, "몇 번 왕복했는지"를
        # "몇 봉이 걸쳐있는지"와 구분한다.
        episodes = 0
        prev = False
        for flag in flags:
            if flag and not prev:
                episodes += 1
            prev = flag
        return episodes

    upper_touches = _count_touch_episodes([row[2] >= upper_band for row in past])
    lower_touches = _count_touch_episodes([row[3] <= lower_band for row in past])
    item["box_upper_touches"] = upper_touches
    item["box_lower_touches"] = lower_touches

    if upper_touches < FC_BOX_MIN_TOUCHES or lower_touches < FC_BOX_MIN_TOUCHES:
        item["s_boxrange"] = 0.0
        item["box_status"] = "not_confirmed"
        return item

    tightness = max(0.0, 1.0 - min(box_range_pct / FC_COMPRESSION_RANGE_MAX, 1.0))
    touch_factor = min(
        ((upper_touches - FC_BOX_MIN_TOUCHES) + (lower_touches - FC_BOX_MIN_TOUCHES)) / 4.0,
        1.0,
    )
    item["s_boxrange"] = max(0.0, min(1.0, 0.5 * tightness + 0.5 * touch_factor))
    item["box_status"] = "confirmed"
    return item


def fc_compute_liquidity_ratio(exchange, item):
    """FC-1[3]: Liquidity_Ratio = (매수10호가+매도10호가 총잔량) / 최근3봉 평균거래량.
    RTM+Percentile을 이미 통과한 후보에게만 적용(무거운 호출을 최소화).
    [설계최적화] fc_compute_rtm에서 캐싱한 일봉데이터를 재사용하여 중복 API호출을 없앤다."""
    symbol = item["symbol"]
    try:
        book = exchange.fetch_order_book(symbol, limit=10)
        bid_depth = sum(a for _, a in (book.get("bids") or []))
        ask_depth = sum(a for _, a in (book.get("asks") or []))

        cached = item.get("daily_ohlcv_cache")
        if not cached or len(cached) < 2:
            # 캐시 부재(정상 흐름에선 발생하지 않음) 시에만 안전 폴백 조회
            cached = exchange.fetch_ohlcv(symbol, timeframe="1d", limit=FC_AVG_VOLUME_LOOKBACK_SHORT + 1)
        if not cached or len(cached) < 2:
            return None

        past = cached[:-1][-FC_AVG_VOLUME_LOOKBACK_SHORT:]
        if not past:
            return None
        avg_vol3 = sum(row[5] for row in past) / len(past)
        liquidity_ratio = (bid_depth + ask_depth) / (avg_vol3 + 1e-8)
        item["liquidity_ratio"] = liquidity_ratio
        return item
    except Exception as e:
        print(f"[WARN] FindCoin 유동성조회 실패({symbol}): {e}")
        return None


def run_findcoin_scan(ex_name):
    """FC-0~FC-1 전체 파이프라인. [V003[C] 이중경로] 경로A(RTM/Percentile/Liquidity, State2/3용)와
    경로B(Liquidity/압축조건, State1 전용, RTM·Percentile 면제)를 병렬 실행 후 병합한다.
    후보 상한 배분: 경로A를 먼저 채우고 잔여 슬롯만 경로B에 배정(기존 State확정성 우선순위
    원칙 재사용 — 별도 배분규칙 신설 없음)."""
    quote = SUPPORTED_EXCHANGES[ex_name]["quote"]
    exchange_class = getattr(ccxt, ex_name)({"enableRateLimit": True, "timeout": 8000})
    try:
        exchange_class.load_markets()
    except Exception as e:
        print(f"[WARN] FindCoin load_markets 실패({ex_name}): {e}")
        return None, 0, 0, 0, 0, []

    universe = fc_prefilter_universe(exchange_class, quote)
    n_total = len(universe)
    if n_total == 0:
        return [], 0, 0, 0, 0, []

    # 1단계(가벼움, 전종목): RTM 계산
    rtm_results, watch_only = [], []
    for item in universe:
        r = fc_compute_rtm(exchange_class, item)
        if r is None:
            continue
        if r.get("mode") == "watch_only":
            watch_only.append(r)
        else:
            rtm_results.append(r)
    n_valid = len(rtm_results)

    if n_valid == 0:
        return [], n_total, 0, 0, 0, watch_only

    # Percentile_Rank: 전체 유효종목(N_valid) 기준 RTM 내림차순 순위
    rtm_results.sort(key=lambda x: x["rtm"], reverse=True)
    for rank, item in enumerate(rtm_results, start=1):
        item["percentile_rank"] = (1 - (rank - 1) / n_valid) * 100.0

    # [FC-1 공통 게이트 — V003[C] Cowork 재설계] 경로A·B 공통 적용 조건이므로 전체 유효종목에
    # 1회만 계산해 두 경로가 공유한다(중복 계산 방지). 신규 API 호출 없음(daily_ohlcv_cache 재사용).
    for item in rtm_results:
        fc_compute_range_expansion(item)
    _range_gate_flagged = sum(1 for it in rtm_results if it.get("range_expansion_ok") is False)
    if _range_gate_flagged:
        _mode_label = "하드게이트" if FC_RANGE_EXPANSION_HARD_GATE else "관측모드(미적용)"
        print(f"[INFO] FC-1 진입타이밍 게이트({_mode_label}): 유효종목 {n_valid}개 중 "
              f"{_range_gate_flagged}개가 R_last/ATR_STD≥{FC_RANGE_EXPANSION_MAX} 조건 해당")

    # [V003[C] Cowork 재설계, 판정50번 갱신] 공통 게이트 통과 여부. FC_RANGE_EXPANSION_HARD_GATE
    # 스위치값에 따라 동적 결정(True=하드탈락, False=항상 통과 취급) — 스위치 하나로 즉시 온/오프.
    def _range_ok(it):
        return it.get("range_expansion_ok", True) or not FC_RANGE_EXPANSION_HARD_GATE

    # 경로A(기존): RTM+Percentile 통과자. [V003[C] Cowork 재설계] 공통 게이트 추가.
    path_a_pool = [it for it in rtm_results if it["rtm"] >= FC_RTM_MIN
                   and it["percentile_rank"] >= FC_PERCENTILE_MIN and _range_ok(it)]

    # 경로B(신규 — V003[C]): 압축조건 통과자. RTM/Percentile 무관, 전체 유효종목 대상 산술 계산(API 호출 없음).
    # [V003[C] Cowork 재설계] 공통 게이트 추가.
    path_b_pool = []
    for item in rtm_results:
        if not _range_ok(item):
            continue
        r = fc_compute_compression(item)
        if r.get("compression_pct") is not None and r["compression_pct"] <= FC_COMPRESSION_RANGE_MAX:
            path_b_pool.append(r)

    a_symbols_pool = {it["symbol"] for it in path_a_pool}
    b_symbols_pool = {it["symbol"] for it in path_b_pool}
    n_gate1 = len(a_symbols_pool | b_symbols_pool)

    # [V003[C] 실효성 수정] path_b_pool은 상승장 등에서 수백 개까지 불어날 수 있는데, 이후
    # fc_compute_liquidity_ratio(호가창 조회, 무거운 API 호출)를 상한 없이 전부에 돌리면
    # 최종적으로 20개만 쓰면서 불필요하게 대량 호출을 하게 된다. 압축도(변동폭) 오름차순으로
    # 미리 정렬 후 FC_MAX_CANDIDATES_TO_LLM만큼만 잘라 무거운 호출 대상 자체를 제한한다
    # (최종 출력이 20개를 넘길 수 없으므로 사전 제한이 결과에 영향을 주지 않는다).
    path_b_pool.sort(key=lambda x: x.get("compression_pct", 999.0))
    path_b_pool = path_b_pool[:FC_MAX_CANDIDATES_TO_LLM]

    # 2단계(무거움, 각 경로 통과자만): Liquidity_Ratio 계산. 경로A 우선 조회 — 중복종목은 경로A로 귀속.
    path_a_final, a_symbols_final = [], set()
    for item in path_a_pool:
        r = fc_compute_liquidity_ratio(exchange_class, item)
        if r and r["liquidity_ratio"] >= FC_LIQUIDITY_MIN:
            r["fc_path"] = "A"
            path_a_final.append(r)
            a_symbols_final.add(r["symbol"])

    path_b_final = []
    for item in path_b_pool:
        if item["symbol"] in a_symbols_final:
            continue  # 이미 경로A로 확정된 종목은 중복 조회하지 않음
        r = fc_compute_liquidity_ratio(exchange_class, item)
        if r and r["liquidity_ratio"] >= FC_LIQUIDITY_MIN:
            r["fc_path"] = "B"
            path_b_final.append(r)
    # 경로B는 이미 압축도 오름차순으로 정렬된 상태에서 순회했으므로 결과 순서도 유지된다.

    # [V003[C] 후보 상한 배분] 경로A 우선, 잔여 슬롯만 경로B
    final_candidates = (path_a_final + path_b_final)[:FC_MAX_CANDIDATES_TO_LLM]
    n_gate2 = len(final_candidates)  # 실제 LLM 상세 판정 입력 수와 동일

    return final_candidates, n_total, n_valid, n_gate1, n_gate2, watch_only


# ============================================================
# FindCoin — Layer2~3 (Gemini 위임, V003[C] 챕터14 FC-2~FC-5)
# 이중경로(경로A/경로B) 통과 후보만 상세 캔들데이터를 붙여 Gemini에 전달, State 진단·
# 옥석검증·S_scout·손익비·Top3 선정까지 엔진 규칙대로 수행시킨다.
# ============================================================
FC_MAX_CANDIDATES_TO_LLM = 20  # 토큰 절약을 위한 상한(초과 시 경로A 우선, 잔여슬롯만 경로B — RTM정렬 아님)


def fc_build_candidate_payload(exchange_class, candidates, btc_daily=None, ex_name=None):
    """최종 후보 각각에 대해 대표 TF(1일봉) 상세 데이터를 붙여 Gemini 입력 payload 구성.
    [설계최적화] fc_compute_rtm에서 이미 조회·캐싱된 50봉 데이터를 재사용한다(판정47번: 30→50).
    [V003[C] 이중경로] 상한 초과 시 RTM 내림차순 정렬을 쓰지 않는다 — 경로B(압축조건 통과,
    저RTM이 정상인 조용한 매집 후보)가 구조적으로 항상 먼저 잘려나가 경로B 도입 취지가
    무력화되기 때문. 대신 호출측(run_findcoin_scan)이 이미 '경로A 우선, 경로B 압축도순'으로
    정렬해 넘긴 순서를 그대로 유지한 채 앞에서부터 자른다.
    [V003[C] 모듈7] btc_daily가 주어지면 상대강도(FC-3 모듈7) 계산용 BTC 참조 일봉을 payload
    맨 앞에 1회만 포함한다(코인별 반복 포함하지 않음).
    [결함수정] ex_name이 주어지면 정확한 한글명(거래소 API 원본)을 payload에 명시한다 —
    한글명을 누락하면 Gemini가 티커만 보고 자기 기억으로 창작해 오매칭(예: CBK를 "무비블록"으로
    오기, 실제로는 코박토큰)을 일으키는 근본원인이었다."""
    if len(candidates) > FC_MAX_CANDIDATES_TO_LLM:
        candidates = candidates[:FC_MAX_CANDIDATES_TO_LLM]

    n_path_a = sum(1 for c in candidates if c.get("fc_path") == "A")
    n_path_b = sum(1 for c in candidates if c.get("fc_path") == "B")
    payload = f"[FindCoin 이중경로 통과 후보: {len(candidates)}개 (경로A {n_path_a} / 경로B {n_path_b})]\n"

    if btc_daily:
        payload += "\n=== BTC 참조 데이터 (FC-3 모듈7 상대강도 계산용 기준자산) ===\n"
        n_btc = len(btc_daily)
        for i, row in enumerate(btc_daily):
            tag = " [진행봉-미종료]" if i == n_btc - 1 else ""
            payload += f"O:{row[1]} H:{row[2]} L:{row[3]} C:{row[4]} V:{row[5]:.2f}{tag}\n"

    for c in candidates:
        path_label = c.get("fc_path", "?")
        base = c["symbol"].split("/")[0]
        k_name = resolve_symbol_korean_name(base, ex_name) if ex_name else None
        name_label = f"{k_name}({c['symbol']})" if k_name else f"{c['symbol']}(한글명 미확인 — 티커만 사용, 임의 한글명 창작 금지)"
        extra = (f"압축폭: {c['compression_pct']:.2f}%" if path_label == "B" and c.get("compression_pct") is not None
                 else f"RTM: {c['rtm']:.2f} | Percentile: {c['percentile_rank']:.1f}%")
        payload += (
            f"\n=== {name_label} [경로{path_label}] ===\n"
            f"{extra} | Liquidity_Ratio: {c['liquidity_ratio']:.2f} | 모드: {c['mode']} "
            f"(신뢰도계수 {c['confidence']})\n"
        )
        # [판정47번 신설] ATR_STD/S_vol_squeeze는 fc_compute_range_expansion이 이미 Python으로
        # 정확히 계산해둔 값 — 아래 원시봉을 Gemini가 다시 훑어 재추정할 필요 없이 이 라벨값을
        # 그대로 모듈1(S_vol_squeeze) 산출에 채택하도록 명시한다(재계산-치환 패턴, compression_pct와 동일).
        if c.get("atr_std") is not None:
            vsq = c.get("s_vol_squeeze")
            vsq_label = f"{vsq:.2f}(이 값을 그대로 채택, 재계산 불필요)" if vsq is not None else "데이터부족(3봉 미만)-미산출"
            atr_n = c.get("atr_std_n", FC_MIN_BARS_STANDARD)
            atr_n_note = "" if atr_n == FC_MIN_BARS_STANDARD else f"(축소모드-실제사용봉수, 표준은 {FC_MIN_BARS_STANDARD})"
            payload += (
                f"[사전계산됨] ATR_STD({atr_n}봉평균TR{atr_n_note}): {c['atr_std']} | "
                f"RangeExpansion_ratio: {c.get('range_expansion_ratio', 0):.2f} | "
                f"모듈1 S_vol_squeeze: {vsq_label}\n"
            )
        # [판정52번 신설 — 모듈8 S_boxrange] FC-1 통과 후 payload에 실릴 최종후보(~20개)에서만 계산 —
        # FC-1 공통게이트(range_expansion)와 달리 전체유효종목(N_valid, 수백~수천)에 적용할 필요가
        # 없는 FC-3 랭킹전용 소프트모듈이라 여기서 지연계산한다(불필요한 전역연산 회피).
        fc_compute_box_range(c)
        if c.get("s_boxrange") is not None:
            box_label = f"{c['s_boxrange']:.2f}(이 값을 그대로 채택, 재계산 불필요)"
            payload += (
                f"[사전계산됨] 모듈8 S_boxrange: {box_label} | 상태: {c.get('box_status')} | "
                f"상단접촉: {c.get('box_upper_touches', 0)}회 | 하단접촉: {c.get('box_lower_touches', 0)}회\n"
            )
        try:
            ohlcv = c.get("daily_ohlcv_cache")
            if not ohlcv:
                # 캐시 부재(정상 흐름에선 발생하지 않음) 시에만 안전 폴백 조회 — 본 fetch(FC_MIN_BARS_STANDARD
                # 기반 caller)와 동일 크기로 동기화(판정47번: 30→50, 불일치 시 폴백경로만 축소창으로
                # 조용히 분석되는 회귀위험 방지)
                ohlcv = exchange_class.fetch_ohlcv(c["symbol"], timeframe="1d", limit=50)
            df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
            n_rows = len(df)
            for i, (_, row) in enumerate(df.iterrows()):
                is_last = (i == n_rows - 1)
                tag = " [진행봉-미종료]" if is_last else ""
                payload += (
                    f"O:{row['open']} H:{row['high']} L:{row['low']} "
                    f"C:{row['close']} V:{row['volume']:.2f}{tag}\n"
                )
        except Exception as e:
            print(f"[WARN] FindCoin 후보 캔들조회 실패({c['symbol']}): {e}")
            payload += "(캔들 데이터 조회 실패 — 이 후보는 판정에서 제외)\n"
    return payload, candidates



def fc_extract_top_symbols(result_text, used_candidates):
    """Gemini 응답 맨 끝의 [FINDCOIN_TOP_SYMBOLS] 태그만 정규식으로 추출한다
    (본문 자유서술 파싱 금지 — 고정형식 태그 1줄만 대상으로 하여 파싱 위험을 최소화).
    추출된 심볼은 반드시 이미 알고 있는 후보목록(used_candidates)에 실존하는 것만
    채택하여, Gemini가 목록에 없는 심볼을 지어내더라도 안전하게 걸러낸다."""
    known_symbols = {c["symbol"] for c in used_candidates}
    m = re.search(r"\[FINDCOIN_TOP_SYMBOLS\]\s*(.*)", result_text)
    if not m:
        return []
    raw = m.group(1).strip()
    if not raw:
        return []
    candidates_in_tag = [s.strip() for s in raw.split("|") if s.strip()]
    return [s for s in candidates_in_tag if s in known_symbols][:3]


def fc_strip_top_symbols_tag(result_text):
    """사용자에게 보여줄 텍스트에서 시스템 연동용 태그 줄만 제거한다."""
    return re.sub(r"\n?\[FINDCOIN_TOP_SYMBOLS\].*", "", result_text).strip()


def run_findcoin(ex_name):
    """FindCoin 전체 실행: Layer0~1(Python) → Layer2~5(Gemini) → 구조화 결과 반환.
    반환: (raw_gemini_text, n_total, n_valid, n_gate1, n_gate2, error_message)"""
    quote = SUPPORTED_EXCHANGES[ex_name]["quote"]
    ex_display = SUPPORTED_EXCHANGES[ex_name]["kr_name"]

    scan = run_findcoin_scan(ex_name)
    candidates, n_total, n_valid, n_gate1, n_gate2, watch_only = scan

    if candidates is None:
        return None, 0, 0, 0, 0, f"{ex_display} 시장 데이터를 불러오지 못했습니다.\n잠시 후 다시 시도해 주세요.", [], 0

    n_watch = len(watch_only)

    if n_gate2 == 0:
        return "", n_total, n_valid, n_gate1, n_gate2, None, [], n_watch

    exchange_class = getattr(ccxt, ex_name)({"enableRateLimit": True, "timeout": 8000})
    try:
        exchange_class.load_markets()
    except Exception as e:
        print(f"[WARN] FindCoin 2차 load_markets 실패({ex_name}): {e}")

    # [V003[C] 모듈7] 상대강도 계산용 BTC 참조 일봉 — 거래소당 1회만 조회(코인별 반복 조회 아님).
    # 실패해도 FindCoin 전체를 중단하지 않고 모듈7만 데이터없음으로 처리되도록 None 허용.
    btc_daily = None
    try:
        btc_symbol = f"BTC/{quote}"
        btc_daily = exchange_class.fetch_ohlcv(btc_symbol, timeframe="1d", limit=30)
    except Exception as e:
        print(f"[WARN] FindCoin BTC 참조데이터 조회 실패({ex_name}): {e}")

    candidate_payload, used_candidates = fc_build_candidate_payload(exchange_class, candidates, btc_daily, ex_name)

    # [결함수정] 서버 타임존과 무관하게 정확한 KST(UTC+9) 시각을 명시적으로 계산해 전달한다.
    # 이 값을 프롬프트에 데이터로 주지 않으면 Gemini가 출력서식의 "스캔 시각"·통계 항목을
    # 채우기 위해 훈련데이터 시점의 임의 날짜·수치를 지어내는 환각이 발생한다(실사용에서 확인됨).
    scan_time_kst = (datetime.now(timezone.utc) + timedelta(hours=9)).strftime("%Y-%m-%d %H:%M")

    fc_prompt = (
        f"{CANDLEVIEW_PROMPT_FULL}\n\n"
        f"지금부터 14장 FindCoin 플러그인 모듈만 실행하십시오. 본체 PHASE1/2는 실행하지 마십시오.\n\n"
        f"[시스템 제공 실측 데이터 — 아래 수치를 반드시 그대로 사용하고 임의로 생성·추정하지 마십시오]\n"
        f"스캔 시각: {scan_time_kst} (KST)\n"
        f"대상 거래소: {ex_display} ({quote} 마켓)\n"
        f"총 스캔 종목(N_total): {n_total}개\n"
        f"유효 종목(N_valid, 사전정제 통과): {n_valid}개\n"
        f"1차 경로 통과(경로A∪경로B 수식 게이트, N_gate1): {n_gate1}개\n"
        f"상세 판정 후보(유동성 통과 후 실제 LLM 입력, N_gate2): {n_gate2}개\n\n"
        f"{candidate_payload}\n\n"
        f"위 후보 각각에 FC-2(State 자동진단 및 우선순위 2>3>1) → FC-3(8대 미시모듈 및 S_scout 집계; "
        f"S_boxrange를 포함하고 FindCoin 전용 상수의 8모듈 가중치만 사용) "
        f"→ FC-4(진입가·손익비, 본체 정의 상속) → FC-5(이중게이트 통과판정) 순서로 적용하고, "
        f"FC-6의 고정 출력 서식대로 최종 결과를 완제 출력하십시오. 각 후보 표기의 [경로A]/[경로B]는 "
        f"어느 진입경로로 후보에 포함됐는지를 나타내며, [경로B]는 RTM·Percentile 요건이 면제된 State1 "
        f"전용 진입이므로 State1 진단 시 이 점을 참고하십시오(RTM 조건 재요구 금지 — 이미 폐지됨). "
        f"출력서식의 스캔시각·총스캔종목·유효종목·단계별 통과 수치는 반드시 위 [시스템 제공 실측 데이터]를 "
        f"그대로 사용하십시오. S_scout 미달 후보를 억지로 포함하지 마십시오.\n\n"
        f"[시스템 연동용 필수 마지막 줄 — 반드시 응답 맨 끝에 이 형식 그대로 정확히 한 줄 추가]\n"
        f"최종 합격한 코인의 심볼만(위 후보 목록에 있던 심볼 표기 그대로, 예: XRP/KRW) "
        f"통과 순위대로 파이프(|)로 구분하여 아래 형식으로 적으십시오. 0개 합격 시에도 태그 자체는 빈 값으로 출력하십시오.\n"
        f"[FINDCOIN_TOP_SYMBOLS] 심볼1|심볼2|심볼3"
    )

    result_text = call_gemini_api_with_retry(fc_prompt, max_tokens=12000)
    if result_text.startswith("AI 서버 일시적 과부하"):
        return None, n_total, n_valid, n_gate1, n_gate2, result_text, [], n_watch
    top_symbols = fc_extract_top_symbols(result_text, used_candidates)
    display_text = fc_strip_top_symbols_tag(result_text)
    return display_text, n_total, n_valid, n_gate1, n_gate2, None, top_symbols, n_watch


# ============================================================
# 인라인 키보드 생성
# callback_data에는 서버 발급 분석 세션 토큰만 실어 Telegram 64바이트 제한을 지킨다.
# 세션 만료·프로세스 재시작 뒤에는 새 시장 데이터를 위해 명시 재명령을 요구한다.
# ============================================================
def make_phase2_retry_keyboard(session_id):
    """P2M03에서만 제공하는 동일 세션·동일 모델 PHASE 2 재시도 버튼."""
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("analysis session id is required")
    payload = f"{CALLBACK_PROTOCOL}|phase2_retry|{session_id}"
    if len(payload.encode("utf-8")) > 64:
        raise ValueError("analysis callback payload exceeds Telegram 64-byte limit")
    return {"inline_keyboard": [[{"text": "🔄 Phase2 동일 모델로 다시 시도", "callback_data": payload}]]}


def make_phase_keyboard(session_id):
    """분석 세션 토큰만 포함하는 inline keyboard를 생성한다.
    사용자의 원문 심볼·한글명·TF는 callback_data에 싣지 않으므로 64바이트 제한과 TF 축약이 발생하지 않는다.
    """
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("analysis session id is required")

    def _callback(action):
        payload = f"{CALLBACK_PROTOCOL}|{action}|{session_id}"
        if len(payload.encode("utf-8")) > 64:
            raise ValueError("analysis callback payload exceeds Telegram 64-byte limit")
        return payload

    return {
        "inline_keyboard": [
            [{"text": "📊 코인 최종 분석내용 보기", "callback_data": _callback("phase2_run")}],
            [
                {"text": "📋 수집데이터", "callback_data": _callback("phase1_view")},
                {"text": "📈 보간지표", "callback_data": _callback("supplement_view")},
                {"text": "🔬 정식모드", "callback_data": _callback("fractal_view")},
            ],
        ]
    }


def make_findcoin_detail_keyboard(ex_name, top_symbols):
    """FindCoin 결과의 TOP1~3 상세분석 버튼. 코인명은 콜백데이터에만 싣고
    버튼 라벨은 순위만 표시한다(본문 파싱 리스크 최소화 + 간결한 UI)."""
    rank_labels = ["✅️ TOP1 코인 상세분석 하기", "✅️ TOP2 코인 상세분석 하기", "✅️ TOP3 코인 상세분석 하기"]
    rows = []
    for i, symbol in enumerate(top_symbols[:3]):
        symbol_clean = symbol.split("/")[0]
        payload = f"fc_detail|{ex_name}|{symbol_clean}"
        if len(payload.encode("utf-8")) > 64:
            continue  # 비정상적으로 긴 심볼은 안전하게 건너뜀(발생 가능성 매우 낮음)
        rows.append([{"text": rank_labels[i], "callback_data": payload}])
    return {"inline_keyboard": rows} if rows else None


# ============================================================
# 텔레그램 메인 루프
# ============================================================
print("🚀 CandleView 봇 가동 시작 (Inline Mode)")

try:
    del_res = requests.get(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteWebhook",
        timeout=10
    )
    print(f"[INFO] deleteWebhook 결과: {del_res.json()}")
except Exception as e:
    print(f"[WARN] deleteWebhook 실패: {e}")

last_update_id = 0

while True:
    try:
        clean_expired_cache()

        url = (
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
            f"?offset={last_update_id + 1}&timeout=30"
        )
        updates = requests.get(url, timeout=35).json()

        if not updates.get("ok"):
            print(f"[WARN] getUpdates 오류: {updates}")
            time.sleep(3)
            continue

        for update in updates.get("result", []):
            last_update_id = update["update_id"]

            # ---------- 콜백 쿼리 처리 (인라인 버튼) ----------
            callback = update.get("callback_query")
            if callback:
                cb_id = callback["id"]
                chat_id = callback["message"]["chat"]["id"]
                data = callback.get("data", "")
                legacy_parts = data.split("|")
                legacy_action = legacy_parts[0] if legacy_parts else ""
                legacy_ex = legacy_parts[1] if len(legacy_parts) > 1 else None
                legacy_sym = legacy_parts[2] if len(legacy_parts) > 2 else None

                # FindCoin TOP1~3 상세분석 버튼은 독립 분석을 새로 시작한다.
                if legacy_action == "fc_detail":
                    fc_ex_name, fc_symbol = legacy_ex, legacy_sym
                    if fc_ex_name not in SUPPORTED_EXCHANGES or not fc_symbol:
                        answer_callback_query(cb_id, "요청 정보가 올바르지 않습니다.")
                        send_telegram_message(chat_id, "상세분석 요청 정보가 유효하지 않습니다.\n다시 코인 명령을 입력해 주세요.")
                        continue
                    answer_callback_query(cb_id, "상세분석 시작...")
                    fc_ex_display = SUPPORTED_EXCHANGES[fc_ex_name]["kr_name"]
                    fc_tfs = list(SUPPORTED_EXCHANGES[fc_ex_name]["default_tfs"])
                    send_telegram_message(
                        chat_id,
                        f"✅️ <b>CandleView</b> [{fc_ex_display}]\n{fc_symbol} {', '.join(fc_tfs)} (자동적용)\n\n"
                        f"🔎 차트 데이터 수집 중..."
                    )
                    d_phase1_result, d_symbol, d_exchange_display, d_supplement = run_phase1(fc_symbol, fc_ex_name, fc_tfs)
                    if d_symbol is None:
                        send_telegram_message(chat_id, d_phase1_result)
                        continue
                    detail_session_id = create_analysis_session(chat_id, {
                        "phase1": d_phase1_result,
                        "symbol": d_symbol,
                        "exchange": d_exchange_display,
                        "supplement": d_supplement,
                        "ex_raw": d_exchange_display.lower(),
                        "sym_raw": fc_symbol,
                        "tfs": tuple(fc_tfs),
                    })
                    send_telegram_message(
                        chat_id,
                        f"✅️ <b>CandleView</b> [{d_exchange_display}]\n{d_symbol}\n\n"
                        f"차트 상세 데이터 수집이 완료되었습니다.\n\n"
                        f"아래에서 원하는 항목을 선택하세요.",
                        reply_markup=make_phase_keyboard(detail_session_id)
                    )
                    continue

                action, session_id = parse_phase_callback(data)
                if action is None:
                    answer_callback_query(cb_id, "분석 세션을 찾을 수 없습니다.")
                    send_telegram_message(chat_id, "분석 데이터가 만료되었거나 구형 버튼입니다.\n같은 코인 명령을 다시 입력해 새 데이터를 수집해 주세요.")
                    continue

                cached = get_analysis_session(chat_id, session_id)
                if cached is None:
                    answer_callback_query(cb_id, "분석 세션이 만료되었습니다.")
                    send_telegram_message(chat_id, "분석 데이터가 만료되었거나 현재 대화의 세션이 아닙니다.\n같은 코인 명령을 다시 입력해 새 데이터를 수집해 주세요.")
                    continue

                if action in ("phase2_run", "phase2_retry"):
                    execution_started, execution_notice = begin_phase2_execution(cached, action)
                    if not execution_started:
                        answer_callback_query(cb_id, execution_notice)
                        send_telegram_message(chat_id, execution_notice)
                        continue

                action_msg = {
                    "phase1_view": "Phase1 데이터 불러오는 중...",
                    "supplement_view": "보간 지표 불러오는 중...",
                    "phase2_run": "Phase2 분석 실행 중...",
                    "phase2_retry": "동일 원천으로 Phase2 재시도 중...",
                    "fractal_view": "정식 모드 보조 지표 불러오는 중...",
                }[action]
                answer_callback_query(cb_id, action_msg)

                if action == "phase1_view":
                    phase1_text = sanitize_html(cached["phase1"])
                    model_line = format_model_provenance((cached.get("supplement") or {}).get("phase1_model"))
                    header = f"<b>CandleView — Phase1 수집 데이터</b>\n{cached['exchange']} {cached['symbol']}\n{model_line}\n\n"
                    full = header + phase1_text
                    for chunk in smart_chunk(full, PHASE1_BOUNDARY_MARKERS):
                        send_telegram_message(chat_id, chunk)

                elif action == "supplement_view":
                    # Gemini 호출 없이 main.py가 수집한 원시데이터를 그대로 표시 (즉시응답, 환각없음)
                    supp_text = format_supplement_display(cached.get("supplement"), cached["symbol"], cached["exchange"])
                    send_telegram_message(chat_id, supp_text)

                elif action in ("phase2_run", "phase2_retry"):
                    phase2_action_label = "최종 분석 진행" if action == "phase2_run" else "동일 원천 재시도"
                    send_telegram_message(
                        chat_id,
                        f"🕯️ <b>CandleView</b>\n"
                        f"{cached['exchange']} {cached['symbol']} Phase2 {phase2_action_label} 중...\n"
                        f"잠시만 기다려 주세요."
                    )
                    phase2_meta = {}
                    try:
                        raw_phase2_result = run_phase2(
                            cached["phase1"],
                            cached["symbol"],
                            cached["exchange"],
                            (cached.get("supplement") or {}).get("phase1_canonical"),
                            (cached.get("supplement") or {}).get("phase1_model"),
                        (cached.get("supplement") or {}).get("phase2_input_provenance"),
                            cached.get("session_id"),
                            1 + int(cached.get("phase2_retry_count", 0) or 0),
                            action,
                            phase2_execution_metadata=phase2_meta,
                        )
                    except Exception:
                        # 예외는 기존 상위 실패 격리로 전달하되, 세션을 in_progress에 남기지 않는다.
                        finish_phase2_execution(cached, "terminal")
                        raise
                    retry_outcome = classify_phase2_retryable_result(raw_phase2_result)
                    retry_state = finish_phase2_execution(
                        cached,
                        retry_outcome or "terminal",
                        retry_after_seconds=extract_p2m02_retry_after_seconds(raw_phase2_result),
                    )
                    phase2_result = sanitize_html(raw_phase2_result)
                    model_line = format_model_provenance(phase2_meta or (cached.get("supplement") or {}).get("phase1_model"))
                    header = f"<b>CandleView — Phase2 최종 분석</b>\n{cached['exchange']} {cached['symbol']}\n{model_line}\n\n"
                    full = header + phase2_result
                    for chunk in smart_chunk(full, PHASE2_BOUNDARY_MARKERS):
                        send_telegram_message(chat_id, chunk)
                    if retry_state == "retry_available":
                        if retry_outcome == "P2M02":
                            retry_notice = (
                                "승인 분석 모델의 할당량 대기 상태가 확인되었습니다.\n"
                                "PHASE 1 원천·모델 호출은 다시 실행하지 않습니다. 안내된 대기 시간 뒤 같은 세션·같은 원천으로 PHASE 2만 한 번 재시도할 수 있습니다."
                            )
                        else:
                            retry_notice = (
                                "승인 분석 모델의 일시적 연결 실패가 확인되었습니다.\n"
                                "PHASE 1 원천·모델 호출은 다시 실행하지 않고, 같은 세션·같은 원천으로 PHASE 2만 한 번 재시도할 수 있습니다."
                            )
                        send_telegram_message(
                            chat_id,
                            retry_notice,
                            reply_markup=make_phase2_retry_keyboard(cached["session_id"]),
                        )

                elif action == "fractal_view":
                    send_telegram_message(
                        chat_id,
                        f"🕯️ <b>CandleView</b>\n"
                        f"{cached['exchange']} {cached['symbol']} 정식 모드 보조 지표 불러오는 중...\n"
                        f"잠시만 기다려 주세요."
                    )
                    phase1_model = (cached.get("supplement") or {}).get("phase1_model") or {}
                    if phase1_model.get("failed"):
                        fractal_result = "[검증보류 — PHASE 1 AI 해석 미완료]\n정식모드는 완료된 PHASE 1 해석을 기반으로 하므로 다시 분석을 실행해 주세요."
                    else:
                        fractal_result = sanitize_html(run_fractal_supplement(
                            cached["phase1"],
                            cached["symbol"],
                            cached["exchange"]
                        ))
                    header = f"<b>CandleView — 정식 모드 보조 지표</b>\n{cached['exchange']} {cached['symbol']}\n\n"
                    full = header + fractal_result
                    for chunk in smart_chunk(full, PHASE2_BOUNDARY_MARKERS):
                        send_telegram_message(chat_id, chunk)

                continue

            # ---------- 일반 메시지 처리 ----------
            msg = update.get("message", {})
            chat_id = msg.get("chat", {}).get("id")
            raw_text = msg.get("text", "").strip()

            if not chat_id:
                continue

            if not raw_text.startswith("/"):
                send_telegram_message(chat_id, UNAUTHORIZED_INPUT_GUIDE)
                continue

            clean_text = raw_text[1:].replace(":", " ").replace(",", " ")
            parts = clean_text.split()
            if not parts:
                continue

            cmd = parts[0].lower()

            if cmd == "backtest" and chat_id == 517008099:
                bt_start = parts[1] if len(parts) > 1 else None
                bt_end = parts[2] if len(parts) > 2 else None
                threading.Thread(
                    target=backtest_framework.execute_and_report,
                    args=(chat_id, bt_start, bt_end), daemon=True
                ).start()
                continue

            if cmd == "start":
                send_telegram_message(
                    chat_id,
                    (
                        "🕯️ <b>CandleView</b> — PA/VSA 정밀 차트 분석 봇\n\n"
                        "[지정코인 분석 예시]\n"
                        "• /업비트 비트코인\n"
                        "• /빗썸 리플\n"
                        "• /coinbase eth\n"
                        "• /업비트 비트코인 1d 4h 1h  (TF 직접 지정)\n\n"
                        "[FindCoin — Top3 스크리닝]\n"
                        "• /업비트   또는   /coinbase\n\n"
                        "지원 거래소: 업비트 · 빗썸 · 코인베이스"
                    ),
                )
                continue

            # 명령 파싱 — 거래소명이 필수 1순위 인자
            ex_name = resolve_exchange(parts[0])
            if ex_name is None:
                send_telegram_message(chat_id, UNAUTHORIZED_INPUT_GUIDE)
                continue

            if len(parts) == 1:
                # 코인명 없이 거래소명만 → FindCoin 실행
                ex_display_fc = SUPPORTED_EXCHANGES[ex_name]["kr_name"]
                quote = SUPPORTED_EXCHANGES[ex_name]["quote"]
                send_telegram_message(
                    chat_id,
                    f"🚨 시세분출 가능성이 높은 코인을 분석해서 Top3 를 알려드리는 🔎 FindCoin 이 실행되었습니다.\n"
                    f"잠시만 기다려 주세요.\n\n"
                    f"🔎 {ex_display_fc} 정보수집중...\n\n"
                    f"거래소 응답속도에 따라 1 ~3분 정도 소요될수 있습니다."
                )

                result_text, n_total, n_valid, n_gate1, n_gate2, err, top_symbols, n_watch = run_findcoin(ex_name)

                if err:
                    send_telegram_message(chat_id, err)
                    continue

                watch_note = f"\n\n💡 참고내용\n데이터가 부족해 판정을 보류한 신규상장 코인 {n_watch}개 있음(배제 아님)" if n_watch > 0 else ""

                if n_gate2 == 0:
                    watch_line = f"\n💡 참고내용\n데이터가 부족해 판정을 보류한 신규상장 코인 {n_watch}개 있음(배제 아님)\n" if n_watch > 0 else ""
                    scan_time_kst_watch = (datetime.now(timezone.utc) + timedelta(hours=9)).strftime("%Y-%m-%d %H:%M")
                    send_telegram_message(
                        chat_id,
                        f"🚨 FindCoin 코인 스캔 결과를 출력합니다.\n\n"
                        f"🔎 스캔 결과 최종 합격코인 : {n_gate2} 개\n\n"
                        f"대상 : {ex_display_fc} {quote} 마켓\n"
                        f"스캔 시각: {scan_time_kst_watch} (KST)\n"
                        f"총 스캔 종목: {n_total}개 (유효 {n_valid}개 / 관측대기 {n_watch}개)\n\n"
                        f"🧭 스캔 단계별 현황\n"
                        f"➔ 1차 경로 통과 {n_gate1}개\n"
                        f"➔ 상세 판정 후보 {n_gate2}개\n"
                        f"➔ 최종 합격 0개\n\n"
                        f"✅️ 시장 상태 : 관망 국면\n\n"
                        f"[관망 권고] 현재 {ex_display_fc} {quote} 마켓 내 경로A(Percentile ≥ 85% AND "
                        f"RTM ≥ 3.0 AND Liquidity_Ratio ≥ 1.0) 및 경로B(변동폭 ≤ 5.0% AND "
                        f"Liquidity_Ratio ≥ 1.0) 중 어느 쪽도 통과하지 못했거나, PA-VSA 옥석 검증·"
                        f"손익비(R:R ≥ 2.0) 조건을 동시에 충족하는 고신뢰 분출 후보가 0개입니다. "
                        f"억지 추격 진입을 지양하고 관망을 권고합니다.\n\n"
                        f"[FindCoin 무결성 검증 완료]\n"
                        f"■ API Direct Stream\n"
                        f"■ Layer 7 감사 100% 통과\n"
                        f"{watch_line}"
                    )
                    continue

                fc_text = sanitize_html(result_text) + sanitize_html(watch_note)
                header = f"<b>CandleView — FindCoin 스캔 결과</b>\n{ex_display_fc}\n\n"
                full = header + fc_text
                chunks = smart_chunk(full, FINDCOIN_BOUNDARY_MARKERS)
                for i, chunk in enumerate(chunks):
                    is_last = (i == len(chunks) - 1)
                    reply_markup = make_findcoin_detail_keyboard(ex_name, top_symbols) if (is_last and top_symbols) else None
                    send_telegram_message(chat_id, chunk, reply_markup=reply_markup)
                continue

            sym_name = parts[1]
            # TF 미지정 시 거래소 유형별 고정값 자동 적용, 수동 입력은 명세 표준으로 정규화한다.
            if len(parts) > 2:
                tfs, tf_error = normalize_timeframes(parts[2:])
                if tf_error:
                    send_telegram_message(chat_id, tf_error)
                    continue
                tf_note = "(직접 지정·표준화)"
            else:
                tfs = list(SUPPORTED_EXCHANGES[ex_name]["default_tfs"])
                tf_note = "(자동 적용)"

            sym_clean = sym_name.replace("/", "").replace(" ", "").strip()
            sym_mapped = resolve_korean_symbol(sym_clean, ex_name)
            quote = SUPPORTED_EXCHANGES[ex_name]["quote"]
            ex_display = SUPPORTED_EXCHANGES[ex_name]["kr_name"]

            # 상태 메시지
            status_msg = (
                f"✅️ <b>CandleView</b> [{ex_display}]\n"
                f"{sym_mapped}/{quote} {', '.join(tfs)} {tf_note}\n\n"
                f"🔎 차트 데이터 수집 중..."
            )
            send_telegram_message(chat_id, status_msg)

            # PHASE 1 실행
            phase1_result, symbol, exchange_display, supplement = run_phase1(sym_clean, ex_name, tfs)

            if symbol is None:
                send_telegram_message(chat_id, phase1_result)
                continue

            # 검증된 PHASE 1 상태를 독립 세션으로 보존한다. 새 분석이 과거 버튼의 상태를 덮어쓰지 않는다.
            analysis_session_id = create_analysis_session(chat_id, {
                "phase1": phase1_result,
                "symbol": symbol,
                "exchange": exchange_display,
                "supplement": supplement,
                "ex_raw": exchange_display.lower(),
                "sym_raw": sym_clean,
                "tfs": tuple(tfs),
            })

            # 안내 메시지 + 인라인 버튼
            model_line = format_model_provenance((supplement or {}).get("phase1_model"))
            guide_msg = (
                f"✅️ <b>CandleView</b> [{exchange_display}]\n"
                f"{symbol}\n{model_line}\n\n"
                f"차트 상세 데이터 수집이 완료되었습니다.\n\n"
                f"아래에서 원하는 항목을 선택하세요."
            )
            send_telegram_message(
                chat_id, guide_msg,
                reply_markup=make_phase_keyboard(analysis_session_id)
            )

    except Exception as e:
        print(f"[ERROR] 메인 루프 예외: {e}")
        time.sleep(2)
