"""Panel de administración de Tu Deseo.

Shell responsive (hamburger + sidebar deslizante) porteado de belux-panel,
con paleta rosa/morado. Secciones:
  - Dashboard (KPIs + charts Plotly: pedidos/día, productos top, picos de demanda)
  - Pedidos (control de estados)
  - Conversaciones (chat-view con hilos completos, pausa/reanuda bot)
  - Abonos (verificación de comprobantes)
  - Escalados
  - Pausados
  - CRM
"""
import html
import secrets
import json as _json
from datetime import datetime

from fastapi import APIRouter, Form, HTTPException, Request, Query
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app import config, db

router = APIRouter(prefix="/admin", tags=["admin"])


def _require_login(request: Request) -> str:
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=302, headers={"Location": "/admin/login"})
    return user


def _fmt_dt(value: datetime | None) -> str:
    return value.strftime("%d/%m/%Y %H:%M") if value else "—"


def _wa_link(wa_id: str) -> str:
    num = str(wa_id).replace("+", "").replace("-", "").replace(" ", "")
    return f"https://wa.me/{num}"


def _time_ago(d: datetime) -> str:
    s = int((datetime.now(d.tzinfo or None) - d).total_seconds())
    if s < 60: return f"hace {s}s"
    if s < 3600: return f"hace {s // 60}min"
    if s < 86400: return f"hace {s // 3600}h"
    return f"hace {s // 86400}d"


# Paleta provisional Tu Deseo (rosa/morado) — fácil de cambiar aquí.
PALETTE = {
    "gold": "#d6336c",        # rosa primario
    "gold-dark": "#a61e4d",
    "accent": "#9c36b5",      # morado
}

