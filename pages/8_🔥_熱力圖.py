"""
8_🔥_熱力圖.py
================
台股熱力圖：族群 × 漲跌幅/資金流向 × 大小(市值或成交金額)

資料來源:
  - 族群: stock_groups.json (自訂，多對多標籤式) 或 stock_meta 資料表的官方產業別
  - 大小: stock_meta.SharesOutstanding × 現價 = 市值；沒有股本資料則退回用成交金額
  - 顏色: 漲跌幅 / institutional_trading 資料表的三大法人買賣超 / OBV 資金流向代理指標
  - 價格: 沿用 common_fubon.py 既有的「本地DB / Yfinance / 富邦WebSocket」三選一架構，
    跟主掃描器、模擬器共用同一套價格來源邏輯，不重新寫一套。

執行方式: 屬於多頁應用程式的其中一頁，跟著 streamlit run Home.py（或主入口）自動出現在側邊欄。
"""
import json
import os
import sqlite3
import time
from datetime import datetime, timedelta
from io import StringIO
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
import streamlit as st
from streamlit_echarts import st_pyecharts
from pyecharts.charts import TreeMap
from pyecharts import options as opts
from pyecharts.commons.utils import JsCode

import common_fubon as cf
import db_utils
import heatmap_utils

st.set_page_config(page_title="台股熱力圖", layout="wide")
st.title("🔥 台股熱力圖")

DB_PATH = "twse_ohlcv.db"
GROUPS_FILE = "stock_groups.json"
LIVE_MODE_WARN_THRESHOLD = 300  # 即時模式下逐檔打 API，股票數太多時提醒改用收盤模式
LABEL_MIN_AREA_FRACTION = 0.0012  # 格子面積佔整張熱力圖不到這個比例時，不顯示文字，只留顏色（改善2）

