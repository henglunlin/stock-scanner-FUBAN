"""
fetch_etf_holdings.py
======================
主動式ETF持股每日抓取腳本（取代原本 Fund_change_common_summary.py 的檔案輸出方式）。

跟原始腳本(使用者提供的 Fund_change_common_summary.py)的差異：
  1. 瀏覽器從「嘗試啟動本機 Microsoft Edge」改成「headless Chromium」——
     因為這支腳本要跑在 GitHub Actions (ubuntu-latest)，機器上沒有 Edge，
     且原本 launch_edge_browser() 設計就是給「使用者自己的電腦(Windows)」用的。
  2. 追蹤的ETF清單從程式碼裡寫死的 ETFS dict，改成動態讀取
     ETF_data/active_etf_list.csv（欄位：股票代號, ETF名稱），
     並用固定URL規則 https://www.etfinfo.tw/etf/{代號}/holdings 組出網址。
     這樣之後 TWSE 有新的主動式ETF上市，只要更新這份CSV，不用改程式碼。
  3. 所有輸出從「逐日 xlsx 檔案」改成寫入 ETF_data/etf_holdings.db（見 etf_db.py），
     方便之後查歷史、跟K線整合查詢。
     ⚠️ 2026-08-24：ETF相關資料檔案(active_etf_list.csv / etf_holdings.db /
     etf_watchlist_config.json)這次統一集中放到獨立的 ETF_data/ 資料夾，
     不再借用scanner既有的 Database/ 資料夾——避免跟掃描器本來就在用的
     Database/ 內容混在一起，這個功能自己的資料要新增/搬移/清空都不會影響到
     掃描器其他部分。
  4. 「找前一天持股檔案」從掃資料夾檔名，改成查資料庫裡「該ETF在今天之前
     最近一次的快照日期」(etf_db.get_previous_snapshot_date)。
  5. 「共同每日異動」的門檻(min_etf_count)不在這裡寫死判斷輸出檔——
     完整異動明細一律存進 etf_holding_changes，門�One檻改成由網頁那端
     (pages/7_📊_主動式ETF分析.py) 讀取時即時套用可調整的滑桿，
     這裡只在 Telegram 通知裡用預設門檻(2)做一個「本次重點摘要」。
  6. 抓取解析核心邏輯(欄位判斷、翻頁、清理)幾乎完全保留原腳本寫法不變，
     這部分已由使用者在自己電腦上驗證過可以正常運作。

⚠️ 重要限制：這支腳本在 sandbox 環境裡因為網路白名單限制，
   無法實際連線到 etfinfo.tw 做端對端測試(见交付說明)，
   请在 GitHub Actions 或本機環境實際跑一次驗證。
"""
import os
import re
import sys
import asyncio
import logging
import traceback
from io import StringIO
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from playwright.async_api import async_playwright

import etf_db

# =========================
# 基本設定
# =========================

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
# ⚠️ 2026-08-24：改用獨立的 ETF_data/ 資料夾，不再放在scanner共用的 Database/ 資料夾
# 或repo根目錄，讓這個功能的資料檔案(清單CSV/db/追蹤清單設定)集中在同一個地方。
ETF_DATA_DIR = os.path.join(REPO_ROOT, "ETF_data")
os.makedirs(ETF_DATA_DIR, exist_ok=True)
ACTIVE_ETF_LIST_CSV = os.path.join(ETF_DATA_DIR, "active_etf_list.csv")
DB_PATH = os.path.join(ETF_DATA_DIR, "etf_holdings.db")

OUTPUT_LOG_DIR = "etf_holdings_log"
TIMEZONE = "Asia/Taipei"

MAX_PAGES_PER_ETF = 10
PAGE_WAIT_MS = 1200
MIN_EXPECTED_ROWS_WARN = 10  # 抓到的持股筆數低於此值只警告、不視為失敗(不同ETF規模差異很大)

# Telegram通知裡「共同異動摘要」預設門檻(僅供通知文字使用，網頁上可自行調整)
NOTIFY_MIN_COMMON_CHANGE_ETF_COUNT = 2


# =========================
# Log 設定
# =========================
def setup_logging():
    os.makedirs(OUTPUT_LOG_DIR, exist_ok=True)
    log_path = os.path.join(OUTPUT_LOG_DIR, "fetch_etf_holdings.log")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(),
        ],
        force=True,
    )


# =========================
# 時間工具
# =========================
def now_taipei() -> datetime:
    return datetime.now(ZoneInfo(TIMEZONE))


def today_string() -> str:
    return now_taipei().strftime("%Y-%m-%d")


# =========================
# Telegram 通知 (沿用 update_db.py 既有做法)
# =========================
def send_telegram_message(text: str):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("未設定 Telegram 變數，略過推播。")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Telegram 傳送失敗: {e}")


