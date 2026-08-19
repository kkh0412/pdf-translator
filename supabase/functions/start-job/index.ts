import 'jsr:@supabase/functions-js/edge-runtime.d.ts'
import { adminClient, corsHeaders, json, requireUser } from '../_shared/common.ts'

Deno.serve(async (req) => {
  if (req.method === 'OPTIONS') return new Response('ok', { headers: corsHeaders })
  if (req.method !== 'POST') return json({ error: 'Method not allowed' }, 405)
  try {
    const user = await requireUser(req)
    const body = await req.json()
    const jobId = String(body.job_id || '')
    if (!/^[0-9a-f-]{36}$/i.test(jobId)) return json({ error: '잘못된 작업 ID입니다.' }, 400)

    const db = adminClient()
    const { data: job, error } = await db.from('translation_jobs').select('*').eq('id', jobId).eq('user_id', user.id).single()
    if (error || !job) return json({ error: '작업을 찾을 수 없습니다.' }, 404)
    if (!['uploading', 'failed'].includes(job.status)) return json({ ok: true, status: job.status })

    const prefix = `${user.id}/${jobId}`
    const { data: objects, error: listError } = await db.storage.from('documents').list(prefix, { limit: 20 })
    if (listError) throw listError
    const expectedName = String(job.original_path).split('/').pop()
    if (!objects?.some((item) => item.name === expectedName)) return json({ error: '업로드된 PDF를 찾을 수 없습니다.' }, 409)

    const githubToken = Deno.env.get('GITHUB_ACTIONS_TOKEN')
    const owner = Deno.env.get('GITHUB_OWNER')
    const repo = Deno.env.get('GITHUB_REPO')
    if (!githubToken || !owner || !repo) throw new Error('GitHub Actions 설정이 완료되지 않았습니다.')

    await db.from('translation_jobs').update({ status: 'queued', error: null }).eq('id', jobId)
    const response = await fetch(`https://api.github.com/repos/${owner}/${repo}/actions/workflows/process-pdf.yml/dispatches`, {
      method: 'POST',
      headers: {
        'Accept': 'application/vnd.github+json',
        'Authorization': `Bearer ${githubToken}`,
        'X-GitHub-Api-Version': '2026-03-10',
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ ref: 'main', inputs: { job_id: jobId } }),
    })
    if (!response.ok) {
      const detail = await response.text()
      await db.from('translation_jobs').update({ status: 'failed', error: `GitHub Actions dispatch failed: ${detail.slice(0, 1000)}` }).eq('id', jobId)
      return json({ error: 'GitHub Actions worker를 시작하지 못했습니다.' }, 502)
    }
    return json({ ok: true, status: 'queued' })
  } catch (error) {
    return json({ error: error instanceof Error ? error.message : String(error) }, 400)
  }
})
