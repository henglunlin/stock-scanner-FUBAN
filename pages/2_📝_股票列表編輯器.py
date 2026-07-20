# -*- coding: utf-8 -*-
"""
pages/2_📝_股票列表編輯器.py
============================
獨立頁面：股票列表編輯器
1. 讀取 Excel (.xlsx) 或 純文字 (.txt) 股票清單，支援一次上傳多個檔案並合併
2. 去重化（依代碼）
3. 60MA 篩選開關：預設關閉，關閉時完全不會打 API；打開後才會抓 yfinance 資料，
   篩選出「站上 60MA」或「跌破 60MA」的股票
4. 可編輯表格（勾選要保留的股票），篩選/編輯完成後可下載成 .txt / .xlsx，
   拿去其他頁面（例如「巧妙點掃描」）的「上傳清單檔案」使用
"""

import os
from datetime import datetime
from io import BytesIO
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

import common_fubon as cf

# ===== 頁面基本設定 =====
st.set_page_config(page_title="股票列表編輯器", layout="wide")

CODE_MAP_FILE = "TWstocklistname2.txt"   # 用來把純數字代碼轉換成正確的 .TW / .TWO 後綴
MA60_HISTORY_TTL_SEC = 60 * 60           # 60MA 歷史資料快取 1 小時，避免重複打 API

st.title("📝 股票列表編輯器")
st.caption("上傳 Excel / txt 股票清單 → 合併去重 → （可選）60MA 篩選 → 編輯後匯出，供其他掃描頁面使用。")

tw_now = datetime.now(ZoneInfo("Asia/Taipei"))

# ===== session_state 初始化 =====
if "sle_merged_df" not in st.session_state:
    st.session_state.sle_merged_df = None
if "sle_ma60_applied" not in st.session_state:
    st.session_state.sle_ma60_applied = False


# ===== 檔案解析工具 =====
def parse_excel_file(file_obj, code_map: dict) -> pd.DataFrame:
    """解析 Excel 清單，嘗試辨識常見欄位名稱（代碼／商品／名稱...）"""
    df = pd.read_excel(file_obj)
    df.columns = [str(c).strip() for c in df.columns]

    code_col = next((c for c in ["代碼", "股票代碼", "代號", "Code", "code"] if c in df.columns), None)
    name_col = next((c for c in ["商品", "股票名稱", "名稱", "商品名稱", "Name", "name"] if c in df.columns), None)
    if code_col is None:
        raise ValueError("Excel 檔案裡找不到「代碼」欄位（支援：代碼／股票代碼／代號／Code）")

    out_rows = []
    for _, r in df.iterrows():
        raw_code = str(r[code_col]).strip()
        if raw_code.endswith(".0"):  # pandas 把整數欄讀成 float 常見的尾巴
            raw_code = raw_code[:-2]
        if not raw_code or raw_code.lower() == "nan":
            continue
        full_ticker = cf.resolve_ticker_suffix(raw_code, code_map)
        name = str(r[name_col]).strip() if name_col else ""
        extra = {}
        for c in ["成交", "漲幅%", "總量", "昨收"]:
            if c in df.columns and pd.notna(r.get(c)):
                extra[c] = r[c]
        out_rows.append({"代碼": full_ticker, "股票名稱": name, **extra})
    return pd.DataFrame(out_rows)


def parse_txt_file(file_obj, code_map: dict) -> pd.DataFrame:
    content = file_obj.read().decode("utf-8-sig", errors="ignore")
    out_rows = []
    for raw_line in content.splitlines():
        line = raw_line.strip().replace("\u3000", "")
        if not line:
            continue
        parts = line.split("\t") if "\t" in line else line.split(None, 1)
        raw_code = parts[0].strip()
        name = parts[1].strip() if len(parts) > 1 else ""
        full_ticker = cf.resolve_ticker_suffix(raw_code, code_map)
        out_rows.append({"代碼": full_ticker, "股票名稱": name})
    return pd.DataFrame(out_rows)


# ===== 上傳區 =====
uploaded_files = st.file_uploader(
    "上傳股票清單檔案（可一次選多個，支援 .xlsx / .txt）",
    type=["xlsx", "xls", "txt"],
    accept_multiple_files=True,
)

