import 'jsr:@supabase/functions-js/edge-runtime.d.ts'
import { adminClient, corsHeaders, json, requireUser } from '../_shared/common.ts'

const LANGUAGES = new Set(['ko', 'en', 'ja', 'zh-CN', 'fr', 'de', 'es'])
const MAX_BYTES = 20 * 1024 * 1024

Deno.serve(async (req) => {
  if (req.method === 'OPTIONS') return new Response('ok', { headers: corsHeaders })
  if (req.method !== 'POST') return json({ error: 'Method not allowed' }, 405)
  try {
    const user = await requireUser(req)
    const body = await req.json()
    const filename = String(body.filename || '').split(/[\\/]/).pop()?.trim() || 'document.pdf'
    const size = Number(body.size || 0)
    const targetLanguage = String(body.target_language || '')
    if (!filename.toLowerCase().endsWith('.pdf')) return json({ error: 'PDF 파일만 업로드할 수 있습니다.' }, 400)
    if (!Number.isFinite(size) || size <= 0 || size > MAX_BYTES) return json({ error: '파일 크기는 20 MB 이하여야 합니다.' }, 400)
    if (!LANGUAGES.has(targetLanguage)) return json({ error: '지원하지 않는 번역 언어입니다.' }, 400)

    const db = adminClient()
    const since = new Date(Date.now() - 60 * 60 * 1000).toISOString()
    const { count, error: countError } = await db.from('translation_jobs')
      .select('id', { count: 'exact', head: true })
      .eq('user_id', user.id)
      .gte('created_at', since)
    if (countError) throw countError
    if ((count || 0) >= 5) return json({ error: '데모에서는 한 사용자당 시간당 5개 작업까지만 허용합니다.' }, 429)

    const jobId = crypto.randomUUID()
    const safeName = filename.replace(/[^A-Za-z0-9._()\-가-힣一-龥ぁ-んァ-ン ]/g, '_').slice(-160)
    const path = `${user.id}/${jobId}/original_${safeName}`
    const { error: insertError } = await db.from('translation_jobs').insert({
      id: jobId,
      user_id: user.id,
      status: 'uploading',
      original_name: filename,
      target_language: targetLanguage,
      original_path: path,
    })
    if (insertError) throw insertError

    const { data: signed, error: signError } = await db.storage.from('documents').createSignedUploadUrl(path)
    if (signError) throw signError
    return json({ job_id: jobId, path: signed.path, token: signed.token })
  } catch (error) {
    return json({ error: error instanceof Error ? error.message : String(error) }, 400)
  }
})
