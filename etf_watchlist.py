"""
etf_watchlist.py
=================
「可編輯ETF追蹤清單」設定檔存取工具。

設計成獨立小檔案(不是直接寫在頁面裡)，方便之後其他頁面(例如未來要在
Stock simulator 疊加ETF標記時)也能重複使用同一份設定，不用複製貼上邏輯。

設定檔位置：ETF_data/etf_watchlist_config.json (2026-08-24起跟 active_etf_list.csv /
etf_holdings.db 一起集中放在 ETF_data/ 資料夾，不再放repo根目錄)。

設定檔格式：
{
    "tracked_etfs": ["00981A", "00991A", "00980A", "00982A", "00403A", "00985A"],
    "common_change_min_etf_count": 2,
    "updated_at": "2026-08-23 12:00:00"
}

- tracked_etfs: 使用者在網頁上勾選、目前想「重點關注/分析」的ETF代碼清單。
  ⚠️ 這份清單只影響「網頁上分析時預設focus哪些ETF」，
     不影響每日抓取範圍——抓取一律抓 ETF_data/active_etf_list.csv 裡全部主動式ETF
     (見 fetch_etf_holdings.py)，這樣之後調整追蹤清單不會漏掉歷史資料。
- common_change_min_etf_count: 「多檔ETF同日共同買賣」門檻的使用者上次設定值，
  網頁上用滑桿調整時順便記住，下次打開網頁不用重設。
"""
import json
import os
from datetime import datetime

DEFAULT_TRACKED_ETFS = ["00981A", "00991A", "00980A", "00982A", "00403A", "00985A"]
DEFAULT_MIN_ETF_COUNT = 2


def default_config() -> dict:
    return {
        "tracked_etfs": list(DEFAULT_TRACKED_ETFS),
        "common_change_min_etf_count": DEFAULT_MIN_ETF_COUNT,
        "updated_at": "",
    }


def load_watchlist_config(config_path: str) -> dict:
    """讀取設定檔，檔案不存在或格式錯誤時回傳預設值(不拋例外，避免整頁掛掉)。"""
    if not os.path.exists(config_path):
        return default_config()
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        cfg = default_config()
        if isinstance(data.get("tracked_etfs"), list):
            cfg["tracked_etfs"] = [str(c).strip() for c in data["tracked_etfs"] if str(c).strip()]
        if isinstance(data.get("common_change_min_etf_count"), (int, float)):
            cfg["common_change_min_etf_count"] = int(data["common_change_min_etf_count"])
        cfg["updated_at"] = data.get("updated_at", "")
        return cfg
    except Exception:
        return default_config()


def save_watchlist_config(config_path: str, tracked_etfs: list, common_change_min_etf_count: int) -> dict:
    """存檔(覆寫)，回傳寫入的完整內容，供呼叫端顯示或選擇性上傳GitHub。"""
    cfg = {
        "tracked_etfs": [str(c).strip() for c in tracked_etfs if str(c).strip()],
        "common_change_min_etf_count": int(common_change_min_etf_count),
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    os.makedirs(os.path.dirname(config_path) or ".", exist_ok=True)
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return cfg


def load_active_etf_name_map(csv_path: str) -> dict:
    """
    讀取 ETF_data/active_etf_list.csv，回傳 {代號: 名稱} 對照表，供頁面checkbox顯示用。

    ⚠️ 2026-08-24：只回傳「啟用」欄位為1的ETF(目前先只穩定6檔純台股ETF)，
    避免頁面下拉選單/「全部XX檔」選項列出還沒有實際資料的ETF、造成使用者選了
    卻看到「沒有資料」的困惑。CSV沒有「啟用」欄位時(舊格式)視為全部啟用。
    """
    import pandas as pd

    if not os.path.exists(csv_path):
        return {}
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
        result[code] = name
    return result


def load_full_active_etf_list(csv_path: str):
    """
    2026-08-24新增：讀取 active_etf_list.csv「完整內容」(不像 load_active_etf_name_map
    那樣篩選掉啟用=0的列)，供頁面「⚙️ ETF抓取範圍管理」UI使用——那裡需要讓使用者
    看到全部32檔、勾選/取消勾選每一檔的啟用狀態，跟 load_active_etf_name_map()
    只回傳「已經啟用」的用途不同。

    回傳一個 pandas.DataFrame，欄位：股票代號、ETF名稱、啟用(int，0或1)，
    保留CSV原本的列順序。檔案不存在時回傳空的DataFrame(欄位仍然存在，避免呼叫端要
    額外判斷None)。
    """
    import pandas as pd

    if not os.path.exists(csv_path):
        return pd.DataFrame(columns=["股票代號", "ETF名稱", "啟用"])
    df = pd.read_csv(csv_path, encoding="utf-8-sig", dtype=str)
    df.columns = [c.strip() for c in df.columns]
    if "啟用" not in df.columns:
        df["啟用"] = "1"
    df["股票代號"] = df["股票代號"].astype(str).str.strip()
    df["ETF名稱"] = df["ETF名稱"].astype(str).str.strip()
    df["啟用"] = df["啟用"].apply(
        lambda v: 1 if str(v).strip() in ("1", "1.0", "True", "true", "是") else 0
    )
    return df[["股票代號", "ETF名稱", "啟用"]]


def save_full_active_etf_list(csv_path: str, updated_df) -> None:
    """
    2026-08-24新增：把「⚙️ ETF抓取範圍管理」UI裡使用者勾選/取消勾選後的啟用狀態，
    寫回 active_etf_list.csv(整份覆寫，保留傳入DataFrame的列順序，通常就是
    load_full_active_etf_list() 讀回來、只改了「啟用」欄位值的那份)。

    ⚠️ 這個函式只負責寫「本次網頁執行環境」的本機檔案——Streamlit Cloud的檔案系統
    是暫時性的(重新部署/長時間無人使用後container會被回收)，而且更重要的是
    GitHub Actions排程是完全獨立的執行環境，每次執行都是重新從repo clone/pull，
    不會讀到這個container本機寫入的檔案。呼叫端(頁面)存完之後，一定要同時呼叫
    upload_file_to_github() 把這個檔案實際推回GitHub repo，排程才會真的套用
    這次的啟用/停用設定，這個函式本身不處理推送GitHub的部分。
    """
    os.makedirs(os.path.dirname(csv_path) or ".", exist_ok=True)
    out_df = updated_df[["股票代號", "ETF名稱", "啟用"]].copy()
    out_df["啟用"] = out_df["啟用"].astype(int)
    out_df.to_csv(csv_path, index=False, encoding="utf-8-sig")
