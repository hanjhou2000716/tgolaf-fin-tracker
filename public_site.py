"""Safe public GitHub Pages output for the Growth dashboard.

The public site is intentionally a demo. Private portfolio values are written
to a separate build directory and are never copied into the Pages publish
directory. Supabase Auth + RLS will become the authenticated data path in the
next security phase.
"""

from copy import deepcopy
import json
import os


DEMO_DATA = {
    "schemaVersion": "public-demo-v1",
    "mode": "demo",
    "dataPolicy": "No personal asset values or holdings are published.",
    "portfolio": {
        "allocation": [
            {"label": "台股資產", "percent": 49.2},
            {"label": "美股資產", "percent": 27.8},
            {"label": "現金與基金", "percent": 15.0},
            {"label": "其它", "percent": 8.0},
        ],
        "risk": {"level": "demo", "message": "登入後查看個人風控資料"},
    },
}


DEMO_HTML = """<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="robots" content="noindex,nofollow">
  <title>Growth Dashboard · Demo</title>
  <style>
    :root { --paper:#f2f0ea; --card:#fbfaf7; --navy:#24425e; --sage:#708a7c; --orange:#c98a4b; --ink:#283d50; --muted:#6d756f; --line:#ddd9d0; }
    * { box-sizing:border-box; }
    body { margin:0; padding:28px 16px 48px; background:var(--paper); color:var(--ink); font-family:ui-sans-serif,system-ui,-apple-system,"Noto Sans TC",sans-serif; }
    main { width:min(760px,100%); margin:0 auto; }
    .brand { color:var(--navy); font-family:Georgia,"Noto Serif TC",serif; font-size:20px; letter-spacing:.12em; }
    .demo-banner { margin-top:18px; padding:18px 20px; border-radius:16px; background:var(--navy); color:#fff; border-top:4px solid var(--orange); }
    .demo-banner h1 { margin:0 0 8px; font-size:24px; }
    .demo-banner p { margin:0; color:#e6ecef; line-height:1.7; font-size:14px; }
    .card { margin-top:16px; padding:20px; background:var(--card); border:1px solid var(--line); border-top:3px solid var(--orange); border-radius:14px; box-shadow:0 8px 24px rgba(36,66,94,.05); }
    .card h2 { margin:0; font-family:Georgia,"Noto Serif TC",serif; font-size:19px; }
    .card .note { margin:8px 0 18px; color:var(--muted); font-size:13px; line-height:1.6; }
    .allocation { display:grid; gap:12px; }
    .allocation-row { display:grid; grid-template-columns:minmax(110px,1fr) 2fr 56px; gap:12px; align-items:center; font-size:13px; }
    .track { height:12px; overflow:hidden; border-radius:99px; background:#e4e3dd; }
    .fill { height:100%; border-radius:inherit; background:var(--navy); }
    .allocation-row:nth-child(2) .fill { background:#477eab; }
    .allocation-row:nth-child(3) .fill { background:var(--sage); }
    .allocation-row:nth-child(4) .fill { background:var(--orange); }
    .percent { color:var(--navy); font-variant-numeric:tabular-nums; text-align:right; font-weight:700; }
    .safe { display:flex; align-items:center; justify-content:space-between; gap:12px; padding:13px 14px; border-radius:10px; background:#edf2ed; color:#537260; font-size:13px; }
    .updated { margin-top:16px; color:var(--muted); font-size:11px; }
    footer { margin-top:24px; color:var(--muted); font-size:11px; text-align:center; }
    @media (max-width:480px) { body { padding:20px 12px 36px; } .card { padding:16px; } .allocation-row { grid-template-columns:96px 1fr 48px; gap:8px; } }
  </style>
</head>
<body>
  <main>
    <div class="brand">PRStK · SFC.e · Growth</div>
    <section class="demo-banner" aria-labelledby="demo-title">
      <h1 id="demo-title">Growth Dashboard · Demo</h1>
      <p>這是公開展示頁。為保護資產隱私，公開網址不提供個人金額、持倉、交易或風控數值。登入功能將由 Supabase Auth + RLS 提供。</p>
    </section>
    <section class="card" aria-labelledby="allocation-title">
      <h2 id="allocation-title">示範資產配置</h2>
      <p class="note">以下為固定測試資料，僅展示介面與百分比呈現方式。</p>
      <div id="allocation" class="allocation"></div>
      <div class="updated" id="updated">Demo dataset</div>
    </section>
    <section class="card" aria-labelledby="risk-title">
      <h2 id="risk-title">資料與風控狀態</h2>
      <p class="note">個人風險摘要、槓桿、維持率與壓力測試僅在驗證後 API 提供。</p>
      <div class="safe"><span>公開資料政策</span><strong>Demo only · Private by default</strong></div>
    </section>
    <footer>@2026 PRStK Lab &amp; SFC.e. | All right reserved.</footer>
  </main>
  <script>
    fetch('./data.public.json', { credentials:'omit', cache:'no-store' })
      .then((response) => response.ok ? response.json() : Promise.reject(new Error('demo data unavailable')))
      .then((payload) => {
        const rows = (payload.portfolio && payload.portfolio.allocation) || [];
        document.getElementById('allocation').innerHTML = rows.map((row) => {
          const percent = Number(row.percent || 0);
          return `<div class="allocation-row"><span>${row.label}</span><span class="track"><span class="fill" style="width:${Math.max(0, Math.min(100, percent))}%"></span></span><span class="percent">${percent.toFixed(1)}%</span></div>`;
        }).join('');
        if (payload.generatedAt) document.getElementById('updated').textContent = `Demo dataset · ${payload.generatedAt}`;
      })
      .catch(() => { document.getElementById('updated').textContent = 'Demo dataset'; });
  </script>
</body>
</html>
"""


