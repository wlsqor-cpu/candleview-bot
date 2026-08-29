"""
CandleView main-10.py 회귀 테스트 스위트
==========================================
이 파일은 main-10.py를 통째로 import하지 않는다(모듈 최상단에 거래소 API
호출·파일 읽기 등 네트워크/부작용 코드가 있어 테스트 환경에서 실패하거나
멈출 수 있기 때문). 대신 순수 로직 함수 4개의 소스코드만 안전하게 추출해서
격리된 네임스페이스에 로드한 뒤 테스트한다.

실행 방법:
    pip install pytest --break-system-packages
    pytest test_main_10.py -v

전제조건: 이 파일과 같은 폴더(또는 상위 폴더)에 main-10.py가 있어야 한다.

각 테스트케이스는 "왜 이게 중요한지"를 실제 발견된 결함(판정이력대장 번호)과
함께 주석으로 남긴다 — 나중에 코드를 고치다가 같은 버그가 재발하는지
자동으로 잡아내는 게 목적이다(Superpowers/TDD 방식론 적용).
"""
import re
import sys
import pathlib
import pytest


# ============================================================
# main-10.py에서 순수 함수 블록만 안전하게 추출하는 헬퍼
# ============================================================
def _find_main_py():
    """main-10.py를 현재 폴더 → 상위 폴더 순으로 탐색."""
    candidates = ["main-10.py", "main-10[C].py", "main.py"]
    for base in [pathlib.Path.cwd(), pathlib.Path.cwd().parent, pathlib.Path(__file__).parent]:
        for name in candidates:
            p = base / name
            if p.exists():
                return p
    raise FileNotFoundError(
        "main-10.py를 찾을 수 없습니다. 이 테스트파일과 같은 폴더에 두세요."
    )


def _find_spec_file():
    """CandleView_API.txt를 현재 폴더 → 상위 폴더 순으로 탐색(_find_main_py와 동일 패턴)."""
    for base in [pathlib.Path.cwd(), pathlib.Path.cwd().parent, pathlib.Path(__file__).parent]:
        p = base / "CandleView_API.txt"
        if p.exists():
            return p
    raise FileNotFoundError(
        "CandleView_API.txt를 찾을 수 없습니다. 이 테스트파일과 같은 폴더에 두세요."
    )


def _load_target_functions():
    """classify_candle_shape, resolve_symbol_korean_name, verify_and_fix_phase2를
    격리된 네임스페이스에 로드. 이 함수들이 의존하는 전역(한글명 맵)은
    테스트용 목(mock) 데이터로 주입한다(실제 거래소 API 호출 없이 테스트하기 위함)."""
    src = _find_main_py().read_text(encoding="utf-8")
    import decimal
    import numpy as _np
    ns = {"re": re, "np": _np, "Decimal": decimal.Decimal,
          "InvalidOperation": decimal.InvalidOperation, "ROUND_HALF_UP": decimal.ROUND_HALF_UP}
    # 명세 SSOT를 실제 파일에서 읽어 주입한다(상수 하드코딩 금지 — 매뉴얼 3번 4항).
    ns["CANDLEVIEW_PROMPT_FULL"] = _find_spec_file().read_text(encoding="utf-8")

    # 테스트용 목 데이터 — 실제 API 대신 사용. 오늘 발견된 실사례(CBK/THETA/MBL) 포함.
    ns["UPBIT_KOREAN_MAP"] = {"코박토큰": "CBK", "세타토큰": "THETA", "무비블록": "MBL"}
    ns["BITHUMB_KOREAN_MAP"] = {"세타토큰": "THETA"}

    # --- classify_candle_shape 블록 (상수 4개 + 함수) ---
    start = src.index("BODY_RATIO_DOJI = 10.0")
    end = src.index("\n\n\n", src.index("return tags"))
    exec(src[start:end], ns)

    # --- resolve_symbol_korean_name 블록 (역맵 구축 + 함수) ---
    exec(
        "_UPBIT_SYMBOL_TO_KOREAN = {v: k for k, v in UPBIT_KOREAN_MAP.items()}\n"
        "_BITHUMB_SYMBOL_TO_KOREAN = {v: k for k, v in BITHUMB_KOREAN_MAP.items()}\n",
        ns,
    )
    start = src.index("def resolve_symbol_korean_name")
    end = src.index("\n\n\n", start)
    exec(src[start:end], ns)

    # --- verify_and_fix_phase2 블록 ---
    # verify_and_fix_phase2는 verify_and_strip_evidence_ledger를 호출하므로 의존도 함께
    # 로드해야 한다. 누락 시 NameError로 이 클래스의 테스트가 전부 실패한다(하네스 결손).
    for dependency in ("def verify_and_strip_evidence_ledger", "def verify_and_fix_phase2"):
        start = src.index(dependency)
        end = src.index("\n\n\n", start)
        exec(src[start:end], ns)

    # --- 표시 결속·경로 렌더링 계층 ---
    for symbol in (
        "TF_STANDARD_MINUTES = {",
        "TF_UNIT_MINUTES = {",
        "KOREAN_TF_UNITS = {",
        "def _canonical_timeframe_from_minutes",
        "PHASE1_FACT_LABELS = (",
        "def _normalize_zero_width_range",
        "def build_phase1_fact_registry",
        "def normalize_phase1_tf_heading",
        "def phase1_registry_timeframes",
        "def _load_core_score_cap_from_spec",
        "LEDGER_ZERO_EPS = ",
        "def _extract_ledger_value",
        "def _extract_signed_axis_scores",
        "def _direction_from_net",
        "def _extract_price_path",
        "def _load_phase2_probability_params_from_spec",
        "def _parse_decimal",
        "def _format_display_number",
        "def _evidence_strength",
        "def _verified_probability_pair",
        "def build_phase2_display_contract",
        "def _strip_model_phase2_success_tag",
        "MAIN_PATH_HEADER_PATTERN = ",
        "def _is_path_body_line",
        "def _path_section_end",
        "def _find_path_header",
        "def _classify_route_context_line",
        "def _retain_route_context",
        "def _scenario_direction_labels",
        "MAIN_PATH_LINE_ORDER = ",
        "def _path_line_rank",
        "def _render_path_block",
        "VISIBLE_DECISION_PATTERN = ",
        "def _annotate_visible_decision_strength",
        "def _append_decision_cards_without_edit",
        "def _strip_nonroute_probability_lines",
        "SIDEWAYS_PATH_NOTICE = ",
        "def _build_decision_cards",
        "def _phase1_fact_value",
        "def _phase2_analysis_target_current_price",
        "PHASE2_ANALYSIS_TARGET_SECTION_PATTERN = ",
        "def normalize_phase2_analysis_target_current_price",
        "def render_verified_phase2_decision_blocks",
        "TF_ANALYSIS_HEADING_PATTERN = ",
        "def _phase2_tf_body_lines",
        "def _phase2_tf_evidence_units",
        "def phase2_briefing_completeness_observations",
        "STATIC_APPROVED_MODELS = [",
        "def _format_origin_fact_number",
        "ORIGIN_FACT_STATE_LABELS = ",
        "def build_phase1_origin_fact_registry",
        "def _phase1_fact_value",
        "def _phase2_tf_backfill_facts",
        "def _phase2_tf_opposing_facts",
        "BACKFILL_PROGRESS_PREFIX = ",
        "def _fact_numbers_already_narrated",
        "def _phase2_tf_missing_state_facts",
        "def _phase2_integration_missing_boundary",
        "PHASE2_INTEGRATION_SECTION_PATTERN = ",
        "def _ensure_phase2_integration_boundary",
        "def _normalize_phase2_integration_structure_claims",
        "def ensure_phase2_tf_narrative_completeness",
    ):
        start = src.index(symbol)
        end = src.index("\n\n\n", start)
        exec(src[start:end], ns)

    # 모듈 최상단에서 함수 호출로 확정되는 SSOT 파생 상수를 동일 방식으로 재현한다.
    ns["LEDGER_SCORE_LIMIT"] = ns["_load_core_score_cap_from_spec"]()
    ns["PHASE2_PROBABILITY_PARAMS"] = ns["_load_phase2_probability_params_from_spec"]()

    return ns


@pytest.fixture(scope="module")
def fns():
    return _load_target_functions()


# ============================================================
# classify_candle_shape 검증
# 근거: 판정이력대장 29·30번 — Gemini가 이미 주어진 고가/저가/현재가로부터
# 도출 가능한 꼬리방향을 반대로 서술(STORJ/KRW 1w·1d 실사례)했던 결함.
# ============================================================
class TestClassifyCandleShape:
    def test_storj_real_case_upper_wick(self, fns):
        """[판정29 재현] 고102.0/저58.6/현재67.5 — 실제로는 윗꼬리인데
        Gemini가 '아래꼬리(망치형)'이라고 반대로 서술했던 실사례.
        정정된 함수는 반드시 '유성형(윗꼬리)'로 분류해야 한다."""
        tags = fns["classify_candle_shape"](o=61.0, h=102.0, l=58.6, c=67.5)
        joined = " ".join(tags)
        assert "유성형" in joined, f"윗꼬리 사례인데 유성형으로 분류 안 됨: {tags}"
        assert "망치형" not in joined, f"윗꼬리 사례가 망치형(반대)으로 분류됨: {tags}"

    def test_genuine_hammer_shape(self, fns):
        """대조군: 진짜 망치형(몸통 상단, 아래꼬리 우세)은 정확히 망치형으로 분류돼야 함."""
        tags = fns["classify_candle_shape"](o=95, h=100, l=58.6, c=98)
        assert any("망치형" in t for t in tags)

    def test_genuine_shooting_star_shape(self, fns):
        """대조군: 진짜 유성형(몸통 하단, 윗꼬리 우세)."""
        tags = fns["classify_candle_shape"](o=60, h=100, l=58, c=62)
        assert any("유성형" in t for t in tags)

    def test_doji_vs_hammer_priority(self, fns):
        """[구체성 원칙, 스펙 33번 인접] 몸통이 도지(≤10%) 조건과 망치형(≤30%+
        위치+꼬리) 조건을 동시 만족하면 반드시 망치형(조건이 더 많은 쪽)이 우선."""
        tags = fns["classify_candle_shape"](o=96, h=100, l=58.6, c=97)
        joined = " ".join(tags)
        assert "망치형" in joined
        assert "도지" not in joined

    def test_engulfing_pattern(self, fns):
        """대조군: 장악형(직전봉 몸통을 100% 이상 반대방향으로 감쌈)."""
        tags = fns["classify_candle_shape"](
            o=60, h=70, l=58, c=68, prev_o=63, prev_h=64, prev_l=59, prev_c=60
        )
        assert any("장악형" in t for t in tags)

    def test_zero_range_bar_is_safe(self, fns):
        """엣지케이스: 고가=저가(무변동봉)일 때 0나눗셈 없이 빈 리스트 반환해야 함."""
        tags = fns["classify_candle_shape"](o=100, h=100, l=100, c=100)
        assert tags == []

    def test_zero_prev_body_is_safe(self, fns):
        """엣지케이스: 직전봉이 완전 도지(몸통=0)일 때 장악형/관통형 계산에서
        0나눗셈이 발생하지 않아야 함(예외 없이 정상 반환)."""
        tags = fns["classify_candle_shape"](
            o=60, h=65, l=59, c=64, prev_o=61, prev_h=62, prev_l=60, prev_c=61
        )
        assert isinstance(tags, list)  # 예외 없이 리스트 반환되면 통과


