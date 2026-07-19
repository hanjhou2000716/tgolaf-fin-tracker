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

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
FINMIND_TOKEN = os.getenv("FINMIND_TOKEN")
GCP_CREDENTIALS_JSON = os.getenv("GCP_CREDENTIALS")

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
        
        asset_type = ""
        mode = ""
        symbol = ""
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

def get_usd_twd_rate():
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/TWD=X?interval=1d&range=1d"
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
        res = requests.get(url, headers=headers, timeout=5)
        return float(res.json()['chart']['result'][0]['meta']['regularMarketPrice'])
    except:
        try: return yf.Ticker("TWD=X").history(period="1d")['Close'].iloc[-1]
        except: return 32.5

def get_us_stock_price(symbol):
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=1d"
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
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

def generate_pie_chart(tw_free_val, debt_val, us_val):
    chart_config = {
        "type": "outlabeledPie",
        "data": {
            "labels": ["🇹🇼 現貨台股", "🦆 質押投資", "🇺🇸 現貨美股"],
            "datasets": [{"backgroundColor": ["#36a2eb", "#ff6384", "#ffce56"], "data": [tw_free_val, debt_val, us_val]}]
        },
        "options": {
            "plugins": {
                "legend": {"display": False},
                "outlabels": {"text": "%l %p", "color": "white", "stretch": 25, "font": {"minSize": 12}}
            }
        }
    }
    if tw_free_val <= 0 and debt_val <= 0 and us_val <= 0:
        chart_config["data"]["labels"] = ["尚無資產數據"]
        chart_config["data"]["datasets"][0]["data"] = [1]
        chart_config["data"]["datasets"][0]["backgroundColor"] = ["#cccccc"]
    # 固定長寬為 510x250
    return f"https://quickchart.io/chart?c={urllib.parse.quote(json.dumps(chart_config))}&w=510&h=250"
        
def generate_line_chart(history_records, today_str, total_asset, net_asset):
    daily_data = {}
    for row in history_records:
        date_str = str(row.get('Date', ''))
        if not date_str: continue
        d_short = date_str[-5:]
        total = float(str(row.get('Total_Asset', 0)).replace(',', ''))
        net = float(str(row.get('Net_Asset', 0)).replace(',', ''))
        if total > 0:
            if d_short not in daily_data:
                daily_data[d_short] = {'total': [], 'net': []}
            daily_data[d_short]['total'].append(total)
            daily_data[d_short]['net'].append(net)
            
    if today_str not in daily_data:
        daily_data[today_str] = {'total': [], 'net': []}
    daily_data[today_str]['total'].append(total_asset)
    daily_data[today_str]['net'].append(net_asset)
    
    unique_dates = list(daily_data.keys())
    historical_totals = [daily_data[d]['total'][-1] for d in unique_dates]
    historical_nets = [daily_data[d]['net'][-1] for d in unique_dates]
    
    total_ma_map, net_ma_map = {}, {}
    for i, d in enumerate(unique_dates):
        start = max(0, i - 19)
        t_win = historical_totals[start:i+1]
        n_win = historical_nets[start:i+1]
        total_ma_map[d] = sum(t_win) / len(t_win)
        net_ma_map[d] = sum(n_win) / len(n_win)
    
    twii_map = {}
    try:
        twii_hist = yf.Ticker("0050.TW").history(period="3mo")
        if twii_hist.empty: twii_hist = yf.Ticker("^TWII").history(period="3mo")
        twii_hist['20MA'] = twii_hist['Close'].rolling(window=20).mean()
        for idx, row in twii_hist.iterrows():
            if not math.isnan(row['20MA']):
                twii_map[idx.strftime("%m-%d")] = row['20MA']
    except Exception as e:
        print("大盤資料抓取錯誤:", e)
        
    recent_days = unique_dates[-30:]
    dates, total_data, net_data = [], [], []
    total_20ma_data, net_20ma_data, twii_ma_data = [], [], []
    
    last_twii_ma = None
    if twii_map:
        for d in recent_days:
            if d in twii_map:
                last_twii_ma = twii_map[d]
                break
        if not last_twii_ma: last_twii_ma = list(twii_map.values())[-1]
            
    for d in recent_days:
        dates.append(d)
        total_data.append(round(daily_data[d]['total'][-1], 2))
        net_data.append(round(daily_data[d]['net'][-1], 2))
        total_20ma_data.append(round(total_ma_map[d], 2))
        net_20ma_data.append(round(net_ma_map[d], 2))
        if d in twii_map: last_twii_ma = twii_map[d]
        twii_ma_data.append(last_twii_ma)
        
    normalized_twii_ma, base_net, base_twii = [], None, None
    for i in range(len(dates)):
        if net_data[i] is not None and twii_ma_data[i] is not None:
            base_net = net_data[i] * 0.95 
            base_twii = twii_ma_data[i]
            break
            
    if base_net and base_twii:
        scale_ratio = base_net / base_twii
        for val in twii_ma_data:
            if val is not None: normalized_twii_ma.append(round(val * scale_ratio, 2))
            else: normalized_twii_ma.append(None)
    else:
        normalized_twii_ma = [None] * len(twii_ma_data)
        
    all_vals = total_data + net_data + [x for x in normalized_twii_ma if x is not None]
    if all_vals:
        min_val, max_val = min(all_vals), max(all_vals)
        y_min = math.floor(min_val / 200000) * 200000
        y_max = math.ceil(max_val / 200000) * 200000
        if y_min == y_max: y_min -= 200000; y_max += 200000
    else:
        y_min, y_max = 0, 1000000
    
    chart_config = {
        "type": "line",
        "data": {
            "labels": dates,
            "datasets": [
                {"label": "總資產", "data": total_data, "borderColor": "#36a2eb", "fill": False, "tension": 0.1},
                {"label": "淨資產", "data": net_data, "borderColor": "#ff6384", "fill": False, "tension": 0.1},
                {"label": "總資產月線", "data": total_20ma_data, "borderColor": "#DAA520", "borderWidth": 2, "borderDash": [5, 5], "fill": False, "pointRadius": 0},
                {"label": "淨資產月線", "data": net_20ma_data, "borderColor": "#DAA520", "borderWidth": 2, "borderDash": [5, 5], "fill": False, "pointRadius": 0},
                {"label": "加權報酬月線", "data": normalized_twii_ma, "borderColor": "#9966ff", "borderWidth": 2, "fill": False, "pointRadius": 0}
            ]
        },
        "options": {
            "title": {"display": False},
            "scales": {"yAxes": [{"ticks": {"min": y_min, "max": y_max, "stepSize": 200000}}]},
            "legend": {"position": "bottom"}
        }
    }
    # 固定長寬為 510x250
    return f"https://quickchart.io/chart?c={urllib.parse.quote(json.dumps(chart_config))}&w=510&h=250"