BASE_CSS = f"""
<style>
  :root {{
    --bg: #0f0a0f;
    --panel: #1a1018;
    --ink: #f3eef0;
    --muted: #9a8a93;
    --line: #2a1d26;
    --gold: {PALETTE['gold']};
    --gold-dark: {PALETTE['gold-dark']};
    --accent: {PALETTE['accent']};
    --red: #e53935;
    --green: #2f9e44;
    --blue: #4c6ef5;
    --wa: #25D366;
    --shadow: 0 4px 20px rgba(0,0,0,.35);
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    background: var(--bg);
    color: var(--ink);
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
    min-height: 100vh;
  }}
  a {{ color: var(--gold); text-decoration: none; }}
  button, input, select {{ font: inherit; }}
  .shell {{ display: grid; grid-template-columns: 220px 1fr; min-height: 100vh; }}
  .side {{
    border-right: 1px solid var(--line);
    background: #14090f;
    padding: 20px 14px;
    position: sticky; top: 0; height: 100vh;
    display: flex; flex-direction: column; z-index: 100;
    transition: transform 0.25s ease;
  }}
  .brand {{ display: flex; align-items: center; gap: 10px; margin-bottom: 22px; padding: 0 4px; }}
  .mark {{
    width: 34px; height: 34px; border-radius: 8px;
    background: linear-gradient(135deg, var(--gold), var(--accent));
    color: #fff; display: grid; place-items: center;
    font-weight: 800; font-size: 16px;
  }}
  .brand strong {{ font-size: 14px; color: var(--gold); }}
  .nav {{ display: grid; gap: 4px; flex: 1; overflow-y: auto; }}
  .nav a {{
    display: flex; align-items: center; gap: 8px;
    padding: 10px 12px; border-radius: 8px; color: #9a8a93;
    font-size: 13px; font-weight: 500;
  }}
  .nav a.active, .nav a:hover {{ background: #25131c; color: #fff; }}
  .nav svg {{ width: 18px; height: 18px; stroke-width: 2; flex-shrink: 0; }}
  .logout-btn {{ margin-top: auto; }}
  .logout-btn button {{
    width: 100%; padding: 9px; border-radius: 8px; border: 1px solid #331e29;
    background: #1a1018; color: var(--muted); cursor: pointer; font-size: 12px;
    display: flex; align-items: center; gap: 6px; justify-content: center;
  }}
  .hamburger {{
    display: none;
    position: fixed; top: 12px; left: 12px; z-index: 200;
    width: 36px; height: 36px; border-radius: 8px; border: 1px solid var(--line);
    background: var(--panel); color: #fff; font-size: 18px; cursor: pointer;
    align-items: center; justify-content: center;
  }}
  .overlay {{ display: none; position: fixed; inset: 0; background: rgba(0,0,0,.6); z-index: 99; }}
  .main {{ padding: 24px 28px 40px; min-width: 0; }}
  .topbar {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; flex-wrap: wrap; gap: 8px; }}
  h1 {{ font-size: 22px; color: #fff; }}
  .sub {{ color: var(--muted); font-size: 13px; }}

  .kpis {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(155px, 1fr)); gap: 12px; margin-bottom: 22px; }}
  .kpi-card {{ background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 16px; }}
  .kpi-num {{ font-size: 28px; font-weight: 700; color: var(--gold); }}
  .kpi-label {{ font-size: 12px; color: var(--muted); margin-top: 4px; }}

  .card {{ background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 18px; margin-bottom: 14px; }}
  .card-title {{ font-size: 14px; color: var(--gold); font-weight: 600; margin-bottom: 14px; padding-bottom: 10px; border-bottom: 1px solid var(--line); display: flex; align-items: center; gap: 6px; justify-content: space-between; }}
  .chart-box {{ background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 14px; margin-bottom: 16px; }}
  .empty {{ color: #555; text-align: center; padding: 24px 0; font-size: 13px; }}

  .item {{ background: #160b11; border: 1px solid var(--line); border-radius: 10px; padding: 12px; margin-bottom: 8px; }}
  .item-row {{ display: flex; justify-content: space-between; align-items: flex-start; gap: 10px; flex-wrap: wrap; }}
  .item-title {{ font-size: 14px; color: #fff; font-weight: 600; }}
  .item-meta {{ font-size: 12px; color: var(--muted); margin-top: 3px; }}
  .item-actions {{ display: flex; gap: 6px; margin-top: 8px; flex-wrap: wrap; }}

  .btn {{ padding: 8px 16px; border-radius: 8px; border: none; font-size: 13px; font-weight: 600; cursor: pointer; }}
  .btn-gold {{ background: var(--gold); color: #fff; }}
  .btn-red {{ background: var(--red); color: #fff; }}
  .btn-green {{ background: var(--green); color: #fff; }}
  .btn-wa {{ background: var(--wa); color: #fff; }}
  .btn-sm {{ padding: 5px 10px; font-size: 11px; border-radius: 6px; border: none; font-weight: 600; cursor: pointer; }}

  .badge {{ font-size: 10px; padding: 2px 8px; border-radius: 20px; font-weight: 600; display: inline-block; }}
  .badge-gold {{ background: var(--gold); color: #fff; }}
  .badge-red {{ background: var(--red); color: #fff; }}
  .badge-green {{ background: #1b5e20; color: #a5d6a7; }}
  .badge-blue {{ background: #0d47a1; color: #90caf9; }}
  .badge-muted {{ background: #333; color: #aaa; }}

  .inp {{ width: 100%; padding: 10px 14px; border-radius: 8px; border: 1px solid #331e29; background: #160b11; color: #fff; font-size: 14px; margin-bottom: 8px; }}
  .sel {{ width: 100%; padding: 10px 14px; border-radius: 8px; border: 1px solid #331e29; background: #160b11; color: #fff; font-size: 14px; margin-bottom: 8px; appearance: none; }}
  label {{ font-size: 12px; color: var(--muted); display: block; margin-bottom: 4px; }}

  .login-box {{ max-width: 380px; margin: 80px auto; background: var(--panel); border: 1px solid var(--line); border-radius: 14px; padding: 28px; }}
  .login-box h1 {{ text-align: center; margin-bottom: 20px; }}

  .chat-bubble {{ padding: 10px 14px; border-radius: 12px; margin: 6px 0; max-width: 85%; font-size: 13px; line-height: 1.5; }}
  .chat-bot {{ background: #241326; color: #ddd; align-self: flex-start; }}
  .chat-user {{ background: var(--gold); color: #fff; align-self: flex-end; margin-left: auto; }}

  .search-box {{ position: relative; margin-bottom: 12px; }}
  .search-box .inp {{ margin-bottom: 0; padding-right: 36px; }}
  .search-clear {{ position: absolute; right: 10px; top: 10px; background: none; border: none; color: var(--muted); cursor: pointer; font-size: 16px; }}

  .toast-wrap {{ position: fixed; bottom: 20px; left: 50%; transform: translateX(-50%); z-index: 999; }}
  .toast {{ padding: 10px 20px; border-radius: 8px; font-size: 13px; font-weight: 600; display: none; }}
  .toast.ok {{ background: #1b5e20; color: #a5d6a7; border: 1px solid #2e7d32; display: block; }}
  .toast.err {{ background: #b71c1c; color: #ffcdd2; border: 1px solid #c62828; display: block; }}

  @media (max-width: 780px) {{
    .shell {{ grid-template-columns: 1fr; }}
    .side {{ position: fixed; left: 0; top: 0; height: 100vh; transform: translateX(-100%); }}
    .side.open {{ transform: translateX(0); }}
    .hamburger {{ display: flex; }}
    .overlay.open {{ display: block; }}
    .main {{ padding: 56px 12px 24px; }}
    .kpis {{ grid-template-columns: repeat(2, 1fr); }}
  }}
</style>
"""