# ============================================================
# resolve_symbol_korean_name 검증
# 근거: 판정이력대장 32번 — CBK를 '무비블록'(실제 MBL)으로,
# THETA를 '테타토큰'(실제 세타토큰)으로 오기했던 FindCoin 실사례.
# ============================================================
class TestResolveSymbolKoreanName:
    def test_cbk_resolves_to_kobak_token(self, fns):
        """[판정32 재현] CBK는 코박토큰이어야 함(무비블록 아님)."""
        assert fns["resolve_symbol_korean_name"]("CBK", "upbit") == "코박토큰"

    def test_theta_resolves_to_correct_name(self, fns):
        """[판정32 재현] THETA는 세타토큰이어야 함('테타토큰' 오기 아님)."""
        name = fns["resolve_symbol_korean_name"]("THETA", "upbit")
        assert name == "세타토큰"
        assert name != "테타토큰"

    def test_mbl_resolves_to_moviebloc(self, fns):
        assert fns["resolve_symbol_korean_name"]("MBL", "upbit") == "무비블록"

    def test_unmapped_symbol_returns_none(self, fns):
        """미상장/미확인 코인은 None을 반환해야 함(호출측이 '임의창작 금지' 폴백
        처리를 할 수 있도록) — 절대 그럴듯한 이름을 지어내면 안 됨."""
        assert fns["resolve_symbol_korean_name"]("NOTREAL", "upbit") is None

    def test_bithumb_prefers_own_map_over_upbit(self, fns):
        """거래소별 우선순위: 빗썸 분석 시에는 빗썸 자체 맵을 1순위로 사용."""
        assert fns["resolve_symbol_korean_name"]("THETA", "bithumb") == "세타토큰"


# ============================================================
# verify_and_fix_phase2 검증 (OB-FVG 중첩 서술 검증)
# 근거: 판정이력대장 29-c번 — 실제로는 겹침폭 0(경계접촉)인데
# '상호 중첩'이라고 과장 서술했던 실사례.
# ============================================================
class TestVerifyAndFixPhase2:
    def test_false_overlap_claim_triggers_warning(self, fns):
        """[판정29-c 재현] OB(59.7~60.6)와 FVG(60.6~70.8)는 경계접촉일 뿐인데
        '상호 중첩'이라 서술 — 겹침폭 0으로 검증실패 경고가 붙어야 함."""
        text = (
            "세력 매집 수급 벽 [OB](59.7 ~ 60.6 KRW)과 가격 공백대 [FVG]"
            "(60.6 ~ 70.8 KRW)가 상호 중첩되어 있습니다."
        )
        result = fns["verify_and_fix_phase2"](text)
        assert "자동검증 로그" in result
        assert "겹침폭" in result

    def test_genuine_overlap_is_not_flagged(self, fns):
        """대조군(false-positive 방지): 실제로 겹치는 구간(겹침폭 > 0)은
        경고 없이 원문 그대로 유지돼야 한다."""
        text = (
            "세력 매집 수급 벽 [OB](59.7 ~ 61.5 KRW)과 가격 공백대 [FVG]"
            "(61.0 ~ 70.8 KRW)가 상호 중첩되어 있습니다."
        )
        result = fns["verify_and_fix_phase2"](text)
        # 근거원장 부재 경고는 이 단문 조각에 대한 정상 동작이므로, OB/FVG 판정 대상
        # 본문만 분리해 비교한다(겹침 오탐 여부가 이 테스트의 검증 대상).
        briefing = result.split("[자동검증 로그 — Python 사후검증]", 1)[0].rstrip()
        assert briefing == text, "실제로 겹치는 정상케이스인데 불필요하게 수정/경고됨"
        assert "겹침폭" not in result

    def test_comma_separated_high_krw_numbers(self, fns):
        """[판정37 재현] 고가 KRW 코인(예: BTC급)은 '68,500,000'처럼 천단위 콤마가
        붙는다. 콤마 미대응 시 '68'로 절단매칭되어 잘못된 값으로 오매칭됐던 결함 —
        정확히 파싱되고 겹침여부도 올바르게 계산돼야 한다."""
        text = (
            "세력 매물대 [OB](68,000,000 ~ 68,500,000 KRW)과 가격 공백대 [FVG]"
            "(69,000,000 ~ 70,000,000 KRW)가 상호 중첩되어 있습니다."
        )
        result = fns["verify_and_fix_phase2"](text)
        assert "자동검증 로그" in result
        assert "겹침폭=-500000.00" in result, "콤마 절단으로 잘못된 겹침폭이 계산됨"

    def test_second_overlap_claim_is_also_caught(self, fns):
        """[판정37 재현] 4️⃣개별TF분석은 TF마다 OB-FVG 중첩을 각각 서술할 수 있다.
        첫 번째(4h)가 진짜 중첩(정상)이고 두 번째(1h)가 거짓 중첩(결함)인 경우 —
        re.search(단일매칭)였다면 첫 매치만 보고 놓쳤을 시나리오. finditer는
        두 번째도 반드시 잡아내야 한다."""
        text = (
            "4h 분석: 세력 매물대 [OB](61.0 ~ 63.0 KRW)과 가격 공백대 [FVG]"
            "(62.0 ~ 65.0 KRW)가 상호 중첩되어 있습니다. "
            "1h 분석: 세력 매물대 [OB](59.7 ~ 60.6 KRW)과 가격 공백대 [FVG]"
            "(60.6 ~ 70.8 KRW)가 상호 중첩되어 있습니다."
        )
        result = fns["verify_and_fix_phase2"](text)
        assert "59.7" in result and "60.6" in result, "두 번째(1h) 거짓주장을 놓침"
        assert result.count("검증실패") == 1, "정상인 첫 번째(4h)까지 잘못 플래그됨"

    def test_no_match_returns_original_unchanged(self, fns):
        """엣지케이스(안전 폴백): OB/FVG 언급 자체가 없으면 원문을 그대로
        반환해야 한다(파싱 실패 시 함부로 건드리지 않는다는 설계원칙)."""
        text = "이번 분석에는 OB나 FVG 언급이 전혀 없습니다."
        result = fns["verify_and_fix_phase2"](text)
        assert result.split("[자동검증 로그 — Python 사후검증]", 1)[0].rstrip() == text
        assert "겹침폭" not in result


def test_production_min_bars_standard_is_40():
    """[판정47번 회귀가드] FC_MIN_BARS_STANDARD(20→40, 사용자 승인)가 이후 무심코
    되돌아가지 않도록 실제 main.py의 값을 직접 읽어 확인한다. 아래 range_expansion_fn
    fixture는 테스트 격리를 위해 이 값과 독립적으로 20을 주입하므로(주석 참조),
    이 테스트가 유일하게 실제 프로덕션 값을 검증한다."""
    src = _find_main_py().read_text(encoding="utf-8")
    line = next(l for l in src.splitlines() if l.strip().startswith("FC_MIN_BARS_STANDARD ="))
    ns = {}
    exec(line, ns)
    assert ns["FC_MIN_BARS_STANDARD"] == 40


def _load_range_expansion_fn():
    src = _find_main_py().read_text(encoding="utf-8")
    start = src.index("FC_MIN_BARS_STANDARD")
    # 상수(FC_MIN_BARS_STANDARD 등)까지 포함해서 함수와 함께 로드해야 하므로,
    # 함수 정의부만 별도로 잘라 상수는 하드코딩 목값으로 주입한다(테스트 격리).
    fn_start = src.index("def fc_compute_range_expansion")
    fn_end = src.index("\n\n\n", fn_start)
    ns = {
        "FC_MIN_BARS_STANDARD": 20,
        "FC_RANGE_EXPANSION_MAX": 2.0,
        "FC_VOL_SQUEEZE_RATIO_LOW": 0.40,
        "FC_VOL_SQUEEZE_RATIO_HIGH": 0.60,
    }
    exec(src[fn_start:fn_end], ns)
    return ns["fc_compute_range_expansion"]


@pytest.fixture(scope="module")
def range_expansion_fn():
    return _load_range_expansion_fn()


