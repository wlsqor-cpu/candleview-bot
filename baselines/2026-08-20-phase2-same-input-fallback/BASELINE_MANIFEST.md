# CandleView PHASE 2 동일 입력 Fallback Base 기준본

이 디렉터리는 2026-08-20에 GitHub 자동 검증·승격과 Render live 전환 뒤, UPBIT·BITHUMB·COINBASE의 PHASE 1→PHASE 2 최종 브리핑 발행을 확인한 뒤 고정한 **운영 Base 기준본**이다. 신규 개발·감사 작업은 이 파일쌍을 직접 수정하지 않는다.

## 기준 정보

| 항목 | 값 |
|---|---|
| GitHub canonical commit | `61b1f171e731584f06b9461e02be27c16c4a09f9` |
| source release commit | `741cd57a62015f4714594eceaa04a3a269b88550` |
| GitHub validation run | `32341874100` — success |
| Render service | `srv-d9osj66gekts73eodcig` |
| Render deploy | `dep-da3ab6p10uac73eq2rh0` |
| Render status at capture | `live` |
| main.py SHA-256 | `6d5c2511b5f54e1fd84b54056239b91d30a47c4ecdd88f9cd97af1c1ea250fb1` |
| CandleView_API.txt SHA-256 | `f072842175b41e76bbc9a5e6b1952b25c278425c4f85f2b655f0d239faa92cf7` |

## 이 Base가 보존하는 계약

PHASE 1 Gemini 수집·품질 카드 생성과 PHASE 2 Gemini 최종 브리핑 재구성의 기존 2단계 계약을 유지한다. PHASE 2는 PHASE 1 성공 모델을 첫 후보로 사용한다. 그 모델이 PHASE 2에서 429 또는 503으로 실제 실패한 경우에만, 같은 PHASE 1 완성 결과·canonical provenance·PHASE 2 프롬프트·JSON schema를 승인 roster의 뒤 순위 모델에 전달한다.

이 Base는 3.7→3.6 단방향 대체만 허용한다. PHASE 1이 3.6으로 성공한 경우 3.6→3.7 역방향 전환은 허용하지 않는다. 400·형식 오류·검증 경고를 이유로 모델을 바꾸지 않는다. 실제 PHASE 2 성공 모델과 fallback 상태는 Telegram header에 표시한다.

근거원장에서는 논리적 확정 순서와 구조화 JSON 문자열 조립 순서를 구분한다. Python은 `user_briefing`을 먼저 렌더링한 뒤 내부 ledger를 검증용으로 조립한다. 이 문서화는 수식·점수·TF·가격 경로·FindCoin 기준을 변경하지 않는다.

## 실제 확인된 동작 범위

| 운영 사례 | 실제 경로 | 확인 결과 |
|---|---|---|
| UPBIT PUMP/KRW | PHASE 1 Gemini 3.6 성공 → PHASE 2 Gemini 3.6 | 최종 브리핑 발행; P2V01·P2V02 관측 후 비차단 발행 |
| BITHUMB PUMP/KRW | PHASE 1 Gemini 3.7 성공 → PHASE 2 Gemini 3.7 | 최종 브리핑 발행 |
| COINBASE PUMP/USD | PHASE 1 Gemini 3.7 성공 → PHASE 2 Gemini 3.7 | 1w·1d·6h·1h 기본 TF로 최종 브리핑 발행 |
| GitHub·Render | 자동 검증 성공 → canonical 승격 → Render live | 배포 후 application error 0건, warning 0건(07:00:33 UTC 이후 확인 범위) |

## 검증 경계

PHASE 2의 3.7→3.6 대체 경로는 3.7 429·503, 동일 payload, 3.6 역방향 차단, 400 차단, 실제 Telegram model provenance를 포함한 결정론적 회귀에서 통과했다. 그러나 **이 대체 경로의 자연 발생 실운영 성공 사례는 Base 고정 시점까지 관측되지 않았다.** 이는 Base의 미확인 외부 공급자 경계로 기록하며, 이후 자연 발생 시 운영 원장에 추가한다.

## 복구 절차

1. 이 디렉터리의 `SHA256SUMS`로 `main.py`와 `CandleView_API.txt`의 무결성을 확인한다.
2. 복구 전 현재 운영 파일과 새 후보의 차이를 보관한다.
3. 사용자 명시 승인 후에만 이 두 파일을 **새로운** `releases/<복구-식별자>/` 파일쌍으로 복제하고 push한다.
4. GitHub workflow 성공·canonical SHA-256 일치·Render live 커밋 일치를 모두 확인한다.
5. 이 Base 보관 경로는 배포를 유발하지 않으며, 직접 운영 root로 복사하지 않는다.

> 이 Base는 위의 확인된 운영 상태를 되돌리기 위한 기준이다. 외부 Gemini 공급자의 모든 미래 quota·503 상태나 모든 시장 조건을 보증하지 않는다.