# =========================
# ETF 清單載入 (動態，取代寫死的 ETFS dict)
# =========================
def load_active_etf_list(csv_path: str = ACTIVE_ETF_LIST_CSV) -> dict:
    """
    讀取 ETF_data/active_etf_list.csv，回傳 {代號: {"name": 名稱, "url": 持股頁面網址}}。
    CSV 欄位需含「股票代號」「ETF名稱」(UTF-8 編碼)。

    ⚠️ 2026-08-24新增「啟用」欄位：這次討論後決定先只穩定抓6檔已驗證過的純台股
    主動式ETF，其餘26檔(含美股/混合持股的)暫時保留在清單裡當「目錄」，但「啟用」
    設為0，不會被實際抓取。之後要擴充只要把對應那一列的「啟用」改成1即可，
    不用重新輸入ETF代號/名稱。CSV裡沒有「啟用」欄位時(舊格式)視為全部啟用，
    保持向下相容。
    """
    if not os.path.exists(csv_path):
        raise FileNotFoundError(
            f"找不到主動式ETF清單檔案: {csv_path}\n"
            "請確認 ETF_data/active_etf_list.csv 已存在於 repo 內。"
        )
    df = pd.read_csv(csv_path, encoding="utf-8-sig", dtype=str)
    df.columns = [c.strip() for c in df.columns]
    has_enabled_col = "啟用" in df.columns

    result = {}
    for _, row in df.iterrows():
        code = str(row.get("股票代號", "")).strip()
        name = str(row.get("ETF名稱", "")).strip()
        if not code:
            continue
        if has_enabled_col:
            enabled_raw = str(row.get("啟用", "")).strip()
            if enabled_raw not in ("1", "1.0", "True", "true", "是"):
                continue
        result[code] = {
            "name": name,
            "url": f"https://www.etfinfo.tw/etf/{code}/holdings",
        }
    return result


# =========================
# 瀏覽器啟動工具 (雙瀏覽器支援：Edge優先、Chromium備援)
# =========================
# ⚠️ 2026-08-24調整：使用者確認「保留雙瀏覽器支援」——這支腳本要能在兩種環境下跑：
#   1. 使用者自己的Windows電腦(本機手動執行) → 用真正的 Microsoft Edge，
#      這是使用者原本 Fund_change_common_summary.py / etf_utils.py 就驗證過穩定可靠的方式。
#   2. GitHub Actions (ubuntu-latest，最終正式排程/Streamlit按鈕觸發的執行環境)
#      → 沒有Edge可用，改用 headless Chromium(這個repo在v3就驗證過能穩定連到etfinfo.tw)。
# launch_browser() 會先嘗試Edge，抓不到才自動退回Chromium，同一份程式碼兩種環境都能跑，
# 不用另外維護兩份腳本。

async def launch_edge_browser(playwright):
    """
    嘗試啟動本機Microsoft Edge(取自使用者確認可靠的 etf_utils.py 原始寫法)。
    只有在真的有安裝Edge的環境(通常是使用者自己的Windows電腦)才會成功；
    GitHub Actions ubuntu-latest 上一定會失敗，由呼叫端 launch_browser() 接住並退回Chromium。
    """
    launch_options = {
        "headless": True,
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
        ],
    }
    try:
        logging.info("嘗試使用 channel='msedge' 啟動 Microsoft Edge")
        return await playwright.chromium.launch(channel="msedge", **launch_options)
    except Exception as e:
        logging.warning(f"使用 channel='msedge' 啟動失敗: {e}")

    edge_paths = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
    ]
    for edge_path in edge_paths:
        if os.path.exists(edge_path):
            logging.info(f"改用 Edge 路徑啟動: {edge_path}")
            return await playwright.chromium.launch(executable_path=edge_path, **launch_options)

    raise RuntimeError("找不到 Microsoft Edge 執行檔，這台機器上沒有安裝Edge或路徑不在預期位置。")


async def launch_chromium_browser(playwright):
    """
    在 GitHub Actions (ubuntu-latest) / 一般 Linux 環境啟動 headless Chromium。
    比照原腳本 launch_edge_browser() 的防偵測參數設定。
    """
    return await playwright.chromium.launch(
        headless=True,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
        ],
    )


# =========================
# 欄位與資料清理工具 (沿用原腳本邏輯，未修改)
# =========================
def make_unique_columns(columns) -> list:
    seen = {}
    new_columns = []
    for col in columns:
        base = str(col).strip().replace("\n", "").replace("\r", "")
        if base not in seen:
            seen[base] = 1
            new_columns.append(base)
        else:
            seen[base] += 1
            new_columns.append(f"{base}_{seen[base]}")
    return new_columns


def flatten_multiindex_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [
            "_".join([str(x) for x in col if str(x) != "nan"]).strip()
            for col in df.columns
        ]
    return df


def clean_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = make_unique_columns(df.columns)
    return df


def normalize_stock_code(value) -> str:
    """
    ⚠️ 2026-08-24調整：改回使用者「確定可以抓值」的 etf_utils.py 版本(只認台股4位
    數字代碼)，取代v4/v7為了處理美股/混合持股ETF加上的英文代碼fallback邏輯。

    原因：這次討論後範圍先縮小到6檔已驗證過的純台股主動式ETF(00981A/00991A/
    00980A/00982A/00403A/00985A)，這6檔完全沒有美股持股問題，不需要那段
    fallback邏輯；而且那段邏輯是根據log現象推理寫的、沒有實際連到etfinfo.tw驗證過，
    相對之下這個簡化版本已經用使用者自己本機真實抓到的資料(2026-08-20/21)驗證過
    正確無誤。之後如果要擴充到含美股/混合持股的ETF，再另外討論怎麼處理。
    """
    if pd.isna(value):
        return ""
    text = str(value).strip()
    if text == "" or text.lower() == "nan":
        return ""
    if text.endswith(".0"):
        text = text[:-2]
    match = re.match(r"^(\d{4})", text)
    if match:
        return match.group(1)
    return ""


