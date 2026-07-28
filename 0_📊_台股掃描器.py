"""
app.py
=======
台股掃描器主程式：只保留 UI 與主迴圈。
資料源（富邦/yfinance）邏輯在 common_fubon.py；
每一種掃描條件（訊號）的判斷邏輯在 signals/ 套件裡，一個訊號一個檔案，方便個別修改。
"""

import re
import os
import json
import copy
import time
import gc
import requests
import base64
import zipfile
from html import escape
from io import BytesIO
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from common_fubon import (
    REFRESH_SEC,
    FORCE_SCAN_ALL_STOCKS_FROM_FILE,
    ALL_STOCK_GROUP_NAME,
    STOCK_SCAN_FILE,
    FubonSDK,
    yf,
    load_all_stock_group_from_file,
    render_price_source_selector,
    bulk_download_yfinance_history,
    bulk_download_yfinance_today,
    download_stock_data_by_source,
    normalize_ohlc,
    get_last_price_by_source,
    get_stock_name,
)
from signals import (
    compute_indicators,
    plot_trend_breakout_chart,
    TREND_VOL_RATIO_MIN,
)
try:
    from signals import build_trend_breakout_chart_figure
except ImportError:
    # 若 signals/__init__.py 尚未重新匯出，直接從趨勢突破模組匯入。
    from signals.trend_breakout import build_trend_breakout_chart_figure

# ===== Streamlit UI 基本設定（一定要放最前面）=====
st.set_page_config(layout="wide")


# ===== 常數設定 =====
GROUP_EDIT_PIN = "1219"
GROUPS_FILE = "stock_groups.json"
BACKUP_DIR = "backups"
APP_LOGO = "dog.jpg"

# ===== 二階段過濾 / 追蹤 / GitHub Database 設定 =====
# GitHub repo: https://github.com/henglunlin/stock-scanner-FUBAN/tree/main/Database
GITHUB_DATABASE_DIR = st.secrets.get("GITHUB_DATABASE_DIR", "Database")
LOCAL_DATABASE_DIR = st.secrets.get("LOCAL_DATABASE_DIR", "Database")
TRACKING_FILE = os.path.join(LOCAL_DATABASE_DIR, "signal_tracking.csv")
SIGNAL_SCORE_MIN = float(st.secrets.get("SIGNAL_SCORE_MIN", 55))
PRIORITY_SCORE_MIN = float(st.secrets.get("PRIORITY_SCORE_MIN", 65))
AUTO_UPLOAD_GITHUB = bool(st.secrets.get("AUTO_UPLOAD_GITHUB", False))

