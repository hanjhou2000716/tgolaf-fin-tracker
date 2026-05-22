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
# 2. Google Sheets 連線與資料讀取
# ==========================================
def get_sheets_data():
    creds_dict = json.loads(GCP_CREDENTIALS_JSON)
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    
    sheet = client.open("Tranquil_Growth_DB")
    inventory_data = sheet.worksheet("Inventory").get_all_records()
    config_data = sheet.worksheet("Config").get_all_records()
    history_sheet = sheet.worksheet("History")
    
    config = {}
    for row in config_data:
        cleaned_row = {str(k).strip().capitalize(): v for k, v in row.items()}
        if 'Key' in cleaned_row and 'Value' in cleaned_row:
            config[cleaned_row['Key']] = cleaned_row['Value']
            
    return inventory_data, config, history_sheet

# ==========================================
# 3. 報價抓取模組
# ==========================================
def get_usd_twd_rate():
    ticker = yf.Ticker("TWD=X")
    return ticker.history(period="1d")['Close'].iloc[-1]

def get_us_stock_price(symbol):
    try:
        ticker = yf.Ticker(symbol)
        return ticker.history(period="1d")['Close'].iloc[-1]
    except:
        return 0

def get_tw_stock_price(symbol):
    url = "https://api.finmindtrade.com/api/v4/data"
    start_date = (datetime.date.today() - datetime.timedelta(days=7)).strftime("%Y-%m-%d")
    parameter = {"dataset": "TaiwanStockPrice", "data_id": str(symbol), "start_date": start_date, "token": FINMIND_TOKEN}
    try:
        resp = requests.get(url, params=parameter)
        data = resp.json()
        if data["msg"] == "success" and len(data["data"]) > 0:
            return data["data"][-1]["close"]
        return 0
    except:
        return 0

# ==========================================
# 4. 圖表渲染模組
# ==========================================
def generate_pie_chart(tw_val, pledged_val, us_val):
    tw_spot = tw_val - pledged_val
    chart_config = {
        "type": "outlabeledPie",
        "data": {
            "labels": ["🇹🇼 現貨台股", "🦆 質押台股", "🇺🇸 現貨美股"],
            "datasets": [{"backgroundColor": ["#36a2eb", "#ff6384", "#ffce56"], "data": [tw_spot, pledged_val, us_val]}]
        },
        "options": {"plugins": {"legend": {"display": False}, "outlabels": {"text": "%l %p", "color": "white", "stretch": 35, "font": {"minSize": 12}}}}
    }
    return f"https://quickchart.io/chart?c={urllib.parse.quote(json.dumps(chart_config))}&w=400&h=250"

def generate_line_chart(history_records, today_str, total_asset, net_asset):
    # 抓取近 14 天的歷史數據並補上今天的數據
    dates = [str(row.get('Date', ''))[-5:] for row in history_records[-14:]] + [today_str]
    total_data = [float(str(row.get('Total_Asset', 0)).replace(',', '')) for row in history_records[-14:]] + [total_asset]
    net_data = [float(str(row.get('Net_Asset', 0)).replace(',', '')) for row in history_records[-14:]] + [net_asset]
    
    chart_config = {
        "type": "line",
        "data": {
            "labels": dates,
            "datasets": [
                {"label": "總資產", "data": total_data, "borderColor": "#36a2eb", "fill": False},
                {"label": "淨資產", "data": net_data, "borderColor": "#ff6384", "fill": False}
            ]
        },
        "options": {"title": {"display": True, "text": "近期資產軌跡 (Total vs Net)"}}
    }
    return f"https://quickchart.io/chart?c={urllib.parse.quote(json.dumps(chart_config))}&w=400&h=250"

