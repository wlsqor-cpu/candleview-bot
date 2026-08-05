from http.server import HTTPServer, BaseHTTPRequestHandler
import json
import os
import re
import threading
import time
from datetime import datetime

import ccxt
import numpy as np
import pandas as pd
import requests

# ============================================================
# 🛡️ 중복 인스턴스 원천 차단 (Render Rolling Deploy 완벽 대응)
# ============================================================
import fcntl, os
_INSTANCE_LOCK = open("/tmp/candleview_single.lock", "w")
try:
    fcntl.flock(_INSTANCE_LOCK, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    print("[INFO] 중복 인스턴스 감지 → 자동 종료 (충돌 방지)")
    os._exit(0)


# ============================================================
# 🔧 1. 공통 유틸리티: 로그 + Render Health Check
# ============================================================
def log(level, msg):
    """Render 대시보드에서 보기 쉽게 타임스탬프 포함 로그 출력"""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] [{level.upper()}] {msg}", flush=True)

class HealthCheckHandler(BaseHTTPRequestHandler):
    """✅ UptimeRobot이 어떤 경로로 핑해도 200 OK 반환 → 절전 방지 안정화"""
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"CandleView AI Bot OK")
    def log_message(self, format, *args):
        pass  # 헬스체크 로그는 출력 안함 (로그 창 깨끗하게 유지)

def run_port_server():
    try:
        port = int(os.environ.get("PORT", 10000))
        server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
        log("info", f"포트 {port} 오픈 완료. Render 수신 대기 중...")
        server.serve_forever()
    except Exception as e:
        log("error", f"포트 서버 스레드 오류: {e}")
threading.Thread(target=run_port_server, daemon=True).start()

# ============================================================
# 🔐 2. 환경변수 로드 + 전역 객체 초기화
# ============================================================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()

if not TELEGRAM_BOT_TOKEN or not GEMINI_API_KEY:
    log("warn", "TELEGRAM_BOT_TOKEN 또는 GEMINI_API_KEY 환경변수가 비어 있습니다. Render 대시보드에서 등록하세요.")

# ✅ ccxt 거래소 객체는 전역으로 한 번만 생성 (매 요청마다 새로 만들면 오버헤드 + 차단 위험)
EXCHANGES = {}
def get_exchange(name):
    name = name.lower()
    if name not in EXCHANGES:
        cls = getattr(ccxt, name)
        EXCHANGES[name] = cls({"enableRateLimit": True, "timeout": 15000})  # ✅ 거래소 타임아웃 15초 제한
    return EXCHANGES[name]

# ============================================================
# 📘 3. 78KB 엔진 파일 + 업비트 한글 코인 맵 로드
# ============================================================
CANDLEVIEW_PROMPT_FULL = ""
for f in sorted(os.listdir(".")):
    if ("001" in f or "Candle" in f or "Api" in f) and f.endswith(".txt"):
        try:
            with open(f, "r", encoding="utf-8") as fp:
                CANDLEVIEW_PROMPT_FULL = fp.read()
            log("info", f"엔진 파일({f}) 로드 성공! 크기: {len(CANDLEVIEW_PROMPT_FULL):,}바이트")
            break
        except Exception as e:
            log("error", f"엔진 파일 로드 실패: {e}")
if not CANDLEVIEW_PROMPT_FULL:
    CANDLEVIEW_PROMPT_FULL = "CandleView_API_V001 정밀 연산 엔진"
    log("warn", "엔진 파일을 찾지 못해 기본 문구로 대체합니다.")

def fetch_upbit_korean_map():
    try:
        res = requests.get(
            "https://api.upbit.com/v1/market/all?isDetails=false",
            timeout=10,
            headers={"User-Agent": "CandleViewBot/1.0"}
        ).json()
        k_map = {}
        for item in res:
            if item["market"].startswith("KRW-"):
                sym = item["market"].replace("KRW-", "")
                k_name = item["korean_name"].replace(" ", "").strip()
                k_map[k_name] = sym
        log("info", f"업비트 한글 맵 로드: {len(k_map)}개 코인")
        return k_map
    except Exception as e:
        log("error", f"업비트 마켓 맵 로드 실패: {e}")
        return {}
UPBIT_KOREAN_MAP = fetch_upbit_korean_map()

# ============================================================
# 📊 4. 기술 지표 계산 (파이썬에서 전부 처리 → 토큰 대폭 절감)
# ============================================================
def calculate_rma_rsi(prices, period=14):
    delta = prices.diff()
    gain = delta.where(delta > 0, 0).fillna(0)
    loss = (-delta.where(delta < 0, 0)).fillna(0)
    avg_gain = gain.ewm(alpha=1/period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, adjust=False).mean()
    rs = avg_gain / (avg_loss + 1e-10)
    return 100 - (100 / (1 + rs))

