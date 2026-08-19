const config = window.PDF_TRANSLATOR_CONFIG || {};
const SUPABASE_URL = String(config.SUPABASE_URL || '').replace(/\/$/, '');
const SUPABASE_PUBLISHABLE_KEY = String(config.SUPABASE_PUBLISHABLE_KEY || '');
const configured = SUPABASE_URL.startsWith('https://') &&
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
const spinner = document.getElementById('spinner');
const resultBox = document.getElementById('resultBox');
const resultMeta = document.getElementById('resultMeta');
const downloadBtn = document.getElementById('downloadBtn');
const errorBox = document.getElementById('errorBox');
const setupWarning = document.getElementById('setupWarning');

let supabase = null;
let currentJob = null;

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
  if (file.size > 20 * 1024 * 1024) {
    showError('현재 데모에서는 20 MB 이하 PDF만 업로드할 수 있습니다.');
    return;
  }
  const dt = new DataTransfer();
  dt.items.add(file);
  input.files = dt.files;
  fileLabel.textContent = file.name;
  errorBox.classList.add('hidden');
}

async function ensureAnonymousSession() {
  const { data } = await supabase.auth.getSession();
  if (data.session) return data.session;
  const { data: signed, error } = await supabase.auth.signInAnonymously();
  if (error) throw new Error(`익명 로그인 실패: ${error.message}`);
  return signed.session;
}

async function invokeFunction(name, body) {
  const { data, error } = await supabase.functions.invoke(name, { body });
  if (error) {
    let detail = error.message;
    try {
      const response = error.context;
      if (response && typeof response.json === 'function') {
        const payload = await response.json();
        detail = payload.error || payload.message || detail;
      }
    } catch (_) {}
    throw new Error(detail);
  }
  if (data?.error) throw new Error(data.error);
  return data;
}

async function poll(jobId) {
  while (true) {
    const { data: job, error } = await supabase
      .from('translation_jobs')
      .select('id,status,original_name,target_language,pages,translated_segments,error,result_path')
      .eq('id', jobId)
      .single();
    if (error) throw new Error(`작업 상태 확인 실패: ${error.message}`);

    currentJob = job;
    if (job.status === 'uploading') {
      statusTitle.textContent = '업로드 확인 중';
      statusText.textContent = 'Supabase Storage에 저장된 PDF를 확인하고 있습니다.';
    } else if (job.status === 'queued') {
      statusTitle.textContent = '작업 대기 중';
      statusText.textContent = 'GitHub Actions 번역 worker 실행을 기다리고 있습니다.';
    } else if (job.status === 'processing') {
      statusTitle.textContent = '번역 및 PDF 생성 중';
      statusText.textContent = '수식과 레이아웃을 보존하면서 XeLaTeX로 결과 PDF를 만들고 있습니다.';
    } else if (job.status === 'done') {
      spinner.classList.add('hidden');
      statusBox.classList.add('hidden');
      resultBox.classList.remove('hidden');
      resultMeta.textContent = `${job.pages ?? '?'}페이지 · ${job.translated_segments ?? '?'}개 텍스트 영역 처리`;
      submitBtn.disabled = false;
      return job;
    } else if (job.status === 'failed') {
      throw new Error(job.error || '번역에 실패했습니다.');
    }
    await new Promise((resolve) => setTimeout(resolve, 2500));
  }
}

async function downloadResult() {
  if (!currentJob?.result_path) return;
  const { data, error } = await supabase.storage
    .from('documents')
    .createSignedUrl(currentJob.result_path, 300, { download: `${currentJob.original_name.replace(/\.pdf$/i, '')}_translated.pdf` });
  if (error) return showError(`다운로드 주소 생성 실패: ${error.message}`);
  window.location.href = data.signedUrl;
}

input.addEventListener('change', () => {
  const file = input.files[0];
  if (file) setSelectedFile(file);
});

['dragenter', 'dragover'].forEach((name) => dropzone.addEventListener(name, (event) => {
  event.preventDefault();
  dropzone.classList.add('dragging');
}));
['dragleave', 'drop'].forEach((name) => dropzone.addEventListener(name, (event) => {
  event.preventDefault();
  dropzone.classList.remove('dragging');
}));
dropzone.addEventListener('drop', (event) => setSelectedFile(event.dataTransfer.files[0]));
downloadBtn.addEventListener('click', downloadResult);

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  if (!configured) return showError('먼저 docs/config.js에 Supabase URL과 publishable key를 입력하세요.');
  const file = input.files[0];
  if (!file) return showError('PDF 파일을 선택하세요.');

  errorBox.classList.add('hidden');
  resultBox.classList.add('hidden');
  spinner.classList.remove('hidden');
  statusBox.classList.remove('hidden');
  statusTitle.textContent = '업로드 준비 중';
  statusText.textContent = 'Supabase에서 안전한 업로드 주소를 만들고 있습니다.';
  submitBtn.disabled = true;

  try {
    await ensureAnonymousSession();
    const targetLanguage = document.getElementById('language').value;
    const created = await invokeFunction('create-job', {
      filename: file.name,
      size: file.size,
      target_language: targetLanguage,
    });

    statusTitle.textContent = 'PDF 업로드 중';
    statusText.textContent = '원본 PDF를 Supabase Storage에 저장하고 있습니다.';
    const { error: uploadError } = await supabase.storage
      .from('documents')
      .uploadToSignedUrl(created.path, created.token, file, { contentType: 'application/pdf' });
    if (uploadError) throw new Error(`PDF 업로드 실패: ${uploadError.message}`);

    const started = await invokeFunction('start-job', { job_id: created.job_id });
    if (!started?.ok) throw new Error('작업 시작 요청에 실패했습니다.');
    await poll(created.job_id);
  } catch (error) {
    showError(error.message || String(error));
  }
});

(async function init() {
  if (!configured) {
    setupWarning.classList.remove('hidden');
    submitBtn.disabled = true;
    return;
  }
  supabase = window.supabase.createClient(SUPABASE_URL, SUPABASE_PUBLISHABLE_KEY);
  try {
    await ensureAnonymousSession();
  } catch (error) {
    showError(error.message || String(error));
  }
})();
