const config = window.PDF_TRANSLATOR_CONFIG || {};
const SUPABASE_URL = String(config.SUPABASE_URL || '').replace(/\/$/, '');
const SUPABASE_PUBLISHABLE_KEY = String(config.SUPABASE_PUBLISHABLE_KEY || '');

const configured =
  SUPABASE_URL.startsWith('https://') &&
  !SUPABASE_URL.includes('YOUR-PROJECT-REF') &&
  SUPABASE_PUBLISHABLE_KEY.startsWith('sb_publishable_') &&
  !SUPABASE_PUBLISHABLE_KEY.includes('REPLACE_ME');

const form = document.getElementById('uploadForm');
const input = document.getElementById('pdfInput');
const dropzone = document.getElementById('dropzone');
const fileLabel = document.getElementById('fileLabel');
const submitBtn = document.getElementById('submitBtn');
const statusBox = document.getElementById('statusBox');
const statusTitle = document.getElementById('statusTitle');
const statusText = document.getElementById('statusText');
const progressValue = document.getElementById('progressValue');
const progressTrack = document.getElementById('progressTrack');
const progressFill = document.getElementById('progressFill');
const spinner = document.getElementById('spinner');
const resultBox = document.getElementById('resultBox');
const resultMeta = document.getElementById('resultMeta');
const downloadBtn = document.getElementById('downloadBtn');
const errorBox = document.getElementById('errorBox');
const setupWarning = document.getElementById('setupWarning');

let currentJob = null;
let supabaseClient = null;
let processingStartedAt = null;

function updateProgress(value, message = null) {
  const numeric = Number(value);
  const progress = Number.isFinite(numeric)
    ? Math.max(0, Math.min(100, Math.round(numeric)))
    : 0;

  progressValue.textContent = `${progress}%`;
  progressFill.style.width = `${progress}%`;
  progressTrack.setAttribute('aria-valuenow', String(progress));

  if (message) statusText.textContent = message;
}

function formatElapsed(ms) {
  const totalSeconds = Math.max(0, Math.floor(ms / 1000));
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return minutes > 0
    ? `${minutes}분 ${String(seconds).padStart(2, '0')}초`
    : `${seconds}초`;
}

function detailedMessage(job, fallback) {
  const base = job.progress_message || fallback;
  if (!processingStartedAt) return base;
  return `${base} · 경과 ${formatElapsed(Date.now() - processingStartedAt)}`;
}

function showError(message) {
  errorBox.textContent = message;
  errorBox.classList.remove('hidden');
  statusBox.classList.add('hidden');
  resultBox.classList.add('hidden');
  submitBtn.disabled = false;
}

function setSelectedFile(file) {
  if (!file) return;

  if (file.type !== 'application/pdf' && !file.name.toLowerCase().endsWith('.pdf')) {
    showError('PDF 파일만 업로드할 수 있습니다.');
    return;
  }

  if (file.size > 50 * 1024 * 1024) {
    showError('현재 데모에서는 50 MB 이하 PDF만 업로드할 수 있습니다.');
    return;
  }

  const dt = new DataTransfer();
  dt.items.add(file);
  input.files = dt.files;
  fileLabel.textContent = file.name;
  errorBox.classList.add('hidden');
}

async function ensureAnonymousSession() {
  if (!supabaseClient) {
    throw new Error('Supabase client가 초기화되지 않았습니다. 페이지를 새로고침해 주세요.');
  }

  const { data, error: sessionError } = await supabaseClient.auth.getSession();
  if (sessionError) {
    throw new Error(`세션 확인 실패: ${sessionError.message}`);
  }

  if (data.session) return data.session;

  const { data: signed, error } = await supabaseClient.auth.signInAnonymously();
  if (error) throw new Error(`익명 로그인 실패: ${error.message}`);

  if (!signed.session) {
    throw new Error('익명 로그인 세션을 생성하지 못했습니다.');
  }

  return signed.session;
}