def calculate_indicators(df):
    """✅ 각 타임프레임별 핵심 지표를 미리 계산 → Gemini에는 요약만 전송"""
    df = df.copy()
    df["rsi"] = calculate_rma_rsi(df["close"])
    df["ma20"] = df["close"].rolling(20).mean()
    df["ma50"] = df["close"].rolling(50).mean()
    df["atr"] = (df["high"] - df["low"]).rolling(14).mean()
    
    recent = df.tail(30).reset_index(drop=True)  # ✅ 최근 30봉만 요약 (원래 100봉 → 70% 절감)
    return {
        "now": {
            "open": recent.iloc[-1]["open"], "high": recent.iloc[-1]["high"],
            "low": recent.iloc[-1]["low"], "close": recent.iloc[-1]["close"],
            "volume": recent.iloc[-1]["volume"], "rsi": recent.iloc[-1]["rsi"],
            "ma20": recent.iloc[-1]["ma20"], "ma50": recent.iloc[-1]["ma50"], "atr": recent.iloc[-1]["atr"],
        },
        "30b_high": recent["high"].max(), "30b_low": recent["low"].min(),
        "30b_vol_avg": recent["volume"].mean(),
        "candles": recent[["open","high","low","close","volume","rsi"]].to_dict("records")
    }

# ============================================================
# 🤖 5. Gemini API 호출 로직 완전 재구현 (핵심 개선)
# ============================================================
# ✅ 2026.8 기준 정상 동작하는 v1 정식 엔드포인트만 사용 (우선순위 순)
GEMINI_ENDPOINTS = [
    "https://generativelanguage.googleapis.com/v1/models/gemini-2.0-flash:generateContent",
    "https://generativelanguage.googleapis.com/v1/models/gemini-1.5-flash-002:generateContent",
    "https://generativelanguage.googleapis.com/v1/models/gemini-1.5-flash:generateContent",
]
GEMINI_HEADERS = {
    "Content-Type": "application/json",
    "x-goog-api-key": GEMINI_API_KEY,
}

def call_gemini(full_prompt):
    """
    ✅ 개선점:
    1. Render 30초 제한 고려 → 타임아웃 25초로 제한
    2. 지수 백오프 재시도 (2초 → 4초 → 8초)
    3. 모든 에러 코드 처리 + URL 순회
    4. 실패시 원인 로그 남기고 None 반환 → 상위에서 폴백 처리
    """
    payload = {"contents": [{"parts": [{"text": full_prompt}]}]}
    
    for ep_idx, url in enumerate(GEMINI_ENDPOINTS, 1):
        for attempt in range(1, 4):  # 엔드포인트당 최대 3회 재시도
            try:
                log("info", f"Gemini 호출 [{ep_idx}/{len(GEMINI_ENDPOINTS)}] 시도 {attempt}/3...")
                res = requests.post(
                    url, headers=GEMINI_HEADERS, json=payload,
                    timeout=25  # ✅ Render 30초 제한보다 5초 짧게 설정
                )
                
                if res.status_code == 200:
                    data = res.json()
                    text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
                    log("info", f"Gemini 응답 성공! ({len(text):,}자)")
                    return text
                
                # 에러 케이스별 처리
                if res.status_code in (500, 502, 503, 504, 408):
                    wait = 2 ** attempt  # 2, 4, 8초 지수 백오프
                    log("warn", f"  → 일시적 오류 {res.status_code}, {wait}초 후 재시도")
                    time.sleep(wait)
                    continue
                if res.status_code == 429:
                    log("warn", f"  → Rate Limit(429), 10초 후 재시도")
                    time.sleep(10)
                    continue
                if res.status_code in (400, 401, 403, 404):
                    log("error", f"  → 치명적 오류 {res.status_code}: {res.text[:200]}")
                    break  # 인증/엔드포인트 오류는 재시도 의미 없음 → 다음 URL로
                
                log("error", f"  → 기타 오류 {res.status_code}: {res.text[:200]}")
                time.sleep(2)
                
            except requests.exceptions.Timeout:
                log("warn", f"  → Gemini 타임아웃 (25초 초과), 다음 시도")
                time.sleep(1)
            except Exception as e:
                log("error", f"  → 예외 발생: {type(e).__name__}: {e}")
                time.sleep(2)
    
    log("error", "모든 Gemini 엔드포인트 최종 실패")
    return None  # ✅ 에러 메시지 직접 반환 안함 → 상위에서 로컬 폴백 처리

