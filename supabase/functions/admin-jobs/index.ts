import 'jsr:@supabase/functions-js/edge-runtime.d.ts'
import { adminClient, corsHeaders, json } from '../_shared/common.ts'

Deno.serve(async (req) => {
  if (req.method === 'OPTIONS') return new Response('ok', { headers: corsHeaders })
  if (req.method !== 'GET') return json({ error: 'Method not allowed' }, 405)
  const expected = Deno.env.get('ADMIN_TOKEN') || ''
  const supplied = req.headers.get('x-admin-token') || ''
  if (!expected || supplied !== expected) return json({ error: '관리자 인증에 실패했습니다.' }, 401)
  try {
    const db = adminClient()
    const { data: jobs, error } = await db.from('translation_jobs').select('*').order('created_at', { ascending: false }).limit(200)
    if (error) throw error
    const result = []
    for (const job of jobs || []) {
      let originalUrl = null
      let resultUrl = null
      if (job.original_path) {
        const { data } = await db.storage.from('documents').createSignedUrl(job.original_path, 600)
        originalUrl = data?.signedUrl || null
      }
      if (job.result_path) {
        const { data } = await db.storage.from('documents').createSignedUrl(job.result_path, 600)
        resultUrl = data?.signedUrl || null
      }
      result.push({ ...job, original_url: originalUrl, result_url: resultUrl })
    }
    return json({ jobs: result })
  } catch (error) {
    return json({ error: error instanceof Error ? error.message : String(error) }, 500)
  }
})
