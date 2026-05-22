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
    
    # 將 Config 轉為字典方便讀取
    config = {row['Key']: row['Value'] for row in config_data}
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
    # 使用 FinMind API 抓取台股報價 (抓取近5天，取最後一筆收盤價)
    url = "https://api.finmindtrade.com/api/v4/data"
    start_date = (datetime.date.today() - datetime.timedelta(days=7)).strftime("%Y-%m-%d")
    parameter = {
        "dataset": "TaiwanStockPrice",
        "data_id": str(symbol),
        "start_date": start_date,
        "token": FINMIND_TOKEN
    }
    try:
        resp = requests.get(url, params=parameter)
        data = resp.json()
        if data["msg"] == "success" and len(data["data"]) > 0:
            return data["data"][-1]["close"]
        return 0
    except:
        return 0

# ==========================================
# 4. 繪製 QuickChart 圖表
# ==========================================
def generate_pie_chart(tw_val, pledged_val, us_val):
    # 圓餅圖：現貨台股、質押台股、現貨美股
    tw_spot = tw_val - pledged_val
    chart_config = {
        "type": "outlabeledPie",
        "data": {
            "labels": ["🇹🇼 現貨台股", "🦆 質押台股", "🇺🇸 現貨美股"],
            "datasets": [{"backgroundColor": ["#36a2eb", "#ff6384", "#ffce56"], "data": [tw_spot, pledged_val, us_val]}]
        },
        "options": {"plugins": {"legend": {"display": False}, "outlabels": {"text": "%l %p", "color": "white", "stretch": 35, "font": {"resizable": True, "minSize": 12, "maxSize": 18}}}}
    }
    encoded_config = urllib.parse.quote(json.dumps(chart_config))
    return f"https://quickchart.io/chart?c={encoded_config}&w=400&h=250"

# ==========================================
# 5. 主程式運算邏輯
# ==========================================
def main():
    today_str = datetime.date.today().strftime("%m-%d")
    inventory, config, history_sheet = get_sheets_data()
    
    usd_rate = get_usd_twd_rate()
    
    tw_stock_value = 0
    us_stock_value_usd = 0
    cash_twd = 0
    cash_usd = 0
    fund_value = 0
    tsmc_exposure_twd = 0
    price_006208 = 0

    # 結算各項資產
    for item in inventory:
        symbol = str(item['Symbol'])
        shares = float(item['Shares'])
        itype = item['Type']
        
        if itype == '台股':
            price = get_tw_stock_price(symbol)
            value = price * shares
            tw_stock_value += value
            if symbol == '2330': tsmc_exposure_twd += value
            if symbol == '006208': 
                tsmc_exposure_twd += (value * 0.55) # 概算含積量 55%
                price_006208 = price
                
        elif itype == '美股':
            price = get_us_stock_price(symbol)
            value = price * shares
            us_stock_value_usd += value
            if symbol == 'TSM': tsmc_exposure_twd += (value * usd_rate)
            
        elif itype == '現金_TWD': cash_twd += shares
        elif itype == '現金_USD': cash_usd += shares
        elif itype == '基金': fund_value += shares

    # 彙整總值
    us_stock_value_twd = us_stock_value_usd * usd_rate
    total_cash_twd = cash_twd + (cash_usd * usd_rate)
    
    total_asset = tw_stock_value + us_stock_value_twd + total_cash_twd + fund_value
    debt = float(config.get('Current_Debt', 690000))
    net_asset = total_asset - debt
    
    # 質押維持率計算
    pledged_shares = float(config.get('Pledged_Shares_006208', 8000))
    pledged_value = pledged_shares * price_006208
    maintenance_ratio = (pledged_value / debt) * 100 if debt > 0 else 0
    ratio_status = "安全 ✅" if maintenance_ratio > 160 else "危險 ⚠️"

    # 進度條計算
    target_asset = float(config.get('Target_Asset', 10000000))
    progress_pct = (net_asset / target_asset) * 100
    filled_blocks = int(progress_pct / 10)
    bar = "█" * filled_blocks + "░" * (10 - filled_blocks)

    # 寫入 History (用於下次畫折線圖)
    history_sheet.append_row([
        datetime.date.today().strftime("%Y-%m-%d"), 
        round(total_asset, 2), 
        round(net_asset, 2), 
        debt, 
        round(tsmc_exposure_twd, 2)
    ])

    # 生成圖表連結
    pie_chart_url = generate_pie_chart(tw_stock_value, pledged_value, us_stock_value_twd)

    # ==========================================
    # 6. Telegram 報表排版與推播
    # ==========================================
    msg = f"""
🦎Tranquil Growth（{today_str} 盤後結算）
======================
💎【資產總覽】
總資產 (Total)：${total_asset:,.0f}
淨資產 (Net)：${net_asset:,.0f}
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

    # 發送 Telegram (文字 + 圓餅圖圖片)
    tg_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "photo": pie_chart_url,
        "caption": msg
    }
    requests.post(tg_url, data=payload)
    print("✅ 結算完成並已推播至 Telegram")

if __name__ == "__main__":
    main()
