# PDF Translator — GitHub Pages + Supabase

이 버전은 개인 access token(PAT), Supabase CLI, Edge Functions 배포가 필요 없습니다.

## 필요한 값

브라우저에 공개되는 값:
- `SUPABASE_URL`
- `SUPABASE_PUBLISHABLE_KEY`

GitHub Actions에 비밀로 저장할 값은 딱 2개입니다.
- `SUPABASE_SECRET_KEY`
- `OPENAI_API_KEY`

그리고 GitHub Actions의 Repository **Variable** 하나가 필요합니다.
- `SUPABASE_URL`

`GITHUB_*` 이름의 개인 Secret이나 만료되는 GitHub PAT은 사용하지 않습니다. Workflow 안의 `GITHUB_TOKEN`은 GitHub가 실행 때마다 자동으로 발급합니다.

## 웹에서 설정하는 순서

### 1. Supabase SQL

새 프로젝트라면 Supabase Dashboard → SQL Editor에서 아래 파일 전체를 실행합니다.

`supabase/migrations/202608190001_init.sql`

이전 버전 SQL을 이미 실행했다면 대신 아래 파일을 한 번 실행합니다.

`supabase/UPDATE_EXISTING_SUPABASE.sql`

### 2. Anonymous Sign-In 켜기

Supabase Dashboard → Authentication 설정에서 Anonymous Sign-In을 활성화합니다.

### 3. GitHub Pages 설정

`docs/config.js`에 Supabase Project URL과 Publishable key를 넣습니다.

### 4. GitHub Actions 설정

Repository → Settings → Secrets and variables → Actions에서 설정합니다.

**Variables**
- `SUPABASE_URL` = `https://<project-ref>.supabase.co`

**Secrets**
- `SUPABASE_SECRET_KEY` = Supabase Settings → API Keys의 `sb_secret_...`
- `OPENAI_API_KEY` = OpenAI API key

다른 token은 만들지 않습니다.

### 5. Pages 켜기

Repository → Settings → Pages → Deploy from a branch → `main` / `/docs`

### 6. Worker 확인

Repository → Actions → `PDF translation worker`에서 `Run workflow`를 한 번 눌러 설정이 맞는지 확인할 수 있습니다.
이후에는 5분 간격 scheduled workflow가 `queued` 작업을 자동으로 찾습니다.

## 파일 확인

별도 관리자 페이지는 두지 않았습니다.

- Supabase Dashboard → Storage → `documents`: 원본/결과 PDF
- Supabase Dashboard → Table Editor → `translation_jobs`: 작업 상태

## 지속 실행

- 사용자 GitHub PAT은 사용하지 않으므로 30일마다 갱신할 것이 없습니다.
- GitHub Actions는 GitHub가 자동 발급하는 일회성 `GITHUB_TOKEN`을 사용합니다.
- Workflow가 월 1회 heartbeat commit을 남기도록 구성했습니다.
- Worker의 정기 Supabase 조회는 프로젝트에 지속적으로 DB 요청을 만듭니다.

플랫폼의 무료 요금제 정책이나 서비스 정책 자체가 바뀌는 경우까지 영구 동작을 보장할 수는 없습니다.
