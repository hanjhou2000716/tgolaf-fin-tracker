import os
import json
import requests
import datetime
import math
import re
import yfinance as yf
import gspread
from google.oauth2.service_account import Credentials
import urllib.parse
from html2image import Html2Image

# 設定 matplotlib 在無 GUI 環境下執行
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

# ==========================================
# 1. 環境變數與金鑰設定
# ==========================================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
FINMIND_TOKEN = os.getenv("FINMIND_TOKEN")
GCP_CREDENTIALS_JSON = os.getenv("GCP_CREDENTIALS")

# 設定中文字型 (優先使用 Noto Sans CJK)
plt.rcParams['font.sans-serif'] = ['Noto Sans CJK TC', 'Microsoft JhengHei', 'sans-serif']
plt.rcParams['axes.unicode_minus'] = False 

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
        if "PRStK" in s.title:
            sheet = s
            break
    if not sheet:
        for s in available_sheets:
            if "Growth" in s.title or "資產" in s.title:
                sheet = s
                break
    if not sheet:
        seen_names = [s.title for s in available_sheets]
        raise ValueError(f"\n❌ 找不到檔案！機器人目前看得到的檔案：{seen_names}。")
        
    print(f"✅ 雷達鎖定成功！正在打開試算表：{sheet.title}")
    
    data_rows = []
    history_sheet = None
    for ws in sheet.worksheets():
        title_clean = ws.title.strip().lower()
        if "history" in title_clean or "歷史" in title_clean or "紀錄" in title_clean:
            history_sheet = ws
        elif "表單" in title_clean or "form" in title_clean or "回覆" in title_clean or "異動" in title_clean:
            rows = ws.get_all_values()
            if len(rows) > 1:
                data_rows.extend(rows[1:])
                
    if not data_rows:
        return {}, history_sheet
        
    def parse_date(row):
        if not row: return datetime.datetime.min
        ts_str = str(row[0]).strip()
        match = re.search(r'(\d{4})[/-](\d{1,2})[/-](\d{1,2})(?:\s+(上午|下午|AM|PM)?\s*(\d{1,2}):(\d{1,2}):(\d{1,2}))?', ts_str, re.IGNORECASE)
        if match:
            y, m, d, ampm, h, mnt, s = match.groups()
            h = int(h) if h else 0
            mnt = int(mnt) if mnt else 0
            s = int(s) if s else 0
            if ampm in ['下午', 'PM', 'pm'] and h < 12: h += 12
            if ampm in ['上午', 'AM', 'am'] and h == 12: h = 0
            try:
                return datetime.datetime(int(y), int(m), int(d), h, mnt, s)
            except:
                pass
        return datetime.datetime.min

    data_rows.sort(key=parse_date)

    inventory = {
        "台股": {}, "美股": {}, "基金": {}, 
        "現金_TWD": {"TWD": 0.0}, "現金_USD": {"USD": 0.0},
        "質押負債": {"Current_Debt": 0.0, "History": []},
        "質押利率": {"Rate": 3.3, "History": []},
        "擔保品": {}  
    }
    
    symbol_overrides = {
        '6208': '006208', '403A': '00403A', '886': '00886', 
        '895': '00895', '878': '00878', '685L': '00685L'
    }
    
    known_symbols = [
        '6208', '006208', '403A', '00403A', '886', '00886', '895', '00895',
        '878', '00878', '3455', '8033', '2330', '3665', '685L', '00685L',
        'QQQM', 'NVDA', 'SPYG', 'TSM', 'VOO', 'VTI', 'TSLA', 'AAPL', 'QQQ',
        'FUND', 'TWD', 'USD', 'CURRENT_DEBT', 'RATE'
    ]
    
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
                if unit == '萬':
                    try: cells.append(str(float(num_part) * 10000))
                    except: cells.append(c)
                else:
                    cells.append(num_part)
            else:
                cells.append(c)
        
        asset_type, mode, symbol = "", "", ""
        potential_numbers = []
        
        for cell in cells:
            c_upper = cell.upper()
            if any(x in cell for x in ["台股", "美股", "基金", "現金", "質押", "負債", "擔保", "利率"]):
                asset_type = cell
            elif any(x in cell for x in ["買入", "存入", "賣出", "提領", "取代", "覆蓋", "更新"]):
                mode = cell
            elif c_upper in known_symbols or any(char.isalpha() for char in c_upper):
                if "/" not in cell and "-" not in cell:
                    symbol = cell
            else:
                try:
                    float(cell.replace(",", "").replace("$", ""))
                    potential_numbers.append(cell)
                except ValueError:
                    pass
                    
        if not symbol and len(potential_numbers) >= 2:
            symbol = potential_numbers[0]
            amount_str = potential_numbers[-1]
        elif len(potential_numbers) >= 1:
            amount_str = potential_numbers[-1]
        else:
            amount_str = "0"
            
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
        
        try:
            amount = float(amount_str.replace(",", "").replace("$", ""))
        except ValueError:
            continue
            
        symbol = symbol_overrides.get(symbol, symbol)
        if asset_type in ["現金_TWD", "現金_USD", "質押負債", "質押利率"] and not symbol:
            if asset_type == "現金_TWD": symbol = "TWD"
            elif asset_type == "現金_USD": symbol = "USD"
            elif asset_type == "質押負債": symbol = "Current_Debt"
            elif asset_type == "質押利率": symbol = "Rate"
            
        if not symbol: continue
        if symbol not in inventory[asset_type] and symbol not in ["History"]:
            inventory[asset_type][symbol] = 0.0
            
        if "買入" in mode or "存入" in mode or "+" in mode:
            inventory[asset_type][symbol] += amount
        elif "賣出" in mode or "提領" in mode or "-" in mode:
            inventory[asset_type][symbol] -= amount
        elif "取代" in mode or "覆蓋" in mode or "更新" in mode:
            inventory[asset_type][symbol] = amount

        if asset_type == "質押負債":
            inventory["質押負債"]["History"].append((row_date, inventory["質押負債"]["Current_Debt"]))
        elif asset_type == "質押利率":
            inventory["質押利率"]["History"].append((row_date, inventory["質押利率"]["Rate"]))

    return inventory, history_sheet

