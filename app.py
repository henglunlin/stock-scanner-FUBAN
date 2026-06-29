import re
import os
import json
import copy
import time
import gc
import requests
import base64
from html import escape
from io import BytesIO
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

# ===== yfinance 選用資料源 =====
try:
    import yfinance as yf
except ImportError:
    yf = None

# ===== 富邦 API 引入 =====
try:
    from fubon_neo.sdk import FubonSDK, Mode
except ImportError:
    st.error("請先安裝富邦 API 套件：執行 `pip install fubon-neo`")
    st.stop()

# ===== Streamlit UI 基本設定（一定要放最前面）=====
st.set_page_config(layout="wide")

# ===== 常數設定 =====
REFRESH_SEC = 3
YFINANCE_HISTORY_CACHE_TTL_SEC = 60 * 60  # yfinance 今日以前歷史資料每小時更新一次
ENABLE_GAP_SIGNAL = True
GROUP_EDIT_PIN = "1219"
GROUPS_FILE = "stock_groups.json"
BACKUP_DIR = "backups"
STOCK_NAME_FILE = "TWstocklistname2.txt"
STOCK_SCAN_FILE = "TWstocklistname2.txt"
FORCE_SCAN_ALL_STOCKS_FROM_FILE = True
ALL_STOCK_GROUP_NAME = "TWstocklistname2 全股票掃描"
AUTO_YFINANCE_AFTER_HOUR = 13
AUTO_YFINANCE_AFTER_MINUTE = 30
APP_LOGO = "dog.jpg"

# ===== Telegram 設定（請替換為你的資訊）=====
TELEGRAM_BOT_TOKEN = st.secrets.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = st.secrets.get("TELEGRAM_CHAT_ID", "")  

DEFAULT_STOCK_GROUPS = {
    "權值股": [
        "2330.TW", "00981A.TW", "2449.TW", "2317.TW", "3711.TW",
        "6488.TWO", "2327.TW", "6176.TW", "2303.TW", "5347.TWO",
    ],
    "自選股1": [
        "3008.TW", "3035.TW", "4566.TW", "4956.TW", "6456.TW",
        "4749.TWO", "6271.TW", "6290.TWO", "4919.TW"
    ],
}

# ===== CSS =====
st.markdown("""
<style>
.dashboard-scroll { overflow-x: auto; overflow-y: hidden; width: 100%; padding-bottom: 8px; }
.dashboard-grid { display: grid; grid-template-columns: repeat(4, minmax(260px, 1fr)); gap: 12px; min-width: 1120px; }
.dashboard-card { border-radius: 12px; padding: 14px 16px; min-height: 180px; box-shadow: 0 1px 4px rgba(0,0,0,0.08); box-sizing: border-box; }
.dashboard-title { font-size: 18px; font-weight: 700; margin-bottom: 10px; color: #000000 !important; }
.dashboard-main { font-size: 28px; font-weight: 800; margin-bottom: 6px; }
.dashboard-sub { font-size: 14px; color: #000000 !important; margin-bottom: 10px; }
.dashboard-detail { font-size: 14px; line-height: 1.7; color: #000000 !important; }
.dashboard-extra { font-size: 13px; line-height: 1.6; color: #000000 !important; margin-top: 10px; padding-top: 8px; border-top: 1px solid rgba(0,0,0,0.12); word-break: break-word; }
.dashboard-link, .dashboard-link:link, .dashboard-link:visited, .dashboard-link:hover, .dashboard-link:active { text-decoration: none !important; color: inherit !important; }
.back-to-dashboard-btn { display: inline-block; padding: 6px 12px; border-radius: 8px; border: 1px solid #999; background: #f5f5f5; color: #000 !important; text-decoration: none !important; font-size: 14px; font-weight: 600; text-align: center; }
.back-to-dashboard-btn:hover { background: #eaeaea; }
</style>
""", unsafe_allow_html=True)

