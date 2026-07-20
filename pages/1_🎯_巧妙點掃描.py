# -*- coding: utf-8 -*-
"""
pages/1_🎯_巧妙點掃描.py
========================
獨立頁面：巧妙點 掃描條件
- 條件1：K線型態符合十字線家族 (實體極小，依據上下影線比例區分 十字/T字/倒T字)
- 條件2：今日成交量 < N日均量 × 門檻%（預設 10日、100%，可調整）
- 使用「獨立」的股票掃描清單檔案（不吃主頁面的 TWstocklistname2.txt / 分組清單）
"""

import os
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

import common_fubon as cf

# ===== 輔助函式：自動判別 .TW 或 .TWO =====
@st.cache_data(ttl=86400)
def load_code_to_ticker_map(filepath="TWstocklistname2.txt"):
    """
    從 TWstocklistname2.txt 載入純號碼對應到 .TW 或 .TWO 的查表字典
    """
    mapping = {}
    if os.path.exists(filepath):
        with open(filepath, "r", encoding="utf-8-sig", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line: continue
                parts = line.split()
                if parts:
                    ticker = parts[0].strip().upper()
                    if "." in ticker:
                        code = ticker.split(".")[0]
                        mapping[code] = ticker
    return mapping

def symbol_to_code(symbol: str) -> str:
    return str(symbol).split(".")[0]

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

def build_yfinance_candidates(symbol: str, code_map: dict = None):
    code_map = code_map or {}
    raw = str(symbol).strip().upper()
    code = symbol_to_code(raw)

    # 已經帶明確後綴（.TW / .TWO）就直接用，不要再猜其他後綴，
    # 避免整批清單被平白多灌近一倍不存在的假代碼，拖垮單次 yf.download() 批次請求。
    if "." in raw:
        return [raw]

    # 純數字、沒有後綴：優先查對照表
    if code in code_map:
        return [code_map[code]]

    # 對照表查不到，才需要用猜的，依序嘗試 .TW / .TWO
    candidates = []
    normalized = normalize_symbol_quick(raw)
    if normalized:
        candidates.append(normalized)
    if code:
        for suffix in (".TW", ".TWO"):
            cand = f"{code}{suffix}"
            if cand not in candidates:
                candidates.append(cand)

    result, seen = [], set()
    for item in candidates:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result

def parse_raw_symbols(text: str) -> list:
    """
    寬鬆解析股票代碼：
    - 允許純數字
    - 以換行符號或逗號分割
    - 遇到空格或 Tab 自動截斷（支援 "2330 台積電" 格式）
    """
    symbols = []
    if not text:
        return symbols
    
    # 將全形空白、逗號統一替換為換行符號
    text = text.replace("\ufeff", "").replace("\u3000", " ").replace(",", "\n")
    
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        # 切割並取第一個元素作為代碼
        raw_code = line.split()[0].upper()
        if raw_code and raw_code not in symbols:
            symbols.append(raw_code)
    return symbols
# ==========================================

# ===== 頁面基本設定 =====
st.set_page_config(page_title="巧妙點掃描", layout="wide")

# ===== 這個頁面專屬的常數 =====
QIAOMIAO_STOCK_FILE = "TWstocklistname_QiaoMiaoDian.txt"

# ===== session_state 初始化 =====
cf.ensure_fubon_session_state()
if "qmd_scan_enabled" not in st.session_state:
    st.session_state.qmd_scan_enabled = False
if "qmd_scan_requested" not in st.session_state:
    st.session_state.qmd_scan_requested = False
if "qmd_last_scan_result" not in st.session_state:
    st.session_state.qmd_last_scan_result = None
if "qmd_manual_symbols_text" not in st.session_state:
    st.session_state.qmd_manual_symbols_text = ""

st.title("🎯 巧妙點掃描")
st.caption("條件１：實體佔全距(高-低)比例極小，且具有長影線特徵。條件２：今日成交量 < N日均量 × 門檻%。兩條件同時成立才算命中。")

# ===== 側邊欄：富邦連線 / 資料來源 =====
cf.render_fubon_login_sidebar()
tw_now = datetime.now(ZoneInfo("Asia/Taipei"))
active_price_source = cf.render_price_source_selector_sidebar(tw_now)

# ===== 側邊欄：巧妙點掃描條件 =====
with st.sidebar.expander("⚙️ 巧妙點掃描條件", expanded=True):
    body_threshold = st.number_input(
        "條件１：實體佔全距上限 (%)",
        min_value=0.0, max_value=90.0, value=60.0, step=5.0, format="%.1f",
        help="實體(|收-開|) 佔 全日振幅(高-低) 的百分比上限。預設 < 10% 視為十字線家族。",
    )
    shadow_threshold = st.number_input(
        "條件１：長影線最低門檻 (%)",
        min_value=0.0, max_value=100.0, value=60.0, step=5.0, format="%.1f",
        help="單邊影線長度需佔全日振幅大於此門檻，才符合 T字線 或 倒T字線 特徵。",
    )
    vol_ma_days = st.number_input(
        "均量天數 N（日）",
        min_value=2, max_value=60, value=10, step=1,
        help="計算成交量移動平均的天數，預設 10 日。",
    )
    vol_ratio_threshold = st.number_input(
        f"條件２：成交量 / {int(vol_ma_days)}日均量 上限 (%)",
        min_value=0.0, max_value=500.0, value=100.0, step=5.0, format="%.1f",
        help="今日成交量需小於「N日均量 × 此百分比」才算命中，預設 100%（即小於均量）。",
    )
    vol_ma_include_today = st.checkbox(
        f"{int(vol_ma_days)}日均量計算是否包含當日",
        value=False,
        help="勾選＝均量取「最近N日（含今日）」；不勾＝均量取「今日以前最近N日」，今日量能拿來跟過去基準比較（預設）。",
    )
    min_volume_lots = st.number_input(
        "最低成交量下限 (張)，過濾冷門股",
        min_value=0, value=0, step=50,
        help="0 表示不過濾。可用來排除成交量太小、不具參考意義的個股。",
    )

# ===== 側邊欄：獨立股票清單來源 =====
with st.sidebar.expander("📋 巧妙點股票清單", expanded=True):
    list_source_mode = st.radio(
        "清單來源",
        options=["預設清單檔案", "上傳清單檔案", "手動輸入代碼"],
        index=0,
        key="qmd_list_source_mode",
        help="巧妙點掃描使用獨立清單，不會套用主頁面的分組或全市場清單。",
    )

    uploaded_symbols = None
    if list_source_mode == "預設清單檔案":
        st.caption(f"讀取檔案：`{QIAOMIAO_STOCK_FILE}`")
        if not os.path.exists(QIAOMIAO_STOCK_FILE):
            st.warning(f"尚未找到 {QIAOMIAO_STOCK_FILE}，請改用「上傳清單檔案」或「手動輸入代碼」。")
    elif list_source_mode == "上傳清單檔案":
        upload_file = st.file_uploader("上傳巧妙點股票清單 (.txt)", type=["txt"], key="qmd_upload_file")
        if upload_file is not None:
            content = upload_file.read().decode("utf-8-sig", errors="ignore")
            uploaded_symbols = parse_raw_symbols(content)
            st.success(f"已解析 {len(uploaded_symbols)} 檔股票代碼")
    else:
        st.session_state.qmd_manual_symbols_text = st.text_area(
            "手動輸入代碼（每行一檔，或用逗號分隔）",
            value=st.session_state.qmd_manual_symbols_text,
            height=140,
            placeholder="2330\n2317\n6488",
        )

# ===== 解析出這次掃描要用的股票清單 =====
if list_source_mode == "預設清單檔案":
    if os.path.exists(QIAOMIAO_STOCK_FILE):
        with open(QIAOMIAO_STOCK_FILE, "r", encoding="utf-8-sig", errors="ignore") as f:
            scan_symbols = parse_raw_symbols(f.read())
    else:
        scan_symbols = []
elif list_source_mode == "上傳清單檔案":
    scan_symbols = uploaded_symbols or []
else:
    scan_symbols = parse_raw_symbols(st.session_state.qmd_manual_symbols_text)

st.caption(
    f"更新時間：{tw_now.strftime('%Y-%m-%d %H:%M:%S')}｜價格來源：{active_price_source}｜"
    f"清單來源：{list_source_mode}｜清單股票數：{len(scan_symbols)}"
)

# ===== 掃描按鈕 =====
btn_col1, btn_col2, toggle_col, spacer_col = st.columns([0.9, 0.9, 1.4, 3.8])
with btn_col1:
    if st.button("▶️ 開始掃描", use_container_width=True, disabled=st.session_state.qmd_scan_enabled, key="qmd_start_btn"):
        st.session_state.qmd_scan_enabled = True
        st.session_state.qmd_scan_requested = True
        st.cache_data.clear()
        st.rerun()
with btn_col2:
    if st.button("⏹️ 停止掃描", use_container_width=True, disabled=not st.session_state.qmd_scan_enabled, key="qmd_stop_btn"):
        st.session_state.qmd_scan_enabled = False
        st.session_state.qmd_scan_requested = False
        st.rerun()
with toggle_col:
    show_only_hits = st.toggle("只顯示命中巧妙點的股票", value=True, key="qmd_show_only_hits")

if st.session_state.qmd_scan_enabled:
    st.caption("🟢 掃描狀態：執行中")
elif st.session_state.qmd_last_scan_result:
    st.caption(f"✅ 掃描狀態：已完成，上次完成時間：{st.session_state.qmd_last_scan_result.get('scan_completed_at', '-')}")
else:
    st.caption("⚪ 掃描狀態：已停止，按「開始掃描」才會抓取資料。")

# ===== 前置檢查 =====
if not scan_symbols:
    st.info("目前清單內沒有股票代碼，請先設定「巧妙點股票清單」。")
    st.stop()

if active_price_source == "WebSocket" and not st.session_state.fubon_logged_in:
    st.warning("⚠️ 目前價格來源為 WebSocket，請先至左側面板連線「富邦 API」，才能開始抓取行情資料。")
    st.stop()
if active_price_source == "Yfinance" and cf.yf is None:
    st.warning("⚠️ 目前價格來源為 Yfinance，請先安裝套件：pip install yfinance")
    st.stop()

should_run_scan = bool(st.session_state.pop("qmd_scan_requested", False))
has_last_result = st.session_state.qmd_last_scan_result is not None

progress_placeholder = st.empty()

if not should_run_scan and not has_last_result:
    st.info("請按「開始掃描」開始抓取股票資料。")
    st.stop()

# ===== 掃描主邏輯 =====
if should_run_scan:
    scan_today_str = tw_now.strftime("%Y-%m-%d")
    
    unique_raw_symbols = tuple(sorted(set(scan_symbols)))

    # 載入股票代號與後綴的對應表
    code_map = load_code_to_ticker_map("TWstocklistname2.txt")

    # 收集所有可能的 yfinance 代碼，確保批次下載時格式正確 (.TW / .TWO)
    all_candidates = []
    for original_symbol in unique_raw_symbols:
        all_candidates.extend(build_yfinance_candidates(original_symbol, code_map))
    all_unique_candidates = tuple(sorted(set(all_candidates)))

    # 🚀 批次預先抓取：把整批股票的Yfinance歷史資料一次抓回來，取代掃描迴圈中逐檔各打一次API。
    if cf.yf is not None:
        yf_history_map = cf.bulk_download_yfinance_history(all_unique_candidates, scan_today_str)
        yf_today_map = (
            cf.bulk_download_yfinance_today(all_unique_candidates, scan_today_str)
            if active_price_source == "Yfinance" else {}
        )
    else:
        yf_history_map = {}
        yf_today_map = {}

    total_count = len(unique_raw_symbols)
    progress_bar = progress_placeholder.progress(0, text=f"掃描進度：0.0%（準備掃描 {total_count} 檔股票）")

    rows = []
    hit_rows = []
    error_count = 0
    need_days = int(vol_ma_days) + (0 if vol_ma_include_today else 1)

    for idx, original_symbol in enumerate(unique_raw_symbols, start=1):
        if not st.session_state.qmd_scan_enabled:
            progress_placeholder.empty()
            st.warning("掃描已停止。")
            st.stop()

        progress_value = min(idx / total_count, 1.0) if total_count else 1.0
        progress_bar.progress(progress_value, text=f"掃描進度：{progress_value*100:.1f}%（{idx}/{total_count}：{original_symbol}）")

        try:
            candidates = build_yfinance_candidates(original_symbol, code_map)
            df = pd.DataFrame()
            valid_symbol = original_symbol
            
            for candidate in candidates:
                try:
                    temp_df = cf.download_stock_data_by_source(
                        candidate, st.session_state.fubon_sdk, active_price_source, scan_today_str,
                        history_map=yf_history_map, yf_today_map=yf_today_map,
                    )
                    temp_df = cf.normalize_ohlc(temp_df)
                    if not temp_df.empty and len(temp_df) >= need_days + 1:
                        df = temp_df
                        valid_symbol = candidate
                        break  
                except Exception:
                    continue

            if df.empty or len(df) < need_days + 1:
                raise ValueError("歷史資料不足，無法計算均量")

            # --- 1. 取得 OHLC 資料 ---
            open_price = df["Open"].iloc[-1]
            if pd.isna(open_price) or open_price == 0:
                raise ValueError("今日尚無有效開盤資料")
            open_price = float(open_price)

            price = cf.get_last_price_by_source(valid_symbol, df, st.session_state.fubon_sdk, active_price_source)
            high_price = float(df["High"].iloc[-1])
            low_price = float(df["Low"].iloc[-1])
            
            stock_name = cf.get_stock_name(valid_symbol, st.session_state.fubon_sdk)

            # --- 取得量能資料 ---
            volume_series = pd.to_numeric(df["Volume"], errors="coerce")
            today_volume = float(volume_series.iloc[-1]) if pd.notna(volume_series.iloc[-1]) else 0.0

            if vol_ma_include_today:
                vol_ma_window = volume_series.tail(int(vol_ma_days))
            else:
                vol_ma_window = volume_series.iloc[-(int(vol_ma_days) + 1):-1]

            if vol_ma_window.empty or vol_ma_window.isna().all():
                raise ValueError("均量資料不足")
            vol_ma = float(vol_ma_window.mean())
            if vol_ma <= 0:
                raise ValueError("均量資料異常")
                
            vol_ratio_pct = today_volume / vol_ma * 100
            today_volume_lots = today_volume / 1000
            vol_ma_lots = vol_ma / 1000

            # --- 2. 計算全距與各部位絕對長度 ---
            k_range = high_price - low_price
            body_size = abs(price - open_price)
            upper_shadow = high_price - max(open_price, price)
            lower_shadow = min(open_price, price) - low_price

            # --- 3. 計算佔比 (%) ---
            if k_range > 0:
                body_ratio_pct = (body_size / k_range) * 100
                upper_shadow_pct = (upper_shadow / k_range) * 100
                lower_shadow_pct = (lower_shadow / k_range) * 100
            else:
                body_ratio_pct = upper_shadow_pct = lower_shadow_pct = 0.0

            # --- 4. 判斷 K 線型態 ---
            k_pattern = "-"
            if body_ratio_pct <= body_threshold:
                if lower_shadow_pct >= shadow_threshold and upper_shadow_pct <= body_threshold:
                    k_pattern = "T字線"
                elif upper_shadow_pct >= shadow_threshold and lower_shadow_pct <= body_threshold:
                    k_pattern = "倒T字線"
                else:
                    k_pattern = "十字線"

            # --- 5. 綜合量能條件判定 ---
            passes_k_pattern = (k_pattern != "-")
            passes_vol = (vol_ratio_pct < vol_ratio_threshold)
            passes_min_volume = (today_volume_lots >= float(min_volume_lots))
            
            is_hit = passes_k_pattern and passes_vol and passes_min_volume

            row = {
                "代碼": valid_symbol,
                "代碼網址": cf.yahoo_quote_url(valid_symbol),
                "股票名稱": stock_name,
                "開盤": round(open_price, 2),
                "現價": round(price, 2),
                "型態": k_pattern,
                "實體佔比%": round(body_ratio_pct, 1),
                "上影佔比%": round(upper_shadow_pct, 1),
                "下影佔比%": round(lower_shadow_pct, 1),
                "成交量(張)": round(today_volume_lots, 1),
                f"{int(vol_ma_days)}日均量(張)": round(vol_ma_lots, 1),
                "量比%": round(vol_ratio_pct, 1),
                "是否命中巧妙點": "✅ 是" if is_hit else "否",
                "來源": active_price_source,
            }
            
            if (not show_only_hits) or is_hit:
                rows.append(row)
            if is_hit:
                hit_rows.append(row)
        except Exception as e:
            error_count += 1
            if not show_only_hits:
                rows.append({
                    "代碼": original_symbol, "代碼網址": "", "股票名稱": cf.get_stock_name(original_symbol, st.session_state.fubon_sdk),
                    "開盤": "-", "現價": "錯誤", "型態": "-", "實體佔比%": "-", "上影佔比%": "-", "下影佔比%": "-", 
                    "成交量(張)": "-", f"{int(vol_ma_days)}日均量(張)": "-", "量比%": "-",
                    "是否命中巧妙點": str(e), "來源": active_price_source,
                })

    progress_placeholder.empty()
    st.session_state.qmd_scan_enabled = False
    st.session_state.qmd_last_scan_result = {
        "rows": rows,
        "hit_rows": hit_rows,
        "error_count": error_count,
        "scan_completed_at": tw_now.strftime("%Y-%m-%d %H:%M:%S"),
        "body_threshold": body_threshold,
        "shadow_threshold": shadow_threshold,
        "vol_ratio_threshold": vol_ratio_threshold,
        "vol_ma_days": int(vol_ma_days),
        "excel_filename": f"巧妙點_scan_{tw_now.strftime('%Y%m%d_%H%M')}.xlsx",
    }

last_result = st.session_state.qmd_last_scan_result or {}
rows = last_result.get("rows", [])
hit_rows = last_result.get("hit_rows", [])
error_count = last_result.get("error_count", 0)

# ===== 結果輸出（Excel / Telegram）=====
def build_qiaomiao_excel_bytes(hit_rows_local):
    from io import BytesIO
    columns = ["代碼", "股票名稱", "開盤", "現價", "型態", "實體佔比%", "上影佔比%", "下影佔比%", "成交量(張)", "量比%", "是否命中巧妙點", "來源"]
    df = pd.DataFrame(hit_rows_local)
    if df.empty:
        df = pd.DataFrame(columns=columns)
    else:
        keep_cols = [c for c in df.columns if c not in ("代碼網址",) and (c in columns or c.endswith("日均量(張)"))]
        df = df[keep_cols]
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="巧妙點命中清單", index=False)
        cf.apply_excel_fonts(writer.book)
    output.seek(0)
    return output.getvalue()

