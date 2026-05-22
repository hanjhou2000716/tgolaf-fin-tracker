import os
import json
import requests
import datetime
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
# 2. Google Sheets 動態資產結算核心 (AI 無敵掃描版)
# ==========================================
def calculate_current_assets():
    """
    自適應掃描整張表所有格子，不看固定欄位名稱，
    智慧辨識 類別、模式、代號、數量 進行流動資產庫存結算。
    """
    creds_dict = json.loads(GCP_CREDENTIALS_JSON)
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    sheet = client.open("Tranquil_Growth_DB")
    
    # 智慧模糊搜尋表單與歷史分頁
    form_ws = None
    history_sheet = None
    for ws in sheet.worksheets():
        title_clean = ws.title.strip().lower()
        if "表單" in title_clean or "form" in title_clean or "回覆" in title_clean:
            form_ws = ws
        elif "history" in title_clean or "歷史" in title_clean or "紀錄" in title_clean:
            history_sheet = ws
            
    if form_ws is None: raise ValueError("❌ 找不到表單回應分頁！")
    if history_sheet is None: raise ValueError("❌ 找不到 History 歷史紀錄分頁！")
        
    all_rows = form_ws.get_all_values()
    if not all_rows or len(all_rows) <= 1:
        return {}, history_sheet
        
    data_rows = all_rows[1:]
    
    inventory = {
        "台股": {}, "美股": {}, "基金": {}, 
        "現金_TWD": {"TWD": 0.0}, "現金_USD": {"USD": 0.0},
        "質押負債": {"Current_Debt": 0.0}
    }
    
    symbol_overrides = {
        '6208': '006208', '403A': '00403A', '886': '00886', 
        '895': '00895', '878': '00878', '685L': '00685L'
    }
    
    # 建立已知資產清單快取，協助特徵掃描
    known_symbols = [
        '6208', '006208', '403A', '00403A', '886', '00886', '895', '00895',
        '878', '00878', '3455', '8033', '2330', '3665', '685L', '00685L',
        'QQQM', 'NVDA', 'SPYG', 'TSM', 'VOO', 'VTI', 'TSLA', 'AAPL', 'QQQ',
        'FUND', 'TWD', 'USD', 'CURRENT_DEBT'
    ]
    
    for row in data_rows:
        cells = [str(c).strip() for c in row if str(c).strip() != ""]
        if not cells: continue
        
        asset_type = ""
        mode = ""
        symbol = ""
        potential_numbers = []
        
        # 內容特徵掃描演算法
        for cell in cells:
            c_upper = cell.upper()
            if any(x in cell for x in ["台股", "美股", "基金", "現金", "質押", "負債"]):
                asset_type = cell
            elif any(x in cell for x in ["買入", "存入", "賣出", "提領", "取代", "覆蓋", "更新"]):
                mode = cell
            elif c_upper in known_symbols or any(char.isalpha() for char in c_upper):
                if "/" not in cell and "-" not in cell: # 排除日期
                    symbol = cell
            else:
                try:
                    float(cell.replace(",", "").replace("$", ""))
                    potential_numbers.append(cell)
                except ValueError:
                    pass
                    
        # 錯位自適應校正
        if not symbol and len(potential_numbers) >= 2:
            symbol = potential_numbers[0]
            amount_str = potential_numbers[-1]
        elif len(potential_numbers) >= 1:
            amount_str = potential_numbers[-1]
        else:
            amount_str = "0"
            
        if not asset_type: continue
        if not mode: mode = "取代"
        
        # 標準化類別
        if "台" in asset_type and "股" in asset_type: asset_type = "台股"
        elif "美" in asset_type and "股" in asset_type: asset_type = "美股"
        elif "基" in asset_type and "金" in asset_type: asset_type = "基金"
        elif "USD" in asset_type or "美金" in asset_type: asset_type = "現金_USD"
        elif "TWD" in asset_type or "台幣" in asset_type or "現金" in asset_type: asset_type = "現金_TWD"
        elif "質押" in asset_type or "負債" in asset_type: asset_type = "質押負債"
        
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
            
        # 執行權重流動運算
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

def generate_pie_chart(tw_val, pledged_val, us_val):
    tw_spot = max(0, tw_val - pledged_val)
    chart_config = {
        "type": "outlabeledPie",
        "data": {
            "labels": ["🇹🇼 現貨台股", "🦆 質押台股", "🇺🇸 現貨美股"],
            "datasets": [{"backgroundColor": ["#36a2eb", "#ff6384", "#ffce56"], "data": [tw_spot, pledged_val, us_val]}]
        },
        "options": {
            "plugins": {
                "legend": {"display": False},
                "outlabels": {"text": "%l %p", "color": "white", "stretch": 35, "font": {"minSize": 12}}
            }
        }
    }
    if tw_spot == 0 and pledged_val == 0 and us_val == 0:
        chart_config["data"]["labels"] = ["尚無資產數據"]
        chart_config["data"]["datasets"][0]["data"] = [1]
        chart_config["data"]["datasets"][0]["backgroundColor"] = ["#cccccc"]
    return f"https://quickchart.io/chart?c={urllib.parse.quote(json.dumps(chart_config))}&w=400&h=250"

