import os
import json
import requests
import datetime
import math
import re
import yfinance as yf
import gspread
from google.oauth2.service_account import Credentials
from risk import (
    HALF_KELLY_LIMIT,
    beta_capacity as calculate_beta_capacity,
    beta_status as classify_beta_capacity,
    maintenance_ratio as calculate_maintenance_ratio,
    maintenance_status,
    stress_scenarios as build_stress_scenarios,
    composite_guardrails,
)
from validation import validate_history_sheet, validate_inventory, validate_quote
from asset_tree import build_asset_tree
from public_site import write_public_site
from supabase_sync import upload_private_snapshot, upload_private_transactions
from transaction_schema import TransactionSchemaError, parse_transaction_rows
from performance import performance_breakdown
from market_data import MarketDataService
from metrics import summarize_performance
from attribution import build_pnl_attribution
from exposure import build_exposure_matrix
from pledge_safety import pledge_safety_center
from alerts import AlertEngine
from scenario_lab import run_scenario
from history_store import (
    build_header_map,
    column_to_a1,
    ensure_history_columns,
    find_row_by_key,
    upsert_history_snapshot,
)
from runtime_extensions import build_runtime_extensions

# ==========================================
# 1. 環境變數與金鑰設定
# ==========================================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
FINMIND_TOKEN = os.getenv("FINMIND_TOKEN")
GCP_CREDENTIALS_JSON = os.getenv("GCP_CREDENTIALS")
FORCE_TELEGRAM = os.getenv("FORCE_TELEGRAM", "false").strip().lower() in {"1", "true", "yes", "on"}
FORM_SCHEMA_STRICT = os.getenv("FORM_SCHEMA_STRICT", "true").strip().lower() in {"1", "true", "yes", "on"}
FORM_SCHEMA_LEGACY_COMPAT = os.getenv("FORM_SCHEMA_LEGACY_COMPAT", "false").strip().lower() in {"1", "true", "yes", "on"}
WEB_APP_URL = "https://hanjhou2000716.github.io/tgolaf-fin-tracker/"


HISTORY_EXTRA_COLUMNS = ["TW_Stock_Value", "US_Stock_Value", "Cash_Value", "Fund_Value", "NVDA_QQQM_Weight", "NVDA_SPYG_Weight", "NVDA_VOO_Weight", "Settlement_Notification_Sent_At"]
ETF_NVDA_WEIGHT_FALLBACKS = {"QQQM": 0.095, "SPYG": 0.075, "VOO": 0.070}
MARKET_DATA = MarketDataService()


def settlement_notification_sent(history_sheet, snapshot_date, window_key):
    """Read the durable per-day/per-window notification marker from History."""
    if history_sheet is None:
        return False
    header_map = build_header_map(history_sheet.row_values(1))
    marker_column = header_map.get("Settlement_Notification_Sent_At")
    if not marker_column:
        return False
    row_number = find_row_by_key(history_sheet, "Date", snapshot_date)
    if row_number is None:
        return False
    row = history_sheet.row_values(row_number)
    raw_marker = str(row[marker_column - 1]).strip() if len(row) >= marker_column else ""
    if not raw_marker:
        return False
    try:
        markers = json.loads(raw_marker)
    except json.JSONDecodeError:
        # Legacy single-timestamp markers do not identify a window; allow both
        # new settlement windows to send once after rollout.
        return False
    return isinstance(markers, dict) and bool(markers.get(window_key))


def mark_settlement_notification_sent(history_sheet, snapshot_date, window_key, sent_at):
    if history_sheet is None:
        return
    header_map = build_header_map(history_sheet.row_values(1))
    marker_column = header_map.get("Settlement_Notification_Sent_At")
    if not marker_column:
        return
    row_number = find_row_by_key(history_sheet, "Date", snapshot_date)
    if row_number is None:
        return
    row = history_sheet.row_values(row_number)
    raw_marker = str(row[marker_column - 1]).strip() if len(row) >= marker_column else ""
    try:
        markers = json.loads(raw_marker) if raw_marker else {}
    except json.JSONDecodeError:
        markers = {}
    if not isinstance(markers, dict):
        markers = {}
    markers[window_key] = sent_at
    marker_a1 = column_to_a1(marker_column)
    history_sheet.update(
        f"{marker_a1}{row_number}",
        [[json.dumps(markers, ensure_ascii=False, separators=(",", ":"))]],
    )


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
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
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
    transaction_audits, accepted_transactions, seen_transaction_ids = [], [], set()
    for ws in sheet.worksheets():
        title_clean = ws.title.strip().lower()
        if "history" in title_clean or "歷史" in title_clean or "紀錄" in title_clean:
            history_sheet = ws
        elif "表單" in title_clean or "form" in title_clean or "回覆" in title_clean or "異動" in title_clean:
            rows = ws.get_all_values()
            if len(rows) > 1:
                if FORM_SCHEMA_STRICT:
                    try:
                        parsed = parse_transaction_rows(
                            rows[0],
                            rows[1:],
                            source_sheet=ws.title,
                            existing_ids=seen_transaction_ids,
                        )
                        seen_transaction_ids.update(item.transaction_id for item in parsed.accepted)
                        seen_transaction_ids.update(item.transaction_id for item in parsed.pending)
                        accepted_transactions.extend(parsed.accepted)
                        transaction_audits.append({"sheet": ws.title, **parsed.audit_payload()})
                        data_rows.extend(parsed.accepted_rows)
                    except TransactionSchemaError as error:
                        if not FORM_SCHEMA_LEGACY_COMPAT:
                            raise
                        transaction_audits.append({
                            "sheet": ws.title,
                            "accepted": 0,
                            "pending": [],
                            "rejected": [{"source_row_id": f"{ws.title}:header", "reason": "legacy_schema_compat", "detail": str(error)}],
                        })
                        # Existing legacy rows are retained only as a temporary
                        # asset snapshot source while the Form is migrated.
                        data_rows.extend(rows[1:])
                else:
                    data_rows.extend(rows[1:])
                
    if transaction_audits:
        write_json(".private-build/transaction_audit.json", {"strict": FORM_SCHEMA_STRICT, "sheets": transaction_audits})
    ledger_sync_result = upload_private_transactions(accepted_transactions)
    if not data_rows: return {}, history_sheet, accepted_transactions, ledger_sync_result
        
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

    return inventory, history_sheet, accepted_transactions, ledger_sync_result

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

