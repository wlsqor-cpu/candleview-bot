from http.server import HTTPServer, SimpleHTTPRequestHandler
import json
import os
import re
import threading
import time
import ccxt
import numpy as np
import pandas as pd
import requests
from datetime import datetime, timedelta

# ============================================================
# 거래소 범용 인식 (ccxt 지원 전체 거래소 자동인식 + 한글명 매핑)
# 화이트리스트 방식 대신 ccxt.exchanges(103개) 전체를 인식 대상으로 삼는다.
# 한글명은 ccxt가 인식하지 못하므로 별도 매핑만 유지한다(필요시 계속 추가 가능).
# ============================================================
EXCHANGE_KR_MAP = {
    "업비트": "upbit", "빗썸": "bithumb", "바이낸스": "binance",
    "바이비트": "bybit", "오케이엑스": "okx", "코인베이스": "coinbase",
    "크라켄": "kraken", "코인원": "coinone", "후오비": "htx",
    "게이트": "gate", "게이트아이오": "gateio", "크립토닷컴": "cryptocom",
    "비트겟": "bitget", "쿠코인": "kucoin",
}

# 원화(KRW) 마켓을 지원하는 거래소 — 그 외는 전부 USDT 기본 적용
KRW_EXCHANGES = {"upbit", "bithumb", "coinone"}

# ============================================================
# 비인가 입력(슬래시 명령이 아닌 임의 입력) 안내 문구
# ============================================================
UNAUTHORIZED_INPUT_GUIDE = (
    "본 시스템은 분석입력, 출력 외 다른 기능은 제공되지 않습니다.\n\n"
    "<b>분석명령어</b>\n"
    "/거래소 + 코인명 + 그리고 분석을 원하는 차트시간대를 입력해주세요.\n\n"
    "예시) /바이낸스 비트코인 1W, 1D, 4H, 1H\n"
    "(시간대는 최소 2개 ~ 최대 4개를 선택)"
)


def resolve_exchange(name: str):
    """영문(ccxt id) 또는 한글명을 ccxt 거래소 id로 변환. 인식 실패 시 None 반환."""
    if not name:
        return None
    raw = name.strip()
    if raw in EXCHANGE_KR_MAP:
        return EXCHANGE_KR_MAP[raw]
    key = raw.lower()
    if key in ccxt.exchanges:
        return key
    return None

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
# 엔진 파일 로드 (CandleView 최신 버전 자동 인식 설계)
# 파일명에 버전(V002-2 등)이 붙어 있더라도 'CandleView'로 시작하는 
# 텍스트 파일 중 가장 최신 파일을 자동으로 찾아 로드합니다.
# 이제 파일명을 바꾸거나 main.py를 수정할 필요 없이 새 버전 파일만 업로드하면 됩니다.
# ============================================================
def get_latest_candleview_file():
    # 'CandleView'로 시작하고 '.txt'로 끝나는 모든 파일 검색
    files = [f for f in os.listdir(".") if f.startswith("CandleView") and f.endswith(".txt")]
    if not files:
        return None
    # 알파벳 역순 정렬을 통해 가장 높은 버전(최신 파일) 선택
    files.sort(reverse=True)
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

# ============================================================
# 분석 결과 임시 저장소 (chat_id 기준)
# ============================================================
analysis_cache = {}
CACHE_TTL_MINUTES = 30


def clean_expired_cache():
    now = datetime.now()
    expired = [cid for cid, data in analysis_cache.items()
               if now - data["created_at"] > timedelta(minutes=CACHE_TTL_MINUTES)]
    for cid in expired:
        del analysis_cache[cid]


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


UPBIT_KOREAN_MAP = fetch_upbit_korean_map()
print(f"[INFO] 업비트 한글 맵 로드: {len(UPBIT_KOREAN_MAP)}개 코인")


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
    return 100 - (100 / (1 + rs))


