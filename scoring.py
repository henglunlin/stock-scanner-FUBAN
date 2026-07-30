"""
scoring.py
=======
二階段過濾：訊號品質評分 / 優先追蹤排序 / 訊號追蹤紀錄。
獨立成模組方便日後單獨調整評分規則（例如新增/修改某個評分項目的權重），
不需要動到 app.py 的主流程與 UI 程式碼。
"""

import os

import pandas as pd
import streamlit as st

# ===== 二階段過濾 / 追蹤 Database 設定 =====
LOCAL_DATABASE_DIR = st.secrets.get("LOCAL_DATABASE_DIR", "Database")
TRACKING_FILE = os.path.join(LOCAL_DATABASE_DIR, "signal_tracking.csv")
SIGNAL_SCORE_MIN = float(st.secrets.get("SIGNAL_SCORE_MIN", 55))
PRIORITY_SCORE_MIN = float(st.secrets.get("PRIORITY_SCORE_MIN", 65))


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


def calc_signal_quality_score(data, signal_types):
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

    # 2. 相對強度：優先抓比大盤 / 族群強的股票
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
    vol_ratio = safe_float(data.get("trend_vol_ratio", 0))
    volume_lots = safe_float(data.get("volume_lots", 0))

    if volume_lots >= 3000:
        score += 8
    elif volume_lots >= 1000:
        score += 4

    if vol_ratio >= 2:
        score += 12
    elif vol_ratio >= 1.2:
        score += 6

    # 4. KD 位置：避免太高追價，也避免太弱接刀
    k = safe_float(data.get("k", 0))
    d = safe_float(data.get("d", 0))
    if 35 <= k <= 75 and k > d:
        score += 12
    elif k > 85:
        score -= 8
    elif k < 15:
        score -= 6

    # 5. 日KD 交叉訊號：黃金交叉 / 即將黃金交叉 / 死亡交叉
    # （與週KD邏輯對稱，用 signals 模組寫入 data["kd_signal"] 的字串判斷）
    day_kd_signal = data.get("kd_signal", "")
    if day_kd_signal == "黃金交叉":
        score += 15
    elif day_kd_signal == "即將黃金交叉":
        score += 8
    elif day_kd_signal == "死亡交叉":
        score -= 5

    # 6. 週KD：週線方向比日線更重要
    week_signal = data.get("week_kd_signal", "")
    if week_signal == "黃金交叉":
        score += 15
    elif week_signal == "即將黃金交叉":
        score += 8
    elif week_signal == "超賣":
        score -= 5

    # 7. MACD
    macd_hist = safe_float(data.get("macd_hist", 0))
    macd_signal = data.get("macd_signal", "")
    if macd_signal == "MACD翻正":
        score += 12
    elif macd_hist > 0:
        score += 6
    elif macd_hist < 0:
        score -= 5

    # 8. 趨勢突破品質
    if data.get("trend_signal") == "趨勢突破":
        score += 20

    touch_count = safe_float(data.get("trend_touch_count", 0))
    violations = safe_float(data.get("trend_violations", 0))
    if touch_count >= 2 and violations == 0:
        score += 8
    elif violations > 0:
        score -= 10

    # 9. 避免暴衝過熱
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

    # 10. 訊號共振：多個訊號同時出現，比單一訊號可靠
    unique_signal_count = len(set(signal_types))
    if unique_signal_count >= 3:
        score += 12
    elif unique_signal_count == 2:
        score += 6

    # 11. 特殊修正：避免空頭弱反彈被誤判成強訊號
    if "即將黃金交叉" in signal_types:
        if ma_trend == "空頭" and ma_range == "<MA20":
            score -= 20

    if "黃金交叉" in signal_types:
        if ma_range in [">MA5", "MA5~10"]:
            score += 10
        elif ma_range == "<MA20":
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
            safe_float(r.get("量能倍數", 0)),
        ),
        reverse=True,
    )


def append_signal_tracking(row, scan_date, tracking_file=TRACKING_FILE):
    ensure_local_database_dir()
    base_cols = [
        "scan_date", "代碼", "股票名稱", "entry_price",
        "訊號類型", "訊號分數", "追蹤等級",
        "RS加權報酬%", "MA位置", "MA排列",
        "K值", "D值", "週K值", "週D值",
        "MACD柱", "趨勢突破", "量能倍數",
        "status",
    ]
    new_record = {
        "scan_date": scan_date,
        "代碼": row.get("代碼"),
        "股票名稱": row.get("股票名稱"),
        "entry_price": row.get("價格"),
        "訊號類型": row.get("訊號類型"),
        "訊號分數": row.get("訊號分數"),
        "追蹤等級": row.get("追蹤等級"),
        "RS加權報酬%": row.get("RS加權報酬%"),
        "MA位置": row.get("MA位置"),
        "MA排列": row.get("MA排列"),
        "K值": row.get("K值"),
        "D值": row.get("D值"),
        "週K值": row.get("週K值"),
        "週D值": row.get("週D值"),
        "MACD柱": row.get("MACD柱"),
        "趨勢突破": row.get("趨勢突破"),
        "量能倍數": row.get("量能倍數"),
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