ICONS = {
    "dashboard": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>',
    "pedidos": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M6 2L3 6v14a2 2 0 002 2h14a2 2 0 002-2V6l-3-4z"/><line x1="3" y1="6" x2="21" y2="6"/><path d="M16 10a4 4 0 01-8 0"/></svg>',
    "conversaciones": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z"/></svg>',
    "abonos": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg>',
    "escalados": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>',
    "pausados": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/></svg>',
    "crm": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 00-3-3.87"/><path d="M16 3.13a4 4 0 010 7.75"/></svg>',
    "logout": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M9 21H5a2 2 0 01-2-2V5a2 2 0 012-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/></svg>',
}

NAV_ITEMS = [
    ("dashboard", "Dashboard", "dashboard"),
    ("pedidos", "Pedidos", "pedidos"),
    ("conversaciones", "Conversaciones", "conversaciones"),
    ("abonos", "Abonos", "abonos"),
    ("escalados", "Escalados", "escalados"),
    ("pausados", "Pausados", "pausados"),
    ("crm", "CRM", "crm"),
]


def _nav(active: str) -> str:
    lines = []
    for slug, label, icon_key in NAV_ITEMS:
        cls = "active" if slug == active else ""
        lines.append(
            f'<a class="{cls}" href="/admin/{slug}">{ICONS[icon_key]} {label}</a>'
        )
    return "\n".join(lines)


def _layout(content: str, active: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{html.escape(config.TITLE)}</title>
{BASE_CSS}
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
</head>
<body>
<button class="hamburger" onclick="toggleSidebar()">☰</button>
<div class="overlay" id="overlay" onclick="toggleSidebar()"></div>
<div class="shell">
  <aside class="side" id="sidebar">
    <div class="brand"><div class="mark">TD</div><strong>{html.escape(config.BUSINESS_NAME)}</strong></div>
    <nav class="nav">{_nav(active)}</nav>
    <form class="logout-btn" method="post" action="/admin/logout"><button>{ICONS['logout']} Cerrar sesión</button></form>
  </aside>
  <main class="main">{content}</main>
</div>
<div class="toast-wrap"><div id="toast" class="toast"></div></div>
<script>
function toggleSidebar() {{
  document.getElementById('sidebar').classList.toggle('open');
  document.getElementById('overlay').classList.toggle('open');
}}
document.querySelectorAll('.nav a').forEach(function(a) {{
  a.addEventListener('click', function() {{ if(window.innerWidth <= 780) toggleSidebar(); }});
}});
function toast(msg, ok) {{
  var t = document.getElementById('toast');
  t.textContent = msg; t.className = 'toast ' + (ok ? 'ok' : 'err');
  setTimeout(function() {{ t.className = 'toast'; }}, 3000);
}}
</script>
</body>
</html>"""


def _login_layout(content: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{html.escape(config.TITLE)} — Login</title>
{BASE_CSS}
</head>
<body style="display:grid;place-items:center;min-height:100vh">{content}</body>
</html>"""


# ── Auth ──

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, msg: str = ""):
    err = f'<p style="color:#e53935;text-align:center;margin-bottom:12px">{html.escape(msg)}</p>' if msg else ""
    return _login_layout(f"""<div class="login-box">
      <h1>💋 {html.escape(config.BUSINESS_NAME)}</h1>
      {err}
      <form method="post" action="/admin/login">
        <label>Usuario</label><input class="inp" name="username" placeholder="admin" required autofocus>
        <label>Contraseña</label><input class="inp" type="password" name="password" required>
        <button class="btn btn-gold" style="width:100%;margin-top:8px">Ingresar</button>
      </form>
    </div>""")


@router.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    if not secrets.compare_digest(username, config.ADMIN_USER):
        return await login_page(request, msg="Usuario incorrecto")
    if not secrets.compare_digest(password, config.ADMIN_PASSWORD):
        return await login_page(request, msg="Contraseña incorrecta")
    request.session["user"] = username
    return RedirectResponse("/admin/dashboard", status_code=302)


@router.post("/logout", response_class=HTMLResponse)
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/admin/login", status_code=302)


# ── Dashboard ──

