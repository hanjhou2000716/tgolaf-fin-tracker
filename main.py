import os
import json
import requests
import datetime
import math
import re
import yfinance as yf
import gspread
from google.oauth2.service_account import Credentials

# ==========================================
# 1. 環境變數設定
# ==========================================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
FINMIND_TOKEN = os.getenv("FINMIND_TOKEN")
GCP_CREDENTIALS_JSON = os.getenv("GCP_CREDENTIALS")

# 🚨 必填項目：請填入您 GitHub Pages 的網址 (結尾要有斜線 /)
# 例如: "https://hanjhou2000716.github.io/tgolaf-fin-tracker/"
WEB_APP_URL = "https://你的帳號.github.io/你的專案名稱/"

# ==========================================
# 2. Google Sheets 動態資產結算 (邏輯不變)
# ==========================================
def calculate_current_assets():
    creds_dict = json.loads(GCP_CREDENTIALS_JSON)
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    
    available_sheets = client.openall()
    sheet = None
    for s in available_sheets:
        if "PRStK" in s.title: sheet = s; break
    if not sheet:
        for s in available_sheets:
            if "Growth" in s.title or "資產" in s.title: sheet = s; break
    if not sheet: raise ValueError("找不到檔案")
        
    data_rows = []
    history_sheet = None
    for ws in sheet.worksheets():
        title_clean = ws.title.strip().lower()
        if "history" in title_clean or "歷史" in title_clean or "紀錄" in title_clean:
            history_sheet = ws
        elif "表單" in title_clean or "form" in title_clean or "回覆" in title_clean or "異動" in title_clean:
            rows = ws.get_all_values()
            if len(rows) > 1: data_rows.extend(rows[1:])
                
    if not data_rows: return {}, history_sheet
        
    def parse_date(row):
        if not row: return datetime.datetime.min
        ts_str = str(row[0]).strip()
        match = re.search(r'(\d{4})[/-](\d{1,2})[/-](\d{1,2})(?:\s+(上午|下午|AM|PM)?\s*(\d{1,2}):(\d{1,2}):(\d{1,2}))?', ts_str, re.IGNORECASE)
        if match:
            y, m, d, ampm, h, mnt, s = match.groups()
            h = int(h) if h else 0
            mnt = int(mnt) if mnt else 0
            if ampm in ['下午', 'PM', 'pm'] and h < 12: h += 12
            if ampm in ['上午', 'AM', 'am'] and h == 12: h = 0
            try: return datetime.datetime(int(y), int(m), int(d), h, mnt, int(s) if s else 0)
            except: pass
        return datetime.datetime.min

    data_rows.sort(key=parse_date)

    inventory = {
        "台股": {}, "美股": {}, "基金": {}, 
        "現金_TWD": {"TWD": 0.0}, "現金_USD": {"USD": 0.0},
        "質押負債": {"Current_Debt": 0.0, "History": []},
        "質押利率": {"Rate": 3.3, "History": []}, "擔保品": {}  
    }
    symbol_overrides = {'6208': '006208', '403A': '00403A', '886': '00886', '895': '00895', '878': '00878', '685L': '00685L'}
    known_symbols = ['6208', '006208', '403A', '00403A', '886', '00886', '895', '00895', '878', '00878', '3455', '8033', '2330', '3665', '685L', '00685L', 'QQQM', 'NVDA', 'SPYG', 'TSM', 'VOO', 'VTI', 'TSLA', 'AAPL', 'QQQ', 'FUND', 'TWD', 'USD', 'CURRENT_DEBT', 'RATE']
    
    for row in data_rows:
        row_date = parse_date(row).date()
        raw_cells = [str(c).strip() for c in row if str(c).strip() != ""]
        if not raw_cells: continue
        
        cells = []
        for c in raw_cells:
            match = re.match(r'^([0-9,.]+)\s*(股|張|萬|元|塊|%)$', c)
            if match:
                num_part = match.group(1).replace(',', '')
                unit = match.group(2)
                cells.append(str(float(num_part) * 10000) if unit == '萬' else num_part)
            else: cells.append(c)
        
        asset_type, mode, symbol = "", "", ""
        potential_numbers = []
        for cell in cells:
            c_upper = cell.upper()
            if any(x in cell for x in ["台股", "美股", "基金", "現金", "質押", "負債", "擔保", "利率"]): asset_type = cell
            elif any(x in cell for x in ["買入", "存入", "賣出", "提領", "取代", "覆蓋", "更新"]): mode = cell
            elif c_upper in known_symbols or any(char.isalpha() for char in c_upper):
                if "/" not in cell and "-" not in cell: symbol = cell
            else:
                try: potential_numbers.append(cell)
                except ValueError: pass
                    
        if not symbol and len(potential_numbers) >= 2: symbol, amount_str = potential_numbers[0], potential_numbers[-1]
        elif len(potential_numbers) >= 1: amount_str = potential_numbers[-1]
        else: amount_str = "0"
            
        if not asset_type: continue
        if not mode: mode = "取代"
        
        if "台" in asset_type and "股" in asset_type: asset_type = "台股"
        elif "美" in asset_type and "股" in asset_type: asset_type = "美股"
        elif "基" in asset_type and "金" in asset_type: asset_type = "基金"
        elif "USD" in asset_type or "美金" in asset_type: asset_type = "現金_USD"
        elif "TWD" in asset_type or "台幣" in asset_type or "現金" in asset_type: asset_type = "現金_TWD"
        elif "利率" in asset_type: asset_type = "質押利率"
        elif "質押" in asset_type or "負債" in asset_type: asset_type = "質押負債"
        elif "擔保" in asset_type: asset_type = "擔保品"
        
        if asset_type not in inventory: continue
        try: amount = float(amount_str.replace(",", "").replace("$", ""))
        except: continue
            
        symbol = symbol_overrides.get(symbol, symbol)
        if asset_type in ["現金_TWD", "現金_USD", "質押負債", "質押利率"] and not symbol:
            symbol = {"現金_TWD": "TWD", "現金_USD": "USD", "質押負債": "Current_Debt", "質押利率": "Rate"}[asset_type]
            
        if not symbol: continue
        if symbol not in inventory[asset_type] and symbol != "History": inventory[asset_type][symbol] = 0.0
            
        if "買入" in mode or "存入" in mode or "+" in mode: inventory[asset_type][symbol] += amount
        elif "賣出" in mode or "提領" in mode or "-" in mode: inventory[asset_type][symbol] -= amount
        elif "取代" in mode or "覆蓋" in mode or "更新" in mode: inventory[asset_type][symbol] = amount

        if asset_type == "質押負債": inventory["質押負債"]["History"].append((row_date, inventory["質押負債"]["Current_Debt"]))
        elif asset_type == "質押利率": inventory["質押利率"]["History"].append((row_date, inventory["質押利率"]["Rate"]))

    return inventory, history_sheet

