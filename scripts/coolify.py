"""Cliente mínimo de la API de Coolify leyendo credenciales de .env.

Nunca imprime el token. Uso:
    python coolify.py GET /api/v1/applications
    python coolify.py POST /api/v1/deploy?uuid=xxx
"""
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ENV = Path(__file__).resolve().parent.parent / '.env'


def load_env():
    out = {}
    for ln in ENV.read_text().splitlines():
        ln = ln.strip()
        if not ln or ln.startswith('#') or '=' not in ln:
            continue
        k, v = ln.split('=', 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def call(method, path, body=None):
    env = load_env()
    base = env['COOLIFY_URL'].rstrip('/')
    req = urllib.request.Request(
        base + path,
        method=method,
        data=json.dumps(body).encode() if body else None,
        headers={
            'Authorization': 'Bearer ' + env['COOLIFY_TOKEN'],
            'Accept': 'application/json',
            'Content-Type': 'application/json',
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


if __name__ == '__main__':
    method, path = sys.argv[1], sys.argv[2]
    body = json.loads(sys.argv[3]) if len(sys.argv) > 3 else None
    status, text = call(method, path, body)
    print('HTTP', status)
    try:
        print(json.dumps(json.loads(text), indent=2, ensure_ascii=False)[:4000])
    except Exception:
        print(text[:4000])