# ============================================================
# fc_compute_range_expansion 검증 (Cowork FindCoin 재설계, 판정45·46번)
# 근거: 경로A/B 공통 진입타이밍 게이트 — 가장 최근 완성봉의 TR이 ATR_STD([판정47번]
# 구 'ATR_20'에서 개명) 대비 과도하게 확장됐는지(추격매수 리스크) 판정. compression_pct와
# 독립 계산이라 경로A(이미돌파) 충돌 위험이 없는지가 핵심 검증 포인트.
# ============================================================
class TestFcComputeRangeExpansion:
    def _make_daily(self, n_calm, last_completed_range_high_low):
        """n_calm개의 평온한 완성봉 + 지정레인지의 '가장 최근 완성봉'(daily[-2] 위치,
        실제 검사대상) + 평온한 당일 진행봉 1개(daily[-1], 함수가 항상 제외하는 자리).
        OHLCV 인덱스: [ts,o,h,l,c,v]"""
        daily = [[i, 100.0, 100.5, 99.5, 100.0, 1000] for i in range(n_calm)]
        h, l = last_completed_range_high_low
        daily.append([n_calm, 100.0, h, l, (h + l) / 2, 1000])  # 가장 최근 완성봉(검사대상)
        daily.append([n_calm + 1, 100.0, 100.5, 99.5, 100.0, 1000])  # 당일 진행봉(항상 제외)
        return daily

    def test_calm_market_passes(self, range_expansion_fn):
        """평온한 시장(최근완성봉도 평소와 비슷한 레인지)은 통과해야 한다."""
        daily = self._make_daily(25, (100.5, 99.5))
        item = {"daily_ohlcv_cache": daily}
        result = range_expansion_fn(item)
        assert result["range_expansion_ok"] is True

    def test_sudden_expansion_fails(self, range_expansion_fn):
        """[판정45재현] 평소 대비 급격히 확장된 최근완성봉(레인지 15 vs 평소 1)은
        탈락(range_expansion_ok=False)해야 한다."""
        daily = self._make_daily(25, (115.0, 100.0))
        item = {"daily_ohlcv_cache": daily}
        result = range_expansion_fn(item)
        assert result["range_expansion_ok"] is False
        assert result["range_expansion_ratio"] >= 2.0

    def test_insufficient_data_safe_fallback(self, range_expansion_fn):
        """[안전폴백] 데이터 부족(3봉 미만)시 탈락시키지 않고 통과 처리돼야 한다."""
        item = {"daily_ohlcv_cache": [[0, 100, 101, 99, 100, 1000], [1, 100, 101, 99, 100, 1000]]}
        result = range_expansion_fn(item)
        assert result["range_expansion_ok"] is True

    def test_no_cache_safe_fallback(self, range_expansion_fn):
        """엣지케이스: daily_ohlcv_cache 자체가 없어도 예외 없이 안전 통과해야 한다."""
        item = {}
        result = range_expansion_fn(item)
        assert result["range_expansion_ok"] is True

    def test_atr_std_key_renamed_from_atr_20(self, range_expansion_fn):
        """[판정47번 명칭전환] 결과 dict가 신규 키 'atr_std'를 쓰고, 구 키 'atr_20'은
        더 이상 생성하지 않아야 한다(잔존 시 하위 소비자가 혼선)."""
        daily = self._make_daily(25, (100.5, 99.5))
        item = {"daily_ohlcv_cache": daily}
        result = range_expansion_fn(item)
        assert "atr_std" in result
        assert "atr_20" not in result

    def test_atr_std_n_reflects_actual_reduced_window(self, range_expansion_fn):
        """[판정48번 신설, 결함3 재발방지] 축소모드(확보봉수 < FC_MIN_BARS_STANDARD)에서
        atr_std_n은 상수(테스트격리 20)가 아니라 실제 사용된 봉수를 정확히 반영해야 한다.
        payload 라벨이 실제보다 안정적인 것처럼 고정 표기되던 결함의 재발을 막는다."""
        # completed=9봉(< 테스트격리 FC_MIN_BARS_STANDARD=20)뿐인 축소모드 상황 재현
        daily = self._make_daily(8, (100.5, 99.5))  # completed=9(calm8+검사대상1), 진행봉1
        item = {"daily_ohlcv_cache": daily}
        result = range_expansion_fn(item)
        assert result["atr_std_n"] == 9
        assert result["atr_std_n"] != 20  # 상수(표준값)와 달라야 함 — 축소모드이므로

    def test_atr_std_n_equals_standard_when_full_window(self, range_expansion_fn):
        """표준모드(확보봉수 ≥ FC_MIN_BARS_STANDARD)에서는 atr_std_n이 정확히 상수(테스트
        격리 20)와 같아야 한다."""
        daily = self._make_daily(25, (100.5, 99.5))  # completed=26 ≥ 20
        item = {"daily_ohlcv_cache": daily}
        result = range_expansion_fn(item)
        assert result["atr_std_n"] == 20

    def test_vol_squeeze_tight_gives_full_score(self, range_expansion_fn):
        """[판정47번 신설] 최근 3봉의 평균 Bar_Range가 ATR_STD 대비 매우 좁으면
        (ratio≤VOL_SQUEEZE_RATIO_LOW=0.40) 완전압축으로 S_vol_squeeze=1.0이어야 한다."""
        calm = [[i, 100.0, 100.5, 99.5, 100.0, 1000] for i in range(17)]
        tight3 = [[17 + i, 100.0, 100.05, 99.95, 100.0, 1000] for i in range(3)]
        today = [[20, 100.0, 100.5, 99.5, 100.0, 1000]]
        item = {"daily_ohlcv_cache": calm + tight3 + today}
        result = range_expansion_fn(item)
        assert result["vol_squeeze_ratio"] <= 0.40
        assert result["s_vol_squeeze"] == 1.0

    def test_vol_squeeze_expanded_gives_zero_score(self, range_expansion_fn):
        """최근 3봉이 ATR_STD 대비 매우 넓으면(ratio≥VOL_SQUEEZE_RATIO_HIGH=0.60)
        비압축으로 S_vol_squeeze=0.0이어야 한다."""
        calm = [[i, 100.0, 100.5, 99.5, 100.0, 1000] for i in range(17)]
        wide3 = [[17 + i, 100.0, 105.0, 95.0, 100.0, 1000] for i in range(3)]
        today = [[20, 100.0, 100.5, 99.5, 100.0, 1000]]
        item = {"daily_ohlcv_cache": calm + wide3 + today}
        result = range_expansion_fn(item)
        assert result["vol_squeeze_ratio"] >= 0.60
        assert result["s_vol_squeeze"] == 0.0

    def test_vol_squeeze_interior_is_linear(self, range_expansion_fn):
        """0.40 < ratio < 0.60 구간은 스펙 공식 그대로 선형 보간이어야 한다:
        (HIGH-ratio)/(HIGH-LOW)."""
        calm = [[i, 100.0, 100.5, 99.5, 100.0, 1000] for i in range(17)]
        mid3 = [[17 + i, 100.0, 100.23, 99.77, 100.0, 1000] for i in range(3)]
        today = [[20, 100.0, 100.5, 99.5, 100.0, 1000]]
        item = {"daily_ohlcv_cache": calm + mid3 + today}
        result = range_expansion_fn(item)
        ratio = result["vol_squeeze_ratio"]
        assert 0.40 < ratio < 0.60, f"테스트 설계 전제 위반: ratio={ratio}가 구간 밖"
        expected = (0.60 - ratio) / (0.60 - 0.40)
        assert result["s_vol_squeeze"] == pytest.approx(expected)

    def test_vol_squeeze_insufficient_bars_returns_none(self, range_expansion_fn):
        """[안전폴백] 완성봉이 2개뿐이면(3봉 미만) range_expansion_ok는 그 자체 임계(2봉)로
        정상 계산되더라도, S_vol_squeeze는 억지로 만들지 않고 None(미산출)이어야 한다
        (두 지표의 데이터충분성 임계가 서로 다름을 검증)."""
        daily = [
            [0, 100, 101, 99, 100, 1000],
            [1, 100, 101, 99, 100, 1000],
            [2, 100, 101, 99, 100, 1000],  # 당일 진행봉(제외) — completed=2봉뿐
        ]
        item = {"daily_ohlcv_cache": daily}
        result = range_expansion_fn(item)
        assert result["s_vol_squeeze"] is None
        assert "range_expansion_ok" in result



def _load_range_ok_fn(hard_gate_value):
    """[판정50번] _range_ok는 run_findcoin_scan 내부 nested function이라 기존 top-level
    함수 추출방식으로는 커버 안 됨 — 정확한 원문(들여쓰기 포함)을 직접 잘라 dedent 후 실행.
    HARD_GATE가 이제(True) 실효를 가지므로 이 판정로직 자체의 직접 테스트가 필요해짐."""
    src = _find_main_py().read_text(encoding="utf-8")
    start = src.index("def _range_ok(it):")
    end = src.index("\n\n", start)
    fn_src = "\n".join(line[4:] if line.startswith("    ") else line
                        for line in src[start:end].splitlines())
    ns = {"FC_RANGE_EXPANSION_HARD_GATE": hard_gate_value}
    exec(fn_src, ns)
    return ns["_range_ok"]


class TestRangeOkGateLogic:
    """[판정50번 신설] FC_RANGE_EXPANSION_HARD_GATE=True 전환으로 이 판정로직이 처음으로
    실제 필터링 효과를 가지게 됨 — 전환 전에는 커버리지가 없던 지점."""

    def test_hard_gate_true_rejects_violator(self):
        """HARD_GATE=True일 때 range_expansion_ok=False(위반)인 종목은 탈락해야 한다."""
        fn = _load_range_ok_fn(True)
        assert fn({"range_expansion_ok": False}) is False

    def test_hard_gate_true_passes_compliant(self):
        """HARD_GATE=True일 때 range_expansion_ok=True(정상/안전폴백 포함)인 종목은 통과해야 한다."""
        fn = _load_range_ok_fn(True)
        assert fn({"range_expansion_ok": True}) is True

    def test_hard_gate_false_passes_everything(self):
        """[구 관측모드 회귀가드] HARD_GATE=False로 되돌릴 경우, 위반종목도 항상 통과해야
        한다(관측모드 시절 동작과 동일해야 함 — 스위치 자체의 의미가 깨지지 않았는지 확인)."""
        fn = _load_range_ok_fn(False)
        assert fn({"range_expansion_ok": False}) is True

    def test_production_hard_gate_is_true(self):
        """[판정50번 회귀가드] FC_RANGE_EXPANSION_HARD_GATE가 의도치 않게 False로
        되돌아가지 않도록 실제 main.py 값을 직접 확인한다."""
        src = _find_main_py().read_text(encoding="utf-8")
        line = next(l for l in src.splitlines()
                    if l.strip().startswith("FC_RANGE_EXPANSION_HARD_GATE ="))
        ns = {}
        exec(line, ns)
        assert ns["FC_RANGE_EXPANSION_HARD_GATE"] is True


