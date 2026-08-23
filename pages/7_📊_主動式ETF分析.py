# -*- coding: utf-8 -*-
"""
pages/7_📊_主動式ETF分析.py
============================
主動式ETF持股/買賣分析頁面。

資料來源：
  - etf_holdings.db (由 fetch_etf_holdings.py 經 GitHub Actions 每日排程寫入)
      etf_holdings         每日持股快照
      etf_holding_changes   每日持股異動 (加碼/減碼/新納入/全數賣出)
  - twse_ohlcv.db (掃描器既有的個股OHLCV資料庫，這裡只讀取，不寫入)
  - Database/active_etf_list.csv  全部主動式ETF代號/名稱對照
  - etf_watchlist_config.json     使用者勾選「重點關注」的ETF清單(可編輯)

功能：
  1. 追蹤ETF清單編輯 (勾選 + 存檔，可選擇同時提交到 GitHub)
  2. 指定ETF買賣狀況 (單一ETF在某天的加碼/減碼明細)
  3. 多數ETF共同買賣 (可調整「至少幾檔ETF同日異動」門檻)
  4. 個股K線 + ETF建倉標記 (簡化版K線圖，疊加ETF加碼/減碼標記)

⚠️ 這個頁面讀取的 etf_holdings.db 需要 fetch_etf_holdings.py 的排程先跑過
   至少一次才會有資料；剛部署、還沒排程執行過的情況下，畫面會顯示「尚無資料」
   提示而不是報錯。
"""
import base64
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st

import db_utils
import etf_db
import etf_watchlist

st.set_page_config(page_title="主動式ETF分析", layout="wide")

# --------------------------------------------------------------------------
# 路徑設定
# --------------------------------------------------------------------------
_REPO_ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT_DIR = os.path.dirname(_REPO_ROOT_DIR)  # pages/ 的上一層 = repo 根目錄

TWSE_DB_PATH = os.path.join(_REPO_ROOT_DIR, "twse_ohlcv.db")
ETF_DB_PATH = os.path.join(_REPO_ROOT_DIR, "etf_holdings.db")
ACTIVE_ETF_CSV = os.path.join(_REPO_ROOT_DIR, "Database", "active_etf_list.csv")
WATCHLIST_CONFIG_PATH = os.path.join(_REPO_ROOT_DIR, "etf_watchlist_config.json")

TW_TZ = ZoneInfo("Asia/Taipei")

MARK_COLOR_BUY = "#c0392b"
MARK_COLOR_SELL = "#1e8449"


# --------------------------------------------------------------------------
# GitHub 上傳工具 (沿用其他頁面既有的做法)
# --------------------------------------------------------------------------
def github_repo_config():
    return {
        "token": st.secrets.get("GITHUB_TOKEN", ""),
        "owner": st.secrets.get("GITHUB_OWNER", "henglunlin"),
        "repo": st.secrets.get("GITHUB_REPO", "stock-scanner-FUBAN"),
        "branch": st.secrets.get("GITHUB_BRANCH", "main"),
    }