def extract_stock_name(value) -> str:
    if pd.isna(value):
        return ""
    text = str(value).strip()
    if text == "" or text.lower() == "nan":
        return ""
    text = re.sub(r"^\d{4}\s*", "", text).strip()
    bad_values = ["登入查看", "登入", "查看", "--", "-", "nan"]
    if text in bad_values:
        return ""
    if "登入" in text or "查看" in text:
        return ""
    return text


def build_stock_name_series(df: pd.DataFrame, code_col: str, name_col) -> pd.Series:
    name_from_code = df[code_col].apply(extract_stock_name)
    if name_col and name_col in df.columns and name_col != code_col:
        name_from_name_col = df[name_col].astype(str).str.strip()
        invalid_name = (
            name_from_name_col.eq("")
            | name_from_name_col.str.lower().eq("nan")
            | name_from_name_col.str.contains("登入|查看", na=False)
        )
        name_from_name_col = name_from_name_col.mask(invalid_name, "")
        return name_from_code.mask(
            name_from_code.astype(str).str.strip() == "", name_from_name_col
        )
    return name_from_code


def is_stock_code_like(value) -> bool:
    code = normalize_stock_code(value)
    return bool(re.fullmatch(r"\d{4}", code))


def score_as_stock_code_column(series: pd.Series) -> float:
    values = series.dropna().astype(str).str.strip()
    if values.empty:
        return 0.0
    sample = values.head(100)
    hit_count = sample.apply(is_stock_code_like).sum()
    return hit_count / len(sample)


def score_as_name_column(series: pd.Series) -> float:
    values = series.dropna().astype(str).str.strip()
    if values.empty:
        return 0.0
    sample = values.head(100)
    bad_values = ["登入查看", "登入", "查看", "--", "-", "nan"]

    def is_name_like(x: str) -> bool:
        x = str(x).strip()
        if not x or x.lower() == "nan":
            return False
        if x in bad_values:
            return False
        if "登入" in x or "查看" in x:
            return False
        if is_stock_code_like(x):
            return False
        return bool(re.search(r"[一-鿿A-Za-z]", x))

    hit_count = sample.apply(is_name_like).sum()
    return hit_count / len(sample)


def score_as_weight_column(series: pd.Series) -> float:
    values = series.dropna().astype(str).str.strip()
    if values.empty:
        return 0.0
    sample = values.head(100)

    def is_weight_like(x: str) -> bool:
        x = x.replace(",", "").replace("%", "").strip()
        try:
            val = float(x)
            return 0 <= val <= 100
        except ValueError:
            return False

    hit_count = sample.apply(is_weight_like).sum()
    return hit_count / len(sample)


def score_as_share_column(series: pd.Series) -> float:
    values = series.dropna().astype(str).str.strip()
    if values.empty:
        return 0.0
    sample = values.head(100)

    def is_share_like(x: str) -> bool:
        x = x.replace(",", "").strip()
        if not re.fullmatch(r"\d+(\.0)?", x):
            return False
        try:
            val = float(x)
            return val >= 1000
        except ValueError:
            return False

    hit_count = sample.apply(is_share_like).sum()
    return hit_count / len(sample)


def parse_weight_to_number(value):
    if pd.isna(value):
        return None
    text = str(value).strip().replace("%", "").replace(",", "")
    if text == "" or text.lower() == "nan":
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_share_to_number(value):
    if pd.isna(value):
        return None
    text = str(value).strip().replace(",", "")
    if text == "" or text.lower() == "nan":
        return None
    try:
        return float(text)
    except ValueError:
        return None


