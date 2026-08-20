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
let queuedStartedAt = null;
let elapsedStartedAt = null;
let elapsedTimer = null;
let activeStatusMessage = '';
let showElapsedTime = false;

function formatElapsed(ms) {
  const totalSeconds = Math.max(0, Math.floor(ms / 1000));
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return minutes > 0
    ? `${minutes}분 ${String(seconds).padStart(2, '0')}초`
    : `${seconds}초`;
}

function renderStatusMessage() {
  if (!activeStatusMessage) return;

  if (showElapsedTime && elapsedStartedAt) {
    statusText.textContent =
      `${activeStatusMessage} · 경과 ${formatElapsed(Date.now() - elapsedStartedAt)}`;
  } else {
    statusText.textContent = activeStatusMessage;
  }
}

function startLocalElapsedClock() {
  if (!elapsedStartedAt) elapsedStartedAt = Date.now();
  if (elapsedTimer !== null) return;

  // The elapsed clock is purely local. Database polling may be delayed, but
  // the displayed seconds continue smoothly in the browser.
  elapsedTimer = window.setInterval(renderStatusMessage, 250);
  renderStatusMessage();
}

function stopLocalElapsedClock() {
  if (elapsedTimer !== null) {
    window.clearInterval(elapsedTimer);
    elapsedTimer = null;
  }
}

function setStatusMessage(message, withElapsed = false) {
  if (message) activeStatusMessage = message;
  showElapsedTime = withElapsed;

  if (withElapsed) startLocalElapsedClock();
  renderStatusMessage();
}

function updateProgress(value, message = null, withElapsed = false) {
  const numeric = Number(value);
  const progress = Number.isFinite(numeric)
    ? Math.max(0, Math.min(100, Math.round(numeric)))
    : 0;

  progressValue.textContent = `${progress}%`;
  progressFill.style.width = `${progress}%`;
  progressTrack.setAttribute('aria-valuenow', String(progress));

  if (message) setStatusMessage(message, withElapsed);
}


function showError(message) {
  stopLocalElapsedClock();
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
      statusTitle.textContent = '번역 준비 중';
      if (!queuedStartedAt) queuedStartedAt = Date.now();
      if (!elapsedStartedAt) elapsedStartedAt = queuedStartedAt;

      const queuedForMs = Date.now() - queuedStartedAt;
      const dbProgress = Number(job.progress ?? 0);

      if (hasDetailedProgress && (dbProgress > 0 || job.progress_message)) {
        updateProgress(
          dbProgress,
          job.progress_message || '번역 작업을 준비하고 있습니다.',
          true
        );
      } else if (queuedForMs >= 8000) {
        updateProgress(
          Math.max(1, dbProgress),
          '현재 요청 순서를 기다리고 있습니다. 준비되는 대로 자동으로 시작합니다.',
          true
        );
      } else {
        updateProgress(
          1,
          '파일 업로드가 끝났습니다 · 번역을 준비하고 있습니다.',
          true
        );
      }
    } else if (job.status === 'processing') {
      statusTitle.textContent = '번역 중';
      queuedStartedAt = null;
      if (!elapsedStartedAt) elapsedStartedAt = Date.now();

      if (hasDetailedProgress) {
        updateProgress(
          job.progress ?? 4,
          job.progress_message ||
            '문서 구조와 수식을 살펴 번역하고, 결과를 다시 조판하고 있습니다.',
          true
        );
      } else {
        progressValue.textContent = '진행 중';
        progressFill.style.width = '12%';
        progressTrack.removeAttribute('aria-valuenow');
        setStatusMessage(
          '번역을 진행하고 있습니다. 잠시만 기다려 주세요.',
          true
        );
      }
    } else if (job.status === 'done') {
      updateProgress(100, '번역이 완료되었습니다.');
      stopLocalElapsedClock();
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
  stopLocalElapsedClock();
  queuedStartedAt = null;
  elapsedStartedAt = null;
  activeStatusMessage = '';
  showElapsedTime = false;
  statusTitle.textContent = '파일 업로드 중';
  updateProgress(1, '번역할 PDF를 업로드하고 있습니다.');
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

    queuedStartedAt = Date.now();
    elapsedStartedAt = queuedStartedAt;
    statusTitle.textContent = '번역 준비 중';
    updateProgress(
      1,
      '업로드가 완료되었습니다 · 번역을 준비하고 있습니다.',
      true
    );

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
