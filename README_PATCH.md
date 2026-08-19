# v4.2 Reliability Patch

이 폴더는 기존 `pdf-translator-supabase-demo` 위에 덮어쓰는 패치입니다.

변경점:
- Supabase documents bucket: 20 MB -> 50 MB
- 결과 PDF 업로드 전 lossless 압축
- Gemini 기본 모델: gemini-3.5-flash-lite
- Gemini 429/500/502/503/504 자동 재시도 + fallback
- 일시적인 Gemini 장애면 job을 failed로 끝내지 않고 queued로 되돌려 다음 worker에서 자동 재시도
- OpenAI / pydantic dependency 제거

반드시 Supabase Dashboard > SQL Editor에서
`supabase/UPDATE_EXISTING_SUPABASE.sql`을 한 번 실행하세요.
