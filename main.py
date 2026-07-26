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


HISTORY_EXTRA_COLUMNS = ["TW_Stock_Value", "US_Stock_Value", "Cash_Value", "Fund_Value", "NVDA_QQQM_Weight", "NVDA_SPYG_Weight", "NVDA_VOO_Weight"]
ETF_NVDA_WEIGHT_FALLBACKS = {"QQQM": 0.095, "SPYG": 0.075, "VOO": 0.070}


def ensure_history_columns(history_sheet):
    """Append fields used by portfolio analytics without changing existing history."""
    if history_sheet is None:
        return
    header = history_sheet.row_values(1)
    for column in HISTORY_EXTRA_COLUMNS:
        if column not in header:
            history_sheet.update_cell(1, len(header) + 1, column)
            header.append(column)


def upsert_history_snapshot(history_sheet, snapshot_date, values):
    """Keep one end-of-day snapshot per Taiwan calendar date."""
    if history_sheet is None:
        return "skipped"

    snapshot = [snapshot_date, *values]
    rows = history_sheet.get_all_values()
    for row_number in range(len(rows), 1, -1):
        row = rows[row_number - 1]
        if row and str(row[0]).strip()[:10] == snapshot_date:
            history_sheet.update(f"A{row_number}:{chr(64 + len(snapshot))}{row_number}", [snapshot])
            return "updated"

    history_sheet.append_row(snapshot)
    return "created"


def get_etf_nvda_weight(symbol, history_records):
    """Read current ETF holding weight, then historical value, then conservative fallback."""
    try:
        holdings = yf.Ticker(symbol).funds_data.top_holdings
        for index, row in holdings.iterrows():
            text = f"{index} {' '.join(str(value) for value in row.values)}".upper()
            if "NVDA" in text or "NVIDIA" in text:
                value = next((float(item) for item in row.values if isinstance(item, (float, int)) and 0 < float(item) < 1), None)
                if value is not None:
                    return value, "official"
    except Exception:
        pass

    field = f"NVDA_{symbol}_Weight"
    for row in reversed(history_records):
        try:
            value = float(str(row.get(field, "")).replace("%", ""))
            if value > 0:
                return value, "history"
        except (TypeError, ValueError):
            pass
    return ETF_NVDA_WEIGHT_FALLBACKS[symbol], "fallback"