async function triggerWorkerNow(jobId) {
  let lastError = null;

  for (let attempt = 1; attempt <= 2; attempt += 1) {
    try {
      updateProgress(
        3,
        attempt === 1
          ? '업로드 완료 · 번역 worker를 즉시 시작하고 있습니다.'
          : 'Worker 시작 요청을 한 번 더 시도하고 있습니다.'
      );

      const { data, error } = await supabaseClient.functions.invoke(
        'trigger-worker',
        {
          body: { job_id: jobId },
        }
      );

      if (error) throw error;
      if (!data?.ok) {
        throw new Error(data?.error || 'Worker 시작 요청에 실패했습니다.');
      }

      updateProgress(
        3,
        data.already_processing
          ? 'Worker가 이미 이 문서를 처리하고 있습니다.'
          : 'Worker 실행 요청 완료 · GitHub runner가 시작되기를 기다리고 있습니다.'
      );
      return true;
    } catch (error) {
      lastError = error;
      if (attempt < 2) {
        await new Promise((resolve) => setTimeout(resolve, 1000));
      }
    }
  }

  console.warn('Immediate worker dispatch failed:', lastError);
  updateProgress(
    2,
    '즉시 실행 요청에 실패했습니다. 15분 간격의 복구 worker가 대기열을 확인합니다.'
  );
  return false;
}

async function poll(jobId) {
  while (true) {
    const { data: job, error } = await supabaseClient
      .from('translation_jobs')
      .select('*')
      .eq('id', jobId)
      .single();

    if (error) throw new Error(`작업 상태 확인 실패: ${error.message}`);

    currentJob = job;

    const hasDetailedProgress =
      Object.prototype.hasOwnProperty.call(job, 'progress') &&
      Object.prototype.hasOwnProperty.call(job, 'progress_message');

    if (job.status === 'queued') {
      statusTitle.textContent = '작업 대기 중';
      processingStartedAt = null;
      updateProgress(
        hasDetailedProgress ? (job.progress ?? 2) : 2,
        hasDetailedProgress
          ? (job.progress_message ||
              '번역 worker가 대기열을 확인하고 있습니다.')
          : '작업 대기 중 · 상세 진행률 DB 설정은 worker가 시작된 뒤 확인됩니다.'
      );
    } else if (job.status === 'processing') {
      statusTitle.textContent = '번역 및 PDF 생성 중';
      if (!processingStartedAt) processingStartedAt = Date.now();

      if (hasDetailedProgress) {
        updateProgress(
          job.progress ?? 4,
          detailedMessage(
            job,
            '문서 구조와 수식을 분석하고 번역한 뒤 LaTeX로 다시 조판하고 있습니다.'
          )
        );
      } else {
        // Do not show a fake 5% forever. Make the missing DB migration explicit.
        progressValue.textContent = '진행 중';
        progressFill.style.width = '12%';
        progressTrack.removeAttribute('aria-valuenow');
        statusText.textContent =
          '상세 진행률 컬럼이 아직 Supabase에 없습니다. ' +
          'supabase/UPDATE_EXISTING_SUPABASE.sql을 한 번 실행하면 페이지·번역 묶음별 진행률이 표시됩니다.';
      }
    } else if (job.status === 'done') {
      updateProgress(100, '번역 PDF가 준비되었습니다.');
      spinner.classList.add('hidden');
      statusBox.classList.add('hidden');
      resultBox.classList.remove('hidden');
      resultMeta.textContent = `${job.pages ?? '?'}페이지 · ${job.translated_segments ?? '?'}개 텍스트 영역 처리`;
      submitBtn.disabled = false;
      return job;
    } else if (job.status === 'failed') {
      throw new Error(job.error || '번역에 실패했습니다.');
    }

    await new Promise((resolve) => setTimeout(resolve, 1500));
  }
}