@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    _require_login(request)
    try:
        metrics = await db.admin_metrics()
        chart = await db.pedidos_por_dia(7)
        top = await db.productos_top(30, 10)
        picos = await db.picos_demanda(14)
    except Exception as e:
        metrics, chart, top, picos = {}, [], [], []
        db_error = str(e)
    else:
        db_error = None

    daily_json = _json.dumps({"x": [r["dia"] for r in chart], "y": [r["pedidos"] for r in chart]})
    total_req = sum(r["total"] for r in top) if top else 0
    top_json = _json.dumps([{
        "producto": r["producto"] or "—",
        "total": r["total"],
        "pct": round(r["total"] / total_req * 100, 1) if total_req else 0
    } for r in top])
    picos_json = _json.dumps({"x": [str(r["hora"]) for r in picos], "y": [r["mensajes"] for r in picos]})

    return _layout(f"""
    <div class="topbar"><h1>Dashboard</h1><span class="sub">{datetime.now(config.bot_zoneinfo()).strftime('%d/%m/%Y %H:%M')}</span></div>
    {f'<div style="background:#3a1a1a;color:#f44336;padding:10px;border-radius:8px;margin-bottom:14px;font-size:13px">⚠️ DB: {db_error}</div>' if db_error else ''}
    <div class="kpis">
      <div class="kpi-card"><div class="kpi-num">{metrics.get('pedidos_hoy') or 0}</div><div class="kpi-label">Pedidos hoy</div></div>
      <div class="kpi-card"><div class="kpi-num">{metrics.get('pedidos_semana') or 0}</div><div class="kpi-label">Pedidos esta semana</div></div>
      <div class="kpi-card"><div class="kpi-num">{metrics.get('abonos_pendientes') or 0}</div><div class="kpi-label">Abonos pendientes</div></div>
      <div class="kpi-card"><div class="kpi-num">{metrics.get('pending_escalations') or 0}</div><div class="kpi-label">Escalados activos</div></div>
    </div>
    <div class="chart-box">
      <div class="card-title">📊 Pedidos por día (7 días) <span style="font-weight:400;font-size:11px;color:var(--muted)">{len(chart)} días</span></div>
      <div id="chart-diario" style="width:100%;height:260px">{"<div class='empty'>Sin datos</div>" if not chart else ""}</div>
    </div>
    <div class="card">
      <div class="card-title">🏆 Productos más solicitados (30 días) <span style="font-weight:400;font-size:11px;color:var(--muted)">{len(top)} · {total_req} ventas</span></div>
      <div id="chart-productos" style="width:100%;height:{max(200, len(top)*36 + 60)}px">{"<div class='empty'>Sin datos</div>" if not top else ""}</div>
    </div>
    <div class="chart-box">
      <div class="card-title">⏰ Picos de demanda (mensajes por hora)</div>
      <div id="chart-picos" style="width:100%;height:240px">{"<div class='empty'>Sin datos</div>" if not picos else ""}</div>
    </div>
    <script>
    if(typeof Plotly !== 'undefined') {{
      var d = {daily_json};
      if(d && d.x && d.x.length > 0) Plotly.newPlot('chart-diario', [{{
        x: d.x, y: d.y, type: 'bar', name: 'Pedidos',
        marker: {{color: '{PALETTE['gold']}', line: {{color: '{PALETTE['gold-dark']}', width: 1}}}}
      }}], {{margin:{{t:10,r:10,b:40,l:30}},plot_bgcolor:'#1a1018',paper_bgcolor:'#1a1018',font:{{color:'#888',size:11}},showlegend:false,xaxis:{{gridcolor:'#2a1d26',tickfont:{{size:10}}}},yaxis:{{gridcolor:'#2a1d26',tickfont:{{size:10}},dtick:1}}}}, {{displayModeBar:false}});

      var sd = {top_json};
      if(sd && sd.length > 0) {{
        sd.reverse();
        Plotly.newPlot('chart-productos', [{{
          x: sd.map(function(r){{return r.total}}), y: sd.map(function(r){{return r.producto}}),
          type: 'bar', orientation: 'h',
          marker: {{color: sd.map(function(v,i){{return ['{PALETTE['gold']}','{PALETTE['accent']}'][i%2]}})}},
          text: sd.map(function(r){{return r.total+' ('+r.pct+'%)'}}), textposition:'auto', textfont:{{color:'#fff',size:11}}
        }}], {{margin:{{t:5,r:20,b:30,l:160}},plot_bgcolor:'#1a1018',paper_bgcolor:'#1a1018',font:{{color:'#888',size:11}},showlegend:false,xaxis:{{gridcolor:'#2a1d26',dtick:1}},yaxis:{{gridcolor:'#2a1d26',automargin:true}}}}, {{displayModeBar:false,responsive:true}});
      }}

      var p = {picos_json};
      if(p && p.x && p.x.length > 0) Plotly.newPlot('chart-picos', [{{
        x: p.x, y: p.y, type: 'bar',
        marker: {{color: '{PALETTE['accent']}'}}
      }}], {{margin:{{t:10,r:10,b:40,l:35}},plot_bgcolor:'#1a1018',paper_bgcolor:'#1a1018',font:{{color:'#888',size:11}},showlegend:false,xaxis:{{title:{{text:'Hora del día',font:{{size:10}}}},gridcolor:'#2a1d26',dtick:2}},yaxis:{{gridcolor:'#2a1d26'}}}}, {{displayModeBar:false}});
    }}
    </script>
    """, "dashboard")


