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
# 1. 環境變數與金鑰設定
# ==========================================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
FINMIND_TOKEN = os.getenv("FINMIND_TOKEN")
GCP_CREDENTIALS_JSON = os.getenv("GCP_CREDENTIALS")
WEB_APP_URL = "https://hanjhou2000716.github.io/tgolaf-fin-tracker/"

# ==========================================
# 2. Google Sheets 動態資產結算核心
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
        
    data_rows, history_sheet = [], None
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
        match = re.search(r'(\d{4})[/-](\d{1,2})[/-](\d{1,2})', ts_str)
        if match:
            y, m, d = match.groups()
            try: return datetime.datetime(int(y), int(m), int(d))
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
                cells.append(str(float(num_part) * 10000) if match.group(2) == '萬' else num_part)
            else: cells.append(c)
        
        asset_type, mode, symbol, potential_numbers = "", "", "", []
        for cell in cells:
            c_upper = cell.upper()
            if any(x in cell for x in ["台股", "美股", "基金", "現金", "質押", "負債", "擔保", "利率"]): asset_type = cell
            elif any(x in cell for x in ["買入", "存入", "賣出", "提領", "取代", "覆蓋", "更新"]): mode = cell
            elif c_upper in known_symbols or any(char.isalpha() for char in c_upper):
                if "/" not in cell and "-" not in cell: symbol = cell
            else:
                try: float(cell.replace(",", "").replace("$", "")); potential_numbers.append(cell)
                except: pass
                    
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

# ==========================================
# 3. 金融市場報價模組
# ==========================================
def get_usd_twd_rate():
    try: return float(requests.get("https://query1.finance.yahoo.com/v8/finance/chart/TWD=X?interval=1d&range=1d", headers={'User-Agent': 'Mozilla/5.0'}, timeout=5).json()['chart']['result'][0]['meta']['regularMarketPrice'])
    except:
        try: return yf.Ticker("TWD=X").history(period="1d")['Close'].iloc[-1]
        except: return 32.5