def main():
    tw_now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    today_str = tw_now.strftime("%m-%d")
    
    if 12 <= tw_now.hour <= 20:
        header_text = f"🇹🇼 PRStK | Growth（{today_str}）"
    else:
        header_text = f"🇺🇸 PRStK | Growth（{today_str}）"
        
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
        if symbol == '006208' and price_006208 > 0:
            price = price_006208
        else:
            price = get_tw_stock_price(symbol)
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
    end_date = tw_now.date()
    days_passed = max(0, (end_date - loan_start_date).days)
    
    accumulated_interest = 0
    initial_debt = debt_history[0][1] if debt_history else debt
    
    for i in range(days_passed):
        current_date = loan_start_date + datetime.timedelta(days=i)
        daily_debt = get_value_on_date(debt_history, current_date, initial_debt)
        daily_annual_rate = get_value_on_date(rate_history, current_date, 3.3) 
        daily_rate = (daily_annual_rate / 100) / 365
        accumulated_interest += daily_debt * daily_rate

    total_debt_with_interest = debt + accumulated_interest
    total_asset = tw_stock_value + us_stock_value_twd + total_cash_twd + fund_value
    net_asset = total_asset - total_debt_with_interest
    
    invested_assets = tw_stock_value + us_stock_value_twd + fund_value
    effective_leverage = ((invested_assets + leveraged_etf_value) / net_asset) if net_asset > 0 else 0
    
    expected_excess_return = 0.08  
    market_volatility = 0.18       
    half_kelly_limit = expected_excess_return / (2 * (market_volatility ** 2))
    
    kelly_utilization = (effective_leverage / half_kelly_limit) * 100 if half_kelly_limit > 0 else 0
    if kelly_utilization > 100: kelly_status = "🔴"
    elif kelly_utilization > 80: kelly_status = "🟡"
    else: kelly_status = "🟢"
    
    debt_ratio = ((total_debt_with_interest / total_asset) * 100) if total_asset > 0 else 0
    
    if total_debt_with_interest > 0:
        maintenance_ratio = (pledged_value / total_debt_with_interest) * 100
        if maintenance_ratio >= 190: ratio_status = "🟢 安全"
        elif maintenance_ratio >= 150: ratio_status = "🟡 注意"
        elif maintenance_ratio >= 130: ratio_status = "🔴 警戒"
        else: ratio_status = "🆘 危險"
    else:
        maintenance_ratio = 0
        ratio_status = "✅ 無借款"

    tw_free_value = max(0, tw_stock_value - total_debt_with_interest)
    tw_free_pct = (tw_free_value / total_asset) * 100 if total_asset > 0 else 0
    debt_pct = (total_debt_with_interest / total_asset) * 100 if total_asset > 0 else 0
    us_pct = (us_stock_value_twd / total_asset) * 100 if total_asset > 0 else 0
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
    sign = "+" if daily_diff >= 0 else ""
    emoji = "📈" if daily_diff >= 0 else "📉"
    
    daily_str_plain = f"單日變化：{sign}{daily_pct:.1f}% ({sign}${daily_diff:,.0f})" if yesterday_net else "單日變化：-- (首日)"

    progress_pct = (net_asset / 10000000) * 100 if net_asset > 0 else 0
    bar_blocks = max(0, min(10, int(progress_pct / 10)))
    bar_str = "█" * bar_blocks + "░" * (10 - bar_blocks)

    if total_asset > 0:
        history_sheet.append_row([
            tw_now.strftime("%Y-%m-%d"), 
            round(total_asset, 2), round(net_asset, 2), total_debt_with_interest, round(tsmc_exposure_twd, 2)
        ])

    pie_url = generate_pie_chart(tw_free_value, total_debt_with_interest, us_stock_value_twd)
    line_url = generate_line_chart(history_records, today_str, total_asset, net_asset)

    daily_net_history = {}
    for row in history_records:
        date_str = str(row.get('Date', ''))[:10]  
        val = float(str(row.get('Net_Asset', 0)).replace(',', ''))
        if val > 0 and len(date_str) == 10:
            daily_net_history[date_str] = val
            
    tw_now_date_str = tw_now.strftime("%Y-%m-%d")
    daily_net_history[tw_now_date_str] = net_asset
    sorted_dates = sorted(daily_net_history.keys())

    def get_growth_str(target_days, sim_text):
        if not sorted_dates: return f"{sim_text}(模)"
        target_date_obj = tw_now.date() - datetime.timedelta(days=target_days)
        closest_date = None
        min_diff = 9999
        for d_str in sorted_dates:
            try:
                d_obj = datetime.datetime.strptime(d_str, "%Y-%m-%d").date()
                diff = abs((d_obj - target_date_obj).days)
                if diff < min_diff:
                    min_diff = diff
                    closest_date = d_str
            except: continue
                
        tolerance = max(7, target_days * 0.2) 
        if closest_date and min_diff <= tolerance:
            past_net = daily_net_history[closest_date]
            rate = ((net_asset - past_net) / past_net) * 100
            s = "+" if rate >= 0 else ""
            return f"{s}{rate:.1f}%(實)"
        else:
            return f"{sim_text}(模)"

    m1_str = get_growth_str(30, "+10.7%")
    m3_str = get_growth_str(90, "+215.9%")
    y1_str = get_growth_str(365, "+83.1%")
    y3_str = get_growth_str(1095, "+195.7%")

    if len(sorted_dates) >= 2:
        first_date_str = sorted_dates[0]
        first_net = daily_net_history[first_date_str]
        first_date_obj = datetime.datetime.strptime(first_date_str, "%Y-%m-%d").date()
        total_days_recorded = (tw_now.date() - first_date_obj).days
        monthly_growth_rate = ((net_asset - first_net) / first_net) / (total_days_recorded / 30) if total_days_recorded >= 1 else 0.015
    else:
        monthly_growth_rate = 0.015 

    calc_rate = max(monthly_growth_rate, 0.015) 
    safe_net_asset = max(net_asset, 1)          
    
    targets = [
        {"name": "850萬", "value": 8500000},
        {"name": "1000萬", "value": 10000000},
        {"name": "100萬鎂", "value": 1000000 * usd_rate}
    ]
    
    timeline_events = [{"year": 2026, "month": 10, "text": "2026-10: 🎖️ 成功嶺退伍"}]
    for t in targets:
        if safe_net_asset >= t["value"]:
            timeline_events.append({"year": 0, "month": 0, "text": f"已達標: {t['name']} ✅"})
        else:
            months_needed = math.log(t["value"] / safe_net_asset) / math.log(1 + calc_rate)
            tm = tw_now.date().month + int(months_needed)
            ty = tw_now.date().year + (tm - 1) // 12
            fm = (tm - 1) % 12 + 1
            timeline_events.append({"year": ty, "month": fm, "text": f"{ty}-{fm:02d}: {t['name']} 達標"})
            
    timeline_events.sort(key=lambda x: (x["year"], x["month"]))
    time_str = tw_now.strftime("%Y/%m/%d %H:%M CST")
    ratio_color = "#ef4444" if maintenance_ratio < 150 else "#10b981"
    
    timeline_html = "".join([f'<li style="margin-bottom:6px; display:flex; align-items:center;"><span style="color:#3b82f6; margin-right:8px;">➜</span>{t["text"]}</li>' for t in timeline_events])
    
    # 終極修復重點：HTML 中強制綁定 <img> 的 width 與 height，並且主 div 限定 1750px 高度。
    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
    <meta charset="UTF-8">
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Noto+Sans+TC:wght@400;700;900&display=swap');
        body {{ font-family: 'Noto Sans TC', sans-serif; background-color: #e2e8f0; margin: 0; padding: 0; }}
        
        .main-wrapper {{ width: 540px; height: 1750px; background-color: #f8fafc; padding: 15px; box-sizing: border-box; overflow: hidden; }}
        
        .header {{ background-color: #0f172a; color: white; padding: 20px; border-radius: 12px; margin-bottom: -15px; }}
        .header h1 {{ margin: 0; font-size: 24px; font-weight: 900; }}
        .header p {{ margin: 4px 0 0 0; font-size: 13px; color: #94a3b8; }}
        
        .summary-box {{ background: linear-gradient(135deg, #f97316, #ea580c); color: white; padding: 20px; border-radius: 12px; position: relative; z-index: 10; margin-bottom: 15px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); }}
        .s-val {{ font-size: 28px; font-weight: 900; margin: 8px 0; }}
        .s-diff {{ font-size: 14px; background: rgba(0,0,0,0.2); display: inline-block; padding: 4px 10px; border-radius: 16px; font-weight: 700; }}
        
        .section-title {{ font-size: 16px; font-weight: 900; color: #334155; margin: 0 0 10px 5px; border-left: 4px solid #3b82f6; padding-left: 8px; }}
        
        .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 15px; }}
        .card {{ background: white; border-radius: 10px; padding: 12px; border: 1px solid #e2e8f0; }}
        
        .c-title {{ font-size: 12px; color: #64748b; font-weight: 700; margin-bottom: 4px; }}
        .c-val {{ font-size: 18px; font-weight: 900; color: #0f172a; }}
        .c-sub {{ font-size: 11px; color: #94a3b8; margin-top: 2px; font-weight: 500; display: block; }}
        
        .badge-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-bottom: 15px; }}
        .badge {{ background: white; padding: 8px; border-radius: 8px; font-weight: 700; color: #475569; font-size: 13px; border: 1px solid #e2e8f0; text-align: center; }}
        
        .chart-box {{ background: white; border-radius: 10px; padding: 10px; border: 1px solid #e2e8f0; margin-bottom: 15px; text-align: center; }}
    </style>
    </head>
    <body>
      <div class="main-wrapper">
        <!-- Header & Summary -->
        <div class="header">
            <h1>{header_text}</h1>
            <p>{time_str}</p>
        </div>
        <div class="summary-box">
            <div style="font-size: 14px; font-weight:700;">💰 總資產：${total_asset:,.0f}</div>
            <div class="s-val">🟢 淨額：${net_asset:,.0f}</div>
            <div class="s-diff">{emoji} {daily_str_plain}</div>
        </div>

        <!-- 區塊 1: 資產 -->
        <div class="section-title">📂 資產明細</div>
        <div class="grid">
            <div class="card"><div class="c-title">🇹🇼 台股現值</div><div class="c-val">${tw_stock_value:,.0f}</div></div>
            <div class="card"><div class="c-title">🇺🇸 美股現值</div><div class="c-val">${us_stock_value_twd:,.0f}</div><span class="c-sub">約 ${us_stock_value_usd:,.0f} USD</span></div>
            <div class="card"><div class="c-title">💵 現金 (TWD)</div><div class="c-val">${cash_twd:,.0f}</div></div>
            <div class="card"><div class="c-title">💴 現金 (USD)</div><div class="c-val">${cash_usd * usd_rate:,.0f}</div><span class="c-sub">約 ${cash_usd:,.0f} USD</span></div>
            <div class="card"><div class="c-title">🐣 基金現值</div><div class="c-val">${fund_value:,.0f}</div></div>
            <div class="card"><div class="c-title">💸 質押借款</div><div class="c-val" style="color:#ef4444">-${total_debt_with_interest:,.0f}</div><span class="c-sub">內含利息 ${accumulated_interest:,.0f}</span></div>
        </div>

        <!-- 區塊 2: 風險 -->
        <div class="section-title">🛡️ 風險監控</div>
        <div class="grid">
            <div class="card"><div class="c-title">⚖️ 總資產Beta</div><div class="c-val">{effective_leverage:.2f}x</div><span class="c-sub">凱利邊界: {half_kelly_limit:.2f}x</span></div>
            <div class="card"><div class="c-title">🐔 TSMC Exposure</div><div class="c-val">{tsmc_pct:.1f}%</div></div>
            <div class="card"><div class="c-title">🕸️ 資產負債比</div><div class="c-val">{debt_ratio:.1f}%</div></div>
            <div class="card"><div class="c-title">🦾 質押維持率</div><div class="c-val" style="color:{ratio_color}">{maintenance_ratio:.1f}%</div><span class="c-sub">狀態: {ratio_status}</span></div>
        </div>

        <!-- 區塊 3: 歷史 -->
        <div class="section-title">🚀 歷史增率</div>
        <div class="badge-grid">
            <div class="badge">1月: {m1_str}</div> 
            <div class="badge">1季: {m3_str}</div>
            <div class="badge">1年: {y1_str}</div> 
            <div class="badge">3年: {y3_str}</div>
        </div>
        
        <!-- 區塊 4: 預測 -->
        <div class="section-title">🎯 模型預測</div>
        <div class="card" style="margin-bottom:15px;">
            <div class="c-title" style="margin-bottom:6px;">千萬目標達成率 ({progress_pct:.1f}%)</div>
            <div style="font-family:monospace; color:#3b82f6; font-size:15px; margin-bottom: 8px;">{bar_str}</div>
            <ul style="padding:0; margin:0; list-style:none; font-size:13px; font-weight:500; color:#1e293b;">{timeline_html}</ul>
        </div>

        <!-- 區塊 5: 圖表 (強制鎖定長寬，防止異步加載造成的裁切) -->
        <div class="section-title">📊 視覺化分析</div>
        <div class="chart-box" style="padding-bottom: 5px;">
            <div style="font-size:13px; font-weight:700; color:#475569; margin-bottom:4px; text-align:left;">近期資產軌跡 (含月線 20MA)</div>
            <img src="{line_url}" width="510" height="250" style="display:block; border-radius:6px; background:#fff;">
        </div>
        <div class="chart-box" style="margin-bottom: 0;">
            <div style="font-size:13px; font-weight:700; color:#475569; margin-bottom:4px; text-align:left;">現貨與質押分佈</div>
            <img src="{pie_url}" width="510" height="250" style="display:block; border-radius:6px; background:#fff;">
        </div>
      </div>
    </body>
    </html>
    """

    with open('dashboard.html', 'w', encoding='utf-8') as f: 
        f.write(html_content)

    hti = Html2Image(custom_flags=['--no-sandbox', '--disable-gpu', '--hide-scrollbars'])
    
    try:
        # 將畫布精準設定為 1750px，與 HTML 的 .main-wrapper 完美契合，杜絕留白與裁切。
        hti.screenshot(html_file='dashboard.html', save_as='dashboard.png', size=(540, 1750))
    except Exception as e:
        print("screenshot 失敗:", e)
        pass

    keyboard = {
        "inline_keyboard": [
            [{"text": "📝 Growth 填寫表單", "url": "https://forms.gle/9ZEJawwNRGfiXQiV8"}],
            [{"text": "📈 Skynet 儀表板", "url": "https://5972x4.csb.app/"}]
        ]
    }
    
    msg_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    with open("dashboard.png", "rb") as f:
        requests.post(msg_url, data={
            "chat_id": TELEGRAM_CHAT_ID, 
            "caption": "✅ **日報結算完畢！**\n為您送上最新的 Growth 儀表板。",
            "parse_mode": "Markdown",
            "reply_markup": json.dumps(keyboard) 
        }, files={"photo": f})

if __name__ == "__main__":
    main()