def pick_common_columns(df: pd.DataFrame) -> dict:
    df = df.copy()
    df.columns = make_unique_columns(df.columns)

    exclude_cols = {"ETF", "抓取時間", "資料來源", "來源頁次", "共同持有ETF"}
    bad_code_keywords = ["股數", "權重", "收盤價", "漲跌幅", "貢獻度", "持股變化", "價格", "報酬"]
    bad_name_keywords = ["持股變化", "貢獻度", "漲跌幅", "收盤價", "權重", "股數", "來源", "頁次"]

    candidate_cols = [col for col in df.columns if str(col) not in exclude_cols]

    # ⚠️ 2026-08-23調整：原本遇到欄名含「股數/權重/收盤價/漲跌幅」等字樣就直接
    # continue(整欄跳過、完全不看內容)。但etfinfo.tw這次的表格出現過「欄名跟欄位
    # 內容對不上」的情況(研判是原始HTML的多層表頭colspan/rowspan跟pandas.read_html
    # 攤平表頭的方式沒對齊，導致某一欄的「表頭文字」其實是別的欄位的、但底下裝的
    # 卻是股票代碼+名稱的真實內容)。原本的寫法會直接把這種「掛羊頭賣狗肉」的欄位
    # 整個排除掉，內容分數再高也永遠不會被選到，導致抓不到代碼欄整檔失敗。
    # 改成「壞關鍵字只扣分、不直接淘汰」——如果內容本身分數夠高(像是一堆看起來
    # 像股票代碼的值)，還是能夠勝出；如果內容真的是價格/權重這種數字，內容分數
    # 接近0，扣分後还是會被自然淘汰，不會誤選。
    code_scores = []
    for col in candidate_cols:
        col_text = str(col)
        score = score_as_stock_code_column(df[col])
        if any(k in col_text for k in bad_code_keywords):
            score -= 0.3
        if "代號" in col_text:
            score += 0.3
        code_scores.append((col, score))
    code_scores = sorted(code_scores, key=lambda x: x[1], reverse=True)
    code_col = code_scores[0][0] if code_scores and code_scores[0][1] > 0.5 else None

    name_scores = []
    for col in candidate_cols:
        if col == code_col:
            continue
        col_text = str(col)
        score = score_as_name_column(df[col])
        if any(k in col_text for k in bad_name_keywords):
            score -= 0.3
        if "名稱" in col_text:
            score += 0.5
        name_scores.append((col, score))
    name_scores = sorted(name_scores, key=lambda x: x[1], reverse=True)
    name_col = name_scores[0][0] if name_scores and name_scores[0][1] > 0.5 else None

    weight_scores = []
    for col in candidate_cols:
        if col in [code_col, name_col]:
            continue
        score = score_as_weight_column(df[col])
        col_text = str(col)
        if "權重" in col_text:
            score += 0.3
        if "股數" in col_text:
            score -= 0.4
        weight_scores.append((col, score))
    weight_scores = sorted(weight_scores, key=lambda x: x[1], reverse=True)
    weight_col = weight_scores[0][0] if weight_scores and weight_scores[0][1] > 0.5 else None

    share_scores = []
    for col in candidate_cols:
        if col in [code_col, name_col, weight_col]:
            continue
        score = score_as_share_column(df[col])
        col_text = str(col)
        if "股數" in col_text:
            score += 0.3
        if "權重" in col_text:
            score -= 0.4
        share_scores.append((col, score))
    share_scores = sorted(share_scores, key=lambda x: x[1], reverse=True)
    share_col = share_scores[0][0] if share_scores and share_scores[0][1] > 0.5 else None

    logging.info(
        f"欄位判斷結果：股票代碼欄={code_col}, 股票名稱欄={name_col}, "
        f"權重欄={weight_col}, 股數欄={share_col}"
    )

    return {"code_col": code_col, "name_col": name_col, "weight_col": weight_col, "share_col": share_col}


# =========================
# 表格解析工具 (沿用原腳本邏輯，未修改)
# =========================
def looks_like_holding_table(df: pd.DataFrame) -> bool:
    if df.empty:
        return False
    columns_text = "".join(map(str, df.columns))
    sample_text = df.head(10).astype(str).to_string()
    text = columns_text + sample_text
    keywords = ["代號", "名稱", "權重", "股數", "持股", "成分股", "貢獻度"]
    hit_count = sum(keyword in text for keyword in keywords)
    return len(df) >= 5 and hit_count >= 2


def extract_best_table_from_html(html: str):
    try:
        tables = pd.read_html(StringIO(html))
    except ValueError:
        # 頁面裡真的一個table都沒有時，pandas會丟這個。
        return None
    except ImportError:
        # ⚠️ 2026-08-23發現：當lxml解析不到任何表格時，pandas.read_html()內部會
        # 自動改試下一個解析引擎(html5lib)當備援，如果html5lib沒裝，會丟出
        # ImportError而不是預期的ValueError。這裡一併接住，避免這種情況把
        # 「這頁根本沒有表格」誤判成程式crash。
        return None
    candidates = []
    for table in tables:
        table = flatten_multiindex_columns(table)
        table = clean_columns(table)
        if looks_like_holding_table(table):
            candidates.append(table)
    if not candidates:
        return None
    return max(candidates, key=len)


def clean_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df = df.dropna(how="all")
    if df.empty:
        return df
    first_col = df.columns[0]
    df = df[df[first_col].astype(str) != str(first_col)]
    joined = df.astype(str).agg(" ".join, axis=1)
    df = df[~joined.str.contains(r"上一頁|下一頁|^\s*\d+\s*/\s*\d+\s*$", regex=True, na=False)]
    return df.reset_index(drop=True)


def add_metadata(df: pd.DataFrame, etf_code: str, source_url: str, page_no: int) -> pd.DataFrame:
    df = df.copy()
    fetch_time = now_taipei().strftime("%Y-%m-%d %H:%M:%S")
    df.insert(0, "ETF", etf_code)
    df.insert(1, "抓取時間", fetch_time)
    df.insert(2, "資料來源", source_url)
    df.insert(3, "來源頁次", page_no)
    return df


