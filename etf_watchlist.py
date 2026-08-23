"""
etf_watchlist.py
=================
「可編輯ETF追蹤清單」設定檔存取工具。

設計成獨立小檔案(不是直接寫在頁面裡)，方便之後其他頁面(例如未來要在
Stock simulator 疊加ETF標記時)也能重複使用同一份設定，不用複製貼上邏輯。

設定檔格式 (etf_watchlist_config.json):
{
    "tracked_etfs": ["00981A", "00991A", "00980A", "00982A", "00403A", "00985A"],
    "common_change_min_etf_count": 2,
    "updated_at": "2026-08-23 12:00:00"
}

- tracked_etfs: 使用者在網頁上勾選、目前想「重點關注/分析」的ETF代碼清單。
  ⚠️ 這份清單只影響「網頁上分析時預設focus哪些ETF」，
     不影響每日抓取範圍——抓取一律抓 Database/active_etf_list.csv 裡全部主動式ETF
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
    """讀取 Database/active_etf_list.csv，回傳 {代號: 名稱} 對照表，供頁面checkbox顯示用。"""
    import pandas as pd

    if not os.path.exists(csv_path):
        return {}
    df = pd.read_csv(csv_path, encoding="utf-8-sig", dtype=str)
    df.columns = [c.strip() for c in df.columns]
    result = {}
    for _, row in df.iterrows():
        code = str(row.get("股票代號", "")).strip()
        name = str(row.get("ETF名稱", "")).strip()
        if code:
            result[code] = name
    return result
