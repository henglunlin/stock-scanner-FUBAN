"""
refresh_active_etf_list.py
============================
維護工具：當 TWSE 有新的主動式ETF上市時，用這支腳本重新產生
ETF_data/active_etf_list.csv，不用手動編輯。

使用方式：
  1. 到證交所/公開資訊網下載最新的「上市ETF總覽」CSV
     (原始檔案通常是 Big5/CP950 編碼，欄位需含「股票代號」「ETF名稱」)。
  2. python refresh_active_etf_list.py 下載的檔案.csv

篩選邏輯：ETF名稱含「主動」視為主動式ETF (跟現有 00981A 等6檔的命名規則一致，
已用實際TWSE清單驗證過：所有代碼結尾為字母且名稱含「主動」的ETF，
跟「代碼結尾字母但不含主動」的槓桿/反向/債券ETF可以用名稱關鍵字正確區分)。

⚠️ 2026-08-24新增「啟用」欄位保留邏輯：現有的 ETF_data/active_etf_list.csv 裡
每一列多了一個「啟用」欄位(1=實際會被抓取、0=清單裡列著但先不抓，見
fetch_etf_holdings.py/etf_watchlist.py的說明)。這支工具改成「合併更新」而不是
整個覆蓋：既有代碼保留原本的「啟用」值不變；新出現的代碼(TWSE新上市的主動式ETF)
一律先設成「啟用=0」，避免一跑這支工具就自動把還沒驗證過能不能正常抓的新ETF
納入排程——想啟用哪一檔，之後手動把那一列的「啟用」改成1即可。
"""
import os
import sys
import pandas as pd

OUTPUT_PATH = "ETF_data/active_etf_list.csv"


def main():
    if len(sys.argv) < 2:
        print("用法: python refresh_active_etf_list.py <TWSE原始ETF清單.csv>")
        sys.exit(1)

    input_path = sys.argv[1]

    df = None
    for enc in ["utf-8-sig", "cp950", "big5", "gb18030"]:
        try:
            df = pd.read_csv(input_path, encoding=enc, dtype=str)
            print(f"使用編碼 {enc} 讀取成功")
            break
        except Exception:
            continue

    if df is None:
        print("無法用常見編碼(utf-8-sig/cp950/big5/gb18030)讀取此檔案，請檢查檔案格式。")
        sys.exit(1)

    df.columns = [c.strip() for c in df.columns]
    if "股票代號" not in df.columns or "ETF名稱" not in df.columns:
        print(f"檔案缺少必要欄位「股票代號」「ETF名稱」，目前欄位: {list(df.columns)}")
        sys.exit(1)

    active = df[df["ETF名稱"].astype(str).str.contains("主動", na=False)].copy()
    active = active[["股票代號", "ETF名稱"]]

    if active.empty:
        print("篩選後沒有任何主動式ETF，請確認來源檔案內容是否正確，未覆寫既有檔案。")
        sys.exit(1)

    # 讀取既有清單裡每個代碼原本的「啟用」值，合併更新時保留下來。
    existing_enabled = {}
    if os.path.exists(OUTPUT_PATH):
        try:
            existing_df = pd.read_csv(OUTPUT_PATH, encoding="utf-8-sig", dtype=str)
            existing_df.columns = [c.strip() for c in existing_df.columns]
            if "啟用" in existing_df.columns:
                for _, row in existing_df.iterrows():
                    code = str(row.get("股票代號", "")).strip()
                    if code:
                        existing_enabled[code] = str(row.get("啟用", "0")).strip()
        except Exception as e:
            print(f"讀取既有清單失敗(將視為全部是新代碼): {e}")

    active["啟用"] = active["股票代號"].apply(lambda c: existing_enabled.get(str(c).strip(), "0"))

    new_codes = [c for c in active["股票代號"] if str(c).strip() not in existing_enabled]

    os.makedirs(os.path.dirname(OUTPUT_PATH) or ".", exist_ok=True)
    active.to_csv(OUTPUT_PATH, index=False, encoding="utf-8-sig")
    print(f"已更新 {OUTPUT_PATH}，共 {len(active)} 檔主動式ETF：")
    print(active.to_string(index=False))
    if new_codes:
        print(f"\n新出現的代碼(已預設「啟用=0」，確認可以正常抓取後再手動改成1)：{new_codes}")


if __name__ == "__main__":
    main()