excel_bytes = build_qiaomiao_excel_bytes(hit_rows)
excel_filename = last_result.get("excel_filename", f"QiaoMiaoDian_scan_{tw_now.strftime('%Y%m%d_%H%M%S')}.xlsx")

action_col1, action_col2 = st.columns(2)
with action_col1:
    st.download_button(
        "下載命中清單 Excel", data=excel_bytes, file_name=excel_filename,
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True, key="qmd_download_excel_btn",
    )
with action_col2:
    if st.button("推送命中清單到 Telegram", use_container_width=True, key="qmd_push_tg_btn"):
        ok = cf.send_telegram_document(
            excel_bytes, excel_filename,
            caption=f"巧妙點掃描結果｜實體佔比<{last_result.get('body_threshold', body_threshold)}%｜"
                    f"量比<{last_result.get('vol_ratio_threshold', vol_ratio_threshold)}%｜{tw_now.strftime('%Y-%m-%d %H:%M:%S')}",
        )
        if ok:
            st.success("已將 Excel 推送到 Telegram。")

st.markdown("### 🔎 掃描結果")
m1, m2, m3 = st.columns(3)
m1.metric("命中巧妙點檔數", len(hit_rows))
m2.metric("清單股票總數", len(scan_symbols))
m3.metric("抓取失敗檔數", error_count)

