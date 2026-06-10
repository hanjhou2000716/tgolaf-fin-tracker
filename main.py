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

# ==========================================
# 1. 環境變數與金鑰設定
# ==========================================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
FINMIND_TOKEN = os.getenv("FINMIND_TOKEN")
GCP_CREDENTIALS_JSON = os.getenv("GCP_CREDENTIALS")

# ==========================================
# 2. Google Sheets 動態資產結算核心
# ==========================================
def calculate_current_assets():
    creds_dict = json.loads(GCP_CREDENTIALS_JSON)
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    
    # 綁定最新試算表網址
    sheet = client.open_by_url("https://docs.google.com/spreadsheets/d/1xMlc6zThljsX-HMmxHrFdgDylKq4NNab5HhSRQrqHU8/edit")
    
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
        
    inventory = {
        "台股": {}, "美股": {}, "基金": {}, 
        "現金_TWD": {"TWD": 0.0}, "現金_USD": {"USD": 0.0},
        "質押負債": {"Current_Debt": 0.0},
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
        'FUND', 'TWD', 'USD', 'CURRENT_DEBT'
    ]
    
    for row in data_rows:
        raw_cells = [str(c).strip() for c in row if str(c).strip() != ""]
        if not raw_cells: continue
        
        # 單位自動淨化器
        cells = []
        for c in raw_cells:
            match = re.match(r'^([0-9,.]+)\s*(股|張|萬|元|塊)$', c)
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
            if any(x in cell for x in ["台股", "美股", "基金", "現金", "質押", "負債", "擔保"]):
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
        elif "質押" in asset_type or "負債" in asset_type: asset_type = "質押負債"
        elif "擔保" in asset_type: asset_type = "擔保品"
        
        if asset_type not in inventory: continue
        
        try:
            amount = float(amount_str.replace(",", "").replace("$", ""))
        except ValueError:
            continue
            
        symbol = symbol_overrides.get(symbol, symbol)
        if asset_type in ["現金_TWD", "現金_USD", "質押負債"] and not symbol:
            symbol = "TWD" if asset_type == "現金_TWD" else ("USD" if asset_type == "現金_USD" else "Current_Debt")
            
        if not symbol: continue
        if symbol not in inventory[asset_type]:
            inventory[asset_type][symbol] = 0.0
            
        if "買入" in mode or "存入" in mode or "+" in mode:
            inventory[asset_type][symbol] += amount
        elif "賣出" in mode or "提領" in mode or "-" in mode:
            inventory[asset_type][symbol] -= amount
        elif "取代" in mode or "覆蓋" in mode or "更新" in mode:
            inventory[asset_type][symbol] = amount

    return inventory, history_sheet

# ==========================================
# 3. 金融市場報價與 QuickChart 繪圖模組
# ==========================================
def get_usd_twd_rate():
    try:
        return yf.Ticker("TWD=X").history(period="1d")['Close'].iloc[-1]
    except:
        return 32.3

def get_us_stock_price(symbol):
    try:
        return yf.Ticker(symbol).history(period="1d")['Close'].iloc[-1]
    except:
        return 0

def get_tw_stock_price(symbol):
    url = "https://api.finmindtrade.com/api/v4/data"
    start_date = (datetime.date.today() - datetime.timedelta(days=7)).strftime("%Y-%m-%d")
    parameter = {"dataset": "TaiwanStockPrice", "data_id": str(symbol), "start_date": start_date, "token": FINMIND_TOKEN}
    try:
        data = requests.get(url, params=parameter).json()
        return data["data"][-1]["close"] if data["msg"] == "success" else 0
    except:
        return 0

def generate_pie_chart(tw_free_val, pledged_val, us_val):
    chart_config = {
        "type": "outlabeledPie",
        "data": {
            "labels": ["🇹🇼 現貨台股", "🦆 擔保品市值", "🇺🇸 現貨美股"],
            "datasets": [{"backgroundColor": ["#36a2eb", "#ff6384", "#ffce56"], "data": [tw_free_val, pledged_val, us_val]}]
        },
        "options": {
            "plugins": {
                "legend": {"display": False},
                "outlabels": {"text": "%l %p", "color": "white", "stretch": 35, "font": {"minSize": 12}}
            }
        }
    }
    if tw_free_val == 0 and pledged_val == 0 and us_val == 0:
        chart_config["data"]["labels"] = ["尚無資產數據"]
        chart_config["data"]["datasets"][0]["data"] = [1]
        chart_config["data"]["datasets"][0]["backgroundColor"] = ["#cccccc"]
    return f"https://quickchart.io/chart?c={urllib.parse.quote(json.dumps(chart_config))}&w=400&h=250"

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
    
    recent_days = list(daily_data.keys())[-14:]
    dates, total_data, net_data = [], [], []
    
    for d in recent_days:
        dates.append(d)
        avg_total = sum(daily_data[d]['total']) / len(daily_data[d]['total'])
        avg_net = sum(daily_data[d]['net']) / len(daily_data[d]['net'])
        total_data.append(round(avg_total, 2))
        net_data.append(round(avg_net, 2))
        
    all_vals = total_data + net_data
    if all_vals:
        min_val = min(all_vals)
        max_val = max(all_vals)
        y_min = math.floor(min_val / 200000) * 200000
        y_max = math.ceil(max_val / 200000) * 200000
        if y_min == y_max:
            y_min -= 200000
            y_max += 200000
    else:
        y_min, y_max = 0, 1000000
    
    chart_config = {
        "type": "line",
        "data": {
            "labels": dates,
            "datasets": [
                {"label": "總資產 (Total)", "data": total_data, "borderColor": "#36a2eb", "fill": False, "tension": 0.1},
                {"label": "淨資產 (Net)", "data": net_data, "borderColor": "#ff6384", "fill": False, "tension": 0.1}
            ]
        },
        "options": {
            "title": {"display": True, "text": "近期資產軌跡 (Total vs Net)"},
            "scales": {
                "yAxes": [{
                    "ticks": {
                        "min": y_min,
                        "max": y_max,
                        "stepSize": 200000
                    }
                }]
            }
        }
    }
    return f"https://quickchart.io/chart?c={urllib.parse.quote(json.dumps(chart_config))}&w=400&h=250"