# ===== 分組讀寫 =====
def load_stock_groups():
    if os.path.exists(GROUPS_FILE):
        try:
            with open(GROUPS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and data:
                return data
        except Exception:
            pass
    return copy.deepcopy(DEFAULT_STOCK_GROUPS)

def save_stock_groups(groups):
    with open(GROUPS_FILE, "w", encoding="utf-8") as f:
        json.dump(groups, f, ensure_ascii=False, indent=2)

def ensure_backup_dir():
    os.makedirs(BACKUP_DIR, exist_ok=True)

def create_backup_filename():
    tw_now = datetime.now(ZoneInfo("Asia/Taipei"))
    return f"stock_groups_backup_{tw_now.strftime('%Y%m%d_%H%M%S')}.json"

def save_backup_snapshot(groups):
    ensure_backup_dir()
    filename = create_backup_filename()
    file_path = os.path.join(BACKUP_DIR, filename)
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(groups, f, ensure_ascii=False, indent=2)
    return file_path

def list_backup_files():
    if not os.path.exists(BACKUP_DIR):
        return []
    files = []
    for name in os.listdir(BACKUP_DIR):
        if name.lower().endswith(".json"):
            full_path = os.path.join(BACKUP_DIR, name)
            if os.path.isfile(full_path):
                files.append((name, os.path.getmtime(full_path)))
    files.sort(key=lambda x: x[1], reverse=True)
    return [name for name, _ in files]

# ===== Telegram 工具 =====
def send_telegram_message(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        res = requests.post(url, json=payload, timeout=5)
        if res.status_code != 200:
            st.error(f"Telegram 傳送失敗，API 回傳：{res.text}")
    except Exception as e:
        st.error(f"Telegram 連線失敗: {e}")

def send_telegram_document(file_bytes: bytes, filename: str, caption: str = "") -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        st.error("Telegram Bot Token 或 Chat ID 尚未設定，無法推送檔案。")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
    data = {"chat_id": TELEGRAM_CHAT_ID, "caption": caption}
    files = {
        "document": (
            filename,
            file_bytes,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    }
    try:
        res = requests.post(url, data=data, files=files, timeout=20)
        if res.status_code == 200:
            return True
        st.error(f"Telegram 檔案傳送失敗，API 回傳：{res.text}")
    except Exception as e:
        st.error(f"Telegram 檔案傳送連線失敗: {e}")
    return False

def check_telegram_push_command():
    if not TELEGRAM_BOT_TOKEN:
        return False
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {"timeout": 1} 
    
    if "tg_last_update_id" in st.session_state and st.session_state.tg_last_update_id:
        params["offset"] = st.session_state.tg_last_update_id + 1

    try:
        res = requests.get(url, params=params, timeout=3)
        if res.status_code == 200:
            data = res.json()
            if data.get("ok") and data.get("result"):
                triggered = False
                for item in data["result"]:
                    update_id = item["update_id"]
                    st.session_state.tg_last_update_id = update_id 
                    
                    message_text = item.get("message", {}).get("text", "").strip().lower()
                    if message_text == "push":
                        triggered = True
                return triggered
    except Exception as e:
        pass
    return False

# ===== Fubon API 行情工具 =====
@st.cache_data(ttl=REFRESH_SEC)
def download_stock_data(symbol: str, _sdk):
    if _sdk is None:
        raise ValueError("富邦 API 尚未連線")
        
    fubon_symbol = str(symbol).split(".")[0]
    end_date = date.today()
    start_date = end_date - timedelta(days=90)
    
    try:
        res = _sdk.marketdata.rest_client.stock.historical.candles(**{
            "symbol": fubon_symbol,
            "from": start_date.strftime("%Y-%m-%d"),
            "to": end_date.strftime("%Y-%m-%d"),
            "timeframe": "D",
            "fields": "open,high,low,close,volume"
        })
        
        if res and "data" in res and isinstance(res["data"], list):
            df = pd.DataFrame(res["data"])
            if not df.empty:
                df.rename(columns={
                    "open": "Open",
                    "high": "High",
                    "low": "Low",
                    "close": "Close",
                    "volume": "Volume"
                }, inplace=True)
                
                if "date" in df.columns:
                    df = df.sort_values("date").reset_index(drop=True)
                    
                for col in ["Open", "High", "Low", "Close", "Volume"]:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
                    
                return df
    except Exception as e:
        print(f"富邦 API 抓取 {fubon_symbol} 歷史 K 線失敗: {e}")
        
    return pd.DataFrame()

def normalize_ohlc(df):
    if df is None or df.empty:
        return pd.DataFrame()
    required_cols = ["Open", "High", "Low", "Close", "Volume"]
    if set(required_cols).issubset(df.columns):
        return df[required_cols].copy()
    return pd.DataFrame()

def get_last_price(symbol, df, _sdk):
    fubon_symbol = str(symbol).split(".")[0]
    if _sdk is not None:
        try:
            res = _sdk.marketdata.rest_client.stock.snapshot.quotes(symbol=fubon_symbol)
            if res and "data" in res and len(res["data"]) > 0:
                quote = res["data"][0]
                price = quote.get("closePrice") or quote.get("tradePrice") or quote.get("close")
                if price is not None and pd.notna(price):
                    return float(price)
        except Exception:
            pass
    if not df.empty and "Close" in df.columns:
        return float(df["Close"].iloc[-1])
    raise ValueError("無法取得即時價格")

@st.cache_data(ttl=86400)
def load_stock_name_map(file_path: str = STOCK_NAME_FILE) -> dict:
    name_map = {}
    if not os.path.exists(file_path): return name_map
    with open(file_path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip().replace("\ufeff", "").replace("\u3000", "")
            if not line: continue
            if "\t" in line:
                parts = [p.strip() for p in line.split("\t") if p.strip()]
                if len(parts) >= 2:
                    name_map[parts[0].upper()] = parts[1].strip()
                    continue
            m = re.match(r"^([^\s]+)\s+(.+)$", line)
            if m:
                name_map[m.group(1).strip().upper()] = m.group(2).strip()
    return name_map

@st.cache_data(ttl=86400)
def load_stock_symbols_from_file(file_path: str = STOCK_SCAN_FILE) -> list:
    symbols = []
    seen = set()
    if not os.path.exists(file_path): return symbols
    with open(file_path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip().replace("\ufeff", "").replace("\u3000", "")
            if not line: continue
            symbol = re.split(r"\s+", line, maxsplit=1)[0].strip().upper()
            if not re.match(r"^[0-9A-Z]+\.(TW|TWO)$", symbol): continue
            if symbol not in seen:
                seen.add(symbol)
                symbols.append(symbol)
    return symbols

def load_all_stock_group_from_file() -> dict:
    symbols = load_stock_symbols_from_file(STOCK_SCAN_FILE)
    return {ALL_STOCK_GROUP_NAME: symbols}

def _normalize_yfinance_ohlcv(df):
    if df is None or df.empty: return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    df = df.reset_index()
    required_cols = ["Open", "High", "Low", "Close", "Volume"]
    if not set(required_cols).issubset(df.columns): return pd.DataFrame()
    date_col = "Date" if "Date" in df.columns else df.columns[0]
    df["Date"] = pd.to_datetime(df[date_col], errors="coerce").dt.date
    for col in required_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df[["Date"] + required_cols].dropna(subset=["Date", "Open", "High", "Low", "Close"]).reset_index(drop=True)

@st.cache_data(ttl=YFINANCE_HISTORY_CACHE_TTL_SEC)
def download_stock_data_yfinance_history(symbol: str, today_str: str):
    if yf is None: return pd.DataFrame()
    try:
        df = yf.download(str(symbol).strip().upper(), period="4mo", interval="1d", auto_adjust=False, progress=False, threads=False)
        df = _normalize_yfinance_ohlcv(df)
        if df.empty: return pd.DataFrame()
        today = pd.to_datetime(today_str).date()
        return df[df["Date"] < today].reset_index(drop=True)
    except Exception:
        return pd.DataFrame()

@st.cache_data(ttl=REFRESH_SEC)
def download_stock_data_yfinance_today(symbol: str, today_str: str):
    if yf is None: return pd.DataFrame()
    try:
        df = yf.download(str(symbol).strip().upper(), period="5d", interval="1d", auto_adjust=False, progress=False, threads=False)
        df = _normalize_yfinance_ohlcv(df)
        if df.empty: return pd.DataFrame()
        today = pd.to_datetime(today_str).date()
        return df[df["Date"] >= today].reset_index(drop=True)
    except Exception:
        return pd.DataFrame()

def download_stock_data_yfinance(symbol: str):
    today_str = datetime.now(ZoneInfo("Asia/Taipei")).strftime("%Y-%m-%d")
    history_df = download_stock_data_yfinance_history(symbol, today_str)
    today_df = download_stock_data_yfinance_today(symbol, today_str)

    frames = [df for df in [history_df, today_df] if df is not None and not df.empty]
    if not frames: return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    if "Date" in df.columns:
        df = df.drop_duplicates(subset=["Date"], keep="last").sort_values("Date").reset_index(drop=True)
    return df.reset_index(drop=True)

def resolve_price_source(now_dt=None) -> str:
    mode = st.session_state.get("price_source_mode", "自動")
    if mode in ["WebSocket", "Yfinance"]: return mode
    if now_dt is None: now_dt = datetime.now(ZoneInfo("Asia/Taipei"))
    cutoff = now_dt.replace(hour=AUTO_YFINANCE_AFTER_HOUR, minute=AUTO_YFINANCE_AFTER_MINUTE, second=0, microsecond=0)
    return "Yfinance" if now_dt >= cutoff else "WebSocket"

def render_price_source_selector(now_dt):
    active_source = resolve_price_source(now_dt)
    source_mode = st.session_state.get("price_source_mode", "自動")
    with st.sidebar.expander("🧭 價格來源模式", expanded=True):
        st.markdown(
            f"""
            <div style="background:#2f4563; color:#35a8ff; border-radius:8px; padding:14px 16px; line-height:1.8; font-weight:600;">
            目前價格模式：{source_mode}；<br>
            實際使用：{active_source}
            </div>
            """, unsafe_allow_html=True,
        )
        c1, c2 = st.columns(2)
        with c1:
            if st.button("WebSocket", use_container_width=True, key="price_source_ws_btn"):
                st.session_state.price_source_mode = "WebSocket"
                st.cache_data.clear(); st.rerun()
        with c2:
            if st.button("Yfinance", use_container_width=True, key="price_source_yf_btn"):
                st.session_state.price_source_mode = "Yfinance"
                st.cache_data.clear(); st.rerun()
        if st.button("恢復自動模式", use_container_width=True, key="price_source_auto_btn"):
            st.session_state.price_source_mode = "自動"
            st.cache_data.clear(); st.rerun()
    return active_source

def download_stock_data_by_source(symbol: str, _sdk, source: str):
    if source == "Yfinance":
        df = download_stock_data_yfinance(symbol)
        if not df.empty: return df
        if _sdk is not None: return download_stock_data(symbol, _sdk)
        return pd.DataFrame()
    return download_stock_data(symbol, _sdk)

def get_last_price_by_source(symbol: str, df, _sdk, source: str):
    if source == "Yfinance":
        if df is not None and not df.empty and "Close" in df.columns:
            price = pd.to_numeric(df["Close"], errors="coerce").dropna()
            if not price.empty: return float(price.iloc[-1])
        if _sdk is not None: return get_last_price(symbol, df, _sdk)
        raise ValueError("yfinance 無法取得價格")
    return get_last_price(symbol, df, _sdk)

def normalize_rows_for_excel(rows):
    # 新增趨勢突破所需欄位
    columns = ["代碼", "股票名稱", "價格", "漲跌%", "成交量(張)", "區高P1", "近高P2", "坡度%", "趨勢價", "MA位置", "MA排列", "K值", "D值", "KD訊號", "MACD柱", "MACD訊號", "跳空訊號", "趨勢突破", "訊號類型", "來源"]
    if not rows: return pd.DataFrame(columns=columns)
    df = pd.DataFrame(rows).drop_duplicates(subset=["代碼"]).copy()
    if "代碼網址" in df.columns: df.drop(columns=["代碼網址"], inplace=True)
    for col in columns:
        if col not in df.columns: df[col] = "-"
    return df[columns]

def contains_cjk(text) -> bool:
    if text is None: return False
    return any(("\u4e00" <= ch <= "\u9fff") or ("\u3400" <= ch <= "\u4dbf") or ("\uf900" <= ch <= "\ufaff") for ch in str(text))

def apply_excel_fonts(workbook):
    from openpyxl.styles import Font
    chinese_font_name = "Microsoft JhengHei"
    english_font_name = "Calibri"
    for worksheet in workbook.worksheets:
        for row in worksheet.iter_rows():
            for cell in row:
                if cell.value is None: cell.font = Font(name=english_font_name)
                elif contains_cjk(cell.value): cell.font = Font(name=chinese_font_name)
                else: cell.font = Font(name=english_font_name)

def build_signal_excel_bytes(signal_buckets: dict) -> bytes:
    gap_rows = signal_buckets.get("跳空", [])
    golden_rows = signal_buckets.get("黃金交叉", [])
    near_golden_rows = signal_buckets.get("即將黃金交叉", [])
    macd_rows = signal_buckets.get("MACD翻正", [])
    trend_rows = signal_buckets.get("趨勢突破", [])

    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        normalize_rows_for_excel(gap_rows).to_excel(writer, sheet_name="跳空", index=False)
        normalize_rows_for_excel(golden_rows).to_excel(writer, sheet_name="黃金交叉", index=False)
        normalize_rows_for_excel(near_golden_rows).to_excel(writer, sheet_name="即將黃金交叉", index=False)
        normalize_rows_for_excel(macd_rows).to_excel(writer, sheet_name="MACD訊號", index=False)
        normalize_rows_for_excel(trend_rows).to_excel(writer, sheet_name="趨勢突破", index=False) # 新增第五分頁
        apply_excel_fonts(writer.book)
    output.seek(0)
    return output.getvalue()

@st.cache_data(ttl=86400)
def get_stock_name(symbol: str, _sdk) -> str:
    name_map = load_stock_name_map(STOCK_NAME_FILE)
    if symbol in name_map: return name_map[symbol]
    fubon_symbol = str(symbol).split(".")[0]
    if _sdk is not None:
        try:
            res = _sdk.marketdata.rest_client.stock.historical.stats(symbol=fubon_symbol)
            if res and "name" in res: return res["name"].strip()
        except Exception: pass
    return fubon_symbol

def make_anchor_id(group_name: str) -> str:
    return "group-" + re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "-", group_name).strip("-")

def yahoo_quote_url(symbol: str) -> str:
    return f"https://tw.stock.yahoo.com/quote/{str(symbol).split('.')[0]}"

def normalize_symbols_from_text(text: str):
    if not text: return []
    text = text.replace("，", ",")
    lines = []
    for raw_line in text.splitlines():
        if raw_line.strip(): lines.extend([p.strip().upper() for p in raw_line.split(",") if p.strip()])
    return list(dict.fromkeys(lines))

def symbol_to_code(symbol: str) -> str:
    return str(symbol).split(".")[0]

def build_top3_html(valid_stock_stats):
    if not valid_stock_stats: return '<span style="color:#666666;">無可用資料</span>'
    top3_sorted = sorted(valid_stock_stats, key=lambda x: x["pct"], reverse=True)[:3]
    parts = []
    for item in top3_sorted:
        pct = float(item["pct"])
        pct_color = "#cf1322" if pct > 0 else "#389e0d" if pct < 0 else "#333333"
        parts.append(f'<span style="color:#000000;">{escape(str(item["code"]))} {escape(str(item["name"]))} </span><span style="color:{pct_color}; font-weight:600;">{pct:+.1f}%</span>')
    return " | ".join(parts)

def compact_name_list(names, max_show=3):
    names = [str(x).strip() for x in names if str(x).strip()]
    if not names: return "無"
    if len(names) <= max_show: return "、".join(names)
    return "、".join(names[:max_show]) + f" 等{len(names)}檔"

# ===== Session State 初始化 =====
if "auto_refresh_enabled" not in st.session_state: st.session_state.auto_refresh_enabled = False
if "refresh_sec" not in st.session_state: st.session_state.refresh_sec = REFRESH_SEC
if "tg_push_enabled" not in st.session_state: st.session_state.tg_push_enabled = False 
if "scheduled_push_enabled" not in st.session_state: st.session_state.scheduled_push_enabled = True 
if "processed_time_slots" not in st.session_state: st.session_state.processed_time_slots = set() 
if "price_source_mode" not in st.session_state: st.session_state.price_source_mode = "自動"
if "scan_enabled" not in st.session_state: st.session_state.scan_enabled = False
if "scan_requested" not in st.session_state: st.session_state.scan_requested = False
if "notified_stocks" not in st.session_state: st.session_state.notified_stocks = set()
if "tg_last_update_id" not in st.session_state: st.session_state.tg_last_update_id = None
if "stock_groups" not in st.session_state:
    st.session_state.stock_groups = load_all_stock_group_from_file() if FORCE_SCAN_ALL_STOCKS_FROM_FILE else load_stock_groups()

def render_auto_refresh_settings():
    with st.sidebar.expander("🔄 自動刷新設定", expanded=True):
        st.toggle("啟用自動刷新", key="auto_refresh_enabled")
        st.number_input("刷新秒數", min_value=1, max_value=300, step=1, key="refresh_sec")

def render_fubon_login():
    st.sidebar.markdown("## 🔑 富邦 API 設定")
    if st.session_state.get("fubon_logged_in"):
        st.sidebar.success("✅ 富邦 API 已成功連線")
        if st.sidebar.button("登出 / 重新連線", use_container_width=True):
            st.session_state.fubon_sdk = None
            st.session_state.fubon_logged_in = False
            st.rerun()
        return

    try:
        pfx_base64 = st.secrets["fubon"]["pfx_base64"]
    except KeyError:
        st.sidebar.error("❌ 找不到 pfx_base64 憑證資料。")
        return

    f_id = st.sidebar.text_input("身分證字號", key="f_id_input")
    f_pw = st.sidebar.text_input("富邦登入密碼", key="f_pw_input", type="password")
    f_cert_pw = st.sidebar.text_input("憑證密碼", key="f_cert_pw_input", type="password")

    if st.sidebar.button("連線行情伺服器", use_container_width=True):
        if not f_id or not f_pw or not f_cert_pw:
            st.sidebar.warning("請填寫完整的帳密資訊！")
        else:
            try:
                temp_cert_path = "temp_cloud_cert.pfx"
                with open(temp_cert_path, "wb") as f: f.write(base64.b64decode(pfx_base64))
                with st.spinner("連線富邦 API 中..."):
                    sdk = FubonSDK()
                    sdk.login(f_id.strip().upper(), f_pw, temp_cert_path, f_cert_pw)
                    sdk.init_realtime()
                    st.session_state.fubon_sdk = sdk
                    st.session_state.fubon_logged_in = True
                st.sidebar.success("✅ 連線成功！")
                st.rerun()
            except Exception as e:
                st.sidebar.error(f"❌ 登入失敗: {e}")

def compute_indicators(df, price):
    if df is None or df.empty: raise ValueError("下載資料為空")
    if len(df) < 20: raise ValueError("歷史資料不足")

    close = pd.to_numeric(df["Close"].squeeze(), errors="coerce")
    low = pd.to_numeric(df["Low"].squeeze(), errors="coerce")
    high = pd.to_numeric(df["High"].squeeze(), errors="coerce")
    volume = pd.to_numeric(df["Volume"].squeeze(), errors="coerce") if "Volume" in df.columns else pd.Series(dtype="float64")

    yesterday_close = float(close.iloc[-2])
    price_val = float(price)
    change_pct = float((price_val / yesterday_close - 1) * 100)
    
    ma5 = float(close.tail(5).mean())
    ma10 = float(close.tail(10).mean())
    ma20 = float(close.tail(20).mean())

    if price_val > ma5: ma_range = ">MA5"
    elif ma5 >= price_val > ma10: ma_range = "MA5~10"
    elif ma10 >= price_val > ma20: ma_range = "MA10~20"
    else: ma_range = "<MA20"

    if ma5 > ma10 > ma20: ma_trend = "多頭"
    elif ma5 < ma10 < ma20: ma_trend = "空頭"
    else: ma_trend = "糾結"

    low_9 = low.rolling(9).min()
    high_9 = high.rolling(9).max()
    rsv = ((close - low_9) / (high_9 - low_9).replace(0, pd.NA)) * 100
    k = rsv.ewm(alpha=1/3, adjust=False).mean()
    d = k.ewm(alpha=1/3, adjust=False).mean()

    k_t, d_t = float(k.iloc[-1]), float(d.iloc[-1])
    k_y, d_y = float(k.iloc[-2]), float(d.iloc[-2])

    if k_y <= d_y and k_t > d_t: kd_signal = "黃金交叉"
    elif k_y >= d_y and k_t < d_t: kd_signal = "死亡交叉"
    elif k_t < d_t and (d_t - k_t) < 3: kd_signal = "即將黃金交叉"
    elif k_t > d_t and (k_t - d_t) < 3: kd_signal = "即將死亡交叉"
    elif k_t < 25: kd_signal = "超賣"
    else: kd_signal = "-"

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd_hist = (ema12 - ema26) - (ema12 - ema26).ewm(span=9, adjust=False).mean()
    macd_hist_t, macd_hist_y = float(macd_hist.iloc[-1]), float(macd_hist.iloc[-2])
    
    if macd_hist_y <= 0 and macd_hist_t > 0: macd_signal = "MACD翻正"
    elif macd_hist_y >= 0 and macd_hist_t < 0: macd_signal = "MACD翻負"
    else: macd_signal = "-"

    latest_volume = float(volume.iloc[-1]) if not volume.empty and pd.notna(volume.iloc[-1]) else 0.0
    volume_lots = latest_volume / 1000

    gap_signal = "-"
    if ENABLE_GAP_SIGNAL and float(low.iloc[-1]) > float(high.iloc[-2]):
        gap_signal = "跳空"

    # ==========================================
    # ===== 新增：趨勢突破策略 (40日 + 8%坡度) =====
    # ==========================================
    trend_signal = "-"
    p1_val = p2_val = slope_pct = tl_val = 0.0

    if len(df) >= 61:
        ma60 = close.rolling(window=60).mean()
        ma60_today = float(ma60.iloc[-1])
        ma60_yesterday = float(ma60.iloc[-2])
        
        # 條件 1: 60MA 方向上揚
        if pd.notna(ma60_today) and pd.notna(ma60_yesterday) and ma60_today > ma60_yesterday:
            data_40 = df.tail(40).copy().reset_index(drop=True)
            high_40 = pd.to_numeric(data_40['High'], errors='coerce')
            close_40 = pd.to_numeric(data_40['Close'], errors='coerce')
            
            p1_pos = high_40.idxmax()
            p1_v = float(high_40.max())
            
            if p1_pos <= 30: # 條件 2: 確保 P1 不要離今天太近
                after_p1_data = high_40.iloc[p1_pos + 5 : -1] # 需距離 P1 五天以上且排除今日
                if not after_p1_data.empty:
                    p2_pos = after_p1_data.idxmax()
                    p2_v = float(after_p1_data.max())
                    
                    # 條件 3: P1 必須大於 P2 至少 8%
                    if p1_v >= (p2_v * 1.08):
                        slope = (p2_v - p1_v) / (p2_pos - p1_pos)
                        x_now = 39 # today is index 39 in tail(40)
                        trendline_now = p2_v + slope * (x_now - p2_pos)
                        
                        today_close = float(price_val)
                        yesterday_close = float(close_40.iloc[-2])
                        
                        # 條件 4: 今日收盤突破趨勢線
                        if today_close > trendline_now and yesterday_close <= (trendline_now - slope):
                            trend_signal = "趨勢突破"
                            p1_val = p1_v
                            p2_val = p2_v
                            slope_pct = ((p1_v / p2_v) - 1) * 100
                            tl_val = trendline_now

    return {
        "price": round(price_val, 2), "pct": round(change_pct, 2),
        "ma_range": ma_range, "ma_trend": ma_trend,
        "k": round(k_t, 1), "d": round(d_t, 1),
        "kd_signal": kd_signal, "gap_signal": gap_signal,
        "macd_hist": round(macd_hist_t, 4), "macd_signal": macd_signal,
        "volume": int(latest_volume), "volume_lots": round(volume_lots, 1),
        "trend_signal": trend_signal,
        "p1_val": round(p1_val, 2) if p1_val else "-",
        "p2_val": round(p2_val, 2) if p2_val else "-",
        "slope_pct": round(slope_pct, 1) if slope_pct else "-",
        "tl_val": round(tl_val, 2) if tl_val else "-"
    }

def format_color(val):
    if isinstance(val, (int, float)):
        if val > 0: return f"🔴 +{val:.2f}%"
        elif val < 0: return f"🟢 {val:.2f}%"
        else: return f"{val:.2f}%"
    return val

def format_k(val):
    if isinstance(val, (int, float)):
        if val >= 74: return f"🔴 {val:.1f}"
        elif val >= 50: return f"🟡 {val:.1f}"
        else: return f"🟢 {val:.1f}"
    return val

def format_gap(val): return "🔴 跳空" if val == "跳空" else "-"
def format_trend(val): return "🔥 突破" if val == "趨勢突破" else "-"

def format_volume(val):
    try: return f"{float(val):,.1f}"
    except Exception: return val

def render_scan_progress_card(placeholder, pct: float, status_text: str = "掃描進度"):
    pct = max(0.0, min(float(pct), 100.0))
    placeholder.markdown(
        f"""
        <div style="width: 120px; min-height: 78px; padding: 8px 10px; text-align: left; box-sizing: border-box;">
            <div style="font-size: 30px; line-height: 1; font-weight: 800;">{pct:.0f}%</div>
            <div style="font-size: 13px; margin-top: 8px;">{status_text}</div>
        </div>
        """, unsafe_allow_html=True
    )

def render_summary_dashboard(group_up_summary, rise_threshold):
    st.markdown("### 📌 漲幅儀表板")
    st.caption(f"目前儀表板統計門檻：漲幅 ≥ {rise_threshold}%")
    html_parts = ['<div class="dashboard-scroll"><div class="dashboard-grid">']
    for item in group_up_summary:
        group_name = escape(str(item["分類"]))
        hit_ratio = (item["達標數"] / item["總數"] * 100) if item["總數"] > 0 else 0
        bg_color, border_color, accent_color = ("#fff1f0", "#ff7875", "#cf1322") if hit_ratio >= 60 else ("#fff7e6", "#ffa940", "#d46b08") if hit_ratio > 0 else ("#f6ffed", "#95de64", "#389e0d")
        
        card_html = (
            f'<a href="#{make_anchor_id(group_name)}" class="dashboard-link">'
            f'<div class="dashboard-card" style="background-color:{bg_color}; border:1px solid {border_color};">'
            f'<div class="dashboard-title">{group_name}</div>'
            f'<div class="dashboard-main" style="color:{accent_color};">{item["達標數"]} / {item["總數"]}</div>'
            f'<div class="dashboard-detail">🎯 達標：<b>{item["達標數"]}</b> 檔（{escape(str(item["達標股票名稱"]))}）<br>🔴 一般上漲：<b>{item["上漲數"]}</b> | 🟢 下跌：<b>{item["下跌數"]}</b></div>'
            f'<div class="dashboard-extra">▶ {item["前三名HTML"]}</div>'
            f'</div></a>'
        )
        html_parts.append(card_html)
    html_parts.append("</div></div>")
    st.markdown("".join(html_parts), unsafe_allow_html=True)

# ==================== 主畫面開始 ====================
st.markdown('<div id="dashboard-top" style="scroll-margin-top: 90px;"></div>', unsafe_allow_html=True)
title_icon_col, title_text_col, scan_progress_col = st.columns([0.45, 7.55, 1])

with title_icon_col:
    if os.path.exists(APP_LOGO): st.image(APP_LOGO, width=58)
    else: st.markdown('<div style="font-size:42px;">📊</div>', unsafe_allow_html=True)

with title_text_col:
    st.markdown('<h1 style="margin:0; padding-top:4px; font-size:42px; font-weight:800;">台股掃描器 - 告訴我你會買日月光</h1>', unsafe_allow_html=True)

with scan_progress_col:
    scan_progress_card_placeholder = st.empty()

render_scan_progress_card(scan_progress_card_placeholder, 0, "掃描進度")
st.markdown('<div style="margin-bottom: 10px;"></div>', unsafe_allow_html=True)
gc.collect()
render_fubon_login()
tw_now = datetime.now(ZoneInfo("Asia/Taipei"))
active_price_source = render_price_source_selector(tw_now)
render_auto_refresh_settings()

if FORCE_SCAN_ALL_STOCKS_FROM_FILE:
    st.sidebar.success(f"✅ 全市場掃描模式：已載入 {len(st.session_state.stock_groups.get(ALL_STOCK_GROUP_NAME, []))} 檔股票")

st.caption(f"更新時間：{tw_now.strftime('%Y-%m-%d %H:%M:%S')}｜價格來源：{active_price_source}")
rise_threshold = st.slider("儀表板漲幅達標門檻 (%)", min_value=5, max_value=9, value=5, step=1)

st.markdown("### 🎯 掃描條件")
# 調整欄位比例以容納新的勾選框
scan_btn_col1, scan_btn_col2, scan_col1, scan_col2, scan_col3, scan_macd_col, scan_trend_col, scan_vol_col, scan_col4 = st.columns([0.9, 0.9, 1.1, 0.7, 1.2, 1.0, 1.1, 1.1, 1.5])
with scan_btn_col1:
    if st.button("▶️ 開始掃描", use_container_width=True, disabled=st.session_state.scan_enabled):
        st.session_state.scan_enabled = True; st.session_state.scan_requested = True; st.cache_data.clear(); st.rerun()
with scan_btn_col2:
    if st.button("⏹️ 停止掃描", use_container_width=True, disabled=not st.session_state.scan_enabled):
        st.session_state.scan_enabled = False; st.session_state.scan_requested = False; st.rerun()
with scan_col1: show_only_signal_rows = st.toggle("只顯示訊號股", value=True)
with scan_col2: include_gap_signal_filter = st.checkbox("跳空", value=True)
with scan_col3: include_kd_signal_filter = st.checkbox("黃金交叉", value=True)
with scan_macd_col: include_macd_signal_filter = st.checkbox("MACD", value=True)
with scan_trend_col: include_trend_signal_filter = st.checkbox("趨勢突破", value=True, help="40日動態雙高點下降趨勢 + 8%坡度 + 60MA上揚")
with scan_vol_col: min_volume_lots = st.number_input("成交量下限", min_value=0, value=1000, step=100)
with scan_col4: scan_action_placeholder = st.empty()

selected_signal_names = []
if include_gap_signal_filter: selected_signal_names.append("跳空")
if include_kd_signal_filter: selected_signal_names.extend(["黃金交叉", "即將黃金交叉"])
if include_macd_signal_filter: selected_signal_names.append("MACD翻正")
if include_trend_signal_filter: selected_signal_names.append("趨勢突破")

if not selected_signal_names: st.warning("請至少勾選一種掃描訊號。")

should_run_scan = bool(st.session_state.pop("scan_requested", False))
if not should_run_scan and "last_scan_result" not in st.session_state:
    render_scan_progress_card(scan_progress_card_placeholder, 0, "掃描進度")
    st.info("請按「開始掃描」開始抓取股票資料。")
    st.stop()

if should_run_scan:
    can_push_now = False
    current_schedule_key = None
    manual_push_triggered = False

    if st.session_state.tg_push_enabled:
        manual_push_triggered = check_telegram_push_command()
        if manual_push_triggered:
            can_push_now = True; st.session_state.notified_stocks = set()
            send_telegram_message("🤖 <b>收到指令，開始強制推播強勢股...</b>")
        elif st.session_state.scheduled_push_enabled:
            TARGET_TIMES = [tw_now.replace(hour=h, minute=m, second=0, microsecond=0) for h, m in [(9,40), (10,0), (11,0), (12,0), (13,0)]]
            for target_dt in TARGET_TIMES:
                if abs((tw_now - target_dt).total_seconds()) <= 45:
                    current_schedule_key = f"slot_{tw_now.strftime('%Y%m%d')}_{target_dt.strftime('%H%M')}"
                    if current_schedule_key not in st.session_state.processed_time_slots:
                        can_push_now = True; break

    group_tables = {}
    group_up_summary = []
    all_signal_rows = []
    # ===== 加入 第5種 緩存 =====
    signal_buckets = {"跳空": [], "黃金交叉": [], "即將黃金交叉": [], "MACD翻正": [], "趨勢突破": []}
    scan_total_count = sum(len(stocks) for stocks in st.session_state.stock_groups.values())
    progress_bar = st.progress(0, text=f"準備掃描 {scan_total_count} 檔")
    processed_count = 0

    for group_name, stocks in st.session_state.stock_groups.items():
        rows, hit_names, valid_stock_stats = [], [], []
        hit_count = up_count = down_count = flat_count = error_count = 0

        for symbol in stocks:
            if not st.session_state.scan_enabled: st.stop()
            processed_count += 1
            progress_pct = (processed_count / scan_total_count) * 100 if scan_total_count else 0
            render_scan_progress_card(scan_progress_card_placeholder, progress_pct, "掃描進度")
            progress_bar.progress(processed_count/scan_total_count, text=f"掃描進度：{progress_pct:.1f}%（{symbol}）")
            
            try:
                df = normalize_ohlc(download_stock_data_by_source(symbol, st.session_state.fubon_sdk, active_price_source))
                if df.empty: raise ValueError("無效K線")
                price = get_last_price_by_source(symbol, df, st.session_state.fubon_sdk, active_price_source)
                stock_name = get_stock_name(symbol, st.session_state.fubon_sdk)
                data = compute_indicators(df, price)

                signal_types = []
                if data["gap_signal"] == "跳空": signal_types.append("跳空")
                if data["kd_signal"] in ["黃金交叉", "即將黃金交叉"]: signal_types.append(data["kd_signal"])
                if data["macd_signal"] == "MACD翻正": signal_types.append("MACD翻正")
                if data["trend_signal"] == "趨勢突破": signal_types.append("趨勢突破")
                
                passes_volume_filter = float(data.get("volume_lots", 0)) >= float(min_volume_lots)
                is_selected_signal = any(sig in selected_signal_names for sig in signal_types) and passes_volume_filter

                if (data["pct"] >= 5 or is_selected_signal) and passes_volume_filter:
                    notify_key = f"{symbol}_{tw_now.strftime('%Y-%m-%d')}"
                    if can_push_now and (notify_key not in st.session_state.notified_stocks):
                        msg = (f"🔔 <b>掃描訊號：{stock_name} ({symbol})</b>\n\n"
                               f"📈 價格：{data['price']} | 🔥 漲幅：{data['pct']}%\n"
                               f"🚀 跳空：{data['gap_signal']} | 📊 KD：{data['kd_signal']} | 🧭 MACD：{data['macd_signal']}\n"
                               f"🔥 趨勢突破：{data['trend_signal']}")
                        send_telegram_message(msg); st.session_state.notified_stocks.add(notify_key)

                if data["pct"] >= rise_threshold: hit_count += 1; hit_names.append(stock_name)
                if data["pct"] > 0: up_count += 1
                elif data["pct"] < 0: down_count += 1
                else: flat_count += 1

                valid_stock_stats.append({"symbol": symbol, "code": symbol_to_code(symbol), "name": stock_name, "pct": float(data["pct"])})
                
                row = {
                    "代碼": symbol, "代碼網址": yahoo_quote_url(symbol), "股票名稱": stock_name,
                    "價格": f"{data['price']:.2f}", "漲跌%": data["pct"], "成交量(張)": data["volume_lots"],
                    "區高P1": data["p1_val"], "近高P2": data["p2_val"], "坡度%": data["slope_pct"], "趨勢價": data["tl_val"],
                    "MA位置": data["ma_range"], "MA排列": data["ma_trend"], "K值": data["k"], "D值": f"{data['d']:.1f}",
                    "KD訊號": data["kd_signal"], "MACD柱": data["macd_hist"], "MACD訊號": data["macd_signal"],
                    "跳空訊號": data["gap_signal"], "趨勢突破": data["trend_signal"],
                    "訊號類型": "、".join(signal_types) if signal_types else "-", "來源": active_price_source,
                }
                
                if (not show_only_signal_rows or is_selected_signal) and passes_volume_filter: rows.append(row)
                if is_selected_signal:
                    all_signal_rows.append(row.copy())
                    for sig in signal_types:
                        if sig in signal_buckets and sig in selected_signal_names: signal_buckets[sig].append(row.copy())
            except Exception as e:
                error_count += 1
                if not show_only_signal_rows: rows.append({"代碼": symbol, "股票名稱": symbol, "漲跌%": "-", "跳空訊號": str(e)})

        display_df = pd.DataFrame(rows)
        if not display_df.empty:
            display_df["漲跌%"] = display_df["漲跌%"].apply(format_color)
            display_df["K值"] = display_df["K值"].apply(format_k)
            display_df["成交量(張)"] = display_df["成交量(張)"].apply(format_volume)
            display_df["跳空訊號"] = display_df["跳空訊號"].apply(format_gap)
            display_df["趨勢突破"] = display_df["趨勢突破"].apply(format_trend)
            
        group_tables[group_name] = {"count": len(stocks), "table": display_df}
        group_up_summary.append({
            "分類": group_name, "達標數": hit_count, "達標股票名稱": compact_name_list(hit_names),
            "前三名HTML": build_top3_html(valid_stock_stats), "上漲數": up_count, "下跌數": down_count,
            "總數": len(stocks)
        })

    progress_bar.empty()
    if can_push_now and st.session_state.scheduled_push_enabled and current_schedule_key and not manual_push_triggered:
        st.session_state.processed_time_slots.add(current_schedule_key)

    st.session_state.last_scan_result = {
        "group_tables": group_tables, "group_up_summary": group_up_summary, "all_signal_rows": all_signal_rows,
        "signal_buckets": signal_buckets, "excel_filename": f"TWstock_signal_scan_{tw_now.strftime('%Y%m%d_%H%M%S')}.xlsx",
        "progress_pct": 100, "min_volume_lots": min_volume_lots,
    }
    st.session_state.scan_enabled = False

last_scan = st.session_state.get("last_scan_result", {})
group_tables, group_up_summary = last_scan.get("group_tables", {}), last_scan.get("group_up_summary", [])
all_signal_rows, signal_buckets = last_scan.get("all_signal_rows", []), last_scan.get("signal_buckets", {})
render_scan_progress_card(scan_progress_card_placeholder, last_scan.get("progress_pct", 100))

excel_bytes = build_signal_excel_bytes(signal_buckets)
with scan_action_placeholder.container():
    bcol1, bcol2 = st.columns(2)
    with bcol1: st.download_button("下載Excel", data=excel_bytes, file_name=last_scan.get("excel_filename", "scan.xlsx"), use_container_width=True)
    with bcol2:
        if st.button("推送到 TG", use_container_width=True):
            if send_telegram_document(excel_bytes, last_scan.get("excel_filename", "scan.xlsx"), caption="台股掃描結果"): st.success("已推送")

st.markdown("### 🔎 訊號掃描結果")
st.metric("符合勾選訊號股票數", len(pd.DataFrame(all_signal_rows).drop_duplicates(subset=["代碼"])) if all_signal_rows else 0)

if all_signal_rows:
    signal_df = pd.DataFrame(all_signal_rows).drop_duplicates(subset=["代碼"]).copy()
    signal_df["漲跌%"] = signal_df["漲跌%"].apply(format_color)
    signal_df["K值"] = signal_df["K值"].apply(format_k)
    signal_df["成交量(張)"] = signal_df["成交量(張)"].apply(format_volume)
    signal_df["跳空訊號"] = signal_df["跳空訊號"].apply(format_gap)
    signal_df["趨勢突破"] = signal_df["趨勢突破"].apply(format_trend)
    signal_df["代碼"] = signal_df["代碼網址"]
    
    # 新增加入呈現的欄位
    display_columns = ["代碼", "股票名稱", "價格", "漲跌%", "成交量(張)", "區高P1", "近高P2", "坡度%", "趨勢價", "趨勢突破", "MA位置", "MA排列", "KD訊號", "MACD訊號", "跳空訊號"]
    st.dataframe(signal_df[[c for c in display_columns if c in signal_df.columns]], use_container_width=True, column_config={"代碼": st.column_config.LinkColumn("代碼", display_text=r"https://tw.stock.yahoo.com/quote/(.*)")})

    st.markdown("### 📑 依訊號分頁查看")
    # ===== 加入第5個分頁 =====
    signal_tab_specs = [("跳空", "跳空"), ("黃金交叉", "黃金交叉"), ("即將黃金交叉", "即將黃金交叉"), ("MACD 訊號", "MACD翻正"), ("趨勢突破", "趨勢突破")]
    tabs = st.tabs([f"{name}（{len(pd.DataFrame(signal_buckets.get(key, [])).drop_duplicates(subset=['代碼'])) if signal_buckets.get(key) else 0}）" for name, key in signal_tab_specs])
    
    for tab, (display_name, bucket_key) in zip(tabs, signal_tab_specs):
        with tab:
            rows = signal_buckets.get(bucket_key, [])
            if rows:
                b_df = pd.DataFrame(rows).drop_duplicates(subset=["代碼"]).copy()
                b_df["漲跌%"] = b_df["漲跌%"].apply(format_color)
                b_df["成交量(張)"] = b_df["成交量(張)"].apply(format_volume)
                b_df["跳空訊號"] = b_df["跳空訊號"].apply(format_gap)
                b_df["趨勢突破"] = b_df["趨勢突破"].apply(format_trend)
                b_df["代碼"] = b_df.get("代碼網址", b_df["代碼"])
                st.dataframe(b_df[[c for c in display_columns if c in b_df.columns]], use_container_width=True, column_config={"代碼": st.column_config.LinkColumn("代碼", display_text=r"https://tw.stock.yahoo.com/quote/(.*)")})
            else:
                st.caption(f"目前沒有符合「{display_name}」的股票。")
else:
    st.info("目前沒有掃描到符合條件的股票。")

st.divider()
render_summary_dashboard(group_up_summary, rise_threshold)
st.divider()

for group_name, info in group_tables.items():
    st.markdown(f'<div id="{make_anchor_id(group_name)}" style="scroll-margin-top: 80px;"></div>', unsafe_allow_html=True)
    st.subheader(f"【{group_name}】({info['count']}檔)")
    table_df = info["table"].copy()
    if not table_df.empty and "代碼網址" in table_df.columns: table_df["代碼"] = table_df["代碼網址"]
    st.dataframe(table_df[[c for c in display_columns if c in table_df.columns]], use_container_width=True, column_config={"代碼": st.column_config.LinkColumn("代碼", display_text=r"https://tw.stock.yahoo.com/quote/(.*)")})

if st.session_state.auto_refresh_enabled:
    time.sleep(max(1, int(st.session_state.get("refresh_sec", REFRESH_SEC))))
    st.rerun()