def upload_file_to_github(file_bytes: bytes, github_path: str, commit_message: str) -> bool:
    cfg = github_repo_config()
    token, owner, repo, branch = cfg["token"], cfg["owner"], cfg["repo"], cfg["branch"]
    if not token or not owner or not repo:
        return False

    github_path = github_path.strip("/")
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{github_path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    sha = None
    try:
        get_res = requests.get(url, headers=headers, params={"ref": branch}, timeout=15)
        if get_res.status_code == 200:
            sha = get_res.json().get("sha")
        payload = {
            "message": commit_message,
            "content": base64.b64encode(file_bytes).decode("utf-8"),
            "branch": branch,
        }
        if sha:
            payload["sha"] = sha
        put_res = requests.put(url, headers=headers, json=payload, timeout=30)
        return put_res.status_code in (200, 201)
    except Exception:
        return False


# --------------------------------------------------------------------------
# 資料庫連線 (分開快取，避免同一個 cache key 混用兩個不同的db)
# --------------------------------------------------------------------------
@st.cache_resource(show_spinner=False)
def _get_etf_conn(path: str, mtime: float):
    return etf_db.get_connection(path)


@st.cache_resource(show_spinner=False)
def _get_twse_conn(path: str, mtime: float):
    return db_utils.get_connection(path)


def get_etf_conn():
    if not os.path.exists(ETF_DB_PATH):
        # 尚未跑過排程時，先建立一個空db(只有schema)，避免整頁直接報錯
        etf_db.get_connection(ETF_DB_PATH)
    return _get_etf_conn(ETF_DB_PATH, os.path.getmtime(ETF_DB_PATH))


def get_twse_conn():
    if not os.path.exists(TWSE_DB_PATH):
        return None
    return _get_twse_conn(TWSE_DB_PATH, os.path.getmtime(TWSE_DB_PATH))


etf_conn = get_etf_conn()
twse_conn = get_twse_conn()

active_etf_name_map = etf_watchlist.load_active_etf_name_map(ACTIVE_ETF_CSV)
all_active_codes = sorted(active_etf_name_map.keys())

if "etf_watchlist_cfg" not in st.session_state:
    st.session_state.etf_watchlist_cfg = etf_watchlist.load_watchlist_config(WATCHLIST_CONFIG_PATH)

cfg = st.session_state.etf_watchlist_cfg


def etf_label(code: str) -> str:
    name = active_etf_name_map.get(code, "")
    return f"{code} {name}" if name else code


st.title("📊 主動式ETF分析")
st.caption(
    "分析主動式ETF的持股買賣狀況。持股資料每日由 GitHub Actions "
    "(update_etf_holdings.yml) 排程抓取，這裡只負責讀取跟分析。"
)

if not all_active_codes:
    st.error(
        f"找不到主動式ETF清單: {ACTIVE_ETF_CSV}\n\n"
        "請確認 Database/active_etf_list.csv 已存在於 repo 內(欄位需含「股票代號」「ETF名稱」)。"
    )
    st.stop()

available_dates = etf_db.get_available_snapshot_dates(etf_conn)
if not available_dates:
    st.warning(
        "etf_holdings.db 裡目前還沒有任何持股資料——"
        "可能是 update_etf_holdings.yml 排程還沒執行過，或是第一次部署。"
        "請確認排程有正常運作，或手動觸發一次 workflow_dispatch。"
    )

# --------------------------------------------------------------------------
# Section A: 追蹤ETF清單編輯
# --------------------------------------------------------------------------
with st.expander("🎯 追蹤ETF清單編輯", expanded=not available_dates):
    st.caption(
        "勾選你想「重點關注/分析」的主動式ETF(下面的分析區塊預設只看這裡勾選的清單，"
        "但每日抓取一律會抓全部主動式ETF，不受這裡影響，所以之後想擴大追蹤範圍，"
        "歷史資料本來就已經在資料庫裡了)。"
    )

    checked_codes = []
    n_cols = 3
    cols = st.columns(n_cols)
    for i, code in enumerate(all_active_codes):
        with cols[i % n_cols]:
            checked = st.checkbox(
                etf_label(code),
                value=(code in cfg["tracked_etfs"]),
                key=f"watchlist_chk_{code}",
            )
            if checked:
                checked_codes.append(code)

    also_push_github = st.checkbox(
        "同時提交到 GitHub (需在 Secrets 設定 GITHUB_TOKEN)",
        value=False,
        key="watchlist_push_github",
    )

    if st.button("💾 儲存追蹤清單", use_container_width=False):
        saved_cfg = etf_watchlist.save_watchlist_config(
            WATCHLIST_CONFIG_PATH, checked_codes, cfg.get("common_change_min_etf_count", 2)
        )
        st.session_state.etf_watchlist_cfg = saved_cfg
        cfg = saved_cfg
        st.success(f"已儲存，目前追蹤 {len(checked_codes)} 檔ETF。")

        if also_push_github:
            with open(WATCHLIST_CONFIG_PATH, "rb") as f:
                file_bytes = f.read()
            ok = upload_file_to_github(
                file_bytes, "etf_watchlist_config.json",
                f"Update etf_watchlist_config.json via 主動式ETF分析頁面",
            )
            if ok:
                st.success("已提交到 GitHub。")
            else:
                st.warning("提交到 GitHub 失敗，請確認 Secrets 中的 GITHUB_TOKEN 設定。")
        st.rerun()

tracked_etfs = [c for c in cfg["tracked_etfs"] if c in active_etf_name_map] or all_active_codes

st.divider()

# --------------------------------------------------------------------------
# Section B: 指定ETF買賣狀況
# --------------------------------------------------------------------------
st.markdown("### 1️⃣ 指定ETF買賣狀況")

if available_dates:
    colB1, colB2 = st.columns([1, 1])
    with colB1:
        pick_etf = st.selectbox(
            "選擇ETF", options=tracked_etfs, format_func=etf_label, key="single_etf_pick"
        )
    with colB2:
        etf_dates = etf_db.get_available_snapshot_dates(etf_conn, pick_etf)
        if etf_dates:
            pick_date = st.selectbox("選擇日期", options=etf_dates, key="single_etf_date")
        else:
            pick_date = None
            st.info(f"{etf_label(pick_etf)} 目前資料庫裡還沒有任何快照。")

    if pick_date:
        changes = etf_db.get_holding_changes(etf_conn, change_date=pick_date, etf_code=pick_etf)
        if changes.empty:
            st.info(f"{pick_date} {etf_label(pick_etf)} 沒有偵測到持股異動(或這是第一天被抓取、沒有比較基準)。")
        else:
            n_buy = (changes["direction"].isin(["加碼", "新納入"])).sum()
            n_sell = (changes["direction"].isin(["減碼", "全數賣出"])).sum()
            m1, m2, m3 = st.columns(3)
            m1.metric("加碼/新納入", f"{n_buy} 檔")
            m2.metric("減碼/全數賣出", f"{n_sell} 檔")
            m3.metric("總異動數", f"{len(changes)} 檔")

            display_cols = [
                "stock_code", "stock_name", "change_type", "direction",
                "weight_prev", "weight_curr", "weight_change",
                "shares_prev", "shares_curr", "shares_change", "compare_base_date",
            ]
            display_names = {
                "stock_code": "股票代碼", "stock_name": "股票名稱", "change_type": "異動類型",
                "direction": "調整方向", "weight_prev": "權重(昨日)", "weight_curr": "權重(今日)",
                "weight_change": "權重變化", "shares_prev": "股數(昨日)", "shares_curr": "股數(今日)",
                "shares_change": "股數變化", "compare_base_date": "比較基準日期",
            }
            show_df = changes[display_cols].rename(columns=display_names)
            st.dataframe(show_df, use_container_width=True, hide_index=True)
else:
    st.info("尚無資料可顯示。")

st.divider()

# --------------------------------------------------------------------------
# Section C: 多數ETF共同買賣
# --------------------------------------------------------------------------
st.markdown("### 2️⃣ 多數ETF共同買賣")

if available_dates:
    colC1, colC2, colC3 = st.columns([1, 1, 1.4])
    with colC1:
        common_date = st.selectbox("選擇日期", options=available_dates, key="common_change_date")
    with colC2:
        scope_choice = st.radio(
            "分析範圍", ["只看追蹤清單", "全部主動式ETF"], horizontal=False, key="common_change_scope"
        )
    scope_codes = tracked_etfs if scope_choice == "只看追蹤清單" else all_active_codes
    with colC3:
        max_possible = max(len(scope_codes), 2)
        min_etf_count = st.slider(
            f"至少幾檔ETF同日共同買賣才顯示 (範圍內共 {len(scope_codes)} 檔)",
            min_value=2, max_value=max_possible,
            value=min(cfg.get("common_change_min_etf_count", 2), max_possible),
            key="common_change_min_count",
        )

    if min_etf_count != cfg.get("common_change_min_etf_count"):
        # 順手記住這次的門檻設定，下次打開網頁不用重設(不強制存GitHub，只更新本機檔案)
        cfg = etf_watchlist.save_watchlist_config(WATCHLIST_CONFIG_PATH, cfg["tracked_etfs"], min_etf_count)
        st.session_state.etf_watchlist_cfg = cfg

    common_df = etf_db.get_common_changes(etf_conn, common_date, scope_codes, min_etf_count=min_etf_count)
    if common_df.empty:
        st.info(f"{common_date} 沒有找到至少 {min_etf_count} 檔ETF同時異動的股票。")
    else:
        st.caption(f"共 {len(common_df)} 檔股票符合門檻。")
        st.dataframe(common_df, use_container_width=True, hide_index=True)

        stock_pick_options = common_df["股票代碼"].tolist()
        pick_stock_for_chart = st.selectbox(
            "選一檔股票查看K線+ETF建倉標記 👇",
            options=stock_pick_options,
            format_func=lambda c: f"{c} {common_df[common_df['股票代碼']==c]['股票名稱'].iloc[0]}",
            key="common_change_pick_stock",
        )
        if st.button("✅ 查看K線圖", key="common_change_view_chart_btn"):
            st.session_state["etf_chart_stock_code"] = pick_stock_for_chart
            st.session_state["etf_chart_end_date"] = common_date
else:
    st.info("尚無資料可顯示。")

st.divider()

# --------------------------------------------------------------------------
# Section D: 個股K線 + ETF建倉標記 (簡化版)
# --------------------------------------------------------------------------
st.markdown("### 3️⃣ 個股K線 + ETF建倉標記")
st.caption(
    "這裡是簡化版K線圖，只用來快速確認ETF買賣點位置。"
    "完整回測/停利/移動停利等功能請到「Stock simulator」頁面查看。"
)

if twse_conn is None:
    st.warning(f"找不到 twse_ohlcv.db ({TWSE_DB_PATH})，無法繪製K線圖。")
else:
    stock_list_df = db_utils.get_stock_list(twse_conn)
    stock_options = [f"{r.SecurityCode} {r.SecurityName}" for r in stock_list_df.itertuples()]

    default_stock_choice = None
    pending_code = st.session_state.get("etf_chart_stock_code")
    if pending_code:
        for opt in stock_options:
            if opt.startswith(pending_code):
                default_stock_choice = opt
                break

    colD1, colD2 = st.columns([1.4, 1])
    with colD1:
        default_idx = stock_options.index(default_stock_choice) if default_stock_choice in stock_options else 0
        chart_stock_choice = st.selectbox(
            "股票代碼", options=stock_options, index=default_idx if stock_options else 0,
            key="etf_chart_stock_select",
        )
    with colD2:
        default_end = st.session_state.get("etf_chart_end_date")
        default_end_dt = pd.to_datetime(default_end) if default_end else pd.to_datetime(datetime.now(TW_TZ).date())
        chart_end_date = st.date_input("K線結束日期", value=default_end_dt, key="etf_chart_end_date_input")

    if stock_options:
        chart_code = chart_stock_choice.split(" ")[0]
        chart_start_date = (pd.to_datetime(chart_end_date) - timedelta(days=120)).strftime("%Y-%m-%d")
        chart_end_str = pd.to_datetime(chart_end_date).strftime("%Y-%m-%d")

        ohlcv_df = db_utils.get_stock_ohlcv(twse_conn, chart_code, chart_start_date, chart_end_str)

        if ohlcv_df.empty:
            st.info(f"{chart_code} 在 {chart_start_date}~{chart_end_str} 期間查無K線資料。")
        else:
            events_df = etf_db.get_stock_etf_events(
                etf_conn, chart_code, start_date=chart_start_date, end_date=chart_end_str
            )

            fig = go.Figure()
            fig.add_trace(go.Candlestick(
                x=ohlcv_df.index, open=ohlcv_df["Open"], high=ohlcv_df["High"],
                low=ohlcv_df["Low"], close=ohlcv_df["Close"],
                increasing_line_color="#c0392b", decreasing_line_color="#1e8449",
                increasing_fillcolor="#c0392b", decreasing_fillcolor="#1e8449",
                name=chart_code,
            ))

            if not events_df.empty:
                bullish_dates, bullish_y, bullish_text = [], [], []
                bearish_dates, bearish_y, bearish_text = [], [], []
                for _, ev in events_df.iterrows():
                    d = ev["change_date"]
                    if d not in ohlcv_df.index:
                        continue
                    y = ohlcv_df.loc[d, "High"] if ev["direction"] in ("加碼", "新納入") else ohlcv_df.loc[d, "Low"]
                    label = f"{ev['etf_code']} {ev['direction']}"
                    if ev["direction"] in ("加碼", "新納入"):
                        bullish_dates.append(d)
                        bullish_y.append(y)
                        bullish_text.append(label)
                    else:
                        bearish_dates.append(d)
                        bearish_y.append(y)
                        bearish_text.append(label)

                if bullish_dates:
                    fig.add_trace(go.Scatter(
                        x=bullish_dates, y=bullish_y, mode="markers+text",
                        marker=dict(symbol="triangle-up", size=12, color=MARK_COLOR_BUY),
                        text=bullish_text, textposition="top center",
                        textfont=dict(size=9, color=MARK_COLOR_BUY),
                        name="ETF加碼/新納入",
                    ))
                if bearish_dates:
                    fig.add_trace(go.Scatter(
                        x=bearish_dates, y=bearish_y, mode="markers+text",
                        marker=dict(symbol="triangle-down", size=12, color=MARK_COLOR_SELL),
                        text=bearish_text, textposition="bottom center",
                        textfont=dict(size=9, color=MARK_COLOR_SELL),
                        name="ETF減碼/全數賣出",
                    ))
            else:
                st.caption("這檔股票在此期間沒有偵測到追蹤ETF的持股異動紀錄。")

            fig.update_layout(
                title=f"{chart_code} {chart_stock_choice.split(' ', 1)[1] if ' ' in chart_stock_choice else ''}",
                xaxis_rangeslider_visible=False, height=560, showlegend=True,
                legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
            )
            fig.update_xaxes(type="category")
            st.plotly_chart(fig, use_container_width=True)

st.divider()

# --------------------------------------------------------------------------
# Section E: 抓取狀態診斷
# --------------------------------------------------------------------------
with st.expander("🔧 抓取狀態診斷", expanded=False):
    log_df = etf_db.get_latest_fetch_log(etf_conn, limit=60)
    if log_df.empty:
        st.caption("尚無抓取紀錄。")
    else:
        st.dataframe(
            log_df[["run_date", "etf_code", "status", "row_count", "message", "created_at"]],
            use_container_width=True, hide_index=True,
        )
