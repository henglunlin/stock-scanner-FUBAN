"""
scoring.py
=======
二階段過濾：訊號品質評分 / 優先追蹤排序 / 訊號追蹤紀錄。
※ 改版重點：原本綁在日/週KD、MACD、趨勢突破上的評分邏輯，
   已隨著這些訊號被移除而拿掉，改成依「本次觸發的訊號清單(含買/賣方向)」評分。
   下面的 SIGNAL_BASE_WEIGHT 可依實際回測結果自行調整每個訊號的權重，
   不影響其他評分邏輯，也不需要更動主程式。
"""

import os

import pandas as pd
import streamlit as st

# ===== 二階段過濾 / 追蹤 Database 設定 =====
LOCAL_DATABASE_DIR = st.secrets.get("LOCAL_DATABASE_DIR", "Database")
TRACKING_FILE = os.path.join(LOCAL_DATABASE_DIR, "signal_tracking.csv")
SIGNAL_SCORE_MIN = float(st.secrets.get("SIGNAL_SCORE_MIN", 55))
PRIORITY_SCORE_MIN = float(st.secrets.get("PRIORITY_SCORE_MIN", 65))

# 各訊號基礎權重：買進型態給正分、賣出/風險型態給負分。
# key 為 signal_module 訊號的「label」(顯示名稱)，可自行調整。
SIGNAL_BASE_WEIGHT = {
    "漲幅達標": 6,
    "KD高腳": 12,
    "周1K": 15,
    "三白兵": 12,
    "布林縮窄突破": 15,
    "3K反轉": 12,
    "巧妙點": 14,
    "雙跳空": 10,
    "單跳空": 6,
    "漲停": 15,
    "雙漲停": 18,
    "廣義上升三法": 15,
    "島狀反轉": 15,
    "跌停": -20,
    "移動停利": -15,
    "廣義下降三法": -18,
    "反向島狀": -18,
}


def ensure_local_database_dir():
    os.makedirs(LOCAL_DATABASE_DIR, exist_ok=True)


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


def calc_signal_quality_score(data, signal_types, signal_kinds=None):
    """
    data: compute_indicators() 回傳的 dict
          (需含 ma_range / ma_trend / rs_raw / volume_lots / pct / volatility_pct)
    signal_types: 本次觸發的訊號「名稱」清單 (例如 ["KD高腳", "雙跳空"])
    signal_kinds: {訊號名稱: "buy"/"sell"}，用於買賣方向修正 (可不傳，僅影響下方第6、7項)
    """
    signal_kinds = signal_kinds or {}
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

    # 2. 相對強度：優先抓比大盤/族群強的股票
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
    volume_lots = safe_float(data.get("volume_lots", 0))
    if volume_lots >= 3000:
        score += 8
    elif volume_lots >= 1000:
        score += 4

    # 4. 避免暴衝過熱 / 波動過大
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

    # 5. 訊號本身的基礎權重 (買進型態加分、賣出/風險型態扣分)
    for sig in signal_types:
        score += SIGNAL_BASE_WEIGHT.get(sig, 0)

    # 6. 訊號共振：多個「買進型態」訊號同時出現，比單一訊號可靠
    buy_signals = {s for s in signal_types if signal_kinds.get(s, "buy") == "buy"}
    if len(buy_signals) >= 3:
        score += 12
    elif len(buy_signals) == 2:
        score += 6

    # 7. 買賣訊號同時出現：代表當下同時有出場/風險警示，降低追蹤分數
    sell_signals = {s for s in signal_types if signal_kinds.get(s) == "sell"}
    if sell_signals and buy_signals:
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
            safe_float(r.get("成交量(張)", 0)),
        ),
        reverse=True,
    )


def append_signal_tracking(row, scan_date, tracking_file=TRACKING_FILE):
    ensure_local_database_dir()
    base_cols = [
        "scan_date", "代碼", "股票名稱", "entry_price",
        "訊號類型", "訊號方向", "訊號分數", "追蹤等級",
        "RS加權報酬%", "MA位置", "MA排列", "成交量(張)",
        "status",
    ]
    new_record = {
        "scan_date": scan_date,
        "代碼": row.get("代碼"),
        "股票名稱": row.get("股票名稱"),
        "entry_price": row.get("價格"),
        "訊號類型": row.get("訊號類型"),
        "訊號方向": row.get("訊號方向"),
        "訊號分數": row.get("訊號分數"),
        "追蹤等級": row.get("追蹤等級"),
        "RS加權報酬%": row.get("RS加權報酬%"),
        "MA位置": row.get("MA位置"),
        "MA排列": row.get("MA排列"),
        "成交量(張)": row.get("成交量(張)"),
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
