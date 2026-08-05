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
# 환경변수 로드 (하드코딩 금지)
# ============================================================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

if not TELEGRAM_BOT_TOKEN or not GEMINI_API_KEY:
    print("⚠️ 경고: TELEGRAM_BOT_TOKEN 또는 GEMINI_API_KEY 환경변수가 비어 있습니다. Render 대시보드 Environment에서 등록하세요.")

# ============================================================
# 엔진 파일 자동 탐색 및 로드
# ============================================================
CANDLEVIEW_PROMPT_FULL = ""
for f in os.listdir("."):
    if ("001" in f or "Candle" in f or "Api" in f) and f.endswith(".txt"):
        try:
            with open(f, "r", encoding="utf-8") as file:
                CANDLEVIEW_PROMPT_FULL = file.read()
            print(f"[INFO] 엔진 파일({f}) 로드 성공! 크기: {len(CANDLEVIEW_PROMPT_FULL):,}바이트")
            break
        except Exception:
            pass

if not CANDLEVIEW_PROMPT_FULL:
    CANDLEVIEW_PROMPT_FULL = "CandleView_API_V001 정밀 연산 엔진"
    print("[WARN] 엔진 파일을 찾지 못해 기본 문자열로 대체합니다.")

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
# RMA-RSI 14 (Wilder's Smoothing)
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
# Gemini API 호출 (재시도 + 모델 폴백)
# ============================================================
def call_gemini_api_with_retry(full_prompt):
    headers = {
        "Content-Type": "application/json",
        "X-goog-api-key": GEMINI_API_KEY,
    }
    payload = {"contents": [{"parts": [{"text": full_prompt}]}]}

    # 2026년 기준 생존 가능성이 높은 정식/최신 모델 우선순위
    urls = [
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash:generateContent",
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash:generateContent",
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent",
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent",
    ]

    for url in urls:
        for _ in range(2):
            try:
                res = requests.post(url, headers=headers, json=payload, timeout=90)
                if res.status_code == 200:
                    return res.json()["candidates"][0]["content"]["parts"][0]["text"]
                elif res.status_code == 503:
                    time.sleep(3)
                else:
                    print(f"[WARN] Gemini {url} 응답 코드: {res.status_code}")
                    time.sleep(1)
            except Exception as e:
                print(f"[WARN] Gemini 호출 예외: {e}")
                time.sleep(2)
    return "AI 서버 일시적 과부하 또는 모델 접근 불가 상태입니다. 잠시 후 다시 시도해 주세요."

# ============================================================
# 핵심 분석 함수
# ============================================================
def analyze_crypto_dynamic(symbol_input="BTC", exchange_name="bybit", custom_tfs=["1d", "4h", "1h"]):
    try:
        ex_name = exchange_name.lower()
        if ex_name in ["업비트", "upbit"]:
            ex_name = "upbit"
            quote = "KRW"
        elif ex_name in ["빗썸", "bithumb"]:
            ex_name = "bithumb"
            quote = "KRW"
        else:
            ex_name = "bybit"
            quote = "USDT"

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

        # 120봉 수집 → 최근 100봉 전송 (RSI 워밍업 + 프롬프트 요구사항 충족)
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

        # PHASE 연속 출력 강제 (봇 원샷 분석용)
        prompt = (
            f"{CANDLEVIEW_PROMPT_FULL}\n\n"
            f"[API 수신 원천 데이터]\n{payload}\n\n"
            f"PHASE 1 표 작성 후 PHASE 2 완제 브리핑까지 연속 완제 출력하십시오."
        )

        return call_gemini_api_with_retry(prompt)

    except Exception as e:
        return f"요청 처리 중 오류 발생: {e}"

# ============================================================
# 텔레그램 메인 루프
# ============================================================
print("🚀 CandleView AI 봇 가동 시작 (Gemini 3.x Flash 우선)")

# 409 Conflict 예방: 기존 webhook 강제 삭제
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
            msg = update.get("message", {})
            chat_id = msg.get("chat", {}).get("id")
            raw_text = msg.get("text", "").strip()

            if not raw_text.startswith("/"):
                continue

            clean_text = raw_text[1:].replace(":", " ").replace(",", " ")
            parts = clean_text.split()
            if not parts:
                continue

            cmd = parts[0].lower()

            # /start 안내
            if cmd == "start":
                send_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
                requests.post(
                    send_url,
                    json={
                        "chat_id": chat_id,
                        "text": (
                            "365일 무중단 CandleView AI 분석 봇입니다.\n\n"
                            "[사용 예시]\n"
                            "• /btc\n"
                            "• /업비트 리플\n"
                            "• /빗썸 도지코인\n"
                            "• /btc 1d 4h 1h\n"
                            "• /upbit sol 4h 1h 15m"
                        ),
                    },
                    timeout=10,
                )
                continue

            # 명령 파싱
            ex_name = "bybit"
            sym_name = cmd
            tfs = ["1d", "4h", "1h"]

            if len(parts) > 1:
                first_p = parts[0].lower()
                if first_p in ["upbit", "bybit", "bithumb", "okx", "업비트", "빗썸"]:
                    ex_name = first_p
                    sym_name = parts[1]
                    if len(parts) > 2:
                        tfs = parts[2:]
                else:
                    tfs = parts[1:]

            sym_clean = sym_name.replace("/", "").replace(" ", "").strip()
            sym_mapped = UPBIT_KOREAN_MAP.get(sym_clean, sym_clean.upper())
            quote = "KRW" if ex_name in ["upbit", "bithumb", "업비트", "빗썸"] else "USDT"

            # 진행 상태 메시지
            status_msg = f"⏳ [{ex_name.upper()}] {sym_mapped}/{quote} ({', '.join(tfs)}) API 수집 및 정밀 연산 중..."
            send_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            requests.post(send_url, json={"chat_id": chat_id, "text": status_msg}, timeout=10)

            # 분석 실행
            result_text = analyze_crypto_dynamic(sym_clean, ex_name, tfs)

            # 4000자 단위 분할 전송
            for i in range(0, len(result_text), 4000):
                requests.post(
                    send_url,
                    json={"chat_id": chat_id, "text": result_text[i : i + 4000]},
                    timeout=15,
                )

    except Exception as e:
        print(f"[ERROR] 메인 루프 예외: {e}")
        time.sleep(2)
