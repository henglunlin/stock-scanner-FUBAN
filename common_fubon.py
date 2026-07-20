# -*- coding: utf-8 -*-
"""
common_fubon.py
================
共用工具模組：把主掃描程式（Sacnner_Web_fubon_...py）裡「跟資料抓取 / 資料源切換 /
股票清單載入 / Excel & Telegram 輸出」有關、不含頁面畫面邏輯的函式抽出來，
讓「巧妙點」等新頁面可以直接 import 使用，不用整份複製主程式。

⚠️ 這個檔案必須跟主程式放在同一個資料夾（例如 app 根目錄），
   新頁面則放在 pages/ 資料夾底下，Streamlit 才能自動辨識成多頁應用程式。
"""

import re
import os
from html import escape
from io import BytesIO
from datetime import date, timedelta

import pandas as pd
import requests
import streamlit as st

try:
    import yfinance as yf
except ImportError:
    yf = None

# ===== 共用常數 =====
REFRESH_SEC = 3
YFINANCE_HISTORY_CACHE_TTL_SEC = 60 * 60
STOCK_NAME_FILE = "TWstocklistname2.txt"          # 股票代碼->名稱 對照表（沿用主程式既有檔案）
AUTO_YFINANCE_AFTER_HOUR = 13
AUTO_YFINANCE_AFTER_MINUTE = 30

TELEGRAM_BOT_TOKEN = st.secrets.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = st.secrets.get("TELEGRAM_CHAT_ID", "")


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