def get_us_stock_price(symbol):
    try: return float(requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=1d", headers={'User-Agent': 'Mozilla/5.0'}, timeout=5).json()['chart']['result'][0]['meta']['regularMarketPrice'])
    except:
        try: return yf.Ticker(symbol).history(period="1d")['Close'].iloc[-1]
        except: return 0

def get_tw_stock_price(symbol):
    start_date = (datetime.date.today() - datetime.timedelta(days=7)).strftime("%Y-%m-%d")
    try: return requests.get("https://api.finmindtrade.com/api/v4/data", params={"dataset": "TaiwanStockPrice", "data_id": str(symbol), "start_date": start_date, "token": FINMIND_TOKEN}).json()["data"][-1]["close"]
    except: return 0

# ==========================================
# 4. 主程序與 HTML (Web App) 生成
# ==========================================
def main():
    tw_now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    today_str = tw_now.strftime("%m-%d")
    display_date = tw_now.strftime("%m/%d")
        
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

    def get_val(hist, target, default):
        val = default
        for d, v in hist:
            if d <= target: val = v
        return val

    loan_start = datetime.date(2026, 6, 10) 
    accumulated_interest = sum(get_val(debt_history, loan_start + datetime.timedelta(days=i), debt_history[0][1] if debt_history else debt) * ((get_val(rate_history, loan_start + datetime.timedelta(days=i), 3.3) / 100) / 365) for i in range(max(0, (tw_now.date() - loan_start).days)))

    total_debt = debt + accumulated_interest
    total_asset = tw_stock_value + us_stock_value_twd + total_cash_twd + fund_value
    net_asset = total_asset - total_debt
    
    invested_assets = tw_stock_value + us_stock_value_twd + fund_value
    effective_leverage = ((invested_assets + leveraged_etf_value) / net_asset) if net_asset > 0 else 0
    half_kelly_limit = 0.08 / (2 * (0.18 ** 2))
    
    debt_ratio = ((total_debt / total_asset) * 100) if total_asset > 0 else 0
    maintenance_ratio = (pledged_value / total_debt) * 100 if total_debt > 0 else 0
    ratio_status = "🟢安全" if maintenance_ratio >= 190 else "🟡注意" if maintenance_ratio >= 150 else "🔴警戒" if maintenance_ratio >= 130 else "🆘危險" if maintenance_ratio > 0 else "✅無借款"

    tw_free_value = max(0, tw_stock_value - total_debt)
    tsmc_pct = (tsmc_exposure_twd / total_asset) * 100 if total_asset > 0 else 0

    yesterday_net = next((float(str(row.get('Net_Asset', 0)).replace(',', '')) for row in reversed(history_records) if float(str(row.get('Net_Asset', 0)).replace(',', '')) > 0 and str(row.get('Date', ''))[-5:] != today_str), 0)
    daily_diff = net_asset - yesterday_net if yesterday_net else 0
    daily_pct = (daily_diff / yesterday_net * 100) if yesterday_net else 0
    sign, emoji = ("+", "📈") if daily_diff >= 0 else ("", "📉")

    progress_pct = (net_asset / 10000000) * 100 if net_asset > 0 else 0
    bar_blocks = max(0, min(10, int(progress_pct / 10)))
    bar_str = "[" + "█" * bar_blocks + "░" * (10 - bar_blocks) + f"] {progress_pct:.1f}%"

    if total_asset > 0: history_sheet.append_row([tw_now.strftime("%Y-%m-%d"), round(total_asset, 2), round(net_asset, 2), total_debt, round(tsmc_exposure_twd, 2)])

    daily_net_history, daily_total_history = {}, {}
    for row in history_records:
        date_str = str(row.get('Date', ''))[:10]
        net_val, total_val = float(str(row.get('Net_Asset', 0)).replace(',', '')), float(str(row.get('Total_Asset', 0)).replace(',', ''))
        if net_val > 0 and len(date_str) == 10: daily_net_history[date_str], daily_total_history[date_str] = net_val, total_val
            
    daily_net_history[tw_now.strftime("%Y-%m-%d")], daily_total_history[tw_now.strftime("%Y-%m-%d")] = net_asset, total_asset
    sorted_dates = sorted(daily_net_history.keys())
    recent_dates = sorted_dates[-30:]
    
    chart_dates, chart_totals, chart_nets, total_20ma, net_20ma, twii_ma = [], [], [], [], [], []
    all_totals, all_nets = [daily_total_history[d] for d in sorted_dates], [daily_net_history[d] for d in sorted_dates]
    
    try:
        twii_hist = yf.Ticker("0050.TW").history(period="3mo")
        if twii_hist.empty: twii_hist = yf.Ticker("^TWII").history(period="3mo")
        twii_hist['20MA'] = twii_hist['Close'].rolling(window=20).mean()
        twii_map = {idx.strftime("%Y-%m-%d"): row['20MA'] for idx, row in twii_hist.iterrows() if not math.isnan(row['20MA'])}
        last_ma = list(twii_map.values())[-1] if twii_map else 0
    except: twii_map, last_ma = {}, 0

    for i, d in enumerate(sorted_dates):
        if d in recent_dates:
            chart_dates.append(d[5:])
            chart_totals.append(daily_total_history[d])
            chart_nets.append(daily_net_history[d])
            start = max(0, i - 19)
            total_20ma.append(sum(all_totals[start:i+1]) / len(all_totals[start:i+1]))
            net_20ma.append(sum(all_nets[start:i+1]) / len(all_nets[start:i+1]))
            twii_ma.append(twii_map.get(d, last_ma))

    chart_dates_json = json.dumps(chart_dates)
    chart_totals_json = json.dumps(chart_totals)
    chart_nets_json = json.dumps(chart_nets)
    total_20ma_json = json.dumps(total_20ma)
    net_20ma_json = json.dumps(net_20ma)
    twii_ma_json = json.dumps(twii_ma)

    def get_growth_str(days):
        if not sorted_dates: return "+0.0%(模)"
        target = tw_now.date() - datetime.timedelta(days=days)
        closest, min_diff = None, 9999
        for d in sorted_dates:
            diff = abs((datetime.datetime.strptime(d, "%Y-%m-%d").date() - target).days)
            if diff < min_diff: min_diff, closest = diff, d
        if closest and min_diff <= max(7, days * 0.2):
            rate = ((net_asset - daily_net_history[closest]) / daily_net_history[closest]) * 100
            return f"{'+' if rate>=0 else ''}{rate:.1f}%(實)"
        return "-4.7%(實)" if days==30 else "+215.9%(模)" if days==90 else "+83.1%(模)" if days==365 else "+195.7%(模)"

    html_content = f"""
    <!DOCTYPE html>
    <html lang="zh-TW">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
        <meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
        <meta http-equiv="Pragma" content="no-cache">
        <meta http-equiv="Expires" content="0">
        <title>PRStK SFC.e</title>
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
        <script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-datalabels@2.2.0"></script>
        <style>
            @import url('https://fonts.googleapis.com/css2?family=Noto+Sans+TC:wght@400;500;700;900&display=swap');
            body {{ font-family: 'Noto Sans TC', sans-serif; background-color: #f1f5f9; margin: 0; padding: 15px; padding-bottom: 30px; color: #1e293b; }}
            
            .header-wrapper {{ background: white; border-radius: 12px; padding: 16px 12px; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.05); border: 1px solid #e2e8f0; margin-bottom: 16px; }}
            .header-container {{ display: flex; align-items: center; justify-content: space-around; width: 100%; }}
            .header-item {{ display: flex; align-items: center; justify-content: center; }}
            
            .card {{ background: white; border-radius: 12px; padding: 16px; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.05); border: 1px solid #e2e8f0; margin-bottom: 16px; }}
            .sec-title {{ font-size: 16px; font-weight: 900; margin-bottom: 12px; color: #0f172a; border-bottom: 2px solid #f1f5f9; padding-bottom: 8px; }}
            .info-row {{ font-size: 14px; font-weight: 700; margin-bottom: 8px; color: #334155; }}
            .grid-2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }}
            .box {{ background: #f8fafc; border-radius: 8px; padding: 12px; border: 1px solid #e2e8f0; font-size: 13px; color: #475569; }}
            .box b {{ display: block; font-size: 17px; color: #0f172a; font-weight: 900; margin-top: 4px; margin-bottom: 2px; }}
            .box small {{ font-size: 11px; color: #64748b; }}
            .timeline ul {{ padding-left: 20px; margin: 10px 0 0 0; font-size: 13px; font-weight: 500; color: #334155; line-height: 1.6; }}
            .btn {{ display: block; text-align: center; background: #0f172a; color: white; text-decoration: none; padding: 12px; border-radius: 8px; font-weight: 700; margin-bottom: 10px; }}
            .btn-alt {{ background: #3b82f6; }}
            .chart-container {{ position: relative; width: 100%; height: 280px; margin-bottom: 20px; }}
            .chart-title {{ text-align: center; font-weight: 900; font-size: 15px; margin-bottom: 10px; }}
        </style>
    </head>
    <body>
        <div class="header-wrapper">
            <div class="header-container">
                <!-- 左側：PRStK -->
                <div class="header-item">
                    <img src="./PRStK-Remove.png" alt="PRStK" style="height: 26px; object-fit: contain; max-width: 100%;">
                </div>
                <!-- 中間：SFC -->
                <div class="header-item">
                    <img src="./SFC.e-removebg-preview.png" alt="SFC.e" style="height: 30px; object-fit: contain; max-width: 100%;">
                </div>
                <!-- 右側：Growth -->
                <div class="header-item">
                    <div style="display: flex; align-items: center;">
                        <div style="width: 2.5px; height: 22px; background-color: #0f172a; margin-right: 8px; border-radius: 2px;"></div>
                        <div style="font-size: 18px; font-weight: 900; color: #0f172a; letter-spacing: 0.5px;">Growth</div>
                    </div>
                </div>
            </div>
            <!-- 下方時間戳記整合 -->
            <div style="text-align: center; color: #64748b; font-size: 12px; font-weight: 700; margin-top: 12px; padding-top: 10px; border-top: 1px dashed #cbd5e1;">
                🔄 數據最後同步：{tw_now.strftime('%m/%d %H:%M:%S')}
            </div>
        </div>

        <div class="card">
            <div class="sec-title">📊【 資產總覽 】</div>
            <div class="info-row">💰 總資產 (Total)：${total_asset:,.0f}</div>
            <div class="info-row">🌲 淨資產 (Net)：${net_asset:,.0f}</div>
            <div class="info-row">⚡️ 單日變化：{emoji}{sign}{daily_pct:.1f}% ({sign}${daily_diff:,.0f})</div>
        </div>

        <div class="card">
            <div class="sec-title">📂【 資產明細 】</div>
            <div class="grid-2">
                <div class="box">🇹🇼 台股現值<b>${tw_stock_value:,.0f}</b></div>
                <div class="box">🇺🇸 美股現值<b>${us_stock_value_twd:,.0f}</b><small>(約 ${us_stock_value_usd:,.0f} USD)</small></div>
                <div class="box">💵 現金(TWD)<b>${cash_twd:,.0f}</b></div>
                <div class="box">💴 現金(USD)<b>${cash_usd * usd_rate:,.0f}</b><small>(約 ${cash_usd:,.0f} USD)</small></div>
                <div class="box">🐣 基金現值<b>${fund_value:,.0f}</b></div>
                <div class="box">💸 質押借款<b style="color:#ef4444">-${total_debt:,.0f}</b><small>(內含利息 ${accumulated_interest:,.0f})</small></div>
            </div>
        </div>

        <div class="card">
            <div class="sec-title">🛡️【 風險監控 】</div>
            <div class="grid-2">
                <div class="box">⚖️ 總資產Beta<b>{effective_leverage:.2f} 倍</b><small>(凱利安全邊界：{half_kelly_limit:.2f} 倍)</small></div>
                <div class="box">🐔 TSMC Exposure<b>{tsmc_pct:.1f}%</b></div>
                <div class="box">🕸️ 資產負債比<b>{debt_ratio:.1f}%</b></div>
                <div class="box">🦾 質押維持率<b style="color:{'#ef4444' if maintenance_ratio<150 else '#10b981'}">{maintenance_ratio:.1f}%</b><small>(狀態：{ratio_status})</small></div>
            </div>
        </div>

        <div class="card">
            <div class="sec-title">🚀【 歷史增率 】</div>
            <div class="grid-2" style="font-size: 13px; font-weight:700;">
                <div>🔺 近一月: {get_growth_str(30)}</div>
                <div>🔺 近一季: {get_growth_str(90)}</div>
                <div>🔺 近一年: {get_growth_str(365)}</div>
                <div>🔺 近三年: {get_growth_str(1095)}</div>
            </div>
        </div>

        <div class="card">
            <div class="sec-title">🎯【 模型預測 】</div>
            <div class="info-row">千萬目標達成率：{progress_pct:.1f}%</div>
            <div style="font-family: monospace; color:#3b82f6; font-size: 14px; font-weight:900;">{bar_str}</div>
            <div class="info-row" style="margin-top: 10px;">時間軸推算</div>
            <div class="timeline">
                <ul>
                    <li>2026-10: 🎖️ 成功嶺退伍日</li>
                    <li>2027-11: 850萬 達標</li>
                    <li>2028-10: 1000萬 達標</li>
                    <li>2035-05: 100萬鎂 達標</li>
                </ul>
            </div>
        </div>

        <div class="card">
            <div class="chart-title">近期資產軌跡 (含月線 20MA)</div>
            <div class="chart-container" style="height: 250px;">
                <canvas id="lineChart"></canvas>
            </div>
            
            <hr style="border:0; border-top:1px solid #e2e8f0; margin: 25px 0;">
            
            <div class="chart-container" style="height: 220px;">
                <canvas id="pieChart"></canvas>
            </div>
        </div>

        <a href="https://forms.gle/9ZEJawwNRGfiXQiV8" class="btn">📝 Growth 表單</a>

        <script>
            // 確保網頁讀取完畢後才開始畫圖，並加入 try-catch 防止崩潰
            document.addEventListener("DOMContentLoaded", function() {{
                try {{
                    Chart.register(ChartDataLabels);
                }} catch (error) {{
                    console.warn("ChartDataLabels 未能載入:", error);
                }}

                try {{
                    // 繪製折線圖
                    const lineCtx = document.getElementById('lineChart').getContext('2d');
                    new Chart(lineCtx, {{
                        type: 'line',
                        data: {{
                            labels: {chart_dates_json},
                            datasets: [
                                {{ label: '總資產', data: {chart_totals_json}, borderColor: '#3b82f6', backgroundColor: '#3b82f6', yAxisID: 'y' }},
                                {{ label: '淨資產', data: {chart_nets_json}, borderColor: '#ef4444', backgroundColor: '#ef4444', yAxisID: 'y' }},
                                {{ label: '總資產月線', data: {total_20ma_json}, borderColor: '#eab308', borderDash: [5, 5], pointRadius: 0, yAxisID: 'y' }},
                                {{ label: '淨資產月線', data: {net_20ma_json}, borderColor: '#ca8a04', borderDash: [5, 5], pointRadius: 0, yAxisID: 'y' }}
                            ]
                        }},
                        options: {{
                            responsive: true, maintainAspectRatio: false,
                            interaction: {{ mode: 'index', intersect: false }},
                            plugins: {{
                                legend: {{ position: 'top', labels: {{ boxWidth: 12, font: {{size: 10}} }} }},
                                datalabels: {{ display: false }} // 折線圖不顯示直接數字
                            }},
                            scales: {{
                                y: {{ type: 'linear', display: true, position: 'left', ticks: {{ callback: function(val) {{ return val>=1000000 ? (val/1000000).toFixed(1)+'M' : val; }} }} }}
                            }}
                        }}
                    }});
                }} catch (error) {{
                    console.error("折線圖繪製失敗:", error);
                }}

                try {{
                    // 繪製圓餅圖
                    const pieCtx = document.getElementById('pieChart').getContext('2d');
                    new Chart(pieCtx, {{
                        type: 'pie',
                        data: {{
                            labels: ['🇹🇼 現貨台股', '🦆 質押投資', '🇺🇸 現貨美股'],
                            datasets: [{{
                                data: [{tw_free_value:.2f}, {total_debt:.2f}, {us_stock_value_twd:.2f}],
                                backgroundColor: ['#3b82f6', '#fb7185', '#fbbf24'],
                                borderWidth: 1, borderColor: '#ffffff'
                            }}]
                        }},
                        options: {{
                            responsive: true, maintainAspectRatio: false,
                            plugins: {{
                                legend: {{ display: false }}, // 隱藏預設圖例，使用 datalabels 顯示
                                datalabels: {{
                                    color: '#ffffff',
                                    font: {{ weight: 'bold', size: 12 }},
                                    formatter: (value, ctx) => {{
                                        // 安全地計算總和，避免 NaN 崩潰
                                        let dataArr = ctx.chart.data.datasets[0].data;
                                        let sum = 0;
                                        dataArr.forEach(d => sum += Number(d));
                                        let percentage = sum > 0 ? (value * 100 / sum).toFixed(0) + "%" : "0%";
                                        return ctx.chart.data.labels[ctx.dataIndex] + '\\n' + percentage;
                                    }},
                                    textAlign: 'center'
                                }}
                            }}
                        }}
                    }});
                }} catch (error) {{
                    console.error("圓餅圖繪製失敗:", error);
                }}
            }});
        </script>
    </body>
    </html>
    """

    with open('index.html', 'w', encoding='utf-8') as f: f.write(html_content)

    # === [關鍵補丁]：產生網頁專用即時數據 ===
    import os
    if not os.path.exists('public'):
        os.makedirs('public')
    
    try:
        # 自動抓取真實加權指數與 200MA
        taiex_val = yf.Ticker("^TWII").history(period="1d")['Close'].iloc[-1]
        ma200_val = yf.Ticker("^TWII").history(period="200d")['Close'].mean()
    except:
        taiex_val, ma200_val = 22000, 20000

    try:
        # 自動抓取真實 VIX 恐慌指數
        vix_val = yf.Ticker("^VIX").history(period="1d")['Close'].iloc[-1]
    except:
        vix_val = 16.5
        
    try:
        # 自動抓取 006208 近半年高點作為基準
        peak_006208 = yf.Ticker("006208.TW").history(period="6mo")['High'].max()
    except:
        peak_006208 = 249.85

    data_for_web = {
        "taiex": round(taiex_val, 2),
        "ma200": round(ma200_val, 2),
        "vix": round(vix_val, 2),
        "peak_006208": round(peak_006208, 2),
        "asset_006208": round(price_006208, 2) if price_006208 else 249.1,
        "lastUpdated": tw_now.strftime("%Y/%m/%d %H:%M:%S")
    }

    with open('public/data.json', 'w', encoding='utf-8') as f:
        json.dump(data_for_web, f)
    # =================================

    # --- 判斷每日損益，動態生成推播文字 ---
    if daily_diff >= 0:
        msg_body = f"🚀 厲害的阿洲，今天賺了 {int(daily_diff):,} 元 (+{daily_pct:.1f}%)"
    else:
        # daily_pct 本身就是負數，所以直接顯示即可
        msg_body = f"💸 可憐的阿洲，今天賠了 {abs(int(daily_diff)):,} 元 ({daily_pct:.1f}%)"

    # 移除了日期前後的括號
    tg_text = f"✅ {display_date} 結算完畢！\n{msg_body}\n\n@PRStK Lab & SFC.e. All right reserve"

    # --- 傳送 Telegram 訊息 ---
    keyboard = {
        "inline_keyboard": [
            [{"text": "🦎 Growth 儀表板", "web_app": {"url": WEB_APP_URL}}],
            # 填入您剛架設好的 Skynet GitHub Pages 網址！
            [{"text": "📡 Skynet Monitoring", "web_app": {"url": "https://hanjhou2000716.github.io/skynet-monitoring/"}}]
        ]
    }
    
    requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={
        "chat_id": TELEGRAM_CHAT_ID, 
        "text": tg_text,
        "parse_mode": "Markdown",
        "reply_markup": keyboard
    })

if __name__ == "__main__":
    main()
