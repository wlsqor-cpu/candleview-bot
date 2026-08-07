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
# 엔진 파일 로드 (정확한 파일명 지정 + 디버그) — V001-3 원본 무수정
# ============================================================
CANDLEVIEW_PROMPT_FULL = ""
TARGET_FILE = "CandleView_API_V001-3.txt"

if os.path.exists(TARGET_FILE):
    try:
        with open(TARGET_FILE, "r", encoding="utf-8") as file:
            CANDLEVIEW_PROMPT_FULL = file.read()
            print(f"[INFO] 엔진 파일({TARGET_FILE}) 로드 성공! 문자수: {len(CANDLEVIEW_PROMPT_FULL):,}자 (약 {len(CANDLEVIEW_PROMPT_FULL.encode('utf-8')):,}바이트)")
    except Exception as e:
        print(f"[ERROR] 엔진 파일 읽기 실패: {e}")
else:
    print(f"[ERROR] {TARGET_FILE} 파일이 존재하지 않습니다.")
    print("[DEBUG] 현재 디렉토리 txt 파일 목록:")
    for f in os.listdir("."):
        if f.endswith(".txt"):
            size = os.path.getsize(f)
            print(f"  - {f} ({size:,} bytes)")

if not CANDLEVIEW_PROMPT_FULL:
    CANDLEVIEW_PROMPT_FULL = "CandleView_API_V001 정밀 연산 엔진"
    print("[WARN] 엔진 파일을 찾지 못해 기본 문자열로 대체합니다.")

# ============================================================
# 텔레그램 요약 브리핑 양식 (2단계에서만 사용)
# ============================================================
TELEGRAM_BRIEF_FORMAT = """
===TELEGRAM_BRIEF_START===
📡 데이터 출처: (거래소명) 실시간 API

📊 *개별 타임프레임 분석*
(TF별로 한 줄 공백을 두고, 구조/힘/방향성 결론 위주로 3~4문장 이내 서술. 전문용어는
V001-3의 1:1 한글 번역 매핑표에 따라 쉬운 말을 우선하고, 괄호 태그는 꼭 필요한 곳에만 최소로 사용)

🔗 *타임프레임 종합 상관관계*
(상위-하위 TF 정합성/충돌 핵심만 2~4문장)

🎯 *메인 시나리오*
(우세 등급, 파동 경로, 확률%, 예상 소요기간을 포함하되 문단 사이 공백 줄 유지)

⚡ *힘의 방향성 근거*
(주도 역학과 메인 시나리오 일치 여부를 2~3문장으로)

💰 *실행 전략*
진입가: 
손절가: 
1차 목표가: 
2차 목표가: 
R:R 비율: 

신뢰도: [상/중/하] — (핵심 근거 한 줄)

⚠️ 본 분석은 기술적 시나리오 서술이며 투자 자문이 아닙니다. 투자 판단과 손익 책임은 본인에게 있습니다.
===TELEGRAM_BRIEF_END===

서식 규칙: 각 섹션 사이 반드시 빈 줄 1개 삽입. *별표*는 텔레그램 굵은 글씨 문법이므로 쌍으로만 사용(홀수 개 금지).
"""

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
# Gemini API 호출 (재시도 + 모델 폴백 + 토큰 한도 조절)
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
                    print(f"[WARN] Gemini {url} 응답 코드: {res.status_code}")
                    time.sleep(1)
            except Exception as e:
                print(f"[WARN] Gemini 호출 예외: {e}")
                time.sleep(2)
    return "AI 서버 일시적 과부하 또는 모델 접근 불가 상태입니다. 잠시 후 다시 시도해 주세요."


# ============================================================
# 텔레그램 요약 브리핑 추출 (마커 기반)
# ============================================================
JARGON_TAGS = [
    "[BOS]", "[CHoCH]", "[OB]", "[FVG]", "[CE]", "[UTAD]", "[Spring]",
    "[SOS]", "[SC]", "[F1]", "[F2]", "[F3]", "[F4]", "[Premium]",
    "[Discount]", "[Decay]", "[Role Reversal]", "[Confluence]",
]


def strip_jargon_tags(text):
    for tag in JARGON_TAGS:
        text = text.replace(" " + tag, "").replace(tag, "")
    return text


def extract_telegram_brief(full_text):
    match = re.search(
        r"===TELEGRAM_BRIEF_START===(.*?)===TELEGRAM_BRIEF_END===",
        full_text,
        re.DOTALL,
    )
    if match:
        return strip_jargon_tags(match.group(1).strip())
    # 마커가 없으면 원문 반환 (2단계 실패 시 최소한의 폴백)
    return full_text