# ===== UI 下拉選單相容工具 =====
def open_dropdown(label: str):
    """Streamlit 1.32+ 使用 st.popover；較舊版本自動退回 st.expander。"""
    if hasattr(st, "popover"):
        return st.popover(label, use_container_width=True)
    return st.expander(label, expanded=False)



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
    "低軌衛星": [
        "6285.TW", "2313.TW",
    ],
    "ABF": [
        "4958.TW", "3037.TW", "8046.TW", "3189.TW",
        "8996.TW", "5439.TWO", "8358.TWO",
    ],
    "記憶體": [
        "6770.TW", "2408.TW", "2344.TW", "8271.TW",
        "4967.TW", "3260.TWO", "2451.TW",
    ],
    "CCL": [
        "2383.TW", "6274.TWO", "6213.TW", "8039.TW"
    ],
    "CPO": [
        "4979.TWO", "3163.TWO", "4977.TW",
        "3081.TWO", "3450.TW", "6442.TW"
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


# ===== GitHub Database 上傳工具 =====
def ensure_local_database_dir():
    os.makedirs(LOCAL_DATABASE_DIR, exist_ok=True)


def github_repo_config():
    return {
        "token": st.secrets.get("GITHUB_TOKEN", ""),
        "owner": st.secrets.get("GITHUB_OWNER", "henglunlin"),
        "repo": st.secrets.get("GITHUB_REPO", "stock-scanner-FUBAN"),
        "branch": st.secrets.get("GITHUB_BRANCH", "main"),
    }


def upload_file_to_github(file_bytes: bytes, github_path: str, commit_message: str) -> bool:
    """使用 GitHub Contents API 將檔案建立或更新到 repo。"""
    cfg = github_repo_config()
    token, owner, repo, branch = cfg["token"], cfg["owner"], cfg["repo"], cfg["branch"]
    if not token or not owner or not repo:
        st.error("GitHub Token / Owner / Repo 尚未設定，無法上傳到 GitHub。")
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
        elif get_res.status_code != 404:
            st.error(f"讀取 GitHub 既有檔案失敗：{get_res.status_code} {get_res.text}")
            return False

        payload = {
            "message": commit_message,
            "content": base64.b64encode(file_bytes).decode("utf-8"),
            "branch": branch,
        }
        if sha:
            payload["sha"] = sha

        put_res = requests.put(url, headers=headers, json=payload, timeout=30)
        if put_res.status_code in (200, 201):
            html_url = put_res.json().get("content", {}).get("html_url", "")
            st.success(f"上傳成功")
            return True

        st.error(f"上傳 GitHub 失敗：{put_res.status_code} {put_res.text}")
        return False
    except Exception as e:
        st.error(f"GitHub 上傳例外：{e}")
        return False


def tracking_github_filename(dt=None) -> str:
    """同一天固定同一個檔名，重複上傳只會更新同日檔案。"""
    if dt is None:
        dt = datetime.now(ZoneInfo("Asia/Taipei"))
    elif isinstance(dt, str):
        try:
            dt = datetime.strptime(dt[:10], "%Y-%m-%d")
        except Exception:
            dt = datetime.now(ZoneInfo("Asia/Taipei"))
    return f"signal_tracking_{dt.strftime('%Y%m%d')}.csv"


def tracking_github_path(dt=None) -> str:
    return f"{GITHUB_DATABASE_DIR}/{tracking_github_filename(dt)}"


def upload_tracking_file_to_github(commit_suffix: str = "") -> bool:
    if not os.path.exists(TRACKING_FILE):
        st.warning(f"尚未建立追蹤檔：{TRACKING_FILE}")
        return False
    with open(TRACKING_FILE, "rb") as f:
        data = f.read()
    upload_dt = datetime.now(ZoneInfo("Asia/Taipei"))
    suffix = f" {commit_suffix}" if commit_suffix else ""
    return upload_file_to_github(
        data,
        tracking_github_path(upload_dt),
        f"Update {tracking_github_filename(upload_dt)}{suffix}",
    )


# ===== 二階段過濾：訊號品質分數 / 追蹤工具 =====
def safe_float(x, default=0):
    try:
        if x in ["-", "", None]:
            return default
        return float(x)
    except Exception:
        return default


def classify_signal_grade(score):
    score = safe_float(score, 0)
    if score >= 75:
        return "A強勢追蹤"
    elif score >= 65:
        return "B可追蹤"
    elif score >= 55:
        return "C觀察"
    return "D過濾"


def calc_signal_quality_score(data, signal_types):
    score = 0

    # 1. 價格位置：避免接刀
    ma_range = data.get("ma_range", "")
    ma_trend = data.get("ma_trend", "")

    if ma_range == ">MA5":
        score += 15
    elif ma_range == "MA5~10":
        score += 10
    elif ma_range == "MA10~20":
        score += 5
    elif ma_range == "<MA20":
        score -= 15

    if ma_trend == "多頭":
        score += 15
    elif ma_trend == "糾結":
        score += 5
    elif ma_trend == "空頭":
        score -= 10

    # 2. 相對強度：優先抓比大盤 / 族群強的股票
    rs = safe_float(data.get("rs_raw", 0))
    if rs >= 5:
        score += 20
    elif rs >= 2:
        score += 12
    elif rs >= 0:
        score += 5
    else:
        score -= 10

    # 3. 量能確認
    vol_ratio = safe_float(data.get("trend_vol_ratio", 0))
    volume_lots = safe_float(data.get("volume_lots", 0))

    if volume_lots >= 3000:
        score += 8
    elif volume_lots >= 1000:
        score += 4

    if vol_ratio >= 2:
        score += 12
    elif vol_ratio >= 1.2:
        score += 6

    # 4. KD 位置：避免太高追價，也避免太弱接刀
    k = safe_float(data.get("k", 0))
    d = safe_float(data.get("d", 0))
    if 35 <= k <= 75 and k > d:
        score += 12
    elif k > 85:
        score -= 8
    elif k < 15:
        score -= 6

    # 5. 週KD：週線方向比日線更重要
    week_signal = data.get("week_kd_signal", "")
    if week_signal == "黃金交叉":
        score += 15
    elif week_signal == "即將黃金交叉":
        score += 8
    elif week_signal == "超賣":
        score -= 5

    # 6. MACD
    macd_hist = safe_float(data.get("macd_hist", 0))
    macd_signal = data.get("macd_signal", "")
    if macd_signal == "MACD翻正":
        score += 12
    elif macd_hist > 0:
        score += 6
    elif macd_hist < 0:
        score -= 5

    # 7. 趨勢突破品質
    if data.get("trend_signal") == "趨勢突破":
        score += 20

    touch_count = safe_float(data.get("trend_touch_count", 0))
    violations = safe_float(data.get("trend_violations", 0))
    if touch_count >= 2 and violations == 0:
        score += 8
    elif violations > 0:
        score -= 10

    # 8. 避免暴衝過熱
    pct = safe_float(data.get("pct", 0))
    volatility = safe_float(data.get("volatility_pct", 0))
    if pct >= 8:
        score -= 8
    elif 1 <= pct <= 5:
        score += 5

    if volatility >= 15:
        score -= 8
    elif volatility <= 8:
        score += 4

    # 9. 訊號共振：多個訊號同時出現，比單一訊號可靠
    unique_signal_count = len(set(signal_types))
    if unique_signal_count >= 3:
        score += 12
    elif unique_signal_count == 2:
        score += 6

    # 10. 特殊修正：避免空頭弱反彈被誤判成強訊號
    if "即將黃金交叉" in signal_types:
        if ma_trend == "空頭" and ma_range == "<MA20":
            score -= 20

    if "黃金交叉" in signal_types:
        if ma_range in [">MA5", "MA5~10"]:
            score += 10
        elif ma_range == "<MA20":
            score -= 10

    return max(0, min(100, round(score, 1)))


def build_priority_rows(all_signal_rows, min_score=None):
    if min_score is None:
        min_score = PRIORITY_SCORE_MIN
    if not all_signal_rows:
        return []
    priority_rows = []
    for row in all_signal_rows:
        score = safe_float(row.get("訊號分數", 0))
        if score >= float(min_score):
            priority_rows.append(row.copy())
    return sorted(
        priority_rows,
        key=lambda r: (
            safe_float(r.get("訊號分數", 0)),
            safe_float(r.get("RS加權報酬%", 0)),
            safe_float(r.get("量能倍數", 0)),
        ),
        reverse=True,
    )


def append_signal_tracking(row, scan_date, tracking_file=TRACKING_FILE):
    ensure_local_database_dir()
    base_cols = [
        "scan_date", "代碼", "股票名稱", "entry_price",
        "訊號類型", "訊號分數", "追蹤等級",
        "RS加權報酬%", "MA位置", "MA排列",
        "K值", "D值", "週K值", "週D值",
        "MACD柱", "趨勢突破", "量能倍數",
        "status",
    ]
    new_record = {
        "scan_date": scan_date,
        "代碼": row.get("代碼"),
        "股票名稱": row.get("股票名稱"),
        "entry_price": row.get("價格"),
        "訊號類型": row.get("訊號類型"),
        "訊號分數": row.get("訊號分數"),
        "追蹤等級": row.get("追蹤等級"),
        "RS加權報酬%": row.get("RS加權報酬%"),
        "MA位置": row.get("MA位置"),
        "MA排列": row.get("MA排列"),
        "K值": row.get("K值"),
        "D值": row.get("D值"),
        "週K值": row.get("週K值"),
        "週D值": row.get("週D值"),
        "MACD柱": row.get("MACD柱"),
        "趨勢突破": row.get("趨勢突破"),
        "量能倍數": row.get("量能倍數"),
        "status": "tracking",
    }

    if os.path.exists(tracking_file):
        df = pd.read_csv(tracking_file, encoding="utf-8-sig")
    else:
        df = pd.DataFrame(columns=base_cols)

    if df.empty:
        should_append = True
    else:
        key = (
            (df["scan_date"].astype(str) == str(scan_date))
            & (df["代碼"].astype(str) == str(row.get("代碼")))
        )
        should_append = not key.any()

    if should_append:
        df = pd.concat([df, pd.DataFrame([new_record])], ignore_index=True)
        df.to_csv(tracking_file, index=False, encoding="utf-8-sig")



# ===== Excel 匯出工具 =====
def normalize_rows_for_excel(rows):
    columns = ["代碼", "股票名稱", "價格", "漲跌%", "成交量(張)", "波動率%", "RS加權報酬%", "訊號分數", "追蹤等級", "P1日期", "區高P1", "P2日期", "近高P2", "坡度%", "趨勢價", "趨勢突破", "貼線數", "穿線數", "量能倍數", "MA位置", "MA排列", "K值", "D值", "KD訊號", "週K值", "週D值", "週KD訊號", "MACD柱", "MACD訊號", "跳空訊號", "訊號類型", "來源"]
    if not rows:
        return pd.DataFrame(columns=columns)
    df = pd.DataFrame(rows).drop_duplicates(subset=["代碼"]).copy()
    if "代碼網址" in df.columns:
        df.drop(columns=["代碼網址"], inplace=True)
    for col in columns:
        if col not in df.columns:
            df[col] = ""
    return df[columns]

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

def build_signal_excel_bytes(signal_buckets: dict) -> bytes:
    priority_rows = signal_buckets.get("優先追蹤", [])
    gain_rows = signal_buckets.get("漲幅達標", [])
    gap_rows = signal_buckets.get("跳空", [])
    golden_rows = signal_buckets.get("黃金交叉", [])
    near_golden_rows = signal_buckets.get("即將黃金交叉", [])
    week_golden_rows = signal_buckets.get("週黃金交叉", [])
    week_near_golden_rows = signal_buckets.get("週即將黃金交叉", [])
    macd_rows = signal_buckets.get("MACD翻正", [])
    trend_rows = signal_buckets.get("趨勢突破", [])

    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        normalize_rows_for_excel(priority_rows).to_excel(writer, sheet_name="優先追蹤", index=False)
        normalize_rows_for_excel(gain_rows).to_excel(writer, sheet_name="漲幅達標", index=False)
        normalize_rows_for_excel(gap_rows).to_excel(writer, sheet_name="跳空", index=False)
        normalize_rows_for_excel(golden_rows).to_excel(writer, sheet_name="黃金交叉", index=False)
        normalize_rows_for_excel(near_golden_rows).to_excel(writer, sheet_name="即將黃金交叉", index=False)
        normalize_rows_for_excel(week_golden_rows).to_excel(writer, sheet_name="週黃金交叉", index=False)
        normalize_rows_for_excel(week_near_golden_rows).to_excel(writer, sheet_name="週即將黃金交叉", index=False)
        normalize_rows_for_excel(macd_rows).to_excel(writer, sheet_name="MACD訊號", index=False)
        normalize_rows_for_excel(trend_rows).to_excel(writer, sheet_name="趨勢突破", index=False)
        apply_excel_fonts(writer.book)
    output.seek(0)
    return output.getvalue()


def build_trend_chart_zip_bytes(trend_chart_store: dict) -> bytes:
    """
    將所有趨勢突破圖表打包成 ZIP。
    ZIP 內容包含：
    1) 每檔股票各一個可互動 HTML 圖表。
    2) 一個「全部圖統整.html」，把所有圖表集中在同一頁方便快速檢視。
    """
    output = BytesIO()
    combined_sections = []

    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for symbol, chart_info in sorted((trend_chart_store or {}).items()):
            stock_name = str(chart_info.get("name", "") or "")
            fig = build_trend_breakout_chart_figure(symbol, stock_name, chart_info)
            if fig is None:
                continue

            safe_symbol = re.sub(r"[^0-9A-Za-z._-]+", "_", str(symbol)).strip("_") or "stock"
            safe_name = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "_", stock_name).strip("_")
            filename = f"{safe_symbol}_{safe_name}_trend_breakout.html" if safe_name else f"{safe_symbol}_trend_breakout.html"

            # 個別圖檔：每個 HTML 自帶 plotly.js，方便單獨開啟。
            individual_html = fig.to_html(full_html=True, include_plotlyjs=True)
            zf.writestr(filename, individual_html)

            # 統整圖檔：第一張圖帶 plotly.js，其餘圖共用，避免檔案過大。
            include_js = "cdn" if not combined_sections else False
            combined_sections.append(
                f"<section style='margin: 0 0 36px 0; padding-bottom: 24px; border-bottom: 1px solid #e5e7eb;'>"
                f"<h2 style='font-family: Arial, Microsoft JhengHei, sans-serif;'>{symbol} {stock_name}</h2>"
                f"{fig.to_html(full_html=False, include_plotlyjs=include_js)}"
                f"</section>"
            )

        if combined_sections:
            combined_html = (
                "<!doctype html>\n"
                "<html lang=\"zh-Hant\">\n"
                "<head>\n"
                "  <meta charset=\"utf-8\">\n"
                "  <title>趨勢突破全部圖統整</title>\n"
                "</head>\n"
                "<body style=\"margin: 24px; font-family: Arial, Microsoft JhengHei, sans-serif;\">\n"
                "  <h1>趨勢突破全部圖統整</h1>\n"
                "  <p>本檔案彙整本次掃描中所有「趨勢突破」個股圖表。</p>\n"
                + "\n".join(combined_sections)
                + "\n</body>\n</html>"
            )
            zf.writestr("00_趨勢突破_全部圖統整.html", combined_html)

    output.seek(0)
    return output.getvalue()


