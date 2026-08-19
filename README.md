# PDF Translator - Semantic LaTeX v5

이번 버전은 원본 PDF 위에 번역문을 덮어쓰지 않습니다.

- 원본 PDF에서 제목 / 절 / 소제목 / 문단 / 불릿 / 인용문(시) / 그림 / 표시수식을 읽습니다.
- 자연어만 Gemini로 번역합니다.
- 표시수식은 원본에서 이미지로 보존하여 수식이 깨지지 않게 합니다.
- 그림도 원본에서 가져와 문서 흐름 안에 다시 넣습니다.
- XeLaTeX로 문서를 새로 조판합니다.
- 원본이 serif 계열이면 Latin은 Kp fonts 계열을 사용하고, 한국어 본문은 Nanum Myeongjo를 사용합니다.
- 원래 페이지의 정확한 문단 좌표는 따라가지 않고, 원본의 활자 계층과 문서 스타일을 우선합니다.

GitHub Actions는 TinyTeX + 필요한 LaTeX 패키지 + 한국어 책 글꼴을 Actions cache에 저장합니다. 첫 처리 때만 설치하고, 이후 cache가 살아 있는 동안에는 복원해서 사용합니다.


## v5.1 Korean TeX dependency fix

`xetexko`의 필수 보조 패키지인 `cjk-ko`를 TinyTeX cache에 포함합니다.
기존 v5 cache가 있으면 이를 복원한 뒤 빠진 패키지만 추가하고, 새 cache key로 다시 저장합니다.
따라서 전체 TinyTeX를 매번 다시 설치하지 않습니다.