# ── Pedidos ──

PEDIDO_ESTADOS = ["pendiente", "pagado", "aprobado", "despachado", "entregado", "cancelado"]
ESTADO_BADGE = {
    "pendiente": '<span class="badge badge-muted">Pendiente</span>',
    "pagado": '<span class="badge badge-blue">Pagado</span>',
    "aprobado": '<span class="badge badge-green">Aprobado</span>',
    "despachado": '<span class="badge badge-blue">Despachado</span>',
    "entregado": '<span class="badge badge-green">Entregado</span>',
    "cancelado": '<span class="badge badge-red">Cancelado</span>',
}


@router.get("/pedidos", response_class=HTMLResponse)
async def pedidos_page(request: Request, estado: str = Query("")):
    _require_login(request)
    try:
        pedidos = await db.list_pedidos(estado=estado or None, limit=100)
    except Exception:
        pedidos = []

    tabs = "".join(
        f'<a class="btn-sm {"btn-gold" if (estado or "")==e else ""}" style="{"color:#fff" if estado==e else "background:#25131c;color:#9a8a93"}" href="/admin/pedidos?estado={e}">{e}</a>'
        for e in PEDIDO_ESTADOS
    ) + f'<a class="btn-sm {"btn-gold" if estado=="" else ""}" style="{"color:#fff" if estado=="" else "background:#25131c;color:#9a8a93"}" href="/admin/pedidos">Todos</a>'

    rows = ""
    for p in pedidos:
        opts = "".join(f'<option value="{e}" {"selected" if e==p["estado"] else ""}>{e}</option>' for e in PEDIDO_ESTADOS)
        rows += f"""
        <div class="item">
          <div class="item-row">
            <div>
              <div class="item-title">#{p['id']} · {html.escape(p.get('nombre_cliente') or '—')}</div>
              <div class="item-meta">{html.escape(p.get('ciudad') or '—')} · 📞 {html.escape(str(p.get('wa_id') or ''))} · ${p.get('total') or 0:,}</div>
              <div class="item-meta">📅 {p.get('fecha','—')} · {ESTADO_BADGE.get(p['estado'], '')}</div>
            </div>
          </div>
          <div class="item-actions">
            <a class="btn-sm btn-wa" style="text-decoration:none" href="{_wa_link(str(p.get('wa_id') or ''))}" target="_blank">💬 WhatsApp</a>
            <select class="sel" style="width:auto;margin:0;padding:5px 10px;font-size:11px" onchange="cambiarEstado({p['id']}, this.value)">{opts}</select>
          </div>
        </div>"""

    return _layout(f"""
    <div class="topbar"><h1>🛍 Pedidos</h1><span class="sub">{len(pedidos)} registros</span></div>
    <div class="card">
      <div class="item-actions" style="margin-bottom:12px">{tabs}</div>
      {rows if rows else '<div class="empty">Sin pedidos</div>'}
    </div>
    <script>
    async function cambiarEstado(id, estado) {{
      try {{
        var r = await fetch('/admin/pedidos/' + id + '/estado', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify({{estado:estado}})}});
        var d = await r.json();
        if(d.ok) toast('Estado actualizado a ' + estado, true);
        else toast(d.error || 'Error', false);
      }} catch(e) {{ toast('Error de conexión', false); }}
    }}
    </script>
    """, "pedidos")


@router.post("/pedidos/{pedido_id}/estado", response_class=JSONResponse)
async def pedido_estado_endpoint(request: Request, pedido_id: int):
    _require_login(request)
    data = await request.json()
    estado = data.get("estado", "pendiente")
    if estado not in PEDIDO_ESTADOS:
        return {"ok": False, "error": "estado inválido"}
    await db.update_pedido_estado(pedido_id, estado)
    return {"ok": True}


# ── Conversaciones (chat-view porteado de Demo Agentico) ──