col_load, col_dedupe = st.columns([1, 1])
with col_load:
    load_btn = st.button("📥 讀取並合併清單", use_container_width=True, disabled=not uploaded_files)
with col_dedupe:
    auto_dedupe = st.checkbox("自動去重化（依代碼，保留第一次出現的資料）", value=True)

if load_btn and uploaded_files:
    code_map = cf.load_code_to_ticker_map(CODE_MAP_FILE)
    frames = []
    parse_errors = []
    for f in uploaded_files:
        try:
            if f.name.lower().endswith((".xlsx", ".xls")):
                part = parse_excel_file(f, code_map)
            else:
                part = parse_txt_file(f, code_map)
            part["來源檔案"] = f.name
            frames.append(part)
        except Exception as e:
            parse_errors.append(f"{f.name}: {e}")

    if parse_errors:
        for err in parse_errors:
            st.error(f"讀取失敗：{err}")

    if frames:
        merged = pd.concat(frames, ignore_index=True)
        merged = merged[merged["代碼"].astype(str).str.strip() != ""]
        total_before = len(merged)

        if auto_dedupe:
            merged = merged.drop_duplicates(subset=["代碼"], keep="first").reset_index(drop=True)
        dup_removed = total_before - len(merged)

        merged.insert(0, "保留", True)
        st.session_state.sle_merged_df = merged
        st.session_state.sle_ma60_applied = False
        st.success(f"已合併 {len(uploaded_files)} 個檔案，共 {total_before} 筆，去重後剩 {len(merged)} 筆（移除 {dup_removed} 筆重複）。")

if st.session_state.sle_merged_df is None:
    st.info("請先上傳清單檔案並按「讀取並合併清單」。")
    st.stop()

# ===== 60MA 篩選（開關預設關閉，關閉時完全不打 API）=====
with st.expander("📈 60MA 篩選（開關預設關閉，開啟才會抓取資料）", expanded=True):
    enable_ma60_filter = st.toggle(
        "啟用 60日均線（60MA）篩選",
        value=False,
        key="sle_enable_ma60",
        help="關閉時完全不會呼叫 yfinance，只做清單合併/去重/手動編輯。開啟後才會抓歷史資料計算 60MA。",
    )
    ma60_direction = st.radio(
        "篩選方向",
        options=["站上 60MA（現價 ≥ 60MA）", "跌破 60MA（現價 ＜ 60MA）"],
        index=0,
        disabled=not enable_ma60_filter,
        key="sle_ma60_direction",
    )
    apply_ma60_btn = st.button(
        "🔄 抓取資料並套用 60MA 篩選",
        disabled=not enable_ma60_filter,
        use_container_width=True,
    )


@st.cache_data(ttl=MA60_HISTORY_TTL_SEC)
def bulk_fetch_for_ma60(symbols: tuple, today_str: str):
    """抓 6 個月資料確保有 >=60 個交易日可算 60MA。
    回傳 (per_symbol_dict, debug_info)，debug_info 用來在畫面上顯示實際發生的錯誤，
    避免整批失敗時完全看不出原因。"""
    debug_info = {"yfinance_version": getattr(cf.yf, "__version__", "未知") if cf.yf else None}
    if cf.yf is None or not symbols:
        debug_info["error"] = "yfinance 套件未安裝或清單為空"
        return {}, debug_info
    try:
        raw = cf.yf.download(
            tickers=list(symbols), period="6mo", interval="1d",
            auto_adjust=False, group_by="ticker", threads=False, progress=False,
        )
        debug_info["raw_empty"] = raw is None or raw.empty
        debug_info["raw_shape"] = None if raw is None else tuple(raw.shape)
    except Exception as e:
        debug_info["error"] = f"{type(e).__name__}: {e}"
        return {s: pd.DataFrame() for s in symbols}, debug_info
    per_symbol = cf._split_yfinance_bulk_result(raw, symbols)
    return per_symbol, debug_info