# ==========================================
# 4. 主程序與 HTML (Web App) 生成
# ==========================================
def main():
    tw_now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    today_str = tw_now.strftime("%m-%d")
    display_date = tw_now.strftime("%m/%d")
        
    inventory, history_sheet, accepted_transactions, ledger_sync_result = calculate_current_assets()
    validate_inventory(inventory)
    validate_history_sheet(history_sheet)
    history_records = history_sheet.get_all_records()
    etf_nvda_weights = {symbol: get_etf_nvda_weight(symbol, history_records) for symbol in ETF_NVDA_WEIGHT_FALLBACKS}
        
    usd_rate = validate_quote("USD/TWD", get_usd_twd_rate())
    tw_stock_value, us_stock_value_usd, tsmc_exposure_twd, price_006208, leveraged_etf_value = 0, 0, 0, 0, 0
    position_values_twd, tw_position_values, us_position_values = {}, {}, {}
    cash_twd, cash_usd = inventory["現金_TWD"].get("TWD", 0), inventory["現金_USD"].get("USD", 0)
    fund_value = sum(v for k, v in inventory["基金"].items() if k != "History")

    for symbol, shares in inventory["台股"].items():
        if symbol == "History" or shares <= 0: continue
        price = validate_quote(symbol, MARKET_DATA.get_taiwan(symbol, FINMIND_TOKEN).price)
        value = price * shares
        tw_stock_value += value 
        position_values_twd[symbol] = value
        tw_position_values[symbol] = value
        if symbol == '2330': tsmc_exposure_twd += (value * 1.0)
        elif symbol == '006208': tsmc_exposure_twd += (value * 0.594); price_006208 = price
        elif symbol == '00685L': tsmc_exposure_twd += (value * 0.728); leveraged_etf_value = value

    if price_006208 <= 0 and inventory["擔保品"].get("006208", 0) > 0:
        price_006208 = MARKET_DATA.get_taiwan("006208", FINMIND_TOKEN).price

    pledged_value, pledged_006208_value = 0, 0
    for symbol, shares in inventory["擔保品"].items():
        if symbol == "History" or shares <= 0:
            continue
        price = price_006208 if symbol == "006208" and price_006208 > 0 else validate_quote(symbol, MARKET_DATA.get_taiwan(symbol, FINMIND_TOKEN).price)
        value = price * shares
        pledged_value += value
        if symbol == "006208":
            pledged_006208_value += value

    for symbol, shares in inventory["美股"].items():
        if symbol == "History" or shares <= 0: continue
        value = validate_quote(symbol, get_us_stock_price(symbol)) * shares
        us_stock_value_usd += value
        position_values_twd[symbol] = value * usd_rate
        us_position_values[symbol] = value * usd_rate
        if symbol == 'TSM': tsmc_exposure_twd += (value * usd_rate * 1.0)

    us_stock_value_twd = us_stock_value_usd * usd_rate
    total_cash_twd = cash_twd + (cash_usd * usd_rate)
    exposure_matrix = build_exposure_matrix(
        position_values_twd,
        total_asset=tw_stock_value + us_stock_value_twd + total_cash_twd + fund_value,
        etf_lookthrough={symbol: {"NVDA": weight} for symbol, (weight, _) in etf_nvda_weights.items()},
        metadata={
            **{symbol: {"market": "TW", "currency": "TWD", "issuer": symbol} for symbol in tw_position_values},
            **{symbol: {"market": "US", "currency": "USD", "issuer": symbol} for symbol in us_position_values},
        },
    )
    
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
    half_kelly_limit = HALF_KELLY_LIMIT
    beta_capacity = calculate_beta_capacity(effective_leverage, half_kelly_limit)
    beta_status, beta_status_class = classify_beta_capacity(beta_capacity)
    
    debt_ratio = ((total_debt / total_asset) * 100) if total_asset > 0 else 0
    net_asset_pct = ((net_asset / total_asset) * 100) if total_asset > 0 else 0
    maintenance_ratio = calculate_maintenance_ratio(pledged_value, total_debt)
    ratio_status, maintenance_status_class = maintenance_status(total_debt, maintenance_ratio)
    pledge_safety = pledge_safety_center(pledged_value, total_debt, stress_decline=0.10)
    scenario_inputs = {
        "total_asset": total_asset,
        "net_asset": net_asset,
        "total_debt": total_debt,
        "pledged_value": pledged_value,
        "tw_value": tw_stock_value,
        "us_value": us_stock_value_twd,
        "nvda_value": position_values_twd.get("NVDA", 0),
        "tsmc_value": tsmc_exposure_twd,
    }
    scenario_lab = {
        "006208Down10": run_scenario(**scenario_inputs, tw_shock=-0.10),
        "006208Down20": run_scenario(**scenario_inputs, tw_shock=-0.20),
    }

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
    guardrails = composite_guardrails(
        beta_capacity,
        maintenance_ratio,
        largest_position_pct,
        (total_cash_twd / total_asset * 100) if total_asset else 0,
        data_fresh=True,
    )
    # 資產板塊採用可動用台股／質押借款／現貨美股的風險視角。
    # 質押台股此處代表借款金額，而非擔保品的市值。
    spot_tw_value = max(0, tw_stock_value - total_debt)
    pledged_loan_value = total_debt
    other_asset_value = max(
        0,
        total_asset - spot_tw_value - pledged_loan_value - us_stock_value_twd,
    )
    asset_blocks = [
        {"label": "現貨台股", "value": round(spot_tw_value, 2)},
        {"label": "質押台股", "value": round(pledged_loan_value, 2)},
        {"label": "現貨美股", "value": round(us_stock_value_twd, 2)},
        {"label": "現金／基金／其它", "value": round(other_asset_value, 2)},
    ]
    asset_blocks_json = json.dumps(asset_blocks, ensure_ascii=False)

    total_market_value = tw_stock_value + us_stock_value_twd + fund_value
    market_mix_values = {
        "台股市值型 (006208)": tw_position_values.get("006208", 0),
        "美股市值型 (QQQM、QQQ、SPYG、VOO、VTI)": sum(
            us_position_values.get(symbol, 0) for symbol in ("QQQM", "QQQ", "SPYG", "VOO", "VTI")
        ),
        "台積電 (2330、TSM ADR)": tw_position_values.get("2330", 0) + us_position_values.get("TSM", 0),
        "台股槓桿型 (00685L)": tw_position_values.get("00685L", 0),
    }
    market_mix_values["其它"] = max(0, total_market_value - sum(market_mix_values.values()))
    market_mix = [{"label": label, "value": round(value, 2)} for label, value in market_mix_values.items()]
    market_mix_json = json.dumps(market_mix, ensure_ascii=False)

    asset_tree = build_asset_tree(
        tw_position_values,
        us_position_values,
        total_cash_twd,
        inventory["基金"],
    )
    # The legacy HTML renderer is retained as a private-build compatibility
    # artifact only. It must never receive the real asset tree; authenticated
    # clients will obtain private data through the Supabase API in P0-SEC-02.
    asset_tree_json = json.dumps({"label": "總資產", "value": 0, "kind": "root", "children": []}, ensure_ascii=False)
    liabilities_payload = {
        "debt": round(total_debt, 2),
        "interest": round(accumulated_interest, 2),
        "netAsset": round(net_asset, 2),
    }
    liabilities_json = json.dumps(liabilities_payload, ensure_ascii=False)

    try:
        taiex_val = float(yf.Ticker("^TWII").history(period="1d")["Close"].iloc[-1])
    except Exception:
        taiex_val = None
    try:
        nasdaq_val = float(yf.Ticker("^IXIC").history(period="1d")["Close"].iloc[-1])
    except Exception:
        nasdaq_val = None
    taiex_display = f"{taiex_val:,.2f}" if taiex_val is not None else "—"
    nasdaq_display = f"{nasdaq_val:,.2f}" if nasdaq_val is not None else "—"
    benchmark_updated = tw_now.strftime("%Y/%m/%d %H:%M")

    stress_scenarios = build_stress_scenarios(
        asset_006208_value, net_asset, pledged_value, pledged_006208_value, total_debt
    )

    yesterday_net = next((float(str(row.get('Net_Asset', 0)).replace(',', '')) for row in reversed(history_records) if float(str(row.get('Net_Asset', 0)).replace(',', '')) > 0 and str(row.get('Date', ''))[-5:] != today_str), 0)
    daily_diff = net_asset - yesterday_net if yesterday_net else 0
    daily_pct = (daily_diff / yesterday_net * 100) if yesterday_net else 0
    today_transactions = [item for item in accepted_transactions if item.transaction_date == tw_now.date()]
    performance = performance_breakdown(net_asset, yesterday_net if yesterday_net else net_asset, today_transactions)
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
    attribution = build_pnl_attribution(
        yesterday_net,
        net_asset,
        previous_categories or {},
        category_values,
        income=performance["income"],
        expenses=performance["expenses"],
        financing_cash_flow=performance["financingCashFlow"],
        external_cash_flow=performance["externalCashFlow"],
    )
    alert_engine = AlertEngine()
    alerts = alert_engine.evaluate({
        "maintenanceRatio": maintenance_ratio,
        "stressMaintenanceRatio": min((item["maintenance"] for item in stress_scenarios if item["maintenance"] is not None), default=999),
        "maxCompanyExposure": largest_position_pct,
        "cashMonths": 999,
        "isStale": False,
        "reconciled": attribution["reconciled"],
    })

    def inline_daily_change(key):
        change = category_daily_changes[key]
        if change is None:
            return '<span class="daily-inline price-flat">🟰 日變動待累積</span>'
        if change["percent"] > 1:
            marker, color = "📈", "price-up"
        elif change["percent"] < -1:
            marker, color = "📉", "price-down"
        else:
            marker, color = "🟰", "price-flat"
        return f'<span class="daily-inline {color}">{marker} {change["percent"]:+.2f}% · ${change["amount"]:+,.0f}</span>'

    snapshot_result = "skipped"
    if total_asset > 0:
        ensure_history_columns(history_sheet, HISTORY_EXTRA_COLUMNS)
        snapshot_values = {
            "Date": tw_now.strftime("%Y-%m-%d"),
            "Total_Asset": round(total_asset, 2),
            "Net_Asset": round(net_asset, 2),
            "Total_Debt": round(total_debt, 2),
            "TSMC_Exposure": round(tsmc_exposure_twd, 2),
            "TW_Stock_Value": round(tw_stock_value, 2),
            "US_Stock_Value": round(us_stock_value_twd, 2),
            "Cash_Value": round(total_cash_twd, 2),
            "Fund_Value": round(fund_value, 2),
            "NVDA_QQQM_Weight": round(etf_nvda_weights["QQQM"][0], 6),
            "NVDA_SPYG_Weight": round(etf_nvda_weights["SPYG"][0], 6),
            "NVDA_VOO_Weight": round(etf_nvda_weights["VOO"][0], 6),
        }
        snapshot_result = upsert_history_snapshot(history_sheet, snapshot_values)

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
    performance_metrics = summarize_performance(all_nets)
    runtime_extensions = build_runtime_extensions(
        net_values=all_nets,
        net_asset=net_asset,
        total_asset=total_asset,
        total_debt=total_debt,
        pledged_value=pledged_value,
        data_as_of=tw_now.isoformat(),
        sources={
            "googleSheet": {"quality": "fresh" if inventory else "unavailable", "source": "Google Sheets"},
            "marketQuotes": {"quality": "fresh" if total_asset > 0 else "unavailable", "source": "market providers"},
        },
        reconciled=bool(performance.get("reconciled", True)),
        now=tw_now,
    )
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
        if not sorted_dates:
            return "資料累積中"
        target = tw_now.date() - datetime.timedelta(days=days)
        closest, min_diff = None, 9999
        for d in sorted_dates:
            diff = abs((datetime.datetime.strptime(d, "%Y-%m-%d").date() - target).days)
            if diff < min_diff: min_diff, closest = diff, d
        if closest and min_diff <= max(7, days * 0.2):
            rate = ((net_asset - daily_net_history[closest]) / daily_net_history[closest]) * 100
            return f"{'+' if rate>=0 else ''}{rate:.1f}%(實)"
        return "資料累積中"

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
            .eyebrow {{ color:var(--muted); font-size:11px; letter-spacing:.14em; text-transform:uppercase; margin:0 0 8px; }}
            .hero {{ position:relative; overflow:hidden; background:var(--navy); border:1px solid #1d3850; border-radius:22px; padding:26px; margin-bottom:14px; color:#f8f6ef; box-shadow:0 10px 24px rgba(36,66,94,.13); }}
            .hero::after {{ content:''; position:absolute; width:210px; height:210px; border:1px solid rgba(255,255,255,.36); border-radius:50%; right:-70px; top:-112px; box-shadow:0 0 0 34px rgba(255,255,255,.055); pointer-events:none; }}
            .hero .eyebrow,.hero .metric-label {{ color:#ccd7dc; }} .hero .metric-value {{ color:#fffdf7; }}
            .hero-top {{ position:relative; z-index:1; display:flex; align-items:end; justify-content:space-between; gap:16px; }}
            .hero-value {{ font-family:'Noto Serif TC', serif; font-size:clamp(34px, 6vw, 52px); line-height:1; letter-spacing:-.03em; }}
            .change,.sync {{ display:flex; align-items:center; flex:0 0 auto; width:fit-content; white-space:nowrap; border:1px solid rgba(255,255,255,.16); border-radius:12px; padding:11px 14px; box-shadow:0 4px 10px rgba(10,24,40,.12); }}
            .change {{ color:{'#91b29d' if daily_diff < 0 else '#ef9a83'}; font-size:clamp(11px, 2vw, 14px); font-weight:700; background:#35536d; }}
            .hero-status-row {{ position:relative; z-index:1; display:flex; align-items:center; gap:10px; border-bottom:1px solid rgba(255,255,255,.22); padding:13px 0 18px; margin-bottom:17px; }}
            .sync {{ justify-content:center; color:#d7e2e1; font-size:clamp(11px, 1.8vw, 12px); font-weight:700; background:#2e4b65; margin-left:auto; }}
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
            .risk-good {{ color:#5e806d !important; }} .risk-watch {{ color:#b78435 !important; }} .risk-alert {{ color:#b84f45 !important; }}
            .timeline ul {{ padding-left:18px; margin:10px 0 0; font-size:13px; color:#514f49; line-height:1.9; }}
            .goal-track {{ height:6px; background:#e5e2db; margin:12px 0 8px; }} .goal-fill {{ height:100%; background:var(--sage); width:{min(progress_pct, 100):.1f}%; }}
            .actions {{ display:grid; grid-template-columns:1fr 1fr; gap:10px; margin:16px 0 30px; }}
            .btn {{ display:block; text-align:center; background:#45443f; color:white; text-decoration:none; padding:14px; font-size:13px; font-weight:500; letter-spacing:.05em; border:1px solid #45443f; transition:background .2s; }}
            .btn:hover {{ background:#30302c; }} .btn-alt {{ background:transparent; color:var(--ink); border-color:var(--line); }} .btn-alt:hover {{ background:#ece9e1; }}
            .chart-container {{ position:relative; width:100%; height:280px; margin-bottom:20px; }}
            .card.market-mix-card {{ background:#f4f2ed; border:1px solid #e5e2db; border-top:3px solid var(--orange); }}
            .asset-treemap-toolbar {{ display:flex; align-items:center; justify-content:space-between; gap:10px; margin:0 0 10px; }}
            .asset-treemap-breadcrumb {{ color:var(--muted); font-size:11px; letter-spacing:.04em; }}
            .asset-treemap-back {{ appearance:none; border:1px solid var(--line); border-radius:999px; background:var(--surface); color:var(--navy); cursor:pointer; font:inherit; font-size:11px; padding:6px 10px; }}
            .asset-treemap-back:disabled {{ cursor:default; opacity:.42; }}
            .asset-treemap {{ position:relative; width:100%; min-height:390px; overflow:hidden; border:1px solid #e0ddd5; border-radius:14px; background:#ece9e1; }}
            .asset-treemap-node {{ position:absolute; display:flex; flex-direction:column; align-items:flex-start; justify-content:flex-start; overflow:hidden; box-sizing:border-box; transition:left .22s ease, top .22s ease, width .22s ease, height .22s ease, filter .18s ease; }}
            .asset-treemap-node.is-group {{ padding:10px; border:3px solid #f4f2ed; border-radius:12px; color:#fffdf7; cursor:pointer; box-shadow:inset 0 0 0 1px rgba(255,255,255,.12); }}
            .asset-treemap-node.is-group:hover,.asset-treemap-node.is-leaf:hover {{ filter:brightness(1.08); }}
            .asset-treemap-node.is-leaf {{ padding:10px; border:2px solid rgba(244,242,237,.8); border-radius:8px; background:rgba(251,250,247,.1); color:#fffdf7; cursor:pointer; }}
            .asset-treemap-node.is-compact {{ padding:7px; }}
            .asset-treemap-node.is-tiny {{ padding:5px; }}
            .asset-treemap-node.is-micro {{ padding:4px; }}
            .asset-treemap-node.is-micro .asset-treemap-value {{ display:none; }}
            .asset-treemap-node.is-micro .asset-treemap-percent {{ font-size:10px; }}
            .asset-treemap-node.is-group > .asset-treemap-title {{ font-weight:700; font-size:14px; line-height:1.25; }}
            .asset-treemap-node.is-leaf > .asset-treemap-title {{ font-weight:700; font-size:13px; line-height:1.25; }}
            .asset-treemap-title {{ display:-webkit-box; width:100%; overflow:hidden; text-overflow:ellipsis; white-space:normal; word-break:break-word; -webkit-line-clamp:2; -webkit-box-orient:vertical; text-shadow:0 1px 2px rgba(36,66,94,.45); }}
            .asset-treemap-value {{ display:block; width:100%; margin-top:7px; color:#f7d49e; font-family:'Noto Sans TC', sans-serif; font-size:13px; font-weight:700; line-height:1.25; white-space:normal; word-break:break-all; }}
            .asset-treemap-percent {{ display:block; width:100%; margin-top:3px; color:#d8ebe2; font-size:12px; font-weight:700; line-height:1.2; white-space:nowrap; }}
            .asset-treemap-note {{ display:flex; flex-wrap:wrap; gap:8px 14px; margin-top:10px; color:var(--muted); font-size:11px; line-height:1.45; }}
            .asset-treemap-note strong {{ color:var(--ink); }}
            .asset-treemap-hint {{ margin-top:8px; color:var(--muted); font-size:11px; }}
            .market-mix-layout {{ display:block; }}
            .market-donut-wrap {{ position:relative; width:min(100%, 420px); aspect-ratio:1; margin:auto; }}
            .market-donut-wrap canvas {{ position:relative; z-index:1; }}
            .market-donut-center {{ position:absolute; inset:22%; z-index:2; display:flex; flex-direction:column; align-items:center; justify-content:center; text-align:center; pointer-events:none; color:var(--muted); background:rgba(251,250,247,.96); border:1px solid #e5e2db; border-radius:50%; padding:6px; box-shadow:0 2px 8px rgba(36,66,94,.06); }}
            .market-donut-center span {{ font-size:11px; letter-spacing:.08em; }}
            .market-donut-center strong {{ color:var(--ink); font-family:'Noto Serif TC', serif; font-size:clamp(20px, 4.2vw, 27px); line-height:1.15; margin:4px 0; letter-spacing:-.04em; white-space:nowrap; }}
            .market-donut-center small {{ font-size:9px; line-height:1.4; white-space:nowrap; }}
            .market-chart-tooltip {{ position:absolute; z-index:4; min-width:176px; max-width:228px; transform:translate(-50%,-50%); padding:8px 10px; border:2px solid var(--orange); border-radius:8px; background:#1b3248; color:#fffdf7; box-shadow:0 8px 18px rgba(27,50,72,.3); pointer-events:none; font-size:11px; line-height:1.4; text-align:left; }}
            .market-chart-tooltip[hidden] {{ display:none; }}
            .market-chart-tooltip strong,.market-chart-tooltip span,.market-chart-tooltip b {{ display:block; }}
            .market-chart-tooltip strong {{ color:#f6cf9a; font-size:11px; }}
            .market-chart-tooltip span {{ margin-top:2px; }}
            .market-chart-tooltip b {{ margin-top:4px; color:#fffdf7; font-size:12px; }}
            .market-donut-labels {{ position:absolute; inset:0; z-index:3; pointer-events:none; overflow:hidden; }}
            .market-donut-label {{ position:absolute; transform:translate(-50%,-50%); color:#fffdf7; font-size:11px; font-weight:700; line-height:1; letter-spacing:-.03em; text-shadow:0 1px 2px rgba(36,66,94,.8); white-space:nowrap; }}
            .market-donut-label.is-inner {{ font-size:10px; }}
            .market-donut-label.is-muted {{ color:#24425e; text-shadow:0 1px 2px rgba(255,255,255,.8); }}
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
            .risk-section {{ background:#f4f2ed; border:1px solid #e5e2db; border-radius:16px; padding:15px; }} .risk-section + .risk-section {{ margin-top:12px; }}
            .risk-pair,.exposure-pair {{ display:grid; grid-template-columns:1fr 1fr; gap:10px; }}
            .risk-column,.exposure-row {{ background:#f8faf7; border:1px solid #d8dfd8; border-radius:12px; padding:15px 13px; min-width:0; }} .risk-column strong,.exposure-row strong {{ display:block; color:var(--navy); font-size:clamp(22px, 5.3vw, 26px); line-height:1.15; margin-top:7px; letter-spacing:-.02em; }} .risk-column small,.exposure-row small {{ display:block; margin-top:6px; color:var(--muted); font-size:12px; line-height:1.45; }}
            .risk-detail {{ border-top:1px solid #d5ddd5; margin-top:12px; padding-top:10px; }} .risk-detail-label {{ display:block; color:var(--muted); font-size:11px; }} .risk-detail-value {{ display:block; margin-top:4px; color:var(--ink); font-size:13px; font-weight:700; line-height:1.45; }} .risk-detail .status,.risk-detail .capacity {{ display:block; margin-top:4px; font-size:12px; font-weight:700; line-height:1.35; white-space:normal; }}
            .risk-section .sec-title {{ margin-bottom:11px !important; }}
            @media (max-width:540px) {{ body {{ padding:22px 14px 34px; }} .header-wrapper {{ align-items:flex-start; gap:10px; }} .hero, .card {{ padding:17px; }} .hero-top {{ align-items:flex-start; flex-direction:column; gap:10px; }} .hero-status-row {{ gap:7px; }} .change,.sync {{ padding:9px 8px; font-size:10px; }} .metric-grid {{ gap:8px; }} .metric-value {{ font-size:15px; }} .grid-2, .stress-grid, .risk-pair, .exposure-pair {{ gap:8px; }} .risk-section {{ padding:13px; }} .risk-column,.exposure-row {{ padding:14px 12px; }} .block-grid {{ grid-template-columns:1fr 1fr; gap:8px; }} .box {{ padding:11px; }} .actions {{ grid-template-columns:1fr; }} .chart-hint {{ width:100%; margin-left:0; }} .asset-treemap {{ min-height:360px; }} .asset-treemap-node.is-group,.asset-treemap-node.is-leaf {{ padding:7px; }} .asset-treemap-node.is-group > .asset-treemap-title,.asset-treemap-node.is-leaf > .asset-treemap-title {{ font-size:12px; }} .asset-treemap-value {{ font-size:11px; }} .asset-treemap-percent {{ font-size:11px; }} .market-chart-tooltip {{ min-width:150px; max-width:190px; padding:7px 8px; font-size:10px; }} .market-chart-tooltip b {{ font-size:11px; }} }}
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
        </div>

        <section class="hero">
            <p class="eyebrow">Portfolio overview</p>
            <div class="hero-top">
                <div><div class="metric-label">淨資產 Net</div><div class="hero-value">${net_asset:,.0f}</div></div>
            </div>
            <div class="hero-status-row">
                <div class="change">今日 {sign}{daily_pct:.1f}% &nbsp;·&nbsp; {sign}${daily_diff:,.0f}</div>
                <div class="sync">資料同步 · {tw_now.strftime('%m/%d %H:%M')}</div>
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

        <div class="card market-mix-card">
            <div class="chart-title">總資產配置</div>
            <div class="chart-caption">依市值比例呈現資產階層；點擊分類可查看個別標的，負債不納入資產面積。</div>
            <div class="asset-treemap-toolbar">
                <span id="assetTreemapBreadcrumb" class="asset-treemap-breadcrumb">總資產</span>
                <button id="assetTreemapBack" class="asset-treemap-back" type="button" disabled>← 返回上一層</button>
            </div>
            <div id="assetTreemap" class="asset-treemap" role="img" aria-label="總資產配置 Treemap"></div>
            <div id="assetTreemapHint" class="asset-treemap-hint">點擊色塊查看下一層；每個色塊顯示市值與占比。</div>
            <div class="asset-treemap-note">
                <span><strong>淨資產</strong> NT${net_asset:,.0f}（{net_asset_pct:.1f}%）</span>
                <span><strong>質押借款</strong> NT${total_debt:,.0f}（含利息，{debt_ratio:.1f}%）</span>
                <span><strong>更新</strong> {benchmark_updated}</span>
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
                    <div class="risk-column"><span class="metric-label">有效Beta</span><strong>{effective_leverage:.2f} ×</strong><div class="risk-detail"><span class="risk-detail-label">凱利安全邊界</span><span class="risk-detail-value">{half_kelly_limit:.2f} 倍</span><span class="status {beta_status_class}">{beta_status}</span><span class="capacity {beta_status_class}">容量: {beta_capacity:.1f}%</span><span class="capacity {'risk-good' if guardrails['eligible'] else 'risk-alert'}">Guardrail：{guardrails['recommendation']}</span></div></div>
                    <div class="risk-column"><span class="metric-label">質押借款</span><strong class="risk-alert">${total_debt:,.0f}</strong><small>含利息 ${accumulated_interest:,.0f}</small><div class="risk-detail"><span class="risk-detail-label">質押維持率</span><span class="risk-detail-value {maintenance_status_class}">{maintenance_ratio:.1f}%</span><span class="status {maintenance_status_class}">{ratio_status}</span></div></div>
                </div>
            </div>
            <div class="risk-section">
                <div class="sec-title" style="margin-bottom:10px;">曝險 <span class="sec-note">Look-through concentration</span></div>
                <div class="exposure-pair">
                    <div class="exposure-row"><span class="metric-label">TSMC 曝險</span><strong>{tsmc_pct:.1f}%</strong><small>台美股 &amp; ETF 綜合曝險</small></div>
                    <div class="exposure-row"><span class="metric-label">NVDA 曝險</span><strong>{nvda_pct:.1f}%</strong><small>純美股 &amp; ETF 綜合曝險</small></div>
                </div>
            </div>
        </div>

        <div class="card">
            <div class="sec-title">集中度與壓力測試</div>
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
        <footer class="footer">@2026 PRStK Lab &amp; SFC.e. | All right reserved.</footer>

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

                if (document.getElementById('marketMixChart')) {{
                    try {{
                    const assetBlocks = {asset_blocks_json};
                    const marketMix = {market_mix_json};
                    const marketMixLabels = {{
                        id: 'marketMixLabels',
                        afterDatasetsDraw: (chart) => {{
                            const layer = chart.canvas.parentNode.querySelector('.market-donut-labels');
                            if (!layer) return;
                            layer.innerHTML = '';
                            chart.data.datasets.forEach((dataset, datasetIndex) => {{
                                const meta = chart.getDatasetMeta(datasetIndex);
                                const total = dataset.data.reduce((sum, item) => sum + Number(item || 0), 0);
                                if (!meta || !meta.data || total <= 0) return;
                                meta.data.forEach((arc, index) => {{
                                    const value = Number(dataset.data[index] || 0);
                                    if (!arc || value <= 0) return;
                                    const angle = (arc.startAngle + arc.endAngle) / 2;
                                    const labelRatio = 0.72;
                                    const radius = arc.innerRadius + (arc.outerRadius - arc.innerRadius) * labelRatio;
                                    const x = arc.x + Math.cos(angle) * radius;
                                    const y = arc.y + Math.sin(angle) * radius;
                                    const percent = (value * 100 / total).toFixed(0) + '%';
                                    const label = document.createElement('span');
                                    label.className = 'market-donut-label ' + (datasetIndex === 0 ? 'is-inner' : '') + ((datasetIndex === 0 && index === 3) ? ' is-muted' : '');
                                    label.textContent = percent;
                                    label.style.left = (x / chart.width * 100) + '%';
                                    label.style.top = (y / chart.height * 100) + '%';
                                    layer.appendChild(label);
                                }});
                            }});
                        }}
                    }};
                    const marketMixCtx = document.getElementById('marketMixChart').getContext('2d');
                    new Chart(marketMixCtx, {{
                        plugins: [marketMixLabels],
                        type: 'doughnut',
                        data: {{
                            labels: [
                                ...assetBlocks.map(item => item.label),
                                ...marketMix.map(item => item.label),
                            ],
                            datasets: [{{
                                label: '總資產板塊（內圈）',
                                labels: assetBlocks.map(item => item.label),
                                data: assetBlocks.map(item => item.value),
                                backgroundColor: ['#24425e', '#687c70', '#3d6f9f', '#c8c1b5'],
                                borderColor: '#f4f2ed', borderWidth: 5,
                                radius: '64%', cutout: '16%', hoverOffset: 6,
                                hoverBorderColor: '#fffdf7', hoverBorderWidth: 3,
                            }}, {{
                                label: '總市值組成（外圈）',
                                labels: marketMix.map(item => item.label),
                                data: marketMix.map(item => item.value),
                                backgroundColor: ['#24425e', '#3d6f9f', '#c4674f', '#687c70', '#c98a4b'],
                                borderColor: '#f4f2ed', borderWidth: 5,
                                radius: '100%', cutout: '52%', hoverOffset: 7,
                                hoverBorderColor: '#fffdf7', hoverBorderWidth: 3,
                            }}]
                        }},
                        options: {{
                            responsive: true, maintainAspectRatio: false,
                            interaction: {{ mode: 'nearest', intersect: true }},
                            plugins: {{
                                legend: {{ display: false }},
                                tooltip: {{
                                    enabled: false,
                                    external: (context) => {{
                                        const chart = context.chart;
                                        const tooltip = context.tooltip;
                                        const panel = chart.canvas.parentNode.querySelector('.market-chart-tooltip');
                                        if (!panel) return;
                                        if (tooltip.opacity === 0 || !tooltip.dataPoints || !tooltip.dataPoints.length) {{
                                            panel.hidden = true;
                                            return;
                                        }}
                                        const point = tooltip.dataPoints[0];
                                        const dataset = point.dataset;
                                        const value = Number(point.raw || 0);
                                        const total = dataset.data.reduce((sum, item) => sum + Number(item), 0);
                                        const percent = total > 0 ? (value * 100 / total).toFixed(1) : '0.0';
                                        const label = (dataset.labels && dataset.labels[point.dataIndex]) || '';
                                        panel.innerHTML = '<strong>' + dataset.label + '</strong>'
                                            + '<span>' + label + '</span>'
                                            + '<b>' + value.toLocaleString('zh-TW') + ' TWD · ' + percent + '%</b>';
                                        const centerX = chart.width / 2;
                                        const centerY = chart.height / 2;
                                        const dx = tooltip.caretX - centerX;
                                        const dy = tooltip.caretY - centerY;
                                        const distance = Math.sqrt(dx * dx + dy * dy) || 1;
                                        const outward = 72;
                                        const targetX = tooltip.caretX + (dx / distance) * outward;
                                        const targetY = tooltip.caretY + (dy / distance) * outward;
                                        const minX = panel.offsetWidth / 2 + 8;
                                        const maxX = Math.max(minX, chart.width - panel.offsetWidth / 2 - 8);
                                        const x = Math.min(Math.max(targetX, minX), maxX);
                                        const y = Math.min(Math.max(targetY, 48), chart.height - 48);
                                        panel.style.left = x + 'px';
                                        panel.style.top = y + 'px';
                                        panel.hidden = false;
                                    }}
                                }},
                                datalabels: {{ display: false }}
                            }}
                        }}
                    }});
                }} catch (error) {{
                    console.error("雙層圓環圖繪製失敗:", error);
                }}
                }}
            }});
                try {{
                    const assetTree = {asset_tree_json};
                    const assetTreemap = document.getElementById('assetTreemap');
                    const assetTreemapBreadcrumb = document.getElementById('assetTreemapBreadcrumb');
                    const assetTreemapBack = document.getElementById('assetTreemapBack');
                    const assetTreeRoot = assetTree;
                    let assetTreePath = [assetTreeRoot];
                    const palette = {{
                        '\u73fe\u8ca8\u53f0\u80a1': ['#24425e', '#315b7b', '#477091', '#6286a0', '#7d99ad'],
                        '\u73fe\u8ca8\u7f8e\u80a1': ['#356b9d', '#447eae', '#5b8fba', '#729fc3', '#89aecb'],
                        '\u73fe\u91d1\u8207\u57fa\u91d1': ['#5f7569', '#708a7c', '#819d8d', '#94ad9c', '#a9bcaa'],
                        '\u53f0\u80a1\u5e02\u503c\u578b': ['#315b7b', '#416d8b', '#527f9a', '#668fa7'],
                        '\u53f0\u7a4d\u96fb': ['#9f5f54', '#b06b5d', '#bf7968', '#ce8874'],
                        '\u53f0\u80a1\u69d3\u687f\u578b': ['#ad743d', '#bd8248', '#ca9155', '#d49f65'],
                        '\u5176\u5b83\u53f0\u80a1': ['#627887', '#728898', '#8298a7', '#93a8b5'],
                        '\u7f8e\u80a1\u5e02\u503c\u578b': ['#356b9d', '#477eab', '#598fb7', '#6ba0c2'],
                        '\u53f0\u7a4d\u96fb ADR': ['#9f5f54', '#b06b5d', '#bf7968', '#ce8874'],
                        '\u5176\u5b83\u7f8e\u80a1': ['#6f879d', '#7f97ac', '#8fa7b9', '#9fb6c5'],
                        '\u73fe\u91d1': ['#5f7569', '#708a7c', '#819d8d'],
                        '\u57fa\u91d1': ['#a77d4d', '#b58a5b', '#c19769']
                    }};
                    const formatMoney = (value) => 'NT$' + Math.round(Number(value || 0)).toLocaleString('zh-TW');
                    const percentOfRoot = (node) => assetTreeRoot.value > 0 ? (Number(node.value || 0) * 100 / assetTreeRoot.value).toFixed(1) : '0.0';
                    const colorIndex = (label, length) => {{
                        const text = String(label || '');
                        let hash = 0;
                        for (let index = 0; index < text.length; index += 1) hash = ((hash << 5) - hash + text.charCodeAt(index)) | 0;
                        return Math.abs(hash) % length;
                    }};
                    const nodeColor = (node) => {{
                        const shades = palette[node.label] || palette[node.category] || ['#7e8a86'];
                        return shades[node.children && node.children.length ? 0 : colorIndex(node.label, shades.length)];
                    }};
                    const visibleChildren = (node) => (node.children || []).filter((child) => Number(child.value || 0) > 0);
                    const layoutNodes = (nodes, x, y, width, height) => {{
                        const sorted = nodes.slice().sort((a, b) => Number(b.value || 0) - Number(a.value || 0));
                        if (!sorted.length) return [];
                        if (sorted.length === 1) return [{{ node: sorted[0], x, y, width, height }}];
                        const total = sorted.reduce((sum, node) => sum + Number(node.value || 0), 0) || 1;
                        const first = [];
                        let firstTotal = 0;
                        for (let index = 0; index < sorted.length - 1; index += 1) {{
                            first.push(sorted[index]);
                            firstTotal += Number(sorted[index].value || 0);
                            if (firstTotal >= total / 2) break;
                        }}
                        const second = sorted.slice(first.length);
                        const ratio = firstTotal / total;
                        if (width >= height) {{
                            const firstWidth = width * ratio;
                            return layoutNodes(first, x, y, firstWidth, height).concat(layoutNodes(second, x + firstWidth, y, width - firstWidth, height));
                        }}
                        const firstHeight = height * ratio;
                        return layoutNodes(first, x, y, width, firstHeight).concat(layoutNodes(second, x, y + firstHeight, width, height - firstHeight));
                    }};
                    const appendText = (element, className, text) => {{
                        const child = document.createElement('span');
                        child.className = className;
                        child.textContent = text;
                        element.appendChild(child);
                    }};
                    const renderTreemapNode = (item, parent) => {{
                        const node = item.node;
                        const children = visibleChildren(node);
                        const element = document.createElement('div');
                        element.className = 'asset-treemap-node ' + (children.length ? 'is-group' : 'is-leaf');
                        if (item.width < 28 || item.height < 28) element.classList.add('is-compact');
                        if (item.width < 17 || item.height < 18) element.classList.add('is-tiny');
                        if (item.width < 11 || item.height < 13) element.classList.add('is-micro');
                        element.style.left = item.x + '%';
                        element.style.top = item.y + '%';
                        element.style.width = item.width + '%';
                        element.style.height = item.height + '%';
                        element.style.backgroundColor = nodeColor(node);
                        element.setAttribute('role', children.length ? 'button' : 'img');
                        element.setAttribute('tabindex', '0');
                        element.setAttribute('aria-label', node.label + ' ' + formatMoney(node.value) + '，占總資產 ' + percentOfRoot(node) + '%');
                        appendText(element, 'asset-treemap-title', node.label);
                        appendText(element, 'asset-treemap-value', formatMoney(node.value));
                        appendText(element, 'asset-treemap-percent', percentOfRoot(node) + '%');
                        if (children.length) {{
                            const openNode = () => {{ assetTreePath.push(node); renderTreemap(); }};
                            element.addEventListener('click', openNode);
                            element.addEventListener('keydown', (event) => {{ if (event.key === 'Enter' || event.key === ' ') {{ event.preventDefault(); openNode(); }} }});
                        }}
                        parent.appendChild(element);
                    }};
                    const renderTreemap = () => {{
                        const currentNode = assetTreePath[assetTreePath.length - 1];
                        const children = visibleChildren(currentNode);
                        assetTreemap.innerHTML = '';
                        assetTreemapBreadcrumb.textContent = assetTreePath.map((node) => node.label).join(' / ');
                        assetTreemapBack.disabled = assetTreePath.length === 1;
                        if (!children.length) {{
                            assetTreemap.textContent = '目前沒有可視化資產資料';
                            return;
                        }}
                        layoutNodes(children, 0, 0, 100, 100).forEach((item) => renderTreemapNode(item, assetTreemap));
                    }};
                    assetTreemapBack.addEventListener('click', () => {{ if (assetTreePath.length > 1) {{ assetTreePath.pop(); renderTreemap(); }} }});
                    renderTreemap();
                }} catch (error) {{
                    console.error("資產配置 Treemap 載入失敗:", error);
                }}
        </script>
    </body>
    </html>
    """

    # Keep the legacy real-value page outside the Pages publish directory. It
    # is a local/private compatibility artifact and is not deployed.
    os.makedirs('.private-build', exist_ok=True)
    with open('.private-build/index.private.html', 'w', encoding='utf-8') as f:
        f.write(html_content)

    # === [關鍵補丁]：產生網頁專用即時數據 ===
    if not os.path.exists('public'):
        os.makedirs('public')
    
    try:
        # 200MA is a separate context metric; preserve null when unavailable.
        ma200_val = float(yf.Ticker("^TWII").history(period="200d")["Close"].mean())
    except Exception:
        ma200_val = None

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

    risk_level = "attention" if (not guardrails["eligible"] or (maintenance_ratio and maintenance_ratio < 150) or largest_position_pct >= 35) else "watch" if (debt_ratio >= 25 or tsmc_pct >= 35 or largest_position_pct >= 20) else "stable"
    risk_summary = {
        "level": risk_level,
        "debtRatio": round(debt_ratio, 1),
        "maintenanceRatio": round(maintenance_ratio, 1),
        "tsmcExposureRatio": round(tsmc_pct, 1),
        "effectiveLeverage": round(effective_leverage, 2),
        "largestPosition": {"symbol": largest_symbol, "value": round(largest_position_value, 2), "percent": round(largest_position_pct, 1), "status": largest_position_status},
        "nvdaExposureRatio": round(nvda_pct, 1),
        "guardrails": guardrails,
    }

    data_for_web = {
        "taiex": round(taiex_val, 2) if taiex_val is not None else None,
        "nasdaq": round(nasdaq_val, 2) if nasdaq_val is not None else None,
        "ma200": round(ma200_val, 2) if ma200_val is not None else None,
        "vix": round(vix_val, 2),
        "peak_006208": round(peak_006208, 2),
        "asset_006208": round(price_006208, 2) if price_006208 else 249.1,
        "lastUpdated": tw_now.strftime("%Y/%m/%d %H:%M:%S"),
        "portfolio": {
            "totalAsset": round(total_asset, 2),
            "netAsset": round(net_asset, 2),
            "totalDebt": round(total_debt, 2),
            "totalMarketValue": round(total_market_value, 2),
            "assetBlocks": asset_blocks,
            "marketMix": market_mix,
            "assetTree": asset_tree,
            "liabilities": liabilities_payload,
            "allocation": allocation_items,
            "risk": risk_summary,
            "stressTests": stress_scenarios,
            "categoryDailyChanges": category_daily_changes,
            "performance": performance,
            "performanceMetrics": performance_metrics,
            "pnlAttribution": attribution,
            "exposureMatrix": exposure_matrix,
            "pledgeSafety": pledge_safety,
            "alerts": alerts,
            "scenarioLab": scenario_lab,
            "runtimeExtensions": runtime_extensions,
            "nvdaExposure": {"value": round(nvda_exposure_twd, 2), "percent": round(nvda_pct, 1), "etfWeights": {symbol: {"weight": round(weight * 100, 2), "source": source} for symbol, (weight, source) in etf_nvda_weights.items()}},
        },
    }

    data_status = "ok" if total_asset > 0 and net_asset > 0 else "degraded"
    status_payload = {
        "status": data_status,
        "generatedAt": tw_now.isoformat(),
        "snapshotResult": snapshot_result,
        "portfolioValueAvailable": total_asset > 0,
        "freshness": {
            "expectedCadenceHours": 12,
            "staleAfterHours": 18,
            "timezone": "Asia/Taipei",
        },
        "sources": {
            "googleSheet": "ok" if inventory else "unavailable",
            "marketQuotes": "ok" if total_asset > 0 else "unavailable",
        },
    }
    data_for_web["status"] = data_status
    data_for_web["generatedAt"] = tw_now.isoformat()
    data_for_web["snapshotResult"] = snapshot_result
    data_for_web["ledgerSyncResult"] = ledger_sync_result
    data_for_web["dataQuality"] = status_payload
    # Private data is retained only in the ignored build directory. GitHub
    # Pages receives a separate fixed Demo contract and never sees this
    # payload. Supabase Auth + RLS will replace this local handoff in P0-SEC-02.
    write_json('.private-build/data.private.json', data_for_web)
    write_json('.private-build/status.private.json', status_payload)
    upload_private_snapshot('.private-build/data.private.json')
    write_public_site('public-site', tw_now.isoformat())
    # =================================

    # --- 判斷每日損益，動態生成推播文字 ---
    if daily_diff >= 0:
        msg_body = f"🚀 厲害的阿洲，今天賺了 {int(daily_diff):,} 元 (+{daily_pct:.1f}%)"
    else:
        # daily_pct 本身就是負數，所以直接顯示即可
        msg_body = f"💸 可憐的阿洲，今天賠了 {abs(int(daily_diff)):,} 元 ({daily_pct:.1f}%)"

    tg_text = f"✅ {display_date} 結算完畢！\n{msg_body}"

    # --- 傳送 Telegram 訊息 ---
    performance_message = (
        f"\n市場損益 {performance['marketPnl']:+,.0f} 元"
        f"\n外部現金流 {performance['externalCashFlow']:+,.0f} 元"
        f"\n融資現金流 {performance['financingCashFlow']:+,.0f} 元"
    )
    tg_text = f"{tg_text}{performance_message}"

    keyboard = {
        "inline_keyboard": [
            [{"text": "🌱 開啟Growth儀表板", "web_app": {"url": WEB_APP_URL}}],
        ]
    }
    
    # Send once in each settlement window. The History marker is keyed by
    # window so the US morning and Taiwan afternoon notifications can both be
    # delivered while remaining idempotent across Cron retries. Manual force
    # mode intentionally bypasses both the time window and the dedupe marker.
    snapshot_date = tw_now.strftime("%Y-%m-%d")
    if FORCE_TELEGRAM:
        settlement_window = "manual"
    elif 5 <= tw_now.hour < 7:
        settlement_window = "us"
    elif 14 <= tw_now.hour < 17:
        settlement_window = "tw"
    else:
        settlement_window = None
    notification_already_sent = (
        settlement_notification_sent(history_sheet, snapshot_date, settlement_window)
        if settlement_window and not FORCE_TELEGRAM
        else False
    )
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID and settlement_window and not notification_already_sent:
        try:
            response = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": tg_text,
                "parse_mode": "Markdown",
                "reply_markup": keyboard,
            }, timeout=10)
            response.raise_for_status()
            mark_settlement_notification_sent(history_sheet, snapshot_date, settlement_window, tw_now.isoformat())
        except requests.RequestException as error:
            print(f"Telegram notification failed: {error}")
    else:
        print(
            "Telegram notification skipped; "
            f"window={settlement_window}, alreadySent={notification_already_sent}, snapshotResult={snapshot_result}"
        )

if __name__ == "__main__":
    main()
