# -*- coding: utf-8 -*-
"""
pages/7_📊_主動式ETF分析.py
============================
主動式ETF持股/買賣分析頁面。

資料來源：
  - ETF_data/etf_holdings.db (由 fetch_etf_holdings.py 經 GitHub Actions 每日排程寫入)
      etf_holdings         每日持股快照
      etf_holding_changes   每日持股異動 (加碼/減碼/新納入/全數賣出)
  - twse_ohlcv.db (掃描器既有的個股OHLCV資料庫，這裡只讀取，不寫入)
  - ETF_data/active_etf_list.csv  全部主動式ETF代號/名稱對照
  - ETF_data/etf_watchlist_config.json  使用者勾選「重點關注」的ETF清單(可編輯)

⚠️ 2026-08-24：ETF相關資料檔案這次統一集中放到獨立的 ETF_data/ 資料夾(跟scanner
   既有、用途不同的 Database/ 資料夾分開)，不再放在repo根目錄或Database/裡。

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
# ⚠️ 2026-08-24：ETF相關資料檔案改集中放到獨立的 ETF_data/ 資料夾，不再放在
# repo根目錄或跟scanner共用的 Database/ 資料夾(避免混在一起)。
_ETF_DATA_DIR = os.path.join(_REPO_ROOT_DIR, "ETF_data")
os.makedirs(_ETF_DATA_DIR, exist_ok=True)
ETF_DB_PATH = os.path.join(_ETF_DATA_DIR, "etf_holdings.db")
ACTIVE_ETF_CSV = os.path.join(_ETF_DATA_DIR, "active_etf_list.csv")
WATCHLIST_CONFIG_PATH = os.path.join(_ETF_DATA_DIR, "etf_watchlist_config.json")

TW_TZ = ZoneInfo("Asia/Taipei")

MARK_COLOR_BUY = "#c0392b"
MARK_COLOR_SELL = "#1e8449"


# --------------------------------------------------------------------------
# GitHub 上傳工具 (沿用其他頁面既有的做法)
# --------------------------------------------------------------------------
def github_repo_config():
    # ⚠️ 2026-08-24發現(用AppTest測新按鈕時意外測出來的)：如果這個環境完全沒有
    # 任何 secrets.toml(不是「GITHUB_TOKEN這個key不存在」，是「整個secrets機制
    # 都沒設定過」)，st.secrets.get(...) 會直接丟 StreamlitSecretNotFoundError、
    # 不會像一般dict.get()一樣安靜地回傳預設值，導致整頁crash。包一層try/except
    # 讓「完全沒設定過secrets」這種情況也能優雅地回傳空字串(呼叫端本來就有在檢查
    # token是否為空、會顯示對應的提示訊息，不會影響既有行為)。
    try:
        return {
            "token": st.secrets.get("GITHUB_TOKEN", ""),
            "owner": st.secrets.get("GITHUB_OWNER", "henglunlin"),
            "repo": st.secrets.get("GITHUB_REPO", "stock-scanner-FUBAN"),
            "branch": st.secrets.get("GITHUB_BRANCH", "main"),
        }
    except Exception:
        return {"token": "", "owner": "henglunlin", "repo": "stock-scanner-FUBAN", "branch": "main"}


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


def trigger_etf_update_workflow(workflow_file: str = "update_etf_holdings.yml") -> tuple[bool, str]:
    """
    觸發 GitHub Actions 的 workflow_dispatch，讓抓取實際跑在 Actions 的
    headless Chromium 環境裡(跟每日排程共用同一支workflow/同一套抓取程式碼)，
    而不是在Streamlit Cloud這邊直接跑瀏覽器。

    ⚠️ 2026-08-24：原本側邊欄還有一顆「🚀 立即抓取」按鈕(App內直接用Playwright跑，
    適合單檔/追蹤清單快速測試)，跟這個按鈕並存。使用者確認GitHub Actions才是
    正式的執行路徑後，那顆按鈕已經移除——省掉Streamlit Cloud端額外裝Playwright/
    Chromium的部署風險(這正是v6當初packages.txt部署失敗的根源)，現在統一只走這條
    「Streamlit觸發GitHub Actions」的路。沿用跟「同步追蹤清單到GitHub」相同的
    github_repo_config()/GITHUB_TOKEN 設定，不用另外設定新的secret。

    回傳 (成功與否, 訊息)。
    """
    cfg = github_repo_config()
    token, owner, repo, branch = cfg["token"], cfg["owner"], cfg["repo"], cfg["branch"]
    if not token or not owner or not repo:
        return False, "尚未設定 GITHUB_TOKEN(在 Streamlit Secrets 裡)，無法觸發 GitHub Actions。"

    url = f"https://api.github.com/repos/{owner}/{repo}/actions/workflows/{workflow_file}/dispatches"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        res = requests.post(url, headers=headers, json={"ref": branch}, timeout=15)
        if res.status_code == 204:
            return True, "已送出，GitHub Actions 開始在背景執行(通常幾分鐘內會跑完)。"
        return False, f"送出失敗：HTTP {res.status_code} {res.text[:200]}"
    except Exception as e:
        return False, f"送出失敗：{type(e).__name__}: {e}"


# --------------------------------------------------------------------------
# GitHub Actions 執行進度查詢 (2026-08-24新增)
# --------------------------------------------------------------------------
# ⚠️ GitHub Actions API本身不會回傳「百分比進度」，只有每個step的status
# (queued/in_progress/completed)。這裡的做法：dispatch後記下時間戳記，
# 因為dispatch API本身不會直接回傳這次執行的run id，所以先用「這個workflow在
# dispatch時間之後、由workflow_dispatch事件觸發的最新一筆run」去配對(通常
# 幾秒內就能配對到)，配對到run id後，改查該run底下所有job的steps清單，
# 用「已完成的step數 / 總step數」自己算出一個進度比例來顯示進度條。
def find_workflow_run_after(cfg: dict, workflow_file: str, after_iso: str):
    """找出 dispatch 之後、由 workflow_dispatch 觸發的最新一筆執行紀錄。"""
    token, owner, repo, branch = cfg["token"], cfg["owner"], cfg["repo"], cfg["branch"]
    url = f"https://api.github.com/repos/{owner}/{repo}/actions/workflows/{workflow_file}/runs"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        res = requests.get(
            url, headers=headers,
            params={"branch": branch, "event": "workflow_dispatch", "per_page": 5},
            timeout=15,
        )
        if res.status_code != 200:
            return None
        for run in res.json().get("workflow_runs", []):
            if run.get("created_at", "") >= after_iso:
                return run
        return None
    except Exception:
        return None


def get_run_jobs_progress(cfg: dict, run_id):
    """回傳 (已完成step數, 總step數, 目前執行中的step名稱)。"""
    token, owner, repo = cfg["token"], cfg["owner"], cfg["repo"]
    url = f"https://api.github.com/repos/{owner}/{repo}/actions/runs/{run_id}/jobs"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        res = requests.get(url, headers=headers, timeout=15)
        if res.status_code != 200:
            return 0, 0, ""
        total = 0
        done = 0
        current_step = ""
        for job in res.json().get("jobs", []):
            for step in job.get("steps", []):
                total += 1
                if step.get("status") == "completed":
                    done += 1
                elif step.get("status") == "in_progress" and not current_step:
                    current_step = step.get("name", "")
        return done, total, current_step
    except Exception:
        return 0, 0, ""


@st.fragment(run_every=5)
def render_gha_progress():
    """每5秒自動輪詢一次目前觸發的 GitHub Actions 執行進度，顯示進度條。
    用 st.fragment 讓這個區塊自己刷新，不會拖著整頁一起重跑。"""
    dispatch_time = st.session_state.get("gha_dispatch_time")
    if not dispatch_time:
        return

    if st.session_state.get("gha_run_done"):
        result = st.session_state.get("gha_run_result", {})
        icon = "✅" if result.get("conclusion") == "success" else "❌"
        st.caption(f"{icon} 上次背景排程結果：{result.get('conclusion', '未知')}")
        if result.get("run_url"):
            st.caption(f"[查看完整log]({result['run_url']})")
        return

    cfg = github_repo_config()
    if not cfg["token"]:
        return

    run = None
    if st.session_state.get("gha_run_id"):
        run_id = st.session_state["gha_run_id"]
        try:
            res = requests.get(
                f"https://api.github.com/repos/{cfg['owner']}/{cfg['repo']}/actions/runs/{run_id}",
                headers={
                    "Authorization": f"Bearer {cfg['token']}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                timeout=15,
            )
            if res.status_code == 200:
                run = res.json()
        except Exception:
            run = None
    else:
        run = find_workflow_run_after(cfg, "update_etf_holdings.yml", dispatch_time)
        if run:
            st.session_state["gha_run_id"] = run["id"]

    if not run:
        st.caption("⏳ 等待 GitHub Actions 開始執行(剛觸發，通常幾秒到幾十秒內會出現)...")
        return

    status = run.get("status")  # queued / in_progress / completed
    conclusion = run.get("conclusion")
    run_url = run.get("html_url", "")
    done, total, current_step = get_run_jobs_progress(cfg, run["id"])
    fraction = (done / total) if total else 0.0

    if status == "completed":
        st.session_state["gha_run_done"] = True
        st.session_state["gha_run_result"] = {"conclusion": conclusion, "run_url": run_url}
        st.progress(1.0, text="執行完成")
        if conclusion == "success":
            st.success("✅ 背景排程執行成功，資料已更新，重新整理頁面即可看到。")
        else:
            st.error(f"❌ 背景排程執行失敗({conclusion})，可以到下面的連結看log。")
        if run_url:
            st.caption(f"[查看完整log]({run_url})")
    else:
        step_text = f"目前步驟：{current_step}" if current_step else f"狀態：{status}"
        st.progress(fraction, text=f"{done}/{total}　{step_text}")
        if run_url:
            st.caption(f"[在GitHub Actions頁面查看]({run_url})")


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
# 2026-08-24新增：db檔案的mtime，當作下面查詢快取(st.cache_data)的失效依據——
# 直接內嵌計算(不呼叫下面才定義的_etf_db_mtime()函式)，避免函式定義順序問題。
etf_db_mtime = os.path.getmtime(ETF_DB_PATH) if os.path.exists(ETF_DB_PATH) else 0.0
# 2026-09-03新增：twse_ohlcv.db的mtime，供「金額估算」的收盤價查詢快取(cached_close_prices)
# 當失效依據，邏輯跟etf_db_mtime一樣。
twse_db_mtime = os.path.getmtime(TWSE_DB_PATH) if os.path.exists(TWSE_DB_PATH) else 0.0

active_etf_name_map = etf_watchlist.load_active_etf_name_map(ACTIVE_ETF_CSV)
all_active_codes = sorted(active_etf_name_map.keys())

if "etf_watchlist_cfg" not in st.session_state:
    st.session_state.etf_watchlist_cfg = etf_watchlist.load_watchlist_config(WATCHLIST_CONFIG_PATH)

cfg = st.session_state.etf_watchlist_cfg


def etf_label(code: str) -> str:
    name = active_etf_name_map.get(code, "")
    return f"{code} {name}" if name else code


# 2026-08-24新增：台股慣例用「張」(1張=1000股)顯示持股/異動股數比「股」直覺，
# 這裡統一用同一個函式把資料庫存的原始股數欄位轉成張數，四個顯示股數的地方
# (1️⃣異動明細、1️⃣成分股比例、3️⃣買賣紀錄明細、4️⃣區間異動查詢)都共用，
# 避免各處分別寫、算法不一致。無條件捨去到整數張(股票交易本來就是以張為單位，
# 剩不滿一張的零股在這幾張表裡不特別處理，直接四捨五入)。
def shares_series_to_lots(series: pd.Series) -> pd.Series:
    return (series / 1000).round(0)


# --------------------------------------------------------------------------
# 金額估算工具 (2026-09-03新增，供「多數ETF共同買賣」「指定ETF買賣狀況」的
# 儀表板卡片使用)
# --------------------------------------------------------------------------
# ⚠️ etf_holdings.db 本身沒有存「成交金額」，只有股數/權重異動。這裡用
# 「異動當天(change_date)這檔股票在 twse_ohlcv.db 的收盤價」估算金額
# (股數變化 × 收盤價)，這是「用市值變化推估」的近似值，不是ETF基金公司實際
# 申報的成交金額(實際成交價可能跟當天收盤價有落差)——畫面上會註明「估算」。
def format_twd_amount(value) -> str:
    """把台幣金額(可能是None/NaN)格式化成「+23.1億」/「-842萬」這種簡短字串，
    金額不到千的直接顯示數字。None/NaN回傳「—」。"""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "—"
    sign = "+" if value > 0 else ("-" if value < 0 else "")
    abs_v = abs(value)
    if abs_v >= 1e8:
        return f"{sign}{abs_v / 1e8:.1f}億"
    if abs_v >= 1e4:
        return f"{sign}{abs_v / 1e4:,.0f}萬"
    if abs_v == 0:
        return "0"
    return f"{sign}{abs_v:,.0f}"


@st.cache_data(show_spinner=False)
def cached_close_prices(_twse_conn, twse_mtime: float, stock_codes: tuple, date: str) -> dict:
    """批次查詢一批股票代碼在指定日期的收盤價，回傳 {股票代碼: 收盤價} dict。
    查不到的股票代碼不會出現在dict裡(呼叫端用.get()處理缺值)。"""
    if _twse_conn is None or not stock_codes:
        return {}
    placeholders = ",".join("?" * len(stock_codes))
    q = f"SELECT SecurityCode, Close FROM ohlcv_data WHERE Date = ? AND SecurityCode IN ({placeholders})"
    try:
        df = pd.read_sql(q, _twse_conn, params=[date] + list(stock_codes))
    except Exception:
        return {}
    return dict(zip(df["SecurityCode"], df["Close"]))


def add_estimated_value_column(changes_df: pd.DataFrame, twse_conn, twse_mtime: float) -> pd.DataFrame:
    """幫一份「異動明細」DataFrame(需含stock_code/change_date/shares_change欄位)
    加上一欄「est_value」(估算金額 = 股數變化 × 當天收盤價)。查不到收盤價的列
    (twse_conn是None、股票不在twse_ohlcv.db、或該天沒有K線資料，例如興櫃/停牌)
    「est_value」會是NaN，呼叫端加總時用.sum(skipna=True)会自動略過。"""
    out = changes_df.copy()
    if twse_conn is None or out.empty:
        out["est_value"] = float("nan")
        return out
    out["est_value"] = float("nan")
    for change_date, group in out.groupby("change_date"):
        price_map = cached_close_prices(twse_conn, twse_mtime, tuple(sorted(group["stock_code"].unique())), change_date)
        for idx in group.index:
            code = out.at[idx, "stock_code"]
            price = price_map.get(code)
            shares_chg = out.at[idx, "shares_change"]
            if price is not None and pd.notna(shares_chg):
                out.at[idx, "est_value"] = float(shares_chg) * float(price)
    return out


def aggregate_change_values(changes_val: pd.DataFrame, group_col: str) -> pd.DataFrame:
    """把已經加上est_value欄位的異動明細，依group_col(stock_code或etf_code)分組加總，
    回傳欄位：group_col、gross_buy(該組加碼估算金額加總，只計正值)、
    gross_sell(該組減碼估算金額加總，只計負值)、net(淨額=gross_buy+gross_sell)。
    查不到收盤價的NaN列在.sum()時會被自動忽略(pandas預設skipna=True)，
    不會讓整組因為一筆缺值就變成NaN。"""
    if changes_val.empty:
        return pd.DataFrame(columns=[group_col, "gross_buy", "gross_sell", "net"])

    def _agg(g):
        vals = g["est_value"]
        return pd.Series({
            "gross_buy": vals[vals > 0].sum(),
            "gross_sell": vals[vals < 0].sum(),
            "net": vals.sum(),
        })

    # ⚠️ pandas 2.2+ 的 groupby.apply() 對「group_keys會被一併傳進_agg」這件事
    # 改了預設行為、加了 include_groups 參數並發出FutureWarning；用try/except
    # 相容新舊版本(舊版沒有這個參數，傳了會直接TypeError，退回不帶這個參數的呼叫)，
    # 不影響實際分組加總結果，只是消除警告訊息。
    try:
        result = changes_val.groupby(group_col).apply(_agg, include_groups=False)
    except TypeError:
        result = changes_val.groupby(group_col).apply(_agg)
    result = result.reset_index()
    return result


# --------------------------------------------------------------------------
# 查詢快取 (2026-08-24新增)
# --------------------------------------------------------------------------
# ⚠️ Streamlit的機制：畫面上任何一個widget互動(切分頁不算，但選日期/選股票/
# 按按鈕都算)都會讓整頁重新執行一次，而且四個(現在五個)分頁的程式碼在同一次
# rerun裡「全部都會執行」(只是畫面上只顯示目前選中的分頁)。這代表原本沒加快取
# 的時候，使用者在「3️⃣」調整K線日期，「1️⃣」「2️⃣」「4️⃣」用不到那次互動結果的
# 查詢其實也都跟著重新對db查了一次，只是沒有顯示變化而已，互動一多、db資料一大
# 就容易感覺卡頓。這裡統一幫 etf_db.get_*() 這幾個查詢函式包一層 st.cache_data：
#   - 參數名稱前面加底線的(_conn)代表「不參與快取key的雜湊比對」(Streamlit的慣例)，
#     單純只是拿來實際執行查詢——sqlite3.Connection物件本身沒辦法被雜湊，一定要
#     用底線開頭排除掉，否則st.cache_data會直接報錯。
#   - mtime(不加底線，會參與快取key)是db檔案的最後修改時間，只要
#     update_etf_holdings.yml排程寫入新資料、db檔案mtime改變，快取就會自動失效
#     重新查詢；沒有新資料進來的期間，同樣的查詢條件會直接吃快取，不用重新連db。
@st.cache_data(show_spinner=False)
def cached_available_dates(_conn, mtime: float, etf_code=None) -> list:
    return etf_db.get_available_snapshot_dates(_conn, etf_code)


@st.cache_data(show_spinner=False)
def cached_holdings_snapshot(_conn, mtime: float, etf_code: str, snapshot_date: str) -> pd.DataFrame:
    return etf_db.get_holdings_snapshot(_conn, etf_code, snapshot_date)


@st.cache_data(show_spinner=False)
def cached_holding_changes(_conn, mtime: float, change_date=None, etf_code=None, etf_codes=None,
                            stock_code=None, start_date=None, end_date=None) -> pd.DataFrame:
    return etf_db.get_holding_changes(
        _conn, change_date=change_date, etf_code=etf_code, etf_codes=etf_codes,
        stock_code=stock_code, start_date=start_date, end_date=end_date,
    )


@st.cache_data(show_spinner=False)
def cached_common_changes(_conn, mtime: float, change_date: str, etf_codes: list, min_etf_count: int) -> pd.DataFrame:
    return etf_db.get_common_changes(_conn, change_date, etf_codes, min_etf_count=min_etf_count)


@st.cache_data(show_spinner=False)
def cached_stock_etf_events(_conn, mtime: float, stock_code: str, start_date=None, end_date=None,
                             etf_codes=None) -> pd.DataFrame:
    return etf_db.get_stock_etf_events(_conn, stock_code, start_date=start_date, end_date=end_date,
                                        etf_codes=etf_codes)


@st.cache_data(show_spinner=False)
def cached_etf_held_stocks(_conn, mtime: float, etf_code: str) -> pd.DataFrame:
    return etf_db.get_etf_held_stocks(_conn, etf_code)


@st.cache_data(show_spinner=False)
def cached_stock_weight_history(_conn, mtime: float, etf_code: str, stock_code: str) -> pd.DataFrame:
    return etf_db.get_stock_weight_history(_conn, etf_code, stock_code)


@st.cache_data(show_spinner=False)
def cached_all_held_stocks(_conn, mtime: float, etf_codes: list) -> pd.DataFrame:
    return etf_db.get_all_held_stocks(_conn, etf_codes)


@st.cache_data(show_spinner=False)
def cached_stock_latest_holdings(_conn, mtime: float, stock_code: str, etf_codes: list) -> pd.DataFrame:
    return etf_db.get_stock_latest_holdings_across_etfs(_conn, stock_code, etf_codes)


@st.cache_data(show_spinner=False)
def cached_latest_fetch_log(_conn, mtime: float, limit: int = 60) -> pd.DataFrame:
    return etf_db.get_latest_fetch_log(_conn, limit=limit)


# --------------------------------------------------------------------------
# 側邊欄：觸發背景排程抓取 (GitHub Actions)
# --------------------------------------------------------------------------
# ⚠️ 2026-08-24：原本這裡還有一顆「🚀 立即抓取」按鈕(App內直接用Playwright/瀏覽器
# 跑一次抓取)。使用者確認GitHub Actions才是正式的執行路徑後，這顆按鈕已經移除，
# 理由：(1) 側邊欄更簡潔、(2) Streamlit Cloud不用再裝Playwright/Chromium，
# 少一塊部署風險(v6當初packages.txt因為Debian套件改名而部署失敗，就是這塊依賴造成的)。
# 唯一的取捨是少了「只抓單一檔/只抓追蹤清單」這種縮小範圍的快速測試能力——
# 現在觸發背景排程一定是抓全部啟用中的ETF；如果之後真的需要單檔測試，
# 可以在GitHub Actions頁面手動執行 fetch_etf_holdings.py，或本機直接跑。
with st.sidebar:
    st.markdown("### 🔄 更新ETF買賣資料")
    st.caption(
        "觸發跟每日排程共用的 GitHub Actions workflow，"
        "在 Actions 的環境裡用headless Chromium跑完整套抓取(全部啟用中的ETF)，"
        "跑完會自動commit回 ETF_data/etf_holdings.db，之後回來這頁重新整理就看得到新資料。"
    )
    if st.button("🛰️ 觸發背景排程抓取 (GitHub Actions)", key="dispatch_gha_btn", use_container_width=True):
        with st.spinner("正在呼叫 GitHub API 觸發 workflow..."):
            ok, msg = trigger_etf_update_workflow()
        if ok:
            st.session_state["gha_dispatch_time"] = datetime.now(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")
            st.session_state["gha_run_id"] = None
            st.session_state["gha_run_done"] = False
            st.session_state.pop("gha_run_result", None)
            st.success(f"✅ {msg}")
        else:
            st.error(f"❌ {msg}")

    if st.session_state.get("gha_dispatch_time"):
        st.divider()
        st.caption("📈 本次觸發的執行進度")
        render_gha_progress()


st.title("📊 主動式ETF分析")
st.caption(
    "分析主動式ETF的持股買賣狀況。持股資料每日由 GitHub Actions "
    "(update_etf_holdings.yml) 排程抓取，這裡只負責讀取跟分析。"
)

if not all_active_codes:
    st.error(
        f"找不到主動式ETF清單: {ACTIVE_ETF_CSV}\n\n"
        "請確認 ETF_data/active_etf_list.csv 已存在於 repo 內(欄位需含「股票代號」「ETF名稱」)。"
    )
    st.stop()

available_dates = cached_available_dates(etf_conn, etf_db_mtime)
if not available_dates:
    st.warning(
        "etf_holdings.db 裡目前還沒有任何持股資料——"
        "可能是 update_etf_holdings.yml 排程還沒執行過，或是第一次部署。"
        "請確認排程有正常運作，或手動觸發一次 workflow_dispatch。"
    )

# --------------------------------------------------------------------------
# Section A0: ETF抓取範圍管理 (2026-08-24新增)
# --------------------------------------------------------------------------
# ⚠️ 這裡管理的是 ETF_data/active_etf_list.csv 的「啟用」欄位，跟下面
# Section A的「追蹤清單」是兩個不同的概念：
#   - 「啟用」(這裡)：同時決定(1)這個頁面所有分頁/下拉選單能選到哪些ETF
#     (all_active_codes)，(2) GitHub Actions每日排程實際會抓哪些ETF
#     (fetch_etf_holdings.py的load_active_etf_list()讀同一個「啟用」欄位)。
#     一開始只有6檔已用真實資料驗證過的純台股ETF是啟用的，其餘26檔先當「目錄」保留。
#   - 「追蹤」(下面Section A)：從「已啟用」的ETF裡，再挑一個子集合當「分析預設focus」，
#     只影響頁面顯示/篩選，不影響抓取範圍。
GITHUB_ACTIVE_ETF_CSV_PATH = os.path.relpath(ACTIVE_ETF_CSV, _REPO_ROOT_DIR).replace(os.sep, "/")
GITHUB_WATCHLIST_CONFIG_PATH = os.path.relpath(WATCHLIST_CONFIG_PATH, _REPO_ROOT_DIR).replace(os.sep, "/")

with st.expander("⚙️ ETF抓取範圍管理(啟用/停用主動式ETF)", expanded=False):
    st.caption(
        "這裡的「啟用」狀態就是 ETF_data/active_etf_list.csv 的「啟用」欄位，"
        "同時控制(1)這個頁面所有分頁能選到哪些ETF、(2)GitHub Actions每日排程實際會抓哪些ETF。"
    )
    st.warning(
        "⚠️ GitHub Actions排程是完全獨立的執行環境，每次執行都是重新從repo抓最新版本的檔案，"
        "不會讀到這個網頁自己儲存在本機的檔案。**只按「💾 儲存」而沒有勾選「同時提交到GitHub」，"
        "這次的啟用/停用設定不會影響排程，而且下次網頁重新啟動(重新部署/長時間沒人用)就會消失**——"
        "要讓改動真的生效，請務必勾選「同時提交到GitHub」一起送出。"
    )
    st.caption(
        "⚠️ 目前只有6檔(00403A/00980A/00981A/00982A/00985A/00991A)是已經用真實資料驗證過、"
        "確定純台股持股、抓取邏輯正確的。其餘ETF裡，名稱含「美國」「全球」「ARK」等字樣的，"
        "很可能持有非台股資產，目前簡化版的抓取/欄位解析邏輯還沒針對這類ETF驗證過——"
        "啟用後建議先手動觸發一次側邊欄「🛰️ 觸發背景排程抓取」，到下面「🔧 抓取狀態診斷」"
        "確認這檔的抓取狀態是「success」，再放心長期使用。"
    )

    full_etf_df = etf_watchlist.load_full_active_etf_list(ACTIVE_ETF_CSV)
    if full_etf_df.empty:
        st.warning(f"讀不到 {ACTIVE_ETF_CSV}，無法管理啟用範圍。")
    else:
        new_enabled = {}
        n_cols0 = 3
        cols0 = st.columns(n_cols0)
        for i, row in enumerate(full_etf_df.itertuples()):
            with cols0[i % n_cols0]:
                checked0 = st.checkbox(
                    f"{row.股票代號} {row.ETF名稱}",
                    value=bool(row.啟用),
                    key=f"enable_chk_{row.股票代號}",
                )
                new_enabled[row.股票代號] = 1 if checked0 else 0

        also_push_github_enable = st.checkbox(
            "同時提交到 GitHub (強烈建議勾選，否則排程不會套用這次改動)",
            value=True,
            key="enable_list_push_github",
        )

        if st.button("💾 儲存啟用範圍", key="save_enable_list_btn"):
            updated_etf_df = full_etf_df.copy()
            updated_etf_df["啟用"] = updated_etf_df["股票代號"].map(new_enabled)
            etf_watchlist.save_full_active_etf_list(ACTIVE_ETF_CSV, updated_etf_df)
            n_enabled_now = int(updated_etf_df["啟用"].sum())
            st.success(f"已儲存，目前啟用 {n_enabled_now} 檔ETF。")

            if also_push_github_enable:
                with open(ACTIVE_ETF_CSV, "rb") as f:
                    file_bytes = f.read()
                ok = upload_file_to_github(
                    file_bytes, GITHUB_ACTIVE_ETF_CSV_PATH,
                    "Update active_etf_list.csv 啟用範圍 via 主動式ETF分析頁面",
                )
                if ok:
                    st.success("已提交到 GitHub，下次排程會套用這次的啟用/停用設定。")
                else:
                    st.warning(
                        "提交到 GitHub 失敗，請確認 Secrets 中的 GITHUB_TOKEN 設定——"
                        "這次改動暫時只存在本次網頁環境裡，排程還不會套用。"
                    )
            else:
                st.warning("⚠️ 沒有勾選「同時提交到GitHub」，這次的啟用/停用設定不會影響排程，且下次網頁重新啟動就會消失。")
            st.rerun()

# --------------------------------------------------------------------------
# Section A: 追蹤ETF清單編輯
# --------------------------------------------------------------------------
with st.expander("🎯 追蹤ETF清單編輯", expanded=not available_dates):
    st.caption(
        "勾選你想「重點關注/分析」的主動式ETF(下面的分析區塊預設只看這裡勾選的清單)。"
        "⚠️ 這裡只影響頁面顯示的預設範圍，每日排程實際抓取哪些ETF是由上面"
        "「⚙️ ETF抓取範圍管理」的「啟用」狀態決定，不是這裡——"
        "之後想擴大追蹤範圍，只要該ETF已經啟用，歷史資料本來就已經在資料庫裡了。"
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
            # ⚠️ 2026-08-24修正：這裡先前傳的github_path是"etf_watchlist_config.json"
            # (repo根目錄)，但本機實際檔案早在v9就搬到"ETF_data/etf_watchlist_config.json"了，
            # 兩者不一致會導致「提交到GitHub」把檔案寫到repo根目錄的錯誤位置，跟本機讀取的
            # 路徑對不起來，等於這個功能實際上沒有真的生效。改用GITHUB_WATCHLIST_CONFIG_PATH
            # (從WATCHLIST_CONFIG_PATH動態算出跟repo根目錄的相對路徑)確保兩邊路徑一致。
            ok = upload_file_to_github(
                file_bytes, GITHUB_WATCHLIST_CONFIG_PATH,
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
# Section B~G：分析區塊改用橫向分頁(st.tabs)顯示，取代原本上下堆疊、
# 要一直往下滾的版面(2026-08-24調整)。同一次rerun裡所有分頁的程式碼都會執行
# (只是畫面上只顯示目前選中的分頁)，所以分頁之間互相連動(例如2️⃣按鈕跳轉到3️⃣
# 的K線圖)的session_state邏輯不用改，只是使用者要自己點一下切換分頁查看結果。
# 「5️⃣ 個股全域查詢」是2026-08-24新增的第五個分頁。
# --------------------------------------------------------------------------
tab1, tab2, tab3, tab4, tab5 = st.tabs([
    "1️⃣ 指定ETF買賣狀況", "2️⃣ 多數ETF共同買賣",
    "3️⃣ 個股K線 + ETF建倉標記", "4️⃣ 區間異動查詢",
    "5️⃣ 個股全域查詢",
])

with tab1:

    if available_dates:
        colB1, colB2 = st.columns([1, 1])
        with colB1:
            pick_etf = st.selectbox(
                "選擇ETF", options=tracked_etfs, format_func=etf_label, key="single_etf_pick"
            )
        with colB2:
            etf_dates = cached_available_dates(etf_conn, etf_db_mtime, pick_etf)
            if etf_dates:
                pick_date = st.selectbox("選擇日期", options=etf_dates, key="single_etf_date")
            else:
                pick_date = None
                st.info(f"{etf_label(pick_etf)} 目前資料庫裡還沒有任何快照。")

        if pick_date:
            changes = cached_holding_changes(etf_conn, etf_db_mtime, change_date=pick_date, etf_code=pick_etf)
            if changes.empty:
                st.info(f"{pick_date} {etf_label(pick_etf)} 沒有偵測到持股異動(或這是第一天被抓取、沒有比較基準)。")
            else:
                # 2026-09-03新增：「📸 最新快照」卡片，比照使用者提供的參考截圖排版——
                # 摘要句+加碼/減碼估算金額+新增/加碼/刪除/減碼四項計數+操作按鈕。
                # ⚠️ 金額是用「異動當天收盤價 × 股數變化」估算出來的市值變化，
                # 不是ETF基金公司實際申報的成交金額(基金實際成交價可能跟當天收盤價
                # 有落差)，畫面上會註明「估算」。
                changes_val = add_estimated_value_column(changes, twse_conn, twse_db_mtime)
                n_new = int((changes["change_type"] == "新增").sum())
                n_removed = int((changes["change_type"] == "刪除").sum())
                n_buy = int((changes["direction"] == "加碼").sum())
                n_sell = int((changes["direction"] == "減碼").sum())
                total_buy_val = changes_val.loc[changes_val["est_value"] > 0, "est_value"].sum()
                total_sell_val = changes_val.loc[changes_val["est_value"] < 0, "est_value"].sum()
                n_missing_price = int(changes_val["est_value"].isna().sum())
                base_date = changes["compare_base_date"].iloc[0] if "compare_base_date" in changes.columns and not changes.empty else ""

                with st.container(border=True):
                    snap_top1, snap_top2 = st.columns([2, 1])
                    with snap_top1:
                        st.markdown(f"**📸 最新快照 — {etf_label(pick_etf)}**")
                    with snap_top2:
                        st.caption(f"{base_date} → {pick_date}" if base_date else pick_date)

                    st.markdown(
                        f"最近一次持股異動 **{len(changes)}** 筆　"
                        f":red[加碼 {format_twd_amount(total_buy_val)}]　"
                        f":green[減碼 {format_twd_amount(total_sell_val)}]"
                        f"　*(估算金額，見下方說明)*"
                    )
                    snap_c1, snap_c2, snap_c3, snap_c4 = st.columns(4)
                    snap_c1.metric("新增", f"{n_new}")
                    snap_c2.metric("加碼", f"{n_buy}")
                    snap_c3.metric("刪除", f"{n_removed}")
                    snap_c4.metric("減碼", f"{n_sell}")
                    if n_missing_price:
                        st.caption(f"⚠️ {n_missing_price} 筆異動在 twse_ohlcv.db 查不到對應收盤價，金額估算已略過這幾筆。")

                    btn1, btn2, btn3 = st.columns(3)
                    with btn1:
                        show_history_toggle = st.toggle("📜 查看歷史", key="tab1_show_history_toggle")
                    with btn2:
                        csv_bytes = changes.drop(columns=[c for c in ("est_value",) if c in changes.columns]) \
                            .to_csv(index=False).encode("utf-8-sig")
                        st.download_button(
                            "⬇️ 匯出CSV", data=csv_bytes,
                            file_name=f"{pick_etf}_{pick_date}_異動明細.csv",
                            mime="text/csv", key="tab1_export_csv_btn", use_container_width=True,
                        )
                    with btn3:
                        copy_toggle = st.toggle("📋 複製異動清單", key="tab1_copy_list_toggle")

                    if copy_toggle:
                        copy_lines = []
                        for r in changes_val.itertuples():
                            lots_chg = shares_series_to_lots(pd.Series([r.shares_change])).iloc[0]
                            amt_txt = format_twd_amount(getattr(r, "est_value", None))
                            copy_lines.append(
                                f"{r.stock_code} {r.stock_name}　{r.direction}　張數變化{lots_chg:+.0f}張　估算金額{amt_txt}"
                            )
                        st.code("\n".join(copy_lines), language=None)

                    if show_history_toggle:
                        # ⚠️ 不用跨分頁跳轉(Streamlit沒有程式化切換分頁的API，見3️⃣/4️⃣的既有註解)，
                        # 改成直接在這裡inline顯示這檔ETF自己最近幾次的異動彙總，自成一體。
                        hist_dates = cached_available_dates(etf_conn, etf_db_mtime, pick_etf)
                        hist_dates_recent = hist_dates[:10]  # 新到舊排序，取最近10次快照
                        hist_rows = []
                        for hd in hist_dates_recent:
                            hd_changes = cached_holding_changes(etf_conn, etf_db_mtime, change_date=hd, etf_code=pick_etf)
                            if hd_changes.empty:
                                continue
                            hd_val = add_estimated_value_column(hd_changes, twse_conn, twse_db_mtime)
                            hist_rows.append({
                                "日期": hd,
                                "異動筆數": len(hd_changes),
                                "加碼估算金額": format_twd_amount(hd_val.loc[hd_val["est_value"] > 0, "est_value"].sum()),
                                "減碼估算金額": format_twd_amount(hd_val.loc[hd_val["est_value"] < 0, "est_value"].sum()),
                            })
                        if hist_rows:
                            st.dataframe(pd.DataFrame(hist_rows), use_container_width=True, hide_index=True)
                        else:
                            st.caption("這檔ETF目前沒有更早的異動歷史可以顯示。")

                display_cols = [
                    "stock_code", "stock_name", "change_type", "direction",
                    "weight_prev", "weight_curr", "weight_change",
                    "shares_prev", "shares_curr", "shares_change", "compare_base_date",
                ]
                display_names = {
                    "stock_code": "股票代碼", "stock_name": "股票名稱", "change_type": "異動類型",
                    "direction": "調整方向", "weight_prev": "權重(昨日)", "weight_curr": "權重(今日)",
                    "weight_change": "權重變化", "shares_prev": "張數(昨日)", "shares_curr": "張數(今日)",
                    "shares_change": "張數變化", "compare_base_date": "比較基準日期",
                }
                show_df = changes[display_cols].copy()
                for _col in ("shares_prev", "shares_curr", "shares_change"):
                    show_df[_col] = shares_series_to_lots(show_df[_col])
                show_df = show_df.rename(columns=display_names)
                st.dataframe(show_df, use_container_width=True, hide_index=True)

            # 2026-08-24新增：除了「異動」明細，也讓使用者能直接看到這檔ETF在
            # 選定日期「當下」的完整持股成分股比例(不是只看有變動的部分)，
            # 資料來源沿用同一個 etf_holdings 快照表(etf_db.get_holdings_snapshot)，
            # 跟畫面上方顯示的異動明細本來就是同一份資料庫、同一次抓取結果。
            st.markdown("#### 📋 成分股比例")
            holdings_snapshot = cached_holdings_snapshot(etf_conn, etf_db_mtime, pick_etf, pick_date)
            if holdings_snapshot.empty:
                st.info(f"{pick_date} {etf_label(pick_etf)} 沒有持股快照資料。")
            else:
                st.caption(f"{pick_date} {etf_label(pick_etf)} 共持有 {len(holdings_snapshot)} 檔股票(依權重排序)。")

                # dropna防呆：極少數情況下權重欄位可能是空值(例如來源網站當次沒有這個數字)，
                # 畫長條圖前先排除，避免 plotly 因為 None 值報錯或畫出空白長條。
                top_n = holdings_snapshot.dropna(subset=["weight"]).head(15).iloc[::-1]
                if top_n.empty:
                    st.caption("這批持股快照沒有權重數值，無法畫長條圖(下面表格仍可查看股數等其他欄位)。")
                else:
                    bar_fig = go.Figure(go.Bar(
                        x=top_n["weight"],
                        y=[f"{r.stock_code} {r.stock_name}" if r.stock_name else str(r.stock_code)
                           for r in top_n.itertuples()],
                        orientation="h",
                        marker_color="#2c6fbb",
                        text=top_n["weight_text"],
                        textposition="outside",
                    ))
                    bar_fig.update_layout(
                        title=f"前{len(top_n)}大持股權重",
                        height=max(320, 28 * len(top_n)),
                        xaxis_title="權重(%)",
                        margin=dict(l=10, r=40, t=40, b=10),
                    )
                    st.plotly_chart(bar_fig, use_container_width=True)

                # 2026-08-24調整：改用數值欄位「shares」(不是抓取來的原始文字「shares_text」)
                # 換算成張數(shares_series_to_lots，÷1000)，台股慣例用張比用股直覺。
                holdings_table = holdings_snapshot[["stock_code", "stock_name", "weight_text", "shares"]].copy()
                holdings_table["shares"] = shares_series_to_lots(holdings_table["shares"])
                holdings_display_names = {
                    "stock_code": "股票代碼", "stock_name": "股票名稱",
                    "weight_text": "權重", "shares": "張數",
                }
                st.dataframe(
                    holdings_table.rename(columns=holdings_display_names),
                    use_container_width=True, hide_index=True,
                )

                # 2026-08-24新增：「成分股比例」只能看單一天的快照，看不出這檔股票
                # 在這檔ETF裡是「持續加碼中」還是「單次調整」，這裡加一個小折線圖，
                # 直接用 etf_holdings 表裡逐日快照的權重畫出歷史趨勢(不用另外抓資料)。
                st.markdown("#### 📈 個股權重歷史趨勢")
                trend_options = [
                    f"{r.stock_code} {r.stock_name}" if r.stock_name else str(r.stock_code)
                    for r in holdings_snapshot.itertuples()
                ]
                trend_pick = st.selectbox(
                    "選擇股票，查看它在這檔ETF裡的權重(%)隨時間變化",
                    options=trend_options, key="tab1_trend_stock_pick",
                )
                trend_code = trend_pick.split(" ")[0]
                trend_df = cached_stock_weight_history(etf_conn, etf_db_mtime, pick_etf, trend_code)
                trend_df = trend_df.dropna(subset=["weight"])
                if trend_df.empty:
                    st.caption("這檔股票沒有足夠的權重歷史資料可以畫趨勢圖。")
                else:
                    trend_fig = go.Figure(go.Scatter(
                        x=trend_df["snapshot_date"], y=trend_df["weight"],
                        mode="lines+markers", line=dict(color="#2c6fbb", width=2),
                        marker=dict(size=6),
                        text=trend_df["weight_text"], hovertemplate="%{x}<br>權重 %{text}<extra></extra>",
                    ))
                    trend_fig.update_layout(
                        title=f"{trend_pick} 在 {etf_label(pick_etf)} 的權重走勢",
                        yaxis_title="權重(%)", height=320,
                        margin=dict(l=10, r=10, t=40, b=10),
                    )
                    trend_fig.update_xaxes(type="category")
                    st.plotly_chart(trend_fig, use_container_width=True)
                    st.caption(
                        "折線只畫出「有出現在當天快照裡」的日期；如果某天這檔股票已經被剔除持股、"
                        "不在快照裡，折線會在那天之前中斷，不會畫成掉到0。"
                    )
    else:
        st.info("尚無資料可顯示。")

with tab2:

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

        # 2026-09-03新增：「📊 今日主動圈快照」儀表板——沿用上面已選好的
        # common_date / scope_codes / scope_choice，不重複開新的日期/範圍選項。
        dash_changes = cached_holding_changes(etf_conn, etf_db_mtime, change_date=common_date, etf_codes=scope_codes)
        if dash_changes.empty:
            st.info(f"{common_date} 在目前範圍({scope_choice})內沒有任何持股異動紀錄。")
        else:
            dash_changes_val = add_estimated_value_column(dash_changes, twse_conn, twse_db_mtime)
            n_missing_price_dash = int(dash_changes_val["est_value"].isna().sum())

            stock_agg = aggregate_change_values(dash_changes_val, "stock_code")
            etf_agg = aggregate_change_values(dash_changes_val, "etf_code")

            total_buy_dash = dash_changes_val.loc[dash_changes_val["est_value"] > 0, "est_value"].sum()
            total_sell_dash = dash_changes_val.loc[dash_changes_val["est_value"] < 0, "est_value"].sum()
            n_etf_involved = dash_changes_val["etf_code"].nunique()

            leader_line = ""
            if not etf_agg.empty and etf_agg["net"].max() > 0:
                top_etf_row = etf_agg.sort_values("net", ascending=False).iloc[0]
                leader_line = f"，其中 **{etf_label(top_etf_row['etf_code'])}** 淨加碼金額最高(約 {format_twd_amount(top_etf_row['net'])})"

            with st.container(border=True):
                st.markdown(f"**📊 今日主動圈快照 — {common_date}（{scope_choice}，共 {n_etf_involved} 檔ETF有異動）**")
                st.markdown(
                    f"今天共 **{len(dash_changes)}** 筆持股異動{leader_line}。"
                    f"（⚠️ 金額皆為「股數變化 × 當天收盤價」的估算值，非各基金實際申報金額；"
                    f"目前資料沒有產業別分類，暫不提供產業加碼統計）"
                )
                dash_c1, dash_c2 = st.columns(2)
                with dash_c1:
                    st.metric("總加碼估算金額", format_twd_amount(total_buy_dash))
                with dash_c2:
                    st.metric("總減碼估算金額", format_twd_amount(total_sell_dash))
                if n_missing_price_dash:
                    st.caption(f"⚠️ {n_missing_price_dash} 筆異動查不到對應收盤價，已從金額估算中略過。")

                st.divider()
                q1, q2, q3, q4 = st.columns(4)

                def _render_top3(container, title, df, key_col, label_func, ascending):
                    with container:
                        st.markdown(f"**{title}**")
                        sub = df.sort_values("net", ascending=ascending).head(3)
                        if sub.empty:
                            st.caption("無資料")
                            return
                        for _, r in sub.iterrows():
                            st.markdown(f"{label_func(r[key_col])}　:red[{format_twd_amount(r['net'])}]" if r["net"] >= 0
                                        else f"{label_func(r[key_col])}　:green[{format_twd_amount(r['net'])}]")
                            st.caption(f"加碼{format_twd_amount(r['gross_buy'])} / 減碼{format_twd_amount(r['gross_sell'])}")

                _render_top3(q1, "🔺 同步淨加碼 Top3(個股)", stock_agg, "stock_code",
                             lambda c: f"{c} {dash_changes_val[dash_changes_val['stock_code']==c]['stock_name'].iloc[0]}",
                             ascending=False)
                _render_top3(q2, "🔻 同步淨減碼 Top3(個股)", stock_agg, "stock_code",
                             lambda c: f"{c} {dash_changes_val[dash_changes_val['stock_code']==c]['stock_name'].iloc[0]}",
                             ascending=True)
                _render_top3(q3, "🔺 ETF 淨加碼 Top3", etf_agg, "etf_code", etf_label, ascending=False)
                _render_top3(q4, "🔻 ETF 淨減碼 Top3", etf_agg, "etf_code", etf_label, ascending=True)

            st.divider()

        common_df = cached_common_changes(etf_conn, etf_db_mtime, common_date, scope_codes, min_etf_count)
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
                # ⚠️ Streamlit 的小坑：selectbox/date_input 只要曾經被畫出來過一次，
                # 之後的 rerun 就會優先採用 st.session_state[該widget自己的key] 裡記住的值，
                # 這裡傳的 index=/value= 參數會被忽略。所以要「從按鈕跳轉、強制切換下面
                # 3️⃣區塊的預設值」，必須直接寫入下面小節那三個widget「自己的key」
                # (etf_chart_etf_select / etf_chart_stock_select / etf_chart_end_date_input)，
                # 而且要在3️⃣區塊的widget於本次rerun建立之前寫入(此區塊C本來就在區塊D之前執行，
                # 順序上沒問題)。

                # 這檔股票在「多數ETF共同買賣」是被好幾檔ETF一起異動，
                # 但K線圖區塊(3️⃣)一次只能看一檔ETF的標記，預設先帶入異動清單裡的第一檔，
                # 使用者到下面可以自己換成想看的其他ETF。
                etf_list_str = common_df.loc[
                    common_df["股票代碼"] == pick_stock_for_chart, "異動ETF清單"
                ].iloc[0]
                first_etf = etf_list_str.split("、")[0].strip() if etf_list_str else None

                if first_etf:
                    st.session_state["etf_chart_etf_select"] = first_etf
                    # 用跟區塊D完全相同的資料來源/組字串邏輯，確保能在下拉選單選項裡精準命中
                    held_df = cached_etf_held_stocks(etf_conn, etf_db_mtime, first_etf)
                    match_row = held_df[held_df["stock_code"] == pick_stock_for_chart]
                    if not match_row.empty:
                        nm = match_row["stock_name"].iloc[0]
                        st.session_state["etf_chart_stock_select"] = (
                            f"{pick_stock_for_chart} {nm}" if nm else str(pick_stock_for_chart)
                        )
                st.session_state["etf_chart_end_date_input"] = pd.to_datetime(common_date).date()
                st.info("已設定完成，請切換到上方「3️⃣ 個股K線 + ETF建倉標記」分頁查看標記結果 👆")
                st.rerun()
    else:
        st.info("尚無資料可顯示。")

with tab3:
    st.caption(
        "先選一檔主動式ETF，再選這檔ETF持有過的股票，"
        "看這檔ETF在這檔股票上的買賣點標示在K線圖上(簡化版K棒，只標記選定的這檔ETF)。"
        "完整回測/停利/移動停利等功能請到「Stock simulator」頁面查看。"
    )

    if twse_conn is None:
        st.warning(f"找不到 twse_ohlcv.db ({TWSE_DB_PATH})，無法繪製K線圖。")
    else:
        colD1, colD2, colD3, colD4 = st.columns([1, 1.2, 1, 1])

        # 這幾個widget的「預設值」都是用 st.session_state.setdefault(widget自己的key, ...) 的方式，
        # 只在該key第一次出現時塞入初始值；之後不管是使用者自己手動改選單、還是2️⃣的
        # 「✅ 查看K線圖」按鈕直接改寫這些key，widget都會照著session_state裡目前的值顯示——
        # 不能同時又傳 index=/value= 又寫session_state，Streamlit會發出警告(兩邊互相打架)。
        if all_active_codes:
            st.session_state.setdefault("etf_chart_etf_select", all_active_codes[0])

        with colD1:
            chart_etf_code = st.selectbox(
                "ETF代碼", options=all_active_codes, format_func=etf_label,
                key="etf_chart_etf_select",
            )

        # 2026-08-24新增：先把「K線結束日期」目前的值讀出來(還沒畫出date_input也沒關係，
        # 用setdefault確保session_state裡一定有值)，才能在下面決定股票下拉選單順序/
        # 顏色標示的時候知道「目前是哪一天」，用來查這檔ETF在這一天對每檔股票的異動方向。
        st.session_state.setdefault("etf_chart_end_date_input", datetime.now(TW_TZ).date())
        _highlight_date = st.session_state["etf_chart_end_date_input"]
        _highlight_date_str = pd.to_datetime(_highlight_date).strftime("%Y-%m-%d")

        held_stocks_df = cached_etf_held_stocks(etf_conn, etf_db_mtime, chart_etf_code)
        stock_options = [
            f"{r.stock_code} {r.stock_name}" if r.stock_name else str(r.stock_code)
            for r in held_stocks_df.itertuples()
        ]

        # 2026-08-24新增：查這檔ETF在「K線結束日期」當天對每檔股票的異動方向，
        # 下拉選單裡有異動的股票前面加🟢(加碼/新納入)或🔴(減碼/全數賣出)圖示。
        # 用format_func只影響「顯示文字」，selectbox實際回傳值還是原本的
        # "代碼 名稱"字串，不影響下面 chart_code = chart_stock_choice.split(" ")[0] 的邏輯。
        _same_day_changes = cached_holding_changes(
            etf_conn, etf_db_mtime, change_date=_highlight_date_str, etf_code=chart_etf_code
        )
        _direction_by_code = dict(zip(_same_day_changes["stock_code"], _same_day_changes["direction"])) \
            if not _same_day_changes.empty else {}

        def _stock_option_label(opt: str) -> str:
            code = opt.split(" ")[0]
            direction = _direction_by_code.get(code)
            if direction in ("加碼", "新納入"):
                return f"🟢 {opt}"
            if direction in ("減碼", "全數賣出"):
                return f"🔴 {opt}"
            return opt

        with colD2:
            if stock_options:
                # 換了ETF代碼、如果session_state裡記住的舊選項不屬於這檔ETF的持股清單，
                # 就清掉讓它自然回到清單第一筆(避免下面selectbox因為值不在options裡而報錯)。
                if st.session_state.get("etf_chart_stock_select") not in stock_options:
                    st.session_state["etf_chart_stock_select"] = stock_options[0]
                chart_stock_choice = st.selectbox(
                    "股票代碼", options=stock_options,
                    format_func=_stock_option_label,
                    key="etf_chart_stock_select",
                    help="🟢＝在「K線結束日期」當天加碼/新納入、🔴＝減碼/全數賣出，沒有圖示代表當天沒有異動。",
                )
            else:
                chart_stock_choice = None
                st.selectbox("股票代碼", options=["(尚無持股資料)"], disabled=True, key="etf_chart_stock_select_disabled")
        with colD3:
            # 2026-08-24新增：改成可以自由選「開始日期」，不再固定寫死回溯120天，
            # 預設值第一次還是抓120天前，之後使用者自己調整過就會照使用者選的範圍。
            st.session_state.setdefault(
                "etf_chart_start_date_input",
                (pd.to_datetime(_highlight_date) - timedelta(days=120)).date(),
            )
            chart_start_date_val = st.date_input("K線開始日期", key="etf_chart_start_date_input")
        with colD4:
            chart_end_date = st.date_input("K線結束日期", key="etf_chart_end_date_input")

        if not stock_options:
            st.info(f"{etf_label(chart_etf_code)} 在資料庫裡目前還沒有任何持股快照，無法選擇股票(可能排程還沒抓過這檔)。")
        elif pd.to_datetime(chart_start_date_val) > pd.to_datetime(chart_end_date):
            st.warning("K線開始日期比結束日期晚，請重新選擇。")
        else:
            chart_code = chart_stock_choice.split(" ")[0]
            chart_start_date = pd.to_datetime(chart_start_date_val).strftime("%Y-%m-%d")
            chart_end_str = pd.to_datetime(chart_end_date).strftime("%Y-%m-%d")

            ohlcv_df = db_utils.get_stock_ohlcv(twse_conn, chart_code, chart_start_date, chart_end_str)

            if ohlcv_df.empty:
                st.info(f"{chart_code} 在 {chart_start_date}~{chart_end_str} 期間查無K線資料。")
            else:
                events_df = cached_stock_etf_events(
                    etf_conn, etf_db_mtime, chart_code, start_date=chart_start_date, end_date=chart_end_str,
                    etf_codes=[chart_etf_code],
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
                    # 2026-09-03再修正：改成跟「6_Stock simulator.py」B/S標記完全一樣的
                    # 「實色底色文字方塊 + 箭頭」樣式(claude/圖表標記與回測功能優化_實作記錄_0903.md)，
                    # 取代原本「三角形marker旁邊直接貼文字(bottom/top center)」的做法——
                    # 原本的做法文字位置由plotly自動貼在marker旁邊，K棒密集時文字仍常常
                    # 貼到隔壁蠟燭。改成 fig.add_annotation()，文字方塊用固定像素距離
                    # (ax=0/ay)撐開到蠟燭範圍外、箭頭指回錨點，並比照同一份記錄裡「同一套
                    # 防重疊堆疊機制」的精神：同一小段日期範圍內有多筆標記時依序往外疊，
                    # 不會疊在同一個位置看不清楚。
                    # 錨點沿用上面已經修正過的當天Low(加碼/買)/High(減碼/賣)。
                    dates_list = ohlcv_df.index.tolist()
                    ANNOTATION_CLUSTER_WINDOW = 2  # 跟Stock simulator同一份記錄裡的分組寬度一致
                    ANNOTATION_BASE_OFFSET = 28
                    ANNOTATION_STACK_STEP = 22
                    ann_slot_count = {}

                    bullish_dates, bullish_y = [], []
                    bearish_dates, bearish_y = [], []

                    for _, ev in events_df.iterrows():
                        d = ev["change_date"]
                        if d not in ohlcv_df.index:
                            continue
                        is_bullish = ev["direction"] in ("加碼", "新納入")
                        y = ohlcv_df.loc[d, "Low"] if is_bullish else ohlcv_df.loc[d, "High"]
                        label = ev["direction"]
                        color = MARK_COLOR_BUY if is_bullish else MARK_COLOR_SELL

                        if is_bullish:
                            bullish_dates.append(d)
                            bullish_y.append(y)
                        else:
                            bearish_dates.append(d)
                            bearish_y.append(y)

                        try:
                            pos = dates_list.index(d)
                        except ValueError:
                            pos = 0
                        slot_key = (pos // ANNOTATION_CLUSTER_WINDOW, is_bullish)
                        slot = ann_slot_count.get(slot_key, 0)
                        ann_slot_count[slot_key] = slot + 1
                        offset = ANNOTATION_BASE_OFFSET + slot * ANNOTATION_STACK_STEP
                        ay = offset if is_bullish else -offset

                        fig.add_annotation(
                            x=d, y=y, text=label, showarrow=True, arrowhead=1,
                            arrowcolor=color, font=dict(color="white", size=11),
                            bgcolor=color, ax=0, ay=ay,
                        )

                    # 保留小三角形marker(不附文字)，純粹標出錨點位置+提供圖例，
                    # 實際的方向文字已經改由上面的 add_annotation 負責顯示。
                    if bullish_dates:
                        fig.add_trace(go.Scatter(
                            x=bullish_dates, y=bullish_y, mode="markers",
                            marker=dict(
                                symbol="triangle-up", size=14, color=MARK_COLOR_BUY,
                                line=dict(width=1.5, color="white"),
                            ),
                            name=f"{chart_etf_code} 加碼/新納入",
                        ))
                    if bearish_dates:
                        fig.add_trace(go.Scatter(
                            x=bearish_dates, y=bearish_y, mode="markers",
                            marker=dict(
                                symbol="triangle-down", size=14, color=MARK_COLOR_SELL,
                                line=dict(width=1.5, color="white"),
                            ),
                            name=f"{chart_etf_code} 減碼/全數賣出",
                        ))
                else:
                    st.caption(f"這檔股票在此期間沒有偵測到 {etf_label(chart_etf_code)} 的持股異動紀錄。")

                stock_name_part = chart_stock_choice.split(" ", 1)[1] if " " in chart_stock_choice else ""
                fig.update_layout(
                    title=f"{chart_code} {stock_name_part} — {etf_label(chart_etf_code)} 建倉標記",
                    xaxis_rangeslider_visible=False, height=560, showlegend=True,
                    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
                )
                fig.update_xaxes(type="category")
                st.plotly_chart(fig, use_container_width=True)

                # 2026-08-24新增：K線圖下方加上這檔ETF對這檔股票的完整買賣紀錄表，
                # 直接沿用畫圖用的 events_df(跟圖上三角形標記是同一份資料，
                # 不用另外查一次)，讓使用者不只看圖上的三角形，也能看到實際數字。
                st.markdown("#### 📋 買賣紀錄明細")
                if events_df.empty:
                    st.caption(f"這檔股票在此期間沒有偵測到 {etf_label(chart_etf_code)} 的持股異動紀錄。")
                else:
                    events_display_cols = [
                        "change_date", "change_type", "direction",
                        "weight_prev", "weight_curr", "weight_change",
                        "shares_prev", "shares_curr", "shares_change",
                    ]
                    events_display_names = {
                        "change_date": "異動日期", "change_type": "異動類型", "direction": "調整方向",
                        "weight_prev": "權重(前次)", "weight_curr": "權重(本次)", "weight_change": "權重變化",
                        "shares_prev": "張數(前次)", "shares_curr": "張數(本次)", "shares_change": "張數變化",
                    }
                    events_show_df = events_df[events_display_cols].copy()
                    for _col in ("shares_prev", "shares_curr", "shares_change"):
                        events_show_df[_col] = shares_series_to_lots(events_show_df[_col])
                    events_show_df = events_show_df.rename(columns=events_display_names)
                    st.dataframe(events_show_df, use_container_width=True, hide_index=True)

with tab4:
    st.caption(
        "查詢一段日期範圍內、資料庫裡已經收集到的持股異動紀錄。"
        "這裡純粹是瀏覽已經存進 etf_holdings.db 的資料，不會觸發新的抓取"
        "(要抓新資料請用左側「🔄 更新ETF買賣資料」)。"
    )

    if available_dates:
        _default_range_start = pd.to_datetime(available_dates[-1])  # available_dates 是新到舊排序
        _default_range_end = pd.to_datetime(available_dates[0])

        colF1, colF2, colF3 = st.columns([1, 1, 1.2])
        with colF1:
            range_start = st.date_input("起始日期", value=_default_range_start, key="range_query_start")
        with colF2:
            range_end = st.date_input("結束日期", value=_default_range_end, key="range_query_end")
        with colF3:
            range_scope_choice = st.radio(
                "查詢範圍", ["只看追蹤清單", "全部主動式ETF"], key="range_query_scope", horizontal=True
            )
        range_scope_codes = tracked_etfs if range_scope_choice == "只看追蹤清單" else all_active_codes

        if pd.to_datetime(range_start) > pd.to_datetime(range_end):
            st.warning("起始日期比結束日期晚，請重新選擇。")
        else:
            range_df = cached_holding_changes(
                etf_conn, etf_db_mtime,
                start_date=pd.to_datetime(range_start).strftime("%Y-%m-%d"),
                end_date=pd.to_datetime(range_end).strftime("%Y-%m-%d"),
                etf_codes=range_scope_codes,
            )
            if range_df.empty:
                st.info(f"{range_start} ~ {range_end} 期間，選定範圍內沒有查到任何持股異動紀錄。")
            else:
                st.caption(f"共 {len(range_df)} 筆異動紀錄。")
                range_display_cols = [
                    "change_date", "etf_code", "stock_code", "stock_name", "change_type", "direction",
                    "weight_prev", "weight_curr", "weight_change",
                    "shares_prev", "shares_curr", "shares_change",
                ]
                range_display_names = {
                    "change_date": "異動日期", "etf_code": "ETF代碼", "stock_code": "股票代碼", "stock_name": "股票名稱",
                    "change_type": "異動類型", "direction": "調整方向",
                    "weight_prev": "權重(前次)", "weight_curr": "權重(本次)", "weight_change": "權重變化",
                    "shares_prev": "張數(前次)", "shares_curr": "張數(本次)", "shares_change": "張數變化",
                }
                range_show_df = range_df[range_display_cols].copy()
                for _col in ("shares_prev", "shares_curr", "shares_change"):
                    range_show_df[_col] = shares_series_to_lots(range_show_df[_col])
                range_show_df = range_show_df.rename(columns=range_display_names)
                st.dataframe(
                    range_show_df,
                    use_container_width=True, hide_index=True,
                )
    else:
        st.info("尚無資料可查詢。")

with tab5:
    # 2026-08-24新增：原本只能「先選ETF、再看它買了哪些股票」，這裡反過來，
    # 輸入/選一檔股票代碼，直接看「這檔股票目前被範圍內哪些主動式ETF持有、
    # 各自權重多少」+「這檔股票過去被範圍內ETF加碼/減碼過的完整歷史」。
    # ⚠️ 查詢範圍限定在這個頁面本來就在追蹤的主動式ETF(ETF_data/active_etf_list.csv
    # 篩選出的清單，目前資料庫裡有資料的是6檔啟用中的)，不是台股市場所有ETF——
    # 資料庫 etf_holdings/etf_holding_changes 這兩張表本來就只存這個頁面在抓的
    # 主動式ETF資料，不含被動型ETF(例如0050)的持股。
    st.caption(
        "輸入或選一檔股票代碼，查看這檔股票目前被範圍內哪些主動式ETF持有、"
        "以及過去被這些ETF加碼/減碼過的完整歷史紀錄。"
        "⚠️ 查詢範圍限定在本頁面追蹤的主動式ETF清單，不含被動型ETF(如0050)。"
    )

    if available_dates:
        colG1, colG2 = st.columns([1, 2])
        with colG1:
            global_scope_choice = st.radio(
                "查詢範圍", ["只看追蹤清單", "全部主動式ETF"], key="global_query_scope", horizontal=True
            )
        global_scope_codes = tracked_etfs if global_scope_choice == "只看追蹤清單" else all_active_codes

        all_held_df = cached_all_held_stocks(etf_conn, etf_db_mtime, global_scope_codes)
        if all_held_df.empty:
            st.info("這個範圍內目前資料庫裡還沒有任何持股資料。")
        else:
            global_stock_options = [
                f"{r.stock_code} {r.stock_name}" if r.stock_name else str(r.stock_code)
                for r in all_held_df.itertuples()
            ]
            with colG2:
                global_pick = st.selectbox(
                    "選擇股票(可直接輸入代碼或名稱搜尋)",
                    options=global_stock_options, key="global_query_stock_pick",
                )
            global_code = global_pick.split(" ")[0]

            latest_df = cached_stock_latest_holdings(etf_conn, etf_db_mtime, global_code, global_scope_codes)
            history_df = cached_holding_changes(
                etf_conn, etf_db_mtime, stock_code=global_code, etf_codes=global_scope_codes,
            )

            gm1, gm2 = st.columns(2)
            gm1.metric("目前持有這檔股票的ETF數", f"{len(latest_df)} 檔")
            gm2.metric("歷史異動紀錄筆數", f"{len(history_df)} 筆")

            st.markdown("#### 📋 目前持有狀況(各ETF最新一次快照)")
            if latest_df.empty:
                st.caption(f"範圍內目前沒有任何ETF的最新快照持有 {global_pick} (可能已經被全部賣出，可以往下看歷史異動紀錄)。")
            else:
                latest_show = latest_df[["etf_code", "snapshot_date", "weight_text", "shares"]].copy()
                latest_show["etf_code"] = latest_show["etf_code"].apply(etf_label)
                latest_show["shares"] = shares_series_to_lots(latest_show["shares"])
                latest_show = latest_show.rename(columns={
                    "etf_code": "ETF", "snapshot_date": "快照日期",
                    "weight_text": "權重", "shares": "張數",
                })
                st.dataframe(latest_show, use_container_width=True, hide_index=True)

            st.markdown("#### 📋 歷史異動紀錄")
            if history_df.empty:
                st.caption(f"範圍內沒有查到 {global_pick} 的任何加碼/減碼歷史紀錄。")
            else:
                history_display_cols = [
                    "change_date", "etf_code", "change_type", "direction",
                    "weight_prev", "weight_curr", "weight_change",
                    "shares_prev", "shares_curr", "shares_change",
                ]
                history_display_names = {
                    "change_date": "異動日期", "etf_code": "ETF代碼",
                    "change_type": "異動類型", "direction": "調整方向",
                    "weight_prev": "權重(前次)", "weight_curr": "權重(本次)", "weight_change": "權重變化",
                    "shares_prev": "張數(前次)", "shares_curr": "張數(本次)", "shares_change": "張數變化",
                }
                history_show = history_df[history_display_cols].copy()
                for _col in ("shares_prev", "shares_curr", "shares_change"):
                    history_show[_col] = shares_series_to_lots(history_show[_col])
                history_show = history_show.rename(columns=history_display_names)
                st.dataframe(history_show, use_container_width=True, hide_index=True)
    else:
        st.info("尚無資料可查詢。")

st.divider()

# --------------------------------------------------------------------------
# Section E: 抓取狀態診斷
# --------------------------------------------------------------------------
with st.expander("🔧 抓取狀態診斷", expanded=False):
    log_df = cached_latest_fetch_log(etf_conn, etf_db_mtime, limit=60)
    if log_df.empty:
        st.caption("尚無抓取紀錄。")
    else:
        st.dataframe(
            log_df[["run_date", "etf_code", "status", "row_count", "message", "created_at"]],
            use_container_width=True, hide_index=True,
        )
