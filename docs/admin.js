const cfg = window.PDF_TRANSLATOR_CONFIG || {};
const base = String(cfg.SUPABASE_URL || '').replace(/\/$/, '');
const publishableKey = String(cfg.SUPABASE_PUBLISHABLE_KEY || '');
const configured = base.startsWith('https://') && !base.includes('YOUR-PROJECT-REF');
const loadBtn = document.getElementById('loadBtn');
const tokenInput = document.getElementById('adminToken');
const jobsEl = document.getElementById('jobs');
const errorEl = document.getElementById('errorBox');
const setupWarning = document.getElementById('setupWarning');

if (!configured) { setupWarning.classList.remove('hidden'); loadBtn.disabled = true; }
const saved = sessionStorage.getItem('pdf_admin_token');
if (saved) tokenInput.value = saved;

function esc(value) {
  return String(value ?? '').replace(/[&<>'"]/g, (ch) => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[ch]));
}

loadBtn.addEventListener('click', async () => {
  errorEl.classList.add('hidden'); jobsEl.innerHTML = '';
  const token = tokenInput.value.trim();
  if (!token) return showError('ADMIN_TOKEN을 입력하세요.');
  sessionStorage.setItem('pdf_admin_token', token);
  loadBtn.disabled = true;
  try {
    const response = await fetch(`${base}/functions/v1/admin-jobs`, { headers: { 'x-admin-token': token, 'apikey': publishableKey } });
    const body = await response.json();
    if (!response.ok) throw new Error(body.error || '관리자 목록을 읽지 못했습니다.');
    if (!body.jobs.length) { jobsEl.innerHTML = '<p>아직 작업이 없습니다.</p>'; return; }
    jobsEl.innerHTML = body.jobs.map((job) => `
      <article class="job">
        <div class="job-head"><div><h3>${esc(job.original_name)}</h3><p>${esc(job.created_at)}</p></div><span class="badge">${esc(job.status)}</span></div>
        <p>언어: ${esc(job.target_language)} · 페이지: ${esc(job.pages ?? '-')} · 텍스트 영역: ${esc(job.translated_segments ?? '-')}</p>
        ${job.error ? `<p style="color:#8f2d22">${esc(job.error)}</p>` : ''}
        <div class="job-links">
          ${job.original_url ? `<a href="${esc(job.original_url)}" target="_blank" rel="noopener">원본 PDF 열기</a>` : ''}
          ${job.result_url ? `<a href="${esc(job.result_url)}" target="_blank" rel="noopener">번역 PDF 열기</a>` : ''}
        </div>
      </article>`).join('');
  } catch (error) { showError(error.message || String(error)); }
  finally { loadBtn.disabled = false; }
});

function showError(message) { errorEl.textContent = message; errorEl.classList.remove('hidden'); }
