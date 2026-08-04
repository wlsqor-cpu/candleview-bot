import json
import os
import re
import time
import ccxt
import numpy as np
import pandas as pd
import requests

# ==========================================
# 1. 사용자 토큰 및 API 키 설정
# ==========================================
TELEGRAM_BOT_TOKEN = "8897306377:AAEZBAvMCLdUajN497MI65r593tZo1wHZCc"
GEMINI_API_KEY = "AQ.Ab8RN6K4Yfao6wc_F1K8jgNdu0hZu-v4vutnFN78L3f52ukhHw"

# ==========================================
# 2. 78.15KB 원본 프롬프트 파일 무삭제 로드
# ==========================================
PROMPT_FILE_NAME = "CandleView_API_V001-2.txt"

# 파일명 유연 감지
if not os.path.exists(PROMPT_FILE_NAME):
    for filename in os.listdir("."):
        if (
            "001-2" in filename
            or "V001" in filename
            or "Api" in filename
            or "Candle" in filename
        ) and filename.endswith(".txt"):
            PROMPT_FILE_NAME = filename
            break

with open(PROMPT_FILE_NAME, "r", encoding="utf-8") as f:
    CANDLEVIEW_PROMPT_FULL = f.read()

print(
    f"🚀 78.15KB 원본 모듈({PROMPT_FILE_NAME}) 100% 무삭제 로드 성공! (총"
    f" {len(CANDLEVIEW_PROMPT_FULL)} 자)"
)


# 업비트 전체 한글 코인명 100% 자동 수집 함수
def fetch_upbit_korean_map():
    try:
        url = "https://api.upbit.com/v1/market/all?isDetails=false"
        res = requests.get(url).json()
        k_map = {}
        for item in res:
            if item["market"].startswith("KRW-"):
                symbol = item["market"].replace("KRW-", "")
                k_name = item["korean_name"].replace(" ", "").strip()
                k_map[k_name] = symbol
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


def call_gemini_api_with_retry(full_prompt):
    headers = {
        "Content-Type": "application/json",
        "X-goog-api-key": GEMINI_API_KEY,
    }
    payload = {"contents": [{"parts": [{"text": full_prompt}]}]}

    model_urls = [
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent",
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-pro:generateContent",
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent",
    ]

    for url in model_urls:
        for _ in range(2):
            try:
                res = requests.post(url, headers=headers, json=payload)
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
    symbol_input="BTC",
    exchange_name="bybit",
    custom_tfs=["1d", "4h", "1h"],
):
    try:
        ex_name = exchange_name.lower()
        if ex_name in ["업비트", "upbit"]:
            ex_name = "upbit"
            quote_currency = "KRW"
        elif ex_name in ["빗썸", "bithumb"]:
            ex_name = "bithumb"
            quote_currency = "KRW"
        else:
            ex_name = "bybit"
            quote_currency = "USDT"

        clean_input = (
            symbol_input.replace("/", "").replace(":", "").replace(" ", "")
        )
        symbol_upper = UPBIT_KOREAN_MAP.get(
            clean_input, clean_input.upper()
        )

        exchange_class = getattr(ccxt, ex_name)()
        symbol = f"{symbol_upper}/{quote_currency}"

        api_data_payload = f"[STAGE 0 사전 환경 점검]\n• 수집 거래소: {ex_name.upper()}\n• 수집 방식: ■ API Direct Data Stream\n• 데이터 출처: REST API Raw OHLCV & 수신 Close 기반 자체 계산 RMA-RSI 14 Array\n\n=== 코인명: {symbol} ===\n"

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
            api_data_payload += f"\n[{tf} 타임프레임 API 수신 배열 (최근 10봉)]\n"
            for _, row in recent.iterrows():
                api_data_payload += f"O: {row['open']} | H: {row['high']} | L: {row['low']} | C: {row['close']} | V: {row['volume']:.2f} | RSI: {row['rsi']:.2f}\n"

        full_prompt = f"{CANDLEVIEW_PROMPT_FULL}\n\n[API 수신 원천 데이터]\n{api_data_payload}\n\n[실행 명령]\nPHASE 1 데이터 수집 표를 작성한 후, 사용자의 명시적 승인이 완료된 상태로 간주하여 PHASE 2 통합 브리핑까지 연속 완제 출력하십시오."

        return call_gemini_api_with_retry(full_prompt)

    except Exception as e:
        return f"요청 처리 중 오류 발생: {e}"


# 텔레그램 루프 365일 무한 구동
print("🚀 365일 무중단 CandleView 봇 가동 중...")
last_update_id = 0

while True:
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates?offset={last_update_id + 1}&timeout=30"
        updates = requests.get(url).json()

        for update in updates.get("result", []):
            last_update_id = update["update_id"]
            message = update.get("message", {})
            chat_id = message.get("chat", {}).get("id")
            raw_text = message.get("text", "").strip()

            if raw_text.startswith("/"):
                clean_text = raw_text[1:].replace(":", " ").replace(",", " ")
                parts = clean_text.split()
                if not parts:
                    continue

                cmd = parts[0].lower()
                if cmd == "start":
                    requests.post(
                        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                        json={
                            "chat_id": chat_id,
                            "text": (
                                "👋 365일 무중단 CandleView AI 분석 봇입니다.\n\n[사용"
                                " 예시]\n• /btc : 바이비트 비트코인 분석\n•"
                                " /업비트 리플 : 업비트 리플 분석\n• /빗썸"
                                " 도지코인 : 빗썸 도지코인 분석\n"
                            ),
                        },
                    )
                    continue

                exchange_name = "bybit"
                symbol_name = cmd
                custom_tfs = ["1d", "4h", "1h"]

                if len(parts) > 1:
                    first_param = parts[0].lower()
                    if first_param in [
                        "upbit",
                        "bybit",
                        "bithumb",
                        "okx",
                        "업비트",
                        "빗썸",
                    ]:
                        exchange_name = first_param
                        symbol_name = parts[1]
                        if len(parts) > 2:
                            custom_tfs = parts[2:]
                    else:
                        custom_tfs = parts[1:]

                symbol_clean = (
                    symbol_name.replace("/", "").replace(" ", "").strip()
                )
                symbol_mapped = UPBIT_KOREAN_MAP.get(
                    symbol_clean, symbol_clean.upper()
                )
                quote = (
                    "KRW"
                    if exchange_name in ["upbit", "bithumb", "업비트", "빗썸"]
                    else "USDT"
                )

                requests.post(
                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": f"⏳ [{exchange_name.upper()}] {symbol_mapped}/{quote} ({', '.join(custom_tfs)}) API 수집 및 CandleView 연산 중...",
                    },
                )

                result_text = analyze_crypto_dynamic(
                    symbol_clean, exchange_name, custom_tfs
                )

                for i in range(0, len(result_text), 4000):
                    requests.post(
                        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                        json={
                            "chat_id": chat_id,
                            "text": result_text[i : i + 4000],
                        },
                    )

    except Exception as e:
        time.sleep(2)