def write_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)

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
    etf_nvda_weights = {symbol: get_etf_nvda_weight(symbol, history_records) for symbol in ETF_NVDA_WEIGHT_FALLBACKS}
        
    usd_rate = get_usd_twd_rate()
    tw_stock_value, us_stock_value_usd, tsmc_exposure_twd, price_006208, leveraged_etf_value = 0, 0, 0, 0, 0
    position_values_twd, tw_position_values, us_position_values = {}, {}, {}
    cash_twd, cash_usd = inventory["現金_TWD"].get("TWD", 0), inventory["現金_USD"].get("USD", 0)
    fund_value = sum(v for k, v in inventory["基金"].items() if k != "History")

    for symbol, shares in inventory["台股"].items():
        if symbol == "History" or shares <= 0: continue
        price = get_tw_stock_price(symbol)
        value = price * shares
        tw_stock_value += value 
        position_values_twd[symbol] = value
        tw_position_values[symbol] = value
        if symbol == '2330': tsmc_exposure_twd += (value * 1.0)
        elif symbol == '006208': tsmc_exposure_twd += (value * 0.594); price_006208 = price
        elif symbol == '00685L': tsmc_exposure_twd += (value * 0.728); leveraged_etf_value = value

    if price_006208 <= 0 and inventory["擔保品"].get("006208", 0) > 0:
        price_006208 = get_tw_stock_price("006208")

    pledged_value, pledged_006208_value = 0, 0
    for symbol, shares in inventory["擔保品"].items():
        if symbol == "History" or shares <= 0:
            continue
        price = price_006208 if symbol == "006208" and price_006208 > 0 else get_tw_stock_price(symbol)
        value = price * shares
        pledged_value += value
        if symbol == "006208":
            pledged_006208_value += value

    for symbol, shares in inventory["美股"].items():
        if symbol == "History" or shares <= 0: continue
        value = get_us_stock_price(symbol) * shares
        us_stock_value_usd += value
        position_values_twd[symbol] = value * usd_rate
        us_position_values[symbol] = value * usd_rate
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
    nvda_exposure_twd = position_values_twd.get("NVDA", 0) + sum(position_values_twd.get(symbol, 0) * weight for symbol, (weight, _) in etf_nvda_weights.items())
    nvda_pct = (nvda_exposure_twd / total_asset * 100) if total_asset > 0 else 0
    largest_symbol, largest_position_value = max(position_values_twd.items(), key=lambda item: item[1], default=("—", 0))
    largest_position_pct = (largest_position_value / total_asset * 100) if total_asset > 0 else 0
    largest_position_status = "警示" if largest_position_pct >= 35 else "觀察" if largest_position_pct >= 20 else "正常"
    asset_006208_value = position_values_twd.get("006208", 0)
    qqqm_value = position_values_twd.get("QQQM", 0)
    tw_largest_symbol, tw_largest_value = max(tw_position_values.items(), key=lambda item: item[1], default=("—", 0))
    us_largest_symbol, us_largest_value = max(us_position_values.items(), key=lambda item: item[1], default=("—", 0))
    tw_largest_pct = (tw_largest_value / total_asset * 100) if total_asset else 0
    us_largest_pct = (us_largest_value / total_asset * 100) if total_asset else 0
    # 資產板塊採用可動用台股／質押借款／現貨美股的風險視角。
    # 質押台股此處代表借款金額，而非擔保品的市值。
    spot_tw_value = max(0, tw_stock_value - total_debt)
    pledged_loan_value = total_debt

    def stressed_maintenance_ratio(decline):
        stressed_collateral = max(0, pledged_value - (pledged_006208_value * decline))
        return (stressed_collateral / total_debt * 100) if total_debt > 0 else 0

    stress_scenarios = [
        {"label": "006208 下跌 10%", "netImpact": asset_006208_value * -0.10, "netAsset": net_asset - asset_006208_value * 0.10, "maintenance": stressed_maintenance_ratio(0.10)},
        {"label": "006208 下跌 20%", "netImpact": asset_006208_value * -0.20, "netAsset": net_asset - asset_006208_value * 0.20, "maintenance": stressed_maintenance_ratio(0.20)},
    ]

    yesterday_net = next((float(str(row.get('Net_Asset', 0)).replace(',', '')) for row in reversed(history_records) if float(str(row.get('Net_Asset', 0)).replace(',', '')) > 0 and str(row.get('Date', ''))[-5:] != today_str), 0)
    daily_diff = net_asset - yesterday_net if yesterday_net else 0
    daily_pct = (daily_diff / yesterday_net * 100) if yesterday_net else 0
    sign, emoji = ("+", "📈") if daily_diff >= 0 else ("", "📉")

    progress_pct = (net_asset / 10000000) * 100 if net_asset > 0 else 0
    bar_blocks = max(0, min(10, int(progress_pct / 10)))
    bar_str = "[" + "█" * bar_blocks + "░" * (10 - bar_blocks) + f"] {progress_pct:.1f}%"
    stress_cards_html = "".join(
        f'''<div class="stress-card">
                <div class="stress-label">{scenario["label"]}</div>
                <div class="stress-impact">${scenario["netImpact"]:,.0f}</div>
                <div class="stress-detail">壓力後淨資產 ${scenario["netAsset"]:,.0f}</div>
                <div class="stress-detail">{f"維持率 {scenario['maintenance']:.1f}%" if scenario["maintenance"] is not None else "不影響質押維持率"}</div>
            </div>'''
        for scenario in stress_scenarios
    )

    category_values = {"TW_Stock_Value": tw_stock_value, "US_Stock_Value": us_stock_value_twd, "Cash_Value": total_cash_twd, "Fund_Value": fund_value}
    previous_categories = next((row for row in reversed(history_records) if any(str(row.get(key, "")).strip() for key in category_values)), None)
    category_daily_changes = {}
    for key, value in category_values.items():
        try:
            previous = float(str(previous_categories.get(key, "")).replace(",", "")) if previous_categories else None
        except (TypeError, ValueError):
            previous = None
        category_daily_changes[key] = None if previous is None else {"amount": round(value - previous, 2), "percent": round((value - previous) / previous * 100, 2) if previous else 0}

    def inline_daily_change(key):
        change = category_daily_changes[key]
        if change is None:
            return '<span class="daily-inline price-flat">日變動待累積</span>'
        color = "price-up" if change["amount"] > 0 else "price-down" if change["amount"] < 0 else "price-flat"
        return f'<span class="daily-inline {color}">{change["percent"]:+.2f}% · ${change["amount"]:+,.0f}</span>'

    snapshot_result = "skipped"
    if total_asset > 0:
        ensure_history_columns(history_sheet)
        snapshot_result = upsert_history_snapshot(
            history_sheet,
            tw_now.strftime("%Y-%m-%d"),
            [round(total_asset, 2), round(net_asset, 2), round(total_debt, 2), round(tsmc_exposure_twd, 2), round(tw_stock_value, 2), round(us_stock_value_twd, 2), round(total_cash_twd, 2), round(fund_value, 2), *[round(etf_nvda_weights[symbol][0], 6) for symbol in ETF_NVDA_WEIGHT_FALLBACKS]],
        )

    daily_net_history, daily_total_history = {}, {}
    for row in history_records:
        date_str = str(row.get('Date', ''))[:10]
        net_val, total_val = float(str(row.get('Net_Asset', 0)).replace(',', '')), float(str(row.get('Total_Asset', 0)).replace(',', ''))
        if net_val > 0 and len(date_str) == 10: daily_net_history[date_str], daily_total_history[date_str] = net_val, total_val
            
    daily_net_history[tw_now.strftime("%Y-%m-%d")], daily_total_history[tw_now.strftime("%Y-%m-%d")] = net_asset, total_asset
    sorted_dates = sorted(daily_net_history.keys())
    all_totals, all_nets = [daily_total_history[d] for d in sorted_dates], [daily_net_history[d] for d in sorted_dates]

    def moving_average(values, window):
        return [
            round(sum(values[index - window + 1:index + 1]) / window, 2) if index >= window - 1 else None
            for index in range(len(values))
        ]

    # Keep every daily snapshot for interactive time ranges on the web dashboard.
    chart_dates = [date[5:] for date in sorted_dates]
    chart_totals, chart_nets = all_totals, all_nets
    total_20ma, total_60ma = moving_average(all_totals, 20), moving_average(all_totals, 60)
    total_240ma = moving_average(all_totals, 240)
    net_20ma, net_60ma = moving_average(all_nets, 20), moving_average(all_nets, 60)
    net_240ma = moving_average(all_nets, 240)

    chart_dates_json = json.dumps(chart_dates)
    chart_totals_json = json.dumps(chart_totals)
    chart_nets_json = json.dumps(chart_nets)
    total_20ma_json = json.dumps(total_20ma)
    total_60ma_json = json.dumps(total_60ma)
    total_240ma_json = json.dumps(total_240ma)
    net_20ma_json, net_60ma_json = json.dumps(net_20ma), json.dumps(net_60ma)
    net_240ma_json = json.dumps(net_240ma)

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
        <script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-zoom@2.2.0/dist/chartjs-plugin-zoom.min.js"></script>
        <style>
            @import url('https://fonts.googleapis.com/css2?family=Noto+Sans+TC:wght@400;500;700&family=Noto+Serif+TC:wght@600;700&display=swap');
            :root {{ --paper: #f1f0eb; --surface: #fbfaf7; --ink: #23354a; --muted: #77736b; --line: #d7d4cc; --sage: #687c70; --brick: #c4674f; --orange:#c98a4b; --navy:#24425e; }}
            * {{ box-sizing: border-box; }} html {{ scroll-behavior:smooth; }}
            body {{ font-family: 'Noto Sans TC', sans-serif; background-color: var(--paper); margin: 0 auto; max-width: 1080px; padding: 32px 20px 48px; color: var(--ink); letter-spacing: .01em; }}
            .header-wrapper {{ display:flex; align-items:center; justify-content:space-between; gap:20px; padding:0 0 20px; border-bottom:1px solid var(--line); margin-bottom:18px; }}
            .header-container {{ display:flex; align-items:center; gap:14px; }}
            .header-item {{ display:flex; align-items:center; }}
            .brand-divider {{ width:1px; height:28px; background:var(--line); }}
            .brand-name {{ font-family:'Noto Serif TC', serif; font-size:20px; font-weight:700; letter-spacing:.08em; }}
            .sync {{ color:var(--navy); font-size:12px; font-weight:700; white-space:nowrap; background:var(--surface); border:1px solid var(--line); border-radius:999px; padding:8px 12px; box-shadow:0 2px 5px rgba(50,54,53,.04); margin-left:auto; }}
            .eyebrow {{ color:var(--muted); font-size:11px; letter-spacing:.14em; text-transform:uppercase; margin:0 0 8px; }}
            .hero {{ position:relative; overflow:hidden; background:var(--navy); border:1px solid #1d3850; border-radius:22px; padding:28px; margin-bottom:14px; color:#f8f6ef; box-shadow:0 10px 24px rgba(36,66,94,.13); }}
            .hero::after {{ content:''; position:absolute; width:210px; height:210px; border:1px solid rgba(255,255,255,.36); border-radius:50%; right:-70px; top:-112px; box-shadow:0 0 0 34px rgba(255,255,255,.055); pointer-events:none; }}
            .hero .eyebrow,.hero .metric-label {{ color:#ccd7dc; }} .hero .metric-value {{ color:#fffdf7; }}
            .hero-top {{ position:relative; z-index:1; display:flex; align-items:end; justify-content:space-between; gap:16px; border-bottom:1px solid rgba(255,255,255,.22); padding-bottom:20px; margin-bottom:18px; }}
            .hero-value {{ font-family:'Noto Serif TC', serif; font-size:clamp(34px, 6vw, 54px); line-height:1; letter-spacing:-.03em; }}
            .change {{ color:{'#91b29d' if daily_diff < 0 else '#ef9a83'}; font-size:14px; font-weight:700; background:#35536d; border:1px solid rgba(255,255,255,.16); border-radius:12px; padding:10px 12px; box-shadow:0 4px 10px rgba(10,24,40,.12); }}
            .metric-grid {{ position:relative; z-index:1; display:grid; grid-template-columns:repeat(3, 1fr); gap:12px; }}
            .metric {{ border-left:2px solid rgba(255,255,255,.33); padding-left:12px; }}
            .metric-label {{ color:var(--muted); font-size:12px; }}
            .metric-value {{ display:block; color:var(--ink); font-size:18px; font-weight:700; margin-top:4px; }}
            .section-nav {{ display:grid; grid-template-columns:repeat(3, 1fr); gap:10px; margin:0 0 28px; }} .section-nav a {{ background:var(--surface); border:1px solid var(--line); border-radius:14px; color:var(--navy); font-weight:700; text-align:center; text-decoration:none; padding:12px; transition:transform .2s, background .2s; }} .section-nav a:hover {{ background:#e9eee9; transform:translateY(-1px); }}
            .dashboard-section {{ scroll-margin-top:18px; margin-bottom:30px; }} .dashboard-heading {{ display:flex; align-items:baseline; gap:10px; margin:0 0 12px; }} .dashboard-heading h2 {{ font-family:'Noto Serif TC', serif; font-size:22px; margin:0; color:var(--navy); }} .dashboard-heading p {{ margin:0; color:var(--orange); font-size:11px; letter-spacing:.12em; text-transform:uppercase; }}
            .card {{ background:var(--surface); padding:21px; border:1px solid var(--line); border-top:3px solid transparent; margin-bottom:14px; box-shadow:0 4px 12px rgba(50,54,53,.035); }}
            .card:nth-of-type(2n) {{ border-top-color:var(--sage); }} .card:nth-of-type(3n) {{ border-top-color:var(--orange); }}
            .sec-title {{ display:flex; align-items:center; justify-content:space-between; font-family:'Noto Serif TC', serif; font-size:17px; font-weight:700; margin-bottom:16px; color:var(--ink); }}
            .sec-note {{ color:var(--muted); font-family:'Noto Sans TC', sans-serif; font-size:11px; font-weight:400; }}
            .info-row {{ font-size:14px; font-weight:500; margin-bottom:9px; color:#514f49; }}
            .grid-2 {{ display:grid; grid-template-columns:1fr 1fr; gap:10px; }}
            .box {{ background:#f4f2ed; padding:14px; border:1px solid #e5e2db; border-radius:3px; font-size:12px; color:var(--muted); }}
            .box b {{ display:block; font-size:19px; color:var(--ink); font-weight:700; margin-top:6px; margin-bottom:2px; }}
            .box small {{ font-size:11px; color:var(--muted); }}
            .risk-good {{ color:#62705e !important; }} .risk-alert {{ color:#ad6658 !important; }}
            .timeline ul {{ padding-left:18px; margin:10px 0 0; font-size:13px; color:#514f49; line-height:1.9; }}
            .goal-track {{ height:6px; background:#e5e2db; margin:12px 0 8px; }} .goal-fill {{ height:100%; background:var(--sage); width:{min(progress_pct, 100):.1f}%; }}
            .actions {{ display:grid; grid-template-columns:1fr 1fr; gap:10px; margin:16px 0 30px; }}
            .btn {{ display:block; text-align:center; background:#45443f; color:white; text-decoration:none; padding:14px; font-size:13px; font-weight:500; letter-spacing:.05em; border:1px solid #45443f; transition:background .2s; }}
            .btn:hover {{ background:#30302c; }} .btn-alt {{ background:transparent; color:var(--ink); border-color:var(--line); }} .btn-alt:hover {{ background:#ece9e1; }}
            .chart-container {{ position:relative; width:100%; height:280px; margin-bottom:20px; }}
            .chart-title {{ font-family:'Noto Serif TC', serif; font-weight:700; font-size:16px; margin-bottom:5px; color:var(--ink); }}
            .chart-caption {{ color:var(--muted); font-size:12px; margin-bottom:14px; }}
            .chart-controls {{ display:flex; align-items:center; flex-wrap:wrap; gap:7px; margin:0 0 14px; }}
            .range-btn {{ appearance:none; background:transparent; border:1px solid var(--line); color:var(--muted); cursor:pointer; font:inherit; font-size:11px; padding:6px 9px; }}
            .range-btn:hover, .range-btn.is-active {{ background:#e9e6de; color:var(--ink); border-color:#c9c4b9; }}
            .chart-hint {{ color:var(--muted); font-size:11px; margin-left:auto; }}
            .exposure-status {{ font-size:11px; padding:4px 7px; border:1px solid var(--line); color:var(--muted); }}
            .stress-grid {{ display:grid; grid-template-columns:repeat(2, 1fr); gap:10px; }}
            .stress-card {{ background:#f4f2ed; border:1px solid #e5e2db; padding:14px; }}
            .stress-label {{ color:var(--ink); font-size:13px; font-weight:700; }}
            .stress-impact {{ color:var(--brick); font-family:'Noto Serif TC', serif; font-size:20px; font-weight:700; margin:7px 0 4px; }}
            .stress-detail {{ color:var(--muted); font-size:11px; line-height:1.7; }}
            .footer {{ border-top:1px solid var(--line); padding-top:16px; color:var(--muted); font-size:11px; text-align:center; letter-spacing:.04em; }}
            .price-up {{ color:#b84f45 !important; }} .price-down {{ color:#5e806d !important; }} .price-flat {{ color:var(--navy) !important; }}
            .daily-inline {{ display:inline-block; margin-left:6px; font-size:11px; font-weight:700; vertical-align:middle; white-space:nowrap; }}
            .block-grid {{ display:grid; grid-template-columns:repeat(3, 1fr); gap:10px; }}
            .risk-section {{ background:#f4f2ed; border:1px solid #e5e2db; border-radius:14px; padding:16px; }} .risk-section + .risk-section {{ margin-top:12px; }}
            .risk-pair,.exposure-pair {{ display:grid; grid-template-columns:1fr 1fr; gap:10px; }}
            .risk-column,.exposure-row {{ background:#f8faf7; border:1px solid #d8dfd8; border-radius:10px; padding:15px; }} .risk-column strong,.exposure-row strong {{ display:block; color:var(--navy); font-size:23px; line-height:1.2; margin-top:6px; }} .risk-column small,.exposure-row small {{ display:block; margin-top:6px; color:var(--muted); font-size:12px; line-height:1.5; }}
            .maintenance-line {{ border-top:1px solid #d5ddd5; margin-top:12px; padding-top:10px; }} .maintenance-line strong {{ display:inline; font-size:17px; margin:0; }}
            @media (max-width:540px) {{ body {{ padding:22px 14px 34px; }} .header-wrapper {{ align-items:flex-start; flex-direction:column; gap:10px; }} .sync {{ align-self:flex-end; }} .hero, .card {{ padding:17px; }} .hero-top {{ align-items:flex-start; flex-direction:column; gap:10px; }} .metric-grid {{ gap:8px; }} .metric-value {{ font-size:15px; }} .grid-2, .stress-grid, .risk-pair, .exposure-pair {{ gap:8px; }} .block-grid {{ grid-template-columns:1fr 1fr; gap:8px; }} .box {{ padding:11px; }} .actions {{ grid-template-columns:1fr; }} .chart-hint {{ width:100%; margin-left:0; }} }}
        </style>
    </head>
    <body>
        <div class="header-wrapper">
            <div class="header-container">
                <div class="header-item">
                    <img src="./PRStK-Remove.png" alt="PRStK" style="height:24px; object-fit:contain; max-width:100%;">
                </div>
                <div class="brand-divider"></div>
                <div class="header-item">
                    <img src="./SFC.e-removebg-preview.png" alt="SFC.e" style="height:26px; object-fit:contain; max-width:100%;">
                </div>
                <div class="brand-divider"></div><div class="brand-name">Growth</div>
            </div>
            <div class="sync">資料同步 · {tw_now.strftime('%m/%d %H:%M')}</div>
        </div>

        <section class="hero">
            <p class="eyebrow">Portfolio overview</p>
            <div class="hero-top">
                <div><div class="metric-label">淨資產 Net Asset</div><div class="hero-value">${net_asset:,.0f}</div></div>
                <div class="change">今日 {sign}{daily_pct:.1f}% &nbsp;·&nbsp; {sign}${daily_diff:,.0f}</div>
            </div>
            <div class="metric-grid">
                <div class="metric"><span class="metric-label">總資產</span><span class="metric-value">${total_asset:,.0f}</span></div>
                <div class="metric"><span class="metric-label">總負債</span><span class="metric-value">${total_debt:,.0f}</span></div>
                <div class="metric"><span class="metric-label">負債比</span><span class="metric-value">{debt_ratio:.1f}%</span></div>
            </div>
        </section>

        <nav class="section-nav" aria-label="Growth 儀表板導覽">
            <a href="#allocation">配置</a>
            <a href="#risk">風險</a>
            <a href="#growth">成長</a>
        </nav>

        <section class="dashboard-section" id="allocation">
            <div class="dashboard-heading"><h2>配置</h2><p>Allocation</p></div>
        <div class="card">
            <div class="sec-title">資產配置 <span class="sec-note">Market value · TWD</span></div>
            <div class="grid-2">
                <div class="box">台股<b>${tw_stock_value:,.0f}{inline_daily_change('TW_Stock_Value')}</b></div>
                <div class="box">美股<b>${us_stock_value_twd:,.0f}{inline_daily_change('US_Stock_Value')}</b><small>約 ${us_stock_value_usd:,.0f} USD</small></div>
                <div class="box">現金<b>${total_cash_twd:,.0f}{inline_daily_change('Cash_Value')}</b><small>TWD 與 USD 合計</small></div>
                <div class="box">基金<b>${fund_value:,.0f}{inline_daily_change('Fund_Value')}</b></div>
            </div>
        </div>

        <div class="card">
            <div class="chart-title">總資產板塊</div>
            <div class="chart-caption">現貨台股以台股市值扣除質押借款計算；質押台股代表借款金額。</div>
            <div class="chart-container" style="height: 250px; margin-bottom:0;">
                <canvas id="pieChart"></canvas>
            </div>
        </div>
        </section>

        <section class="dashboard-section" id="risk">
            <div class="dashboard-heading"><h2>風險</h2><p>Risk management</p></div>
        <div class="card">
            <div class="sec-title">風險摘要 <span class="sec-note">Current safeguards</span></div>
            <div class="risk-section">
                <div class="sec-title" style="margin-bottom:10px;">槓桿 <span class="sec-note">Leverage &amp; collateral</span></div>
                <div class="risk-pair">
                    <div class="risk-column"><span class="metric-label">有效槓桿</span><strong>{effective_leverage:.2f} ×</strong><small>凱利安全邊界 {half_kelly_limit:.2f} ×</small></div>
                    <div class="risk-column"><span class="metric-label">質押借款</span><strong class="risk-alert">${total_debt:,.0f}</strong><small>含利息 ${accumulated_interest:,.0f}</small><div class="maintenance-line"><span class="metric-label">質押維持率</span> <strong class="{'risk-alert' if maintenance_ratio<150 else 'risk-good'}">{maintenance_ratio:.1f}%</strong> <small style="display:inline;">| {ratio_status}</small></div></div>
                </div>
            </div>
            <div class="risk-section">
                <div class="sec-title" style="margin-bottom:10px;">曝險 <span class="sec-note">Look-through concentration</span></div>
                <div class="exposure-pair">
                    <div class="exposure-row"><span class="metric-label">TSMC 曝險</span><strong>{tsmc_pct:.1f}%</strong><small>台美股與 ETF 統合曝險</small></div>
                    <div class="exposure-row"><span class="metric-label">NVDA 曝險</span><strong>{nvda_pct:.1f}%</strong><small>美股與 ETF 統合曝險</small></div>
                </div>
            </div>
        </div>

        <div class="card">
            <div class="sec-title">集中度與壓力測試 <span class="exposure-status">20% 觀察 · 35% 警示</span></div>
            <div class="grid-2" style="margin-bottom:12px;">
                <div class="box">台股最大單一標的<b>{tw_largest_symbol} · {tw_largest_pct:.1f}%</b><small>${tw_largest_value:,.0f} ／ 總資產</small></div>
                <div class="box">美股最大單一標的<b>{us_largest_symbol} · {us_largest_pct:.1f}%</b><small>${us_largest_value:,.0f} ／ 總資產</small></div>
            </div>
            <div class="stress-grid">{stress_cards_html}</div>
        </div>

        </section>

        <section class="dashboard-section" id="growth">
            <div class="dashboard-heading"><h2>成長</h2><p>Progress &amp; trajectory</p></div>
        <div class="card">
            <div class="sec-title">成長軌跡 <span class="sec-note">Net asset return</span></div>
            <div class="grid-2">
                <div class="box">近一月<b>{get_growth_str(30)}</b></div>
                <div class="box">近一季<b>{get_growth_str(90)}</b></div>
                <div class="box">近一年<b>{get_growth_str(365)}</b></div>
                <div class="box">近三年<b>{get_growth_str(1095)}</b></div>
            </div>
        </div>

        <div class="card">
            <div class="sec-title">目標進度 <span class="sec-note">10,000,000 TWD</span></div>
            <div class="info-row">千萬目標達成率 {progress_pct:.1f}%</div>
            <div class="goal-track"><div class="goal-fill"></div></div>
            <div class="timeline">
                <ul>
                    <li>2026-10：成功嶺退伍日</li>
                    <li>2027-11: 850萬 達標</li>
                    <li>2028-10: 1000萬 達標</li>
                    <li>2035-05: 100萬鎂 達標</li>
                </ul>
            </div>
        </div>

        <div class="card">
            <div class="chart-title">近期資產軌跡</div>
            <div class="chart-caption">總資產與淨資產 · 可回看完整每日快照</div>
            <div class="chart-controls" aria-label="資產軌跡時間範圍">
                <button class="range-btn is-active" type="button" data-range="30">1M</button>
                <button class="range-btn" type="button" data-range="90">3M</button>
                <button class="range-btn" type="button" data-range="365">1Y</button>
                <button class="range-btn" type="button" data-range="all">全部</button>
                <button class="range-btn" type="button" id="resetZoom">重設縮放</button>
                <span class="chart-hint">滾輪／雙指縮放 · 拖曳回看</span>
            </div>
            <div class="chart-container" style="height: 250px;">
                <canvas id="lineChart"></canvas>
            </div>
            
        </div>

        </section>

        <div class="actions">
            <a href="https://hanjhou2000716.github.io/skynet-monitoring/" class="btn btn-alt">開啟 Risk Monitor</a>
            <a href="https://forms.gle/9ZEJawwNRGfiXQiV8" class="btn">登錄資產異動</a>
        </div>
        <footer class="footer">© 2026 PRStK Lab &amp; SFC.e. | All rights reserved.</footer>

        <script>
            // 確保網頁讀取完畢後才開始畫圖，並加入 try-catch 防止崩潰
            document.addEventListener("DOMContentLoaded", function() {{
                try {{
                    Chart.register(ChartDataLabels);
                }} catch (error) {{
                    console.warn("ChartDataLabels 未能載入:", error);
                }}

                try {{
                    // Full history is kept in the browser; ranges only change what is visible.
                    const lineCtx = document.getElementById('lineChart').getContext('2d');
                    const fullLabels = {chart_dates_json};
                    const fullTotals = {chart_totals_json};
                    const fullNets = {chart_nets_json};
                    const movingAverages = {{
                        20: {total_20ma_json}, 60: {total_60ma_json},
                        240: {total_240ma_json}
                    }};
                    const netMovingAverages = {{20: {net_20ma_json}, 60: {net_60ma_json}, 240: {net_240ma_json}}};
                    const lineChart = new Chart(lineCtx, {{
                        type: 'line',
                        data: {{
                            labels: fullLabels,
                            datasets: [
                                {{ label: '總資產', data: fullTotals, borderColor: '#727a6d', backgroundColor: '#727a6d', borderWidth: 2, pointRadius: 0, yAxisID: 'y' }},
                                {{ label: '淨資產', data: fullNets, borderColor: '#ad6658', backgroundColor: '#ad6658', borderWidth: 2, pointRadius: 0, yAxisID: 'y' }},
                                {{ label: '總資產月線', data: movingAverages[20], borderColor: '#9a9387', borderDash: [5, 5], borderWidth: 1.5, pointRadius: 0, yAxisID: 'y' }},
                                {{ label: '淨資產月線', data: netMovingAverages[20], borderColor: '#d28a76', borderDash: [2, 4], borderWidth: 1.5, pointRadius: 0, yAxisID: 'y' }},
                                {{ label: '總資產季線', data: movingAverages[60], borderColor: '#b58a72', borderDash: [5, 5], borderWidth: 1.5, pointRadius: 0, yAxisID: 'y', hidden: true }},
                                {{ label: '淨資產季線', data: netMovingAverages[60], borderColor: '#c98a4b', borderDash: [2, 4], borderWidth: 1.5, pointRadius: 0, yAxisID: 'y', hidden: true }},
                                {{ label: '總資產年線', data: movingAverages[240], borderColor: '#77736b', borderDash: [5, 5], borderWidth: 1.5, pointRadius: 0, yAxisID: 'y', hidden: true }},
                                {{ label: '淨資產年線', data: netMovingAverages[240], borderColor: '#687c70', borderDash: [2, 4], borderWidth: 1.5, pointRadius: 0, yAxisID: 'y', hidden: true }}
                            ]
                        }},
                        options: {{
                            responsive: true, maintainAspectRatio: false,
                            interaction: {{ mode: 'index', intersect: false }},
                            plugins: {{
                                legend: {{ position: 'top', labels: {{ boxWidth: 12, font: {{size: 10}} }} }},
                                datalabels: {{ display: false }},
                                zoom: {{
                                    pan: {{ enabled: true, mode: 'x' }},
                                    zoom: {{ wheel: {{ enabled: true }}, pinch: {{ enabled: true }}, mode: 'x' }},
                                    onPanComplete: ({{chart}}) => {{
                                        const visibleDays = Math.max(1, Math.round(chart.scales.x.max - chart.scales.x.min + 1));
                                        applyTrendLines(visibleDays);
                                        chart.update('none');
                                    }},
                                    onZoomComplete: ({{chart}}) => {{
                                        const visibleDays = Math.max(1, Math.round(chart.scales.x.max - chart.scales.x.min + 1));
                                        applyTrendLines(visibleDays);
                                        chart.update('none');
                                    }}
                                }}
                            }},
                            scales: {{
                                x: {{ ticks: {{ maxTicksLimit: 8 }} }},
                                y: {{ type: 'linear', display: true, position: 'left', ticks: {{ callback: function(val) {{ return val>=1000000 ? (val/1000000).toFixed(1)+'M' : val; }} }} }}
                            }}
                        }}
                    }});
                    const applyTrendLines = (days) => {{
                        const showMonth = days <= 90;
                        const showSeason = days > 90 && days <= 365;
                        const showYear = days > 365 && fullLabels.length >= 240;
                        lineChart.data.datasets[2].hidden = !showMonth;
                        lineChart.data.datasets[3].hidden = !showMonth;
                        lineChart.data.datasets[4].hidden = !showSeason;
                        lineChart.data.datasets[5].hidden = !showSeason;
                        lineChart.data.datasets[6].hidden = !showYear;
                        lineChart.data.datasets[7].hidden = !showYear;
                    }};
                    const applyRange = (range) => {{
                        const days = range === 'all' ? fullLabels.length : Math.min(Number(range), fullLabels.length);
                        const start = Math.max(0, fullLabels.length - days);
                        lineChart.options.scales.x.min = start;
                        lineChart.options.scales.x.max = fullLabels.length - 1;
                        applyTrendLines(days);
                        lineChart.update();
                        document.querySelectorAll('.range-btn[data-range]').forEach((button) => button.classList.toggle('is-active', button.dataset.range === String(range)));
                    }};
                    document.querySelectorAll('.range-btn[data-range]').forEach((button) => button.addEventListener('click', () => applyRange(button.dataset.range)));
                    document.getElementById('resetZoom').addEventListener('click', () => applyRange('30'));
                    applyRange('30');
                }} catch (error) {{
                    console.error("折線圖繪製失敗:", error);
                }}

                try {{
                    // 繪製圓餅圖
                    const pieCtx = document.getElementById('pieChart').getContext('2d');
                    new Chart(pieCtx, {{
                        type: 'pie',
                        data: {{
                            labels: ['現貨台股', '質押台股', '現貨美股'],
                            datasets: [{{
                                data: [{spot_tw_value:.2f}, {pledged_loan_value:.2f}, {us_stock_value_twd:.2f}],
                                backgroundColor: ['#24425e', '#687c70', '#c98a4b'],
                                borderWidth: 1, borderColor: '#fbfaf7'
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

    allocation_items = [
        {"label": "台股", "value": round(tw_stock_value, 2), "color": "#727a6d"},
        {"label": "美股", "value": round(us_stock_value_twd, 2), "color": "#9a9387"},
        {"label": "現金", "value": round(total_cash_twd, 2), "color": "#c8c1b5"},
        {"label": "基金", "value": round(fund_value, 2), "color": "#b58a72"},
    ]
    for item in allocation_items:
        item["percent"] = round((item["value"] / total_asset * 100), 1) if total_asset > 0 else 0

    risk_level = "attention" if ((maintenance_ratio and maintenance_ratio < 150) or largest_position_pct >= 35) else "watch" if (debt_ratio >= 25 or tsmc_pct >= 35 or largest_position_pct >= 20) else "stable"
    risk_summary = {
        "level": risk_level,
        "debtRatio": round(debt_ratio, 1),
        "maintenanceRatio": round(maintenance_ratio, 1),
        "tsmcExposureRatio": round(tsmc_pct, 1),
        "effectiveLeverage": round(effective_leverage, 2),
        "largestPosition": {"symbol": largest_symbol, "value": round(largest_position_value, 2), "percent": round(largest_position_pct, 1), "status": largest_position_status},
        "nvdaExposureRatio": round(nvda_pct, 1),
    }

    data_for_web = {
        "taiex": round(taiex_val, 2),
        "ma200": round(ma200_val, 2),
        "vix": round(vix_val, 2),
        "peak_006208": round(peak_006208, 2),
        "asset_006208": round(price_006208, 2) if price_006208 else 249.1,
        "lastUpdated": tw_now.strftime("%Y/%m/%d %H:%M:%S"),
        "portfolio": {
            "totalAsset": round(total_asset, 2),
            "netAsset": round(net_asset, 2),
            "totalDebt": round(total_debt, 2),
            "allocation": allocation_items,
            "risk": risk_summary,
            "stressTests": stress_scenarios,
            "categoryDailyChanges": category_daily_changes,
            "nvdaExposure": {"value": round(nvda_exposure_twd, 2), "percent": round(nvda_pct, 1), "etfWeights": {symbol: {"weight": round(weight * 100, 2), "source": source} for symbol, (weight, source) in etf_nvda_weights.items()}},
        },
    }

    data_for_web["status"] = "ok" if total_asset > 0 else "degraded"
    data_for_web["generatedAt"] = tw_now.isoformat()
    data_for_web["snapshotResult"] = snapshot_result
    write_json("public/data.json", data_for_web)
    write_json("public/status.json", {
        "status": "ok" if total_asset > 0 else "degraded",
        "generatedAt": tw_now.isoformat(),
        "snapshotResult": snapshot_result,
        "portfolioValueAvailable": total_asset > 0,
    })
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
