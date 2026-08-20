# 대관령산양의 번역기 v8.6.4

## v8.6.4 layout fidelity update

v8.6.3의 번역/429/checkpoint 신뢰성 구조를 유지하면서 최종 LaTeX 조판 경로를 수정한 버전입니다.
Supabase schema 변경은 없으며 SQL을 다시 실행할 필요가 없습니다.

### 1. display equation이 간헐적으로 커지는 문제 수정

긴 단일 수식을 `\resizebox{0.98\linewidth}{!}{...}`로 처리하면 자연 폭이 더 작은 수식까지
목표 폭으로 **확대**될 수 있었습니다. 이제 `\SourceFitDisplayMath`가 실제 수식 폭을 먼저 측정하고,
컬럼을 넘는 경우에만 축소합니다. 짧거나 이미 들어맞는 수식은 원래 크기로 조판합니다.

### 2. figure/table 크기를 source geometry에서 복원

모든 figure를 일괄적으로 약 90% 폭으로 키우던 방식을 제거했습니다. figure asset의 실제 source PDF bbox
(잘못된 Vision bbox를 native PDF geometry로 고친 경우에는 그 repaired bbox)를 사용해 원본 컬럼/본문 폭 대비
비율을 계산합니다. semantic table도 같은 source geometry 비율을 사용합니다. vector/raster 보존 정책은 그대로입니다.

### 3. list bullet 중복 제거

`list_item`의 원문/번역문에 `•`, `-`, `▪` 등의 marker가 남아 있는데 LaTeX `itemize`가 marker를 하나 더
붙여 `••`처럼 보이던 경로를 정리했습니다. 구조가 list item으로 확정된 경우 source marker는 제거하고
LaTeX marker 하나만 사용합니다. 오래된 checkpoint 번역에 bullet이 남아 있어도 render 직전에 다시 제거합니다.

### 4. section/subsection 앞 여백

section 앞에는 `\addvspace{0.92em}`, subsection 앞에는 `\addvspace{0.68em}`을 적용합니다. 단순 `\vspace`보다
연속 heading/column transition에서 불필요한 여백 누적이 적습니다. heading font family도 document scan에서 검출한
serif/sans 설정을 사용하도록 수정했습니다.

### 5. caption source style 보존

PDF text layer에서 caption의 source font size/family/weight를 다시 측정해 checkpoint resume에서도 복구합니다.
caption이 직전 figure/table과 연결되면 해당 visual의 원본 폭을 caption container에도 반영하고, source bbox 관계를
이용해 left/center/right alignment를 결정합니다. text layer가 없는 scan은 안전한 기본 caption style로 fallback합니다.

### 6. layout regression tests

worker 시작 전 회귀 테스트에 다음을 추가했습니다.

- 긴 단일 수식이 shrink-only 경로를 쓰는지
- 2-column figure 폭이 source bbox 비율로 계산되는지
- caption alignment/font size/container width 보존
- structural bullet 제거
- section/subsection 앞 여백 생성

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
