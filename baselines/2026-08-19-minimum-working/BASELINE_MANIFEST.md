# CandleView 최소 작동 Base 기준본

이 디렉터리는 2026-08-19에 실운영 PUMP/KRW 분석의 PHASE 1·PHASE 2 최종 발행을 확인한 뒤 고정한 **최소 작동 기준본**이다. 신규 개발·감사 작업은 이 파일쌍을 직접 변경하지 않는다.

## 기준 정보

| 항목 | 값 |
|---|---|
| GitHub canonical commit | `b31ad2844546e68d8bc4bdecbf1cc84fdf01130f` |
| source release commit | `117dcf4c0d5586d1462d5c900d45a253cafa8f17` |
| Render service | `srv-d9osj66gekts73eodcig` |
| Render deploy | `dep-da2r1310dvcs73al33qg` |
| Render status at capture | `live` |
| main.py SHA-256 | `0b14d8d9cb94111d46e927a5d096b5b3da2e440d509cb5b24e712930d51c20ba` |
| CandleView_API.txt SHA-256 | `96d9c0c4cd70dc2fcf5bd700ba2513921a9641eb02fc880a4dee6192ba7b5c59` |

## 실제 확인된 동작 범위

PUMP/KRW 실운영에서 기본 TF(1w, 1d, 4h, 1h)로 PHASE 1 수집·카드·인라인 승인·PHASE 2 1️⃣~6️⃣ 최종 발행을 확인했다. PHASE 1은 static roster 내부 Gemini 3.6 Flash fallback으로 성공했고, PHASE 2는 동일한 실제 성공 모델을 사용했다. 첫 PHASE 2 응답의 P2V02 보류는 동일 원천·동일 모델 2회차 재질의에서 통과했다.

이 기준본에는 세션 token callback, PHASE 2 provenance/rule-ID 관측, P2M03 동일 세션·동일 모델 1회 재시도가 포함된다. 수식·점수·가격공식·FindCoin·백테스트 파일·분석 카드 UI는 이전 기준에서 변경하지 않았다.

## 복구 절차

1. 이 디렉터리의 `SHA256SUMS`로 `main.py`와 `CandleView_API.txt`의 무결성을 확인한다.
2. 복구 전 현재 운영 파일과 새 후보의 차이를 보관한다.
3. 사용자 명시 승인 후에만 이 두 파일을 **새로운** `releases/<복구-식별자>/` 파일쌍으로 복제하고 push한다.
4. GitHub workflow 성공·canonical SHA-256 일치·Render live 커밋 일치를 모두 확인한다.
5. `releases/` 밖의 이 Base 보관 경로는 배포를 유발하지 않으며, 직접 운영 root로 복사하지 않는다.

> 이 Base는 현재 확인된 최소 작동 상태를 되돌리기 위한 기준이며, 전체 시스템의 모든 외부 API 상태나 모든 향후 시장 조건을 보증하는 문서는 아니다.