# ==========================================
# 4. 核心結算與通知發送主程序
# ==========================================
def main():
    tw_now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    today_str = tw_now.strftime("%m-%d")
    
    if 12 <= tw_now.hour <= 20:
        title_header = f"🇹🇼 PRStK | Growth（{today_str}）"
    else:
        title_header = f"🇺🇲 PRStK | Growth（{today_str}）"
        
    inventory, history_sheet = calculate_current_assets()
    try: history_records = history_sheet.get_all_records()
    except: history_records = []
        
    usd_rate = get_usd_twd_rate()
    tw_stock_value, us_stock_value_usd, tsmc_exposure_twd, price_006208 = 0, 0, 0, 0
    tw_free_value = 0  # 紀錄未質押的台股部位(供圓餅圖使用)
    cash_twd = inventory["現金_TWD"].get("TWD", 0)
    cash_usd = inventory["現金_USD"].get("USD", 0)
    fund_value = sum(inventory["基金"].values())

    # 1. 結算一般台股 (未質押部位)
    for symbol, shares in inventory["台股"].items():
        if shares <= 0: continue
        price = get_tw_stock_price(symbol)
        value = price * shares
        tw_free_value += value
        tw_stock_value += value  # 疊加進台股總值
        if symbol == '2330': tsmc_exposure_twd += (value * 1.0)
        elif symbol == '006208': 
            tsmc_exposure_twd += (value * 0.594)
            price_006208 = price
        elif symbol == '00685L': tsmc_exposure_twd += (value * 0.728)

    # 2. 結算擔保品 (加回總值與曝險)
    pledged_value = 0
    for symbol, shares in inventory["擔保品"].items():
        if shares <= 0: continue
        if symbol == '006208' and price_006208 > 0:
            price = price_006208
        else:
            price = get_tw_stock_price(symbol)
        
        value = price * shares
        pledged_value += value
        tw_stock_value += value  # 🌟 擔保品市值加回台股總資產
        
        # 🌟 擔保品同樣具有台積電曝險，必須計入
        if symbol == '2330': tsmc_exposure_twd += (value * 1.0)
        elif symbol == '006208': tsmc_exposure_twd += (value * 0.594)
        elif symbol == '00685L': tsmc_exposure_twd += (value * 0.728)

    # 3. 結算美股
    for symbol, shares in inventory["美股"].items():
        if shares <= 0: continue
        price = get_us_stock_price(symbol)
        value = price * shares
        us_stock_value_usd += value
        if symbol == 'TSM': tsmc_exposure_twd += (value * usd_rate * 1.0)

    us_stock_value_twd = us_stock_value_usd * usd_rate
    total_cash_twd = cash_twd + (cash_usd * usd_rate)
    debt = inventory["質押負債"].get("Current_Debt", 0)
    
    # 4. 利息結算 (起算日：2026-06-08, 日息)
    loan_start_date = datetime.date(2026, 6, 8)
    days_passed = max(0, (tw_now.date() - loan_start_date).days)
    daily_rate = 0.033 / 365
    accumulated_interest = debt * daily_rate * days_passed
    total_debt_with_interest = debt + accumulated_interest
    
    # 5. 總資產與淨資產結算 (此時 tw_stock_value 已經完整包含擔保品)
    total_asset = tw_stock_value + us_stock_value_twd + total_cash_twd + fund_value
    net_asset = total_asset - total_debt_with_interest
    
    # 6. 維持率多階狀態判定
    if debt > 0:
        maintenance_ratio = (pledged_value / debt) * 100
        if maintenance_ratio >= 167:
            ratio_status = "安全 ✅"
        elif maintenance_ratio >= 130:
            ratio_status = "警戒 ⚠️ (無法借新還舊)"
        else:
            ratio_status = "危險 🆘 (追繳風險)"
    else:
        maintenance_ratio = 0
        ratio_status = "無借款 ✅"

    tw_pct = (tw_stock_value / total_asset) * 100 if total_asset > 0 else 0
    debt_pct = (debt / total_asset) * 100 if total_asset > 0 else 0
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
    daily_str = f"單日變化：{emoji}{sign}{daily_pct:.1f}% ({sign}${daily_diff:,.0f})" if yesterday_net else "單日變化：-- (首日累積數據中)"

    progress_pct = (net_asset / 10000000) * 100 if net_asset > 0 else 0
    bar_blocks = max(0, min(10, int(progress_pct / 10)))
    bar = "█" * bar_blocks + "░" * (10 - bar_blocks)

    if total_asset > 0:
        history_sheet.append_row([
            tw_now.strftime("%Y-%m-%d"), 
            round(total_asset, 2), round(net_asset, 2), total_debt_with_interest, round(tsmc_exposure_twd, 2)
        ])

    pie_url = generate_pie_chart(tw_free_value, pledged_value, us_stock_value_twd)
    line_url = generate_line_chart(history_records, today_str, total_asset, net_asset)

    valid_history = []
    for row in history_records:
        val = float(str(row.get('Net_Asset', 0)).replace(',', ''))
        if val > 0: valid_history.append(val)

    history_len = len(valid_history)

    def get_growth_str(days, sim_text):
        if history_len >= days:
            past_net = valid_history[-days]
            rate = ((net_asset - past_net) / past_net) * 100
            s = "+" if rate >= 0 else ""
            return f"{s}{rate:.1f}%(實)"
        else:
            return f"{sim_text}(模)"

    m1_str = get_growth_str(30, "+10.7%")
    m3_str = get_growth_str(90, "+215.9%")
    y1_str = get_growth_str(365, "+83.1%")
    y3_str = get_growth_str(1095, "+195.7%")

    growth_text = f"🔺 近一月:{m1_str} | 近一季:{m3_str}\n🔺 近一年:{y1_str} | 近三年:{y3_str}"

    if history_len >= 30:
        past_30_net = valid_history[-30]
        monthly_growth_rate = (net_asset - past_30_net) / past_30_net
    else:
        monthly_growth_rate = 0.015 

    calc_rate = max(monthly_growth_rate, 0.001) 
    safe_net_asset = max(net_asset, 1)          
    
    targets = [7000000, 8000000, 9000000, 10000000]
    timeline_strs = ["- 2026-10: 🎖️ 成功嶺退伍日"]
    
    for target in targets:
        if safe_net_asset >= target:
            timeline_strs.append(f"- 已達標: {target//10000}萬 ✅")
        else:
            months_needed = math.log(target / safe_net_asset) / math.log(1 + calc_rate)
            target_month = tw_now.date().month + int(months_needed)
            target_year = tw_now.date().year + (target_month - 1) // 12
            final_month = (target_month - 1) % 12 + 1
            timeline_strs.append(f"- {target_year}-{final_month:02d}: {target//10000}萬 達標")
            
    timeline_text = "\n".join(timeline_strs)

    msg = f"""
{title_header}
======================
💎【資產總覽】
總資產 (Total)：${total_asset:,.0f}
淨資產 (Net)：${net_asset:,.0f}
{daily_str}
======================
📂【資產明細】
🇹🇼 台股現值：${tw_stock_value:,.0f}
🇺🇸 美股現值：${us_stock_value_twd:,.0f} (約 ${us_stock_value_usd:,.0f} USD)
🐣 基金現值：${fund_value:,.0f}
💵 現金(TWD)：${cash_twd:,.0f}
💴 現金(USD)：${cash_usd * usd_rate:,.0f} (約 ${cash_usd:,.0f} USD)
💸 質押借款：-${total_debt_with_interest:,.0f} (內含利息 ${accumulated_interest:,.0f})
======================
📑【資產板塊】
🇹🇼 現貨台股：{tw_pct:.1f}%
🦆 借款佔比：{debt_pct:.1f}%
🇺🇲 現貨美股：{us_pct:.1f}%
🐔 TSMC Exposure：{tsmc_pct:.1f}% 
======================
🛡️【風險盾牌】
質押維持率：{maintenance_ratio:.1f}% (狀態：{ratio_status})
======================
🚀【歷史增率】
{growth_text}
======================
🎯【模型預測】
• 千萬目標達成率：{progress_pct:.1f}%
 [{bar}] {progress_pct:.1f}%
• 時間軸推算
{timeline_text}
======================
📝【資產異動登錄】
🔗 表單捷徑：https://forms.gle/9ZEJawwNRGfiXQiV8
"""

    base_tg_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    requests.post(base_tg_url, data={"chat_id": TELEGRAM_CHAT_ID, "photo": line_url})
    requests.post(base_tg_url, data={"chat_id": TELEGRAM_CHAT_ID, "photo": pie_url, "caption": msg})

if __name__ == "__main__":
    main()
