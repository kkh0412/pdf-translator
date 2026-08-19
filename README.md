# PDF Translator - Semantic LaTeX v5.4

이번 버전은 원본 PDF의 정확한 문단 좌표를 복제하지 않고, 제목 / 절 / 소제목 / 본문 / 불릿 / 인용문 / 그림 / 수식의 **조판 스타일**을 우선해 XeLaTeX로 새 문서를 만듭니다.


## v5.4 번역 안정성 수정

- Gemini가 segment ID를 다시 출력하는 구조를 제거했습니다.
- 각 배치에 대해 번역 문자열 배열만 받고, 입력 순서대로 로컬에서 ID에 매핑합니다.
- JSON Schema의 `minItems` / `maxItems`를 배치 크기와 동일하게 설정합니다.
- 배치 크기를 최대 40개 / 약 9,000자로 낮췄습니다.
- 응답 개수가 맞지 않거나 빈 번역이 있으면 해당 배치만 재시도합니다.
- 두 번 실패하면 해당 배치를 반으로 나눠 재귀적으로 복구합니다.
- 마지막 한 segment가 반복해서 실패하더라도 전체 PDF를 실패시키지 않고 원문을 보존합니다.

## v5.3 핵심 수정

- 원본 syllabus에서 실제로 사용된 Kp 계열 분위기를 유지합니다.
- `kpfonts-otf.sty`는 더 이상 로드하지 않습니다.
- 대신 `KpRoman-*.otf` / `KpSans-*.otf` 파일을 `fontspec`으로 직접 로드합니다.
- 따라서 `kpfonts-otf.sty`가 요구하던 `realscripts`와 `unicode-math` 때문에 컴파일이 연쇄적으로 깨지는 문제가 없어집니다.
- 한국어 본문은 Nanum Myeongjo를 사용합니다.
- GitHub Actions가 실제 생성 문서와 동일한 핵심 preamble을 사용한 smoke-test PDF를 먼저 컴파일합니다. 이 테스트가 성공한 뒤에만 사용자 PDF 처리를 시작합니다.
- TinyTeX와 폰트는 Actions cache를 재사용합니다.

## 브라우저 / Supabase 설정

`docs/config.js`에는 현재 프로젝트의 Supabase URL과 publishable key가 이미 설정되어 있습니다.

GitHub Actions Secrets에는 다음 두 값만 필요합니다.

- `GEMINI_API_KEY`
- `SUPABASE_SECRET_KEY`

GitHub Actions Variable에는 다음 값이 필요합니다.

- `SUPABASE_URL = https://ejiwkhalnssozcrjxojc.supabase.co`