def get_usd_twd_rate():
    try: return float(requests.get("https://query1.finance.yahoo.com/v8/finance/chart/TWD=X?interval=1d&range=1d", headers={'User-Agent': 'Mozilla/5.0'}, timeout=5).json()['chart']['result'][0]['meta']['regularMarketPrice'])
    except: return 32.5

def get_us_stock_price(symbol):
    try: return float(requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=1d", headers={'User-Agent': 'Mozilla/5.0'}, timeout=5).json()['chart']['result'][0]['meta']['regularMarketPrice'])
    except: return 0

def get_tw_stock_price(symbol):
    try:
        start_date = (datetime.date.today() - datetime.timedelta(days=7)).strftime("%Y-%m-%d")
        data = requests.get("https://api.finmindtrade.com/api/v4/data", params={"dataset": "TaiwanStockPrice", "data_id": str(symbol), "start_date": start_date, "token": FINMIND_TOKEN}).json()
        return data["data"][-1]["close"] if data["msg"] == "success" else 0
    except: return 0

# ==========================================
# 3. 主程序：計算並寫入 HTML 檔案
# ==========================================
def main():
    tw_now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    today_str = tw_now.strftime("%m-%d")
    header_text = f"🇹🇼 PRStK | Growth Web App" if 12 <= tw_now.hour <= 20 else f"🇺🇸 PRStK | Growth Web App"
        
    inventory, history_sheet = calculate_current_assets()
    try: history_records = history_sheet.get_all_records()
    except: history_records = []
        
    usd_rate = get_usd_twd_rate()
    tw_stock_value, us_stock_value_usd, tsmc_exposure_twd, price_006208, leveraged_etf_value = 0, 0, 0, 0, 0
    cash_twd, cash_usd = inventory["現金_TWD"].get("TWD", 0), inventory["現金_USD"].get("USD", 0)
    fund_value = sum(v for k, v in inventory["基金"].items() if k != "History")

    for symbol, shares in inventory["台股"].items():
        if symbol == "History" or shares <= 0: continue
        price = get_tw_stock_price(symbol)
        value = price * shares
        tw_stock_value += value 
        if symbol == '2330': tsmc_exposure_twd += (value * 1.0)
        elif symbol == '006208': tsmc_exposure_twd += (value * 0.594); price_006208 = price
        elif symbol == '00685L': tsmc_exposure_twd += (value * 0.728); leveraged_etf_value = value

    pledged_value = sum((price_006208 if sym == '006208' and price_006208 > 0 else get_tw_stock_price(sym)) * shares for sym, shares in inventory["擔保品"].items() if sym != "History" and shares > 0)

    for symbol, shares in inventory["美股"].items():
        if symbol == "History" or shares <= 0: continue
        value = get_us_stock_price(symbol) * shares
        us_stock_value_usd += value
        if symbol == 'TSM': tsmc_exposure_twd += (value * usd_rate * 1.0)

    us_stock_value_twd = us_stock_value_usd * usd_rate
    total_cash_twd = cash_twd + (cash_usd * usd_rate)
    
    debt = inventory["質押負債"].get("Current_Debt", 0)
    debt_history = inventory["質押負債"].get("History", [])
    rate_history = inventory["質押利率"].get("History", [])

    def get_val(hist, d_target, d_default):
        val = d_default
        for d, v in hist:
            if d <= d_target: val = v
        return val

    loan_start = datetime.date(2026, 6, 10) 
    accumulated_interest = sum(get_val(debt_history, loan_start + datetime.timedelta(days=i), debt_history[0][1] if debt_history else debt) * ((get_val(rate_history, loan_start + datetime.timedelta(days=i), 3.3) / 100) / 365) for i in range(max(0, (tw_now.date() - loan_start).days)))

    total_debt = debt + accumulated_interest
    total_asset = tw_stock_value + us_stock_value_twd + total_cash_twd + fund_value
    net_asset = total_asset - total_debt
    
    invested_assets = tw_stock_value + us_stock_value_twd + fund_value
    effective_leverage = ((invested_assets + leveraged_etf_value) / net_asset) if net_asset > 0 else 0
    debt_ratio = ((total_debt / total_asset) * 100) if total_asset > 0 else 0
    maintenance_ratio = (pledged_value / total_debt) * 100 if total_debt > 0 else 0
    ratio_status = "🟢 安全" if maintenance_ratio >= 190 else "🟡 注意" if maintenance_ratio >= 150 else "🔴 警戒" if maintenance_ratio >= 130 else "🆘 危險" if maintenance_ratio > 0 else "✅ 無借款"
    ratio_color = "var(--success)" if maintenance_ratio >= 150 else "var(--danger)"

    tw_free_value = max(0, tw_stock_value - total_debt)
    tsmc_pct = (tsmc_exposure_twd / total_asset) * 100 if total_asset > 0 else 0

    yesterday_net = next((float(str(row.get('Net_Asset', 0)).replace(',', '')) for row in reversed(history_records) if float(str(row.get('Net_Asset', 0)).replace(',', '')) > 0 and str(row.get('Date', ''))[-5:] != today_str), 0)
    daily_diff = net_asset - yesterday_net if yesterday_net else 0
    daily_pct = (daily_diff / yesterday_net * 100) if yesterday_net else 0

    if total_asset > 0: history_sheet.append_row([tw_now.strftime("%Y-%m-%d"), round(total_asset, 2), round(net_asset, 2), total_debt, round(tsmc_exposure_twd, 2)])

    daily_net, daily_total = {}, {}
    for row in history_records:
        date_str = str(row.get('Date', ''))[:10]
        n_val, t_val = float(str(row.get('Net_Asset', 0)).replace(',', '')), float(str(row.get('Total_Asset', 0)).replace(',', ''))
        if n_val > 0 and len(date_str) == 10: daily_net[date_str], daily_total[date_str] = n_val, t_val
            
    daily_net[tw_now.strftime("%Y-%m-%d")] = net_asset
    daily_total[tw_now.strftime("%Y-%m-%d")] = total_asset
    
    recent_dates = sorted(daily_net.keys())[-14:] # 取最近14天做圖表
    chart_dates = [d[5:] for d in recent_dates]
    chart_totals = [daily_total[d] for d in recent_dates]
    chart_nets = [daily_net[d] for d in recent_dates]

    # --- 生成 index.html ---
    html_content = f"""
    <!DOCTYPE html>
    <html lang="zh-TW">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
        <title>PRStK Growth</title>
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
        <style>
            @import url('https://fonts.googleapis.com/css2?family=Noto+Sans+TC:wght@400;700;900&display=swap');
            :root {{ --bg-color: #f1f5f9; --card-bg: #ffffff; --text-main: #0f172a; --text-sub: #64748b; --accent: #3b82f6; --danger: #ef4444; --success: #10b981; }}
            @media (prefers-color-scheme: dark) {{ :root {{ --bg-color: #1e293b; --card-bg: #0f172a; --text-main: #f8fafc; --text-sub: #94a3b8; }} }}
            body {{ font-family: 'Noto Sans TC', sans-serif; background-color: var(--bg-color); color: var(--text-main); margin: 0; padding: 15px; box-sizing: border-box; overscroll-behavior-y: none; padding-bottom: 50px; }}
            .header h1 {{ margin: 0; font-size: 24px; font-weight: 900; }}
            .header p {{ margin: 5px 0 15px 0; font-size: 13px; color: var(--text-sub); }}
            .summary-box {{ background: linear-gradient(135deg, #f97316, #ea580c); color: white; padding: 20px; border-radius: 16px; margin-bottom: 20px; box-shadow: 0 4px 15px rgba(234, 88, 12, 0.3); }}
            .s-val {{ font-size: 32px; font-weight: 900; margin: 10px 0; }}
            .s-diff {{ font-size: 14px; background: rgba(0,0,0,0.2); display: inline-block; padding: 6px 12px; border-radius: 20px; font-weight: 700; }}
            .card {{ background: var(--card-bg); border-radius: 16px; padding: 20px; margin-bottom: 16px; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.05); }}
            .section-title {{ font-size: 16px; font-weight: 900; margin-bottom: 15px; border-left: 4px solid var(--accent); padding-left: 10px; }}
            .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }}
            .box {{ background: rgba(148, 163, 184, 0.1); border-radius: 12px; padding: 12px; }}
            .t {{ font-size: 12px; color: var(--text-sub); font-weight: 700; margin-bottom: 4px; }}
            .v {{ font-size: 18px; font-weight: 900; }}
            .s {{ font-size: 11px; color: var(--text-sub); margin-top: 4px; display: block; }}
            .chart-container {{ position: relative; width: 100%; height: 250px; margin-top: 15px; }}
            .pie-container {{ position: relative; width: 100%; height: 200px; margin-top: 15px; }}
        </style>
    </head>
    <body>
        <div class="header"><h1>{header_text}</h1><p>最後更新：{tw_now.strftime("%Y/%m/%d %H:%M")} CST</p></div>
        <div class="summary-box">
            <div style="font-size: 14px; font-weight:700;">💰 總資產：${total_asset:,.0f}</div>
            <div class="s-val">🟢 淨額：${net_asset:,.0f}</div>
            <div class="s-diff">{'📈' if daily_diff>=0 else '📉'} 單日變化：{'+' if daily_diff>=0 else ''}{daily_pct:.1f}% ({'+' if daily_diff>=0 else ''}${daily_diff:,.0f})</div>
        </div>
        <div class="card"><div class="section-title">📂 核心資產明細</div><div class="grid">
            <div class="box"><div class="t">🇹🇼 台股現值</div><div class="v">${tw_stock_value:,.0f}</div></div>
            <div class="box"><div class="t">🇺🇸 美股現值</div><div class="v">${us_stock_value_twd:,.0f}</div><span class="s">約 ${us_stock_value_usd:,.0f} USD</span></div>
            <div class="box"><div class="t">💵 現金 (TWD)</div><div class="v">${cash_twd:,.0f}</div></div>
            <div class="box"><div class="t">💴 現金 (USD)</div><div class="v">${cash_usd * usd_rate:,.0f}</div><span class="s">約 ${cash_usd:,.0f} USD</span></div>
            <div class="box"><div class="t">🐣 基金現值</div><div class="v">${fund_value:,.0f}</div></div>
            <div class="box"><div class="t">🐔 TSMC 總曝險</div><div class="v">{tsmc_pct:.1f}%</div></div>
        </div></div>
        <div class="card"><div class="section-title">🛡️ 槓桿與風險監控</div><div class="grid">
            <div class="box"><div class="t">💸 質押借款</div><div class="v" style="color:var(--danger)">-${total_debt:,.0f}</div><span class="s">內含利息 ${accumulated_interest:,.0f}</span></div>
            <div class="box"><div class="t">🦾 質押維持率</div><div class="v" style="color:{ratio_color}">{maintenance_ratio:.1f}%</div><span class="s">狀態: {ratio_status}</span></div>
            <div class="box"><div class="t">⚖️ 總資產 Beta</div><div class="v">{effective_leverage:.2f}x</div></div>
            <div class="box"><div class="t">🕸️ 資產負債比</div><div class="v">{debt_ratio:.1f}%</div></div>
        </div></div>
        <div class="card"><div class="section-title">📈 互動資產軌跡</div><div class="chart-container"><canvas id="lineChart"></canvas></div></div>
        <div class="card"><div class="section-title">📊 資產配置分佈</div><div class="pie-container"><canvas id="pieChart"></canvas></div></div>
        <script>
            Chart.defaults.color = window.matchMedia('(prefers-color-scheme: dark)').matches ? '#94a3b8' : '#64748b';
            Chart.defaults.font.family = "'Noto Sans TC', sans-serif";
            new Chart(document.getElementById('lineChart').getContext('2d'), {{
                type: 'line',
                data: {{ labels: {chart_dates}, datasets: [
                    {{ label: '總資產', data: {chart_totals}, borderColor: '#3b82f6', tension: 0.3, borderWidth: 2, pointRadius: 1 }},
                    {{ label: '淨資產', data: {chart_nets}, borderColor: '#ef4444', tension: 0.3, borderWidth: 2, pointRadius: 1 }}
                ]}},
                options: {{ responsive: true, maintainAspectRatio: false, interaction: {{ mode: 'index', intersect: false }}, plugins: {{ legend: {{ position: 'bottom' }} }} }}
            }});
            new Chart(document.getElementById('pieChart').getContext('2d'), {{
                type: 'pie',
                data: {{ labels: ['🇹🇼 現貨台股', '🦆 質押投資', '🇺🇸 現貨美股'], datasets: [{{ data: [{tw_free_value}, {total_debt}, {us_stock_value_twd}], backgroundColor: ['#3b82f6', '#ef4444', '#eab308'], borderWidth: 0 }}] }},
                options: {{ responsive: true, maintainAspectRatio: false, plugins: {{ legend: {{ position: 'right' }} }} }}
            }});
        </script>
    </body></html>
    """

    with open('index.html', 'w', encoding='utf-8') as f:
        f.write(html_content)

    # --- 發送 Telegram 通知 (帶有 Web App 按鈕) ---
    # 設定 Web App 的按鈕結構
    keyboard = {
        "inline_keyboard": [
            [{"text": "⚡️ 開啟互動儀表板", "web_app": {"url": WEB_APP_URL}}],
            [{"text": "📝 填寫異動表單", "url": "https://forms.gle/9ZEJawwNRGfiXQiV8"}]
        ]
    }
    
    # 這次不傳圖片，改傳送一則簡潔的文字訊息
    text_message = f"✅ **日報結算完畢！({today_str})**\n\n您的資產數據已更新，請點擊下方按鈕開啟全新的「**Web App 互動儀表板**」。"
    
    requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={
        "chat_id": TELEGRAM_CHAT_ID, 
        "text": text_message,
        "parse_mode": "Markdown",
        "reply_markup": keyboard
    })

if __name__ == "__main__":
    main()