async function downloadResult() {
  if (!currentJob?.result_path) return;

  const filename = `${currentJob.original_name.replace(/\.pdf$/i, '')}_translated.pdf`;

  const { data, error } = await supabaseClient.storage
    .from('documents')
    .createSignedUrl(currentJob.result_path, 300, { download: filename });

  if (error) {
    showError(`다운로드 주소 생성 실패: ${error.message}`);
    return;
  }

  window.location.href = data.signedUrl;
}

input.addEventListener('change', () => {
  const file = input.files[0];
  if (file) setSelectedFile(file);
});

['dragenter', 'dragover'].forEach((name) =>
  dropzone.addEventListener(name, (event) => {
    event.preventDefault();
    dropzone.classList.add('dragging');
  })
);

['dragleave', 'drop'].forEach((name) =>
  dropzone.addEventListener(name, (event) => {
    event.preventDefault();
    dropzone.classList.remove('dragging');
  })
);

dropzone.addEventListener('drop', (event) => {
  setSelectedFile(event.dataTransfer.files[0]);
});

downloadBtn.addEventListener('click', downloadResult);

form.addEventListener('submit', async (event) => {
  event.preventDefault();

  if (!configured) {
    showError('먼저 docs/config.js에 Supabase URL과 publishable key를 입력하세요.');
    return;
  }

  if (!supabaseClient) {
    showError('Supabase 연결 초기화에 실패했습니다. 페이지를 새로고침해 주세요.');
    return;
  }

  const file = input.files[0];
  if (!file) {
    showError('PDF 파일을 선택하세요.');
    return;
  }

  errorBox.classList.add('hidden');
  resultBox.classList.add('hidden');
  spinner.classList.remove('hidden');
  statusBox.classList.remove('hidden');
  processingStartedAt = null;
  statusTitle.textContent = 'PDF 업로드 중';
  updateProgress(1, '원본 PDF를 Supabase Storage에 저장하고 있습니다.');
  submitBtn.disabled = true;

  try {
    const session = await ensureAnonymousSession();
    const userId = session.user.id;
    const jobId = crypto.randomUUID();
    const targetLanguage = document.getElementById('language').value;
    const originalPath = `${userId}/${jobId}/original.pdf`;

    const { error: uploadError } = await supabaseClient.storage
      .from('documents')
      .upload(originalPath, file, {
        contentType: 'application/pdf',
        upsert: false,
      });

    if (uploadError) {
      throw new Error(`PDF 업로드 실패: ${uploadError.message}`);
    }

    const { error: insertError } = await supabaseClient
      .from('translation_jobs')
      .insert({
        id: jobId,
        user_id: userId,
        status: 'queued',
        original_name: file.name,
        target_language: targetLanguage,
        original_path: originalPath,
      });

    if (insertError) {
      throw new Error(`작업 생성 실패: ${insertError.message}`);
    }

    statusTitle.textContent = 'Worker 시작 중';
    updateProgress(
      2,
      '업로드가 끝났습니다. 번역 worker를 즉시 호출합니다.'
    );

    await triggerWorkerNow(jobId);
    await poll(jobId);
  } catch (error) {
    showError(error.message || String(error));
  }
});

function initializeSupabase() {
  if (!configured) {
    setupWarning.classList.remove('hidden');
    submitBtn.disabled = true;
    return;
  }

  if (!window.supabase || typeof window.supabase.createClient !== 'function') {
    showError('Supabase JavaScript 라이브러리를 불러오지 못했습니다. 페이지를 새로고침해 주세요.');
    submitBtn.disabled = true;
    return;
  }

  supabaseClient = window.supabase.createClient(
    SUPABASE_URL,
    SUPABASE_PUBLISHABLE_KEY,
    {
      auth: {
        persistSession: true,
        autoRefreshToken: true,
        detectSessionInUrl: false,
      },
    }
  );

  ensureAnonymousSession().catch((error) => {
    showError(error.message || String(error));
  });
}

initializeSupabase();