# ===== 輔助工具函式 =====
def make_anchor_id(group_name: str) -> str:
    anchor = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "-", group_name).strip("-")
    return f"group-{anchor}"

def yahoo_quote_url(symbol: str) -> str:
    fubon_symbol = str(symbol).split(".")[0]
    return f"https://tw.stock.yahoo.com/quote/{fubon_symbol}"

def normalize_symbols_from_text(text: str):
    if not text:
        return []
    text = text.replace("，", ",")
    lines = []
    for raw_line in text.splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        parts = [p.strip().upper() for p in raw_line.split(",") if p.strip()]
        lines.extend(parts)
    seen = set()
    result = []
    for s in lines:
        if s not in seen:
            seen.add(s)
            result.append(s)
    return result

def validate_and_normalize_group_json(data):
    if not isinstance(data, dict) or not data:
        raise ValueError("JSON 格式錯誤：最外層必須是非空物件（dict）")
    validated = {}
    for group_name, symbols in data.items():
        group_name = str(group_name).strip()
        if not group_name:
            raise ValueError("JSON 格式錯誤：分類名稱不可為空")
        if isinstance(symbols, list):
            raw_text = "\n".join(str(x) for x in symbols)
        elif isinstance(symbols, str):
            raw_text = symbols
        else:
            raise ValueError(f"JSON 格式錯誤：分類「{group_name}」的股票清單必須是 list 或 string")
        normalized_symbols = normalize_symbols_from_text(raw_text)
        validated[group_name] = normalized_symbols
    if not validated:
        raise ValueError("JSON 內容為空")
    return validated

def normalize_symbol_quick(input_text: str):
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

def set_next_selected_group(group_name: str):
    st.session_state._next_selected_group = group_name

def enter_edit_mode():
    st.session_state.editing_mode = True

def leave_edit_mode():
    st.session_state.editing_mode = False

def symbol_to_code(symbol: str) -> str:
    return str(symbol).split(".")[0]

def build_top3_html(valid_stock_stats):
    if not valid_stock_stats:
        return '<span style="color:#666666;">無可用資料</span>'
    top3_sorted = sorted(valid_stock_stats, key=lambda x: x["pct"], reverse=True)[:3]
    parts = []
    for item in top3_sorted:
        pct = float(item["pct"])
        if pct > 0:
            pct_color = "#cf1322"
        elif pct < 0:
            pct_color = "#389e0d"
        else:
            pct_color = "#333333"
        code_text = escape(str(item["code"]))
        name_text = escape(str(item["name"]))
        pct_text = f"{pct:+.1f}%"
        parts.append(
            f'<span style="color:#000000;">{code_text} {name_text} </span>'
            f'<span style="color:{pct_color}; font-weight:600;">{pct_text}</span>'
        )
    return " | ".join(parts)

def compact_name_list(names, max_show=3):
    names = [str(x).strip() for x in names if str(x).strip()]
    if not names:
        return "無"
    if len(names) <= max_show:
        return "、".join(names)
    return "、".join(names[:max_show]) + f" 等{len(names)}檔"

# ===== Session State 初始化 =====
if "auto_refresh_enabled" not in st.session_state:
    st.session_state.auto_refresh_enabled = False

if "refresh_sec" not in st.session_state:
    st.session_state.refresh_sec = REFRESH_SEC

if "tg_push_enabled" not in st.session_state:
    st.session_state.tg_push_enabled = False 

if "scheduled_push_enabled" not in st.session_state:
    st.session_state.scheduled_push_enabled = True 

if "processed_time_slots" not in st.session_state:
    st.session_state.processed_time_slots = set() 

if "stock_groups" not in st.session_state:
    st.session_state.stock_groups = load_all_stock_group_from_file() if FORCE_SCAN_ALL_STOCKS_FROM_FILE else load_stock_groups()

if FORCE_SCAN_ALL_STOCKS_FROM_FILE:
    st.session_state.stock_groups = load_all_stock_group_from_file()
if "price_source_mode" not in st.session_state:
    st.session_state.price_source_mode = "自動"
if "scan_enabled" not in st.session_state:
    st.session_state.scan_enabled = False
if "scan_requested" not in st.session_state:
    st.session_state.scan_requested = False

if "group_editor_unlocked" not in st.session_state:
    st.session_state.group_editor_unlocked = False

if "editing_mode" not in st.session_state:
    st.session_state.editing_mode = False

if "fubon_sdk" not in st.session_state:
    st.session_state.fubon_sdk = None

if "fubon_logged_in" not in st.session_state:
    st.session_state.fubon_logged_in = False

if "selected_group_editor" not in st.session_state:
    group_names_init = list(st.session_state.stock_groups.keys())
    st.session_state.selected_group_editor = group_names_init[0] if group_names_init else ""

if "rename_group_input" not in st.session_state:
    st.session_state.rename_group_input = st.session_state.selected_group_editor

if "symbols_text_area" not in st.session_state:
    selected = st.session_state.selected_group_editor
    st.session_state.symbols_text_area = "\n".join(
        st.session_state.stock_groups.get(selected, [])
    )

if "quick_add_symbol_input" not in st.session_state:
    st.session_state.quick_add_symbol_input = ""

if "notified_stocks" not in st.session_state:
    st.session_state.notified_stocks = set()

if "tg_last_update_id" not in st.session_state:
    st.session_state.tg_last_update_id = None
if "trend_charts_collapsed" not in st.session_state:
    st.session_state.trend_charts_collapsed = True
if "trend_charts_collapse_version" not in st.session_state:
    st.session_state.trend_charts_collapse_version = 0

if "_next_selected_group" in st.session_state:
    pending_group = st.session_state._next_selected_group
    del st.session_state._next_selected_group
    if pending_group in st.session_state.stock_groups:
        st.session_state.selected_group_editor = pending_group
        st.session_state.rename_group_input = pending_group
        st.session_state.symbols_text_area = "\n".join(
            st.session_state.stock_groups.get(pending_group, [])
        )

def sync_editor_fields_from_selected_group():
    groups = st.session_state.stock_groups
    selected_group = st.session_state.selected_group_editor
    if selected_group not in groups:
        group_names = list(groups.keys())
        if group_names:
            selected_group = group_names[0]
            st.session_state.selected_group_editor = selected_group
        else:
            selected_group = ""
    st.session_state.rename_group_input = selected_group
    st.session_state.symbols_text_area = "\n".join(groups.get(selected_group, []))
    st.session_state.editing_mode = False

# ===== UI 元件 =====
def render_auto_refresh_settings():
    with st.sidebar.expander("🔄 自動刷新設定", expanded=True):
        st.toggle(
            "啟用自動刷新",
            key="auto_refresh_enabled",
            help="開啟後會依照下方秒數自動重新整理；分組編輯解鎖或編輯中會自動暫停。",
        )
        st.number_input(
            "刷新秒數",
            min_value=1,
            max_value=300,
            step=1,
            key="refresh_sec",
            help="自動刷新間隔秒數，預設 3 秒。",
        )

def render_fubon_login():
    st.sidebar.markdown("## 🔑 富邦 API 設定 (Fubon Neo)")
    
    if st.session_state.fubon_logged_in:
        st.sidebar.success("✅ 富邦 API 已成功連線")
        if st.sidebar.button("登出 / 重新連線", use_container_width=True):
            st.session_state.fubon_sdk = None
            st.session_state.fubon_logged_in = False
            st.rerun()
        return

    try:
        fubon_secrets = st.secrets["fubon"]
        pfx_base64 = fubon_secrets["pfx_base64"]
    except KeyError:
        st.sidebar.error("❌ 找不到 Streamlit Secrets 中的 pfx_base64 憑證資料。")
        return

    st.sidebar.info("請輸入富邦證券登入資訊")
    f_id = st.sidebar.text_input("身分證字號", key="f_id_input")
    f_pw = st.sidebar.text_input("富邦登入密碼", key="f_pw_input", type="password")
    f_cert_pw = st.sidebar.text_input("憑證密碼", key="f_cert_pw_input", type="password")

    if st.sidebar.button("連線行情伺服器", use_container_width=True):
        if not f_id or not f_pw or not f_cert_pw:
            st.sidebar.warning("請填寫完整的身分證字號與密碼！")
        else:
            try:
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

def render_group_editor_lock():
    st.sidebar.markdown("## 🔐 分組編輯鎖")
    if st.session_state.group_editor_unlocked:
        st.sidebar.success("已解鎖，可編輯股票分組")
        st.sidebar.info("為避免編輯中被重刷，分組編輯解鎖時會暫停自動更新")
        if st.sidebar.button("鎖定編輯", key="lock_group_editor_btn", use_container_width=True):
            st.session_state.group_editor_unlocked = False
            leave_edit_mode()
            st.rerun()
        return

    pin_input = st.sidebar.text_input(
        "請輸入 PIN 碼以編輯分組", type="password", key="group_edit_pin_input"
    )
    if st.sidebar.button("解鎖編輯", key="unlock_group_editor_btn", use_container_width=True):
        if pin_input == GROUP_EDIT_PIN:
            st.session_state.group_editor_unlocked = True
            enter_edit_mode()
            st.sidebar.success("PIN 正確，已解鎖")
            st.rerun()
        else:
            st.sidebar.error("PIN 錯誤")

