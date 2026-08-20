# 대관령산양의 번역기 v7.8

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