def _load_box_range_fn():
    """[판정52번] fc_compute_box_range 격리 로드 — range_expansion_fn과 동일 패턴(테스트 격리를
    위해 상수는 하드코딩 값 주입, 실제 main.py 값과 독립)."""
    src = _find_main_py().read_text(encoding="utf-8")
    fn_start = src.index("def fc_compute_box_range")
    fn_end = src.index("\n\n\n", fn_start)
    ns = {
        "FC_MIN_BARS_STANDARD": 20,
        "FC_COMPRESSION_RANGE_MAX": 5.0,
        "FC_BOX_TOUCH_BAND_RATIO": 0.20,
        "FC_BOX_MIN_TOUCHES": 2,
    }
    exec(src[fn_start:fn_end], ns)
    return ns["fc_compute_box_range"]


@pytest.fixture(scope="module")
def box_range_fn():
    return _load_box_range_fn()


# ============================================================
# fc_compute_box_range 검증 (판정52번, 외부검토 3차 왕복 후 통합설계)
# 핵심 검증축: (1)극단압축(State1 이상적 타깃)에서도 확정 성공하는지(1차 외부안의 치명적
# 결함 재발방지) (2)순수 추세(왕복 없는 일방향 이동)는 오탐 없이 배제하는지(2차 수정안의
# "compression만 통과하면 자동확정" 결함 재발방지)
# ============================================================
class TestFcComputeBoxRange:

    def test_insufficient_data_safe_fallback(self, box_range_fn):
        item = {"daily_ohlcv_cache": [[0, 100, 101, 99, 100, 1000]]}
        result = box_range_fn(item)
        assert result["s_boxrange"] is None
        assert result["box_status"] == "insufficient_data"

    def test_no_cache_safe_fallback(self, box_range_fn):
        result = box_range_fn({})
        assert result["s_boxrange"] is None
        assert result["box_status"] == "insufficient_data"

    def test_already_broken_excluded(self, box_range_fn):
        """당일 진행봉 종가가 박스 상단을 이미 넘었으면 s_boxrange=0.0, 재계산 없이 즉시 배제."""
        box = [[i, 100.0, 100.5, 99.5, 100.0, 1000] for i in range(20)]
        today = [[20, 100.0, 106.0, 105.0, 105.5, 1000]]  # 박스상단(100.5) 훨씬 초과
        item = {"daily_ohlcv_cache": box + today}
        result = box_range_fn(item)
        assert result["s_boxrange"] == 0.0
        assert result["box_status"] == "already_broken"

    def test_extreme_flat_still_confirms(self, box_range_fn):
        """[핵심 회귀가드 — 1차 외부안 결함1 재발방지] 왕복폭이 매우 좁아 거의 평탄한
        데이터라도, 상/하단을 각각 반복 접촉했다면 반드시 확정돼야 한다. 스윙탐지 방식은
        이런 케이스에서 스윙이 1개로 뭉개져 실패했었음(시뮬레이션으로 확인된 실제 버그)."""
        bars = []
        for i in range(20):
            if i % 2 == 0:
                bars.append([i, 100.0, 100.5, 100.0, 100.2, 1000])  # 상단 근접
            else:
                bars.append([i, 99.0, 99.0, 99.0, 99.0, 1000])       # 하단 근접(완전평탄)
        today = [[20, 99.5, 99.7, 99.4, 99.5, 1000]]
        item = {"daily_ohlcv_cache": bars + today}
        result = box_range_fn(item)
        assert result["box_status"] == "confirmed"
        assert result["box_upper_touches"] >= 2
        assert result["box_lower_touches"] >= 2
        assert result["s_boxrange"] > 0.0

    def test_perfectly_constant_data_no_crash(self, box_range_fn):
        """완전히 동일한 값만 반복되는 극단 케이스(레인지=0)도 크래시 없이 안전 처리돼야 한다."""
        bars = [[i, 100.0, 100.0, 100.0, 100.0, 1000] for i in range(20)]
        today = [[20, 100.0, 100.0, 100.0, 100.0, 1000]]
        item = {"daily_ohlcv_cache": bars + today}
        result = box_range_fn(item)
        assert result["box_status"] in ("confirmed", "not_confirmed")  # 크래시만 안 나면 됨
        assert result["s_boxrange"] is not None

    def test_pure_uptrend_does_not_falsely_confirm(self, box_range_fn):
        """[핵심 회귀가드 — 2차 외부수정안 결함 재발방지] 왕복 없이 한 방향으로만 흐르는
        추세는, 전체 레인지가 압축조건(5%)을 우연히 만족하더라도 왕복접촉 부족으로
        확정되면 안 된다 — "compression만 통과하면 자동확정"이던 결함의 재발 여부 확인."""
        n = 20
        highs = [100.0 + i * 0.15 for i in range(n)]  # 100.0 → 102.85, 단조증가(약 2.8% 상승)
        lows = [h - 0.3 for h in highs]
        bars = [[i, lows[i], highs[i], lows[i], (highs[i] + lows[i]) / 2, 1000] for i in range(n)]
        today = [[n, highs[-1], highs[-1] + 0.1, highs[-1] - 0.1, highs[-1], 1000]]
        item = {"daily_ohlcv_cache": bars + today}
        result = box_range_fn(item)
        # 단조추세는 하단(초반 1~2봉)/상단(후반 1~2봉)을 각 1회 정도만 스치므로 not_confirmed 기대
        assert result["box_status"] == "not_confirmed"
        assert result["s_boxrange"] == 0.0

    def test_touch_boundary_exactly_min_touches(self, box_range_fn):
        """상/하단 접촉이 정확히 FC_BOX_MIN_TOUCHES(2)회(분리된 두 번의 방문)면 경계값이므로
        확정돼야 한다(< 미만만 배제, >= 이상은 포함). 에피소드 카운트 방식이므로 연속된
        봉은 1회로 묶이는 것과 구분하기 위해 두 접촉을 서로 떨어뜨려 배치한다."""
        bars = []
        for i in range(20):
            if i in (0, 10):
                bars.append([i, 100.0, 100.5, 100.0, 100.2, 1000])  # 상단접촉(서로 분리된 2회)
            elif i in (5, 19):
                bars.append([i, 99.0, 99.2, 99.0, 99.1, 1000])       # 하단접촉(서로 분리된 2회)
            else:
                bars.append([i, 99.7, 99.9, 99.6, 99.75, 1000])      # 중간대(밴드 밖)
        today = [[20, 99.7, 99.8, 99.6, 99.7, 1000]]
        item = {"daily_ohlcv_cache": bars + today}
        result = box_range_fn(item)
        assert result["box_upper_touches"] == 2
        assert result["box_lower_touches"] == 2
        assert result["box_status"] == "confirmed"

    def test_production_uses_high_based_denominator(self):
        """[구문-의미 일치성 확인] box_range_pct는 compression_pct와 동일하게 고가를
        분모로 써야 COMPRESSION_RANGE_MAX(고가기준 정의)와 같은 척도로 비교 가능하다 —
        실제 main.py 소스에 'box_high' 나눗셈이 존재하는지 정적 확인(mid 기준 회귀 방지)."""
        src = _find_main_py().read_text(encoding="utf-8")
        fn_start = src.index("def fc_compute_box_range")
        fn_end = src.index("\n\n\n", fn_start)
        fn_src = src[fn_start:fn_end]
        assert "/ box_high * 100.0" in fn_src
        assert "box_mid" not in fn_src  # 1차 외부안의 mid기준 설계가 재유입되지 않았는지 확인


def _load_error_message_fn():
    """friendly_error_message는 ccxt 예외타입에 의존하므로 별도 로드."""
    import ccxt
    src = _find_main_py().read_text(encoding="utf-8")
    start = src.index("def friendly_error_message")
    end = src.index("\n\n\n", start)
    ns = {"ccxt": ccxt}
    exec(src[start:end], ns)
    return ns["friendly_error_message"], ccxt


@pytest.fixture(scope="module")
def error_fn():
    return _load_error_message_fn()


# ============================================================
# friendly_error_message 검증
# 근거: Context7로 ccxt 공식문서 확인 결과 "에러메시지 문자열 파싱은 비권장,
# 구조화된 예외타입(isinstance)으로 판별하라"는 공식 권고를 따르지 않고 있던 결함.
# ccxt 타입 판별을 1순위로 추가하고 기존 문자열매칭은 안전망(2순위)으로 유지.
# ============================================================
class TestFriendlyErrorMessage:
    def test_ccxt_request_timeout_type(self, error_fn):
        fn, ccxt = error_fn
        msg = fn(ccxt.RequestTimeout("timed out"), "업비트", "BTC")
        assert "지연" in msg

    def test_ccxt_not_supported_type(self, error_fn):
        """[근본원인 재현] '지원하지 않는 시간대' 판정이 ccxt.NotSupported 타입으로
        정확히 잡혀야 한다(과거엔 문자열매칭만 있어 타입정보를 활용 못했음)."""
        fn, ccxt = error_fn
        msg = fn(ccxt.NotSupported("not supported timeframe"), "업비트", "BTC")
        assert "시간대" in msg

    def test_ccxt_rate_limit_type(self, error_fn):
        fn, ccxt = error_fn
        msg = fn(ccxt.RateLimitExceeded("too many"), "업비트", "BTC")
        assert "많습니다" in msg

    def test_ccxt_network_error_type(self, error_fn):
        fn, ccxt = error_fn
        msg = fn(ccxt.NetworkError("conn refused"), "업비트", "BTC")
        assert "연결" in msg

    def test_non_ccxt_exception_falls_back_to_string_matching(self, error_fn):
        """회귀방지: ccxt타입이 아닌 일반 예외는 기존 문자열매칭 안전망으로
        여전히 정상 처리돼야 한다(1순위 계층 추가가 기존 동작을 깨지 않는지 확인)."""
        fn, ccxt = error_fn
        msg = fn(Exception("Connection timed out"), "업비트", "BTC")
        assert "지연" in msg

    def test_unrelated_ccxt_type_safely_falls_back_to_default(self, error_fn):
        """엣지케이스: 1순위 4개 타입에 해당 안 되는 다른 ccxt예외(예: BadSymbol)는
        문자열매칭에도 안 걸리면 안전하게 기본 메시지로 폴백돼야 한다(예외 없이)."""
        fn, ccxt = error_fn
        msg = fn(ccxt.BadSymbol("no market found"), "업비트", "BTC")
        assert "문제가 발생했습니다" in msg

    def test_or_and_precedence_no_false_positive(self, error_fn):
        """[판정43재현] 문자열매칭 안전망에서 'timeframe' 단독(에러성격과 무관하게)
        만으로 오탐되지 않고 'not a valid'가 동반돼야 매칭돼야 한다."""
        fn, ccxt = error_fn
        msg = fn(Exception("timeframe data temporarily unavailable"), "업비트", "BTC")
        assert "시간대" not in msg


