# PDF Translator — GitHub Pages + Supabase demo

이 버전은 Render를 사용하지 않습니다.

- 사용자 웹페이지: **GitHub Pages** (`docs/`)
- 백엔드 데이터/API: **Supabase**
  - Auth: 사용자별 익명 세션
  - Storage: 원본 PDF / 번역 PDF
  - Postgres: 작업 상태와 기록
  - Edge Functions: 업로드 준비, 작업 시작, 관리자 목록
- 무거운 PDF 처리: **GitHub Actions worker**
  - PyMuPDF로 레이아웃 분석
  - OpenAI API로 번역
  - XeLaTeX로 결과 PDF 생성
  - 결과를 다시 Supabase Storage에 업로드

Supabase Hosted Edge Functions 안에서 XeLaTeX를 직접 실행하지 않는 이유는 Edge 런타임의 CPU 제한 때문입니다. Supabase는 백엔드의 영구 저장소, DB, 인증, API를 담당하고 GitHub Actions는 계산 worker 역할만 합니다.

---

# 처음 배포할 때 할 일

웹 개발 지식이 거의 없다는 전제로 작성했습니다. 아래 순서대로 하면 됩니다.

## 1. GitHub repository 만들기

1. GitHub에 로그인합니다.
2. 새 repository를 하나 만듭니다. 예: `pdf-translator`.
3. 이 프로젝트 ZIP을 압축 해제합니다.
4. 압축을 푼 **내용물 전체**를 repository 최상위에 업로드합니다.
5. GitHub 첫 화면에서 다음 항목들이 보이면 정상입니다.

```text
.github/
docs/
supabase/
worker/
README.md
```

## 2. Supabase 프로젝트 만들기

1. Supabase Dashboard에서 새 프로젝트를 만듭니다.
2. 프로젝트 생성이 끝날 때까지 기다립니다.
3. Authentication 설정에서 **Allow anonymous sign-ins**를 켭니다.
   - 이 사이트는 회원가입 화면 없이 사용자별 안전한 세션을 만들기 위해 Supabase Anonymous Auth를 사용합니다.

## 3. Supabase 데이터베이스와 Storage 만들기

1. Supabase 프로젝트 왼쪽 메뉴에서 **SQL Editor**를 엽니다.
2. GitHub repository에서 다음 파일을 엽니다.

```text
supabase/migrations/202608190001_init.sql
```

3. 파일 내용을 전부 복사합니다.
4. Supabase SQL Editor에 붙여넣습니다.
5. **Run**을 누릅니다.

성공하면 다음이 자동으로 만들어집니다.

- `translation_jobs` 테이블
- `documents` 비공개 Storage bucket
- 사용자가 자기 작업만 볼 수 있게 하는 RLS 정책

Supabase Dashboard에서 Table Editor를 열었을 때 `translation_jobs`가 보이면 정상입니다.
Storage를 열었을 때 `documents` bucket이 보이면 정상입니다.

## 4. Supabase에서 필요한 값 확인하기

다음 세 값을 준비합니다.

### A. Project reference

Supabase 프로젝트 URL이 예를 들어

```text
https://abcdefghijk.supabase.co
```

이면 project reference는

```text
abcdefghijk
```

입니다.

### B. Publishable key

Supabase의 API Keys 화면에서 `sb_publishable_...` 형태의 **Publishable key**를 복사합니다.
이 값은 웹페이지에 들어가는 공개용 키입니다.

### C. Secret key

같은 화면에서 `sb_secret_...` 형태의 **Secret key**를 만듭니다/복사합니다.
이 키는 관리자 권한이 있으므로 웹페이지 코드에 절대 넣지 않습니다.
GitHub Actions secret에만 넣습니다.

## 5. Supabase Personal Access Token 만들기

Edge Functions를 GitHub Actions에서 자동 배포하기 위해 필요합니다.

1. Supabase 계정 설정에서 **Access Tokens**로 들어갑니다.
2. 새 Personal Access Token을 만듭니다.
3. 생성된 값을 복사해 둡니다.

이 값도 GitHub secret에만 저장합니다.

## 6. GitHub Actions를 호출할 전용 GitHub token 만들기

Supabase Edge Function이 번역 요청을 받으면 GitHub Actions의 PDF worker를 실행해야 합니다.

GitHub에서 **Fine-grained personal access token**을 하나 만듭니다.

설정은 다음처럼 합니다.

- Repository access: 지금 만든 `pdf-translator` repository만 선택
- Repository permissions → **Actions: Read and write**

다른 repository 권한은 줄 필요가 없습니다.

생성된 token을 복사해 둡니다.

## 7. 관리자 비밀번호 역할의 긴 문자열 하나 정하기

예를 들어 비밀번호 관리자에서 무작위 문자열을 생성합니다.

```text
예: 아주 길고 추측하기 어려운 문자열
```

이 값의 이름은 `ADMIN_TOKEN`으로 사용합니다.
관리자 페이지에서 업로드된 원본/결과 PDF 목록을 볼 때 입력합니다.

## 8. OpenAI API key 준비하기

번역에 사용할 OpenAI API key를 준비합니다.
이 값 역시 GitHub 코드에는 넣지 않고 GitHub Actions secret에만 저장합니다.

## 9. GitHub repository에 Secret 6개 넣기

repository에서

**Settings → Secrets and variables → Actions → New repository secret**

으로 들어갑니다.

다음 **6개**를 정확한 이름으로 만듭니다.

```text
SUPABASE_PROJECT_REF
SUPABASE_ACCESS_TOKEN
SUPABASE_SECRET_KEY
GITHUB_ACTIONS_TOKEN
ADMIN_TOKEN
OPENAI_API_KEY
```

