# On-demand worker setup

1. Create one GitHub fine-grained personal access token:
   - Resource owner: kkh0412
   - Repository access: only pdf-translator
   - Repository permission: Actions -> Read and write
   - Expiration: No expiration

2. Supabase Dashboard -> Edge Functions -> Secrets
   - Name: GH_ACTIONS_TOKEN
   - Value: the token

3. Supabase Dashboard -> Edge Functions -> Deploy a new function
   - Function name: trigger-worker
   - Paste the contents of:
     supabase/functions/trigger-worker/index.ts
   - Deploy

The token stays inside Supabase. Never put it in docs/config.js or browser JavaScript.
