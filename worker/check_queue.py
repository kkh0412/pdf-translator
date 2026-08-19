from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request


def main() -> int:
    base = os.environ['SUPABASE_URL'].rstrip('/')
    key = os.environ['SUPABASE_SECRET_KEY']
    query = urllib.parse.urlencode({
        'select': 'id',
        'status': 'eq.queued',
        'order': 'created_at.asc',
        'limit': '1',
    })
    request = urllib.request.Request(
        f'{base}/rest/v1/translation_jobs?{query}',
        headers={'apikey': key, 'Accept': 'application/json'},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        rows = json.loads(response.read().decode('utf-8'))
    print('true' if rows else 'false')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