if apply_ma60_btn and enable_ma60_filter:
    if cf.yf is None:
        st.warning("⚠️ 尚未安裝 yfinance 套件：pip install yfinance")
    else:
        symbols = tuple(sorted(set(st.session_state.sle_merged_df["代碼"].astype(str))))
        today_str = tw_now.strftime("%Y-%m-%d")

        progress_placeholder = st.empty()
        progress_bar = progress_placeholder.progress(0, text=f"抓取 60MA 資料中：0/{len(symbols)}")
        history_map, debug_info = bulk_fetch_for_ma60(symbols, today_str)

        if debug_info.get("error"):
            st.error(f"⚠️ 抓取 yfinance 資料時發生錯誤，這通常代表 Yahoo Finance 暫時擋掉/限流了這次請求，"
                      f"或 requirements.txt 裡的 yfinance 版本太舊需要更新：\n\n`{debug_info['error']}`")
        elif debug_info.get("raw_empty"):
            st.error("⚠️ yfinance 這次回傳完全是空的資料（沒有拋出例外），最常見原因是 Yahoo Finance 對目前這個雲端環境的 IP 做了限流／封鎖。"
                      "可以稍等幾分鐘後重試，或改用本機/其他主機執行看看是否正常。")
        with st.expander("🔧 除錯資訊（回報問題時可以附上這段）", expanded=bool(debug_info.get("error") or debug_info.get("raw_empty"))):
            st.json(debug_info)

        bulk_all_empty = all(
            (df is None or df.empty) for df in history_map.values()
        ) if history_map else True

        if bulk_all_empty and symbols:
            st.warning("批次下載沒有拿到任何資料，改用逐檔下載重試中（較慢，請耐心等候）...")
            fallback_map = {}
            fallback_bar = st.progress(0, text="逐檔下載中：0/%d" % len(symbols))
            for i, sym in enumerate(symbols, start=1):
                try:
                    d = cf.yf.download(sym, period="6mo", interval="1d", auto_adjust=False, progress=False, threads=False)
                    fallback_map[sym] = cf._normalize_yfinance_ohlcv(d)
                except Exception:
                    fallback_map[sym] = pd.DataFrame()
                fallback_bar.progress(i / len(symbols), text=f"逐檔下載中：{i}/{len(symbols)}（{sym}）")
            fallback_bar.empty()
            history_map = fallback_map
            still_all_empty = all((df is None or df.empty) for df in history_map.values())
            if still_all_empty:
                st.error("逐檔下載仍然全部失敗，確定是 Yahoo Finance 目前封鎖/限流了這個環境的請求，"
                          "而不是程式邏輯問題。建議稍後再試，或檢查 requirements.txt 裡 yfinance 是否為最新版本。")

        results = []
        for idx, symbol in enumerate(symbols, start=1):
            progress_bar.progress(idx / len(symbols), text=f"計算 60MA 中：{idx}/{len(symbols)}（{symbol}）")
            df = history_map.get(symbol)
            df = cf.normalize_ohlc(df) if df is not None else pd.DataFrame()
            if df.empty or len(df) < 60:
                results.append({"代碼": symbol, "現價": None, "60MA": None, "現價/60MA%": None, "60MA狀態": "資料不足"})
                continue
            close = pd.to_numeric(df["Close"], errors="coerce")
            ma60 = float(close.tail(60).mean())
            price = float(close.iloc[-1])
            if ma60 <= 0:
                results.append({"代碼": symbol, "現價": price, "60MA": None, "現價/60MA%": None, "60MA狀態": "資料異常"})
                continue
            status = "站上60MA" if price >= ma60 else "跌破60MA"
            results.append({
                "代碼": symbol, "現價": round(price, 2), "60MA": round(ma60, 2),
                "現價/60MA%": round(price / ma60 * 100, 1), "60MA狀態": status,
            })
        progress_placeholder.empty()

        ma60_df = pd.DataFrame(results)
        base_df = st.session_state.sle_merged_df.drop(
            columns=[c for c in ["現價", "60MA", "現價/60MA%", "60MA狀態"] if c in st.session_state.sle_merged_df.columns]
        )
        merged_with_ma60 = base_df.merge(ma60_df, on="代碼", how="left")

        want_above = ma60_direction.startswith("站上")
        target_status = "站上60MA" if want_above else "跌破60MA"
        merged_with_ma60["保留"] = merged_with_ma60["60MA狀態"] == target_status

        st.session_state.sle_merged_df = merged_with_ma60
        st.session_state.sle_ma60_applied = True
        hit_count = int((merged_with_ma60["60MA狀態"] == target_status).sum())
        st.success(f"60MA 篩選完成：符合「{target_status}」的有 {hit_count} 檔（已自動勾選「保留」，可再手動調整）。")

