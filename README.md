# PDF Translator - Semantic LaTeX v5.3

이번 버전은 원본 PDF의 정확한 문단 좌표를 복제하지 않고, 제목 / 절 / 소제목 / 본문 / 불릿 / 인용문 / 그림 / 수식의 **조판 스타일**을 우선해 XeLaTeX로 새 문서를 만듭니다.

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
