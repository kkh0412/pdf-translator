# 대관령산양의 번역기 v7.3

## Model-quota failover

A 429 is now classified before retrying.

Daily per-model quota (RPD), for example:
`GenerateRequestsPerDayPerProjectPerModel-FreeTier`
- mark that model exhausted for the rest of the current job;
- do not wait for Retry-After on the same model;
- immediately continue with the next configured model.

Temporary RPM/TPM throttling:
- honor Retry-After;
- retry the same model;
- if temporary throttling persists, continue through the model chain.

Default chain:
1. gemini-3.5-flash-lite
2. gemini-3.1-flash-lite
3. gemini-2.5-flash-lite
4. gemini-2.5-flash
5. gemini-3.5-flash
6. gemini-3.6-flash

Vision and translation maintain independent configurable lists through
`GEMINI_VISION_MODELS` and `GEMINI_TRANSLATION_MODELS`.

## Smooth browser-side elapsed timer

The elapsed-time display no longer advances only when the browser receives a
database polling response. The browser starts one local clock and redraws it
every 250 ms, so the visible second counter advances continuously even when a
network poll takes longer than expected.

## User-facing waiting/status copy

Normal progress messages no longer expose implementation terms such as Worker,
runner, GitHub, or Supabase.

Examples:
- `문서를 불러오고 있습니다.`
- `현재 요청 순서를 기다리고 있습니다. 준비되는 대로 자동으로 시작합니다.`
- `현재 요청이 많아 잠시 기다리는 중입니다. 준비되는 대로 자동으로 이어서 처리합니다.`
- `완성된 번역본을 저장하고 있습니다.`

All v7.2 math fixes, v7.1 inline-math transport recovery, and v7.0 semantic
LaTeX tables / source-native vector-raster figure handling are retained.