# ===== 可編輯表格 =====
st.markdown("### ✏️ 編輯清單（可勾選「保留」，或直接刪除整列）")
edited_df = st.data_editor(
    st.session_state.sle_merged_df,
    use_container_width=True,
    num_rows="dynamic",
    key="sle_data_editor",
    column_config={
        "保留": st.column_config.CheckboxColumn("保留", help="取消勾選 = 匯出時排除此檔"),
        "代碼": st.column_config.TextColumn("代碼", disabled=True),
    },
)
st.session_state.sle_merged_df = edited_df

final_df = edited_df[edited_df.get("保留", True) == True].drop(columns=["保留"], errors="ignore").reset_index(drop=True)

m1, m2, m3 = st.columns(3)
m1.metric("清單總筆數", len(edited_df))
m2.metric("目前保留（將匯出）", len(final_df))
m3.metric("60MA 篩選狀態", "已套用" if st.session_state.sle_ma60_applied else "未啟用")

st.markdown("### 📋 匯出後清單預覽")
st.dataframe(final_df, use_container_width=True)


# ===== 匯出（txt / xlsx）=====
def build_export_txt(df: pd.DataFrame) -> bytes:
    lines = []
    for _, r in df.iterrows():
        code = str(r["代碼"]).strip()
        name = str(r.get("股票名稱", "")).strip()
        lines.append(f"{code}\t{name}" if name else code)
    return "\n".join(lines).encode("utf-8-sig")


def build_export_xlsx(df: pd.DataFrame) -> bytes:
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="股票清單", index=False)
        cf.apply_excel_fonts(writer.book)
    output.seek(0)
    return output.getvalue()


export_col1, export_col2 = st.columns(2)
with export_col1:
    st.download_button(
        "下載 .txt（可用於其他頁面「上傳清單檔案」）",
        data=build_export_txt(final_df),
        file_name=f"stocklist_{tw_now.strftime('%Y%m%d_%H%M')}.txt",
        mime="text/plain",
        use_container_width=True,
        disabled=final_df.empty,
    )
with export_col2:
    st.download_button(
        "下載 .xlsx",
        data=build_export_xlsx(final_df),
        file_name=f"stocklist_{tw_now.strftime('%Y%m%d_%H%M')}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
        disabled=final_df.empty,
    )

with st.expander("ℹ️ 使用說明"):
    st.markdown(
        f"""
- **多檔合併**：可一次上傳多個 Excel / txt，會自動合併成一份清單。
- **去重化**：依「代碼」去重，預設保留第一次出現的資料，可關閉。
- **60MA 篩選開關**：預設關閉，關閉時完全不會呼叫 yfinance；打開後按「抓取資料並套用」才會抓取近 6 個月資料計算 60 日均線，
  篩選「站上 60MA」或「跌破 60MA」的股票（資料不足 60 個交易日的股票會標記「資料不足」，不列入篩選結果）。
- **手動編輯**：下方表格可直接勾選/取消「保留」，或用列尾的刪除鈕整列刪除、最下方新增空白列手動輸入。
- **匯出**：完成篩選/編輯後，用「代碼<TAB>股票名稱」的格式匯出 .txt，可直接拿到「巧妙點掃描」等頁面的「上傳清單檔案」使用；也可下載 .xlsx 備份。
- 代碼對照表（純數字轉 .TW/.TWO）讀取自 `{CODE_MAP_FILE}`，找不到時才會用猜測（數字開頭 3/6/8 猜 .TWO，其餘猜 .TW）。
        """
    )