@router.get("/conversaciones", response_class=HTMLResponse)
async def conversaciones_page(request: Request, wa_id: str = Query("")):
    _require_login(request)
    try:
        threads = await db.list_conversation_threads(limit=100)
    except Exception:
        threads = []

    selected = wa_id or (threads[0]["wa_id"] if threads else "")
    messages = []
    lead = None
    if selected:
        try:
            messages = await db.list_conversation_messages(selected, limit=120)
            lead = await db.get_lead(selected)
        except Exception:
            messages, lead = [], None

    # Lista izquierda
    list_html = ""
    for t in threads:
        cls = "active" if t["wa_id"] == selected else ""
        nombre = html.escape(t.get("nombre") or t["wa_id"])
        last = html.escape((t.get("last_content") or "")[:50])
        list_html += f'<a class="item {cls}" style="text-decoration:none;display:block" href="/admin/conversaciones?wa_id={t["wa_id"]}"><div class="item-title">{nombre}</div><div class="item-meta">{last}</div></a>'

    # Chat derecho
    bubbles = ""
    for m in messages:
        cls = "chat-user" if m["role"] == "user" else "chat-bot"
        quien = "Cliente" if m["role"] == "user" else "Bot"
        bubbles += f'<div class="chat-bubble {cls}"><strong>{quien}</strong> · {_fmt_dt(m["created_at"])}<br>{html.escape(m["content"])}</div>'

    paused = bool(lead and lead.get("bot_paused"))
    pause_btn = (
        f'<button class="btn-sm btn-green" onclick="toggleBot(\'{selected}\', false)">▶ Reanudar bot</button>'
        if paused else
        f'<button class="btn-sm btn-red" onclick="toggleBot(\'{selected}\', true)">⏸ Pausar bot</button>'
    ) if selected else ""

    pausa_badge = '<span class="badge badge-red">BOT PAUSADO</span>' if paused else ""

    return _layout(f"""
    <div class="topbar"><h1>💬 Conversaciones</h1></div>
    <div style="display:grid;grid-template-columns:300px 1fr;gap:14px">
      <div class="card" style="max-height:70vh;overflow-y:auto">{list_html or '<div class="empty">Sin conversaciones</div>'}</div>
      <div class="card">
        {f'''<div class="item-row" style="align-items:center;margin-bottom:8px">
          <div><div class="item-title">{html.escape(selected)} {pausa_badge}</div>
          <div class="item-meta">{html.escape(lead.get('nombre') or '') if lead else ''}</div></div>
          <div class="item-actions">
            <a class="btn-sm btn-wa" style="text-decoration:none" href="{_wa_link(selected)}" target="_blank">💬 WhatsApp</a>
            {pause_btn}
          </div>
        </div>
        <div style="max-height:60vh;overflow-y:auto">{bubbles or '<div class="empty">Sin mensajes</div>'}</div>''' if selected else '<div class="empty">Selecciona una conversación</div>'}
      </div>
    </div>
    <script>
    async function toggleBot(wa, pause) {{
      try {{
        var r = await fetch('/admin/contacts/' + encodeURIComponent(wa) + '/' + (pause ? 'pause' : 'resume'), {{method:'POST'}});
        var d = await r.json();
        if(d.ok) {{ toast(pause ? 'Bot pausado' : 'Bot reanudado', true); setTimeout(function(){{location.reload()}}, 500); }}
        else toast('Error', false);
      }} catch(e) {{ toast('Error de conexión', false); }}
    }}
    </script>
    <style>@media (max-width:780px){{.shell + .main div[style*="grid-template-columns:300px"]{{grid-template-columns:1fr!important}}}}</style>
    """, "conversaciones")


@router.post("/contacts/{wa_id}/pause", response_class=JSONResponse)
async def pause_bot(request: Request, wa_id: str):
    _require_login(request)
    await db.set_bot_paused(wa_id, True)
    return {"ok": True}


@router.post("/contacts/{wa_id}/resume", response_class=JSONResponse)
async def resume_bot(request: Request, wa_id: str):
    _require_login(request)
    await db.reanudar_bot(wa_id)
    return {"ok": True}


# ── Abonos ──

ABONO_ESTADO_LABEL = {
    "verificado_bot": '<span class="badge badge-green">✅ Bot</span>',
    "verificado_manual": '<span class="badge badge-blue">👤 Manual</span>',
    "no_valido": '<span class="badge badge-red">❌ No válido</span>',
    "pendiente": '<span class="badge badge-muted">⏳ Pendiente</span>',
}