# ============================================================
# 💡 6. 코인 분석 메인 로직 (프롬프트 경량화 적용)
# ============================================================
def analyze_crypto(symbol_input="BTC", exchange_name="bybit", custom_tfs=None):
    custom_tfs = custom_tfs or ["1d", "4h", "1h"]
    
    # 거래소/시세 통화 매핑
    ex_name = exchange_name.lower()
    kr_ex = {"업비트":"upbit", "빗썸":"bithumb"}
    if ex_name in kr_ex: ex_name = kr_ex[ex_name]
    quote = "KRW" if ex_name in ("upbit","bithumb") else "USDT"
    
    # 심볼 정규화 + 한글 매핑
    clean = symbol_input.replace("/","").replace(":","").replace(" ","").strip()
    symbol_upper = UPBIT_KOREAN_MAP.get(clean, clean.upper())
    symbol = f"{symbol_upper}/{quote}"
    
    try:
        exchange = get_exchange(ex_name)
        log("info", f"데이터 수집 시작: {ex_name} {symbol} / 타임프레임: {custom_tfs}")
        
        # ✅ 각 타임프레임별 데이터 수집 + 지표 계산
        tf_data = {}
        for tf in custom_tfs:
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe=tf, limit=120)
            df = pd.DataFrame(ohlcv, columns=["timestamp","open","high","low","close","volume"])
            tf_data[tf] = calculate_indicators(df)
        
        # ✅ 프롬프트 조립: 원시 데이터 대신 계산 완료된 요약만 전송 → 토큰 60% 절감
        payload = f"[STAGE 0 사전 환경 점검]\n• 거래소: {ex_name.upper()}\n• 방식: API Direct Data\n\n=== 코인: {symbol} ===\n"
        for tf, d in tf_data.items():
            n = d["now"]
            payload += f"\n--- [{tf}] 최종 상태 ---\n"
            payload += f"현재가 O:{n['open']:.2f} H:{n['high']:.2f} L:{n['low']:.2f} C:{n['close']:.2f}\n"
            payload += f"거래량: {n['volume']:.2f} | RSI(14): {n['rsi']:.2f}\n"
            payload += f"MA20: {n['ma20']:.2f} | MA50: {n['ma50']:.2f} | ATR(14): {n['atr']:.4f}\n"
            payload += f"30봉 최고: {d['30b_high']:.2f} / 최저: {d['30b_low']:.2f} / 평균거래량: {d['30b_vol_avg']:.2f}\n"
            payload += "[최근 15봉 O/H/L/C/V/RSI]\n"
            for r in d["candles"][-15:]:
                payload += f"{r['open']:.2f}|{r['high']:.2f}|{r['low']:.2f}|{r['close']:.2f}|{r['volume']:.2f}|{r['rsi']:.2f}\n"
        
        final_prompt = (
            f"{CANDLEVIEW_PROMPT_FULL}\n\n"
            f"[API 수신 원천 데이터 (요약)]\n{payload}\n\n"
            "PHASE 1 표 작성 후 PHASE 2 완제 브리핑까지 연속 완제 출력하십시오."
        )
        
        # Gemini 호출
        result = call_gemini(final_prompt)
        if result:
            return result
        
        # ✅ Gemini 완전 실패시 → 로컬 정량 분석 폴백 반환
        log("warn", "로컬 정량 분석 결과로 폴백 응답합니다.")
        return build_local_fallback_report(symbol, ex_name, custom_tfs, tf_data)
        
    except Exception as e:
        log("error", f"분석 중 예외: {type(e).__name__}: {e}")
        return f"❌ 요청 처리 중 오류가 발생했습니다.\n사유: {type(e).__name__}: {str(e)[:150]}\n잠시 후 다시 시도해 주세요."

def build_local_fallback_report(symbol, ex_name, tfs, tf_data):
    """✅ Gemini 불능시 최소한의 분석 결과라도 제공"""
    lines = [
        "⚠️ AI 서버 일시 지연으로 **로컬 정량 분석 결과**만 우선 전송합니다.",
        f"잠시 후 명령을 다시 입력하시면 정밀 AI 분석이 가능합니다.\n",
        f"📊 [{ex_name.upper()}] {symbol} 정량 요약",
        "=" * 40,
    ]
    for tf in tfs:
        d = tf_data[tf]
        n = d["now"]
        rsi = n["rsi"]
        rsi_state = "과매수🔴" if rsi > 70 else "과매도🟢" if rsi < 30 else "중립⚪"
        trend = "상승추세" if n["ma20"] > n["ma50"] else "하락추세" if n["ma20"] < n["ma50"] else "횡보"
        
        lines.extend([
            f"\n⏱ {tf} 타임프레임",
            f"  종가: {n['close']:.2f} | RSI(14): {rsi:.1f} ({rsi_state})",
            f"  MA20: {n['ma20']:.2f} / MA50: {n['ma50']:.2f} → {trend}",
            f"  30봉 구간: 최고 {d['30b_high']:.2f} / 최저 {d['30b_low']:.2f}",
            f"  ATR(14): {n['atr']:.4f} (1봉 평균 변동폭)",
        ])
    return "\n".join(lines)

