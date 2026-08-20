# 대관령산양의 번역기 v8.6

## Python Google Translate fallback

본문 번역의 최후 fallback을 Google Cloud Translation API에서 Python `googletrans`로 교체했습니다.

- `googletrans==4.0.2`를 worker 가상환경에 자동 설치합니다.
- 별도의 Google Cloud 프로젝트, billing, API key가 필요하지 않습니다.
- Gemini 본문 번역에서 429가 한 번이라도 나오면 기다리지 않고 즉시 Python Google 번역으로 전환합니다.
- 한 번 전환된 작업은 남은 본문 translation batch에서도 Gemini를 다시 호출하지 않습니다.
- 수식 `[[MATH_n]]` placeholder와 KEEP ENGLISH 전문용어는 opaque token으로 보호한 뒤 정확히 복원합니다.
- Google 번역 결과도 기존 placeholder / 번역 품질 / inline-math 연속성 검사를 통과해야 checkpoint에 저장됩니다.
- Python Google 번역 자체가 실패하면 긴 대기 재시도를 하지 않고 기존 checkpoint를 보존해 다음 실행에서 이어갑니다.
- Vision, 수식 OCR, source-aware math repair는 Google 번역으로 대체할 수 없으므로 기존 Gemini/PDF-source 구조를 유지합니다.

`googletrans`는 Google의 공식 Cloud Translation SDK가 아니라 `translate.google.com` 계열 웹 번역 서비스를 사용하는 비공식 라이브러리입니다. 따라서 Google이 GitHub runner IP를 제한하거나 웹 인터페이스를 변경하면 일시적으로 실패할 수 있습니다.

## Retained features

v8.5.3 이하의 다음 기능을 유지합니다.

- Gemini translation 429 zero-wait fallback policy
- persistent checkpoint/resume
- browser heartbeat and disconnected-worker cancellation
- hybrid PDF text layer + Vision reconstruction
- resilient malformed figure bbox recovery
- semantic LaTeX tables
- vector/raster source-native figure preservation
- mathematical preflight and source-aware repair
- paragraph indentation reconstruction
- inline-math sentence continuity
- 100-page limit