@router.get("/abonos", response_class=HTMLResponse)
async def abonos_page(request: Request):
    _require_login(request)
    try:
        abonos = await db.list_abonos(limit=50)
    except Exception:
        abonos = []

    rows = ""
    for a in abonos:
        comp = f'<div style="margin-top:6px"><a href="{html.escape(a.get("url_comprobante") or "#")}" target="_blank" style="color:var(--gold);font-size:11px">📎 Ver comprobante</a></div>' if a.get("url_comprobante") else ""
        monto_info = f"${a.get('monto') or 0:,}"
        if a.get("monto_esperado") and a.get("monto_declarado"):
            monto_info += f' · esperado ${a["monto_esperado"]:,} / declarado ${a["monto_declarado"]:,}'
        ref = f'<div class="item-meta">Ref: {html.escape(a.get("referencia") or "—")} · {html.escape(a.get("banco") or "—")}</div>' if a.get("referencia") else ""
        rows += f"""
        <div class="item">
          <div class="item-row">
            <div>
              <div class="item-title">{html.escape(str(a.get('telefono') or '—'))}</div>
              <div class="item-meta">{a.get('fecha','—')} · {monto_info}</div>
              {ref}
            </div>
            <div style="text-align:right">
              {ABONO_ESTADO_LABEL.get(a.get('estado'), ABONO_ESTADO_LABEL['pendiente'])}
              {comp}
            </div>
          </div>
          <div class="item-actions">
            <a class="btn-sm btn-wa" style="text-decoration:none" href="{_wa_link(str(a.get('telefono') or ''))}" target="_blank">💬 WhatsApp</a>
            <button class="btn-sm btn-green" onclick="actualizarAbono({a['id']}, 'verificado_manual', this)">👤 Verificar</button>
            <button class="btn-sm btn-red" onclick="actualizarAbono({a['id']}, 'no_valido', this)">❌ Rechazar</button>
          </div>
        </div>"""

    return _layout(f"""
    <div class="topbar"><h1>💰 Abonos</h1><span class="sub">{len(abonos)} registros</span></div>
    <div class="card">
      <div class="card-title">Últimos abonos ({len(abonos)})</div>
      {rows if rows else '<div class="empty">Sin abonos registrados</div>'}
    </div>
    <script>
    async function actualizarAbono(id, estado, btn) {{
      btn.disabled = true;
      try {{
        var r = await fetch('/admin/abonos/' + id + '/estado', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify({{estado:estado}})}});
        var d = await r.json();
        if(d.ok) {{ toast('Estado actualizado', true); setTimeout(function(){{location.reload()}}, 500); }}
        else toast('Error', false);
      }} catch(e) {{ toast('Error de conexión', false); }}
      btn.disabled = false;
    }}
    </script>
    """, "abonos")


@router.post("/abonos/{abono_id}/estado", response_class=JSONResponse)
async def abono_estado_endpoint(request: Request, abono_id: int):
    _require_login(request)
    data = await request.json()
    estado = data.get("estado", "pendiente")
    if estado not in ("pendiente", "verificado_bot", "verificado_manual", "no_valido"):
        return {"ok": False, "error": "estado inválido"}
    await db.update_abono_estado(abono_id, estado)
    return {"ok": True}


# ── Escalados ──

@router.get("/escalados", response_class=HTMLResponse)
async def escalados_page(request: Request, status: str = Query("")):
    _require_login(request)
    try:
        esc = await db.list_escalations(status=status or None, limit=100)
    except Exception:
        esc = []

    rows = ""
    for e in esc:
        rows += f"""
        <div class="item">
          <div class="item-row">
            <div>
              <div class="item-title">{html.escape(e.get('wa_id') or '—')} <span class="badge badge-gold">{html.escape(e.get('status') or '')}</span></div>
              <div class="item-meta">{_time_ago(e['created_at']) if e.get('created_at') else ''}</div>
              <div class="item-meta" style="color:#aaa;margin-top:4px">{html.escape((e.get('reason_detail') or '')[:150])}</div>
            </div>
          </div>
          <div class="item-actions">
            <a class="btn-sm btn-wa" style="text-decoration:none" href="{_wa_link(e.get('wa_id') or '')}" target="_blank">💬 WhatsApp</a>
            <a class="btn-sm btn-gold" style="text-decoration:none" href="/admin/escalados/{e['id']}">Ver detalle</a>
          </div>
        </div>"""

    return _layout(f"""
    <div class="topbar"><h1>🚨 Escalados</h1></div>
    <div class="card">
      <div class="card-title">Conversaciones escaladas ({len(esc)})</div>
      {rows if rows else '<div class="empty">No hay escalados activos 🎉</div>'}
    </div>
    """, "escalados")