# ============================================================
# PHASE2 시나리오 경로 서식 정합성 검증 (판정53번)
# [테스트전략 — engineering:testing-strategy 스킬 적용] 이번 변경은 Python 로직이
# 아니라 Gemini용 고정출력서식(순수 텍스트) 변경이라, LLM의 실제 해석·출력은
# 단위테스트로 검증할 수 없음(고유 한계, 스킬 트레이드오프 명시). 대신 "계약
# 테스트(contract test)" 관점으로 스펙 텍스트 자체의 내부 일관성을 테스트 대상으로
# 삼는다: (1)중복 서브섹션이 실제로 제거됐는지 (2)신규 서식에 FVG/RoleReversal이
# 14대 매핑표 정확 표기로 들어갔는지 (3)교차참조 서술이 실제 섹션 내용과 어긋나지
# 않는지 (4)main.py가 이 텍스트를 파싱하는 코드가 없어 회귀 리스크가 낮음을 고정.
# ============================================================
class TestPhase2ScenarioPathFormat:

    def test_duplicate_subsections_removed(self):
        """[판정53번] 종전 2️⃣ 섹션의 중복 서브섹션(1.리스크및하방깊이평가/
        2.기회판단및상방열린구간/3.최종실행전략수치화)이 실제로 제거됐는지 확인 —
        메인/대체시나리오와 동일정보를 다른 이름으로 반복하던 부분."""
        text = _find_spec_file().read_text(encoding="utf-8")
        assert "1. 리스크 및 하방 깊이 평가" not in text
        assert "2. 기회 판단 및 상방 열린 구간" not in text
        assert "3. 최종 실행 전략 수치화" not in text

    def test_main_scenario_contains_fvg_with_correct_terminology(self):
        """[판정53번] FVG가 메인 시나리오(상방 경로)에 14대 매핑표 정확 표기
        ("비어있는 매물 공백대 [FVG]")로 들어갔는지 확인 — 축약형("FVG(매물공백대)")
        같은 비표준 표기가 남지 않았는지가 핵심."""
        text = _find_spec_file().read_text(encoding="utf-8")
        main_start = text.index("📈 메인 시나리오 파동 경로")
        main_end = text.index("📉 대체 시나리오 파동 경로")
        main_section = text[main_start:main_end]
        assert "비어있는 매물 공백대 [FVG]" in main_section
        assert "FVG(매물공백대)" not in main_section  # 이전 축약형 잔존 방지

    def test_alt_scenario_contains_role_reversal_with_correct_terminology(self):
        """[판정53번] Role Reversal이 대체 시나리오(하방 경로)에 14대 매핑표 정확
        표기("지지·저항 역할 전환선 [Role Reversal]")로 들어갔는지 확인."""
        text = _find_spec_file().read_text(encoding="utf-8")
        alt_start = text.index("📉 대체 시나리오 파동 경로")
        alt_end = text.index("⏰️ 예상 소요기간")
        alt_section = text[alt_start:alt_end]
        assert "지지·저항 역할 전환선 [Role Reversal]" in alt_section

    def test_entry_price_labeled_inline_not_dropped(self):
        """[판정53번] 진입예상가를 별도 라인 삭제가 아니라 '1차 근거리 지지/저항'
        라인에 인라인 병기(=진입예상가)했는지 확인 — 정보 삭제가 아니라 표기 통합."""
        text = _find_spec_file().read_text(encoding="utf-8")
        assert "1차 근거리 지지/저항(=진입예상가)" in text

    def test_cross_reference_matches_actual_section_content(self):
        """[판정53번, 360도 파급 확인] PHASE2 섹션 목록을 설명하는 교차참조 문장이
        실제 2️⃣ 섹션 내용(RSI및다이버전스검증)과 일치하는지 확인 — 섹션 내용만 바꾸고
        이걸 설명하는 별도 문장을 안 바꾸면 문서 자체가 자기모순에 빠짐(이번 세션에서
        반복 경계해온 SSOT 비일관 패턴과 동일 유형)."""
        text = _find_spec_file().read_text(encoding="utf-8")
        assert "2️⃣ RSI 및 다이버전스" in text
        assert "2️⃣ 리스크/기회" not in text  # 갱신 전 구 표현 잔존 방지

    def test_no_python_code_parses_removed_format(self):
        """[판정53번 갱신] 제거된 구 서식은 여전히 코드에 없어야 하며,
        코드가 참조하는 서식 앵커는 명세에 실재해야 한다(양방향 SSOT 계약) — 사전검증 시 grep으로 확인한
        내용을 테스트로도 고정해 향후 재작업 시 자동 검증되게 한다."""
        main_src = _find_main_py().read_text(encoding="utf-8")
        spec_src = _find_spec_file().read_text(encoding="utf-8")
        assert "1. 리스크 및 하방 깊이 평가" not in main_src
        # 표시 결속 계층 도입으로 코드가 경로 서식을 참조하게 됐으므로, 계약을 뒤집어
        # 고정한다 — 코드가 참조하는 서식 앵커는 반드시 명세에 실재해야 한다.
        # 명세 서식만 바꾸고 코드를 안 고치면 이 테스트가 즉시 잡는다.
        for anchor in ("메인 시나리오 파동 경로", "대체 시나리오 파동 경로", "타임 프레임 분석", "분석 대상"):
            assert anchor in main_src, f"코드가 참조하던 앵커가 사라짐: {anchor}"
            assert anchor in spec_src, f"코드가 명세에 없는 서식 앵커를 참조함: {anchor}"


# ============================================================
# [표시 결속·경로 렌더링 회귀] 정합성 검토에서 실측 확인된 결함 8건 고정
# 각 테스트는 "무엇이 어떻게 틀렸었는지"를 재현 가능한 형태로 남긴다.
# ============================================================
_LEDGER_UP = ("[INTERNAL_EVIDENCE_LEDGER]\n결론: 상방\n등급: 강\n등급보정: 없음\n"
              "축점수: S_1=1.5, S_2=1.0, S_3=0.5, S_4=-0.5\n순합방향: 상방\n"
              "가격경로: P_entry=3.80 | P_inv=3.55 | P_target_1=4.05 | P_target_2=4.40\n"
              "최종신뢰도점수: 4.7\n지지축: S_1\n반대축: S_4\n상충축: 없음\n중립축: 없음\n"
              "축내상쇄: 없음\n진행국면: 지속\nBundle:\n"
              "- 1d|최근20봉|BOS|1d:구조 돌파|상방|결정|S_1\n[/INTERNAL_EVIDENCE_LEDGER]")

_NORMAL_BODY = (
    "**분석 대상**\n\n비트코인 업비트 기준 (현재가: 68,500,000 KRW)\n\n"
    "**방향성 평가**\n\n상방 우세 (우세 등급: 강)\n\n"
    "**📈 메인 시나리오 파동 경로**\n(확률 (수치)% — 참고용, 백테스트 검증치 아님)\n\n"
    "➔ 현재가 (수치)\n➔ 1차 근거리 지지/저항(=진입예상가) (수치)\n"
    "➔ 비어있는 매물 공백대 [FVG] 3.93 ~ 3.96 KRW\n➔ 1차 목표가 (수치)\n➔ 2차 목표가 (수치)\n\n"
    "**📉 대체 시나리오 파동 경로**\n(확률 (수치)% — 참고용, 백테스트 검증치 아님)\n\n"
    "➔ 현재가 (수치)\n➔ 지지·저항 역할 전환선 [Role Reversal] 3.41 KRW\n"
    "➔ 무효화 손절선 (수치) 이탈\n➔ 다음 매물대 3.20 KRW\n\n"
    "3️⃣ 힘의 방향성\n\n주도 역학 F2가 1차 목표가 4.05 방향과 일치합니다.\n")


def _origin_registry(fns):
    return fns["build_phase1_origin_fact_registry"](
        {"1h": {"quality_status": "적격", "current_price": 68500000.0,
                "current_candle": {"high": 68900000.0, "low": 68200000.0, "midpoint": 68550000.0}}},
        "KRW")


def _contract(fns, ledger=_LEDGER_UP):
    contract, warnings = fns["build_phase2_display_contract"](ledger)
    return contract, warnings