# ==========================================
# 3. 金融市場報價模組 (防爬蟲阻擋優化版)
# ==========================================
def get_usd_twd_rate():
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/TWD=X?interval=1d&range=1d"
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        res = requests.get(url, headers=headers, timeout=5)
        return float(res.json()['chart']['result'][0]['meta']['regularMarketPrice'])
    except:
        try: return yf.Ticker("TWD=X").history(period="1d")['Close'].iloc[-1]
        except: return 32.5

def get_us_stock_price(symbol):
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=1d"
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        res = requests.get(url, headers=headers, timeout=5)
        return float(res.json()['chart']['result'][0]['meta']['regularMarketPrice'])
    except:
        try: return yf.Ticker(symbol).history(period="1d")['Close'].iloc[-1]
        except: return 0

def get_tw_stock_price(symbol):
    url = "https://api.finmindtrade.com/api/v4/data"
    start_date = (datetime.date.today() - datetime.timedelta(days=7)).strftime("%Y-%m-%d")
    parameter = {"dataset": "TaiwanStockPrice", "data_id": str(symbol), "start_date": start_date, "token": FINMIND_TOKEN}
    try:
        data = requests.get(url, params=parameter).json()
        return data["data"][-1]["close"] if data["msg"] == "success" else 0
    except: return 0

# ==========================================
# 4. 本地端繪圖模組 (Matplotlib)
# ==========================================
def generate_local_pie_chart(tw_free_val, debt_val, us_val, filename='pie_chart.png'):
    labels = ['🇹🇼 現貨台股', '🦆 質押投資', '🇺🇸 現貨美股']
    sizes = [max(0, tw_free_val), max(0, debt_val), max(0, us_val)]
    colors = ['#36a2eb', '#ff6384', '#ffce56']
    
    if sum(sizes) <= 0:
        labels = ['尚無資產數據']
        sizes = [1]
        colors = ['#cccccc']

    fig, ax = plt.subplots(figsize=(6, 4))
    wedges, texts, autotexts = ax.pie(
        sizes, labels=labels, autopct='%1.1f%%', startangle=90, colors=colors,
        textprops={'color': '#334155', 'weight': 'bold', 'fontsize': 10}
    )
    for autotext in autotexts:
        autotext.set_color('white')
        autotext.set_weight('bold')

    ax.axis('equal') 
    
    # 將圖片背景設為完全透明，並緊縮邊緣
    plt.savefig(filename, bbox_inches='tight', pad_inches=0.1, dpi=120, transparent=True)
    plt.close(fig)
    return filename