# ============================================================
# Gemini API 호출
# ============================================================
def call_gemini_api_with_retry(full_prompt, max_tokens=16384):
    headers = {
        "Content-Type": "application/json",
        "X-goog-api-key": GEMINI_API_KEY,
    }
    payload = {
        "contents": [{"parts": [{"text": full_prompt}]}],
        "generationConfig": {"maxOutputTokens": max_tokens},
    }

    urls = [
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash:generateContent",
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent",
    ]

    for url in urls:
        for _ in range(2):
            try:
                res = requests.post(url, headers=headers, json=payload, timeout=120)
                if res.status_code == 200:
                    return res.json()["candidates"][0]["content"]["parts"][0]["text"]
                elif res.status_code == 503:
                    time.sleep(3)
                else:
                    print(f"[WARN] Gemini 응답 코드: {res.status_code}")
                    time.sleep(1)
            except Exception as e:
                print(f"[WARN] Gemini 호출 예외: {e}")
                time.sleep(2)
    return "AI 서버 일시적 과부하 또는 모델 접근 불가 상태입니다. 잠시 후 다시 시도해 주세요."


# ============================================================
# 텔레그램 메시지 전송 (일반 + 인라인 버튼 지원)
# ============================================================
def send_telegram_message(chat_id, text, reply_markup=None, timeout=15):
    send_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup

    res = requests.post(send_url, json=payload, timeout=timeout)
    try:
        ok = res.json().get("ok", False)
    except Exception:
        ok = res.status_code == 200

    if not ok:
        payload.pop("parse_mode", None)
        if reply_markup:
            payload["reply_markup"] = reply_markup
        requests.post(send_url, json=payload, timeout=timeout)