def remove_duplicate_holdings(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = make_unique_columns(df.columns)
    col_map = pick_common_columns(df)
    code_col = col_map["code_col"]
    if code_col:
        df["_股票代碼"] = df[code_col].apply(normalize_stock_code)
        df = df[df["_股票代碼"] != ""]
        df = df.drop_duplicates(subset=["ETF", "_股票代碼"], keep="first")
        df = df.drop(columns=["_股票代碼"])
    else:
        logging.warning("找不到股票代碼欄，改用整列去重。")
        df = df.drop_duplicates(keep="first")
    return df.reset_index(drop=True)


# =========================
# 翻頁工具 (沿用原腳本邏輯，未修改)
# =========================
async def make_page_signature(page) -> str:
    try:
        text = await page.locator("body").inner_text(timeout=5000)
        return text[:3000]
    except Exception:
        return ""


async def click_next_page_if_possible(page) -> bool:
    # ⚠️ 2026-08-23調整：原本第一個候選用 page.get_by_text(...)，但在Streamlit Cloud
    # 實際部署環境上曾經出現 AttributeError: 'Page' object has no attribute 'get_by_text'
    # ——原因研判是雲端環境實際解析安裝到的 playwright 版本跟sandbox不同(此環境沒辦法
    # 直接驗證，因為requirements.txt裡的 playwright 沒有鎖版本)，導致這個相對較新的
    # Locators輔助方法在該版本上不存在。改用 page.locator("text=下一頁") 達到完全一樣的
    # 效果——.locator() 是Playwright從一開始就有的最基礎API，不會有這個版本相容性問題，
    # 而且這行本來就已經是下面候選清單的其中一個，等於只是把它移到第一順位、不再依賴
    # get_by_text。
    next_locators = [
        page.locator("text=下一頁"),
        page.locator("a:has-text('下一頁')"),
        page.locator("button:has-text('下一頁')"),
        page.locator("a:has-text('»')"),
        page.locator("button:has-text('»')"),
    ]
    before_signature = await make_page_signature(page)
    for locator in next_locators:
        try:
            count = await locator.count()
            if count == 0:
                continue
            item = locator.last
            if not await item.is_visible(timeout=2000):
                continue
            try:
                if not await item.is_enabled(timeout=2000):
                    continue
            except Exception:
                pass
            await item.click(timeout=5000)
            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
            await page.wait_for_timeout(PAGE_WAIT_MS)
            after_signature = await make_page_signature(page)
            if after_signature and after_signature != before_signature:
                return True
        except Exception:
            continue
    return False


# =========================
# 抓取單一 ETF (沿用原腳本邏輯，未修改)
# =========================
async def fetch_one_etf_full_holdings(browser, etf_code: str, url: str) -> pd.DataFrame:
    logging.info(f"開始抓取完整持股: {etf_code} {url}")
    page = await browser.new_page(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
        locale="zh-TW",
        viewport={"width": 1440, "height": 1200},
    )

    # ⚠️ 2026-08-23調整：原本用 wait_until="networkidle"，但實際在 GitHub Actions
    # 上觀察到部分ETF頁面會因為背景的分析/廣告連線一直有流量、導致「網路真的完全
    # 閒置」遲遲不會發生，白白等到60秒逾時——即使頁面內容其實早就渲染完成了。
    # 改成「等DOM載入完成」+「明確等待頁面出現表格」，只要真正需要的內容(表格)
    # 出現就繼續，不會被無關的背景網路流量拖累到逾時。
    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    try:
        await page.wait_for_selector("table", timeout=20000)
    except Exception:
        logging.warning(f"{etf_code} 等待表格出現逾時(20秒)，仍嘗試繼續解析目前頁面內容。")
    await page.wait_for_timeout(PAGE_WAIT_MS)

    page_tables = []
    seen_signatures = set()

    for page_no in range(1, MAX_PAGES_PER_ETF + 1):
        signature = await make_page_signature(page)
        if signature in seen_signatures:
            logging.warning(f"{etf_code} 偵測到頁面重複，停止翻頁")
            break
        seen_signatures.add(signature)

        html = await page.content()
        table = extract_best_table_from_html(html)

        if table is None:
            logging.warning(f"{etf_code} 第 {page_no} 頁找不到持股表")
        else:
            table = clean_dataframe(table)
            if not table.empty:
                table = add_metadata(table, etf_code=etf_code, source_url=url, page_no=page_no)
                page_tables.append(table)
                logging.info(f"{etf_code} 第 {page_no} 頁抓到 {len(table)} 筆")
            else:
                logging.warning(f"{etf_code} 第 {page_no} 頁表格為空")

        has_next = await click_next_page_if_possible(page)
        if not has_next:
            logging.info(f"{etf_code} 沒有下一頁，翻頁結束")
            break

    await page.close()

    if not page_tables:
        raise RuntimeError(f"{etf_code} 未抓到任何持股資料")

    result = pd.concat(page_tables, ignore_index=True)
    result = remove_duplicate_holdings(result)
    logging.info(f"{etf_code} 完整抓取完成，去重後共 {len(result)} 筆")
    return result


# =========================
# 標準化持股資料 (沿用原腳本邏輯，未修改)
# =========================
def standardize_holdings_for_compare(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = make_unique_columns(df.columns)
    col_map = pick_common_columns(df)
    code_col = col_map["code_col"]
    name_col = col_map["name_col"]
    weight_col = col_map["weight_col"]
    share_col = col_map["share_col"]

    if not code_col:
        # ⚠️ 2026-08-23調整：原本錯誤訊息只有欄位「名稱」列表，看不出實際內容長什麼樣子，
        # 導致每次判斷失敗都要另外想辦法(WebFetch/使用者截圖)才能知道實際資料格式。
        # 這裡把每個「候選欄位」的前3筆實際內容一起印出來，下次如果還是抓不到，
        # log會直接告訴我實際資料長怎樣，不用再用猜的。
        sample_lines = []
        for col in df.columns:
            if str(col) in {"ETF", "抓取時間", "資料來源", "來源頁次"}:
                continue
            try:
                samples = df[col].dropna().astype(str).head(3).tolist()
            except Exception:
                samples = ["<無法讀取>"]
            sample_lines.append(f"[{col}] {samples}")
        sample_text = " | ".join(sample_lines)
        raise RuntimeError(
            f"找不到股票代碼欄位，目前欄位為：{list(df.columns)}；"
            f"各候選欄位前3筆內容：{sample_text}"
        )

    result = pd.DataFrame(index=df.index)
    result["股票代碼"] = df[code_col].apply(normalize_stock_code)
    result = result[result["股票代碼"] != ""].copy()

    name_series = build_stock_name_series(df, code_col, name_col)
    result["股票名稱"] = name_series.loc[result.index]

    if weight_col and weight_col in df.columns:
        result["權重"] = df.loc[result.index, weight_col]
        result["權重數值"] = result["權重"].apply(parse_weight_to_number)
    else:
        result["權重"] = ""
        result["權重數值"] = None

    if share_col and share_col in df.columns:
        result["股數"] = df.loc[result.index, share_col]
        result["股數數值"] = result["股數"].apply(parse_share_to_number)
    else:
        result["股數"] = ""
        result["股數數值"] = None

    result = result.drop_duplicates(subset=["股票代碼"], keep="first")
    result = result.reset_index(drop=True)
    return result


# =========================
# 每日異動比較 (改讀資料庫，取代讀取前一天的xlsx檔案)
# =========================
def format_number_change(value) -> str:
    if pd.isna(value):
        return ""
    if value == 0:
        return "0"
    return f"{value:,.0f}"


def format_weight_change(value) -> str:
    if pd.isna(value):
        return ""
    if value == 0:
        return "0.00%"
    return f"{value:+.2f}%"


def get_action(change_type, share_change):
    if change_type == "新增":
        return "新納入"
    if change_type == "刪除":
        return "全數賣出"
    if pd.isna(share_change):
        return ""
    if share_change > 0:
        return "加碼"
    if share_change < 0:
        return "減碼"
    return ""


def compare_etf_holdings(etf_code: str, current_df: pd.DataFrame, previous_std_df: pd.DataFrame, previous_date: str) -> pd.DataFrame:
    """
    比較 current_df(本次抓到、尚未標準化) 跟 previous_std_df(資料庫讀出、已經是
    standardize_holdings_for_compare() 格式) 的差異。取代原本讀 xlsx 再標準化的做法，
    因為資料庫存的時候就已經是標準化格式了，不用再轉一次。
    """
    current_std = standardize_holdings_for_compare(current_df)

    merged = previous_std_df.merge(
        current_std, on="股票代碼", how="outer", suffixes=("_昨日", "_今日"), indicator=True
    )

    def get_change_type(row):
        if row["_merge"] == "left_only":
            return "刪除"
        if row["_merge"] == "right_only":
            return "新增"
        return "持續持有"

    merged["異動類型"] = merged.apply(get_change_type, axis=1)
    merged["權重變化數值"] = merged["權重數值_今日"] - merged["權重數值_昨日"]
    merged["股數變化數值"] = merged["股數數值_今日"] - merged["股數數值_昨日"]
    merged["權重變化"] = merged["權重變化數值"].apply(format_weight_change)
    merged["股數變化"] = merged["股數變化數值"].apply(format_number_change)
    merged["調整方向"] = merged.apply(lambda r: get_action(r["異動類型"], r["股數變化數值"]), axis=1)
    merged["ETF"] = etf_code
    merged["比較基準日期"] = previous_date

    share_changed = merged["股數變化數值"].fillna(0) != 0
    added_or_removed = merged["異動類型"].isin(["新增", "刪除"])
    changed_only = merged[added_or_removed | share_changed].copy()

    if changed_only.empty:
        return pd.DataFrame()

    output = changed_only[[
        "ETF", "異動類型", "調整方向", "股票代碼", "股票名稱_昨日", "股票名稱_今日",
        "權重_昨日", "權重_今日", "權重變化", "股數_昨日", "股數_今日", "股數變化",
        "比較基準日期",
        "權重數值_昨日", "權重數值_今日", "權重變化數值",
        "股數數值_昨日", "股數數值_今日", "股數變化數值",
    ]].copy()

    # 補上 etf_db.save_holding_changes() 需要的內部欄位名稱
    output["_權重數值_昨日"] = output["權重數值_昨日"]
    output["_權重數值_今日"] = output["權重數值_今日"]
    output["_權重變化數值"] = output["權重變化數值"]
    output["_股數數值_昨日"] = output["股數數值_昨日"]
    output["_股數數值_今日"] = output["股數數值_今日"]
    output["_股數變化數值"] = output["股數變化數值"]

    sort_priority = {"新增": 1, "刪除": 2, "持續持有": 3}
    output["_排序"] = output["異動類型"].map(sort_priority).fillna(9)
    output["_股票代碼排序"] = pd.to_numeric(output["股票代碼"], errors="coerce")
    output = (
        output.sort_values(by=["_排序", "_股票代碼排序", "股票代碼"], ascending=[True, True, True], na_position="last")
        .drop(columns=["_排序", "_股票代碼排序"])
        .reset_index(drop=True)
    )
    return output


# =========================
# 單一ETF「抓取+存快照+比較異動+寫紀錄」(排程/手動共用)
# =========================
async def fetch_and_save_one_etf(browser, db_path: str, etf_code: str, url: str, run_date: str) -> dict:
    """
    抓取單一ETF的完整持股、存快照、跟資料庫裡前一次快照比較存異動、寫入抓取紀錄。

    這個函式被兩個地方共用：
      - main_async()：GitHub Actions 排程，一次跑全部主動式ETF。
      - run_fetch_for_etfs()：pages/7 頁面上「立即抓取」按鈕，可以只跑使用者
        選擇的部分ETF(例如只抓目前選擇的那一檔)。
    共用同一套邏輯，才不會「排程抓的」跟「手動按鈕抓的」行為兜不起來。

    回傳 {"etf_code", "status"("success"/"failed"), "row_count", "n_changes", "message"}。
    """
    # ⚠️ 2026-08-23新增：GitHub Actions上觀察到部分ETF會遇到 Page.goto Timeout
    # (可能是etfinfo.tw對雲端機房IP偶發性的流量限制、或單純網路較慢/不穩定，
    # 使用者自己電腦上直接跑同一支腳本反而不會遇到)。這種逾時通常是暫時性的，
    # 重試個1~2次很有機會就成功，所以在「抓取」這一步(不含後面存檔/比較邏輯)
    # 加上最多3次嘗試、每次間隔5秒再重試。
    df = None
    fetch_error = None
    for attempt in range(1, 4):
        try:
            df = await fetch_one_etf_full_holdings(browser=browser, etf_code=etf_code, url=url)
            break
        except Exception as e:
            fetch_error = e
            logging.warning(f"{etf_code} 第{attempt}次抓取嘗試失敗: {type(e).__name__}: {e}")
            if attempt < 3:
                await asyncio.sleep(5)

    if df is None:
        error_msg = f"{type(fetch_error).__name__}: {fetch_error}"
        logging.error(f"{etf_code} 重試3次後仍抓取失敗: {error_msg}\n{traceback.format_exc()}")
        etf_db.log_fetch_run(db_path, run_date, etf_code, "failed", row_count=0, message=error_msg)
        return {"etf_code": etf_code, "status": "failed", "row_count": 0, "n_changes": 0, "message": error_msg}

    try:
        if len(df) < MIN_EXPECTED_ROWS_WARN:
            logging.warning(f"{etf_code} 抓到 {len(df)} 筆，數量偏少，請留意網站是否改版。")

        std_df = standardize_holdings_for_compare(df)
        n_saved = etf_db.save_holdings_snapshot(db_path, etf_code, run_date, std_df)
        logging.info(f"{etf_code} 持股快照已存入資料庫，共 {n_saved} 筆")

        conn = etf_db.get_connection(db_path)
        previous_date = etf_db.get_previous_snapshot_date(conn, etf_code, run_date)

        n_changes = 0
        if previous_date:
            try:
                previous_std_df = etf_db.get_holdings_snapshot(conn, etf_code, previous_date)
                # 欄位名稱對齊 standardize_holdings_for_compare() 的輸出格式
                previous_std_df = previous_std_df.rename(columns={
                    "stock_code": "股票代碼", "stock_name": "股票名稱",
                    "weight": "權重數值", "weight_text": "權重",
                    "shares": "股數數值", "shares_text": "股數",
                })[["股票代碼", "股票名稱", "權重", "權重數值", "股數", "股數數值"]]

                changes_df = compare_etf_holdings(etf_code, df, previous_std_df, previous_date)
                n_changes = etf_db.save_holding_changes(db_path, etf_code, run_date, changes_df)
                logging.info(f"{etf_code} 異動比較完成，共 {n_changes} 筆異動 (基準日 {previous_date})")
            except Exception as e:
                logging.error(f"{etf_code} 異動比較失敗: {e}")
        else:
            logging.warning(f"{etf_code} 資料庫內找不到更早的快照，略過本次異動比較(這是它第一次被抓取)。")

        etf_db.log_fetch_run(db_path, run_date, etf_code, "success", row_count=len(df))
        return {"etf_code": etf_code, "status": "success", "row_count": len(df), "n_changes": n_changes, "message": ""}

    except Exception as e:
        error_msg = f"{type(e).__name__}: {e}"
        logging.error(f"{etf_code} 抓取失敗: {error_msg}\n{traceback.format_exc()}")
        etf_db.log_fetch_run(db_path, run_date, etf_code, "failed", row_count=0, message=error_msg)
        return {"etf_code": etf_code, "status": "failed", "row_count": 0, "n_changes": 0, "message": error_msg}


async def _launch_chromium_with_auto_install(playwright_instance):
    """
    啟動Chromium；如果偵測到「瀏覽器binary根本沒安裝」(在Streamlit Cloud這種
    環境第一次跑很可能會遇到)，自動跑一次 `playwright install chromium` 再重試一次。

    ⚠️ 這裡只能自動補「瀏覽器binary本身」，沒辦法補系統動態函式庫(例如libnss3等)——
    那些需要在部署環境的 packages.txt 裡設定好，這裡沒有sudo權限可以裝。
    如果是缺系統函式庫，這裡重試還是會失敗，錯誤訊息會直接往外拋，讓網頁那邊
    可以印出清楚的錯誤提示。
    """
    try:
        return await launch_chromium_browser(playwright_instance)
    except Exception as e:
        err_text = str(e)
        if "Executable doesn't exist" not in err_text:
            raise
        logging.warning("偵測到Chromium瀏覽器binary尚未安裝，嘗試自動安裝(python -m playwright install chromium)...")
        import subprocess
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            check=True, capture_output=True, text=True, timeout=300,
        )
        return await launch_chromium_browser(playwright_instance)


async def launch_browser(playwright_instance):
    """
    雙瀏覽器統一入口：先試Edge(本機Windows環境會成功)，失敗就自動退回Chromium
    (GitHub Actions / Streamlit Cloud等沒有Edge的環境)。main_async()跟
    run_fetch_for_etfs()都改叫這個函式，不用各自判斷要用哪個瀏覽器。
    """
    try:
        browser = await launch_edge_browser(playwright_instance)
        logging.info("已使用 Microsoft Edge 啟動瀏覽器")
        return browser
    except Exception as e:
        logging.info(f"本機找不到可用的Edge({e})，改用 headless Chromium")
        return await _launch_chromium_with_auto_install(playwright_instance)


async def run_fetch_for_etfs(etf_codes: list, db_path: str = DB_PATH, csv_path: str = ACTIVE_ETF_LIST_CSV, on_progress=None) -> dict:
    """
    只抓「指定的一部分ETF」，供 pages/7 頁面的「立即抓取」按鈕使用
    (跟 main_async() 的差異是：main_async() 一定抓 active_etf_list.csv 裡全部ETF，
    這裡可以是任意子集合，例如只抓目前選擇的1檔)。

    on_progress(index, total, result_dict)：每抓完一檔就會呼叫一次，供網頁畫進度條用。
    """
    etf_map = load_active_etf_list(csv_path)
    run_date = today_string()

    targets = [(code, etf_map[code]) for code in etf_codes if code in etf_map]
    missing = [code for code in etf_codes if code not in etf_map]

    results = []
    async with async_playwright() as p:
        browser = await launch_browser(p)
        try:
            for i, (etf_code, info) in enumerate(targets, start=1):
                result = await fetch_and_save_one_etf(browser, db_path, etf_code, info["url"], run_date)
                results.append(result)
                if on_progress:
                    on_progress(i, len(targets), result)
        finally:
            await browser.close()

    success = [r["etf_code"] for r in results if r["status"] == "success"]
    fail = [(r["etf_code"], r["message"]) for r in results if r["status"] == "failed"]
    return {"run_date": run_date, "results": results, "success": success, "fail": fail, "missing_from_csv": missing}


def run_fetch_for_etfs_sync(etf_codes: list, db_path: str = DB_PATH, csv_path: str = ACTIVE_ETF_LIST_CSV, on_progress=None) -> dict:
    """
    給 Streamlit 頁面(同步的script)直接呼叫用的包裝——內部用 asyncio.run() 執行，
    跟一般的 async def 函式呼叫方式不同，這裡故意設計成同步函式，
    這樣頁面程式碼不用自己管理事件迴圈。
    """
    return asyncio.run(run_fetch_for_etfs(etf_codes, db_path=db_path, csv_path=csv_path, on_progress=on_progress))


# =========================
# 主程式
# =========================
async def main_async():
    setup_logging()
    logging.info("開始抓取主動式 ETF 完整持股明細 (Chromium 版)")

    etf_map = load_active_etf_list()
    logging.info(f"本次共 {len(etf_map)} 檔主動式ETF需要抓取: {list(etf_map.keys())}")

    run_date = today_string()
    success_list = []
    fail_list = []

    async with async_playwright() as p:
        browser = await launch_browser(p)

        for etf_code, info in etf_map.items():
            result = await fetch_and_save_one_etf(browser, DB_PATH, etf_code, info["url"], run_date)
            if result["status"] == "success":
                success_list.append(etf_code)
            else:
                fail_list.append((etf_code, result["message"]))

        await browser.close()

    # ===== Telegram 通知 =====
    lines = [
        "🏦 <b>主動式ETF持股每日抓取完成</b>",
        f"📅 {run_date}",
        f"✅ 成功: {len(success_list)} 檔",
    ]
    if fail_list:
        lines.append(f"❌ 失敗: {len(fail_list)} 檔")
        for code, msg in fail_list[:8]:
            lines.append(f"　- {code}: {msg[:80]}")
        if len(fail_list) > 8:
            lines.append(f"　...(還有 {len(fail_list) - 8} 檔，詳見 Actions log)")

    try:
        conn = etf_db.get_connection(DB_PATH)
        common_df = etf_db.get_common_changes(
            conn, run_date, list(etf_map.keys()), min_etf_count=NOTIFY_MIN_COMMON_CHANGE_ETF_COUNT
        )
        if not common_df.empty:
            lines.append(f"\n📊 至少{NOTIFY_MIN_COMMON_CHANGE_ETF_COUNT}檔ETF同日共同異動: {len(common_df)} 檔股票")
            for _, r in common_df.head(10).iterrows():
                lines.append(f"　{r['股票代碼']} {r['股票名稱']}｜{r['共同方向']}｜{r['異動ETF數']}檔ETF")
        else:
            lines.append(f"\n📊 今日沒有至少{NOTIFY_MIN_COMMON_CHANGE_ETF_COUNT}檔ETF同時異動的股票。")
    except Exception as e:
        lines.append(f"\n⚠️ 產生共同異動摘要時發生錯誤: {e}")

    send_telegram_message("\n".join(lines))
    logging.info("程式結束")

    # 若全部ETF都失敗，讓 GitHub Actions 這個 step 回報失敗，方便觸發警示
    if success_list == [] and fail_list:
        sys.exit(1)


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
