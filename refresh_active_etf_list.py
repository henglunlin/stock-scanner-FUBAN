"""
refresh_active_etf_list.py
============================
維護工具：當 TWSE 有新的主動式ETF上市時，用這支腳本重新產生
Database/active_etf_list.csv，不用手動編輯。

使用方式：
  1. 到證交所/公開資訊網下載最新的「上市ETF總覽」CSV
     (原始檔案通常是 Big5/CP950 編碼，欄位需含「股票代號」「ETF名稱」)。
  2. python refresh_active_etf_list.py 下載的檔案.csv

篩選邏輯：ETF名稱含「主動」視為主動式ETF (跟現有 00981A 等6檔的命名規則一致，
已用實際TWSE清單驗證過：所有代碼結尾為字母且名稱含「主動」的ETF，
跟「代碼結尾字母但不含主動」的槓桿/反向/債券ETF可以用名稱關鍵字正確區分)。
"""
import sys
import pandas as pd

OUTPUT_PATH = "Database/active_etf_list.csv"


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

    active.to_csv(OUTPUT_PATH, index=False, encoding="utf-8-sig")
    print(f"已更新 {OUTPUT_PATH}，共 {len(active)} 檔主動式ETF：")
    print(active.to_string(index=False))


if __name__ == "__main__":
    main()
