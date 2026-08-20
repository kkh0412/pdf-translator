# 대관령산양의 번역기 v8.6.3

## v8.6.3 translation reliability update

v8.6.2를 기준으로 본문 번역 경로와 checkpoint 복구 구조를 점검하고 수정한 버전입니다.
Supabase schema 변경은 없으며 SQL을 다시 실행할 필요가 없습니다.

### 1. inline math 문장부호 false positive 제거

이전 검사는 원문에서 문장 중간에 있던 `[[MATH_n]]`가 번역문에서 마침표 바로 앞/뒤로
이동하면 다음 오류로 전체 작업을 실패시킬 수 있었습니다.

```text
Translation introduced a sentence break immediately before embedded inline math
```

언어가 바뀌면 어순이 달라져 수식이 번역문의 문장 끝으로 이동하는 것은 정상일 수 있습니다.
따라서 v8.6.3은 **문장부호 위치를 무결성 조건으로 사용하지 않습니다.** 대신 다음을 엄격하게
검사합니다.

- 원문의 모든 `[[MATH_n]]` placeholder가 정확히 존재해야 함
- placeholder 삭제 / 중복 / 번호 변경 / 임의 추가 금지
- 내부 math transport marker가 최종 번역문에 남으면 실패
- PDF 렌더링 직전에도 placeholder 집합을 한 번 더 검증

즉 수식 자체의 보존은 strict하게 유지하면서, 목표 언어의 자연스러운 어순은 허용합니다.

### 2. Gemini 429 이후 provider route를 job 단위로 고정

기존에는 module-global Event와 single-block quality fallback이 섞여 있어, 이미 Google 번역으로
전환된 뒤에도 품질 복구 경로가 다른 Gemini model을 다시 호출할 수 있었습니다.

v8.6.3은 `TranslationRoute`를 job별로 사용합니다.

```text
Gemini prose translation 429
        ↓
즉시 Google Translate lock
        ↓
현재 job의 남은 본문은 Google Translate only
        ↓
checkpoint에도 lock 저장
        ↓
새 GitHub runner에서 resume해도 Gemini를 다시 probe하지 않음
```

본문 translation에서는 Gemini 429의 `Retry-After`를 해석하거나 기다리지 않습니다.
Vision / math source repair의 retry 정책은 별도이며 그대로 유지됩니다.

### 3. Google fallback 오류를 분류

Google fallback 오류를 다음 두 종류로 분리했습니다.

- transient: 네트워크 / 서비스 응답 오류 → checkpoint 보존 후 재시도 가능한 상태
- integrity: 보호 token / placeholder 복구 실패 → multi-block이면 작은 묶음으로 격리

Google mode로 들어간 뒤 integrity recovery가 발생해도 Gemini model chain으로 되돌아가지 않습니다.

### 4. translation exception routing 수정

기존 `_request_batch()`에서는 `GeminiOutputError`가 `RuntimeError`의 하위 클래스인데도 broad
`except RuntimeError`가 먼저 있어 quality error가 transport error처럼 처리될 수 있었습니다.

v8.6.3에서는 network/protocol 오류를 `GeminiTransportError`로 별도 분리하고,
structured/content quality 오류와 transport 오류의 recovery 경로를 명확히 나눴습니다.

### 5. checkpoint translation state versioning

`translation_state_version = 2`를 추가했습니다. 이전 버전에서 완료 표시된 translation
checkpoint도 v8.6.3에서 한 번 재검증됩니다. 이미 정상인 block은 그대로 사용하고, 현재 무결성
규칙을 통과하지 못하는 block만 다시 번역합니다.

Gemini 429로 Google-only route가 된 job은
`translation_google_fallback_locked = true` 상태가 checkpoint에 저장됩니다.

### 6. regression tests

다음 회귀 테스트를 추가했으며 GitHub Actions worker 시작 전에 실행합니다.

- Gemini 429 → 즉시 Google 전환
- 429 이후 Gemini 재진입 금지
- resume된 Google-only route에서 Gemini 호출 금지
- inline math가 번역문 문장 끝으로 이동해도 정상 처리
- placeholder 삭제/번호 변경은 실패
- Google integrity error batch isolation
- old checkpoint의 inline-math 무결성 재검증
- quality error가 transport error로 잘못 소비되지 않는지 확인

### 7. obsolete heartbeat SQL 정리

literal `\n` 문제가 있던 오래된 `V851_HEARTBEAT_SCHEMA_HOTFIX.sql`은 프로젝트에서 제거했습니다.
현재 heartbeat 관련 standalone hotfix는 `V852_HEARTBEAT_SCHEMA_HOTFIX.sql`만 남겨 두었습니다.
기존 DB에 다시 SQL을 적용할 필요는 없습니다.

## v8.6.2 frontend refresh behavior

수동 새로고침 시 이전 progress card를 즉시 복원하지 않고 화면의 진행 UI를 초기화합니다.
서버-side job/checkpoint 구조와 active job id 보존 방식은 유지됩니다.

## googletrans runtime

본문 fallback은 Python `googletrans==4.0.2`를 사용합니다.
`GOOGLE_TRANSLATE_API_KEY`는 필요하지 않습니다.

GitHub Actions는 worker 본작업 전에 `worker/ensure_python_runtime.sh`를 실행해
`pymupdf`, `supabase`, `googletrans` import 가능 여부를 검증합니다. runtime cache key에는
현재 Python/LaTeX requirements hash가 포함되어 있으며, cache miss/older restore에서는
`worker/setup_runtime.sh`가 requirements를 동기화합니다.

정상 로그에는 worker 시작 전에 다음과 같은 dependency verification이 나타납니다.

```text
Verify Python runtime dependencies
Python dependencies ready · googletrans=4.0.2
```

## Retained features

- persistent checkpoint / resume
- browser heartbeat and disconnected-worker cancellation
- hybrid PDF text layer + Gemini Vision reconstruction
- malformed figure bbox normalization / native geometry recovery / figure-only skip
- semantic LaTeX tables
- vector PDF figure preservation and raster figure preservation
- real LaTeX display equations
- source-aware math preflight / repair
- paragraph indentation reconstruction
- local page/column flow reconstruction
- 100-page limit
