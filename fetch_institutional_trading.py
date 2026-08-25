"""
fetch_institutional_trading.py
================================
每日三大法人（外資、投信、自營商）個股買賣超批次更新腳本。

寫法與風格比照現有的 update_db.py（同樣的 ROC 日期處理、Telegram 通知、
try/except 保護不讓單一市場失敗擋住另一邊）。

抓取來源:
  上市: TWSE 三大法人買賣超日報 (T86)
  上櫃: TPEx 三大法人買賣明細 (3itrade_hedge_result)

執行方式:
    python fetch_institutional_trading.py

寫入的資料表: institutional_trading (Date, SecurityCode 為複合主鍵)
"""
import argparse
import os
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from typing import Any

import pandas as pd
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

DB_NAME = "twse_ohlcv.db"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json, text/plain, */*"}
TWSE_T86_URL = "https://www.twse.com.tw/rwd/zh/fund/T86"
TPEX_INSTI_URL = "https://www.tpex.org.tw/web/stock/3insti/daily_trade/3itrade_hedge_result.php"


def send_telegram_message(text: str):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("未設定 Telegram 變數，略過推播。")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Telegram 傳送失敗: {e}")


def number(value: Any) -> float:
    text = str(value).replace(",", "").strip()
    if text in {"", "-", "--", "---", "----", "None", "nan", "X"}:
        return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def fetch_twse_institutional(report_date: str) -> pd.DataFrame:
    """report_date: YYYYMMDD"""
    try:
        resp = requests.get(
            TWSE_T86_URL,
            params={"response": "json", "date": report_date, "selectType": "ALL"},
            headers=HEADERS, timeout=30, verify=False,
        )
        resp.raise_for_status()
        payload = resp.json()
        if str(payload.get("stat", "")) not in {"", "OK"}:
            return pd.DataFrame()

        fields = payload.get("fields", [])
        data = payload.get("data", [])
        if not fields or not data:
            return pd.DataFrame()

        df = pd.DataFrame(data, columns=fields)
        # 官方欄位名稱偶爾會有些微調整，這裡用「包含關鍵字」比對比較不容易因為
        # 多一個字/少一個字就整批解析失敗
        def find_col(keyword):
            for c in df.columns:
                if keyword in c:
                    return c
            return None

        code_col = find_col("證券代號")
        name_col = find_col("證券名稱")
        foreign_col = find_col("外陸資買賣超股數(不含外資自營商)") or find_col("外陸資買賣超")
        trust_col = find_col("投信買賣超股數") or find_col("投信買賣超")
        dealer_col = find_col("自營商買賣超股數") or find_col("自營商買賣超")
        total_col = find_col("三大法人買賣超股數合計") or find_col("三大法人買賣超")

        if not code_col:
            return pd.DataFrame()

        out = pd.DataFrame({
            "SecurityCode": df[code_col].astype(str).str.strip(),
            "SecurityName": df[name_col].astype(str).str.strip() if name_col else "",
            "ForeignNet": df[foreign_col].map(number) if foreign_col else 0.0,
            "TrustNet": df[trust_col].map(number) if trust_col else 0.0,
            "DealerNet": df[dealer_col].map(number) if dealer_col else 0.0,
        })
        if total_col:
            out["TotalNet"] = df[total_col].map(number)
        else:
            out["TotalNet"] = out["ForeignNet"] + out["TrustNet"] + out["DealerNet"]

        out.insert(0, "Date", pd.to_datetime(report_date, format="%Y%m%d").date())
        out.insert(1, "Market", "上市")
        return out[out["SecurityCode"].str.len() <= 6].drop_duplicates("SecurityCode")
    except Exception as e:
        print(f"上市三大法人資料解析失敗 ({report_date}): {type(e).__name__}: {e}")
        return pd.DataFrame()


def fetch_tpex_institutional(report_date: str) -> pd.DataFrame:
    """report_date: YYYYMMDD"""
    dt = datetime.strptime(report_date, "%Y%m%d")
    roc_date = f"{dt.year - 1911}/{dt.strftime('%m/%d')}"
    try:
        resp = requests.get(
            TPEX_INSTI_URL,
            params={"l": "zh-tw", "se": "AL", "t": "D", "d": roc_date, "o": "json"},
            headers=HEADERS, timeout=30, verify=False,
        )
        resp.raise_for_status()
        payload = resp.json()

        data_list = []
        if "tables" in payload and payload["tables"]:
            for table in payload["tables"]:
                data_list.extend(table.get("data", []))
        elif "aaData" in payload:
            data_list = payload["aaData"]
        if not data_list:
            return pd.DataFrame()

        # 上櫃三大法人買賣超表欄位順序（依 TPEx 官方頁面欄位定義）:
        # 0代號 1名稱 2外陸資買進 3外陸資賣出 4外陸資買賣超 5外資自營商買賣超
        # 6投信買進 7投信賣出 8投信買賣超 9自營商買賣超(避險前) ... 最後一欄通常是合計
        rows = []
        for row in data_list:
            if len(row) < 9:
                continue
            code = str(row[0]).strip()
            if len(code) > 6:
                continue
            rows.append({
                "Date": dt.date(), "Market": "上櫃", "SecurityCode": code,
                "SecurityName": str(row[1]).strip(),
                "ForeignNet": number(row[4]),
                "TrustNet": number(row[8]) if len(row) > 8 else 0.0,
                "DealerNet": number(row[-2]) if len(row) > 9 else 0.0,
                "TotalNet": number(row[-1]),
            })
        df = pd.DataFrame(rows)
        return df.drop_duplicates("SecurityCode") if not df.empty else df
    except Exception as e:
        print(f"上櫃三大法人資料解析失敗 ({report_date}): {type(e).__name__}: {e}")
        return pd.DataFrame()


def save_to_database(df: pd.DataFrame):
    if df.empty:
        return
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS institutional_trading (
                Date TEXT, Market TEXT, SecurityCode TEXT, SecurityName TEXT,
                ForeignNet REAL, TrustNet REAL, DealerNet REAL, TotalNet REAL,
                PRIMARY KEY (Date, SecurityCode)
            )
        """)
        dates = df["Date"].unique()
        for d in dates:
            conn.execute("DELETE FROM institutional_trading WHERE Date = ?", (str(d),))
        df.to_sql("institutional_trading", conn, if_exists="append", index=False)
        conn.commit()


def parse_args():
    parser = argparse.ArgumentParser(description="抓取台股三大法人買賣超，寫入 institutional_trading 表")
    parser.add_argument(
        "--date", type=str, default=None,
        help="指定單一日期，格式 YYYYMMDD，例如 --date 20260824。"
             "不帶這個參數時，維持原本行為：自動抓「昨天+今天」兩天。",
    )
    parser.add_argument(
        "--start-date", type=str, default=None,
        help="搭配 --end-date 使用，指定日期區間的起始日 (YYYYMMDD)，會逐日抓取範圍內每一天。",
    )
    parser.add_argument(
        "--end-date", type=str, default=None,
        help="搭配 --start-date 使用，指定日期區間的結束日 (YYYYMMDD)。",
    )
    return parser.parse_args()


def build_date_list(args, tw_now: datetime) -> list:
    if args.date:
        return [args.date]
    if args.start_date and args.end_date:
        start = datetime.strptime(args.start_date, "%Y%m%d")
        end = datetime.strptime(args.end_date, "%Y%m%d")
        if end < start:
            raise ValueError("--end-date 不能早於 --start-date")
        dates = []
        d = start
        while d <= end:
            dates.append(d.strftime("%Y%m%d"))
            d += timedelta(days=1)
        return dates
    # 沒帶任何日期參數時，維持原本「昨天+今天」的預設行為（給 GitHub Actions 每日排程用）
    return [
        (tw_now - timedelta(days=1)).strftime("%Y%m%d"),
        tw_now.strftime("%Y%m%d"),
    ]


if __name__ == "__main__":
    tw_tz = timezone(timedelta(hours=8))
    tw_now = datetime.now(tw_tz)
    args = parse_args()
    dates_to_fetch = build_date_list(args, tw_now)
    print(f"本次要抓取的日期: {dates_to_fetch}")

    summary_lines = []
    has_valid_data = False

    for date_str in dates_to_fetch:
        print(f"開始執行三大法人買賣超抓取: {date_str}")
        twse_df = fetch_twse_institutional(date_str)
        time.sleep(2)
        tpex_df = fetch_tpex_institutional(date_str)

        daily_df = pd.concat([twse_df, tpex_df], ignore_index=True)
        if not daily_df.empty:
            save_to_database(daily_df)
            msg = f"📅 {date_str}: 上市 {len(twse_df)} 檔 / 上櫃 {len(tpex_df)} 檔"
            print(f"✅ {msg}")
            summary_lines.append(msg)
            has_valid_data = True
        else:
            msg = f"⏸️ {date_str}: 無三大法人資料 (可能為假日)"
            print(msg)
            summary_lines.append(msg)

        time.sleep(2)

    if has_valid_data:
        send_telegram_message(
            "✅ <b>三大法人買賣超更新成功</b>\n" + "\n".join(summary_lines) +
            "\n🤖 Github Actions 已將資料推回 Repo。"
        )
    else:
        print("兩日皆無三大法人資料，不發送推播。")