PRIVATE_HTML_TEMPLATE = """<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="robots" content="noindex,nofollow">
  <title>Growth Dashboard · Private</title>
  <style>
    :root { --paper:#f2f0ea; --card:#fbfaf7; --navy:#24425e; --sage:#708a7c; --orange:#c98a4b; --ink:#283d50; --muted:#6d756f; --line:#ddd9d0; }
    * { box-sizing:border-box; } body { margin:0; padding:28px 16px 48px; background:var(--paper); color:var(--ink); font-family:ui-sans-serif,system-ui,-apple-system,"Noto Sans TC",sans-serif; }
    main { width:min(760px,100%); margin:0 auto; } .brand { color:var(--navy); font-family:Georgia,"Noto Serif TC",serif; font-size:20px; letter-spacing:.12em; }
    .card { margin-top:16px; padding:20px; background:var(--card); border:1px solid var(--line); border-top:3px solid var(--orange); border-radius:14px; box-shadow:0 8px 24px rgba(36,66,94,.05); }
    h1,h2 { margin:0 0 12px; font-family:Georgia,"Noto Serif TC",serif; } h1 { font-size:22px; } h2 { font-size:18px; }
    p { color:var(--muted); line-height:1.6; font-size:13px; } label { display:block; margin-top:12px; color:var(--muted); font-size:12px; }
    input { width:100%; margin-top:5px; padding:10px 11px; border:1px solid var(--line); border-radius:8px; background:#fff; color:var(--ink); font:inherit; }
    button { margin-top:14px; padding:10px 14px; border:0; border-radius:8px; background:var(--navy); color:#fff; cursor:pointer; font:inherit; } button.secondary { margin-left:8px; background:var(--sage); }
    .error { min-height:20px; color:#a24f45; font-size:12px; } .hidden { display:none !important; }
    .metrics { display:grid; grid-template-columns:repeat(3,1fr); gap:10px; } .metric { padding:13px; border-radius:10px; background:#eef2ef; }
    .metric span { display:block; color:var(--muted); font-size:11px; } .metric strong { display:block; margin-top:6px; color:var(--navy); font-size:18px; }
    .private-note { padding:12px 14px; border-radius:9px; background:#fff4df; color:#8b632d; font-size:12px; line-height:1.6; }
    @media(max-width:540px) { body { padding:20px 12px 36px; } .card { padding:16px; } .metrics { grid-template-columns:1fr; } }
  </style>
</head>
<body>
  <main>
    <div class="brand">PRStK · SFC.e · Growth · Private</div>
    <section class="card" id="loginCard">
      <h1>登入 Growth Dashboard</h1>
      <p>私有資產資料只會透過 Supabase Auth + RLS 驗證後 API 取得。此頁面不包含任何資產數字或持倉。</p>
      <form id="loginForm">
        <label>Email<input id="email" type="email" autocomplete="username" required></label>
        <label>Password<input id="password" type="password" autocomplete="current-password" required></label>
        <button type="submit">登入</button>
      </form>
      <div class="error" id="loginError" role="alert"></div>
    </section>
    <section class="card hidden" id="privateCard">
      <h1>Private portfolio</h1>
      <div class="private-note">資料來自驗證後 API；頁面不會把 service role key 放到瀏覽器。</div>
      <div class="metrics" id="metrics"></div>
      <p id="privateUpdated"></p>
      <button class="secondary" id="logoutButton" type="button">登出</button>
      <div class="error" id="privateError" role="alert"></div>
    </section>
  </main>
  <script src="https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2"></script>
  <script>
    const config = __SUPABASE_CONFIG__;
    const loginCard = document.getElementById('loginCard');
    const privateCard = document.getElementById('privateCard');
    const loginError = document.getElementById('loginError');
    const privateError = document.getElementById('privateError');
    const formatMoney = (value) => Number(value || 0).toLocaleString('zh-TW', { style:'currency', currency:'TWD', maximumFractionDigits:0 });
    const showLogin = () => { loginCard.classList.remove('hidden'); privateCard.classList.add('hidden'); };
    const showPrivate = () => { loginCard.classList.add('hidden'); privateCard.classList.remove('hidden'); };
    const apiUrl = config.functionUrl || (config.url ? `${config.url}/functions/v1/portfolio-data` : '');
    let client = null;
    if (window.supabase && config.url && config.anonKey) client = window.supabase.createClient(config.url, config.anonKey);
    const loadPrivateData = async (session) => {
      if (!apiUrl || !session) throw new Error('Private API 尚未設定或登入已過期');
      const response = await fetch(apiUrl, { headers: { Authorization:`Bearer ${session.access_token}` }, cache:'no-store' });
      if (response.status === 401) throw new Error('登入已過期，請重新登入');
      if (!response.ok) throw new Error(`Private API error (${response.status})`);
      const body = await response.json(); const portfolio = (body.data || {}).portfolio || {};
      const rows = [['淨資產', portfolio.netAsset], ['總資產', portfolio.totalAsset], ['質押借款', portfolio.totalDebt]];
      document.getElementById('metrics').innerHTML = rows.map(([label, value]) => `<div class="metric"><span>${label}</span><strong>${formatMoney(value)}</strong></div>`).join('');
      document.getElementById('privateUpdated').textContent = body.generatedAt ? `資料更新：${body.generatedAt}` : '';
    };
    document.getElementById('loginForm').addEventListener('submit', async (event) => {
      event.preventDefault(); loginError.textContent = '';
      if (!client) { loginError.textContent = 'Supabase 公開設定尚未完成'; return; }
      const { error } = await client.auth.signInWithPassword({ email:document.getElementById('email').value, password:document.getElementById('password').value });
      if (error) { loginError.textContent = '登入失敗，請確認帳號或密碼'; return; }
    });
    document.getElementById('logoutButton').addEventListener('click', () => client?.auth.signOut());
    if (client) client.auth.onAuthStateChange(async (_event, session) => {
      if (!session) { showLogin(); return; }
      showPrivate(); privateError.textContent = '';
      try { await loadPrivateData(session); } catch (error) { privateError.textContent = error.message; }
    });
    else showLogin();
  </script>
</body>
</html>
"""


