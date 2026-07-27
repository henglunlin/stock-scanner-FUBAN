"""
common_fubon.py
================
台股資料源模組：富邦(Fubon Neo) WebSocket/REST 行情 + Yfinance 備援/批次抓取。
所有跟「怎麼拿到 K 線與即時價」有關的邏輯都集中在這裡，
掃描邏輯(signals/)與 UI(app.py) 都只呼叫這裡提供的函式，不用關心資料怎麼來的。
"""

import os
import re
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

# ===== 資料源相關常數 =====
REFRESH_SEC = 3
YFINANCE_HISTORY_CACHE_TTL_SEC = 60 * 60  # yfinance 今日以前歷史資料每小時更新一次
STOCK_NAME_FILE = "TWstocklistname2.txt"
STOCK_SCAN_FILE = "TWstocklistname2.txt"
FORCE_SCAN_ALL_STOCKS_FROM_FILE = True
ALL_STOCK_GROUP_NAME = "TWstocklistname2 全股票掃描"
AUTO_YFINANCE_AFTER_HOUR = 13
AUTO_YFINANCE_AFTER_MINUTE = 30

# ===== Fubon API 行情工具 =====
def _fetch_fubon_candles(symbol: str, _sdk, start_date, end_date) -> pd.DataFrame:
    """
    向富邦 API 抓取指定日期區間的日K線，統一整理成與 yfinance 對齊的欄位格式
    （Date/Open/High/Low/Close/Volume），方便後續與 Yfinance 資料合併串接。
    """
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
    """富邦完整90天歷史資料（僅在混合資料源都抓不到時作為備援使用，平常掃描不會走這條慢路徑）"""
    end_date = date.today()
    start_date = end_date - timedelta(days=90)
    return _fetch_fubon_candles(symbol, _sdk, start_date, end_date)

@st.cache_data(ttl=REFRESH_SEC)
def download_stock_data_fubon_today(symbol: str, _sdk, today_str: str):
    """
    🚀 加速重點：盤中(9:00-13:30)只跟富邦要『今日』單日K線，不再像過去一樣每次都要
    90天完整歷史。今日以前的資料改由 Yfinance 批次快取提供（見下方 bulk_download_yfinance_history），
    單檔富邦請求的資料量大幅縮小，掃描速度明顯提升。
    """
    if _sdk is None:
        return pd.DataFrame()
    today = date.today()
    return _fetch_fubon_candles(symbol, _sdk, today, today)

def normalize_ohlc(df):
    if df is None or df.empty:
        return pd.DataFrame()

    df = df.copy()
    # 保留日期欄位，讓趨勢線可以回報 P1/P2 是哪一天。
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
    if not os.path.exists(file_path):
        return symbols
    with open(file_path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip().replace("\ufeff", "").replace("\u3000", "")
            if not line:
                continue
            symbol = re.split(r"\s+", line, maxsplit=1)[0].strip().upper()
            if not re.match(r"^[0-9A-Z]+\.(TW|TWO)$", symbol):
                continue
            if symbol not in seen:
                seen.add(symbol)
                symbols.append(symbol)
    return symbols

def load_all_stock_group_from_file() -> dict:
    symbols = load_stock_symbols_from_file(STOCK_SCAN_FILE)
    return {ALL_STOCK_GROUP_NAME: symbols}

def _normalize_yfinance_ohlcv(df):
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

def download_stock_data_yfinance(symbol: str):
    today_str = datetime.now(ZoneInfo("Asia/Taipei")).strftime("%Y-%m-%d")
    history_df = download_stock_data_yfinance_history(symbol, today_str)
    today_df = download_stock_data_yfinance_today(symbol, today_str)

    frames = [df for df in [history_df, today_df] if df is not None and not df.empty]
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    if "Date" in df.columns:
        df = df.drop_duplicates(subset=["Date"], keep="last").sort_values("Date").reset_index(drop=True)
    return df.reset_index(drop=True)

def _split_yfinance_bulk_result(raw: pd.DataFrame, symbols: tuple) -> dict:
    """把 yf.download 多檔批次結果拆成 {symbol: 單檔DataFrame} 字典"""
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
    """
    🚀 加速重點：一次批次下載整批股票『今日以前』的歷史資料（yfinance 內部會自動平行抓取多檔），
    取代過去逐檔各打一次 API 的作法。全市場掃描時（可能上百檔股票），這能把歷史資料的
    網路請求次數從「N次」降為「1次」，是掃描速度提升最主要的來源。
    快取1小時，同一小時內重複掃描不會再次觸發下載。
    """
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
    """批次下載整批股票『今日』資料，供盤後(13:30後)全面改用Yfinance時使用"""
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
        now_dt = datetime.now(ZoneInfo("Asia/Taipei"))
    cutoff = now_dt.replace(hour=AUTO_YFINANCE_AFTER_HOUR, minute=AUTO_YFINANCE_AFTER_MINUTE, second=0, microsecond=0)
    return "Yfinance" if now_dt >= cutoff else "WebSocket"

def render_price_source_selector(now_dt):
    active_source = resolve_price_source(now_dt)
    source_mode = st.session_state.get("price_source_mode", "自動")
    with st.sidebar.expander("🧭 資料來源開關", expanded=True):
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
            key="price_source_mode_radio",
            label_visibility="collapsed",
        )
        if selected_mode != source_mode:
            st.session_state.price_source_mode = selected_mode
            st.cache_data.clear()
            st.rerun()
    return active_source

def download_stock_data_by_source(
    symbol: str, _sdk, source: str, today_str: str,
    history_map: dict = None, yf_today_map: dict = None,
):
    """
    依資料來源模式取得K線資料（邏輯維持不變，僅優化抓取方式加速）：
    - Yfinance：優先查表使用外部預先批次下載好的 history_map / yf_today_map（一次API呼叫換來的整批結果），
      查不到才退回單檔即時查詢（例如臨時加入、不在原批次清單中的股票）。
    - WebSocket（盤中9:00-13:30混合模式）：『今日以前』歷史資料一樣查表使用 Yfinance 批次快取，
      『今日』資料改成只跟富邦要當天單日K線（不再要90天），大幅減少富邦API的資料量與延遲。
    - 兩種模式都抓不到資料時，才退回最慢但最保險的富邦90天完整歷史。
    """
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
    # 混合來源都抓不到資料時，退回原本較慢的富邦90天完整歷史作為最終備援
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