class TestPathSectionBoundary:
    """[C2] 대체 경로 섹션의 종료 앵커(예상 소요기간/2️⃣)가 없으면 경로 본문이 문서
    끝까지 확장돼 3️⃣ 이후 정상 서술이 조용히 삭제되고도 검증 태그가 붙었다."""

    def test_body_line_accepts_only_arrow_and_parenthetical(self, fns):
        assert fns["_is_path_body_line"]("➔ 현재가 3.95 KRW")
        assert fns["_is_path_body_line"]("(확률 60%)")
        assert fns["_is_path_body_line"]("")
        # 일반 불릿을 경로로 인정하면 인접 문단이 흡수돼 삭제된다
        assert not fns["_is_path_body_line"]("- 리테스트 실패 시 재확인")
        assert not fns["_is_path_body_line"]("3️⃣ 힘의 방향성")

    def test_missing_end_anchor_never_deletes_following_sections(self, fns):
        contract, _ = _contract(fns)
        out, warnings = fns["render_verified_phase2_decision_blocks"](
            _NORMAL_BODY, contract, "KRW", all_validation_warnings=[], display_warnings=[],
            phase1_fact_registry=_origin_registry(fns))
        assert "주도 역학 F2가 1차 목표가 4.05 방향과 일치합니다." in out, "본문이 삭제됨"
        assert "결정값 경로 섹션 누락" not in warnings

    def test_bullet_paragraph_after_path_is_not_absorbed(self, fns):
        """[신규발견] 일반 불릿을 경로 항목으로 인정하면 경로 섹션 뒤의 불릿 문단이
        본문으로 흡수돼, 목표·수치를 담은 줄이 경고 없이 삭제된다."""
        body = ("**📈 메인 시나리오 파동 경로**\n(확률 (수치)%)\n\n➔ 현재가 (수치)\n\n"
                "**📉 대체 시나리오 파동 경로**\n(확률 (수치)%)\n\n➔ 다음 매물대 3.20 KRW\n\n"
                "- 리테스트 실패 시 1차 목표가 4.05 재확인 필요\n- 거래량 동반 여부 관찰\n")
        contract, _ = _contract(fns)
        out, _ = fns["render_verified_phase2_decision_blocks"](
            body, contract, "KRW", all_validation_warnings=[], display_warnings=[])
        assert "리테스트 실패 시 1차 목표가 4.05 재확인 필요" in out, "불릿 문단이 삭제됨"
        assert "거래량 동반 여부 관찰" in out

    def test_missing_path_header_appends_without_editing_body(self, fns):
        """[C2 폴백] 경로 헤더 자체가 없으면 본문을 고치지 않고 카드만 덧붙이며,
        이 경우에만 경고를 남겨 정격 태그를 보류한다."""
        body = "1️⃣ 메인 시나리오\n\n상방 우세. 1차 목표가 4.05 KRW 부근입니다.\n"
        contract, _ = _contract(fns)
        out, warnings = fns["render_verified_phase2_decision_blocks"](
            body, contract, "KRW", all_validation_warnings=[], display_warnings=[])
        assert "상방 우세. 1차 목표가 4.05 KRW 부근입니다." in out, "본문이 삭제됨"
        assert "결정값 경로 섹션 누락" in warnings
        assert "검증된 진입 예상가 (3.8 KRW)" in out
        assert "시스템 무결성 검증 완료" not in out, "보류 상태인데 정격 태그가 붙음"


class TestSidewaysIsNormal:
    """[C3] 명세 9장 ④·11장이 정상 결론으로 규정한 횡보가 `결정값 검증보류`로
    표시되고 정격 태그까지 억제됐다."""

    def _sideways_ledger(self):
        return (_LEDGER_UP.replace("결론: 상방", "결론: 횡보")
                .replace("순합방향: 상방", "순합방향: 횡보")
                .replace("축점수: S_1=1.5, S_2=1.0, S_3=0.5, S_4=-0.5",
                         "축점수: S_1=0.5, S_2=-0.5, S_3=0.3, S_4=-0.3")
                .replace("가격경로: P_entry=3.80 | P_inv=3.55 | P_target_1=4.05 | P_target_2=4.40",
                         "가격경로: 횡보 — 방향성 목표가 미확정"))

    def test_sideways_uses_spec_notice_and_keeps_tag(self, fns):
        contract, contract_warnings = _contract(fns, self._sideways_ledger())
        assert contract["direction"] == "횡보" and not contract_warnings
        out, warnings = fns["render_verified_phase2_decision_blocks"](
            _NORMAL_BODY, contract, "KRW", all_validation_warnings=[],
            display_warnings=list(contract_warnings), phase1_fact_registry=_origin_registry(fns))
        assert fns["SIDEWAYS_PATH_NOTICE"] in out
        assert "검증보류" not in out, "정상 횡보가 검증 실패로 표기됨"
        assert "결정값 표시 보류" not in warnings
        assert "시스템 무결성 검증 완료" in out

    def test_absent_contract_still_holds_the_tag(self, fns):
        """대조군: 계약 자체가 없으면 종전대로 보류·태그 억제."""
        out, warnings = fns["render_verified_phase2_decision_blocks"](
            _NORMAL_BODY, None, "KRW", all_validation_warnings=[], display_warnings=[],
            phase1_fact_registry=_origin_registry(fns))
        assert "결정값 표시 보류" in warnings
        assert "시스템 무결성 검증 완료" not in out


class TestIntegrityTagGate:
    """[⑦⑧] 모델이 지시대로 수치를 쓰지 않아 발생하는 미채움 template 제거와
    현재가 표기 정규화는 설계상 정상인데 표시 경고로 분류돼, 정상 분석에서도
    Layer 5-B 정격 태그가 영구히 출력되지 않았다."""

    def test_normal_output_emits_the_tag(self, fns):
        contract, _ = _contract(fns)
        out, warnings = fns["render_verified_phase2_decision_blocks"](
            _NORMAL_BODY, contract, "KRW", all_validation_warnings=[], display_warnings=[],
            phase1_fact_registry=_origin_registry(fns))
        assert "시스템 무결성 검증 완료" in out, f"정상인데 태그 미출력: {warnings}"
        # 관측 신호 자체는 반환값에 그대로 보존돼야 한다(로그용)
        assert "미채움 경로 template 제거" in warnings
        assert "분석 대상 현재가 source 정규화" in warnings

    def test_current_price_bound_to_origin_close(self, fns):
        contract, _ = _contract(fns)
        out, _ = fns["render_verified_phase2_decision_blocks"](
            _NORMAL_BODY, contract, "KRW", all_validation_warnings=[], display_warnings=[],
            phase1_fact_registry=_origin_registry(fns))
        assert "(현재가: 68500000 KRW)" in out

    def test_real_anomalies_still_block_the_tag(self, fns):
        blocking = ("결정값 경로 섹션 누락", "결정값 표시 보류", "비경로 확률 표기 제거")
        for warning in blocking:
            assert warning not in fns["NON_BLOCKING_DISPLAY_OBSERVATIONS"], warning

    def test_validation_warnings_always_block(self, fns):
        contract, _ = _contract(fns)
        out, _ = fns["render_verified_phase2_decision_blocks"](
            _NORMAL_BODY, contract, "KRW", all_validation_warnings=["P2V02 축점수 불일치"],
            display_warnings=[], phase1_fact_registry=_origin_registry(fns))
        assert "시스템 무결성 검증 완료" not in out


class TestBoldHeadingObservation:
    """[M10] 명세 4️⃣ 제목은 `**▶️ 1d 타임 프레임 분석**`인데 볼드 미대응으로
    TF 완결성 관측이 사실상 동작하지 않았다."""

    def test_bold_heading_is_observed(self, fns):
        thin = "**▶️ 1d 타임 프레임 분석**\n\n> 방향성 판정: 상방\n\n짧음\n"
        assert fns["phase2_briefing_completeness_observations"](thin) == ["BRIEFING_THIN_TF:1d"]

    def test_plain_heading_still_observed(self, fns):
        thin = "▶️ 1d 타임 프레임 분석\n\n> 방향성 판정: 상방\n\n짧음\n"
        assert fns["phase2_briefing_completeness_observations"](thin) == ["BRIEFING_THIN_TF:1d"]

    def test_bold_heading_covers_coinbase_6h(self, fns):
        thin = "**▶️ 6h 타임 프레임 분석**\n\n> 방향성 판정: 상방\n\n짧음\n"
        assert fns["phase2_briefing_completeness_observations"](thin) == ["BRIEFING_THIN_TF:6h"]

    def test_sufficient_body_is_not_flagged(self, fns):
        full = ("**▶️ 1d 타임 프레임 분석**\n\n> 방향성 판정: 상방\n\n"
                "구조 상태는 상승 BOS 확정입니다.\n거래량 배율 1.8배로 수급이 동반됐습니다.\n")
        assert fns["phase2_briefing_completeness_observations"](full) == []


class TestApprovedModelRosterSsot:
    """[H4] 명세 2장은 승인 모델을 두 개로 고정하고 목록 밖 모델의 실행을 금지하는데
    코드에만 3.5-flash가 추가돼 429 3연속 시 실제 호출까지 도달했다."""

    def test_roster_matches_spec_declaration(self, fns):
        spec = _find_spec_file().read_text(encoding="utf-8")
        line = next(l for l in spec.splitlines() if "실행 모델은 수동 승인된" in l)
        declared = set(re.findall(r"`(gemini-[0-9a-z.\-]+)`", line))
        assert declared, "명세에서 승인 모델 선언을 찾지 못함"
        assert {m["model_id"] for m in fns["STATIC_APPROVED_MODELS"]} == declared

    def test_spec_declares_closed_roster(self):
        spec = _find_spec_file().read_text(encoding="utf-8")
        assert "이 목록 밖의 모델은 탐색·등록·실행하지 않는다" in spec


class TestQuotaRetryBudget:
    """[H5] 429 provider RetryInfo 단기 재시도가 max_transport_attempts=1인 PHASE 1에서
    range 루프 소진으로 재호출 없이 종료돼, 규정이 실행되지 않은 채 대기만 소모했다."""

    def test_retry_loop_uses_budget_not_fixed_range(self):
        main_src = _find_main_py().read_text(encoding="utf-8")
        assert "for transport_attempt in range(max_transport_attempts)" not in main_src
        assert "transport_budget = max_transport_attempts" in main_src
        assert "transport_budget += 1" in main_src

    def test_backoff_index_is_zero_based_after_conversion(self):
        """1-based 루프로 바꿨으므로 backoff 인덱스는 -1 보정돼야 한다(off-by-one 방지)."""
        main_src = _find_main_py().read_text(encoding="utf-8")
        assert "_bounded_retry_delay_seconds(transport_attempt)" not in main_src
        assert main_src.count("_bounded_retry_delay_seconds(transport_attempt - 1") >= 4


class TestSpecDisplayContractClauses:
    """코드로 구현한 표시 규칙이 명세에도 반영돼 있는지(SSOT 양방향 확인)."""

    def test_spec_defines_sideways_and_no_deletion_and_nonblocking(self):
        spec = _find_spec_file().read_text(encoding="utf-8")
        assert "[횡보 표기]" in spec and "횡보 — 방향성 목표가 미확정" in spec
        assert "[무삭제 보장]" in spec
        assert "[비차단 관측 구분]" in spec



# ============================================================
# [Python 보완 행 중복 억제 회귀] 실사용 2차 표본(FOLD)에서 같은 문장이 8회 반복돼
# 4️⃣ 유효행의 32%를 차지한 결함을 고정한다.
# ============================================================
_SB = "진행봉 — 구조 돌파 여부 미확정 (현재 진행봉은 확정 판단에 사용하지 않음)"