# ============================================================
# 📱 7. 텔레그램 메시지 전송 유틸
# ============================================================
def tg_send(chat_id, text, reply_to=None):
    """텔레그램 메시지 전송 + 4000자 자동 분할"""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for i in range(0, len(text), 4000):
        chunk = text[i:i+4000]
        try:
            payload = {"chat_id": chat_id, "text": chunk, "parse_mode": "Markdown"}
            r = requests.post(url, json=payload, timeout=10)
            if r.status_code != 200:
                # 마크다운 실패시 일반 텍스트로 재시도
                requests.post(url, json={"chat_id": chat_id, "text": chunk}, timeout=10)
        except Exception as e:
            log("error", f"텔레그램 전송 실패: {e}")

def tg_edit(chat_id, msg_id, text):
    """기존 '처리 중' 메시지를 결과로 교체"""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText"
    try:
        requests.post(url, json={"chat_id": chat_id, "message_id": msg_id, "text": text}, timeout=10)
    except Exception as e:
        log("error", f"메시지 수정 실패: {e}")
        tg_send(chat_id, text)  # 수정 실패시 새로 보냄

# ============================================================
# 🔄 8. 메인 루프 (롱 폴링 안정화)
# ============================================================
log("info", "🚀 CandleView AI 봇 가동 시작!")
last_update_id = 0
START_MSG = """👋 365일 무중단 CandleView AI 분석 봇입니다.

[사용 예시]
• /btc : 바이비트 비트코인 분석
• /업비트 리플 : 업비트 리플 분석
• /빗썸 도지코인 : 빗썸 도지코인 분석
• /업비트 pump 1d,4h,1h,15m : 타임프레임 직접 지정
"""

while True:
    try:
        # ✅ 롱 폴링 타임아웃 15초로 단축 → Render 30초 제한 절대 안걸림
        updates = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates",
            params={"offset": last_update_id + 1, "timeout": 15, "allowed_updates": ["message"]},
            timeout=20,
        ).json()
        
        if not updates.get("ok"):
            log("warn", f"텔레그램 getUpdates 오류: {updates}")
            time.sleep(3)
            continue
        
        for update in updates.get("result", []):
            last_update_id = update["update_id"]
            msg = update.get("message", {})
            chat_id = msg.get("chat", {}).get("id")
            raw_text = msg.get("text", "").strip()
            if not chat_id or not raw_text.startswith("/"):
                continue
            
            # 명령 파싱
            clean_text = raw_text[1:].replace(":", " ").replace(",", " ")
            parts = [p for p in clean_text.split() if p]
            if not parts:
                continue
            cmd = parts[0].lower()
            
            # /start
            if cmd == "start":
                tg_send(chat_id, START_MSG)
                continue
            
            # 분석 명령 처리
            ex_name = "bybit"
            sym_name = cmd
            tfs = ["1d", "4h", "1h"]
            
            if len(parts) >= 2 and parts[0].lower() in ("upbit","bybit","bithumb","okx","업비트","빗썸"):
                ex_name = parts[0]
                sym_name = parts[1]
                tfs = parts[2:] if len(parts) > 2 else tfs
            elif len(parts) >= 2:
                sym_name = parts[0]
                tfs = parts[1:]
            
            # ✅ 1. 먼저 "처리 중" 메시지 보내기 (사용자 대기감 해소)
            quote = "KRW" if ex_name in ("upbit","bithumb","업비트","빗썸") else "USDT"
            sym_mapped = UPBIT_KOREAN_MAP.get(sym_name.replace(" ",""), sym_name.upper())
            status = f"⏳ [{ex_name.upper()}] {sym_mapped}/{quote} ({', '.join(tfs)})\nAPI 수집 및 AI 연산 중... (평균 15~25초)"
            status_resp = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": status}, timeout=10
            ).json()
            status_msg_id = status_resp.get("result", {}).get("message_id")
            
            # ✅ 2. 실제 분석 실행
            log("info", f"명령 수신: {raw_text} → {ex_name} {sym_name} {tfs}")
            result = analyze_crypto(sym_name, ex_name, tfs)
            
            # ✅ 3. 결과를 기존 상태 메시지에 덮어쓰기 (깔끔한 UX)
            if status_msg_id:
                tg_edit(chat_id, status_msg_id, result)
            else:
                tg_send(chat_id, result)
        
    except Exception as e:
        log("error", f"메인 루프 예외: {type(e).__name__}: {e}")
        time.sleep(3)