@router.get("/escalados/{eid}", response_class=HTMLResponse)
async def escalado_detalle(request: Request, eid: int):
    _require_login(request)
    e = await db.get_escalation(eid)
    if not e:
        return RedirectResponse("/admin/escalados", status_code=302)
    return _layout(f"""
    <div class="topbar"><h1>Escalado #{e['id']}</h1></div>
    <div class="card">
      <div class="card-title">Detalle</div>
      <div class="item-meta">📞 {html.escape(e.get('wa_id') or '—')}</div>
      <div class="item-meta">👤 {html.escape(e.get('customer_name') or '—')}</div>
      <div class="item-meta">🏙 {html.escape(e.get('city') or '—')}</div>
      <div class="item-meta">Estado: {html.escape(e.get('status') or '')}</div>
      <div class="item-meta" style="margin-top:8px">{html.escape(e.get('reason_detail') or '')}</div>
      <div class="item-meta" style="margin-top:8px;white-space:pre-wrap;color:#888">{html.escape((e.get('conversation_excerpt') or '')[:800])}</div>
      <div class="item-actions" style="margin-top:10px">
        <a class="btn-sm btn-wa" style="text-decoration:none" href="{_wa_link(e.get('wa_id') or '')}" target="_blank">💬 WhatsApp</a>
        <button class="btn-sm btn-green" onclick="resolver({e['id']})">✓ Resolver</button>
      </div>
    </div>
    <script>
    async function resolver(id) {{
      var r = await fetch('/admin/escalados/' + id + '/update', {{method:'POST', headers:{{'Content-Type':'application/x-www-form-urlencoded'}}, body:'status=resuelto'}});
      var d = await r.json();
      if(d.ok) {{ toast('Marcado como resuelto', true); setTimeout(function(){{location.href='/admin/escalados'}}, 600); }}
    }}
    </script>
    """, "escalados")


@router.post("/escalados/{eid}/update", response_class=JSONResponse)
async def escalado_update(request: Request, eid: int, status: str = Form(...), notes: str = Form("")):
    _require_login(request)
    await db.update_escalation_status(eid, status, notes or None)
    return {"ok": True}


# ── Pausados ──

@router.get("/pausados", response_class=HTMLResponse)
async def pausados_page(request: Request):
    _require_login(request)
    try:
        p = await db.list_pausados()
    except Exception:
        p = []

    rows = ""
    for item in p:
        tel = str(item.get("telefono") or "")
        motivo = html.escape((item.get("motivo") or "")[:100])
        rows += f"""<div class="item">
          <div class="item-row" style="align-items:center">
            <div>
              <div class="item-title">{html.escape(tel)} <span class="badge badge-red">Pausado</span></div>
              <div class="item-meta">{motivo or '—'}</div>
            </div>
          </div>
          <div class="item-actions">
            <a class="btn-sm btn-wa" style="text-decoration:none" href="{_wa_link(tel)}" target="_blank">💬 WhatsApp</a>
            <button class="btn-sm btn-green" onclick="reanudarBot('{tel}')">▶ Reanudar</button>
          </div>
        </div>"""

    return _layout(f"""
    <div class="topbar"><h1>⏸ Chats Pausados</h1><span class="sub">{len(p)} contactos</span></div>
    <div class="card">
      <div class="card-title">Contactos con bot pausado</div>
      {rows if rows else '<div class="empty">No hay chats pausados 🎉</div>'}
    </div>
    <script>
    async function reanudarBot(tel) {{
      if(!confirm('¿Reactivar el bot para ' + tel + '?')) return;
      try {{
        var r = await fetch('/admin/contacts/' + encodeURIComponent(tel) + '/resume', {{method:'POST'}});
        var d = await r.json();
        if(d.ok) {{ toast('Bot reanudado ✅', true); setTimeout(function(){{location.reload()}}, 500); }}
        else toast('Error', false);
      }} catch(e) {{ toast('Error de conexión', false); }}
    }}
    </script>
    """, "pausados")


# ── CRM ──

@router.get("/crm", response_class=HTMLResponse)
async def crm_page(request: Request, status: str = Query("")):
    _require_login(request)
    try:
        leads = await db.list_leads(status=status or None, limit=200)
    except Exception:
        leads = []

    rows = ""
    for l in leads:
        rows += f"""
        <div class="item">
          <div class="item-row">
            <div>
              <div class="item-title">{html.escape(l.get('nombre') or l.get('wa_id') or '—')}</div>
              <div class="item-meta">📞 {html.escape(l.get('wa_id') or '')} · {html.escape(l.get('qualification_status') or '')}</div>
            </div>
          </div>
        </div>"""

    return _layout(f"""
    <div class="topbar"><h1>👥 CRM</h1><span class="sub">{len(leads)} leads</span></div>
    <div class="card">
      <div class="card-title">Leads</div>
      {rows if rows else '<div class="empty">Sin leads</div>'}
    </div>
    """, "crm")


# ── Root / redirect ──

@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def admin_root():
    return RedirectResponse("/admin/dashboard", status_code=302)