def _origin_reg(entries):
    registry = {}
    for tf, price, candle in entries:
        for label, value in (("현재가", price), ("현재 진행 봉", candle), ("구조 돌파", _SB)):
            registry[f"{tf}:{label}"] = {"fact_ref": f"{tf}:{label}", "tf": tf, "label": label,
                                          "value": value, "source": "stage0_origin_current_ohlcv"}
    return registry


def _tf_section(tf, direction, paragraph):
    return f"**▶️ {tf} 타임 프레임 분석**\n(요약)\n\n> 방향성 판정: {direction}\n\n{paragraph}\n\n"


class TestBackfillDuplicationSuppression:

    def test_numbers_already_narrated_detects_reworded_fact(self, fns):
        """[원인] 모델은 같은 값을 다른 문장으로 쓴다. 문자열 완전일치로 검사하면
        항상 미서술로 판정돼 모든 TF에 같은 사실이 중복 삽입된다."""
        value = "고가 148.0 KRW / 저가 114.0 KRW / 중심가 131.0 KRW"
        reworded = "진행봉(고가 148.0 KRW, 저가 114.0 KRW, 중심가 131.0 KRW) 기준"
        assert fns["_fact_numbers_already_narrated"](value, reworded)
        assert not fns["_fact_numbers_already_narrated"](value, "다른 수치 999.9 KRW만 있는 문장")

    def test_sufficient_evidence_tf_gets_no_backfill(self, fns):
        registry = _origin_reg([("4h", "130.0 KRW", "고가 136.0 KRW / 저가 126.0 KRW / 중심가 131.0 KRW")])
        body = "\n> 방향성 판정: 상방\n\n첫 근거 문장입니다. 둘째 근거 문장입니다. 셋째 근거 문장입니다.\n"
        units = fns["_phase2_tf_evidence_units"](body)
        assert units >= 2
        assert fns["_phase2_tf_missing_state_facts"]("4h", body, registry, evidence_units=units) == []

    def test_thin_tf_still_gets_backfill(self, fns):
        """대조군: 얇은 서술에는 종전대로 보완이 들어가야 한다."""
        registry = _origin_reg([("1w", "130.0 KRW", "고가 172.0 KRW / 저가 107.0 KRW / 중심가 139.5 KRW")])
        body = "\n> 방향성 판정: 중립\n\n완성봉 0개로 판정 불가.\n"
        facts = fns["_phase2_tf_missing_state_facts"](
            "1w", body, registry, evidence_units=fns["_phase2_tf_evidence_units"](body))
        assert any(f.startswith(fns["BACKFILL_PROGRESS_PREFIX"]) for f in facts)
        assert any(f.startswith("구조 경계:") for f in facts)

    def test_progress_note_attached_once_only(self, fns):
        registry = _origin_reg([
            ("1w", "130.0 KRW", "고가 172.0 KRW / 저가 107.0 KRW / 중심가 139.5 KRW"),
            ("1h", "130.0 KRW", "고가 133.0 KRW / 저가 130.0 KRW / 중심가 131.5 KRW")])
        body = ("4️⃣ 개별 TF 차트분석\n\n"
                + _tf_section("1w", "중립", "완성봉 결손.")
                + _tf_section("1h", "중립", "단기 눌림.")
                + "5️⃣ 통합\n\n요약 문장.\n\n6️⃣ 신뢰도\n")
        out, _ = fns["ensure_phase2_tf_narrative_completeness"](body, registry)
        assert out.count("확정 판단에는 사용하지 않음") == 1, "진행봉 주의 문구가 TF마다 반복됨"
        assert out.count(fns["BACKFILL_PROGRESS_PREFIX"]) == 2

    def test_integration_boundary_summarised_when_all_same(self, fns):
        registry = _origin_reg([(tf, "130.0 KRW", f"고가 1{i}0.0 KRW") for i, tf in enumerate(("1w", "1d", "4h", "1h"), 3)])
        line = fns["_phase2_integration_missing_boundary"](registry, "통합 서술 본문")
        assert line.startswith("통합 근거 경계: 선택 TF 전체(")
        # TF별 [xx 구조 돌파] 라벨이 반복되지 않아야 한다(값 안의 '구조 돌파'는 1회 유지)
        assert len(re.findall(r"\[\S+ 구조 돌파\]", line)) == 0, "TF별로 같은 문장이 반복 나열됨"

    def test_integration_boundary_lists_when_states_differ(self, fns):
        """대조군: 상태가 다르면 종전대로 TF별 나열."""
        registry = _origin_reg([("1d", "130.0 KRW", "고가 148.0 KRW"), ("4h", "130.0 KRW", "고가 136.0 KRW")])
        registry["4h:구조 돌파"]["value"] = "상방 BOS 리테스트 대기"
        line = fns["_phase2_integration_missing_boundary"](registry, "통합 서술 본문")
        assert "선택 TF 전체" not in line
        assert len(re.findall(r"\[\S+ 구조 돌파\]", line)) == 2

    def test_backfill_line_stays_compact(self, fns):
        """보완 행이 TF 고유 근거보다 길어지지 않아야 한다."""
        registry = _origin_reg([("1w", "130.0 KRW", "고가 172.0 KRW / 저가 107.0 KRW / 중심가 139.5 KRW")])
        body = "\n> 방향성 판정: 중립\n\n완성봉 0개.\n"
        facts = fns["_phase2_tf_missing_state_facts"](
            "1w", body, registry, evidence_units=fns["_phase2_tf_evidence_units"](body))
        progress = next(f for f in facts if f.startswith(fns["BACKFILL_PROGRESS_PREFIX"]))
        assert "[현재가]" not in progress and "[현재 진행 봉]" not in progress
        assert len(progress) < 100, f"보완 행이 과도하게 김: {len(progress)}자"


class TestOriginMidpointDecimal:
    """[③] float 나눗셈이 (9.36+7.81)/2를 8.584999999999999로 만들고,
    그 값이 곧 최단 무손실 표현이라 표시단에서는 되돌릴 수 없다."""

    def test_midpoint_uses_decimal_arithmetic(self):
        main_src = _find_main_py().read_text(encoding="utf-8")
        assert '"midpoint": (Decimal(str(current_row["high"])) + Decimal(str(current_row["low"]))) / 2' in main_src
        assert '"midpoint": (current_row["high"] + current_row["low"]) / 2' not in main_src

    def test_decimal_midpoint_is_exact(self, fns):
        from decimal import Decimal
        registry = fns["build_phase1_origin_fact_registry"](
            {"4h": {"quality_status": "적격", "current_price": 8.49,
                    "current_candle": {"high": 9.36, "low": 7.81,
                                       "midpoint": (Decimal("9.36") + Decimal("7.81")) / 2}}}, "KRW")
        assert "8.585 KRW" in registry["4h:현재 진행 봉"]["value"]
        assert "8.584999" not in registry["4h:현재 진행 봉"]["value"]


class TestAnalysisTargetPriceBackfill:
    """[②] 모델이 명세 서식의 현재가 표기를 빠뜨리면 분석 기준가가 사라진다."""

    def _registry(self, fns):
        return fns["build_phase1_origin_fact_registry"](
            {"1h": {"quality_status": "적격", "current_price": 130.0,
                    "current_candle": {"high": 133.0, "low": 130.0, "midpoint": 131.5}}}, "KRW")

    def test_missing_price_is_supplemented(self, fns):
        text = "**분석 대상**\n\nFOLD/KRW UPBIT 기준\n\n**방향성 평가**\n\n상방 우세\n"
        out, warnings = fns["normalize_phase2_analysis_target_current_price"](text, self._registry(fns))
        assert "(현재가: 130 KRW)" in out
        assert warnings == ["분석 대상 현재가 source 보완"]

    def test_supplement_is_non_blocking(self, fns):
        assert "분석 대상 현재가 source 보완" in fns["NON_BLOCKING_DISPLAY_OBSERVATIONS"]

    def test_existing_price_is_normalised_not_duplicated(self, fns):
        text = "**분석 대상**\n\nFOLD/KRW UPBIT 기준 (현재가: 130.00 KRW)\n\n**방향성 평가**\n\n상방 우세\n"
        out, _ = fns["normalize_phase2_analysis_target_current_price"](text, self._registry(fns))
        assert out.count("현재가:") == 1


class TestProbabilityFootnoteResidue:
    """[①] 모델이 확률 라벨을 두 줄로 쪼개 쓰면 주석부만 카드 아래에 남는다."""

    def test_standalone_footnote_is_dropped(self, fns):
        assert fns["_classify_route_context_line"]("(참고용, 백테스트 검증치 아님)") == "drop_empty_template"
        assert fns["_classify_route_context_line"]("(참고용 백테스트 검증치 아님)") == "drop_empty_template"

    def test_real_context_line_is_kept(self, fns):
        kept = "➔ 비어있는 매물 공백대 [FVG] (1d: 172.0 ~ 224.0 KRW)"
        assert fns["_classify_route_context_line"](kept) == "keep_auxiliary_context"


class TestSpecClausesForBackfill:
    def test_spec_defines_new_clauses(self):
        spec = _find_spec_file().read_text(encoding="utf-8")
        for clause in ("[Python 보완 행 중복 억제]", "[분석 대상 현재가 보완]", "[방향 배타 용어 규정]"):
            assert clause in spec, clause



# ============================================================
# [수치 대조 경계 · 앵커 완화 · 인과 서술 규정] 재검토에서 확인된 결함 고정
# ============================================================
class TestFactNumberBoundary:
    """[재검토 발견] 수치를 부분문자열로 대조하면 148.0을 2148.05 안에서,
    8.0을 58.0 안에서 찾아내 다른 수치를 같은 수치로 오인하고 보완 행을 잘못 생략한다."""

    def test_substring_false_positive_removed(self, fns):
        assert not fns["_fact_numbers_already_narrated"]("고가 148.0 KRW", "가격 2148.05 부근")
        assert not fns["_fact_numbers_already_narrated"]("현재가 8.0 KRW", "RSI 58.0 / 거래량 1.8배")

    def test_genuine_match_still_detected(self, fns):
        assert fns["_fact_numbers_already_narrated"](
            "고가 172.0 KRW / 저가 107.0 KRW", "진행봉(고가 172.0 KRW, 저가 107.0 KRW) 기준")
        assert fns["_fact_numbers_already_narrated"]("현재가 130.0 KRW", "현재가는 130.0 KRW입니다")

    def test_non_numeric_value_falls_back_to_string(self, fns):
        assert fns["_fact_numbers_already_narrated"]("구조 돌파 미확정", "구조 돌파 미확정 상태입니다")
        assert not fns["_fact_numbers_already_narrated"]("구조 돌파 미확정", "무관한 문장")


