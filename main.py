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


# 1. Render 포트 오픈 (0.1초 즉시 가동)
def run_port_server():
    try:
        port = int(os.environ.get("PORT", 10000))
        server = HTTPServer(("0.0.0.0", port), SimpleHTTPRequestHandler)
        print(f"포트 {port} 오픈 완료. Render 수신 대기 중...")
        server.serve_forever()
    except Exception as e:
        print(f"포트 서버 스레드 오류: {e}")


threading.Thread(target=run_port_server, daemon=True).start()

# 2. 유저 토큰 및 API 키 설정
TELEGRAM_BOT_TOKEN = "8897306377:AAEZBAvMCLdUajN497MI65r593tZo1wHZCc"
GEMINI_API_KEY = "AQ.Ab8RN6K4Yfao6wc_F1K8jgNdu0hZu-v4vutnFN78L3f52ukhHw"

# 3. 파일 자동 탐색 및 안전 로드 (FileNotFoundError 100% 방지)
CANDLEVIEW_PROMPT_FULL = ""
current_files = os.listdir(".")
target_file_name = None

for f in current_files:
    if ("001" in f or "Candle" in f or "Api" in f) and f.endswith(".txt"):
        target_file_name = f
        break

if target_file_name:
    try:
        with open(target_file_name, "r", encoding="utf-8") as file:
            CANDLEVIEW_PROMPT_FULL = file.read()
        print(f"🚀 엔진 파일({target_file_name}) 로드 성공!")
    except Exception as e:
        print(f"파일 로드 중 예외 발생: {e}")

if not CANDLEVIEW_PROMPT_FULL:
    CANDLEVIEW_PROMPT_FULL = """
✅ CandleView_API_V001 (PA-VSA 전용 API 데이터 정밀 연산 엔진)
=== 0. 입력 인터페이스 ===
필수 입력 규격: 코인명 (심볼, 예: BTCUSDT), 타임프레임 개수 선택: 2~4개 자유 선택
=== 1. PHASE 실행 강제 규칙 ===
1. PHASE 1 데이터 수집 표 작성.
2. PHASE 2 개별 TF 분석, TF간 유기적 상관관계, 메인 시나리오, P_entry, P_inv, P_target_1/2, R:R 비율 완제 출력.
=== 6. 정체성 및 사고 우선순위 ===
PA-VSA 전용 기술적 차트 분석 엔진.
=== 11. [Layer 5-A] PHASE 2 서술 출력 서식 ===
① 개별 TF 분석 ➔ ② TF 간 유기적 상관관계 ➔ ③ 메인 시나리오 ➔ ④ 힘의 방향성 원리 ➔ ⑤ 리스크/기회/실행전략 수치화 ➔ ⑥ 신뢰도 라벨 ➔ ⑦ 투자판단 책임 고지 완제 출력.
"""
    print("기본 백업 프롬프트 로드 완료.")


# 업비트 전체 한글 코인 오토 매핑
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
print("🚀 365일 무중단 CandleView 봇 가동 시작!")
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