각 값은 앞 단계에서 준비한 값을 넣습니다.

## 10. Supabase Edge Functions 자동 배포하기

GitHub repository에서

**Actions → Deploy Supabase backend → Run workflow**

를 누릅니다.

초록색 체크 표시가 뜨면 다음 세 Edge Function이 Supabase에 배포된 것입니다.

```text
create-job
start-job
admin-jobs
```

그리고 GitHub secret에 넣었던 관리자 token과 GitHub Actions token도 Supabase의 Function secret으로 자동 등록됩니다.

Supabase Dashboard의 **Edge Functions** 화면에서 위 세 함수가 보이면 정상입니다.

## 11. GitHub Pages가 Supabase를 찾게 설정하기

GitHub에서

```text
docs/config.js
```

를 엽니다.

다음 두 줄만 수정합니다.

```javascript
SUPABASE_URL: "https://YOUR-PROJECT-REF.supabase.co",
SUPABASE_PUBLISHABLE_KEY: "sb_publishable_REPLACE_ME"
```

예를 들어 project reference가 `abcdefghijk`라면

```javascript
SUPABASE_URL: "https://abcdefghijk.supabase.co",
SUPABASE_PUBLISHABLE_KEY: "sb_publishable_실제값"
```

으로 바꿉니다.

**Secret key가 아니라 Publishable key를 넣어야 합니다.**

Commit changes를 누릅니다.

## 12. GitHub Pages 켜기

repository에서

**Settings → Pages**

으로 이동합니다.

설정:

```text
Source: Deploy from a branch
Branch: main
Folder: /docs
```

으로 지정하고 Save를 누릅니다.

사이트 주소는 보통 다음 형태입니다.

```text
https://GITHUB-ID.github.io/pdf-translator/
```

이 주소가 일반 사용자에게 공유할 웹사이트입니다.

---

# 사용자가 하는 일

사용자는 웹사이트에서 다음만 합니다.

1. PDF 선택
2. 번역 언어 선택
3. `번역 시작`
4. 완료될 때까지 페이지를 열어 둠
5. `PDF 다운로드`

내부에서는 다음 순서로 처리됩니다.

```text
GitHub Pages
  ↓
Supabase Anonymous Auth
  ↓
Supabase Edge Function
  ↓
Supabase Storage에 원본 저장
  ↓
GitHub Actions worker 실행
  ↓
PyMuPDF → OpenAI 번역 → XeLaTeX
  ↓
Supabase Storage에 결과 저장
  ↓
Postgres 작업 상태 = done
  ↓
사용자가 결과 다운로드
```

GitHub Actions worker가 시작될 때까지 약간의 대기 시간이 생길 수 있습니다.

---

# 업로드된 PDF를 관리자가 보는 방법

사이트 오른쪽 위의 **관리자**를 누르거나 다음 주소로 들어갑니다.

```text
https://GITHUB-ID.github.io/pdf-translator/admin.html
```

`ADMIN_TOKEN` 입력란에 GitHub Actions secret에 넣었던 것과 같은 관리자 token을 입력하고 **목록 불러오기**를 누릅니다.

여기에서 다음을 확인할 수 있습니다.

- 원본 파일명
- 처리 상태
- 번역 언어
- 페이지 수
- 번역한 텍스트 영역 수
- 오류
- 원본 PDF
- 번역 결과 PDF

파일 자체는 GitHub에 저장되지 않고 **Supabase Storage의 private `documents` bucket**에 저장됩니다.

Supabase Dashboard → Storage → `documents`에서도 실제 파일을 직접 볼 수 있습니다.
Supabase Dashboard → Table Editor → `translation_jobs`에서는 작업 상태를 직접 볼 수 있습니다.

---

# 번역 결과를 만드는 방법

현재 1차 데모는 다음 원칙으로 처리합니다.

1. **수식 보존**: 수식으로 판단되는 영역은 번역 대상으로 잡지 않습니다.
2. **형식 유지**: 원본 페이지를 고해상도 배경으로 유지합니다.
3. **텍스트 교체**: 자연어 영역만 가린 후 같은 위치에 번역문을 배치합니다.
4. **겹침 방지**: 원래 bounding box보다 번역문이 커지면 XeLaTeX `adjustbox`를 이용해 영역 안으로 맞춥니다.

따라서 수식/그림/표는 원본과 시각적으로 동일하게 유지되는 편입니다.

---

# 현재 제한

- 최대 파일 크기: 20 MB
- 최대 페이지: 20페이지
- 텍스트를 선택할 수 있는 born-digital PDF 대상
- 스캔 PDF OCR은 아직 없음
- 복잡한 색 배경 위의 글자는 흰색 masking 흔적이 보일 수 있음
- GitHub Actions가 worker라서 즉시 실행되는 전용 서버보다 대기시간이 길 수 있음
- 여러 사용자가 동시에 많이 요청하는 production 서비스에는 별도의 장기 실행 worker/queue 구조가 더 적합함

---

# 중요 보안 규칙

`docs/config.js`에는 다음 두 값만 들어갈 수 있습니다.

- Supabase URL
- Supabase **Publishable** key

다음 값은 절대로 `docs/`나 다른 공개 GitHub 코드에 적지 않습니다.

- `SUPABASE_SECRET_KEY`
- `SUPABASE_ACCESS_TOKEN`
- `GITHUB_ACTIONS_TOKEN`
- `ADMIN_TOKEN`
- `OPENAI_API_KEY`

이 값들은 GitHub repository의 Actions Secrets에만 저장합니다.
