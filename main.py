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
# 2. Google Sheets 動態資產結算核心 (錯位自適應版)
# ==========================================
def calculate_current_assets():
    """
    自適應解析因複製產生的欄位錯位，
    精準提取 B(類別)、C/D(代號)、E(模式)、F(數量) 進行流動資產結算。
    """
    creds_dict = json.loads(GCP_CREDENTIALS_JSON)
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    sheet = client.open("Tranquil_Growth_DB")
    
    # 智慧模糊搜尋工作表
    form_ws = None
    history_sheet = None
    for ws in sheet.worksheets():
        title_clean = ws.title.strip().lower()
        if "表單" in title_clean or "form" in title_clean or "回覆" in title_clean:
            form_ws = ws
        elif "history" in title_clean or "歷史" in title_clean or "紀錄" in title_clean:
            history_sheet = ws
            
    if form_ws is None:
        raise ValueError("❌ 找不到表單回應分頁！")
    if history_sheet is None:
        raise ValueError("❌ 找不到 History 歷史紀錄分頁！")
        
    # 讀取整張表所有格子 (包含無表頭的 F 欄)
    all_rows = form_ws.get_all_values()
    if not all_rows or len(all_rows) <= 1:
        print("⚠️ 表單分頁中無任何數據列。")
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
    
    print(f"🔄 開始動態解析 {len(data_rows)} 筆歷史異動...")
    
    for idx, row in enumerate(data_rows, start=2):
        # 補足長度防範空列索引錯誤
        while len(row) < 6:
            row.append("")
            
        row_clean = [str(cell).strip() for cell in row]
        
        # 核心：破解複製規律的位置自適應映射
        asset_type = row_clean[1]  # B欄：永遠是資產類別
        mode = row_clean[4]        # E欄：永遠是異動模式/全部取代
        raw_amount = row_clean[5]  # F欄：永遠是真正的數量/金額
        
        # 代號會根據文字或數字分裂在 C欄 或 D欄
        if row_clean[2]:
            raw_symbol = row_clean[2]
        elif row_clean[3]:
            raw_symbol = row_clean[3]
        else:
            raw_symbol = ""
            
        # 備用機制：萬一連 B/E/F 欄位順序都變了，自動啟動內容特徵掃描
        if not any(x in asset_type for x in ["股", "金", "TWD", "USD", "質押", "負債"]):
            for cell in row_clean:
                if any(x in cell for x in ["台股", "美股", "基金", "現金", "質押"]): asset_type = cell
                if any(x in cell for x in ["全部取代", "覆蓋", "買入", "賣出"]): mode = cell
                
        if not asset_type: 
            continue
            
        # 規格化資產種類名稱
        if "台" in asset_type and "股" in asset_type: asset_type = "台股"
        elif "美" in asset_type and "股" in asset_type: asset_type = "美股"
        elif "基" in asset_type and "金" in asset_type: asset_type = "基金"
        elif "USD" in asset_type or "美金" in asset_type: asset_type = "現金_USD"
        elif "TWD" in asset_type or "台幣" in asset_type or "現金" in asset_type: asset_type = "現金_TWD"
        elif "質押" in asset_type or "負債" in asset_type: asset_type = "質押負債"
        
        if asset_type not in inventory: continue
        
        # 規格化數量/股數
        try:
            amount_str = raw_amount.replace(",", "").replace("$", "")
            amount = float(amount_str) if amount_str else 0.0
        except ValueError:
            continue
            
        # 規格化股票代號
        symbol = symbol_overrides.get(raw_symbol, raw_symbol)
        if asset_type in ["現金_TWD", "現金_USD", "質押負債"] and not symbol:
            symbol = "TWD" if asset_type == "現金_TWD" else ("USD" if asset_type == "現金_USD" else "Current_Debt")
            
        if not symbol: continue
        
        if symbol not in inventory[asset_type]:
            inventory[asset_type][symbol] = 0.0
            
        # 依照模式進行流動式權重堆疊計算
        if "買入" in mode or "存入" in mode or "+" in mode:
            inventory[asset_type][symbol] += amount
        elif "賣出" in mode or "提領" in mode or "-" in mode:
            inventory[asset_type][symbol] -= amount
        elif "取代" in mode or "覆蓋" in mode or "更新" in mode:
            inventory[asset_type][symbol] = amount
            
        print(f"✅ 解析命中 -> 列號:{idx} | 類別:{asset_type} | 代號:{symbol} | 數量:{amount}")

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
    for row in history_records[-14:]:
        d = str(row.get('Date', ''))[-5:]
        if d:
            dates.append(d)
            total_data.append(float(str(row.get('Total_Asset', 0)).replace(',', '')))
            net_data.append(float(str(row.get('Net_Asset', 0)).replace(',', '')))
            
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

    # 結算美股與含積量 (TSM)
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
    
    # 計算風險指標與佔比
    maintenance_ratio = (pledged_value / debt) * 100 if debt > 0 else 0
    ratio_status = "安全 ✅" if maintenance_ratio > 160 else ("危險 ⚠️" if debt > 0 else "無借款 ✅")

    tw_pct = ((tw_stock_value - pledged_value)/total_asset)*100 if total_asset > 0 else 0
    pledged_pct = (pledged_value/total_asset)*100 if total_asset > 0 else 0
    us_pct = (us_stock_value_twd/total_asset)*100 if total_asset > 0 else 0
    tsmc_pct = (tsmc_exposure_twd/net_asset)*100 if net_asset > 0 else 0

    # 計算單日損益變化
    yesterday_net = float(str(history_records[-1].get('Net_Asset', 0)).replace(',', '')) if len(history_records) > 0 else 0
    daily_diff = net_asset - yesterday_net if yesterday_net else 0
    daily_pct = (daily_diff / yesterday_net * 100) if yesterday_net else 0
    sign = "+" if daily_diff >= 0 else ""
    emoji = "📈" if daily_diff >= 0 else "📉"
    daily_str = f"單日變化：{emoji}{sign}{daily_pct:.1f}% ({sign}${daily_diff:,.0f})" if yesterday_net else "單日變化：-- (首日累積數據中)"

    progress_pct = (net_asset / 10000000) * 100 if net_asset > 0 else 0
    bar_blocks = max(0, min(10, int(progress_pct / 10)))
    bar = "█" * bar_blocks + "░" * (10 - bar_blocks)

    # 落庫保存今日正確歷史數據
    history_sheet.append_row([
        datetime.date.today().strftime("%Y-%m-%d"), 
        round(total_asset, 2), round(net_asset, 2), debt, round(tsmc_exposure_twd, 2)
    ])

    pie_url = generate_pie_chart(tw_stock_value, pledged_value, us_stock_value_twd)
    line_url = generate_line_chart(history_records, today_str, total_asset, net_asset)

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
