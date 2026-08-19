import { createClient } from 'npm:@supabase/supabase-js@2'

export const corsHeaders = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Headers': 'authorization, x-client-info, apikey, content-type, x-admin-token',
  'Access-Control-Allow-Methods': 'POST, GET, OPTIONS',
}

export function json(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { ...corsHeaders, 'Content-Type': 'application/json; charset=utf-8' },
  })
}

export function serverKey(): string {
  const dictionary = Deno.env.get('SUPABASE_SECRET_KEYS')
  if (dictionary) {
    try {
      const parsed = JSON.parse(dictionary)
      if (typeof parsed.default === 'string') return parsed.default
      const first = Object.values(parsed).find((x) => typeof x === 'string')
      if (first) return first as string
    } catch (_) {}
  }
  const legacy = Deno.env.get('SUPABASE_SERVICE_ROLE_KEY')
  if (legacy) return legacy
  throw new Error('No Supabase server secret key is available')
}

export function adminClient() {
  return createClient(Deno.env.get('SUPABASE_URL')!, serverKey(), {
    auth: { persistSession: false, autoRefreshToken: false },
  })
}

export async function requireUser(req: Request) {
  const auth = req.headers.get('Authorization') || ''
  if (!auth.startsWith('Bearer ')) throw new Error('Missing authorization')
  const token = auth.slice('Bearer '.length)
  const client = adminClient()
  const { data, error } = await client.auth.getUser(token)
  if (error || !data.user) throw new Error('Invalid user session')
  return data.user
}
