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
# 2. Google Sheets 動態資產結算核心
# ==========================================
def calculate_current_assets():
    """
    連線 Google Sheets，讀取表單回應紀錄，
    依時間流動順序動態結算出「買入、賣出、全部取代」後的最新資產庫存。
    """
    creds_dict = json.loads(GCP_CREDENTIALS_JSON)
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    
    sheet = client.open("Tranquil_Growth_DB")
    
    # 讀取 Google 表單連動的預設分頁
    form_ws = sheet.worksheet("表單回應 1")
    form_records = form_ws.get_all_records()
    history_sheet = sheet.worksheet("History")
    
    # 初始化資產板塊結構
    inventory = {
        "台股": {}, "美股": {}, "基金": {}, 
        "現金_TWD": {"TWD": 0.0}, "現金_USD": {"USD": 0.0},
        "質押負債": {"Current_Debt": 0.0}
    }
    
    # 修正 Google 試算表自動去掉股票代號開頭 0 的防呆字典
    symbol_overrides = {
        '6208': '006208', '403A': '00403A', '886': '00886', 
        '895': '00895', '878': '00878', '685L': '00685L'
    }
    
    # 核心計算迴圈：流動式結算歷史所有填寫紀錄
    for row in form_records:
        # 去除欄位名稱與數值的隱形空白鍵
        cleaned_row = {str(k).strip(): str(v).strip() for k, v in row.items()}
        
        asset_type = cleaned_row.get('資產類別', '')
        raw_symbol = cleaned_row.get('資產代號/名稱', '')
        mode = cleaned_row.get('異動模式', '')
        raw_amount = cleaned_row.get('數量/股數/金額', '0')
        
        if not asset_type or asset_type not in inventory:
            continue
            
        try:
            amount = float(raw_amount.replace(',', ''))
        except ValueError:
            continue
            
        # 修正代號
        symbol = symbol_overrides.get(raw_symbol, raw_symbol)
        
        # 若填寫現金或負債時不小心將代號留空，給予預設基準金鑰
        if asset_type in ["現金_TWD", "現金_USD", "質押負債"] and not symbol:
            symbol = "TWD" if asset_type == "現金_TWD" else ("USD" if asset_type == "現金_USD" else "Current_Debt")
            
        if not symbol:
            continue
            
        # 若此資產首次出現，初始化數值
        if symbol not in inventory[asset_type]:
            inventory[asset_type][symbol] = 0.0
            
        # 依異動模式進行全方位加、減、或覆蓋計算
        if "買入" in mode or "存入" in mode or "+" in mode:
            inventory[asset_type][symbol] += amount
        elif "賣出" in mode or "提領" in mode or "-" in mode:
            inventory[asset_type][symbol] -= amount
        elif "全部取代" in mode or "覆蓋" in mode:
            inventory[asset_type][symbol] = amount

    return inventory, history_sheet

# ==========================================
# 3. 金融市場報價與 QuickChart 繪圖模組
# ==========================================
def get_usd_twd_rate():
    try:
        return yf.Ticker("TWD=X").history(period="1d")['Close'].iloc[-1]
    except:
        return 32.3  # 網路異常時的安全基本匯率防呆

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
    """產出包含現貨台股、質押台股、現貨美股三板塊比例的圓餅圖"""
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
    return f"https://quickchart.io/chart?c={urllib.parse.quote(json.dumps(chart_config))}&w=400&h=250"

def generate_line_chart(history_records, today_str, total_asset, net_asset):
    """讀取 History 工作表，繪製最近 14 天總資產與淨資產交叉變化的雙線折線圖"""
    dates = []
    total_data = []
    net_data = []
    
    for row in history_records[-14:]:
        d = str(row.get('Date', ''))[-5:]
        if d:
            dates.append(d)
            total_data.append(float(str(row.get('Total_Asset', 0)).replace(',', '')))
            net_data.append(float(str(row.get('Net_Asset', 0)).replace(',', '')))
            
    # 補上今日最新實時點
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
        "options": {
            "title": {"display": True, "text": "近期資產軌跡 (Total vs Net)"}
        }
    }
    return f"https://quickchart.io/chart?c={urllib.parse.quote(json.dumps(chart_config))}&w=400&h=250"