# ============================================================
# 텔레그램 전송 (마크다운 우선 시도 → 파싱 실패 시 일반 텍스트로 자동 재전송)
# ============================================================
def send_telegram_message(chat_id, text, timeout=15):
    send_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    res = requests.post(
        send_url,
        json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
        timeout=timeout,
    )
    try:
        ok = res.json().get("ok", False)
    except Exception:
        ok = res.status_code == 200
    if not ok:
        requests.post(
            send_url,
            json={"chat_id": chat_id, "text": text},
            timeout=timeout,
        )


# ============================================================
# 핵심 분석 함수 (2-Stage 구조)
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

        # ---------- 데이터 수집 ----------
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

        # ============================================================
        # STAGE 1 : PHASE 1 + PHASE 2 전체 강제 수행 (분석 전용)
        # ============================================================
        stage1_prompt = (
            f"{CANDLEVIEW_PROMPT_FULL}\n\n"
            f"[API 수신 원천 데이터]\n{payload}\n\n"
            f"PHASE 1 표 작성 후 PHASE 2 완제 브리핑까지 연속 완제 출력하십시오.\n"
            f"텔레그램 요약 형식은 절대 출력하지 마십시오. PHASE 1과 PHASE 2만 완성하십시오."
        )

        print(f"[STAGE 1] {symbol} PHASE 1/2 전체 분석 시작...")
        full_analysis = call_gemini_api_with_retry(stage1_prompt, max_tokens=16384)

        if not full_analysis or "과부하" in full_analysis or len(full_analysis) < 200:
            return f"STAGE 1 분석 실패: {full_analysis}"

        print(f"[STAGE 1] 완료 (문자수: {len(full_analysis):,})")

        # ============================================================
        # STAGE 2 : 이미 도출된 결과만 재료로 텔레그램 요약 재서술
        # ============================================================
        stage2_prompt = (
            f"아래는 이미 완성된 PHASE 1 / PHASE 2 전체 분석 결과입니다.\n"
            f"이 결과만을 유일한 재료로 사용하십시오.\n"
            f"새로운 수치·새로운 판단·새로운 추정·새로운 시나리오를 절대 추가하지 마십시오.\n"
            f"이미 도출된 결론을 그대로 재서술만 하십시오.\n\n"
            f"[PHASE 1 / PHASE 2 원본 결과]\n"
            f"{full_analysis}\n\n"
            f"위 결과만을 사용하여 아래 형식의 텔레그램 요약 브리핑만 출력하십시오.\n"
            f"{TELEGRAM_BRIEF_FORMAT}"
        )

        print(f"[STAGE 2] 텔레그램 요약 재서술 시작...")
        brief_raw = call_gemini_api_with_retry(stage2_prompt, max_tokens=4096)

        if not brief_raw or "과부하" in brief_raw:
            return f"STAGE 2 요약 실패: {brief_raw}"

        brief = extract_telegram_brief(brief_raw)
        print(f"[STAGE 2] 완료 (문자수: {len(brief):,})")

        return brief

    except Exception as e:
        return f"요청 처리 중 오류 발생: {e}"


# ============================================================
# 텔레그램 메인 루프
# ============================================================
print("🚀 CandleView AI 봇 가동 시작 (2-Stage / Gemini 3.6 / 3.5-lite)")

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

            if cmd == "start":
                send_telegram_message(
                    chat_id,
                    (
                        "🕯️ CandleView AI — PA/VSA 정밀 차트 분석 봇입니다.\n\n"
                        "[사용 예시]\n"
                        "• /btc\n"
                        "• /업비트 리플\n"
                        "• /빗썸 도지코인\n"
                        "• /btc 1d 4h 1h\n"
                        "• /upbit sol 4h 1h 15m"
                    ),
                )
                continue

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

            status_msg = f"🕯️ CandleView AI가 [{ex_name.upper()}] {sym_mapped}/{quote} ({', '.join(tfs)}) 분석 중..."
            send_telegram_message(chat_id, status_msg)

            result_text = analyze_crypto_dynamic(sym_clean, ex_name, tfs)

            for i in range(0, len(result_text), 4000):
                send_telegram_message(chat_id, result_text[i : i + 4000])

    except Exception as e:
        print(f"[ERROR] 메인 루프 예외: {e}")
        time.sleep(2)