# ===== 富邦 API 行情工具 =====
def _fetch_fubon_candles(symbol: str, _sdk, start_date, end_date) -> pd.DataFrame:
    if _sdk is None:
        raise ValueError("富邦 API 尚未連線")
    fubon_symbol = str(symbol).split(".")[0]
    try:
        res = _sdk.marketdata.rest_client.stock.historical.candles(**{
            "symbol": fubon_symbol,
            "from": start_date.strftime("%Y-%m-%d"),
            "to": end_date.strftime("%Y-%m-%d"),
            "timeframe": "D",
            "fields": "open,high,low,close,volume"
        })
        if res and "data" in res and isinstance(res["data"], list) and res["data"]:
            df = pd.DataFrame(res["data"])
            df.rename(columns={
                "open": "Open", "high": "High", "low": "Low",
                "close": "Close", "volume": "Volume", "date": "Date",
            }, inplace=True)
            if "Date" in df.columns:
                df["Date"] = pd.to_datetime(df["Date"], errors="coerce").dt.date
                df = df.sort_values("Date").reset_index(drop=True)
            for col in ["Open", "High", "Low", "Close", "Volume"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            keep_cols = [c for c in ["Date", "Open", "High", "Low", "Close", "Volume"] if c in df.columns]
            return df[keep_cols].dropna(subset=["Open", "High", "Low", "Close"]).reset_index(drop=True)
    except Exception as e:
        print(f"富邦 API 抓取 {fubon_symbol} K 線失敗: {e}")
    return pd.DataFrame()


@st.cache_data(ttl=REFRESH_SEC)
def download_stock_data(symbol: str, _sdk):
    end_date = date.today()
    start_date = end_date - timedelta(days=90)
    return _fetch_fubon_candles(symbol, _sdk, start_date, end_date)


@st.cache_data(ttl=REFRESH_SEC)
def download_stock_data_fubon_today(symbol: str, _sdk, today_str: str):
    if _sdk is None:
        return pd.DataFrame()
    today = date.today()
    return _fetch_fubon_candles(symbol, _sdk, today, today)


def normalize_ohlc(df):
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.copy()
    if "date" in df.columns and "Date" not in df.columns:
        df.rename(columns={"date": "Date"}, inplace=True)
    required_cols = ["Open", "High", "Low", "Close", "Volume"]
    if set(required_cols).issubset(df.columns):
        cols = (["Date"] if "Date" in df.columns else []) + required_cols
        out = df[cols].copy()
        if "Date" in out.columns:
            out["Date"] = pd.to_datetime(out["Date"], errors="coerce").dt.date
        for col in required_cols:
            out[col] = pd.to_numeric(out[col], errors="coerce")
        return out.dropna(subset=["Open", "High", "Low", "Close"]).reset_index(drop=True)
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
    if not os.path.exists(file_path):
        return name_map
    with open(file_path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip().replace("\ufeff", "").replace("\u3000", "")
            if not line:
                continue
            if "\t" in line:
                parts = [p.strip() for p in line.split("\t") if p.strip()]
                if len(parts) >= 2:
                    name_map[parts[0].upper()] = parts[1].strip()
                    continue
            m = re.match(r"^([^\s]+)\s+(.+)$", line)
            if m:
                name_map[m.group(1).strip().upper()] = m.group(2).strip()
    return name_map


def _parse_symbol_lines(lines):
    """把一行一行的文字（'代碼 名稱' 或純代碼）解析成去重後的代碼 list。"""
    symbols = []
    seen = set()
    for raw_line in lines:
        line = str(raw_line).strip().replace("\ufeff", "").replace("\u3000", "")
        if not line:
            continue
        symbol = re.split(r"[\s,，]+", line, maxsplit=1)[0].strip().upper()
        if not re.match(r"^[0-9A-Z]+\.(TW|TWO)$", symbol):
            continue
        if symbol not in seen:
            seen.add(symbol)
            symbols.append(symbol)
    return symbols


@st.cache_data(ttl=86400)
def load_stock_symbols_from_file(file_path: str) -> list:
    """從指定的股票清單檔案載入代碼清單（各頁面可傳入各自獨立的檔案路徑）。"""
    if not os.path.exists(file_path):
        return []
    with open(file_path, "r", encoding="utf-8") as f:
        return _parse_symbol_lines(f.readlines())


def parse_stock_symbols_from_text(text: str) -> list:
    """從使用者上傳/貼上的文字內容解析代碼清單（不快取，即時解析）。"""
    if not text:
        return []
    return _parse_symbol_lines(text.splitlines())


def normalize_symbol_quick(input_text: str):
    """把使用者手動輸入的單一代碼補齊成 xxxx.TW / xxxx.TWO 格式。"""
    s = str(input_text).strip().upper()
    if not s:
        return None
    if "." in s:
        return s
    if s.isdigit():
        if s.startswith(("3", "6", "8")):
            return f"{s}.TWO"
        return f"{s}.TW"
    return s


def parse_manual_symbols(text: str) -> list:
    """手動輸入區塊專用：逐行 / 逗號分隔，並自動補齊 .TW / .TWO。"""
    if not text:
        return []
    text = text.replace("，", ",")
    tokens = []
    for raw_line in text.splitlines():
        for part in raw_line.split(","):
            part = part.strip()
            if part:
                tokens.append(part)
    symbols = []
    seen = set()
    for t in tokens:
        sym = normalize_symbol_quick(t)
        if sym and sym not in seen:
            seen.add(sym)
            symbols.append(sym)
    return symbols


@st.cache_data(ttl=86400)
def load_code_to_ticker_map(file_path: str = STOCK_NAME_FILE) -> dict:
    """從『代碼<TAB/空白>名稱』格式的清單檔載入『純數字代碼 -> 完整代碼(含.TW/.TWO)』對照表。
    用來把使用者輸入或 Excel 匯入的純數字代碼，準確轉換成正確的 .TW / .TWO 後綴，
    避免對每個代碼同時亂猜兩種後綴（那樣會讓批次 yfinance 請求膨脹、拖累抓取成功率）。
    """
    mapping = {}
    if not os.path.exists(file_path):
        return mapping
    with open(file_path, "r", encoding="utf-8-sig", errors="ignore") as f:
        for raw_line in f:
            line = raw_line.strip().replace("\u3000", "")
            if not line:
                continue
            parts = re.split(r"[\t]+", line) if "\t" in line else line.split(None, 1)
            if not parts:
                continue
            ticker = parts[0].strip().upper()
            if "." in ticker:
                mapping[ticker.split(".")[0]] = ticker
    return mapping


def resolve_ticker_suffix(raw_code, code_map: dict = None) -> str:
    """把單一輸入（可能是純數字、可能已含 .TW/.TWO）解析成正確的完整代碼。
    已經帶明確後綴就直接使用；純數字則優先查對照表，查不到才退回猜測 .TW。"""
    code_map = code_map or {}
    raw = str(raw_code).strip().upper()
    if not raw:
        return ""
    if "." in raw:
        return raw
    if raw in code_map:
        return code_map[raw]
    return normalize_symbol_quick(raw) or raw



    if df is None or df.empty:
        return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    df = df.reset_index()
    required_cols = ["Open", "High", "Low", "Close", "Volume"]
    if not set(required_cols).issubset(df.columns):
        return pd.DataFrame()
    date_col = "Date" if "Date" in df.columns else df.columns[0]
    df["Date"] = pd.to_datetime(df[date_col], errors="coerce").dt.date
    for col in required_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df[["Date"] + required_cols].dropna(subset=["Date", "Open", "High", "Low", "Close"]).reset_index(drop=True)


@st.cache_data(ttl=YFINANCE_HISTORY_CACHE_TTL_SEC)
def download_stock_data_yfinance_history(symbol: str, today_str: str):
    if yf is None:
        return pd.DataFrame()
    try:
        df = yf.download(str(symbol).strip().upper(), period="4mo", interval="1d", auto_adjust=False, progress=False, threads=False)
        df = _normalize_yfinance_ohlcv(df)
        if df.empty:
            return pd.DataFrame()
        today = pd.to_datetime(today_str).date()
        return df[df["Date"] < today].reset_index(drop=True)
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=REFRESH_SEC)
def download_stock_data_yfinance_today(symbol: str, today_str: str):
    if yf is None:
        return pd.DataFrame()
    try:
        df = yf.download(str(symbol).strip().upper(), period="5d", interval="1d", auto_adjust=False, progress=False, threads=False)
        df = _normalize_yfinance_ohlcv(df)
        if df.empty:
            return pd.DataFrame()
        today = pd.to_datetime(today_str).date()
        return df[df["Date"] >= today].reset_index(drop=True)
    except Exception:
        return pd.DataFrame()


def _split_yfinance_bulk_result(raw: pd.DataFrame, symbols: tuple) -> dict:
    result = {}
    if raw is None or raw.empty:
        return {s: pd.DataFrame() for s in symbols}
    is_multi = isinstance(raw.columns, pd.MultiIndex)
    for symbol in symbols:
        try:
            if is_multi:
                if symbol not in raw.columns.get_level_values(0):
                    result[symbol] = pd.DataFrame()
                    continue
                sub = raw[symbol].copy()
            else:
                sub = raw.copy()
            result[symbol] = _normalize_yfinance_ohlcv(sub)
        except Exception:
            result[symbol] = pd.DataFrame()
    return result


@st.cache_data(ttl=YFINANCE_HISTORY_CACHE_TTL_SEC)
def bulk_download_yfinance_history(symbols: tuple, today_str: str) -> dict:
    if yf is None or not symbols:
        return {}
    try:
        raw = yf.download(
            tickers=list(symbols), period="4mo", interval="1d",
            auto_adjust=False, group_by="ticker", threads=True, progress=False,
        )
    except Exception:
        return {s: pd.DataFrame() for s in symbols}
    today = pd.to_datetime(today_str).date()
    per_symbol = _split_yfinance_bulk_result(raw, symbols)
    return {
        s: (df[df["Date"] < today].reset_index(drop=True) if not df.empty else df)
        for s, df in per_symbol.items()
    }


@st.cache_data(ttl=REFRESH_SEC)
def bulk_download_yfinance_today(symbols: tuple, today_str: str) -> dict:
    if yf is None or not symbols:
        return {}
    try:
        raw = yf.download(
            tickers=list(symbols), period="5d", interval="1d",
            auto_adjust=False, group_by="ticker", threads=True, progress=False,
        )
    except Exception:
        return {s: pd.DataFrame() for s in symbols}
    today = pd.to_datetime(today_str).date()
    per_symbol = _split_yfinance_bulk_result(raw, symbols)
    return {
        s: (df[df["Date"] >= today].reset_index(drop=True) if not df.empty else df)
        for s, df in per_symbol.items()
    }


def resolve_price_source(now_dt=None) -> str:
    mode = st.session_state.get("price_source_mode", "自動")
    if mode in ["WebSocket", "Yfinance"]:
        return mode
    if now_dt is None:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now_dt = datetime.now(ZoneInfo("Asia/Taipei"))
    cutoff = now_dt.replace(hour=AUTO_YFINANCE_AFTER_HOUR, minute=AUTO_YFINANCE_AFTER_MINUTE, second=0, microsecond=0)
    return "Yfinance" if now_dt >= cutoff else "WebSocket"


def download_stock_data_by_source(
    symbol: str, _sdk, source: str, today_str: str,
    history_map: dict = None, yf_today_map: dict = None,
):
    history_map = history_map or {}
    yf_today_map = yf_today_map or {}

    def _combine(history_df, today_df):
        frames = [d for d in [history_df, today_df] if d is not None and not d.empty]
        if not frames:
            return pd.DataFrame()
        combined = pd.concat(frames, ignore_index=True)
        if "Date" in combined.columns:
            combined = combined.drop_duplicates(subset=["Date"], keep="last").sort_values("Date").reset_index(drop=True)
        return combined

    if source == "Yfinance":
        history_df = history_map.get(symbol)
        if history_df is None:
            history_df = download_stock_data_yfinance_history(symbol, today_str)
        today_df = yf_today_map.get(symbol)
        if today_df is None:
            today_df = download_stock_data_yfinance_today(symbol, today_str)
        df = _combine(history_df, today_df)
        if not df.empty:
            return df
        if _sdk is not None:
            return download_stock_data(symbol, _sdk)
        return pd.DataFrame()

    # ===== WebSocket：盤中混合模式 =====
    history_df = history_map.get(symbol)
    if history_df is None:
        history_df = download_stock_data_yfinance_history(symbol, today_str)
    today_df = download_stock_data_fubon_today(symbol, _sdk, today_str) if _sdk is not None else pd.DataFrame()
    df = _combine(history_df, today_df)
    if not df.empty:
        return df
    return download_stock_data(symbol, _sdk)


def get_last_price_by_source(symbol: str, df, _sdk, source: str):
    if source == "Yfinance":
        if df is not None and not df.empty and "Close" in df.columns:
            price = pd.to_numeric(df["Close"], errors="coerce").dropna()
            if not price.empty:
                return float(price.iloc[-1])
        if _sdk is not None:
            return get_last_price(symbol, df, _sdk)
        raise ValueError("yfinance 無法取得價格")
    return get_last_price(symbol, df, _sdk)


@st.cache_data(ttl=86400)
def get_stock_name(symbol: str, _sdk) -> str:
    name_map = load_stock_name_map(STOCK_NAME_FILE)
    if symbol in name_map:
        return name_map[symbol]
    fubon_symbol = str(symbol).split(".")[0]
    if _sdk is not None:
        try:
            res = _sdk.marketdata.rest_client.stock.historical.stats(symbol=fubon_symbol)
            if res and "name" in res:
                return res["name"].strip()
        except Exception:
            pass
    return fubon_symbol


# ===== 輔助工具 =====
def yahoo_quote_url(symbol: str) -> str:
    fubon_symbol = str(symbol).split(".")[0]
    return f"https://tw.stock.yahoo.com/quote/{fubon_symbol}"


def symbol_to_code(symbol: str) -> str:
    return str(symbol).split(".")[0]


def contains_cjk(text) -> bool:
    if text is None:
        return False
    s = str(text)
    return any(
        ("\u4e00" <= ch <= "\u9fff") or
        ("\u3400" <= ch <= "\u4dbf") or
        ("\uf900" <= ch <= "\ufaff")
        for ch in s
    )


def apply_excel_fonts(workbook):
    from openpyxl.styles import Font
    chinese_font_name = "Microsoft JhengHei"
    english_font_name = "Calibri"
    for worksheet in workbook.worksheets:
        for row in worksheet.iter_rows():
            for cell in row:
                if cell.value is None:
                    cell.font = Font(name=english_font_name)
                elif contains_cjk(cell.value):
                    cell.font = Font(name=chinese_font_name)
                else:
                    cell.font = Font(name=english_font_name)


def format_color(val):
    if isinstance(val, (int, float)):
        if val > 0:
            return f"🔴 +{val:.2f}%"
        elif val < 0:
            return f"🟢 {val:.2f}%"
        else:
            return f"{val:.2f}%"
    return val


def format_volume(val):
    try:
        return f"{float(val):,.1f}"
    except Exception:
        return val


def ensure_fubon_session_state():
    """讓任何一個頁面（包含子頁面）都能獨立初始化富邦連線相關的 session_state，
    避免使用者直接從子頁面進站時 key 不存在而噴錯。"""
    if "fubon_sdk" not in st.session_state:
        st.session_state.fubon_sdk = None
    if "fubon_logged_in" not in st.session_state:
        st.session_state.fubon_logged_in = False
    if "price_source_mode" not in st.session_state:
        st.session_state.price_source_mode = "自動"


def render_fubon_login_sidebar():
    """共用的富邦登入元件，跟主程式行為一致，任何頁面都能掛在側邊欄使用。
    因為 session_state 在整個瀏覽器 session 內跨頁共享，只要有一頁登入過，
    其他頁面就能直接沿用已連線的 SDK，不需要重複登入。"""
    ensure_fubon_session_state()
    st.sidebar.markdown("## 🔑 富邦 API 設定 (Fubon Neo)")

    if st.session_state.fubon_logged_in:
        st.sidebar.success("✅ 富邦 API 已成功連線")
        if st.sidebar.button("登出 / 重新連線", use_container_width=True, key="qmd_fubon_logout_btn"):
            st.session_state.fubon_sdk = None
            st.session_state.fubon_logged_in = False
            st.rerun()
        return

    try:
        from fubon_neo.sdk import FubonSDK
    except ImportError:
        st.sidebar.error("請先安裝富邦 API 套件：執行 `pip install fubon-neo`")
        return

    try:
        fubon_secrets = st.secrets["fubon"]
        pfx_base64 = fubon_secrets["pfx_base64"]
    except KeyError:
        st.sidebar.error("❌ 找不到 Streamlit Secrets 中的 pfx_base64 憑證資料。")
        return

    st.sidebar.info("請輸入富邦證券登入資訊")
    f_id = st.sidebar.text_input("身分證字號", key="qmd_f_id_input")
    f_pw = st.sidebar.text_input("富邦登入密碼", key="qmd_f_pw_input", type="password")
    f_cert_pw = st.sidebar.text_input("憑證密碼", key="qmd_f_cert_pw_input", type="password")

    if st.sidebar.button("連線行情伺服器", use_container_width=True, key="qmd_fubon_login_btn"):
        if not f_id or not f_pw or not f_cert_pw:
            st.sidebar.warning("請填寫完整的身分證字號與密碼！")
        else:
            try:
                import base64
                temp_cert_path = "temp_cloud_cert.pfx"
                with open(temp_cert_path, "wb") as f:
                    f.write(base64.b64decode(pfx_base64))
                with st.spinner("連線富邦 API 中..."):
                    sdk = FubonSDK()
                    sdk.login(f_id.strip().upper(), f_pw, temp_cert_path, f_cert_pw)
                    sdk.init_realtime()
                    st.session_state.fubon_sdk = sdk
                    st.session_state.fubon_logged_in = True
                st.sidebar.success("✅ 富邦 API 連線成功！")
                st.rerun()
            except Exception as e:
                st.sidebar.error(f"❌ 登入失敗: {e}")


def render_price_source_selector_sidebar(now_dt):
    """精簡版資料來源開關，跟主程式共用同一個 session_state key，兩頁設定互通。"""
    ensure_fubon_session_state()
    active_source = resolve_price_source(now_dt)
    source_mode = st.session_state.get("price_source_mode", "自動")
    with st.sidebar.expander("🧭 資料來源開關", expanded=False):
        st.markdown(
            f"""
            <div style="background:#2f4563; color:#35a8ff; border-radius:8px; padding:14px 16px; line-height:1.8; font-weight:600;">
            目前資料來源模式：{source_mode}；<br>
            實際使用：{active_source}
            </div>
            """,
            unsafe_allow_html=True,
        )
        st.caption(
            f"自動模式邏輯：{AUTO_YFINANCE_AFTER_HOUR}:{AUTO_YFINANCE_AFTER_MINUTE:02d} 前 → "
            f"富邦WebSocket(今日) + Yfinance(今日以前) 混合資料；"
            f"{AUTO_YFINANCE_AFTER_HOUR}:{AUTO_YFINANCE_AFTER_MINUTE:02d} 後 → 全部改用 Yfinance。"
        )
        mode_options = ["自動", "WebSocket", "Yfinance"]
        selected_mode = st.radio(
            "資料來源開關",
            options=mode_options,
            index=mode_options.index(source_mode) if source_mode in mode_options else 0,
            horizontal=True,
            key="qmd_price_source_mode_radio",
            label_visibility="collapsed",
        )
        if selected_mode != source_mode:
            st.session_state.price_source_mode = selected_mode
            st.cache_data.clear()
            st.rerun()
    return active_source
