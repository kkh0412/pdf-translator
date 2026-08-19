# PDF Translator v6 - Vision-first semantic LaTeX

목표는 픽셀 단위 위치 복사가 아니라 **"원 저자가 대상 언어로 같은 논문/책을 만들었다면"**에 가까운 PDF입니다.

처리 순서:

1. 원본 PDF의 대표 페이지를 이미지로 렌더링
2. Gemini vision style agent가 컬럼 수, serif/sans 역할, 제목 색/크기, 여백, 문단 스타일을 분석
3. 각 페이지 이미지를 Gemini vision content agent가 읽음
   - 자연어와 inline math를 분리
   - 독립 수식을 실제 LaTeX로 복원
   - 그림/표만 이미지 asset으로 분리
4. 번역 agent는 자연어만 번역하고 `[[MATH_n]]` placeholder를 절대 수정하지 않음
5. XeLaTeX가 원본 style profile을 사용해 전체 문서를 새로 조판
6. 원본이 2-column이면 결과도 2-column인지 자동 검사
7. 결과 페이지 수가 비정상적으로 폭증하면 실패 처리

## API

새 secret은 필요 없습니다.

- `GEMINI_API_KEY`
- `SUPABASE_SECRET_KEY`

GitHub Actions variable:
- `SUPABASE_URL`

Vision agent는 기본 `gemini-3.6-flash`, 번역 agent도 기본 `gemini-3.6-flash`를 사용하고
일시 장애 시 3.5 Flash / Flash-Lite로 fallback합니다.

## 중요한 차이

v5까지는 번역 Gemini가 페이지 이미지를 보지 않았습니다.
v6부터는 style agent와 page reconstruction agent가 실제 렌더된 페이지 이미지를 봅니다.

수식은 더 이상 PDF crop PNG로 넣지 않습니다.
- inline math -> LaTeX math part
- display equation -> LaTeX equation environment
- 실제 figure/table만 원본 PDF에서 crop하여 이미지로 유지합니다.


## v6.1 Gemini REST fix

- `generateContent` vision requests now use `responseFormat.text.mimeType = "APPLICATION_JSON"`.
  The raw API field is an enum; the Interactions-style `"application/json"` string caused HTTP 400.
- If Google serves the legacy GenerateContent structured-output shape instead, the worker automatically retries with:
  - `responseMimeType = "application/json"`
  - `responseJsonSchema = ...`
- Deprecated Gemini 3.6 sampling parameter `temperature` has been removed.
- Model fallback is restricted to the currently documented GA models:
  - `gemini-3.6-flash`
  - `gemini-3.5-flash-lite`