def build_public_payload(generated_at: str) -> dict:
    """Return a fixed demo contract with no private portfolio fields."""
    payload = deepcopy(DEMO_DATA)
    payload["generatedAt"] = generated_at
    return payload


def build_public_status(generated_at: str) -> dict:
    """Return a freshness contract that contains no account or market values."""
    return {
        "status": "ok",
        "mode": "demo",
        "generatedAt": generated_at,
        "dataPolicy": "No personal asset values or holdings are published.",
        "freshness": {"expectedCadenceHours": 12, "staleAfterHours": 18, "timezone": "Asia/Taipei"},
        "sources": {"publicDemo": "ok"},
    }


def write_public_site(directory: str, generated_at: str) -> None:
    """Write only safe static artifacts to the Pages publish directory."""
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, "index.html"), "w", encoding="utf-8") as file:
        file.write(DEMO_HTML)
    with open(os.path.join(directory, "data.public.json"), "w", encoding="utf-8") as file:
        json.dump(build_public_payload(generated_at), file, ensure_ascii=False, indent=2)
    with open(os.path.join(directory, "status.json"), "w", encoding="utf-8") as file:
        json.dump(build_public_status(generated_at), file, ensure_ascii=False, indent=2)
    private_directory = os.path.join(directory, "private")
    os.makedirs(private_directory, exist_ok=True)
    supabase_url = os.getenv("SUPABASE_URL", "").strip()
    anon_key = os.getenv("SUPABASE_ANON_KEY", "").strip()
    function_url = os.getenv("SUPABASE_FUNCTION_URL", "").strip()
    config = json.dumps({"url": supabase_url, "anonKey": anon_key, "functionUrl": function_url}, ensure_ascii=True)
    with open(os.path.join(private_directory, "index.html"), "w", encoding="utf-8") as file:
        file.write(PRIVATE_HTML_TEMPLATE.replace("__SUPABASE_CONFIG__", config))