display_columns = ["代碼", "股票名稱", "開盤", "現價", "型態", "實體佔比%", "上影佔比%", "下影佔比%", "成交量(張)",
                    f"{last_result.get('vol_ma_days', int(vol_ma_days))}日均量(張)", "量比%", "是否命中巧妙點", "來源"]

if rows:
    result_df = pd.DataFrame(rows)
    display_df = result_df.copy()
    if "代碼網址" in display_df.columns:
        display_df["代碼"] = display_df["代碼網址"].where(display_df["代碼網址"] != "", display_df["代碼"])
    for col in display_columns:
        if col not in display_df.columns:
            display_df[col] = "-"
    if "成交量(張)" in display_df.columns:
        display_df["成交量(張)"] = display_df["成交量(張)"].apply(cf.format_volume)
    vol_ma_col = f"{last_result.get('vol_ma_days', int(vol_ma_days))}日均量(張)"
    if vol_ma_col in display_df.columns:
        display_df[vol_ma_col] = display_df[vol_ma_col].apply(cf.format_volume)

    st.dataframe(
        display_df[display_columns], use_container_width=True,
        column_config={
            "代碼": st.column_config.LinkColumn("代碼", help="點擊前往台股 Yahoo", display_text=r"https://tw.stock.yahoo.com/quote/(.*)"),
            "股票名稱": st.column_config.TextColumn("股票名稱"),
        },
    )
else:
    st.info("目前沒有符合條件的股票（或清單抓取皆失敗）。")

with st.expander("ℹ️ 巧妙點條件說明"):
    st.markdown(
        f"""
- **條件１（價格與型態）**：
    - 計算全日振幅 `(最高價 - 最低價)`。
    - 實體佔比：`|收盤價 - 開盤價| ÷ 振幅 × 100%` 需小於 **{last_result.get('body_threshold', body_threshold)}%**。
    - 上/下影線佔比依據長短判斷為 **十字線**、**T字線**（下影線大於 {last_result.get('shadow_threshold', shadow_threshold)}%）、**倒T字線**（上影線大於 {last_result.get('shadow_threshold', shadow_threshold)}%）。
- **條件２（量能）**：今日成交量 `÷ {last_result.get('vol_ma_days', int(vol_ma_days))}日均量 × 100%` 需小於 **{last_result.get('vol_ratio_threshold', vol_ratio_threshold)}%**。
- 兩條件需同時成立才算「命中巧妙點」。
- 股票清單為**獨立清單**（`{QIAOMIAO_STOCK_FILE}` 或使用者自行上傳/輸入），不會套用主頁面的分組或全市場清單。
        """
    )