def answer_callback_query(callback_query_id, text=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery"
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    requests.post(url, json=payload, timeout=10)


# ============================================================
# PHASE 1 전용 실행 (데이터 수집 + 표 작성만)
# ============================================================
def run_phase1(symbol_input, exchange_name, custom_tfs):
    try:
        resolved = resolve_exchange(exchange_name)
        ex_name = resolved if resolved else "bybit"
        quote = "KRW" if ex_name in KRW_EXCHANGES else "USDT"

        clean = symbol_input.replace("/", "").replace(":", "").replace(" ", "")
        symbol_upper = UPBIT_KOREAN_MAP.get(clean, clean.upper())
        exchange_class = getattr(ccxt, ex_name)()
        symbol = f"{symbol_upper}/{quote}"

        payload = (
            f"[STAGE 0 사전 환경 점검]\n"
            f"• 수집 거래소: {ex_name.upper()}\n"
            f"• 수집 방식: ■ API Direct Data Stream\n\n"
            f"=== 코인명: {symbol} ===\n"
        )

        for tf in custom_tfs:
            ohlcv = exchange_class.fetch_ohlcv(symbol, timeframe=tf, limit=120)
            df = pd.DataFrame(
                ohlcv,
                columns=["timestamp", "open", "high", "low", "close", "volume"],
            )
            df["rsi"] = calculate_rma_rsi(df["close"])
            recent = df.tail(100)

            payload += f"\n[{tf} 타임프레임 API 수신 배열 (최근 {len(recent)}봉)]\n"
            for _, row in recent.iterrows():
                payload += (
                    f"O: {row['open']} | H: {row['high']} | L: {row['low']} | "
                    f"C: {row['close']} | V: {row['volume']:.2f} | RSI: {row['rsi']:.2f}\n"
                )

        phase1_prompt = (
            f"{CANDLEVIEW_PROMPT_FULL}\n\n"
            f"[API 수신 원천 데이터]\n{payload}\n\n"
            f"지금은 PHASE 1만 수행하십시오.\n"
            f"PHASE 1 표 작성을 완료한 뒤, 엔진에 규정된 PHASE 1 최종 종료 고정 문구를 출력하고 멈추십시오.\n"
            f"PHASE 2 관련 서술·해석·전략은 절대 출력하지 마십시오."
        )

        phase1_result = call_gemini_api_with_retry(phase1_prompt, max_tokens=8192)
        return phase1_result, symbol, ex_name.upper()

    except Exception as e:
        return f"PHASE 1 실행 중 오류: {e}", None, None


# ============================================================
# PHASE 2 전용 실행 (이미 완성된 PHASE 1을 재료로 사용)
# ============================================================
def run_phase2(phase1_result, symbol, exchange_name):
    phase2_prompt = (
        f"{CANDLEVIEW_PROMPT_FULL}\n\n"
        f"아래는 이미 완성된 PHASE 1 결과입니다.\n"
        f"사용자는 PHASE 2 진행을 명시적으로 승인하였습니다.\n\n"
        f"[PHASE 1 완성 결과]\n{phase1_result}\n\n"
        f"이제 PHASE 2 통합 브리핑을 엔진 규칙에 따라 완제 출력하십시오.\n"
        f"새로운 수치나 판단을 임의로 추가하지 말고, PHASE 1에서 도출된 데이터만을 근거로 사용하십시오."
    )
    return call_gemini_api_with_retry(phase2_prompt, max_tokens=8192)


# ============================================================
# 인라인 키보드 생성
# callback_data에 거래소/심볼/TF를 함께 실어 보내, 캐시가 사라져도
# 버튼만으로 동일 조건 재분석이 가능하도록 한다 (64byte 제한 내 안전 설계).
# ============================================================
def make_phase_keyboard(ex_name, symbol_raw, tfs):
    tfs_str = ",".join(tfs)
    payload_tail = f"{ex_name}|{symbol_raw}|{tfs_str}"
    # Telegram callback_data 최대 64byte 안전장치: 초과 시 TF를 2개로 축약
    if len(f"phase2_run|{payload_tail}".encode("utf-8")) > 64:
        tfs_str = ",".join(tfs[:2])
        payload_tail = f"{ex_name}|{symbol_raw}|{tfs_str}"

    return {
        "inline_keyboard": [
            [
                {"text": "📊 Phase1 수집 데이터 열람", "callback_data": f"phase1_view|{payload_tail}"},
                {"text": "📜 Phase2 최종 분석내용 보기", "callback_data": f"phase2_run|{payload_tail}"},
            ]
        ]
    }


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
                cb_parts = data.split("|")
                action = cb_parts[0] if cb_parts else ""
                cb_ex = cb_parts[1] if len(cb_parts) > 1 else None
                cb_sym = cb_parts[2] if len(cb_parts) > 2 else None
                cb_tfs = cb_parts[3].split(",") if len(cb_parts) > 3 and cb_parts[3] else None

                cached = analysis_cache.get(chat_id)
                cache_matches = bool(
                    cached
                    and cached.get("ex_raw") == cb_ex
                    and cached.get("sym_raw") == cb_sym
                )

                if action not in ("phase1_view", "phase2_run"):
                    answer_callback_query(cb_id)
                    send_telegram_message(chat_id, "알 수 없는 요청입니다.\n다시 코인 명령을 입력해 주세요.")
                    continue

                if not cache_matches and (not cb_ex or not cb_sym or not cb_tfs):
                    # 구버전 콜백(정보 없음) 등 재계산 불가 케이스만 안내 후 종료
                    answer_callback_query(cb_id, "재분석 정보가 없습니다.")
                    send_telegram_message(chat_id, "분석 데이터가 만료되었습니다.\n다시 코인 명령을 입력해 주세요.")
                    continue

                if not cache_matches:
                    # 캐시 만료/불일치 시 버튼에 실려온 정보로 동일 조건 자동 재계산 (오류 대신 자동복구)
                    answer_callback_query(cb_id, "데이터 재계산 중...")
                    send_telegram_message(chat_id, "🕯️ <b>CandleView</b>\n이전 데이터가 만료되어 동일 조건으로 재계산합니다...")
                    phase1_result, symbol, exchange_display = run_phase1(cb_sym, cb_ex, cb_tfs)
                    if symbol is None:
                        send_telegram_message(chat_id, phase1_result)
                        continue
                    analysis_cache[chat_id] = {
                        "phase1": phase1_result,
                        "symbol": symbol,
                        "exchange": exchange_display,
                        "ex_raw": cb_ex,
                        "sym_raw": cb_sym,
                        "tfs": cb_tfs,
                        "created_at": datetime.now(),
                    }
                    cached = analysis_cache[chat_id]
                else:
                    answer_callback_query(
                        cb_id,
                        "Phase1 데이터 불러오는 중..." if action == "phase1_view" else "Phase2 분석 실행 중..."
                    )

                if action == "phase1_view":
                    phase1_text = cached["phase1"]
                    header = f"<b>CandleView — Phase1 수집 데이터</b>\n{cached['exchange']} {cached['symbol']}\n\n"
                    full = header + phase1_text
                    for i in range(0, len(full), 4000):
                        send_telegram_message(chat_id, full[i:i+4000])

                elif action == "phase2_run":
                    send_telegram_message(
                        chat_id,
                        f"🕯️ <b>CandleView</b>\n"
                        f"{cached['exchange']} {cached['symbol']} Phase2 최종 분석 진행 중...\n"
                        f"잠시만 기다려 주세요."
                    )
                    phase2_result = run_phase2(
                        cached["phase1"],
                        cached["symbol"],
                        cached["exchange"]
                    )
                    header = f"<b>CandleView — Phase2 최종 분석</b>\n{cached['exchange']} {cached['symbol']}\n\n"
                    full = header + phase2_result
                    for i in range(0, len(full), 4000):
                        send_telegram_message(chat_id, full[i:i+4000])

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

            if cmd == "start":
                send_telegram_message(
                    chat_id,
                    (
                        "🕯️ <b>CandleView</b> — PA/VSA 정밀 차트 분석 봇\n\n"
                        "[사용 예시]\n"
                        "• /btc\n"
                        "• /업비트 리플\n"
                        "• /빗썸 도지코인\n"
                        "• /btc 1d 4h 1h\n"
                        "• /upbit sol 4h 1h 15m"
                    ),
                )
                continue

            # 명령 파싱
            ex_name = "bybit"
            sym_name = cmd
            tfs = ["1d", "4h", "1h"]

            if len(parts) > 1:
                first_p = parts[0]
                if resolve_exchange(first_p) is not None:
                    ex_name = first_p
                    sym_name = parts[1]
                    if len(parts) > 2:
                        tfs = parts[2:]
                else:
                    tfs = parts[1:]

            sym_clean = sym_name.replace("/", "").replace(" ", "").strip()
            sym_mapped = UPBIT_KOREAN_MAP.get(sym_clean, sym_clean.upper())
            quote = "KRW" if (resolve_exchange(ex_name) or "bybit") in KRW_EXCHANGES else "USDT"

            # 상태 메시지
            status_msg = (
                f"🕯️ <b>CandleView</b>\n"
                f"[{ex_name.upper()}] {sym_mapped}/{quote} ({', '.join(tfs)})\n"
                f"Phase1 차트 데이터 수집 중..."
            )
            send_telegram_message(chat_id, status_msg)

            # PHASE 1 실행
            phase1_result, symbol, exchange_display = run_phase1(sym_clean, ex_name, tfs)

            if symbol is None:
                send_telegram_message(chat_id, phase1_result)
                continue

            # 캐시에 저장 (ex_raw/sym_raw는 콜백 재계산 시 run_phase1에 그대로 재사용되는 원본 파라미터)
            analysis_cache[chat_id] = {
                "phase1": phase1_result,
                "symbol": symbol,
                "exchange": exchange_display,
                "ex_raw": exchange_display.lower(),
                "sym_raw": sym_clean,
                "tfs": tfs,
                "created_at": datetime.now(),
            }

            # 안내 메시지 + 인라인 버튼
            guide_msg = (
                f"🕯️ <b>CandleView</b>\n"
                f"[{exchange_display}] {symbol}\n\n"
                f"Phase1 차트 상세 데이터 수집이 완료되었습니다.\n\n"
                f"아래에서 원하는 항목을 선택하세요."
            )
            send_telegram_message(
                chat_id, guide_msg,
                reply_markup=make_phase_keyboard(exchange_display.lower(), sym_clean, tfs)
            )

    except Exception as e:
        print(f"[ERROR] 메인 루프 예외: {e}")
        time.sleep(2)