TWSE_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
TPEX_URL = "https://www.tpex.org.tw/web/stock/aftertrading/otc_quotes_no1430/stk_wn1430_result.php"
TWSE_T86_URL = "https://www.twse.com.tw/rwd/zh/fund/T86"
TPEX_INSTI_URL = "https://www.tpex.org.tw/web/stock/3insti/daily_trade/3itrade_hedge_result.php"
MOPS_URLS = {"上市": "https://mopsfin.twse.com.tw/opendata/t187ap03_L.csv", "上櫃": "https://mopsfin.twse.com.tw/opendata/t187ap03_O.csv"}
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json, text/plain, */*"}
INDUSTRY_CODE_MAP = {
    "01": "水泥工業", "02": "食品工業", "03": "塑膠工業", "04": "紡織纖維",
    "05": "電機機械", "06": "電器電纜", "08": "玻璃陶瓷", "09": "造紙工業",
    "10": "鋼鐵工業", "11": "橡膠工業", "12": "汽車工業", "13": "電子工業",
    "14": "建材營造", "15": "航運業", "16": "觀光餐旅", "17": "金融保險",
    "18": "貿易百貨", "19": "綜合", "20": "其他", "21": "化學工業",
    "22": "生技醫療", "23": "油電燃氣", "24": "半導體業",
    "25": "電腦及週邊設備業", "26": "光電業", "27": "通信網路業",
    "28": "電子零組件業", "29": "電子通路業", "30": "資訊服務業",
    "31": "其他電子業", "32": "文化創意業", "33": "農業科技業",
    "34": "電子商務業", "35": "綠能環保", "36": "數位雲端",
    "37": "運動休閒", "38": "居家生活", "80": "管理股票",
}


# ============================================================================
# 「🔄 刷新資料」用的抓取函式
# 寫法比照 pages/6_💻_Stock simulator.py 側邊欄「更新資料庫」的既有作法
# (自成一份、不 import update_db.py，維持跟現有頁面一致的慣例)，
# 分別對應熱力圖用到的三張表：ohlcv_data (收盤價) / institutional_trading (三大法人) / stock_meta (產業別股本)。
# ============================================================================
def _num(value) -> float:
    text = str(value).replace(",", "").strip()
    if text in {"", "-", "--", "---", "----", "None", "nan", "X"}:
        return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def _get_json(url: str, params: dict = None):
    resp = requests.get(url, params=params, headers=HEADERS, timeout=30, verify=False)
    resp.raise_for_status()
    return resp.json()


def _unique_columns(fields: list) -> list:
    seen, result = {}, []
    for field in fields:
        base = str(field).strip()
        seen[base] = seen.get(base, 0) + 1
        result.append(base if seen[base] == 1 else f"{base}_{seen[base]}")
    return result


def fetch_twse_ohlcv_daily(report_date: str) -> pd.DataFrame:
    try:
        payload = _get_json(TWSE_URL, {"date": report_date, "type": "ALLBUT0999", "response": "json"})
        if str(payload.get("stat", "")) not in {"", "OK"}:
            return pd.DataFrame()
        for table in payload.get("tables", []):
            columns = _unique_columns(table.get("fields", []))
            if "證券代號" in columns and "收盤價" in columns:
                df = pd.DataFrame(table.get("data", []), columns=columns)
                df = df[["證券代號", "證券名稱", "開盤價", "最高價", "最低價", "收盤價", "成交股數"]].copy()
                df.columns = ["SecurityCode", "SecurityName", "Open", "High", "Low", "Close", "Volume"]
                df["SecurityCode"] = df["SecurityCode"].astype(str).str.strip()
                df["SecurityName"] = df["SecurityName"].astype(str).str.strip()
                for col in ["Open", "High", "Low", "Close", "Volume"]:
                    df[col] = df[col].map(_num)
                df.insert(0, "Date", pd.to_datetime(report_date, format="%Y%m%d").date())
                df.insert(1, "Market", "上市")
                return df[df["Close"] > 0].drop_duplicates("SecurityCode")
        return pd.DataFrame()
    except Exception:
        return pd.DataFrame()


def fetch_tpex_ohlcv_daily(report_date: str) -> pd.DataFrame:
    dt = datetime.strptime(report_date, "%Y%m%d")
    roc_date = f"{dt.year - 1911}/{dt.strftime('%m/%d')}"
    try:
        payload = _get_json(TPEX_URL, {"l": "zh-tw", "d": roc_date, "se": "EW", "o": "json"})
        data_list = []
        if payload.get("tables"):
            for table in payload["tables"]:
                data_list.extend(table.get("data", []))
        elif "aaData" in payload:
            data_list = payload["aaData"]
        if not data_list:
            return pd.DataFrame()
        rows = []
        for row in data_list:
            if len(row) >= 8:
                code = str(row[0]).strip()
                if len(code) > 6:
                    continue
                close_p = _num(row[2])
                if close_p > 0:
                    rows.append({
                        "Date": dt.date(), "Market": "上櫃", "SecurityCode": code, "SecurityName": str(row[1]).strip(),
                        "Open": _num(row[4]), "High": _num(row[5]), "Low": _num(row[6]), "Close": close_p, "Volume": _num(row[7]),
                    })
        df = pd.DataFrame(rows)
        return df.drop_duplicates("SecurityCode") if not df.empty else df
    except Exception:
        return pd.DataFrame()


def save_ohlcv_to_database(db_path: str, df: pd.DataFrame):
    if df.empty:
        return
    with sqlite3.connect(db_path, timeout=30.0) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        db_utils.ensure_indexes(conn)
        for d in df["Date"].unique():
            for m in df["Market"].unique():
                conn.execute("DELETE FROM ohlcv_data WHERE Date = ? AND Market = ?", (str(d), m))
        df.to_sql("ohlcv_data", conn, if_exists="append", index=False)
        conn.execute("PRAGMA journal_mode=DELETE;")  # 寫回 repo 前切回 DELETE 模式，避免留下 -wal/-shm 側車檔
        conn.commit()


def fetch_twse_institutional_daily(report_date: str) -> pd.DataFrame:
    try:
        payload = _get_json(TWSE_T86_URL, {"response": "json", "date": report_date, "selectType": "ALL"})
        if str(payload.get("stat", "")) not in {"", "OK"}:
            return pd.DataFrame()
        fields, data = payload.get("fields", []), payload.get("data", [])
        if not fields or not data:
            return pd.DataFrame()
        df = pd.DataFrame(data, columns=fields)

        def find_col(keyword):
            for c in df.columns:
                if keyword in c:
                    return c
            return None

        code_col, name_col = find_col("證券代號"), find_col("證券名稱")
        foreign_col = find_col("外陸資買賣超股數(不含外資自營商)") or find_col("外陸資買賣超")
        trust_col = find_col("投信買賣超股數") or find_col("投信買賣超")
        dealer_col = find_col("自營商買賣超股數") or find_col("自營商買賣超")
        total_col = find_col("三大法人買賣超股數合計") or find_col("三大法人買賣超")
        if not code_col:
            return pd.DataFrame()

        out = pd.DataFrame({
            "SecurityCode": df[code_col].astype(str).str.strip(),
            "SecurityName": df[name_col].astype(str).str.strip() if name_col else "",
            "ForeignNet": df[foreign_col].map(_num) if foreign_col else 0.0,
            "TrustNet": df[trust_col].map(_num) if trust_col else 0.0,
            "DealerNet": df[dealer_col].map(_num) if dealer_col else 0.0,
        })
        out["TotalNet"] = df[total_col].map(_num) if total_col else out["ForeignNet"] + out["TrustNet"] + out["DealerNet"]
        out.insert(0, "Date", pd.to_datetime(report_date, format="%Y%m%d").date())
        out.insert(1, "Market", "上市")
        return out[out["SecurityCode"].str.len() <= 6].drop_duplicates("SecurityCode")
    except Exception:
        return pd.DataFrame()


def fetch_tpex_institutional_daily(report_date: str) -> pd.DataFrame:
    dt = datetime.strptime(report_date, "%Y%m%d")
    roc_date = f"{dt.year - 1911}/{dt.strftime('%m/%d')}"
    try:
        payload = _get_json(TPEX_INSTI_URL, {"l": "zh-tw", "se": "AL", "t": "D", "d": roc_date, "o": "json"})
        data_list = []
        if payload.get("tables"):
            for table in payload["tables"]:
                data_list.extend(table.get("data", []))
        elif "aaData" in payload:
            data_list = payload["aaData"]
        if not data_list:
            return pd.DataFrame()
        rows = []
        for row in data_list:
            if len(row) < 9:
                continue
            code = str(row[0]).strip()
            if len(code) > 6:
                continue
            rows.append({
                "Date": dt.date(), "Market": "上櫃", "SecurityCode": code, "SecurityName": str(row[1]).strip(),
                "ForeignNet": _num(row[4]),
                "TrustNet": _num(row[8]) if len(row) > 8 else 0.0,
                "DealerNet": _num(row[-2]) if len(row) > 9 else 0.0,
                "TotalNet": _num(row[-1]),
            })
        df = pd.DataFrame(rows)
        return df.drop_duplicates("SecurityCode") if not df.empty else df
    except Exception:
        return pd.DataFrame()


def save_institutional_to_database(db_path: str, df: pd.DataFrame):
    if df.empty:
        return
    with sqlite3.connect(db_path, timeout=30.0) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS institutional_trading (
                Date TEXT, Market TEXT, SecurityCode TEXT, SecurityName TEXT,
                ForeignNet REAL, TrustNet REAL, DealerNet REAL, TotalNet REAL,
                PRIMARY KEY (Date, SecurityCode)
            )
        """)
        for d in df["Date"].unique():
            conn.execute("DELETE FROM institutional_trading WHERE Date = ?", (str(d),))
        df.to_sql("institutional_trading", conn, if_exists="append", index=False)
        conn.execute("PRAGMA journal_mode=DELETE;")
        conn.commit()


def fetch_and_save_stock_meta(db_path: str) -> int:
    frames = []
    for market_label, url in MOPS_URLS.items():
        try:
            resp = requests.get(url, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            resp.encoding = "utf-8-sig"
            df = pd.read_csv(StringIO(resp.text), dtype=str)
        except Exception:
            continue
        keep = {
            "公司代號": "SecurityCode", "公司名稱": "SecurityName", "公司簡稱": "ShortName",
            "產業別": "IndustryCode", "普通股每股面額": "ParValueRaw",
            "實收資本額": "PaidInCapital", "已發行普通股數或TDR原股發行股數": "SharesOutstanding",
        }
        if any(c not in df.columns for c in keep):
            continue
        out = df[list(keep.keys())].rename(columns=keep)
        out["SecurityCode"] = out["SecurityCode"].astype(str).str.strip()
        out["SecurityName"] = out["SecurityName"].astype(str).str.strip()
        out["ShortName"] = out["ShortName"].astype(str).str.strip()
        out["IndustryCode"] = out["IndustryCode"].astype(str).str.strip()
        out["IndustryName"] = out["IndustryCode"].map(INDUSTRY_CODE_MAP).fillna("未分類")
        out["PaidInCapital"] = out["PaidInCapital"].map(_num)
        out["SharesOutstanding"] = out["SharesOutstanding"].map(_num)
        out["ParValue"] = out["ParValueRaw"].str.extract(r"([\d.]+)").astype(float)
        out = out.drop(columns=["ParValueRaw"])
        out.insert(1, "Market", market_label)
        frames.append(out[out["SecurityCode"].str.len() <= 6].drop_duplicates("SecurityCode"))
        time.sleep(1)

    if not frames:
        return 0
    combined = pd.concat(frames, ignore_index=True).drop_duplicates("SecurityCode")
    combined["UpdateDate"] = datetime.now(ZoneInfo("Asia/Taipei")).strftime("%Y-%m-%d")
    with sqlite3.connect(db_path, timeout=30.0) as conn:
        combined.to_sql("stock_meta", conn, if_exists="replace", index=False)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_stock_meta_code ON stock_meta(SecurityCode)")
        conn.commit()
    return len(combined)


cf.ensure_fubon_session_state()
cf.render_fubon_login_sidebar()

tw_now = datetime.now(ZoneInfo("Asia/Taipei"))
active_source = cf.render_price_source_selector(tw_now)


# --------------------------------------------------------------------------
# 資料載入 (族群 / 公司基本資料 / 法人買賣超)
# --------------------------------------------------------------------------
@st.cache_data(ttl=3600)
def load_custom_groups() -> dict:
    if not os.path.exists(GROUPS_FILE):
        return {}
    with open(GROUPS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


@st.cache_data(ttl=3600)
def load_stock_meta() -> pd.DataFrame:
    if not os.path.exists(DB_PATH):
        return pd.DataFrame()
    try:
        with sqlite3.connect(DB_PATH) as conn:
            return pd.read_sql("SELECT * FROM stock_meta", conn)
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=1800)
def load_institutional_latest() -> pd.DataFrame:
    if not os.path.exists(DB_PATH):
        return pd.DataFrame()
    try:
        with sqlite3.connect(DB_PATH) as conn:
            latest = pd.read_sql(
                "SELECT MAX(Date) AS d FROM institutional_trading", conn
            )["d"].iloc[0]
            if not latest:
                return pd.DataFrame()
            return pd.read_sql(
                "SELECT * FROM institutional_trading WHERE Date = ?", conn, params=[latest]
            )
    except Exception:
        return pd.DataFrame()


custom_groups = load_custom_groups()
stock_meta = load_stock_meta()
insti_latest = load_institutional_latest()

with st.sidebar:
    st.markdown("---")
    st.subheader("🔥 熱力圖設定")
    group_source = st.radio(
        "族群來源", ["自訂族群 (stock_groups.json)", "TWSE 官方產業別"], key="hm_group_source"
    )
    color_metric = st.radio(
        "顏色依據", ["漲跌幅 (%)", "三大法人買賣超", "資金流向代理指標 (OBV)"], key="hm_color_metric"
    )
    data_mode = st.radio("資料模式", ["收盤資料 (較快)", "盤中即時 (較慢)"], key="hm_data_mode")
    if data_mode.startswith("收盤"):
        selected_date = st.date_input(
            "資料日期", value=tw_now.date(), max_value=tw_now.date(), key="hm_selected_date",
            help="選擇要看哪一天的收盤資料。若當天不是交易日、或資料庫還沒同步到那天，"
                 "會自動往前抓最近一個有資料的交易日（下方會顯示實際抓到的日期）。",
        )
    else:
        selected_date = tw_now.date()
    selected_date_str = selected_date.strftime("%Y-%m-%d")
    size_metric = st.radio("方塊大小", ["市值 (股本 × 股價)", "成交金額"], key="hm_size_metric")
    min_group_size = st.slider(
        "族群最少股票數（改善5）", min_value=1, max_value=10, value=3, key="hm_min_group_size",
        help="族群裡股票數低於這個數字時，會被合併進「其他」分類，避免畫面被切成一堆看不清楚的碎格子。",
    )

    st.markdown("---")
    with st.expander("🔄 刷新資料", expanded=False):
        refresh_target_date = st.date_input(
            "刷新日期", value=tw_now.date(), max_value=tw_now.date(), key="hm_refresh_date",
            help="要抓哪一天的收盤價／三大法人買賣超。盤中(收盤前)抓當天通常會是空的，屬正常現象。",
        )
        refresh_date_str8 = refresh_target_date.strftime("%Y%m%d")

        if st.button("① 更新收盤價 (TWSE/TPEX 官方 API)", use_container_width=True, key="hm_refresh_ohlcv_btn"):
            with st.spinner(f"正在抓取 {refresh_target_date} 收盤價..."):
                twse_df = fetch_twse_ohlcv_daily(refresh_date_str8)
                time.sleep(1)
                tpex_df = fetch_tpex_ohlcv_daily(refresh_date_str8)
                daily_df = pd.concat([twse_df, tpex_df], ignore_index=True)
                if not daily_df.empty:
                    st.cache_data.clear()
                    save_ohlcv_to_database(DB_PATH, daily_df)
                    st.success(f"收盤價更新成功！上市 {len(twse_df)} 檔／上櫃 {len(tpex_df)} 檔")
                else:
                    st.warning(f"{refresh_target_date} 查無交易資料（可能是假日或尚未收盤）。")

        if st.button("② 更新三大法人買賣超", use_container_width=True, key="hm_refresh_insti_btn"):
            with st.spinner(f"正在抓取 {refresh_target_date} 三大法人買賣超..."):
                twse_insti = fetch_twse_institutional_daily(refresh_date_str8)
                time.sleep(1)
                tpex_insti = fetch_tpex_institutional_daily(refresh_date_str8)
                daily_insti = pd.concat([twse_insti, tpex_insti], ignore_index=True)
                if not daily_insti.empty:
                    st.cache_data.clear()
                    save_institutional_to_database(DB_PATH, daily_insti)
                    st.success(f"三大法人買賣超更新成功！上市 {len(twse_insti)} 檔／上櫃 {len(tpex_insti)} 檔")
                else:
                    st.warning(f"{refresh_target_date} 查無三大法人資料（可能是假日或尚未收盤）。")

        st.caption("③ 產業別／股本資料屬於低頻資料（除非增資、私募），不用常常更新：")
        if st.button("③ 更新產業別/股本資料", use_container_width=True, key="hm_refresh_meta_btn"):
            with st.spinner("正在抓取 MOPS 公司基本資料（上市+上櫃）..."):
                count = fetch_and_save_stock_meta(DB_PATH)
                if count > 0:
                    st.cache_data.clear()
                    st.success(f"產業別/股本資料更新成功！共 {count} 檔（上市+上櫃）。")
                else:
                    st.warning("抓取失敗，請稍後再試（可能是 MOPS 端點暫時異常）。")


# --------------------------------------------------------------------------
# 決定掃描範圍: Group -> [SecurityCode, ...]
# 自訂族群沿用掃描器主程式原本的邏輯：一檔股票可以同時出現在多個族群，
# 不強迫只能歸屬一個族群（stock_groups.json 本身就是多對多標籤式設計）。
# --------------------------------------------------------------------------
if group_source.startswith("自訂"):
    group_map = {g: [str(s).split(".")[0] for s in syms] for g, syms in custom_groups.items()}
    if not group_map:
        st.warning(f"讀不到 {GROUPS_FILE}，請確認檔案存在於同一個資料夾。")
else:
    if stock_meta.empty:
        st.warning("找不到 stock_meta 資料表，請先在有網路的環境執行一次 `python fetch_stock_meta.py` 建立官方產業別資料。")
        group_map = {}
    else:
        group_map = {
            industry: sub["SecurityCode"].tolist()
            for industry, sub in stock_meta.groupby("IndustryName")
        }

all_codes = sorted({c for codes in group_map.values() for c in codes})

if not all_codes:
    st.stop()

# 改善5：股票數太少的族群合併成「其他」，避免畫面被切成一堆看不清楚的碎格子
if group_map:
    merged_group_map, leftover_codes = {}, []
    for g, codes in group_map.items():
        if len(codes) < min_group_size:
            leftover_codes.extend(codes)
        else:
            merged_group_map[g] = codes
    if leftover_codes:
        merged_group_map["其他"] = sorted(set(leftover_codes))
    group_map = merged_group_map

if data_mode.startswith("盤中即時") and len(all_codes) > LIVE_MODE_WARN_THRESHOLD:
    st.warning(
        f"目前範圍共 {len(all_codes)} 檔股票，即時模式是逐檔打報價 API，數量較多時會偏慢、"
        f"也容易被來源端限流。建議：TWSE 全官方族群模式先用「收盤資料」，"
        f"想看盤中即時變化時改用範圍較小的「自訂族群」。"
    )

st.caption(
    f"掃描範圍：{len(group_map)} 個族群、共 {len(all_codes)} 檔股票"
    f"（{data_mode}"
    + (f"，日期：{selected_date_str}" if data_mode.startswith("收盤") else f"，價格來源：{active_source}")
    + "）"
)


# --------------------------------------------------------------------------
# 抓價格資料
# --------------------------------------------------------------------------
today_str = tw_now.strftime("%Y-%m-%d")


@st.cache_data(ttl=600)
def load_closing_snapshot(codes: tuple, as_of_date: str) -> dict:
    """
    收盤模式專用：直接從本地 DB 撈「不晚於 as_of_date」的最後兩個交易日資料，用來算漲跌幅。

    不能沿用 common_fubon.bulk_download_db_history() —— 那支函式內部寫死用
    「執行當下的系統日期」過濾 (df[df['Date'] < today])，不管資料庫裡實際存了哪天的資料，
    永遠會排除「今天」這一筆，導致熱力圖看起來永遠慢一天。這裡改成完全依照使用者
    指定的 as_of_date 為準，資料庫裡有到哪天就用到哪天。
    """
    if not os.path.exists(DB_PATH) or not codes:
        return {}, None
    placeholders = ",".join("?" * len(codes))
    query = f"""
        SELECT Date, SecurityCode, Open, High, Low, Close, Volume
        FROM ohlcv_data
        WHERE SecurityCode IN ({placeholders}) AND Date <= ?
    """
    try:
        with sqlite3.connect(DB_PATH) as conn:
            df = pd.read_sql(query, conn, params=list(codes) + [as_of_date])
    except Exception:
        return {}, None

    if df.empty:
        return {}, None

    df["Date"] = pd.to_datetime(df["Date"]).dt.date
    latest_available_date = df["Date"].max()

    result = {}
    for code, sub in df.groupby("SecurityCode"):
        sub = sub.sort_values("Date").tail(2).reset_index(drop=True)
        result[code] = sub[["Date", "Open", "High", "Low", "Close", "Volume"]]
    return result, latest_available_date


symbols = tuple(f"{c}.TW" for c in all_codes)

if data_mode.startswith("收盤"):
    price_data, latest_available_date = load_closing_snapshot(tuple(all_codes), selected_date_str)
    if latest_available_date is not None and latest_available_date != selected_date:
        st.info(f"資料庫裡沒有 {selected_date_str} 的資料，已自動顯示最近一個有資料的交易日：{latest_available_date}")
    elif latest_available_date is None:
        st.warning("資料庫裡完全查不到這個範圍的股票資料，請確認 twse_ohlcv.db 是否已同步。")
else:
    yf_today_map = (
        cf.bulk_download_yfinance_today(symbols, today_str) if active_source == "Yfinance" else {}
    )
    price_data = {}
    progress = st.progress(0.0, text="正在抓取即時價格...")
    for i, code in enumerate(all_codes):
        sym = f"{code}.TW"
        try:
            df = cf.download_stock_data_by_source(
                sym, st.session_state.fubon_sdk, active_source, today_str, yf_today_map=yf_today_map,
            )
        except Exception:
            df = pd.DataFrame()
        price_data[code] = df
        if i % 20 == 0:
            progress.progress(min(1.0, i / max(1, len(all_codes))), text=f"正在抓取即時價格... ({i}/{len(all_codes)})")
    progress.empty()


# --------------------------------------------------------------------------
# 組成每檔股票的漲跌幅 / 成交金額 / 市值 / 資金流向欄位
# --------------------------------------------------------------------------
insti_map = insti_latest.set_index("SecurityCode")["TotalNet"].to_dict() if not insti_latest.empty else {}
meta_map = stock_meta.set_index("SecurityCode").to_dict("index") if not stock_meta.empty else {}

rows = []
for code in all_codes:
    df = price_data.get(code)
    if df is None or df.empty or len(df) < 2:
        continue

    pct = heatmap_utils.pct_change_today(df)
    last_close = float(df["Close"].iloc[-1]) if "Close" in df.columns else 0.0
    last_vol = float(df["Volume"].iloc[-1]) if "Volume" in df.columns else 0.0
    trade_value = last_close * last_vol * 1000  # Volume 單位為「張」，1張=1000股

    meta = meta_map.get(code, {})
    shares = meta.get("SharesOutstanding") or 0.0
    market_cap = shares * last_close
    full_name = meta.get("SecurityName") or code
    short_name = meta.get("ShortName") or full_name

    rows.append({
        "SecurityCode": code,
        "ShortName": short_name,
        "FullName": full_name,
        "Close": last_close,
        "PctChange": pct,
        "TradeValue": trade_value,
        "MarketCap": market_cap if market_cap > 0 else trade_value,  # 沒股本資料就退回用成交金額頂替
        "MoneyFlowProxy": heatmap_utils.money_flow_proxy(df),
        "InstiNet": insti_map.get(code),
    })

df_all = pd.DataFrame(rows)
if df_all.empty:
    st.warning("沒有抓到任何股票的價格資料，請確認資料來源設定，或稍後再試一次。")
    st.stop()

# 展開成 (族群, 股票) 長表 —— 同一檔股票若屬於多個自訂族群，會在每個族群底下各出現一次
records = []
for group, codes in group_map.items():
    sub = df_all[df_all["SecurityCode"].isin(codes)]
    for _, r in sub.iterrows():
        rec = r.to_dict()
        rec["Group"] = group
        records.append(rec)

df_plot = pd.DataFrame(records)
if df_plot.empty:
    st.warning("目前族群清單裡的股票都沒有抓到資料。")
    st.stop()


# --------------------------------------------------------------------------
# 決定顏色欄位與大小欄位
# --------------------------------------------------------------------------
if color_metric.startswith("三大法人"):
    if insti_latest.empty:
        st.info("資料庫裡目前沒有三大法人買賣超資料，請先在有網路的環境執行 `python fetch_institutional_trading.py`，這裡暫時改用漲跌幅上色。")
        color_col, color_label = "PctChange", "漲跌幅(%)"
    else:
        df_plot["InstiNet"] = df_plot["InstiNet"].fillna(0)
        color_col, color_label = "InstiNet", "三大法人買賣超(股)"
elif color_metric.startswith("資金流向代理"):
    color_col, color_label = "MoneyFlowProxy", "資金流向代理分數(元)"
else:
    color_col, color_label = "PctChange", "漲跌幅(%)"

size_col = "MarketCap" if size_metric.startswith("市值") else "TradeValue"
df_plot[size_col] = df_plot[size_col].clip(lower=1)  # treemap 面積不可為 0 或負值

# 改善1：面積開根號壓縮 —— 台積電市值(~26兆)跟其他個股差了兩三個數量級，
# 純線性大小會讓它一檔就佔掉快一半畫面，其他族群全部被壓成看不清楚的細條。
# 開根號後，極端值跟中位數個股的「視覺面積差距」會被大幅壓縮，其他股票才有機會被看見。
df_plot["AreaValue"] = np.sqrt(df_plot[size_col])

# --------------------------------------------------------------------------
# 顏色計算：把 color_col 的值換算成 hex 色碼 (台股慣例：紅漲、綠跌、白色=持平)
# 上下限用「分位數」而不是最大/最小值 —— 少數離群值 (例如漲停/跌停個股) 才不會把
# 其他正常漲跌 1~2% 的股票全部拉成幾乎無色的白色。
# 改善4：t 再開根號一次 (非線性)，人眼對顏色深淺的感知本來就不是線性的，
# 這樣中段漲跌幅 (例如 1~3%) 也會有明顯可辨識的顏色差異，不用等到接近漲跌停才看得出紅綠差別。
# --------------------------------------------------------------------------
def color_scale_cap(series: pd.Series) -> float:
    cap = series.abs().quantile(0.9)
    if pd.isna(cap) or cap <= 0:
        cap = series.abs().max()
    return max(float(cap), 1e-9)


def pct_to_color(value: float, cap: float) -> str:
    v = max(-cap, min(cap, value))
    t = (abs(v) / cap) ** 0.5  # 開根號讓中段差異也看得出來 (改善4)
    if v >= 0:  # 紅
        r = int(255 - t * (255 - 192))
        g = int(255 - t * (255 - 57))
        b = int(255 - t * (255 - 43))
    else:  # 綠
        r = int(255 - t * (255 - 30))
        g = int(255 - t * (255 - 132))
        b = int(255 - t * (255 - 73))
    return f"#{r:02x}{g:02x}{b:02x}"


color_cap = color_scale_cap(df_plot[color_col])
df_plot["NodeColor"] = df_plot[color_col].apply(lambda v: pct_to_color(v, color_cap))

# 改善2：格子標籤改用「公司簡稱」而不是「股份有限公司」全名，全名留給 tooltip 顯示
df_plot["Label"] = df_plot.apply(
    lambda r: f"{r['SecurityCode']} {r['ShortName']}\n{r['PctChange']:+.2f}%", axis=1
)

# 改善2（續）：格子面積佔整張圖不到 LABEL_MIN_AREA_FRACTION 的，不顯示文字，只留顏色，
# 避免小格子文字擠在一起、被截斷看不清楚；完整資訊留給 tooltip (改善6) 補回來。
total_area = df_plot["AreaValue"].sum()
df_plot["ShowLabel"] = (df_plot["AreaValue"] / total_area) >= LABEL_MIN_AREA_FRACTION


# --------------------------------------------------------------------------
# 組成 ECharts Treemap 要的巢狀資料結構: [{name, children:[{name, value, itemStyle, label, ...}]}]
# 每個葉節點額外帶上 code / fullName / close / pctChange / 金額顯示字串等欄位，
# 專門給下面的 tooltip formatter 用 —— 這樣 tooltip 顯示的是完整資訊，不會跟著格子標籤一起被截斷 (改善6)。
# --------------------------------------------------------------------------
def build_tree_data(df: pd.DataFrame, group_map: dict) -> list:
    tree = []
    for group in group_map.keys():
        sub = df[df["Group"] == group]
        if sub.empty:
            continue
        children = []
        for _, r in sub.iterrows():
            node = {
                "name": r["Label"],
                "value": round(float(r["AreaValue"]), 4),
                "itemStyle": {"color": r["NodeColor"]},
                "label": {
                    "show": bool(r["ShowLabel"]),
                    "color": "#ffffff",
                    "textBorderColor": "rgba(0,0,0,0.55)",
                    "textBorderWidth": 2,
                    "fontSize": 12,
                },
                # tooltip 專用的完整資訊欄位
                "code": r["SecurityCode"],
                "fullName": r["FullName"],
                "close": round(float(r["Close"]), 2),
                "pctChange": round(float(r["PctChange"]), 2),
                "marketCapDisplay": heatmap_utils.format_twd(r["MarketCap"]),
                "tradeValueDisplay": heatmap_utils.format_twd(r["TradeValue"]),
            }
            if pd.notna(r["InstiNet"]):
                node["instiNetDisplay"] = heatmap_utils.format_shares_as_lots(r["InstiNet"])
            children.append(node)
        tree.append({"name": group, "children": children})
    return tree


tree_data = build_tree_data(df_plot, group_map)

# 改善6：tooltip 用自訂 JS formatter，顯示不受格子大小限制的完整資訊
# (代碼、全名、現價、漲跌幅、市值、成交金額，三大法人買賣超則視資料是否存在才顯示)
tooltip_formatter = JsCode(
    """
    function (params) {
        var d = params.data;
        if (!d || d.code === undefined) { return params.name; }
        var pct = d.pctChange >= 0 ? ('+' + d.pctChange) : d.pctChange;
        var lines = [
            d.code + ' ' + d.fullName,
            '現價: ' + d.close,
            '漲跌幅: ' + pct + '%',
            '市值: ' + d.marketCapDisplay,
            '成交金額: ' + d.tradeValueDisplay
        ];
        if (d.instiNetDisplay) {
            lines.push('三大法人買賣超: ' + d.instiNetDisplay);
        }
        return lines.join('<br/>');
    }
    """
)

treemap = (
    TreeMap(init_opts=opts.InitOpts(bg_color="#ffffff"))  # 改善3：固定白色背景，不受 App 深色主題影響
    .add(
        series_name="heatmap",
        data=tree_data,
        pos_top="0%", pos_bottom="0%", pos_left="0%", pos_right="0%",
        width="100%", height="100%",
        leaf_depth=2,
        roam=False,
        breadcrumb_opts=opts.TreeMapBreadcrumbOpts(is_show=False),
        levels=[
            opts.TreeMapLevelsOpts(),  # 根節點：不特別上色
            opts.TreeMapLevelsOpts(  # 族群節點：淺灰底 + 左上角族群名稱標題列，模仿 TradingView 的分組標頭
                treemap_itemstyle_opts=opts.TreeMapItemStyleOpts(
                    border_color="#f0f0f0", border_width=4, gap_width=4, color="#e8e8e8",
                ),
                upper_label_opts=opts.LabelOpts(
                    is_show=True, position="insideTopLeft", font_size=13, color="#333333",
                ),
            ),
            opts.TreeMapLevelsOpts(  # 個股節點
                treemap_itemstyle_opts=opts.TreeMapItemStyleOpts(border_color="#ffffff", border_width=1, gap_width=1),
            ),
        ],
        tooltip_opts=opts.TooltipOpts(formatter=tooltip_formatter),
    )
    .set_global_opts(
        title_opts=opts.TitleOpts(title=""),
        legend_opts=opts.LegendOpts(is_show=False),  # 改善3：關掉左上角「heatmap」圖例
    )
)

st_pyecharts(treemap, height="800px")

with st.expander("查看原始資料表"):
    show_cols = ["Group", "SecurityCode", "ShortName", "Close", "PctChange", "TradeValue", "MarketCap", "MoneyFlowProxy", "InstiNet"]
    st.dataframe(
        df_plot[show_cols].sort_values("PctChange", ascending=False),
        use_container_width=True, hide_index=True,
    )