def render_stock_group_editor():
    st.sidebar.markdown("## 🛠️ 股票分組編輯")
    groups = st.session_state.stock_groups
    group_names = list(groups.keys())

    if not group_names:
        st.session_state.stock_groups = copy.deepcopy(DEFAULT_STOCK_GROUPS)
        groups = st.session_state.stock_groups
        group_names = list(groups.keys())

    if st.session_state.selected_group_editor not in group_names:
        first_group = group_names[0]
        st.session_state.selected_group_editor = first_group
        st.session_state.rename_group_input = first_group
        st.session_state.symbols_text_area = "\n".join(groups.get(first_group, []))

    with st.sidebar.expander("➕ 新增分類", expanded=False):
        new_group_name = st.text_input("分類名稱", key="new_group_name_input")
        if st.button("新增分類", key="add_group_btn", use_container_width=True):
            enter_edit_mode()
            name = new_group_name.strip()
            if not name:
                st.sidebar.warning("請輸入分類名稱")
            elif name in groups:
                st.sidebar.warning("分類名稱已存在")
            else:
                groups[name] = []
                st.session_state.stock_groups = groups
                save_stock_groups(groups)
                set_next_selected_group(name)
                st.rerun()

    with st.sidebar.expander("📝 編輯分類", expanded=True):
        st.selectbox("選擇分類", options=group_names, key="selected_group_editor", on_change=sync_editor_fields_from_selected_group)
        selected_group = st.session_state.selected_group_editor
        new_group_name = st.text_input("分類名稱（可修改）", key="rename_group_input", on_change=enter_edit_mode)
        symbols_text = st.text_area("股票清單（每行一檔，或逗號分隔）", height=220, key="symbols_text_area", on_change=enter_edit_mode)

        st.markdown("### ⚡ 快速新增股票搜尋")
        quick_col1, quick_col2 = st.columns([2, 1])
        with quick_col1:
            quick_input = st.text_input("輸入股票代碼或 ticker", key="quick_add_symbol_input", on_change=enter_edit_mode)
        normalized_quick_symbol = normalize_symbol_quick(quick_input)
        if normalized_quick_symbol:
            st.caption(f"標準化代碼：{normalized_quick_symbol}")

        with quick_col2:
            if st.button("加入目前分類", key="quick_add_btn", use_container_width=True):
                enter_edit_mode()
                symbol = normalize_symbol_quick(quick_input)
                if not symbol:
                    st.warning("請輸入股票代碼")
                else:
                    current_list = groups.get(selected_group, [])
                    if symbol in current_list:
                        st.warning("此股票已存在於目前分類")
                    else:
                        current_list.append(symbol)
                        groups[selected_group] = current_list
                        st.session_state.stock_groups = groups
                        save_stock_groups(groups)
                        st.session_state.symbols_text_area = "\n".join(current_list)
                        st.session_state.quick_add_symbol_input = ""
                        st.success(f"已加入 {symbol}")
                        st.rerun()

        col1, col2 = st.columns(2)
        with col1:
            if st.button("💾 儲存分類", key="save_group_btn", use_container_width=True):
                new_name = new_group_name.strip()
                if not new_name:
                    st.sidebar.warning("分類名稱不可為空")
                elif new_name != selected_group and new_name in groups:
                    st.sidebar.warning("分類名稱已存在，請使用其他名稱")
                else:
                    new_symbols = normalize_symbols_from_text(symbols_text)
                    updated = {}
                    for k, v in groups.items():
                        if k == selected_group:
                            updated[new_name] = new_symbols
                        else:
                            updated[k] = v
                    st.session_state.stock_groups = updated
                    save_stock_groups(updated)
                    leave_edit_mode()
                    set_next_selected_group(new_name)
                    st.rerun()
        with col2:
            if st.button("🗑️ 刪除分類", key="delete_group_btn", use_container_width=True):
                if len(groups) <= 1:
                    st.sidebar.warning("至少保留一個分類")
                else:
                    groups.pop(selected_group, None)
                    st.session_state.stock_groups = groups
                    save_stock_groups(groups)
                    leave_edit_mode()
                    remaining = list(groups.keys())
                    set_next_selected_group(remaining[0])
                    st.rerun()

    with st.sidebar.expander("📦 備份 / 匯出 / 匯入 JSON", expanded=False):
        export_json_str = json.dumps(st.session_state.stock_groups, ensure_ascii=False, indent=2)
        st.download_button(label="⬇️ 匯出目前分組 JSON", data=export_json_str, file_name="stock_groups.json", mime="application/json", key="download_groups_json_btn", use_container_width=True)
        if st.button("🗂️ 建立本地備份", key="create_local_backup_btn", use_container_width=True):
            try:
                backup_file = save_backup_snapshot(st.session_state.stock_groups)
                st.sidebar.success(f"已建立備份：{os.path.basename(backup_file)}")
            except Exception as e:
                st.sidebar.error(f"建立備份失敗：{e}")
        uploaded_file = st.file_uploader("上傳股票分組 JSON", type=["json"], key="upload_groups_json_file")
        if uploaded_file is not None:
            st.caption("上傳後按下「匯入並覆蓋目前分組」才會生效")
            if st.button("📥 匯入並覆蓋目前分組", key="import_groups_json_btn", use_container_width=True):
                try:
                    raw = uploaded_file.read()
                    data = json.loads(raw.decode("utf-8"))
                    validated = validate_and_normalize_group_json(data)
                    save_backup_snapshot(st.session_state.stock_groups)
                    st.session_state.stock_groups = validated
                    save_stock_groups(validated)
                    leave_edit_mode()
                    first_group = list(validated.keys())[0]
                    set_next_selected_group(first_group)
                    st.sidebar.success("JSON 匯入成功，已覆蓋目前股票分組")
                    st.rerun()
                except Exception as e:
                    st.sidebar.error(f"JSON 匯入失敗：{e}")

        backups = list_backup_files()
        if backups:
            st.markdown("**最近備份檔**")
            for name in backups[:5]:
                st.caption(name)
        else:
            st.caption("目前沒有本地備份檔")

    with st.sidebar.expander("♻️ 重設", expanded=False):
        if st.button("還原預設分組", key="reset_groups_btn", use_container_width=True):
            try:
                save_backup_snapshot(st.session_state.stock_groups)
            except Exception:
                pass
            st.session_state.stock_groups = copy.deepcopy(DEFAULT_STOCK_GROUPS)
            save_stock_groups(st.session_state.stock_groups)
            leave_edit_mode()
            first_group = list(st.session_state.stock_groups.keys())[0]
            set_next_selected_group(first_group)
            st.rerun()

    with st.sidebar.expander("👀 分組預覽", expanded=False):
        for g, symbols in st.session_state.stock_groups.items():
            st.markdown(f"**{g}**（{len(symbols)}檔）")
            st.caption(", ".join(symbols) if symbols else "（空）")



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

def format_gap(val):
    if val == "跳空": return "🔴 跳空"
    return "-"

def format_trend(val):
    if val == "趨勢突破": return "🔥 突破"
    return "-"

def format_vol_ratio(val):
    try:
        v = float(val)
    except (TypeError, ValueError):
        return "-"
    if v >= TREND_VOL_RATIO_MIN:
        return f"🔥 {v:.2f}x"
    return f"{v:.2f}x"

def format_volume(val):
    try:
        return f"{float(val):,.1f}"
    except Exception:
        return val


def format_volatility(val):
    try:
        return f"{float(val):.2f}%"
    except Exception:
        return val


