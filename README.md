# 대관령산양의 번역기 v8.0

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