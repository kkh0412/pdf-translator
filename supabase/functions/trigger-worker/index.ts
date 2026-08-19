import { withSupabase } from 'npm:@supabase/server@^1'

const GITHUB_OWNER = 'kkh0412'
const GITHUB_REPO = 'pdf-translator'
const GITHUB_WORKFLOW = 'process-pdf.yml'
const GITHUB_REF = 'main'

function json(data: unknown, status = 200) {
  return Response.json(data, { status })
}

export default {
  fetch: withSupabase({ auth: 'user' }, async (req, ctx) => {
    if (req.method !== 'POST') {
      return json({ error: 'Method not allowed' }, 405)
    }

    let body: { job_id?: string }
    try {
      body = await req.json()
    } catch {
      return json({ error: 'Invalid JSON body' }, 400)
    }

    const jobId = String(body.job_id || '').trim()
    if (!/^[0-9a-fA-F-]{36}$/.test(jobId)) {
      return json({ error: 'Invalid job_id' }, 400)
    }

    // RLS-scoped user client: users can only see/trigger their own job.
    const { data: job, error: jobError } = await ctx.supabase
      .from('translation_jobs')
      .select('id,status,user_id')
      .eq('id', jobId)
      .maybeSingle()

    if (jobError) {
      console.error('job lookup failed', jobError)
      return json({ error: 'Could not read translation job' }, 500)
    }

    if (!job) return json({ error: 'Job not found' }, 404)
    if (job.status === 'done') return json({ ok: true, already_done: true })
    if (job.status === 'processing') {
      return json({ ok: true, already_processing: true })
    }
    if (job.status !== 'queued') {
      return json({ error: `Job is not dispatchable from status ${job.status}` }, 409)
    }

    const token = Deno.env.get('GH_ACTIONS_TOKEN')
    if (!token) {
      console.error('GH_ACTIONS_TOKEN is not configured')
      return json({ error: 'Worker trigger is not configured' }, 500)
    }

    const endpoint =
      `https://api.github.com/repos/${GITHUB_OWNER}/${GITHUB_REPO}` +
      `/actions/workflows/${GITHUB_WORKFLOW}/dispatches`

    const response = await fetch(endpoint, {
      method: 'POST',
      headers: {
        'Accept': 'application/vnd.github+json',
        'Authorization': `Bearer ${token}`,
        'X-GitHub-Api-Version': '2026-03-10',
        'Content-Type': 'application/json',
        'User-Agent': 'pdf-translator-supabase-worker-trigger',
      },
      body: JSON.stringify({
        ref: GITHUB_REF,
        inputs: { job_id: jobId },
      }),
    })

    if (!response.ok) {
      const detail = await response.text()
      console.error('GitHub dispatch failed', response.status, detail)
      return json(
        {
          error: 'GitHub worker dispatch failed',
          github_status: response.status,
        },
        502,
      )
    }

    let github: unknown = null
    const contentType = response.headers.get('content-type') || ''
    if (contentType.includes('application/json')) {
      try {
        github = await response.json()
      } catch {
        github = null
      }
    }

    return json({ ok: true, dispatched: true, github })
  }),
}