def render_scan_progress_card(placeholder, pct: float, status_text: str = "掃描進度"):
    pct = max(0.0, min(float(pct), 100.0))
    placeholder.markdown(
        f"""
        <div style="
            width: 120px;
            min-height: 78px;
            border: none;
            border-radius: 0;
            padding: 8px 10px;
            text-align: left;
            background: transparent;
            box-sizing: border-box;
        ">
            <div style="font-size: 30px; line-height: 1; font-weight: 800;">{pct:.0f}%</div>
            <div style="font-size: 13px; margin-top: 8px;">{status_text}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

def render_summary_dashboard(group_up_summary, rise_threshold):
    st.markdown("### 📌 漲幅儀表板")
    st.caption(f"目前儀表板統計門檻：漲幅 ≥ {rise_threshold}%")
    html_parts = []
    html_parts.append('<div class="dashboard-scroll"><div class="dashboard-grid">')

    for item in group_up_summary:
        group_name = escape(str(item["分類"]))
        anchor_id = make_anchor_id(group_name)
        hit_count = item["達標數"]
        total_count = item["總數"]
        up_count = item["上漲數"]
        down_count = item["下跌數"]
        hit_names_text = escape(str(item["達標股票名稱"]))
        top3_html = item["前三名HTML"]

        hit_ratio = (hit_count / total_count * 100) if total_count > 0 else 0
        if hit_ratio >= 60: bg_color = "#fff1f0"; border_color = "#ff7875"; accent_color = "#cf1322"
        elif hit_ratio > 0: bg_color = "#fff7e6"; border_color = "#ffa940"; accent_color = "#d46b08"
        else: bg_color = "#f6ffed"; border_color = "#95de64"; accent_color = "#389e0d"

        card_html = (
            f'<a href="#{anchor_id}" class="dashboard-link">'
            f'<div class="dashboard-card" style="background-color:{bg_color}; border:1px solid {border_color}; cursor:pointer;">'
            f'<div class="dashboard-title">{group_name}</div>'
            f'<div class="dashboard-main" style="color:{accent_color};">{hit_count} / {total_count}</div>'
            f'<div class="dashboard-sub">漲幅達標比例（≥{rise_threshold}%）：{hit_ratio:.0f}%</div>'
            f'<div class="dashboard-detail">'
            f'🎯 達標：<b>{hit_count}</b> 檔（{hit_names_text}）<br>'
            f'🔴 一般上漲：<b>{up_count}</b><br>'
            f'🟢 下跌：<b>{down_count}</b>'
            f'</div>'
            f'<div class="dashboard-extra">▶ {top3_html}</div>'
            f'</div></a>'
        )
        html_parts.append(card_html)
    html_parts.append("</div></div>")
    st.markdown("".join(html_parts), unsafe_allow_html=True)

# ==================== 主畫面開始 ====================
st.markdown('<div id="dashboard-top" style="scroll-margin-top: 90px;"></div>', unsafe_allow_html=True)

title_icon_col, title_text_col, scan_progress_col = st.columns([0.45, 7.55, 1])

with title_icon_col:
    if os.path.exists(APP_LOGO):
        st.image(APP_LOGO, width=58)
    else:
        st.markdown('<div style="font-size:42px; line-height:1.2;">📊</div>', unsafe_allow_html=True)

with title_text_col:
    st.markdown(
        """
        <h1 style="margin:0; padding-top:4px; font-size:42px; font-weight:800; line-height:1.2;">
            台股掃描器 - 告訴我你會買日月光
        </h1>
        """,
        unsafe_allow_html=True,
    )

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
    all_symbols_count = len(st.session_state.stock_groups.get(ALL_STOCK_GROUP_NAME, []))
    st.sidebar.success(f"✅ 全市場掃描模式：已從 {STOCK_SCAN_FILE} 載入 {all_symbols_count} 檔股票")
    st.sidebar.caption("此模式會忽略 stock_groups.json 與手動分組，直接掃描 txt 內全部股票。")
else:
    render_group_editor_lock()
    if st.session_state.group_editor_unlocked:
        render_stock_group_editor()
    else:
        st.sidebar.info("目前為唯讀模式：輸入 PIN 後才能修改股票分組")

st.caption(f"更新時間：{tw_now.strftime('%Y-%m-%d %H:%M:%S')}｜價格來源：{active_price_source}")


rise_threshold = st.number_input(
    "儀表板漲幅達標門檻 (%)",
    min_value=0.0,
    max_value=10.0,
    value=7.0,
    step=0.5,
    format="%.2f"
)


st.markdown("### 🎯 掃描條件")
scan_btn_col1, scan_btn_col2, scan_setting_col, scan_status_col = st.columns([0.9, 0.9, 1.25, 5.95])
with scan_btn_col1:
    if st.button("▶️ 開始掃描", use_container_width=True, disabled=st.session_state.scan_enabled):
        st.session_state.scan_enabled = True
        st.session_state.scan_requested = True
        st.cache_data.clear()
        st.rerun()
with scan_btn_col2:
    if st.button("⏹️ 停止掃描", use_container_width=True, disabled=not st.session_state.scan_enabled):
        st.session_state.scan_enabled = False
        st.session_state.scan_requested = False
        st.rerun()
with scan_setting_col:
    with open_dropdown("⚙️ Setting"):
        st.caption("掃描條件設定")
        show_only_signal_rows = st.toggle("只顯示訊號股票", value=True, key="setting_show_only_signal_rows")
        include_gain_threshold_filter = st.checkbox(
            "漲幅達標",
            value=True,
            key="setting_include_gain_threshold_filter",
            help="選出漲幅 >= 上方『儀表板漲幅達標門檻』的股票，並新增到漲幅達標分頁。",
        )
        include_gap_signal_filter = st.checkbox("跳空", value=True, key="setting_include_gap_signal_filter")
        include_kd_signal_filter = st.checkbox("黃金交叉 / 即將黃金交叉", value=True, key="setting_include_kd_signal_filter")
        include_week_kd_signal_filter = st.checkbox(
            "週KD 黃金交叉 / 即將黃金交叉",
            value=True,
            key="setting_include_week_kd_signal_filter",
            help="以同一批已下載的日線資料重採樣成週線後計算KD，資料抓取區間與日KD相同，不會多打API。",
        )
        include_macd_signal_filter = st.checkbox("MACD翻正", value=True, key="setting_include_macd_signal_filter")
        include_trend_signal_filter = st.checkbox(
            "趨勢突破",
            value=True,
            key="setting_include_trend_signal_filter",
            help="40日動態雙高點下降趨勢 + 8%坡度 + 60MA上揚",
        )
        min_volume_lots = st.number_input(
            "成交量(張)下限",
            min_value=0,
            value=1000,
            step=100,
            key="setting_min_volume_lots",
        )
with scan_status_col:
    scan_action_placeholder = st.empty()

if st.session_state.scan_enabled:
    st.caption("🟢 掃描狀態：執行中")
elif "last_scan_result" in st.session_state:
    st.caption(
        f"✅ 掃描狀態：已完成，上次完成時間：{st.session_state.last_scan_result.get('scan_completed_at', '-')}｜成交量下限：{st.session_state.last_scan_result.get('min_volume_lots', 1000)} 張"
    )
else:
    st.caption("⚪ 掃描狀態：已停止，按「開始掃描」才會抓取資料。")

selected_signal_names = []
if include_gain_threshold_filter:
    selected_signal_names.append("漲幅達標")
if include_gap_signal_filter:
    selected_signal_names.append("跳空")
if include_kd_signal_filter:
    selected_signal_names.extend(["黃金交叉", "即將黃金交叉"])
if include_week_kd_signal_filter:
    selected_signal_names.extend(["週黃金交叉", "週即將黃金交叉"])
if include_macd_signal_filter:
    selected_signal_names.append("MACD翻正")
if include_trend_signal_filter:
    selected_signal_names.append("趨勢突破")
    
if not selected_signal_names:
    st.warning("請至少勾選一種掃描訊號，否則不會列出訊號股票。")

if active_price_source == "WebSocket" and not st.session_state.fubon_logged_in:
    st.warning("⚠️ 目前價格來源為 WebSocket，請先至左側面板連線「富邦 API」，才能開始抓取行情資料。")
    st.stop()
if active_price_source == "Yfinance" and yf is None:
    st.warning("⚠️ 目前價格來源為 Yfinance，請先安裝套件：pip install yfinance")
    st.stop()

should_run_scan = bool(st.session_state.pop("scan_requested", False))
has_last_scan_result = "last_scan_result" in st.session_state

if not should_run_scan and not has_last_scan_result:
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
            can_push_now = True
            st.session_state.notified_stocks = set() 
            st.toast("🚀 收到 'push' 指令，強制觸發推播！")
            send_telegram_message("🤖 <b>收到指令，開始為您掃描並強制推播強勢股...</b>")
        elif st.session_state.scheduled_push_enabled:
            TARGET_TIMES = [
                tw_now.replace(hour=9, minute=40, second=0, microsecond=0),
                tw_now.replace(hour=10, minute=0, second=0, microsecond=0),
                tw_now.replace(hour=11, minute=0, second=0, microsecond=0),
                tw_now.replace(hour=12, minute=0, second=0, microsecond=0),
                tw_now.replace(hour=13, minute=0, second=0, microsecond=0)
            ]
            for target_dt in TARGET_TIMES:
                diff_seconds = (tw_now - target_dt).total_seconds()
                if abs(diff_seconds) <= 45:
                    time_str = target_dt.strftime("%H%M")
                    today_str = tw_now.strftime("%Y%m%d")
                    current_schedule_key = f"slot_{today_str}_{time_str}"
                    if current_schedule_key not in st.session_state.processed_time_slots:
                        can_push_now = True
                        break
        else:
            can_push_now = False

    group_tables = {}
    group_up_summary = []
    all_signal_rows = []
    signal_buckets = {"優先追蹤": [], "漲幅達標": [], "跳空": [], "黃金交叉": [], "即將黃金交叉": [], "週黃金交叉": [], "週即將黃金交叉": [], "MACD翻正": [], "趨勢突破": []}
    trend_chart_store = {}  # symbol -> {df, p1_pos, p2_pos, p1_val, slope, name} 供「趨勢突破」分頁畫K線+趨勢線圖
    scan_total_count = sum(len(stocks) for stocks in st.session_state.stock_groups.values())

    # 🚀 批次預先抓取：把整批股票的Yfinance歷史資料一次抓回來，取代掃描迴圈中逐檔各打一次API。
    # 這是加速全市場掃描最主要的一步，尤其股票數量多時效果最明顯。
    scan_today_str = tw_now.strftime("%Y-%m-%d")
    all_unique_symbols = tuple(sorted({s for stocks in st.session_state.stock_groups.values() for s in stocks}))
    yf_history_map = bulk_download_yfinance_history(all_unique_symbols, scan_today_str) if yf is not None else {}
    yf_today_map = (
        bulk_download_yfinance_today(all_unique_symbols, scan_today_str)
        if (yf is not None and active_price_source == "Yfinance") else {}
    )

    render_scan_progress_card(scan_progress_card_placeholder, 0, "掃描進度")
    progress_bar = st.progress(0, text=f"掃描進度：0.0%（準備掃描 {scan_total_count} 檔股票）")
    processed_count = 0

    for group_name, stocks in st.session_state.stock_groups.items():
        rows = []
        hit_count = up_count = down_count = flat_count = error_count = 0
        valid_stock_stats = []
        hit_names = []

        for symbol in stocks:
            if not st.session_state.scan_enabled:
                progress_bar.empty()
                st.warning("掃描已停止。")
                st.stop()
            processed_count += 1
            if scan_total_count > 0:
                progress_value = min(processed_count / scan_total_count, 1.0)
                progress_pct = progress_value * 100
                render_scan_progress_card(scan_progress_card_placeholder, progress_pct, "掃描進度")
                progress_bar.progress(progress_value, text=f"掃描進度：{progress_pct:.1f}%（{processed_count}/{scan_total_count}：{symbol}）")
            try:
                df = download_stock_data_by_source(
                    symbol, st.session_state.fubon_sdk, active_price_source, scan_today_str,
                    history_map=yf_history_map, yf_today_map=yf_today_map,
                )
                df = normalize_ohlc(df)
                if df.empty: raise ValueError("無效的 K 線資料")

                price = get_last_price_by_source(symbol, df, st.session_state.fubon_sdk, active_price_source)
                stock_name = get_stock_name(symbol, st.session_state.fubon_sdk)
                data = compute_indicators(df, price, symbol=symbol, rise_threshold=rise_threshold)

                # 🌟 modular：直接彙整 SIGNAL_REGISTRY 每個訊號函式觸發的 labels，
                # 新增/修改掃描條件時完全不用動這裡。
                signal_types = []
                for _sig_name, _sig_result in data["signal_results"].items():
                    signal_types.extend(_sig_result.get("labels", []))

                signal_score = calc_signal_quality_score(data, signal_types)
                signal_grade = classify_signal_grade(signal_score)

                passes_volume_filter = float(data.get("volume_lots", 0)) >= float(min_volume_lots)
                is_selected_signal = (
                    any(sig in selected_signal_names for sig in signal_types)
                    and passes_volume_filter
                    and signal_score >= SIGNAL_SCORE_MIN
                )

                # ===== 執行推播檢查 =====
                is_high_gain = data["pct"] >= 5
                if (is_high_gain or is_selected_signal) and passes_volume_filter:
                    base_symbol = symbol.split('.')[0]
                    yahoo_url = f"https://tw.stock.yahoo.com/quote/{base_symbol}"
                    symbol_link = f'<a href="{yahoo_url}">{symbol}</a>'
                    today_str = tw_now.strftime("%Y-%m-%d")
                    notify_key = f"{symbol}_{today_str}"
                    if can_push_now and (notify_key not in st.session_state.notified_stocks):
                        msg = (
                            f"🔔 <b>全市場掃描訊號：{stock_name} ({symbol_link})</b>\n\n"
                            f"📈 價格：{data['price']}\n"
                            f"🔥 漲幅：{data['pct']}%\n"
                            f"📦 成交量：{data['volume_lots']:,.1f} 張\n"
                            f"⭐ 訊號分數：{signal_score} / {signal_grade}\n"
                            f"🌊 波動率：{data['volatility_pct']}%\n"
                            f"📊 KD訊號：{data['kd_signal']}\n"
                            f"📊 週KD訊號：{data['week_kd_signal']}\n"
                            f"🧭 MACD訊號：{data['macd_signal']} / MACD柱：{data['macd_hist']}\n"
                            f"🚀 跳空訊號：{data['gap_signal']}\n"
                            f"🔥 趨勢突破：{data['trend_signal']}\n"
                            f"🔌 來源：{active_price_source}"
                        )
                        send_telegram_message(msg)
                        st.session_state.notified_stocks.add(notify_key)
                # =======================

                if data["pct"] >= rise_threshold:
                    hit_count += 1
                    hit_names.append(stock_name)
                if data["pct"] > 0: up_count += 1
                elif data["pct"] < 0: down_count += 1
                else: flat_count += 1

                valid_stock_stats.append({"symbol": symbol, "code": symbol_to_code(symbol), "name": stock_name, "pct": float(data["pct"])})
                
                row = {
                    "代碼": symbol, "代碼網址": yahoo_quote_url(symbol), "股票名稱": stock_name,
                    "價格": f"{data['price']:.2f}", "漲跌%": data["pct"],
                    "成交量(張)": data["volume_lots"],
                    "波動率%": data["volatility_pct"],
                    "RS加權報酬%": data["rs_raw"],
                    "訊號分數": signal_score,
                    "追蹤等級": signal_grade,
                    "P1日期": data["p1_date"], "區高P1": data["p1_val"],
                    "P2日期": data["p2_date"], "近高P2": data["p2_val"],
                    "坡度%": data["slope_pct"], "趨勢價": data["tl_val"], "趨勢突破": data["trend_signal"],
                    "貼線數": data["trend_touch_count"], "穿線數": data["trend_violations"],
                    "量能倍數": data["trend_vol_ratio"],
                    "MA位置": data["ma_range"], "MA排列": data["ma_trend"],
                    "K值": data["k"], "D值": f"{data['d']:.1f}",
                    "KD訊號": data["kd_signal"],
                    "週K值": data["week_k"],
                    "週D值": data["week_d"] if data["week_d"] == "-" else f"{data['week_d']:.1f}",
                    "週KD訊號": data["week_kd_signal"],
                    "MACD柱": data["macd_hist"],
                    "MACD訊號": data["macd_signal"], "跳空訊號": data["gap_signal"],
                    "訊號類型": "、".join(signal_types) if signal_types else "-",
                    "來源": active_price_source,
                }
                if ((not show_only_signal_rows) or is_selected_signal) and passes_volume_filter:
                    rows.append(row)
                if is_selected_signal:
                    all_signal_rows.append(row.copy())
                    if signal_score >= PRIORITY_SCORE_MIN:
                        signal_buckets["優先追蹤"].append(row.copy())
                    append_signal_tracking(row, scan_today_str)
                    for sig in signal_types:
                        if sig in signal_buckets and sig in selected_signal_names:
                            signal_buckets[sig].append(row.copy())

                    if (
                        data["trend_signal"] == "趨勢突破"
                        and data.get("trend_chart_df") is not None
                        and data.get("trend_p1_pos") is not None
                        and data.get("trend_p2_pos") is not None
                    ):
                        trend_chart_store[symbol] = {
                            "name": stock_name,
                            "df": data["trend_chart_df"],
                            "p1_pos": data["trend_p1_pos"],
                            "p2_pos": data["trend_p2_pos"],
                            "p1_val": float(data["p1_val"]) if data["p1_val"] != "-" else None,
                            "slope": data["trend_slope"],
                            "vol_ratio": data["trend_vol_ratio"],
                        }
            except Exception as e:
                error_count += 1
                if not show_only_signal_rows:
                    rows.append({
                        "代碼": symbol, "代碼網址": "", "股票名稱": get_stock_name(symbol, st.session_state.fubon_sdk),
                        "價格": "錯誤", "漲跌%": "-", "成交量(張)": "-", "波動率%": "-", "RS加權報酬%": "-",
                        "P1日期": "-", "區高P1": "-", "P2日期": "-", "近高P2": "-", "坡度%": "-", "趨勢價": "-", "趨勢突破": "-", "貼線數": "-", "穿線數": "-", "量能倍數": "-",
                        "MA位置": "-", "MA排列": "-", "K值": "-", "D值": "-",
                        "KD訊號": "-", "週K值": "-", "週D值": "-", "週KD訊號": "-",
                        "MACD柱": "-", "MACD訊號": "-",
                        "跳空訊號": str(e), "訊號類型": "錯誤", "來源": active_price_source,
                    })

        hit_names_text = compact_name_list(hit_names, max_show=4)
        top3_html = build_top3_html(valid_stock_stats)
        df_table = pd.DataFrame(rows)
        display_df = df_table.copy()
        if not display_df.empty:
            display_df["漲跌%"] = display_df["漲跌%"].apply(format_color)
            display_df["K值"] = display_df["K值"].apply(format_k)
            if "週K值" in display_df.columns:
                display_df["週K值"] = display_df["週K值"].apply(format_k)
            display_df["成交量(張)"] = display_df["成交量(張)"].apply(format_volume)
            if "波動率%" in display_df.columns:
                display_df["波動率%"] = display_df["波動率%"].apply(format_volatility)
            display_df["跳空訊號"] = display_df["跳空訊號"].apply(format_gap)
            if "趨勢突破" in display_df.columns:
                display_df["趨勢突破"] = display_df["趨勢突破"].apply(format_trend)
            if "量能倍數" in display_df.columns:
                display_df["量能倍數"] = display_df["量能倍數"].apply(format_vol_ratio)
        group_tables[group_name] = {"count": len(stocks), "table": display_df}
        group_up_summary.append({
            "分類": group_name, "達標數": hit_count, "達標股票名稱": hit_names_text,
            "前三名HTML": top3_html, "上漲數": up_count, "下跌數": down_count,
            "平盤數": flat_count, "錯誤數": error_count, "總數": len(stocks)
        })

    render_scan_progress_card(scan_progress_card_placeholder, 100, "掃描進度")
    progress_bar.empty()
    if can_push_now and st.session_state.scheduled_push_enabled and current_schedule_key and not manual_push_triggered:
        st.session_state.processed_time_slots.add(current_schedule_key)

    # 保險：掃描完成後重新產生「優先追蹤」分頁，確保排序與去重後結果一致。
    signal_buckets["優先追蹤"] = build_priority_rows(all_signal_rows, PRIORITY_SCORE_MIN)

    st.session_state.last_scan_result = {
        "group_tables": group_tables,
        "group_up_summary": group_up_summary,
        "all_signal_rows": all_signal_rows,
        "signal_buckets": signal_buckets,
        "trend_chart_store": trend_chart_store,
        "excel_filename": f"TWstock_signal_scan_{tw_now.strftime('%Y%m%d_%H%M%S')}.xlsx",
        "scan_completed_at": tw_now.strftime('%Y-%m-%d %H:%M:%S'),
        "progress_pct": 100,
        "min_volume_lots": min_volume_lots,
    }
    if AUTO_UPLOAD_GITHUB:
        auto_excel_bytes = build_signal_excel_bytes(signal_buckets)
        auto_excel_filename = st.session_state.last_scan_result["excel_filename"]
        upload_file_to_github(
            auto_excel_bytes,
            f"{GITHUB_DATABASE_DIR}/{auto_excel_filename}",
            f"Auto upload TW stock scan result {tw_now.strftime('%Y-%m-%d %H:%M:%S')}",
        )
        if os.path.exists(TRACKING_FILE):
            upload_tracking_file_to_github(tw_now.strftime('%Y-%m-%d %H:%M:%S'))

    st.session_state.scan_enabled = False
else:
    last_scan_result = st.session_state.last_scan_result
    group_tables = last_scan_result.get("group_tables", {})
    group_up_summary = last_scan_result.get("group_up_summary", [])
    all_signal_rows = last_scan_result.get("all_signal_rows", [])
    signal_buckets = last_scan_result.get("signal_buckets", {"漲幅達標": [], "跳空": [], "黃金交叉": [], "即將黃金交叉": [], "週黃金交叉": [], "週即將黃金交叉": [], "MACD翻正": [], "趨勢突破": []})
    trend_chart_store = last_scan_result.get("trend_chart_store", {})
    render_scan_progress_card(scan_progress_card_placeholder, last_scan_result.get("progress_pct", 100), "掃描進度")

excel_bytes = build_signal_excel_bytes(signal_buckets)
excel_filename = st.session_state.get("last_scan_result", {}).get(
    "excel_filename",
    f"TWstock_signal_scan_{tw_now.strftime('%Y%m%d_%H%M%S')}.xlsx"
)

with scan_action_placeholder.container():
    download_col, info_col = st.columns([1.15, 6.85])
    with download_col:
        with open_dropdown("📁 Download"):
            st.caption("下載 / 推播 / GitHub 上傳")
            st.download_button(
                "下載 Excel",
                data=excel_bytes,
                file_name=excel_filename,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
                key="download_signal_excel_btn",
            )
            if st.button("推送到 Telegram", use_container_width=True, key="push_signal_excel_to_tg_btn"):
                ok = send_telegram_document(
                    excel_bytes,
                    excel_filename,
                    caption=f"TWstock 訊號掃描結果｜成交量下限 {st.session_state.get('last_scan_result', {}).get('min_volume_lots', min_volume_lots)} 張｜{tw_now.strftime('%Y-%m-%d %H:%M:%S')}",
                )
                if ok:
                    st.success("已將 Excel 推送到 Telegram。")
            if st.button("上傳 Excel 到 GitHub", use_container_width=True, key="push_signal_excel_to_github_btn"):
                upload_file_to_github(
                    excel_bytes,
                    f"{GITHUB_DATABASE_DIR}/{excel_filename}",
                    f"Upload TW stock scan result {tw_now.strftime('%Y-%m-%d %H:%M:%S')}",
                )
            if st.button("上傳追蹤 CSV", use_container_width=True, key="push_tracking_csv_to_github_btn"):
                upload_tracking_file_to_github(tw_now.strftime('%Y-%m-%d %H:%M:%S'))
            st.caption(f"今日追蹤CSV檔名：{tracking_github_filename(tw_now)}")
    with info_col:
        st.caption(f"Excel：{excel_filename} ｜ 追蹤CSV GitHub 目標：{tracking_github_path(tw_now)}")

st.markdown("### 🔎 訊號掃描結果")
# ========== 新增：掃描資料總計 ==========
total_scanned = sum(item.get("總數", 0) for item in group_up_summary)
total_errors = sum(item.get("錯誤數", 0) for item in group_up_summary)
total_success = total_scanned - total_errors

stat_col1, stat_col2, stat_col3, stat_col4 = st.columns(4)
with stat_col1:
    st.metric("掃描資料總數", total_scanned)
with stat_col2:
    st.metric("得到資料數", total_success)
with stat_col3:
    st.metric("缺少資料數", total_errors)
with stat_col4:
    unique_signal_count = len(pd.DataFrame(all_signal_rows).drop_duplicates(subset=["代碼"])) if all_signal_rows else 0
    st.metric("符合勾選條件數", unique_signal_count)

# ========== 2. 新增：找出缺少資料的股票並顯示浮動視窗 ==========
missing_stocks = []
# 走訪每個群組的資料表，把「訊號類型」為「錯誤」的股票挑出來
for group_name, info in group_tables.items():
    table_df = info.get("table")
    if table_df is not None and not table_df.empty and "訊號類型" in table_df.columns:
        error_rows = table_df[table_df["訊號類型"] == "錯誤"]
        for _, row in error_rows.iterrows():
            # 取得代碼並去除可能的連結格式，保留乾淨的代碼與名稱
            code = str(row.get("代碼", "")).split(">")[-1].replace("</a", "") if "<a" in str(row.get("代碼", "")) else str(row.get("代碼", ""))
            name = str(row.get("股票名稱", ""))
            missing_stocks.append(f"{code} {name}")


unique_signal_count = len(pd.DataFrame(all_signal_rows).drop_duplicates(subset=["代碼"])) if all_signal_rows else 0
st.metric("符合勾選掃描條件股票數", unique_signal_count)
if os.path.exists(TRACKING_FILE):
    st.caption(f"追蹤檔：{TRACKING_FILE} ｜ GitHub 目標：{tracking_github_path(tw_now)}")
else:
    st.caption(f"追蹤檔尚未建立；第一次掃描到符合二階段過濾的股票後會建立：{TRACKING_FILE}")

# 全域定義顯示的欄位，確保資料表一定找得到
display_columns = ["代碼", "股票名稱", "價格", "漲跌%", "成交量(張)", "波動率%", "RS加權報酬%", "訊號分數", "追蹤等級", "P1日期", "區高P1", "P2日期", "近高P2", "坡度%", "趨勢價", "趨勢突破", "貼線數", "穿線數", "量能倍數", "MA位置", "MA排列", "K值", "D值", "KD訊號", "週K值", "週D值", "週KD訊號", "MACD柱", "MACD訊號", "跳空訊號", "訊號類型", "來源"]

if all_signal_rows:
    signal_df = pd.DataFrame(all_signal_rows).drop_duplicates(subset=["代碼"])
    signal_display_df = signal_df.copy()
    signal_display_df["漲跌%"] = signal_display_df["漲跌%"].apply(format_color)
    signal_display_df["K值"] = signal_display_df["K值"].apply(format_k)
    if "週K值" in signal_display_df.columns:
        signal_display_df["週K值"] = signal_display_df["週K值"].apply(format_k)
    signal_display_df["成交量(張)"] = signal_display_df["成交量(張)"].apply(format_volume)
    if "波動率%" in signal_display_df.columns:
        signal_display_df["波動率%"] = signal_display_df["波動率%"].apply(format_volatility)
    signal_display_df["跳空訊號"] = signal_display_df["跳空訊號"].apply(format_gap)
    
    # 🌟 防呆：確認有該欄位才套用格式，沒有則補上 "-"
    if "趨勢突破" in signal_display_df.columns:
        signal_display_df["趨勢突破"] = signal_display_df["趨勢突破"].apply(format_trend)
    else:
        signal_display_df["趨勢突破"] = "-"

    if "量能倍數" in signal_display_df.columns:
        signal_display_df["量能倍數"] = signal_display_df["量能倍數"].apply(format_vol_ratio)
    else:
        signal_display_df["量能倍數"] = "-"
        
    signal_display_df["代碼"] = signal_display_df["代碼網址"]
    
    # 🌟 防呆：補齊遺失的顯示欄位
    for col in display_columns:
        if col not in signal_display_df.columns:
            signal_display_df[col] = "-"
            
    st.dataframe(signal_display_df[display_columns], use_container_width=True, column_config={
        "代碼": st.column_config.LinkColumn("代碼", help="點擊前往台股 Yahoo", display_text=r"https://tw.stock.yahoo.com/quote/(.*)"),
        "股票名稱": st.column_config.TextColumn("股票名稱"),
        "訊號分數": st.column_config.NumberColumn("訊號分數", format="%.1f"),
        "追蹤等級": st.column_config.TextColumn("追蹤等級"),
    })
    
    st.markdown("### 📑 依訊號分頁查看")

    signal_tab_specs = [
        ("優先追蹤", "優先追蹤"),
        ("漲幅達標", "漲幅達標"),
        ("跳空", "跳空"),
        ("黃金交叉", "黃金交叉"),
        ("即將黃金交叉", "即將黃金交叉"),
        ("週黃金交叉", "週黃金交叉"),
        ("週即將黃金交叉", "週即將黃金交叉"),
        ("MACD 訊號", "MACD翻正"),
        ("趨勢突破", "趨勢突破"), 
    ]

    tab_labels = []
    for display_name, bucket_key in signal_tab_specs:
        bucket_rows = signal_buckets.get(bucket_key, [])
        unique_count = len(pd.DataFrame(bucket_rows).drop_duplicates(subset=["代碼"])) if bucket_rows else 0
        tab_labels.append(f"{display_name}（{unique_count}）")

    signal_tabs = st.tabs(tab_labels)
    for tab, (display_name, bucket_key) in zip(signal_tabs, signal_tab_specs):
        with tab:
            bucket_rows = signal_buckets.get(bucket_key, [])
            unique_count = len(pd.DataFrame(bucket_rows).drop_duplicates(subset=["代碼"])) if bucket_rows else 0
            st.markdown(f"#### {display_name}（{unique_count} 檔）")

            if bucket_rows:
                bucket_df = pd.DataFrame(bucket_rows).drop_duplicates(subset=["代碼"])
                bucket_display_df = bucket_df.copy()
                bucket_display_df["漲跌%"] = bucket_display_df["漲跌%"].apply(format_color)
                bucket_display_df["K值"] = bucket_display_df["K值"].apply(format_k)
                if "週K值" in bucket_display_df.columns:
                    bucket_display_df["週K值"] = bucket_display_df["週K值"].apply(format_k)
                bucket_display_df["成交量(張)"] = bucket_display_df["成交量(張)"].apply(format_volume)
                if "波動率%" in bucket_display_df.columns:
                    bucket_display_df["波動率%"] = bucket_display_df["波動率%"].apply(format_volatility)
                bucket_display_df["跳空訊號"] = bucket_display_df["跳空訊號"].apply(format_gap)
                
                # 🌟 防呆：確認有該欄位才套用格式，沒有則補上 "-"
                if "趨勢突破" in bucket_display_df.columns:
                    bucket_display_df["趨勢突破"] = bucket_display_df["趨勢突破"].apply(format_trend)
                else:
                    bucket_display_df["趨勢突破"] = "-"

                if "量能倍數" in bucket_display_df.columns:
                    bucket_display_df["量能倍數"] = bucket_display_df["量能倍數"].apply(format_vol_ratio)
                else:
                    bucket_display_df["量能倍數"] = "-"
                    
                bucket_display_df["代碼"] = bucket_display_df["代碼網址"]
                
                # 🌟 防呆：補齊遺失的顯示欄位
                for col in display_columns:
                    if col not in bucket_display_df.columns:
                        bucket_display_df[col] = "-"
                        
                st.dataframe(bucket_display_df[display_columns], use_container_width=True, column_config={
                    "代碼": st.column_config.LinkColumn("代碼", help="點擊前往台股 Yahoo", display_text=r"https://tw.stock.yahoo.com/quote/(.*)"),
                    "股票名稱": st.column_config.TextColumn("股票名稱"),
                    "訊號分數": st.column_config.NumberColumn("訊號分數", format="%.1f"),
                    "追蹤等級": st.column_config.TextColumn("追蹤等級"),
                })

                # 🌟 趨勢突破分頁：額外提供 K線 + 下降趨勢線圖表，方便肉眼確認訊號品質
                if bucket_key == "趨勢突破" and trend_chart_store:
                    chart_title_col, chart_download_col, chart_collapse_col = st.columns([3.8, 0.95, 0.95])
                    with chart_title_col:
                        st.markdown("##### 📈 個股圖表（K線 + 下降趨勢線）")
                    with chart_download_col:
                        trend_chart_zip_bytes = build_trend_chart_zip_bytes(trend_chart_store)
                        st.download_button(
                            "📥 下載全部圖",
                            data=trend_chart_zip_bytes,
                            file_name=f"trend_breakout_charts_{tw_now.strftime('%Y%m%d_%H%M%S')}.zip",
                            mime="application/zip",
                            use_container_width=True,
                            key="download_all_trend_charts_btn",
                            disabled=(len(trend_chart_zip_bytes) == 0),
                            help="ZIP 內含每檔個別 HTML 圖表，以及 00_趨勢突破_全部圖統整.html。",
                        )
                    with chart_collapse_col:
                        if st.button("📁 收折全部圖", use_container_width=True, key="collapse_all_trend_charts_btn"):
                            st.session_state.trend_charts_collapsed = True
                            # st.expander 會記住前端展開狀態；改變不可見 label suffix 可強制重建 expander。
                            st.session_state.trend_charts_collapse_version = st.session_state.get("trend_charts_collapse_version", 0) + 1
                            st.rerun()

                    invisible_version_suffix = "\u200b" * int(st.session_state.get("trend_charts_collapse_version", 0))
                    for _, r in bucket_df.iterrows():
                        sym = r["代碼"]
                        chart_info = trend_chart_store.get(sym)
                        if not chart_info:
                            continue
                        expander_label = f"{sym}　{r.get('股票名稱', '')}　量能倍數 {chart_info.get('vol_ratio', '-')}{invisible_version_suffix}"
                        with st.expander(
                            expander_label,
                            expanded=not st.session_state.get("trend_charts_collapsed", True),
                        ):
                            plot_trend_breakout_chart(sym, r.get("股票名稱", ""), chart_info)
            else:
                st.caption(f"目前沒有符合「{display_name}」的股票。")
else:
    st.info("目前沒有掃描到符合勾選條件的股票。")

st.divider()
render_summary_dashboard(group_up_summary, rise_threshold)
st.divider()

for group_name, info in group_tables.items():
    anchor_id = make_anchor_id(group_name)
    st.markdown(f'<div id="{anchor_id}" style="scroll-margin-top: 80px;"></div>', unsafe_allow_html=True)
    header_col1, header_col2 = st.columns([8, 2])
    with header_col1: st.subheader(f"【{group_name}】({info['count']}檔)")
    with header_col2: st.markdown("""<div style="text-align:right; padding-top:0.4rem;"><a href="#dashboard-top" class="back-to-dashboard-btn">⬆ 回到儀表板</a></div>""", unsafe_allow_html=True)
    
    table_df = info["table"].copy()
    if not table_df.empty and "代碼網址" in table_df.columns: 
        table_df["代碼"] = table_df["代碼網址"]
        
    for col in display_columns:
        if col not in table_df.columns:
            table_df[col] = "-"
            
    st.dataframe(table_df[display_columns], use_container_width=True, column_config={
        "代碼": st.column_config.LinkColumn("代碼", help="點擊前往台股 Yahoo", display_text=r"https://tw.stock.yahoo.com/quote/(.*)"),
        "股票名稱": st.column_config.TextColumn("股票名稱"),
        "訊號分數": st.column_config.NumberColumn("訊號分數", format="%.1f"),
        "追蹤等級": st.column_config.TextColumn("追蹤等級"),
    })
    st.markdown('<div style="margin-bottom: 10px;"></div>', unsafe_allow_html=True)

if (st.session_state.auto_refresh_enabled and not st.session_state.group_editor_unlocked and not st.session_state.editing_mode):
    refresh_sec = max(1, int(st.session_state.get("refresh_sec", REFRESH_SEC)))
    time.sleep(refresh_sec)
    st.rerun()
