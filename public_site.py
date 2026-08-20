"""Safe public GitHub Pages output for the Growth dashboard.

The public site is intentionally a demo. Private portfolio values are written
to a separate build directory and are never copied into the Pages publish
directory. Supabase Auth + RLS will become the authenticated data path in the
next security phase.
"""

from copy import deepcopy
import json
import os
import shutil


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
  <script src="https://telegram.org/js/telegram-web-app.js"></script>
  <script>
    (() => {
      const telegram = window.Telegram && window.Telegram.WebApp;
      if (telegram && telegram.initData) {
        telegram.ready();
        window.location.replace('./private/');
      }
    })();
  </script>
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
    <div class="brand">PRStK · SFC.e · Growth</div>
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


TELEGRAM_PRIVATE_HTML_TEMPLATE = """<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="robots" content="noindex,nofollow"><title>Growth Dashboard · Private</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"></script>
  <style>
    :root{--paper:#f2f0ea;--surface:#fbfaf7;--navy:#24425e;--blue:#3d6f9f;--sage:#708a7c;--orange:#c98a4b;--brick:#bf6654;--ink:#283d50;--muted:#6d756f;--line:#ddd9d0}
    *{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;padding:20px 14px 44px;background:var(--paper);color:var(--ink);font-family:ui-sans-serif,system-ui,-apple-system,"Noto Sans TC",sans-serif}main{width:min(820px,100%);margin:auto}.brand{display:flex;align-items:center;gap:11px;color:var(--navy);font:700 20px Georgia,"Noto Serif TC",serif;letter-spacing:.12em;padding:4px 2px 16px;border-bottom:1px solid var(--line)}.brand img{display:block;width:auto;object-fit:contain}.brand .prstk{height:29px;max-width:126px}.brand .sfce{height:31px;max-width:116px}.brand .divider{color:#a39e93;font-weight:400;letter-spacing:0}.brand .growth{white-space:nowrap}
    .hero{position:relative;overflow:hidden;margin-top:16px;padding:22px;border-radius:22px;background:var(--navy);border-top:4px solid var(--orange);color:#fff;box-shadow:0 10px 24px rgba(36,66,94,.15)}.hero:after{content:"";position:absolute;width:190px;height:190px;border:1px solid #ffffff55;border-radius:50%;right:-74px;top:-110px;box-shadow:0 0 0 30px #ffffff0d}.hero-header{position:relative;z-index:1;display:flex;align-items:center;justify-content:space-between;gap:12px}.eyebrow{position:relative;z-index:1;margin:0 0 7px;color:#cbd9df;font-size:11px;letter-spacing:.14em;text-transform:uppercase}.sync-meta{color:#dce9e6;font-size:11px;white-space:nowrap}.hero-kpi-stack{position:relative;z-index:1;display:flex;flex-direction:column;gap:12px}.net-value-group{min-width:0}.hero-value{display:flex;flex-wrap:wrap;align-items:baseline;gap:7px;min-width:0;font:700 clamp(34px,9vw,54px)/1.05 Georgia,"Noto Serif TC",serif;letter-spacing:-.04em}.hero-value>span{white-space:nowrap}.equity-ratio{color:#f7d6ab;font-size:clamp(15px,3vw,22px);font-weight:700;letter-spacing:-.01em}.hero-kpi-stack>.pill{align-self:flex-start;white-space:nowrap}.hero-divider{position:relative;z-index:1;margin:16px 0 15px;border-top:1px solid #ffffff38}.pill{display:inline-flex;align-items:center;min-height:35px;padding:8px 12px;border:1px solid #ffffff2c;border-radius:12px;background:#35536d;color:#f7d6ab;font-size:12px;font-weight:700}.metrics{position:relative;z-index:1;display:grid;grid-template-columns:repeat(3,1fr);gap:10px}.metric{padding-left:11px;border-left:2px solid #ffffff55}.metric span{display:block;color:#cbd9df;font-size:11px}.metric strong{display:block;margin-top:4px;color:#fff;font-size:18px}
    nav{display:grid;grid-template-columns:repeat(3,1fr);gap:9px;margin:14px 0 26px}nav a{padding:12px;border:1px solid var(--line);border-radius:13px;background:var(--surface);color:var(--navy);font-weight:700;text-align:center;text-decoration:none}nav a:active{background:#e8eee9}.section{scroll-margin-top:10px;margin-top:27px}.section-heading{display:flex;align-items:baseline;gap:10px;margin:0 3px 10px}.section-heading h2{margin:0;font:700 22px Georgia,"Noto Serif TC",serif;color:var(--navy)}.section-heading span{color:var(--orange);font-size:10px;letter-spacing:.12em;text-transform:uppercase}.card{margin-top:12px;padding:18px;background:var(--surface);border:1px solid var(--line);border-top:3px solid var(--orange);border-radius:14px;box-shadow:0 6px 18px #24354a0b}.card-title{display:flex;justify-content:space-between;gap:12px;margin-bottom:13px;font:700 17px Georgia,"Noto Serif TC",serif}.card-note{color:var(--muted);font:400 11px ui-sans-serif,sans-serif}.grid-2{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}.box{min-width:0;padding:13px;border:1px solid #e2dfd7;border-radius:11px;background:#f4f2ed;color:var(--muted);font-size:12px}.box b{display:block;margin-top:5px;color:var(--navy);font-size:19px}.box small{display:block;margin-top:4px;color:var(--muted);font-size:11px}.box .up{color:var(--brick)}.box .down{color:var(--sage)}
    .tree-toolbar{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:10px 0 9px;color:var(--muted);font-size:11px}.tree-breadcrumb{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.tree-back{border:1px solid var(--line);border-radius:999px;background:var(--surface);color:var(--navy);padding:7px 11px;font:inherit;font-weight:700;white-space:nowrap}.tree-back:disabled{opacity:.4}.tree{position:relative;min-height:390px;padding:7px;border:1px solid #dedbd3;border-radius:14px;background:#ebe8e0;overflow:hidden}.tree-node{position:absolute;display:flex;flex-direction:column;justify-content:flex-start;box-sizing:border-box;min-width:0;border:2px solid #f4f2ed;border-radius:11px;color:#fff;cursor:pointer;overflow:hidden;transition:filter .18s,transform .18s,outline .18s}.tree-node.group{padding:13px;box-shadow:inset 0 0 0 1px #ffffff2b}.tree-node.leaf{padding:11px;background:#527f9a}.tree-node:hover,.tree-node:focus-visible,.tree-node.is-selected{filter:brightness(1.08);transform:translateY(-1px);outline:2px solid var(--orange);outline-offset:-2px}.tree-node .name{display:block;font-size:14px;font-weight:800;line-height:1.3;word-break:break-word;text-shadow:0 1px 2px #24354a66}.tree-node .value{display:block;margin-top:auto;padding-top:9px;color:#f8d6a2;font-size:13px;font-weight:800;line-height:1.25;white-space:normal;word-break:break-word}.tree-node .pct{display:block;margin-top:4px;color:#e1eee8;font-size:12px;font-weight:800;line-height:1.15}.tree-node.is-compact{padding:8px}.tree-node.is-compact .name{font-size:12px}.tree-node.is-compact .value,.tree-node.is-compact .pct{font-size:11px}.tree-node.is-tiny{padding:6px}.tree-node.is-tiny .name{font-size:11px}.tree-node.is-tiny .value{font-size:10px;padding-top:4px}.tree-node.is-tiny .pct{font-size:10px}.tree-tooltip{position:fixed;z-index:30;max-width:min(320px,calc(100vw - 24px));padding:9px 11px;border:1px solid var(--orange);border-radius:9px;background:#24425e;color:#fff;box-shadow:0 8px 20px #24354a40;font-size:12px;line-height:1.45;pointer-events:none;opacity:0;visibility:hidden;transform:translate(-50%,-100%) translateY(-8px);transition:opacity .12s,transform .12s}.tree-tooltip strong{color:#f8d6a2}.tree-tooltip.is-visible{opacity:1;visibility:visible;transform:translate(-50%,-100%)}.tree-tooltip.below{transform:translate(-50%,8px)}.hint{margin-top:8px;color:var(--muted);font-size:11px}.subtle{color:var(--muted);font-size:11px}
    .risk-group{padding:15px;border:1px solid #e3dfd6;border-radius:14px;background:#f4f2ed}.risk-group+.risk-group{margin-top:12px}.risk-group-title{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:11px;font:700 17px Georgia,"Noto Serif TC",serif}.risk-group-title span{font:400 11px ui-sans-serif,sans-serif;color:var(--muted)}.risk-box{padding:15px;border:1px solid #d5ded8;border-radius:12px;background:#f8faf7}.risk-box label{display:block;color:var(--muted);font-size:12px}.risk-box strong{display:block;margin-top:6px;color:var(--navy);font-size:28px;line-height:1.1;letter-spacing:-.02em}.risk-box small{display:block;margin-top:7px;color:var(--muted);line-height:1.45;font-size:12px}.risk-box strong.alert{color:var(--brick)}.risk-box strong.good{color:var(--sage)}.detail{margin-top:11px;padding-top:10px;border-top:1px solid #d5ddd5;color:var(--muted);font-size:12px;line-height:1.65}.risk-badge{display:inline-block;margin-top:5px;padding:4px 9px;border-radius:999px;background:#eadcad;color:#936a28;font-weight:800}.stress{display:grid;grid-template-columns:repeat(2,1fr);gap:10px;margin-top:12px}.stress .box{min-height:0}.stress .box b{color:var(--brick)}
    .chart-wrap{position:relative;height:260px}.range{display:flex;flex-wrap:wrap;gap:7px;margin-bottom:10px}.range button{border:1px solid var(--line);background:transparent;color:var(--muted);padding:6px 9px;font:11px inherit}.range button.active{background:#e8eee9;color:var(--navy);border-color:#bfcfc3}.goal-track{height:8px;margin:12px 0 8px;border-radius:99px;background:#e5e2db;overflow:hidden}.goal-fill{height:100%;background:var(--sage);border-radius:inherit}.health-card{padding:0;overflow:hidden}.health-card > summary.health-summary{display:flex;align-items:center;justify-content:space-between;gap:12px;min-height:46px;margin:0;padding:0 18px;cursor:pointer;list-style:none}.health-card > summary.health-summary::-webkit-details-marker{display:none}.health-card > summary.health-summary:focus-visible{outline:2px solid var(--orange);outline-offset:-2px}.health-card > summary.health-summary:after{content:'＋';color:var(--orange);font:700 18px ui-sans-serif;line-height:1}.health-card[open] > summary.health-summary:after{content:'－'}.health-card .health{margin:14px 18px 9px}.health-card #advisor{margin:0 18px 14px}.health-subsection{margin:0 18px 14px;padding-top:12px;border-top:1px solid var(--line)}.health-subsection-title{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:8px;color:var(--navy);font-size:12px;font-weight:800}.health-subsection-title .card-note{font-size:10px}.health{display:grid;grid-template-columns:repeat(3,1fr);gap:9px}.health .box strong{font-size:16px}.ingestion-list{display:grid;gap:8px}.ingestion-row{display:grid;grid-template-columns:auto 1fr auto;gap:8px;align-items:center;padding:9px 10px;border:1px solid var(--line);border-radius:10px;background:#f4f2ed;font-size:11px}.ingestion-row strong{color:var(--navy)}.ingestion-row small{display:block;color:var(--muted);margin-top:3px}.ingestion-status{font-weight:800;color:var(--sage)}.ingestion-status.applied_with_compatibility,.ingestion-status.pending{color:var(--orange)}.ingestion-status.rejected{color:var(--brick)}.error{margin-top:10px;color:#a24f45;font-size:12px}.footer{margin-top:30px;padding-top:15px;border-top:1px solid var(--line);color:var(--muted);font-size:11px;text-align:center}
    .actions{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:18px}.btn{display:block;padding:12px 10px;border:1px solid var(--navy);border-radius:11px;background:var(--navy);color:#fff;text-align:center;text-decoration:none;font-size:12px;font-weight:700;box-shadow:0 4px 10px #24425e20}.btn.secondary{background:var(--navy);color:#fff;border-color:var(--navy)}.btn:active{transform:translateY(1px)}
    @media(max-width:560px){body{padding:14px 10px 34px}.hero,.card{padding:15px}.health-card{padding:0}.hero-header{align-items:flex-start}.sync-meta{font-size:10px}.hero-kpi-stack{gap:12px}.hero-value{font-size:clamp(34px,9vw,42px);gap:5px}.equity-ratio{font-size:clamp(15px,4vw,18px)}.hero-divider{margin:14px 0 13px}.hero-kpi-stack>.pill{padding:8px 12px;font-size:12px}.pill{padding:8px 9px;font-size:10px}.metrics{gap:7px}.metric{padding-left:8px}.metric strong{font-size:15px}.grid-2,.stress{gap:8px}.risk-box{padding:12px 10px}.risk-box strong{font-size:22px}.tree{min-height:320px}.tree-toolbar{margin-top:8px}.tree-node.group{padding:8px}.tree-node.leaf{padding:8px}.tree-node .name{font-size:12px}.tree-node .value,.tree-node .pct{font-size:10px}.tree-node.is-compact{padding:6px}.tree-node.is-tiny{padding:4px}.tree-tooltip{font-size:11px}.chart-wrap{height:235px}.health-card > summary.health-summary{padding:0 15px;min-height:46px}.health-card .health{margin:14px 15px 9px}.health-card #advisor{margin:0 15px 14px}.health-subsection{margin:0 15px 12px;padding-top:11px}.health-card .ingestion-row{grid-template-columns:1fr;gap:3px;padding:8px 9px}.health-card .ingestion-row>span:last-child{color:var(--muted);font-size:10px}.health{grid-template-columns:1fr}.section-heading h2{font-size:20px}.actions{grid-template-columns:1fr}}
    @media(max-width:389px){.brand{gap:6px;font-size:17px;letter-spacing:.06em}.brand .prstk{height:23px;max-width:86px}.brand .sfce{height:23px;max-width:78px}.brand .growth{font-size:17px}}
  </style>
</head>
<body>
<main>
  <div class="brand"><img class="prstk" src="../PRStK-Remove.png" alt="PRStK"><span class="divider">|</span><img class="sfce" src="../SFC.e-removebg-preview.png" alt="SFC.e"><span class="divider">|</span><span class="growth">Growth</span></div>
  <div id="loading" class="card"><strong>Growth Dashboard</strong><p>正在從 Telegram 驗證並載入完整私有資料…</p></div>
  <div id="dashboard" hidden>
    <section class="hero"><div class="hero-header"><p class="eyebrow">Portfolio overview</p><span id="sync" class="sync-meta">同步 —</span></div><div class="hero-kpi-stack"><div class="net-value-group"><div class="hero-label">淨資產 Net</div><div class="hero-value"><span id="netAsset">—</span> <span id="equityRatio" class="equity-ratio" aria-label="淨資產占總資產 —">(—)</span></div></div><span id="dailyChange" class="pill">今日 —</span></div><div class="hero-divider" aria-hidden="true"></div><div class="metrics"><div class="metric"><span>總資產</span><strong id="totalAsset">—</strong></div><div class="metric"><span>總負債</span><strong id="totalDebt">—</strong></div><div class="metric"><span>負債比</span><strong id="debtRatio">—</strong></div></div></section>
    <nav aria-label="Growth dashboard sections"><a href="#allocation">配置</a><a href="#risk">風險</a><a href="#growth">成長</a></nav>
    <section id="allocation" class="section"><div class="section-heading"><h2>配置</h2><span>Allocation</span></div><div class="card"><div class="card-title">總資產配置 <span class="card-note">點擊分類查看下一層</span></div><div class="tree-toolbar"><span id="treeBreadcrumb" class="tree-breadcrumb">總資產</span><button id="treeBack" class="tree-back" type="button" disabled>← 返回上一層</button></div><div id="tree" class="tree" role="img" aria-label="總資產配置 Treemap"></div><div id="treeDetail" class="tree-tooltip" role="status" aria-live="polite" hidden></div><div class="hint">同色系代表同一資產類別；子資產只在點入後顯示。</div></div></section>
    <section id="risk" class="section"><div class="section-heading"><h2>風險</h2><span>Risk management</span></div><div class="card"><div class="card-title">風險摘要 <span class="card-note">Current safeguards</span></div><div class="risk-group"><div class="risk-group-title">槓桿 <span>Leverage &amp; collateral</span></div><div class="grid-2"><div class="risk-box"><label>有效 Beta</label><strong id="leverage">—</strong><div class="detail">凱利安全邊界 <b id="kelly">—</b><br><span id="betaStatus" class="risk-badge">—</span><br>容量：<b id="capacity">—</b></div></div><div class="risk-box"><label>質押借款本金</label><strong id="debtRisk" class="alert">—</strong><small id="interest">含利息 — · 風控負債 —</small><div class="detail">質押維持率 <b id="maintenance">—</b><br><span id="maintenanceStatus" class="risk-badge">—</span></div></div></div></div><div class="risk-group"><div class="risk-group-title">曝險 <span>Look-through concentration</span></div><div class="grid-2"><div class="risk-box"><label>TSMC 曝險</label><strong id="tsmc">—</strong><small>台美股 &amp; ETF 綜合曝險</small></div><div class="risk-box"><label>NVDA 曝險</label><strong id="nvda">—</strong><small>純美股 &amp; ETF 綜合曝險</small></div></div></div></div><div class="card"><div class="card-title">集中度與壓力測試</div><div class="grid-2" id="concentration"></div><div class="stress" id="stress"></div></div></section>
    <section id="growth" class="section"><div class="section-heading"><h2>成長</h2><span>Progress &amp; trajectory</span></div><div class="card"><div class="card-title">成長軌跡 <span class="card-note">淨資產報酬</span></div><div id="growthStats" class="grid-2"></div></div><div class="card"><div class="card-title">近期資產軌跡 <span class="card-note">可縮放查看更早點位</span></div><div class="range"><button data-range="30" class="active">1M</button><button data-range="90">3M</button><button data-range="365">1Y</button><button data-range="all">全部</button></div><div class="chart-wrap"><canvas id="historyChart"></canvas></div></div><div class="card goal-card"><div class="card-title goal-card-title"><span>目標進度</span><span id="goalNativeTarget" class="card-note">—</span></div><div id="goalStatus" class="subtle"></div><div id="goalLabel" class="subtle"></div><div class="goal-track"><div id="goalFill" class="goal-fill"></div></div><div id="goalForecast" class="subtle"></div></div></section>
    <section class="section"><div class="section-heading"><h2>資料健康</h2><span>Data health</span></div><details class="card health-card"><summary class="card-title health-summary"><span>資料品質檢查</span><span id="healthSummary" class="card-note">點擊展開</span></summary><div id="health" class="health"></div><div class="health-subsection"><div class="health-subsection-title"><span>最近資產更新</span><span id="transactionSummary" class="card-note">—</span></div><div id="transactionIngestion" class="ingestion-list" aria-live="polite"></div></div><div id="advisor" class="subtle"></div></details></section>
    <div class="actions"><a class="btn secondary" href="https://hanjhou2000716.github.io/skynet-monitoring/">開啟 Skynet Monitoring</a><a class="btn" href="https://forms.gle/9ZEJawwNRGfiXQiV8">新增資產資料</a></div>
    <footer class="footer">@2026 PRStK Lab &amp; SFC.e. | All right reserved.</footer>
  </div>
  <div id="privateError" class="error" role="alert"></div>
</main>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<script>
(() => {
  const config = __SUPABASE_CONFIG__;
  const apiUrl = config.functionUrl || (config.url ? `${config.url}/functions/v1/portfolio-data` : '');
  const $ = (id) => document.getElementById(id);
  const money = (v) => Number(v || 0).toLocaleString('zh-TW',{style:'currency',currency:'TWD',maximumFractionDigits:0});
  const num = (v, digits=1) => Number(v || 0).toLocaleString('zh-TW',{maximumFractionDigits:digits});
  const pct = (v) => `${Number(v || 0).toFixed(1)}%`;
  const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  let chart = null;
  let treeRoot = null; let treePath = [];
  const paletteByCategory = {
    '現貨台股':['#24425e','#315b7b','#477091','#6286a0'],
    '現貨美股':['#3d6f9f','#4e80ad','#6695bc','#7fa7c7'],
    '現金與基金':['#687c70','#7d9183','#91a596','#a6b8a8'],
    '台積電':['#b06b5d','#c27a68','#d18b76','#df9c83'],
    '台股槓桿型':['#ad743d','#bd8248','#ca9155','#d49f65']
  };
  const nodeColor = (node, depth=0) => { const key=String(node.category||node.label||''); const palettes=paletteByCategory[key]||[['#24425e','#315b7b','#477091','#6286a0'],['#3d6f9f','#4e80ad','#6695bc','#7fa7c7'],['#687c70','#7d9183','#91a596','#a6b8a8']][depth%3]; let hash=0; for(const c of String(node.label||'')) hash=(hash*31+c.charCodeAt(0))|0; return palettes[Math.abs(hash)%palettes.length]; };
  const layoutNodes = (nodes,x,y,width,height) => { const sorted=nodes.slice().sort((a,b)=>Number(b.value||0)-Number(a.value||0)); if(!sorted.length)return []; if(sorted.length===1)return [{node:sorted[0],x,y,width,height}]; const total=sorted.reduce((sum,node)=>sum+Number(node.value||0),0)||1; const first=[]; let firstTotal=0; for(let i=0;i<sorted.length-1;i+=1){first.push(sorted[i]);firstTotal+=Number(sorted[i].value||0);if(firstTotal>=total/2)break;} const second=sorted.slice(first.length); const ratio=firstTotal/total; if(width>=height){const firstWidth=width*ratio;return layoutNodes(first,x,y,firstWidth,height).concat(layoutNodes(second,x+firstWidth,y,width-firstWidth,height));} const firstHeight=height*ratio; return layoutNodes(first,x,y,width,firstHeight).concat(layoutNodes(second,x,y+firstHeight,width,height-firstHeight)); };
  const treePercent = (node) => treeRoot?.value>0 ? Number(node.value||0)*100/Number(treeRoot.value) : 0;
  const hideTooltip = (id) => { const tooltip=$(id); if(!tooltip)return; tooltip.classList.remove('is-visible','below'); tooltip.setAttribute('hidden',''); };
  const showTooltip = (id, html, element) => { const tooltip=$(id); if(!tooltip||!element)return; tooltip.innerHTML=html; tooltip.removeAttribute('hidden'); const rect=element.getBoundingClientRect(); const above=rect.top>120; tooltip.classList.toggle('below',!above); tooltip.style.left=`${rect.left+rect.width/2}px`; tooltip.style.top=`${above?rect.top:rect.bottom}px`; requestAnimationFrame(()=>tooltip.classList.add('is-visible')); };
  const showTreeDetail = (node, element) => { document.querySelectorAll('.tree-node.is-selected').forEach((item)=>item.classList.remove('is-selected')); if(element)element.classList.add('is-selected'); showTooltip('treeDetail',`<strong>${esc(node.label)}</strong> · ${money(node.value)} · ${pct(treePercent(node))}（佔總資產）`,element); };
  const renderTreeNode = (item,parent,depth=0) => { const node=item.node; const children=(node.children||[]).filter((child)=>Number(child.value||0)>0); const element=document.createElement('div'); element.className=`tree-node ${children.length?'group':'leaf'}`; if(item.width<24||item.height<24)element.classList.add('is-compact'); if(item.width<13||item.height<16)element.classList.add('is-tiny'); element.style.left=`${item.x}%`; element.style.top=`${item.y}%`; element.style.width=`${item.width}%`; element.style.height=`${item.height}%`; element.style.background=nodeColor(node,depth); element.setAttribute('role','button'); element.setAttribute('tabindex','0'); element.setAttribute('aria-label',`${node.label} ${money(node.value)}，佔總資產 ${pct(treePercent(node))}${children.length?'，點擊查看下一層':''}`); element.title=children.length?`${node.label} · 點擊查看下一層`:`${node.label} · ${money(node.value)} · ${pct(treePercent(node))}`; element.innerHTML=`<span class="name">${esc(node.label)}</span><span class="value">${money(node.value)}</span><span class="pct">${pct(treePercent(node))}</span>`; const open=()=>{if(children.length){hideTooltip('treeDetail');treePath.push(node);renderTree();}else showTreeDetail(node,element);}; element.addEventListener('click',open); element.addEventListener('pointerenter',()=>showTreeDetail(node,element)); element.addEventListener('pointerleave',()=>hideTooltip('treeDetail')); element.addEventListener('focus',()=>showTreeDetail(node,element)); element.addEventListener('blur',()=>hideTooltip('treeDetail')); element.addEventListener('keydown',(event)=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();open();}}); parent.appendChild(element); };
  const renderTree = () => { const tree=$('tree'); const root=treePath[treePath.length-1]||treeRoot; const children=(root?.children||[]).filter((node)=>Number(node.value||0)>0).sort((a,b)=>Number(b.value||0)-Number(a.value||0)); $('treeBreadcrumb').textContent=treePath.length?[treeRoot.label,...treePath.map((node)=>node.label)].join(' / '):treeRoot?.label||'總資產'; $('treeBack').disabled=treePath.length===0; tree.innerHTML=''; hideTooltip('treeDetail'); if(!children.length){tree.textContent='目前沒有可顯示的持倉';return;} layoutNodes(children,0,0,100,100).forEach((item)=>renderTreeNode(item,tree,treePath.length)); };
  const ratioText = (net,total) => { const n=Number(net); const t=Number(total); return Number.isFinite(n)&&Number.isFinite(t)&&t>0 ? '('+(n*100/t).toFixed(1)+'%)' : '(—)'; };
  const syncText = (raw) => { if(!raw)return '同步 —'; const date=new Date(raw); if(Number.isNaN(date.getTime()))return '同步 —'; const parts=new Intl.DateTimeFormat('zh-TW',{timeZone:'Asia/Taipei',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).formatToParts(date).reduce((out,item)=>(out[item.type]=item.value,out),{}); const todayParts=new Intl.DateTimeFormat('zh-TW',{timeZone:'Asia/Taipei',year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(new Date()).reduce((out,item)=>(out[item.type]=item.value,out),{}); const today=todayParts.year+'/'+todayParts.month+'/'+todayParts.day; const stamp=parts.year+'/'+parts.month+'/'+parts.day; return stamp===today ? '同步 '+parts.hour+':'+parts.minute : '同步 '+parts.month+'/'+parts.day+' '+parts.hour+':'+parts.minute; };
  const render = (body) => { const data=body.data||{}; const p=data.portfolio||{}; const risk=p.risk||{}; const netValue=Number(p.netAsset); const totalValue=Number(p.totalAsset); $('netAsset').textContent=Number.isFinite(netValue)?money(netValue):'—'; $('equityRatio').textContent=ratioText(netValue,totalValue); $('equityRatio').setAttribute('aria-label','淨資產占總資產 '+ratioText(netValue,totalValue)); $('totalAsset').textContent=money(totalValue); $('totalDebt').textContent=money(p.totalDebt); $('debtRatio').textContent=pct(risk.debtRatio); $('sync').textContent=syncText(body.generatedAt||data.lastUpdated); const perf=p.performance||{}; const diff=Number(perf.netChange); const diffPct=Number(perf.netChangePercent); const daily=$('dailyChange'); if(Number.isFinite(diff)&&Number.isFinite(diffPct)){daily.textContent='今日 '+(diff>=0?'+':'')+pct(diffPct)+' · '+(diff>=0?'+':'')+money(diff); daily.style.color=diff>=0?'#f1a08b':'#9bc1a8';}else{daily.textContent='今日 —'; daily.style.color='#dbe5e5';}
    const safety=p.pledgeSafety||{};
    treeRoot=p.assetTree||{label:'總資產',value:p.totalAsset||0,children:[]}; treePath=[]; renderTree(); $('treeBack').onclick=()=>{if(treePath.length){hideTooltip('treeDetail');treePath.pop();renderTree();}};
    $('leverage').textContent=`${num(risk.effectiveLeverage,2)} ×`; $('kelly').textContent=`${num(risk.kellyLimit||1.23,2)} ×`; $('capacity').textContent=pct(risk.betaCapacity); $('betaStatus').textContent=risk.betaStatus||'Beta 維持'; const principal=Number(p.pledgePrincipal ?? p.liabilities?.principal ?? p.totalDebt ?? 0); const riskDebt=Number(p.totalDebt ?? p.liabilities?.debt ?? 0); $('debtRisk').textContent=money(principal); $('interest').textContent=`含利息 ${money(p.liabilities?.interest||0)} · 風控負債 ${money(riskDebt)}`; $('maintenance').textContent=pct(risk.maintenanceRatio||safety.currentRatio); $('maintenanceStatus').textContent=safety.status==='healthy'?'🟢 可加槓桿':safety.status==='warning'?'🟡 注意槓桿':'🔴 補擔保品'; $('tsmc').textContent=pct(risk.tsmcExposureRatio); $('nvda').textContent=pct(risk.nvdaExposureRatio);
    const largest=risk.largestPosition||{}; $('concentration').innerHTML=`<div class="box"><span>最大單一標的（台股）</span><b>${esc(largest.symbol||'—')} · ${pct(largest.percent)}</b><small>${money(largest.value)}</small></div><div class="box"><span>美股最大單一標的</span><b>${esc(p.usLargest?.symbol||'—')} · ${pct(p.usLargest?.percent)}</b><small>${money(p.usLargest?.value)}</small></div>`; $('stress').innerHTML=(p.stressTests||[]).map(s=>`<div class="box"><span>${esc(s.label||'壓力測試')}</span><b>${money(s.netImpact||0)}</b><small>壓力後淨資產 ${money(s.netAsset||0)}${s.maintenance!=null?' · 維持率 '+pct(s.maintenance):''}</small></div>`).join('');
    const metrics=p.performanceMetrics||{}; $('growthStats').innerHTML=[['年化報酬',metrics.annualizedReturn],['年化波動',metrics.annualizedVolatility],['Sharpe',metrics.sharpe],['最大回撤',metrics.maxDrawdown]].map(x=>`<div class="box">${x[0]}<b>${x[1]==null?'—':pct(Number(x[1])*100)}</b></div>`).join(''); const history=p.history||{}; renderChart(history); const goalForecast=p.runtimeExtensions?.goalForecast||{}; const activeGoal=goalForecast.activeGoal; const rawTarget=Number(activeGoal?.targetTwdEquivalent); const rawProgress=rawTarget>0?Number(p.netAsset||0)/rawTarget*100:null; const visualProgress=Number.isFinite(rawProgress)?Math.max(0,Math.min(100,rawProgress)):0; $('goalNativeTarget').textContent=activeGoal?`${Number(activeGoal.targetAmount).toLocaleString('zh-TW')} ${activeGoal.targetCurrency}`:'—'; $('goalLabel').textContent=activeGoal&&Number.isFinite(rawProgress)?`目前進度 ${rawProgress.toFixed(1)}%`:(goalForecast.status==='completed'?'三階段資產目標已完成':'目前進度 —'); $('goalFill').style.width=`${visualProgress}%`; const displayYear=activeGoal?.displayYear||'—'; const probability=goalForecast.probability; $('goalForecast').textContent=goalForecast.status==='completed'?'目標里程碑 · 三階段資產目標已完成':`${displayYear}前達成機率 ${probability==null?'—':pct(Number(probability)*100)}`; document.querySelector('.goal-card')?.classList.toggle('is-overdue',Boolean(goalForecast.overdue));
    const health=p.runtimeExtensions?.dataHealth||data.dataQuality||{}; const ingestionHealth=p.transactionIngestion?.ingestionHealth||health.ingestionHealth||{}; const healthState=ingestionHealth.status==='DEGRADED'?`異常 · ${ingestionHealth.message||ingestionHealth.reasonCode||'資料已隔離'}`:health.stale?'需更新':'正常'; const sourceRows=Array.isArray(health.sources)?health.sources:[]; const marketSource=sourceRows.find((source)=>source&&source.name==='marketQuotes'); const marketQuality=marketSource?.quality||'unknown'; const reconciled=health.reconciled??p.pnlAttribution?.reconciled; const reconcileState=reconciled===true?'已平衡':reconciled===false?'待檢查':'未知'; const portfolioAsOf=health.portfolioDataAsOf||data.portfolioDataAsOf||'—'; $('health').innerHTML=`<div class="box"><span>資料狀態</span><strong>${esc(healthState)}</strong></div><div class="box"><span>行情來源</span><strong>${esc(marketQuality)}</strong></div><div class="box"><span>帳本對帳</span><strong>${reconcileState}</strong></div><div class="box"><span>資產資料截至</span><strong>${esc(portfolioAsOf)}</strong></div>`; const healthCard=document.querySelector('.health-card'); const updateHealthSummary=()=>{if($('healthSummary'))$('healthSummary').textContent=`${healthState} · 點擊${healthCard?.open?'收合':'展開'}`;}; const healthSummary=healthCard?.querySelector('.health-summary'); healthSummary?.addEventListener('keydown',(event)=>{if(event.key===' '||event.key==='Spacebar'){event.preventDefault();healthCard.open=!healthCard.open;updateHealthSummary();}}); updateHealthSummary(); healthCard?.addEventListener('toggle',updateHealthSummary); $('advisor').textContent=p.runtimeExtensions?.advisor?.reason||'風控建議需以最新資料與 Guardrail 為前提。'; $('loading').hidden=true; $('dashboard').hidden=false;
  };
  const renderChart=(h)=>{ if(!window.Chart||!h.dates||h.dates.length<1)return; const ctx=$('historyChart').getContext('2d'); if(chart)chart.destroy(); const datasets=[{label:'總資產',data:h.totals||[],borderColor:'#687c70',backgroundColor:'#687c70',pointRadius:0,borderWidth:2},{label:'淨資產',data:h.nets||[],borderColor:'#bf6654',backgroundColor:'#bf6654',pointRadius:0,borderWidth:2},{label:'總資產月線',data:h.totalMonthly||[],borderColor:'#2F6B9A',borderDash:[7,4],pointRadius:0,borderWidth:1.5,hidden:false},{label:'淨資產月線',data:h.netMonthly||[],borderColor:'#68A5D2',borderDash:[2,4],pointRadius:0,borderWidth:1.5,hidden:false},{label:'總資產季線',data:h.totalQuarterly||[],borderColor:'#B86A2D',borderDash:[7,4],pointRadius:0,borderWidth:1.5,hidden:true},{label:'淨資產季線',data:h.netQuarterly||[],borderColor:'#D99A4A',borderDash:[2,4],pointRadius:0,borderWidth:1.5,hidden:true},{label:'總資產年線',data:h.totalYearly||[],borderColor:'#5B527D',borderDash:[7,4],pointRadius:0,borderWidth:1.5,hidden:true},{label:'淨資產年線',data:h.netYearly||[],borderColor:'#8A73A8',borderDash:[2,4],pointRadius:0,borderWidth:1.5,hidden:true}]; chart=new Chart(ctx,{type:'line',data:{labels:h.dates,datasets},options:{responsive:true,maintainAspectRatio:false,interaction:{mode:'index',intersect:false},plugins:{legend:{position:'top',labels:{boxWidth:12,font:{size:10}}}},scales:{x:{ticks:{maxTicksLimit:8}},y:{ticks:{callback:(v)=>money(v)}}}}}); const setTrendLines=(days)=>{const showMonth=days<=90,showQuarter=days>90&&days<=365,showYear=days>365;chart.data.datasets[2].hidden=!showMonth;chart.data.datasets[3].hidden=!showMonth;chart.data.datasets[4].hidden=!showQuarter;chart.data.datasets[5].hidden=!showQuarter;chart.data.datasets[6].hidden=!showYear;chart.data.datasets[7].hidden=!showYear;}; document.querySelectorAll('.range button').forEach((b)=>b.onclick=()=>{const days=b.dataset.range==='all'?h.dates.length:Math.min(Number(b.dataset.range),h.dates.length);chart.options.scales.x.min=Math.max(0,h.dates.length-days);chart.options.scales.x.max=h.dates.length-1;setTrendLines(days);chart.update();document.querySelectorAll('.range button').forEach(x=>x.classList.toggle('active',x===b));}); document.querySelector('.range button.active')?.click();};
  const load=async()=>{const tg=window.Telegram&&window.Telegram.WebApp;if(!tg||!tg.initData)throw new Error('請從 Telegram 的 Growth 按鈕開啟此頁面。');if(!apiUrl)throw new Error('Private API 尚未完成設定。');tg.ready();tg.expand();const res=await fetch(apiUrl,{headers:{'X-Telegram-Init-Data':tg.initData},cache:'no-store'});if(res.status===401)throw new Error('Telegram 身分驗證失敗或連結已過期，請從 Bot 重新開啟。');if(!res.ok)throw new Error(`Private API error (${res.status})`);render(await res.json());};
  load().catch((e)=>{$('loading').innerHTML='<strong>Growth Dashboard</strong><p>私有資料載入失敗，請回到 Bot 重新開啟。</p>';$('privateError').textContent=e.message;});
})();
</script>
</body></html>
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
    # Brand assets are static artwork and contain no portfolio data. Copy them
    # into Pages so the private Telegram shell can render the same identity.
    for asset_name in ("PRStK-Remove.png", "SFC.e-removebg-preview.png"):
        source = os.path.join(os.getcwd(), asset_name)
        if os.path.isfile(source):
            shutil.copy2(source, os.path.join(directory, asset_name))
    private_directory = os.path.join(directory, "private")
    os.makedirs(private_directory, exist_ok=True)
    supabase_url = os.getenv("SUPABASE_URL", "").strip()
    function_url = os.getenv("SUPABASE_FUNCTION_URL", "").strip()
    config = json.dumps({"url": supabase_url, "functionUrl": function_url}, ensure_ascii=True)
    private_html = TELEGRAM_PRIVATE_HTML_TEMPLATE.replace("__SUPABASE_CONFIG__", config)
    private_html = private_html.replace(
        "  const load=async()=>{",
        "  const renderIngestion=(body)=>{const data=body.data||{};const p=data.portfolio||{};const contract=p.transactionIngestion??data.transactionIngestion??{};const items=Array.isArray(contract)?contract:(Array.isArray(contract.recent)?contract.recent:[]);const target=document.getElementById('transactionIngestion');if(!target)return;target.innerHTML=items.length?items.slice(-3).reverse().map((item)=>{const status=String(item.status||'UNKNOWN');const klass=status.toLowerCase();const detail=item.reason||item.detail||item.compatibilityUsed||item.transactionDate||'';return `<div class=\\\"ingestion-row\\\"><span class=\\\"ingestion-status ${klass}\\\">${esc(status)}</span><div><strong>${esc(item.command||item.symbol||'交易')}</strong><small>${esc(item.currency||'')} ${esc(item.targetBalance||item.amount||'')} ${esc(detail)}</small></div><span>${esc(item.sourceRowId||'')}</span></div>`;}).join(''):'<div class=\\\"subtle\\\">目前沒有交易狀態紀錄。</div>';};\n  const load=async()=>{",
    )
    private_html = private_html.replace(
        "const target=document.getElementById('transactionIngestion');if(!target)return;",
        "const target=document.getElementById('transactionIngestion');if(!target)return;const visibleCount=Math.min(items.length,3);const healthCard=document.querySelector('.health-card');if(healthCard)healthCard.dataset.ingestionCount=String(visibleCount);const transactionSummary=document.getElementById('transactionSummary');if(transactionSummary)transactionSummary.textContent=visibleCount?`最近 ${visibleCount} 筆`:'無更新';const healthSummary=document.getElementById('healthSummary');if(healthSummary){const state=(healthSummary.textContent||'正常').split(' · ')[0];healthSummary.textContent=`${state} · ${visibleCount?`最近 ${visibleCount} 筆`:'無更新'} · 點擊${healthCard?.open?'收合':'展開'}`;}",
    )
    private_html = private_html.replace(
        "const detail=item.reason||item.detail||item.compatibilityUsed||item.transactionDate||'';",
        "const detail=[item.compatibilityUsed?`相容模式：${item.compatibilityUsed===true?'已啟用':item.compatibilityUsed}`:'',item.reason||item.detail||'',item.transactionDate||''].filter(Boolean).join(' · ');",
    )
    private_html = private_html.replace("render(await res.json());", "const body=await res.json(); render(body); renderIngestion(body);")
    with open(os.path.join(private_directory, "index.html"), "w", encoding="utf-8") as file:
        file.write(private_html)