class TestAnalysisTargetAnchorRelaxed:
    """[재검토 발견] 종료 앵커를 '방향성 평가' 제목 하나에만 의존하면 모델이 제목을
    달리 쓸 때 현재가 결속이 통째로 미작동한다(C2와 같은 앵커 의존)."""

    def _reg(self, fns):
        from decimal import Decimal
        return fns["build_phase1_origin_fact_registry"](
            {"1h": {"quality_status": "적격", "current_price": 130.0,
                    "current_candle": {"high": 133.0, "low": 130.0, "midpoint": Decimal("131.5")}}}, "KRW")

    def test_works_without_direction_heading(self, fns):
        for tail in ("**방향성 평가**\n\n상방\n", "**시장 평가**\n\n상방\n", "2️⃣ 검증\n\n중립\n"):
            text = "**분석 대상**\n\nFOLD/KRW 기준\n\n" + tail
            out, warnings = fns["normalize_phase2_analysis_target_current_price"](text, self._reg(fns))
            assert "(현재가: 130 KRW)" in out, tail
            assert warnings == ["분석 대상 현재가 source 보완"]

    def test_body_is_not_swallowed(self, fns):
        text = "**분석 대상**\n\nFOLD/KRW 기준\n\n**방향성 평가**\n\n상방 우세\n\n2️⃣ 검증\n\n중립\n"
        out, _ = fns["normalize_phase2_analysis_target_current_price"](text, self._reg(fns))
        assert "상방 우세" in out and "2️⃣ 검증" in out and "중립" in out
        assert out.count("현재가:") == 1


class TestNarrativeCausalityClauses:
    def test_spec_defines_causality_and_readability_rules(self):
        spec = _find_spec_file().read_text(encoding="utf-8")
        for clause in ("[근거·판정 인과 일치]", "[인과 서술 순서]", "[선택 인용 원칙]",
                       "[분량 및 표기]", "[통합 인과 규정]"):
            assert clause in spec, clause

    def test_tag_scope_limited_to_section_one(self):
        spec = _find_spec_file().read_text(encoding="utf-8")
        assert "1️⃣ 안에서 최초 1회만 검증용 대괄호" in spec
        assert "각 서술 단위(TF별 문단 또는 PHASE2 각 번호 항목) 내 최초 1회만" not in spec



# ============================================================
# [경로 순서·근거강도·단일 레벨] 실사용 3차 표본(CHIP)에서 확인된 표시 결함 고정
# ============================================================
_LEDGER_WEAK = ("[INTERNAL_EVIDENCE_LEDGER]\n결론: 상방\n등급: 강\n등급보정: 없음\n"
                "축점수: S_1=1.5, S_2=1.0, S_3=0.5, S_4=-1.46\n순합방향: 상방\n"
                "가격경로: P_entry=54 | P_inv=30.3 | P_target_1=65.8 | P_target_2=76.4\n"
                "최종신뢰도점수: 3.0\n지지축: S_1, S_2, S_3\n반대축: S_4\n상충축: 없음\n중립축: 없음\n"
                "축내상쇄: 없음\n진행국면: 지속\nBundle:\n"
                "- 1d|최근20봉|BOS|1d:구조 돌파|상방|결정|S_1\n[/INTERNAL_EVIDENCE_LEDGER]")

_PATH_BODY = (
    "**방향성 평가**\n\n상방 우세 (우세 등급: 강)\n\n"
    "**📈 메인 시나리오 파동 경로**\n(확률 (수치)% — 참고용, 백테스트 검증치 아님)\n\n"
    "➔ 비어있는 매물 공백대 [FVG] (4h 53.2 ~ 54.8 KRW)\n➔ 주요 돌파선 (4h 60.4 KRW)\n\n"
    "**📉 대체 시나리오 파동 경로**\n(확률 (수치)%)\n\n"
    "➔ 지지·저항 역할 전환선 [Role Reversal] (1d 49.8 KRW)\n➔ 다음 매물대 (1d 44.4 ~ 49.8 KRW)\n\n"
    "3️⃣ 힘의 방향성\n\n주도 역학 F1 관측.\n")


class TestPathLineOrdering:
    """[3차 표본] 카드가 섹션 최상단에 붙어 진입·목표가가 FVG·역할 전환선보다 앞서 나오면
    가격 진행 순서로 읽히지 않는다(명세 11장 나열 순서 위반)."""

    def _contract(self, fns):
        contract, warnings = fns["build_phase2_display_contract"](_LEDGER_WEAK)
        assert not warnings
        return contract

    def _rendered(self, fns):
        out, _ = fns["render_verified_phase2_decision_blocks"](
            _PATH_BODY, self._contract(fns), "KRW", all_validation_warnings=[], display_warnings=[])
        return [l for l in out.splitlines() if l.strip()]

    def test_main_path_follows_spec_order(self, fns):
        lines = self._rendered(fns)
        def at(keyword):
            return next(i for i, l in enumerate(lines) if keyword in l)
        assert at("진입 예상가") < at("공백대") < at("돌파선") < at("1차 목표가") < at("2차 목표가")

    def test_alt_path_follows_spec_order(self, fns):
        lines = self._rendered(fns)
        def at(keyword):
            return next(i for i, l in enumerate(lines) if keyword in l)
        assert at("역할 전환선") < at("무효화선") < at("다음 매물대")

    def test_probability_line_stays_first(self, fns):
        lines = self._rendered(fns)
        head = next(i for i, l in enumerate(lines) if "메인 시나리오 파동 경로" in l)
        assert lines[head + 1].strip().startswith("(확률")

    def test_ordering_never_drops_lines(self, fns):
        """정렬은 행을 지우거나 합치지 않는다 — 순서표에 없는 행도 보존된다."""
        body = _PATH_BODY.replace("➔ 주요 돌파선 (4h 60.4 KRW)",
                                  "➔ 주요 돌파선 (4h 60.4 KRW)\n➔ 분류 불가한 임의 보조 근거 행")
        out, _ = fns["render_verified_phase2_decision_blocks"](
            body, self._contract(fns), "KRW", all_validation_warnings=[], display_warnings=[])
        assert "분류 불가한 임의 보조 근거 행" in out
        assert "주도 역학 F1 관측." in out

    def test_unranked_line_goes_last_within_section(self, fns):
        assert fns["_path_line_rank"]("➔ 알 수 없는 행", fns["MAIN_PATH_LINE_ORDER"]) == fns["UNORDERED_PATH_LINE_RANK"]
        assert fns["_path_line_rank"]("(확률 60%)", fns["MAIN_PATH_LINE_ORDER"]) == 0


class TestEvidenceStrengthAnnotation:
    """[3차 표본] 우세등급 '강'과 확률 53.9%가 나란히 나와 확신도가 과대 전달됐다.
    등급은 표수, 근거강도는 순합 크기로 서로 다른 축이므로 함께 표시한다."""

    def test_strength_matches_spec_formula(self, fns):
        from decimal import Decimal
        scores = {"S_1": 1.5, "S_2": 1.0, "S_3": 0.5, "S_4": -1.46}
        # |ΣS| = 1.54, 최대 = 4 × CORE_SCORE_CAP(2.0) = 8.0
        assert fns["_evidence_strength"](scores) == Decimal("0.19")

    def test_strength_clamped_at_one(self, fns):
        from decimal import Decimal
        assert fns["_evidence_strength"]({"S_1": 2.0, "S_2": 2.0, "S_3": 2.0, "S_4": 2.0}) == Decimal("1.00")

    def test_annotation_appended_once(self, fns):
        contract, _ = fns["build_phase2_display_contract"](_LEDGER_WEAK)
        out, _ = fns["render_verified_phase2_decision_blocks"](
            _PATH_BODY, contract, "KRW", all_validation_warnings=[], display_warnings=[])
        assert "상방 우세 (우세 등급: 강 · 근거강도 0.19)" in out
        assert out.count("근거강도") == 1

    def test_grade_text_is_not_altered(self, fns):
        contract, _ = fns["build_phase2_display_contract"](_LEDGER_WEAK)
        out, _ = fns["render_verified_phase2_decision_blocks"](
            _PATH_BODY, contract, "KRW", all_validation_warnings=[], display_warnings=[])
        assert "상방 우세" in out and "우세 등급: 강" in out


class TestZeroWidthRange:
    """[3차 표본] `49.8 ~ 49.8`은 범위가 아닌데 구간처럼 PHASE 2에 인용됐다."""

    def test_zero_width_range_collapsed(self, fns):
        card = "🔹 1d\n\n**중첩 매물대**\n\n49.8 ~ 49.8 KRW (미약 중첩 또는 미확인)\n"
        registry = fns["build_phase1_fact_registry"](card)
        assert registry["1d:중첩 매물대"]["value"] == "49.8 KRW (미약 중첩 또는 미확인)"

    def test_real_range_is_untouched(self, fns):
        card = "🔹 1d\n\n**세력 매물대**\n\n44.4 ~ 49.8 KRW\n"
        registry = fns["build_phase1_fact_registry"](card)
        assert registry["1d:세력 매물대"]["value"] == "44.4 ~ 49.8 KRW"

    def test_prefix_number_not_falsely_collapsed(self, fns):
        assert fns["_normalize_zero_width_range"]("4.8 ~ 4.85 KRW") == "4.8 ~ 4.85 KRW"


class TestSpecClausesForDisplayOrder:
    def test_spec_defines_display_clauses(self):
        spec = _find_spec_file().read_text(encoding="utf-8")
        for clause in ("[경로 나열 순서 보존]", "[우세등급·근거강도 병기]", "[단일 레벨 표기]"):
            assert clause in spec, clause



if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