def generate_local_line_chart(dates_str, total_data, net_data, total_20ma, net_20ma, twii_ma, filename='line_chart.png'):
    dates = [datetime.datetime.strptime(d, "%Y-%m-%d") for d in dates_str]
    
    fig, ax1 = plt.subplots(figsize=(8, 4.5))
    
    color_total, color_net = '#36a2eb', '#ff6384'
    ax1.set_ylabel('資產金額 (TWD)', color='#334155', fontweight='bold')
    l1 = ax1.plot(dates, total_data, label='總資產', color=color_total, linewidth=2, marker='o', markersize=4)
    l2 = ax1.plot(dates, net_data, label='淨資產', color=color_net, linewidth=2, marker='o', markersize=4)
    ax1.plot(dates, total_20ma, label='總資產 20MA', color='#DAA520', linestyle='--', linewidth=1.5, alpha=0.7)
    ax1.plot(dates, net_20ma, label='淨資產 20MA', color='#DAA520', linestyle='--', linewidth=1.5, alpha=0.7)
    ax1.tick_params(axis='y', labelcolor='#334155')
    
    def currency_fmt(x, pos):
        if x >= 1000000: return f'{x/1000000:.1f}M'
        return f'{x:,.0f}'
    ax1.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(currency_fmt))

    ax2 = ax1.twinx() 
    color_twii = '#9966ff'
    ax2.set_ylabel('加權報酬 (20MA)', color=color_twii, fontweight='bold')
    l3 = ax2.plot(dates, twii_ma, label='加權報酬月線', color=color_twii, linewidth=2, alpha=0.8)
    ax2.tick_params(axis='y', labelcolor=color_twii)
    
    plt.gca().xaxis.set_major_formatter(mdates.DateFormatter('%m-%d'))
    plt.gca().xaxis.set_major_locator(mdates.DayLocator(interval=max(1, len(dates)//6)))
    plt.xticks(rotation=0)
    
    lns = l1 + l2 + l3
    labs = [l.get_label() for l in lns]
    ax1.legend(lns, labs, loc='upper left', frameon=True, shadow=True, fontsize=9)
    plt.grid(True, which='major', linestyle='--', linewidth=0.5, color='#e2e8f0')
    
    plt.tight_layout()
    plt.savefig(filename, bbox_inches='tight', pad_inches=0.1, dpi=120, transparent=True)
    plt.close(fig)
    return filename

# ==========================================
# 5. 主程序與完美的單一長圖 HTML 組裝
# ==========================================
def main():
    tw_now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    today_str = tw_now.strftime("%m-%d")
    
    if 12 <= tw_now.hour <= 20: header_text = f"🇹🇼 PRStK | Growth（{today_str}）"
    else: header_text = f"🇺🇸 PRStK | Growth（{today_str}）"
        
    inventory, history_sheet = calculate_current_assets()
    try: history_records = history_sheet.get_all_records()
    except: history_records = []
        
    usd_rate = get_usd_twd_rate()
    tw_stock_value, us_stock_value_usd, tsmc_exposure_twd, price_006208 = 0, 0, 0, 0
    leveraged_etf_value = 0  
    
    cash_twd = inventory["現金_TWD"].get("TWD", 0)
    cash_usd = inventory["現金_USD"].get("USD", 0)
    fund_value = sum(v for k, v in inventory["基金"].items() if k != "History")

    for symbol, shares in inventory["台股"].items():
        if symbol == "History" or shares <= 0: continue
        price = get_tw_stock_price(symbol)
        value = price * shares
        tw_stock_value += value 
        if symbol == '2330': tsmc_exposure_twd += (value * 1.0)
        elif symbol == '006208': 
            tsmc_exposure_twd += (value * 0.594)
            price_006208 = price
        elif symbol == '00685L': 
            tsmc_exposure_twd += (value * 0.728)
            leveraged_etf_value = value

    pledged_value = 0
    for symbol, shares in inventory["擔保品"].items():
        if symbol == "History" or shares <= 0: continue
        price = price_006208 if (symbol == '006208' and price_006208 > 0) else get_tw_stock_price(symbol)
        pledged_value += price * shares

    for symbol, shares in inventory["美股"].items():
        if symbol == "History" or shares <= 0: continue
        price = get_us_stock_price(symbol)
        value = price * shares
        us_stock_value_usd += value
        if symbol == 'TSM': tsmc_exposure_twd += (value * usd_rate * 1.0)

    us_stock_value_twd = us_stock_value_usd * usd_rate
    total_cash_twd = cash_twd + (cash_usd * usd_rate)
    
    debt = inventory["質押負債"].get("Current_Debt", 0)
    debt_history = inventory["質押負債"].get("History", [])
    rate_history = inventory["質押利率"].get("History", [])

    def get_value_on_date(history_list, target_date, default_val):
        val = default_val
        for d, v in history_list:
            if d <= target_date: val = v
        return val

    loan_start_date = datetime.date(2026, 6, 10) 
    days_passed = max(0, (tw_now.date() - loan_start_date).days)
    
    accumulated_interest = 0
    initial_debt = debt_history[0][1] if debt_history else debt
    
    for i in range(days_passed):
        current_date = loan_start_date + datetime.timedelta(days=i)
        daily_debt = get_value_on_date(debt_history, current_date, initial_debt)
        daily_rate = (get_value_on_date(rate_history, current_date, 3.3) / 100) / 365
        accumulated_interest += daily_debt * daily_rate

    total_debt_with_interest = debt + accumulated_interest
    total_asset = tw_stock_value + us_stock_value_twd + total_cash_twd + fund_value
    net_asset = total_asset - total_debt_with_interest
    
    invested_assets = tw_stock_value + us_stock_value_twd + fund_value
    effective_leverage = ((invested_assets + leveraged_etf_value) / net_asset) if net_asset > 0 else 0
    half_kelly_limit = 0.08 / (2 * (0.18 ** 2))
    
    debt_ratio = ((total_debt_with_interest / total_asset) * 100) if total_asset > 0 else 0
    if total_debt_with_interest > 0:
        maintenance_ratio = (pledged_value / total_debt_with_interest) * 100
        if maintenance_ratio >= 190: ratio_status = "🟢 安全"
        elif maintenance_ratio >= 150: ratio_status = "🟡 注意"
        elif maintenance_ratio >= 130: ratio_status = "🔴 警戒"
        else: ratio_status = "🆘 危險"
    else:
        maintenance_ratio, ratio_status = 0, "✅ 無借款"

    tw_free_value = max(0, tw_stock_value - total_debt_with_interest)
    tsmc_pct = (tsmc_exposure_twd / total_asset) * 100 if total_asset > 0 else 0

    yesterday_net = 0
    for row in reversed(history_records):
        val = float(str(row.get('Net_Asset', 0)).replace(',', ''))
        row_date = str(row.get('Date', ''))[-5:]
        if val > 0 and row_date != today_str:
            yesterday_net = val
            break

    daily_diff = net_asset - yesterday_net if yesterday_net else 0
    daily_pct = (daily_diff / yesterday_net * 100) if yesterday_net else 0
    sign, emoji = ("+", "📈") if daily_diff >= 0 else ("", "📉")
    daily_str_plain = f"單日變化：{sign}{daily_pct:.1f}% ({sign}${daily_diff:,.0f})" if yesterday_net else "單日變化：--"

    progress_pct = (net_asset / 10000000) * 100 if net_asset > 0 else 0
    bar_blocks = max(0, min(10, int(progress_pct / 10)))
    bar_str = "█" * bar_blocks + "░" * (10 - bar_blocks)

    if total_asset > 0:
        history_sheet.append_row([tw_now.strftime("%Y-%m-%d"), round(total_asset, 2), round(net_asset, 2), total_debt_with_interest, round(tsmc_exposure_twd, 2)])

    daily_net_history, daily_total_history = {}, {}
    for row in history_records:
        date_str = str(row.get('Date', ''))[:10]
        net_val = float(str(row.get('Net_Asset', 0)).replace(',', ''))
        total_val = float(str(row.get('Total_Asset', 0)).replace(',', ''))
        if net_val > 0 and len(date_str) == 10:
            daily_net_history[date_str] = net_val
            daily_total_history[date_str] = total_val
            
    daily_net_history[tw_now.strftime("%Y-%m-%d")] = net_asset
    daily_total_history[tw_now.strftime("%Y-%m-%d")] = total_asset
    sorted_dates = sorted(daily_net_history.keys())

    recent_dates_str = sorted_dates[-30:]
    recent_total = [daily_total_history[d] for d in recent_dates_str]
    recent_net = [daily_net_history[d] for d in recent_dates_str]
    
    total_20ma, net_20ma, twii_ma = [], [], []
    all_totals = [daily_total_history[d] for d in sorted_dates]
    all_nets = [daily_net_history[d] for d in sorted_dates]
    for i in range(len(sorted_dates)):
        if sorted_dates[i] in recent_dates_str:
            start = max(0, i - 19)
            total_20ma.append(sum(all_totals[start:i+1]) / len(all_totals[start:i+1]))
            net_20ma.append(sum(all_nets[start:i+1]) / len(all_nets[start:i+1]))

    try:
        twii_hist = yf.Ticker("0050.TW").history(period="3mo")
        if twii_hist.empty: twii_hist = yf.Ticker("^TWII").history(period="3mo")
        twii_hist['20MA'] = twii_hist['Close'].rolling(window=20).mean()
        twii_map = {idx.strftime("%Y-%m-%d"): row['20MA'] for idx, row in twii_hist.iterrows() if not math.isnan(row['20MA'])}
        last_ma = list(twii_map.values())[-1] if twii_map else 0
        for d_str in recent_dates_str: twii_ma.append(twii_map.get(d_str, last_ma))
    except: twii_ma = [0] * len(recent_dates_str)

    pie_chart_file = generate_local_pie_chart(tw_free_value, total_debt_with_interest, us_stock_value_twd)
    line_chart_file = generate_local_line_chart(recent_dates_str, recent_total, recent_net, total_20ma, net_20ma, twii_ma)
    
    def get_growth_str(target_days, sim_text):
        if not sorted_dates: return f"{sim_text}(模)"
        target_date_obj = tw_now.date() - datetime.timedelta(days=target_days)
        closest_date, min_diff = None, 9999
        for d_str in sorted_dates:
            try:
                diff = abs((datetime.datetime.strptime(d_str, "%Y-%m-%d").date() - target_date_obj).days)
                if diff < min_diff: min_diff, closest_date = diff, d_str
            except: continue
        if closest_date and min_diff <= max(7, target_days * 0.2):
            rate = ((net_asset - daily_net_history[closest_date]) / daily_net_history[closest_date]) * 100
            return f"{'+' if rate >= 0 else ''}{rate:.1f}%(實)"
        return f"{sim_text}(模)"

    m1_str, m3_str = get_growth_str(30, "+10.7%"), get_growth_str(90, "+215.9%")
    y1_str, y3_str = get_growth_str(365, "+83.1%"), get_growth_str(1095, "+195.7%")

    calc_rate = 0.015
    if len(sorted_dates) >= 2:
        td = (tw_now.date() - datetime.datetime.strptime(sorted_dates[0], "%Y-%m-%d").date()).days
        if td >= 1: calc_rate = max(0.015, ((net_asset - daily_net_history[sorted_dates[0]]) / daily_net_history[sorted_dates[0]]) / (td / 30))
    
    timeline_events = [{"year": 2026, "month": 10, "text": "2026-10: 🎖️ 成功嶺退伍"}]
    for t in [{"name": "850萬", "value": 8500000}, {"name": "1000萬", "value": 10000000}, {"name": "100萬鎂", "value": 1000000 * usd_rate}]:
        if max(net_asset, 1) >= t["value"]: timeline_events.append({"year": 0, "month": 0, "text": f"已達標: {t['name']} ✅"})
        else:
            tm = tw_now.date().month + int(math.log(t["value"] / max(net_asset, 1)) / math.log(1 + calc_rate))
            timeline_events.append({"year": tw_now.date().year + (tm - 1) // 12, "month": (tm - 1) % 12 + 1, "text": f"{tw_now.date().year + (tm - 1) // 12}-{((tm - 1) % 12 + 1):02d}: {t['name']} 達標"})
            
    timeline_events.sort(key=lambda x: (x["year"], x["month"]))
    timeline_html = "".join([f'<li style="margin-bottom:6px; display:flex; align-items:center;"><span style="color:#3b82f6; margin-right:8px;">➜</span>{t["text"]}</li>' for t in timeline_events])

    # ==========================================
    # 6. 組合完美比例 HTML
    # ==========================================
    css = """
    @import url('https://fonts.googleapis.com/css2?family=Noto+Sans+TC:wght@400;700;900&display=swap');
    body { font-family: 'Noto Sans TC', sans-serif; background-color: #f1f5f9; margin: 0; padding: 15px; width: 540px; }
    .card-wrap { background: white; border-radius: 12px; padding: 16px; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1); border: 1px solid #e2e8f0; margin-bottom: 12px; }
    .title { font-size: 16px; font-weight: 900; color: #1e293b; margin: 0 0 12px 0; border-left: 4px solid #3b82f6; padding-left: 10px; }
    .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
    .box { background: #f8fafc; border-radius: 8px; padding: 10px; border: 1px solid #e2e8f0; }
    .t { font-size: 12px; color: #64748b; font-weight: 700; margin-bottom: 2px; }
    .v { font-size: 18px; font-weight: 900; color: #0f172a; }
    .s { font-size: 11px; color: #94a3b8; margin-top: 2px; display: block; }
    .badge { background: #f8fafc; padding: 5px 8px; border-radius: 6px; font-weight: 700; color: #475569; font-size: 12px; border: 1px solid #e2e8f0; text-align:center;}
    .h-wrap { background: #0f172a; color: white; padding: 15px; border-radius: 12px 12px 0 0; margin-bottom: 0; margin-top: -15px; margin-left: -15px; margin-right: -15px; }
    .h-wrap h1 { margin: 0; font-size: 20px; font-weight: 900; }
    .h-wrap p { margin: 5px 0 0 0; font-size: 11px; color: #94a3b8; }
    .s-wrap { background: linear-gradient(135deg, #f97316, #ea580c); color: white; padding: 16px; border-radius: 0 0 12px 12px; margin-bottom: 12px; margin-left: -15px; margin-right: -15px; }
    .s-val { font-size: 26px; font-weight: 900; margin: 5px 0; }
    .s-diff { font-size: 13px; background: rgba(0,0,0,0.2); display: inline-block; padding: 3px 10px; border-radius: 20px; font-weight: 700; }
    .chart-container { display: flex; flex-direction: column; align-items: center; justify-content: center; width: 100%; gap: 15px; padding: 10px 0;}
    .chart-img { max-width: 100%; height: auto; border-radius: 8px; }
    """

    full_html = f"""
    <html><head><meta charset="UTF-8"><style>{css}</style></head><body>
    <div class="h-wrap"><h1>{header_text}</h1><p>{tw_now.strftime("%Y/%m/%d %H:%M CST")}</p></div>
    <div class="s-wrap">
        <div style="font-size: 13px; font-weight:700;">💰 總資產：${total_asset:,.0f}</div>
        <div class="s-val">🟢 淨額：${net_asset:,.0f}</div>
        <div class="s-diff">{emoji} {daily_str_plain}</div>
    </div>

    <div class="card-wrap">
        <div class="title">📂 核心資產 (現貨與基金)</div>
        <div class="grid">
            <div class="box"><div class="t">🇹🇼 台股現值</div><div class="v">${tw_stock_value:,.0f}</div></div>
            <div class="box"><div class="t">🇺🇸 美股現值</div><div class="v">${us_stock_value_twd:,.0f}</div><span class="s">約 ${us_stock_value_usd:,.0f} USD</span></div>
            <div class="box"><div class="t">🐣 基金現值</div><div class="v">${fund_value:,.0f}</div></div>
            <div class="box"><div class="t">🐔 TSMC 總曝險</div><div class="v">{tsmc_pct:.1f}%</div><span class="s">佔總資產比例</span></div>
        </div>
    </div>

    <div class="card-wrap">
        <div class="title">💵 資金與借貸部位</div>
        <div class="grid">
            <div class="box"><div class="t">💵 現金 (TWD)</div><div class="v">${cash_twd:,.0f}</div></div>
            <div class="box"><div class="t">💴 現金 (USD)</div><div class="v">${cash_usd * usd_rate:,.0f}</div><span class="s">約 ${cash_usd:,.0f} USD</span></div>
            <div class="box"><div class="t">💸 質押借款</div><div class="v" style="color:#ef4444">-${total_debt_with_interest:,.0f}</div><span class="s">內含利息 ${accumulated_interest:,.0f}</span></div>
            <div class="box"><div class="t">🕸️ 資產負債比</div><div class="v">{debt_ratio:.1f}%</div></div>
        </div>
    </div>

    <div class="card-wrap">
        <div class="title">🛡️ 槓桿與風險監控</div>
        <div class="grid">
            <div class="box"><div class="t">⚖️ 總資產 Beta</div><div class="v">{effective_leverage:.2f}x</div><span class="s">凱利邊界: {half_kelly_limit:.2f}x</span></div>
            <div class="box"><div class="t">🦾 質押維持率</div><div class="v" style="color:{ratio_color}">{maintenance_ratio:.1f}%</div><span class="s">狀態: {ratio_status}</span></div>
        </div>
    </div>

    <div class="card-wrap">
        <div class="title">🚀 歷史與預測</div>
        <div class="grid" style="margin-bottom:12px; grid-template-columns: repeat(4, 1fr); gap: 6px;">
            <div class="badge">{m1_str}</div> <div class="badge">{m3_str}</div>
            <div class="badge">{y1_str}</div> <div class="badge">{y3_str}</div>
        </div>
        <div class="t" style="margin-bottom:4px;">千萬目標達成率 ({progress_pct:.1f}%)</div>
        <div style="font-family:monospace; color:#3b82f6; font-size:14px; margin-bottom: 10px;">{bar_str}</div>
        <ul style="padding:0; margin:0; list-style:none; font-size:12px; font-weight:500; color:#1e293b;">{timeline_html}</ul>
    </div>

    <div class="card-wrap" style="margin-bottom: 0;">
        <div class="title">📊 資產軌跡與分佈</div>
        <div class="chart-container">
            <img src="{line_chart_file}" class="chart-img">
            <img src="{pie_chart_file}" class="chart-img" style="margin-top: 5px;">
        </div>
    </div>
    </body></html>
    """

    # ==========================================
    # 7. 截圖發送與清除暫存
    # ==========================================
    dashboard_file = 'dashboard_full.png'
    hti = Html2Image(custom_flags=['--no-sandbox', '--disable-gpu', '--hide-scrollbars'])
    
    with open('temp_full.html', 'w', encoding='utf-8') as f:
        f.write(full_html)
        
    # 高度設為稍微寬裕的值，確保絕對不裁切，配合背景色可以無縫接軌
    try: hti.screenshot(html_file='temp_full.html', save_as=dashboard_file, size=(540, 1950))
    except Exception as e: print(f"截圖失敗:", e)

    keyboard = {
        "inline_keyboard": [
            [{"text": "📝 Growth 填寫表單", "url": "https://forms.gle/9ZEJawwNRGfiXQiV8"}],
            [{"text": "📈 Skynet 儀表板", "url": "https://5972x4.csb.app/"}]
        ]
    }
    
    msg_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    with open(dashboard_file, "rb") as f:
        requests.post(msg_url, data={
            "chat_id": TELEGRAM_CHAT_ID, 
            "caption": "✅ **日報結算完畢！**\n為您送上最新的完整 Growth 儀表板。",
            "parse_mode": "Markdown",
            "reply_markup": json.dumps(keyboard) 
        }, files={"photo": f})

    for f in ['temp_full.html', dashboard_file, line_chart_file, pie_chart_file]:
        if os.path.exists(f): os.remove(f)

if __name__ == "__main__":
    main()