# ==========================================
# 4. 核心結算與通知發送主程序
# ==========================================
def main():
    today_str = datetime.date.today().strftime("%m-%d")
    
    # 1. 執行流動資產庫存加總結算
    inventory, history_sheet = calculate_current_assets()
    
    try:
        history_records = history_sheet.get_all_records()
    except:
        history_records = []
        
    # 2. 獲取即時市場資訊
    usd_rate = get_usd_twd_rate()
    
    tw_stock_value = 0
    us_stock_value_usd = 0
    cash_twd = inventory["現金_TWD"].get("TWD", 0)
    cash_usd = inventory["現金_USD"].get("USD", 0)
    fund_value = sum(inventory["基金"].values())
    tsmc_exposure_twd = 0
    price_006208 = 0

    # 3. 逐筆計算台股現值與含積量曝險
    for symbol, shares in inventory["台股"].items():
        if shares <= 0: continue
        price = get_tw_stock_price(symbol)
        value = price * shares
        tw_stock_value += value
        if symbol == '2330': 
            tsmc_exposure_twd += value
        if symbol == '006208': 
            tsmc_exposure_twd += (value * 0.55) # 006208 含積權重粗估 55%
            price_006208 = price

    # 4. 逐筆計算美股現值與含積量曝險 (TSM ADR)
    for symbol, shares in inventory["美股"].items():
        if shares <= 0: continue
        price = get_us_stock_price(symbol)
        value = price * shares
        us_stock_value_usd += value
        if symbol == 'TSM': 
            tsmc_exposure_twd += (value * usd_rate)

    # 5. 全球總體資產規模彙整計算
    us_stock_value_twd = us_stock_value_usd * usd_rate
    total_cash_twd = cash_twd + (cash_usd * usd_rate)
    debt = inventory["質押負債"].get("Current_Debt", 0)
    
    total_asset = tw_stock_value + us_stock_value_twd + total_cash_twd + fund_value
    net_asset = total_asset - debt
    
    # 6. 質押安全健康度控管
    total_006208_shares = inventory["台股"].get("006208", 0)
    # 控制質押專戶上限股數（預設取總股數中最多 8,000 股進行精準維持率監控）
    pledged_shares = min(total_006208_shares, 8000) if total_006208_shares > 0 else 0
    pledged_value = pledged_shares * price_006208
    
    maintenance_ratio = (pledged_value / debt) * 100 if debt > 0 else 0
    ratio_status = "安全 ✅" if maintenance_ratio > 160 else "危險 ⚠️"
    if debt == 0: 
        ratio_status = "無借款 ✅"

    # 7. 計算單日財富增減變化幅度 (對比 History 分頁上一次執行成果)
    yesterday_net = float(str(history_records[-1].get('Net_Asset', 0)).replace(',', '')) if len(history_records) > 0 else 0
    daily_diff = net_asset - yesterday_net if yesterday_net else 0
    daily_pct = (daily_diff / yesterday_net * 100) if yesterday_net else 0
    sign = "+" if daily_diff >= 0 else ""
    emoji = "📈" if daily_diff >= 0 else "📉"
    daily_str = f"單日變化：{emoji}{sign}{daily_pct:.1f}% ({sign}${daily_diff:,.0f})" if yesterday_net else "單日變化：-- (首日累積數據中)"

    # 8. 視覺化進度條生成
    progress_pct = (net_asset / 10000000) * 100
    bar_blocks = max(0, min(10, int(progress_pct / 10)))
    bar = "█" * bar_blocks + "░" * (10 - bar_blocks)

    # 9. 數據落庫自動化備份
    history_sheet.append_row([
        datetime.date.today().strftime("%Y-%m-%d"), 
        round(total_asset, 2), 
        round(net_asset, 2), 
        debt, 
        round(tsmc_exposure_twd, 2)
    ])

    # 10. 呼叫 QuickChart 渲染生成雙動態圖表網址
    pie_url = generate_pie_chart(tw_stock_value, pledged_value, us_stock_value_twd)
    line_url = generate_line_chart(history_records, today_str, total_asset, net_asset)

    # 11. 建立 Telegram 排版文字內容
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
🐔 TSMC Exposure：{((tsmc_exposure_twd / net_asset * 100) if net_asset > 0 else 0):.1f}% 
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

    # 12. 雙連發推送 Telegram 機制
    base_tg_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    # 第一發：推送歷史資產軌跡折線圖
    requests.post(base_tg_url, data={"chat_id": TELEGRAM_CHAT_ID, "photo": line_url})
    # 第二發：推送板塊比例圓餅圖並附帶詳盡文字對帳單
    requests.post(base_tg_url, data={"chat_id": TELEGRAM_CHAT_ID, "photo": pie_url, "caption": msg})
    print("✅ 🦎Tranquil Growth 系統全自動資產結算與雙圖表推播完成。")

if __name__ == "__main__":
    main()
