"""
heatmap_utils.py
=================
熱力圖頁面共用的輕量工具函式。獨立成檔案，不動到 indicators.py，
避免影響掃描器/模擬器既有的訊號邏輯。
"""
import pandas as pd


def money_flow_proxy(df: pd.DataFrame, lookback: int = 5) -> float:
    """
    價量估算的「主力進出強度」代理指標（不需要三大法人資料，
    純粹用現有 OHLCV 就能算，可當作「資金流向」的替代/輔助視角）。

    邏輯：
      1. 用近 lookback 天的 OBV (On-Balance Volume) 變化方向，
         乘以近 lookback 天的平均成交金額，得到一個「有方向的資金規模」數字。
      2. 正值 = 資金流入傾向（量增價漲/收紅居多），負值 = 資金流出傾向。

    這不是真正的法人買賣超金額，只是一個相對強弱的代理分數，
    熱力圖上主要拿來跟法人買賣超做「交叉比對」用，不建議單獨當作進出場依據。

    回傳: float，資金流向代理分數（元），正負代表方向，數值大小代表規模。
    """
    if df is None or df.empty or len(df) < 2:
        return 0.0
    d = df.tail(max(lookback + 1, 2)).copy()
    if not {"Close", "Volume"}.issubset(d.columns):
        return 0.0

    price_diff = d["Close"].diff()
    direction = price_diff.apply(lambda x: 1 if x > 0 else (-1 if x < 0 else 0))
    obv_delta = (direction * d["Volume"]).iloc[1:]  # 第一天沒有前一天可比較，丟棄

    avg_price = d["Close"].iloc[1:].mean()
    if pd.isna(avg_price) or avg_price <= 0:
        return 0.0

    # OBV 淨變化(股) × 均價 ≈ 有方向的資金規模(元)
    return float(obv_delta.sum() * avg_price)


def pct_change_today(df: pd.DataFrame) -> float:
    """最新一天相對前一天的漲跌幅 (%)。df 需按日期由舊到新排序，且至少 2 列。"""
    if df is None or df.empty or len(df) < 2 or "Close" not in df.columns:
        return 0.0
    prev_close = df["Close"].iloc[-2]
    last_close = df["Close"].iloc[-1]
    if pd.isna(prev_close) or prev_close == 0 or pd.isna(last_close):
        return 0.0
    return float((last_close - prev_close) / prev_close * 100)


def format_twd(value, unit: str = "元") -> str:
    """把金額格式化成「億/萬」單位的中文顯示字串，給熱力圖 tooltip 用，避免顯示一長串數字。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "-"
    if pd.isna(v):
        return "-"
    sign = "-" if v < 0 else ""
    v = abs(v)
    if v >= 1e8:
        return f"{sign}{v / 1e8:.2f}億{unit}"
    if v >= 1e4:
        return f"{sign}{v / 1e4:.1f}萬{unit}"
    return f"{sign}{v:.0f}{unit}"


def format_shares_as_lots(value) -> str:
    """把股數格式化成「張」為單位（1張=1000股），給三大法人買賣超這類股數欄位的 tooltip 用。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "-"
    if pd.isna(v):
        return "-"
    return f"{v / 1000:,.0f}張"
