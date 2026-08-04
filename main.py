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


# Render 포트 바인딩
def run_port_server():
    try:
        port = int(os.environ.get("PORT", 10000))
        server = HTTPServer(("0.0.0.0", port), SimpleHTTPRequestHandler)
        print(f"포트 {port} 오픈 완료. Render 수신 대기 중...")
        server.serve_forever()
    except Exception as e:
        print(f"포트 서버 스레드 오류: {e}")


threading.Thread(target=run_port_server, daemon=True).start()

# 사용자 설정
TELEGRAM_BOT_TOKEN = "8897306377:AAEZBAvMCLdUajN497MI65r593tZo1wHZCc"
GEMINI_API_KEY = "AQ.Ab8RN6K4Yfao6wc_F1K8jgNdu0hZu-v4vutnFN78L3f52ukhHw"

# 78.15KB 원본 파일 자동 탐색 및 로드
CANDLEVIEW_PROMPT_FULL = ""
for f in os.listdir("."):
    if ("001" in f or "Candle" in f or "Api" in f) and f.endswith(".txt"):
        try:
            with open(f, "r", encoding="utf-8") as file:
                CANDLEVIEW_PROMPT_FULL = file.read()
            print(f"🚀 엔진 파일({f}) 로드 성공!")
            break
        except Exception:
            pass

if not CANDLEVIEW_PROMPT_FULL:
    CANDLEVIEW_PROMPT_FULL = "CandleView_API_V001 정밀 연산 엔진"


# 업비트 전체 한글 코인 오토 매핑
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


def calculate_rma_rsi(prices, period=14):
    delta = prices.diff()
    gain = (delta.where(delta > 0, 0)).fillna(0)
    loss = (-delta.where(delta < 0, 0)).fillna(0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


# 💡 1순위: Gemini 1.5 Pro 최상위 고성능 모델 적용
def call_gemini_api_with_retry(full_prompt):
    headers = {
        "Content-Type": "application/json",
        "X-goog-api-key": GEMINI_API_KEY,
    }
    payload = {"contents": [{"parts": [{"text": full_prompt}]}]}

    # 1.5-pro 모델 1순위 배치
    urls = [
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-pro:generateContent",
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent",
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent",
    ]

    for url in urls:
        for _ in range(2):
            try:
                res = requests.post(url, headers=headers, json=payload, timeout=60)
                if res.status_code == 200:
                    return res.json()["candidates"][0]["content"]["parts"][0][
                        "text"
                    ]
                elif res.status_code == 503:
                    time.sleep(3)
            except Exception:
                time.sleep(2)
    return "AI 서버 일시적 과부하(503) 상태입니다. 잠시 후 다시 시도해 주세요."


def analyze_crypto_dynamic(
    symbol_input="BTC", exchange_name="bybit", custom_tfs=["1d", "4h", "1h"]
):
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

        payload = f"[STAGE 0 사전 환경 점검]\n• 수집 거래소: {ex_name.upper()}\n• 수집 방식: ■ API Direct Data Stream\n\n=== 코인명: {symbol} ===\n"

        for tf in custom_tfs:
            ohlcv = exchange_class.fetch_ohlcv(symbol, timeframe=tf, limit=30)
            df = pd.DataFrame(
                ohlcv,
                columns=[
                    "timestamp",
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                ],
            )
            df["rsi"] = calculate_rma_rsi(df["close"])

            recent = df.tail(10)
            payload += f"\n[{tf} 타임프레임 API 수신 배열]\n"
            for _, row in recent.iterrows():
                payload += f"O: {row['open']} | H: {row['high']} | L: {row['low']} | C: {row['close']} | V: {row['volume']:.2f} | RSI: {row['rsi']:.2f}\n"

        prompt = f"{CANDLEVIEW_PROMPT_FULL}\n\n[API 수신 원천 데이터]\n{payload}\n\nPHASE 1 표 작성 후 PHASE 2 완제 브리핑까지 연속 완제 출력하십시오."
        return call_gemini_api_with_retry(prompt)
    except Exception as e:
        return f"요청 처리 중 오류 발생: {e}"


# 텔레그램 메인 루프 (프리징 차단 timeout=35 적용)
print("🚀 Gemini 1.5 Pro 적용 및 프리징 방지 365일 봇 가동 시작!")
last_update_id = 0

while True:
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates?offset={last_update_id + 1}&timeout=30"
        # 💡 timeout=35 속성을 통해 무한 대기 프리징 현상 원천 차단
        updates = requests.get(url, timeout=35).json()

        for update in updates.get("result", []):
            last_update_id = update["update_id"]
            msg = update.get("message", {})
            chat_id = msg.get("chat", {}).get("id")
            raw_text = msg.get("text", "").strip()

            if raw_text.startswith("/"):
                clean_text = raw_text[1:].replace(":", " ").replace(",", " ")
                parts = clean_text.split()
                if not parts:
                    continue

                cmd = parts[0].lower()
                if cmd == "start":
                    send_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
                    requests.post(
                        send_url,
                        json={
                            "chat_id": chat_id,
                            "text": (
                                "👋 365일 무중단 CandleView AI 분석"
                                " 봇입니다.\n\n[사용 예시]\n• /btc : 바이비트"
                                " 비트코인 분석\n• /업비트 리플 : 업비트 리플"
                                " 분석\n• /빗썸 도지코인 : 빗썸 도지코인 분석\n"
                            ),
                        },
                        timeout=10,
                    )
                    continue

                ex_name = "bybit"
                sym_name = cmd
                tfs = ["1d", "4h", "1h"]

                if len(parts) > 1:
                    first_p = parts[0].lower()
                    if first_p in [
                        "upbit",
                        "bybit",
                        "bithumb",
                        "okx",
                        "업비트",
                        "빗썸",
                    ]:
                        ex_name = first_p
                        sym_name = parts[1]
                        if len(parts) > 2:
                            tfs = parts[2:]
                    else:
                        tfs = parts[1:]

                sym_clean = sym_name.replace("/", "").replace(" ", "").strip()
                sym_mapped = UPBIT_KOREAN_MAP.get(sym_clean, sym_clean.upper())
                quote = (
                    "KRW"
                    if ex_name in ["upbit", "bithumb", "업비트", "빗썸"]
                    else "USDT"
                )

                status_msg = f"⏳ [{ex_name.upper()}] {sym_mapped}/{quote} ({', '.join(tfs)}) API 수집 및 Gemini 1.5 Pro 정밀 연산 중..."
                send_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
                requests.post(
                    send_url,
                    json={"chat_id": chat_id, "text": status_msg},
                    timeout=10,
                )

                result_text = analyze_crypto_dynamic(sym_clean, ex_name, tfs)

                for i in range(0, len(result_text), 4000):
                    requests.post(
                        send_url,
                        json={"chat_id": chat_id, "text": result_text[i : i + 4000]},
                        timeout=10,
                    )

    except Exception as e:
        time.sleep(2)
