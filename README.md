# PDF Translator v6.2

v6.2는 "원 저자가 대상 언어로 같은 논문을 다시 썼다면"이라는 목표를 유지하면서
수식, 가변 컬럼, 용어 일관성과 처리 시간을 개선한 버전입니다.

## 변경점

1. **긴 display equation 자동 줄바꿈**
   - Vision agent가 `equation_lines`를 제안합니다.
   - renderer가 한 줄이 여전히 길면 top-level `=`, `+`, `-`, 관계연산자에서 다시 나눕니다.
   - 분수/루트/첨자/윗첨자/괄호 내부는 자동 줄바꿈 지점으로 사용하지 않습니다.
   - 안전한 분할점이 전혀 없는 경우에만 마지막 수단으로 폭에 맞게 축소합니다.

2. **윗첨자/아랫첨자 및 bra-ket 복원 강화**
   - 페이지 이미지를 기준으로 `^` / `_`를 명시적으로 복원합니다.
   - 예: `P_R^\gamma`, `S_{\mathcal M,\gamma}^{(j)}`, `\rho^T`, `q_y`.
   - inline math는 번역 agent에 전달되지 않고 `[[MATH_n]]` placeholder로 보호됩니다.

3. **가독성**
   - 검출한 본문 크기보다 약 6% 작게 조판합니다.
   - baseline 간격은 약간 넓혀 한국어 2단 논문에서 답답함을 줄입니다.

4. **가변 컬럼**
   - `twocolumn` document class 고정 방식을 제거했습니다.
   - `multicol`을 사용하고 각 semantic block에 `flow_columns = 1/2/3`를 저장합니다.
   - 2 -> 1, 1 -> 2, 2 -> 3 등 지역별 전환을 문서 흐름에서 반영합니다.
   - full-width figure/equation은 컬럼 flow를 잠시 닫고 전체 폭으로 배치한 뒤 다음 블록의 flow를 다시 엽니다.

5. **번역 전 domain/terminology scan**
   - style-only 호출 대신 한 번의 가벼운 document scan으로:
     - 분야/세부분야
     - 문서 유형과 문체
     - 전문용어 glossary
     - 번역 원칙
     - 보존할 항목
     을 먼저 만듭니다.
   - 이 glossary를 모든 번역 batch에 동일하게 넣어 용어 일관성을 높입니다.

6. **속도**
   - 예전: 페이지 이미지 1장당 Gemini 호출 1회, 순차 처리.
   - v6.2: 기본 2페이지/vision call + 2개 vision call 병렬 처리.
   - 번역 batch도 최대 2개 병렬 처리.
   - style 분석과 terminology scan을 하나의 API 호출로 합쳤습니다.
   - 429/5xx가 나면 기존 retry/fallback이 작동합니다.
   - Actions 로그 마지막에 vision / translation / LaTeX / total 시간을 각각 출력합니다.

환경 변수 기본값:
- `VISION_PAGES_PER_CALL=2`
- `VISION_WORKERS=2`
- `TRANSLATION_WORKERS=2`

무료 API의 실제 rate limit은 계정/프로젝트 상태에 따라 달라질 수 있으므로 동시성을 2로 보수적으로 제한했습니다.