# ==========================================
# 5. 主程式運算邏輯
# ==========================================
def main():
    today_str = datetime.date.today().strftime("%m-%d")
    inventory, config, history_sheet = get_sheets_data()
    
    try:
        history_records = history_sheet.get_all_records()
    except:
        history_records = []
    
    usd_rate = get_usd_twd_rate()
    
    tw_stock_value = 0
    us_stock_value_usd = 0
    cash_twd = 0
    cash_usd = 0
    fund_value = 0
    tsmc_exposure_twd = 0
    price_006208 = 0

    # 修正 Google Sheets 自動去掉 0 的問題
    symbol_overrides = {'6208': '006208', '403A': '00403A', '886': '00886', '895': '00895', '878': '00878', '685L': '00685L'}

    for item in inventory:
        raw_symbol = str(item['Symbol']).strip()
        symbol = symbol_overrides.get(raw_symbol, raw_symbol)
        shares = float(item['Shares'])
        itype = item['Type']
        
        if itype == '台股':
            price = get_tw_stock_price(symbol)
            value = price * shares
            tw_stock_value += value
            if symbol == '2330': tsmc_exposure_twd += value
            if symbol == '006208': 
                tsmc_exposure_twd += (value * 0.55)
                price_006208 = price
                
        elif itype == '美股':
            price = get_us_stock_price(symbol)
            value = price * shares
            us_stock_value_usd += value
            if symbol == 'TSM': tsmc_exposure_twd += (value * usd_rate)
            
        elif itype == '現金_TWD': cash_twd += shares
        elif itype == '現金_USD': cash_usd += shares
        elif itype == '基金': fund_value += shares

    us_stock_value_twd = us_stock_value_usd * usd_rate
    total_cash_twd = cash_twd + (cash_usd * usd_rate)
    
    total_asset = tw_stock_value + us_stock_value_twd + total_cash_twd + fund_value
    debt = float(config.get('Current_Debt', 690000))
    net_asset = total_asset - debt
    
    # 計算單日變化 (抓昨天最後一筆淨資產)
    yesterday_net = float(str(history_records[-1].get('Net_Asset', 0)).replace(',', '')) if len(history_records) > 0 else 0
    daily_diff = net_asset - yesterday_net if yesterday_net else 0
    daily_pct = (daily_diff / yesterday_net * 100) if yesterday_net else 0
    sign = "+" if daily_diff >= 0 else ""
    emoji = "📈" if daily_diff >= 0 else "📉"
    daily_str = f"單日變化：{emoji}{sign}{daily_pct:.1f}% ({sign}${daily_diff:,.0f})" if yesterday_net else "單日變化：-- (首日無數據)"

    # 質押維持率
    pledged_shares = float(config.get('Pledged_Shares_006208', 8000))
    pledged_value = pledged_shares * price_006208
    maintenance_ratio = (pledged_value / debt) * 100 if debt > 0 else 0
    ratio_status = "安全 ✅" if maintenance_ratio > 160 else "危險 ⚠️"

    # 目標進度
    target_asset = float(config.get('Target_Asset', 10000000))
    progress_pct = (net_asset / target_asset) * 100
    bar = "█" * int(progress_pct / 10) + "░" * (10 - int(progress_pct / 10))

    # 寫入今日歷史數據
    history_sheet.append_row([
        datetime.date.today().strftime("%Y-%m-%d"), 
        round(total_asset, 2), 
        round(net_asset, 2), 
        debt, 
        round(tsmc_exposure_twd, 2)
    ])

    # 產生兩張圖表
    pie_url = generate_pie_chart(tw_stock_value, pledged_value, us_stock_value_twd)
    line_url = generate_line_chart(history_records, today_str, total_asset, net_asset)

    # 組合訊息字串
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
🇹🇼 現貨台股：{((tw_stock_value - pledged_value)/total_asset)*100:.1f}%
🦆 質押台股：{(pledged_value/total_asset)*100:.1f}%
🇺🇲 現貨美股：{(us_stock_value_twd/total_asset)*100:.1f}%
🐔 TSMC Exposure：{(tsmc_exposure_twd/net_asset)*100:.1f}% 
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
"""

    # 傳送至 Telegram (傳送兩張圖：折線圖與圓餅圖+報表)
    base_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    requests.post(base_url, data={"chat_id": TELEGRAM_CHAT_ID, "photo": line_url})
    requests.post(base_url, data={"chat_id": TELEGRAM_CHAT_ID, "photo": pie_url, "caption": msg})

if __name__ == "__main__":
    main()
