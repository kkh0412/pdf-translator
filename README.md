# PDF Translator — GitHub Pages + Supabase + Gemini

이 버전은 GitHub Pages 프런트엔드, Supabase Storage/Database/Auth, GitHub Actions worker, Gemini API를 사용합니다.

## 필요한 설정

### GitHub Pages의 `docs/config.js`
- `SUPABASE_URL`
- `SUPABASE_PUBLISHABLE_KEY`

### GitHub Actions → Variables
- `SUPABASE_URL`

### GitHub Actions → Secrets
- `SUPABASE_SECRET_KEY`
- `GEMINI_API_KEY`

`OPENAI_API_KEY`는 더 이상 사용하지 않습니다.

## Gemini 모델

기본값은 `gemini-2.5-flash`입니다. GitHub Actions의 `GEMINI_MODEL` 환경변수에서 변경할 수 있습니다.

## 동작

1. 사용자가 GitHub Pages에서 PDF를 업로드합니다.
2. PDF는 Supabase private Storage에 저장되고 `translation_jobs`에 queued 작업이 생성됩니다.
3. GitHub Actions worker가 대기 작업을 가져옵니다.
4. PyMuPDF로 자연어 텍스트 영역을 추출하고 수식으로 판단되는 영역은 건드리지 않습니다.
5. Gemini가 텍스트 영역을 번역합니다.
6. 번역문을 원래 텍스트 박스 안에 직접 삽입하고 결과 PDF를 Supabase에 저장합니다.
7. 웹페이지에서 결과 PDF를 다운로드합니다.

## 기존 OpenAI 버전에서 바꾸는 경우

GitHub → Settings → Secrets and variables → Actions에서:

1. Secret `GEMINI_API_KEY`를 새로 만듭니다.
2. Google AI Studio에서 발급받은 Gemini API key를 값으로 넣습니다.
3. 기존 `OPENAI_API_KEY`는 삭제해도 됩니다.
4. 나머지 Supabase 설정은 그대로 유지합니다.
