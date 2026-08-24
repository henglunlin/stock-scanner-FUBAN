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

active_etf_name_map = etf_watchlist.load_active_etf_name_map(ACTIVE_ETF_CSV)
all_active_codes = sorted(active_etf_name_map.keys())

if "etf_watchlist_cfg" not in st.session_state:
    st.session_state.etf_watchlist_cfg = etf_watchlist.load_watchlist_config(WATCHLIST_CONFIG_PATH)

cfg = st.session_state.etf_watchlist_cfg


def etf_label(code: str) -> str:
    name = active_etf_name_map.get(code, "")
    return f"{code} {name}" if name else code


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
# Section B~F：四個分析區塊改用橫向分頁(st.tabs)顯示，取代原本上下堆疊、
# 要一直往下滾的版面(2026-08-24調整)。同一次rerun裡所有分頁的程式碼都會執行
# (只是畫面上只顯示目前選中的分頁)，所以分頁之間互相連動(例如2️⃣按鈕跳轉到3️⃣
# 的K線圖)的session_state邏輯不用改，只是使用者要自己點一下切換分頁查看結果。
# --------------------------------------------------------------------------
tab1, tab2, tab3, tab4 = st.tabs([
    "1️⃣ 指定ETF買賣狀況", "2️⃣ 多數ETF共同買賣",
    "3️⃣ 個股K線 + ETF建倉標記", "4️⃣ 區間異動查詢",
])

with tab1:

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

            # 2026-08-24新增：除了「異動」明細，也讓使用者能直接看到這檔ETF在
            # 選定日期「當下」的完整持股成分股比例(不是只看有變動的部分)，
            # 資料來源沿用同一個 etf_holdings 快照表(etf_db.get_holdings_snapshot)，
            # 跟畫面上方顯示的異動明細本來就是同一份資料庫、同一次抓取結果。
            st.markdown("#### 📋 成分股比例")
            holdings_snapshot = etf_db.get_holdings_snapshot(etf_conn, pick_etf, pick_date)
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

                holdings_display_cols = ["stock_code", "stock_name", "weight_text", "shares_text"]
                holdings_display_names = {
                    "stock_code": "股票代碼", "stock_name": "股票名稱",
                    "weight_text": "權重", "shares_text": "股數",
                }
                st.dataframe(
                    holdings_snapshot[holdings_display_cols].rename(columns=holdings_display_names),
                    use_container_width=True, hide_index=True,
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
                    held_df = etf_db.get_etf_held_stocks(etf_conn, first_etf)
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
        colD1, colD2, colD3 = st.columns([1, 1.2, 1])

        # 這三個widget的「預設值」都是用 st.session_state.setdefault(widget自己的key, ...) 的方式，
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

        held_stocks_df = etf_db.get_etf_held_stocks(etf_conn, chart_etf_code)
        stock_options = [
            f"{r.stock_code} {r.stock_name}" if r.stock_name else str(r.stock_code)
            for r in held_stocks_df.itertuples()
        ]

        with colD2:
            if stock_options:
                # 換了ETF代碼、如果session_state裡記住的舊選項不屬於這檔ETF的持股清單，
                # 就清掉讓它自然回到清單第一筆(避免下面selectbox因為值不在options裡而報錯)。
                if st.session_state.get("etf_chart_stock_select") not in stock_options:
                    st.session_state["etf_chart_stock_select"] = stock_options[0]
                chart_stock_choice = st.selectbox(
                    "股票代碼", options=stock_options,
                    key="etf_chart_stock_select",
                )
            else:
                chart_stock_choice = None
                st.selectbox("股票代碼", options=["(尚無持股資料)"], disabled=True, key="etf_chart_stock_select_disabled")
        with colD3:
            st.session_state.setdefault("etf_chart_end_date_input", datetime.now(TW_TZ).date())
            chart_end_date = st.date_input("K線結束日期", key="etf_chart_end_date_input")

        if not stock_options:
            st.info(f"{etf_label(chart_etf_code)} 在資料庫裡目前還沒有任何持股快照，無法選擇股票(可能排程還沒抓過這檔)。")
        else:
            chart_code = chart_stock_choice.split(" ")[0]
            chart_start_date = (pd.to_datetime(chart_end_date) - timedelta(days=120)).strftime("%Y-%m-%d")
            chart_end_str = pd.to_datetime(chart_end_date).strftime("%Y-%m-%d")

            ohlcv_df = db_utils.get_stock_ohlcv(twse_conn, chart_code, chart_start_date, chart_end_str)

            if ohlcv_df.empty:
                st.info(f"{chart_code} 在 {chart_start_date}~{chart_end_str} 期間查無K線資料。")
            else:
                events_df = etf_db.get_stock_etf_events(
                    etf_conn, chart_code, start_date=chart_start_date, end_date=chart_end_str,
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
                    bullish_dates, bullish_y, bullish_text = [], [], []
                    bearish_dates, bearish_y, bearish_text = [], [], []
                    for _, ev in events_df.iterrows():
                        d = ev["change_date"]
                        if d not in ohlcv_df.index:
                            continue
                        y = ohlcv_df.loc[d, "High"] if ev["direction"] in ("加碼", "新納入") else ohlcv_df.loc[d, "Low"]
                        label = ev["direction"]
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
                            name=f"{chart_etf_code} 加碼/新納入",
                        ))
                    if bearish_dates:
                        fig.add_trace(go.Scatter(
                            x=bearish_dates, y=bearish_y, mode="markers+text",
                            marker=dict(symbol="triangle-down", size=12, color=MARK_COLOR_SELL),
                            text=bearish_text, textposition="bottom center",
                            textfont=dict(size=9, color=MARK_COLOR_SELL),
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
                        "shares_prev": "股數(前次)", "shares_curr": "股數(本次)", "shares_change": "股數變化",
                    }
                    events_show_df = events_df[events_display_cols].rename(columns=events_display_names)
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
            range_df = etf_db.get_holding_changes(
                etf_conn,
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
                    "shares_prev": "股數(前次)", "shares_curr": "股數(本次)", "shares_change": "股數變化",
                }
                st.dataframe(
                    range_df[range_display_cols].rename(columns=range_display_names),
                    use_container_width=True, hide_index=True,
                )
    else:
        st.info("尚無資料可查詢。")

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
