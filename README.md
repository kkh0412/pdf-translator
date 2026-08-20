# 대관령산양의 번역기 v8.4.1

## Supabase SQL hotfix
- `pdf_translation_client_signal` 함수의 PL/pgSQL 지역변수 `current_user`를 `v_client_user_id`로 변경했습니다.
- PostgreSQL 예약 표현 `CURRENT_USER`와의 충돌로 발생하던 `42601 syntax error`를 수정했습니다.
- v8.4의 resilient figure 처리와 client-heartbeat worker pause 기능은 그대로 유지됩니다.

## Resilient figure extraction
- Vision bbox 좌표는 먼저 정렬/클램프하므로 x0>x1, y0>y1이 문서를 중단시키지 않습니다.
- 너무 얇은 bbox는 원본 PDF의 실제 embedded image/vector drawing geometry로 복구합니다.
- native vector/raster 보존이 실패하면 해당 영역만 안전한 raster crop으로 fallback합니다.
- 신뢰할 수 있는 visual object를 전혀 찾지 못하면 그 figure 하나만 건너뛰고 전체 번역은 계속합니다.
- hybrid PDF-text 복구 뒤 중복 prose block도 제거해 100%를 크게 넘는 coverage/중복 삽입을 줄였습니다.

## Browser heartbeat / worker stop
- 번역 중 브라우저는 8초마다 소유 job에 heartbeat RPC를 보냅니다.
- worker는 별도 thread에서 heartbeat를 감시합니다. 기본 45초 동안 heartbeat가 없으면 현재 checkpoint를 저장하고 status=paused로 바꾼 뒤 GitHub worker 프로세스를 정상 종료합니다.
- paused job은 recovery cron이 자동으로 다시 실행하지 않습니다.
- 같은 브라우저가 다시 접속하면 localStorage의 job id를 복구하고 heartbeat/resume RPC가 status를 queued로 바꿔 저장된 checkpoint부터 새 worker를 실행합니다.
- Gemini rate-limit 대기, Vision/translation API 호출, math preflight에도 cooperative cancellation check를 추가했습니다.

## Required database update
이 버전의 heartbeat/resume 기능을 사용하려면 최신 `supabase/UPDATE_EXISTING_SUPABASE.sql`을 한 번 실행해야 합니다.

## Hybrid PDF reconstruction safety architecture

v8.3 removes two page-fatal assumptions that caused repeated failures on
equation-heavy scientific pages.

### 1. Prose completeness is source-text-backed, not Vision-count-backed
The old validator compared all alphabetic characters extracted from the PDF
(including letters inside equations) against only Vision prose characters.
Equation-heavy pages could therefore fail with misleading ratios such as
`226/714` even when most missing characters belonged to mathematics.

Now:
- ordinary prose uses the PDF text layer as an authoritative wording source;
- a strongly matched plain-prose Vision block can be restored from source text;
- source prose blocks genuinely omitted by Vision are inserted deterministically;
- math-looking source blocks are NEVER converted into prose;
- completeness is diagnostic only after source recovery, not a page-fatal gate.

### 2. equation_lines can never kill a page
`equation_lines` is only a layout hint. v8.3 discards it and uses the full
`equation_latex` plus the renderer's deterministic line breaking. A malformed
matrix/determinant line hint therefore no longer causes the page to be retried.

### 3. Malformed full formulas are deferred to math preflight
A brace problem in `equation_latex` or inline math is flagged but does not cause
page OCR to restart. The formula reaches the existing independent XeLaTeX
preflight, which can reconstruct only that formula from its original PDF crop.

### 4. Persistent checkpoints are upgraded automatically
Pages saved by older v7/v8 checkpoints are passed through the same deterministic
hybrid source-text recovery on resume. Already-finished pages do not need a new
Vision API call.

Structural failures that cannot be safely guessed (empty equations, broken
semantic table geometry, invalid bboxes) remain strict and still trigger a
Vision retry.

All v8.2 homepage behavior and earlier checkpoint, LaTeX-table, vector/raster,
translation, quota, and math-preflight features are retained.