def generate_line_chart(history_records, today_str, total_asset, net_asset):
    dates = []
    total_data = []
    net_data = []
    
    # 過濾 History 中可能因為測試產生的 0 異常值
    for row in history_records[-14:]:
        d = str(row.get('Date', ''))[-5:]
        total = float(str(row.get('Total_Asset', 0)).replace(',', ''))
        net = float(str(row.get('Net_Asset', 0)).replace(',', ''))
        if d and total > 0:  
            dates.append(d)
            total_data.append(total)
            net_data.append(net)
            
    dates.append(today_str)
    total_data.append(total_asset)
    net_data.append(net_asset)
    
    chart_config = {
        "type": "line",
        "data": {
            "labels": dates,
            "datasets": [
                {"label": "總資產 (Total)", "data": total_data, "borderColor": "#36a2eb", "fill": False, "tension": 0.1},
                {"label": "淨資產 (Net)", "data": net_data, "borderColor": "#ff6384", "fill": False, "tension": 0.1}
            ]
        },
        "options": {"title": {"display": True, "text": "近期資產軌跡 (Total vs Net)"}}
    }
    return f"https://quickchart.io/chart?c={urllib.parse.quote(json.dumps(chart_config))}&w=400&h=250"

# ==========================================
# 4. 核心結算與通知發送主程序
# ==========================================
def main():
    today_str = datetime.date.today().strftime("%m-%d")
    
    inventory, history_sheet = calculate_current_assets()
    try: history_records = history_sheet.get_all_records()
    except: history_records = []
        
    usd_rate = get_usd_twd_rate()
    tw_stock_value, us_stock_value_usd, tsmc_exposure_twd, price_006208 = 0, 0, 0, 0
    cash_twd = inventory["現金_TWD"].get("TWD", 0)
    cash_usd = inventory["現金_USD"].get("USD", 0)
    fund_value = sum(inventory["基金"].values())

    # 結算台股與含積量
    for symbol, shares in inventory["台股"].items():
        if shares <= 0: continue
        price = get_tw_stock_price(symbol)
        value = price * shares
        tw_stock_value += value
        if symbol == '2330': tsmc_exposure_twd += value
        if symbol == '006208': 
            tsmc_exposure_twd += (value * 0.55)
            price_006208 = price

    # 結算美股與含積量
    for symbol, shares in inventory["美股"].items():
        if shares <= 0: continue
        price = get_us_stock_price(symbol)
        value = price * shares
        us_stock_value_usd += value
        if symbol == 'TSM': tsmc_exposure_twd += (value * usd_rate)

    us_stock_value_twd = us_stock_value_usd * usd_rate
    total_cash_twd = cash_twd + (cash_usd * usd_rate)
    debt = inventory["質押負債"].get("Current_Debt", 0)
    
    total_asset = tw_stock_value + us_stock_value_twd + total_cash_twd + fund_value
    net_asset = total_asset - debt
    
    total_006208_shares = inventory["台股"].get("006208", 0)
    pledged_shares = min(total_006208_shares, 8000) if total_006208_shares > 0 else 0
    pledged_value = pledged_shares * price_006208
    
    # 數據健康度與比例指標計算
    maintenance_ratio = (pledged_value / debt) * 100 if debt > 0 else 0
    ratio_status = "安全 ✅" if maintenance_ratio > 160 else ("危險 ⚠️" if debt > 0 else "無借款 ✅")

    tw_pct = ((tw_stock_value - pledged_value)/total_asset)*100 if total_asset > 0 else 0
    pledged_pct = (pledged_value/total_asset)*100 if total_asset > 0 else 0
    us_pct = (us_stock_value_twd/total_asset)*100 if total_asset > 0 else 0
    tsmc_pct = (tsmc_exposure_twd/net_asset)*100 if net_asset > 0 else 0

    # 尋找上一筆大於0的有效歷史數據計算單日變化
    yesterday_net = 0
    for row in reversed(history_records):
        val = float(str(row.get('Net_Asset', 0)).replace(',', ''))
        if val > 0:
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

    # 防呆：只有當計算出真實資產時，才將紀錄寫入 History
    if total_asset > 0:
        history_sheet.append_row([
            datetime.date.today().strftime("%Y-%m-%d"), 
            round(total_asset, 2), round(net_asset, 2), debt, round(tsmc_exposure_twd, 2)
        ])

    pie_url = generate_pie_chart(tw_stock_value, pledged_value, us_stock_value_twd)
    line_url = generate_line_chart(history_records, today_str, total_asset, net_asset)

    # 建立包含歷史增率的 Telegram 模板
    msg = f"""
🦎Tranquil Growth（{today_str} 盤後結算）
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
💸 質押借款：-${debt:,.0f}
======================
📑【資產板塊】
🇹🇼 現貨台股：{tw_pct:.1f}%
🦆 質押台股：{pledged_pct:.1f}%
🇺🇲 現貨美股：{us_pct:.1f}%
🐔 TSMC Exposure：{tsmc_pct:.1f}% 
======================
🛡️【風險盾牌】
質押維持率：{maintenance_ratio:.1f}% (狀態：{ratio_status})
======================
🚀【歷史增率】
🔺 近一月:+10.7%(模) | 近一季:+215.9%(模)
🔺 近一年:+83.1%(模) | 近三年:+195.7%(模)
======================
🎯【模型預測】
• 千萬目標達成率：{progress_pct:.1f}%
 [{bar}] {progress_pct:.1f}%
• 時間軸推算
- 2026-10: 🎖️ 成功嶺退伍日
- 2027-01: 700萬 達標
- 2027-12: 800萬 達標
- 2028-11: 900萬 達標
- 2029-08: 1000萬 達標
======================
📝【資產異動登錄】
🔗 表單捷徑：https://forms.gle/9ZEJawwNRGfiXQiV8
"""

    base_tg_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    requests.post(base_tg_url, data={"chat_id": TELEGRAM_CHAT_ID, "photo": line_url})
    requests.post(base_tg_url, data={"chat_id": TELEGRAM_CHAT_ID, "photo": pie_url, "caption": msg})

if __name__ == "__main__":
    main()
