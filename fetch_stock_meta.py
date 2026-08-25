"""
fetch_stock_meta.py
====================
熱力圖用的「公司基本資料」批次更新腳本。

從 MOPS OpenData（上市 + 上櫃，同一組資料格式）抓取：
  - 產業別（官方分類，用於熱力圖「族群」維度）
  - 已發行普通股數（用於熱力圖「方塊大小」＝市值，市值 = 已發行股數 × 現價，
    現價在熱力圖頁面即時代入，這裡不存市值本身）

這份資料屬於「低頻異動」等級（除非現金增資、庫藏股、私募等資本額變動事件），
不需要像股價一樣每天抓。建議排程：每週一次（例如週一凌晨），
或財報/重訊公告後手動重跑一次即可，跟 update_db.py 的「每日」排程分開。

執行方式:
    python fetch_stock_meta.py

寫入的資料表: stock_meta (SecurityCode 為主鍵)
"""
import io
import sqlite3
import time
from datetime import datetime, timezone, timedelta

import pandas as pd
import requests

DB_NAME = "twse_ohlcv.db"
HEADERS = {"User-Agent": "Mozilla/5.0"}

# 上市、上櫃公司基本資料共用同一套 MOPS OpenData 欄位格式，只是網址不同
MOPS_URLS = {
    "上市": "https://mopsfin.twse.com.tw/opendata/t187ap03_L.csv",
    "上櫃": "https://mopsfin.twse.com.tw/opendata/t187ap03_O.csv",
}

# TWSE/MOPS 官方「產業別」代碼 → 中文名稱對照表
# (與台股各券商 App 的「類股」分類一致，來源: MOPS 公司代號查詢頁)
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


def number(value) -> float:
    text = str(value).replace(",", "").strip()
    if text in {"", "-", "--", "None", "nan"}:
        return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def fetch_mops_basic(market_label: str, url: str) -> pd.DataFrame:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        resp.encoding = "utf-8-sig"
        df = pd.read_csv(io.StringIO(resp.text), dtype=str)
    except Exception as e:
        print(f"{market_label} 基本資料抓取失敗: {type(e).__name__}: {e}")
        return pd.DataFrame()

    keep = {
        "公司代號": "SecurityCode",
        "公司名稱": "SecurityName",
        "公司簡稱": "ShortName",
        "產業別": "IndustryCode",
        "普通股每股面額": "ParValueRaw",
        "實收資本額": "PaidInCapital",
        "已發行普通股數或TDR原股發行股數": "SharesOutstanding",
    }
    missing = [c for c in keep if c not in df.columns]
    if missing:
        print(f"{market_label} 基本資料欄位不齊全，缺少: {missing}（MOPS 可能異動了欄位名稱）")
        return pd.DataFrame()

    out = df[list(keep.keys())].rename(columns=keep)
    out["SecurityCode"] = out["SecurityCode"].astype(str).str.strip()
    out["SecurityName"] = out["SecurityName"].astype(str).str.strip()
    out["ShortName"] = out["ShortName"].astype(str).str.strip()
    out["IndustryCode"] = out["IndustryCode"].astype(str).str.strip()
    out["IndustryName"] = out["IndustryCode"].map(INDUSTRY_CODE_MAP).fillna("未分類")
    out["PaidInCapital"] = out["PaidInCapital"].map(number)
    out["SharesOutstanding"] = out["SharesOutstanding"].map(number)
    # 面額格式通常是「新台幣    10.0000元」這種字串，少數股票 (如國巨) 面額非10元，
    # 直接用官方欄位裡已經算好的「已發行普通股數」即可，不需要自己用資本額/面額換算，
    # 這裡把面額也存起來只是留作除錯/驗證用。
    out["ParValue"] = out["ParValueRaw"].str.extract(r"([\d.]+)").astype(float)
    out = out.drop(columns=["ParValueRaw"])
    out.insert(1, "Market", market_label)

    # 過濾掉代號不像股票代號的雜訊列 (理論上不會有，但防呆)
    out = out[out["SecurityCode"].str.len() <= 6]
    return out.drop_duplicates("SecurityCode")


def save_to_database(df: pd.DataFrame):
    if df.empty:
        print("⚠️ 沒有任何資料可寫入，略過。")
        return
    with sqlite3.connect(DB_NAME) as conn:
        # 整表覆蓋（stock_meta 是「目前最新一期」的快照，不是逐日累積的歷史資料，
        # 所以直接 replace 最簡單、也不會有舊代碼殘留的問題）
        df.to_sql("stock_meta", conn, if_exists="replace", index=False)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_stock_meta_code ON stock_meta(SecurityCode)")
        conn.commit()


if __name__ == "__main__":
    tw_tz = timezone(timedelta(hours=8))
    tw_now = datetime.now(tw_tz)
    print(f"開始執行公司基本資料抓取: {tw_now.strftime('%Y-%m-%d %H:%M:%S')}")

    frames = []
    for market_label, url in MOPS_URLS.items():
        df = fetch_mops_basic(market_label, url)
        print(f"{market_label}: {len(df)} 檔")
        if not df.empty:
            frames.append(df)
        time.sleep(2)  # 保護性暫停，避免對 MOPS 端點過於頻繁

    if frames:
        combined = pd.concat(frames, ignore_index=True).drop_duplicates("SecurityCode")
        combined["UpdateDate"] = tw_now.strftime("%Y-%m-%d")
        save_to_database(combined)
        print(f"✅ stock_meta 已更新，共 {len(combined)} 檔（上市 + 上櫃）。")
    else:
        print("❌ 上市、上櫃兩邊皆抓取失敗，stock_meta 未更新，維持舊資料。")