## Large logo + fixed two-line title
- 산양 로고를 다시 데스크톱 120–156px로 복원했습니다.
- 제목 폭을 확보하기 위해 hero 영역을 전체 shell 폭까지 사용합니다.
- 제목을 두 개의 명시적 줄로 분리하고 데스크톱에서는 각 줄이 다시 꺾이지 않도록 했습니다.
- 큰 로고를 유지하면서 두 줄이 들어가도록 제목 크기를 48–66px 범위로 조정했습니다.
- 작은 화면에서는 overflow 방지를 위해 다시 자연스러운 줄바꿈을 허용합니다.

## Hero title 2-line adjustment
- 메인 제목이 3줄이 아니라 2줄로 보이도록 제목 레이아웃을 조정했습니다.
- 데스크톱 로고 크기를 약 88–112px로 줄여 제목 폭을 확보했습니다.
- 제목 줄바꿈이 더 안정적으로 2줄이 되도록 데스크톱 h1 크기와 폭을 미세 조정했습니다.
- 모바일 로고는 58px로 조정했습니다.

## Hero logo size adjustment
- 홈페이지 제목 왼쪽 산양 로고를 크게 키웠습니다.
- 데스크톱에서는 약 120–156px 범위로 표시되어, 두 줄짜리 메인 제목 높이와 비슷한 인상을 줍니다.
- 모바일에서는 72px로 유지합니다.
- 제목은 계속 로고의 오른쪽에 배치됩니다.
- 기존 배경색 일치 및 외곽선 제거 설정은 그대로 유지됩니다.

## Homepage logo fix
- 이전 CSS의 `.hero-logo { width: 100%; }` 중복 규칙을 완전히 제거했습니다.
- 홈페이지 로고는 데스크톱 44px, 모바일 34px로 고정했습니다.
- 제목은 항상 flex row에서 로고의 오른쪽에 배치됩니다.
- 기존 둥근 사각형 타일 배경/외곽선을 제거한 `hero-logo.png`를 새로 만들었습니다.
- 이미지 주변은 홈페이지 배경과 정확히 같은 `#f5f4ef`이며 JPEG 압축 halo도 없습니다.

## Homepage logo refinement
- 홈페이지용 로고를 `hero-logo.jpg`로 분리했습니다.
- 투명 모서리를 제거하고 홈페이지 배경색 `#f5f4ef`로 완전히 채워 검은 주변부가 나타나지 않습니다.
- 로고 크기를 데스크톱 54–72px, 모바일 46px로 줄였습니다.
- `PDF TRANSLATION`은 위에 유지하고, 큰 제목은 산양 로고의 정확한 오른쪽에 배치했습니다.

## Hero logo
- 브라우저 favicon으로 사용하는 산양 아이콘을 메인 제목 왼쪽에도 배치했습니다.
- 데스크톱에서는 제목과 나란히, 모바일에서는 작은 크기로 유지됩니다.
- 홈페이지의 기존 베이지/차콜 색조와 레이아웃을 유지했습니다.

## Browser favicon
- 산양 번역 아이콘을 브라우저 탭 favicon으로 추가했습니다.
- 홈페이지 배경색 `#f5f4ef`과 맞도록 바깥 영역은 투명 처리했습니다.
- `favicon.ico`, 32px PNG, 512px PNG, Apple touch icon을 함께 제공합니다.
- favicon URL에 `?v=76`을 붙여 기존 브라우저 캐시가 새 아이콘을 덜 가로막도록 했습니다.

## Paragraph indentation
Each semantic body paragraph is checked against the source PDF's actual line
geometry. The first line x-position is compared with following lines, so
indented and non-indented paragraphs can coexist exactly as in the source.
This pass is deterministic and also refreshes older resumed checkpoints.

## Short display equations
The renderer no longer keeps Vision-supplied line breaks merely because
`equation_lines` contains multiple entries. If the complete expression is
comfortably short for its local column, it is rendered on one line. Long
equations retain semantic/safe breaking.

## Sentence continuity around mathematics
Inline math placeholders are treated as grammatical constituents of the same
sentence. The translation prompt forbids creating a sentence boundary merely
around inline mathematics, and a narrow validator rejects that failure mode.
Read-only previous/next context is also supplied where prose continues across a
standalone display equation.

Translations restored from older checkpoints are revalidated with the new
continuity rule; only blocks that fail are translated again.

All v7.4 checkpoint/resume behavior and earlier v7 table, figure, math, quota,
and frontend features are retained.