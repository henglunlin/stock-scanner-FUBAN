"""
signals/context.py
====================
SignalContext：所有掃描條件共用的基礎資料與指標（MA/KD/週KD/MACD/RS/波動率），
只計算一次後傳給每個 check_xxx_signal() 使用，避免各訊號重複計算。
"""

from dataclasses import dataclass, field

import pandas as pd


def calc_custom_volatility(df, price_val, window=20):
    if df is None or df.empty:
        return None
    required_cols = ["Open", "High", "Low", "Close"]
    if not set(required_cols).issubset(df.columns):
        return None

    work = df.copy().reset_index(drop=True)
    for col in required_cols:
        work[col] = pd.to_numeric(work[col], errors="coerce")
    work = work.dropna(subset=required_cols).reset_index(drop=True)

    if len(work) < window + 1:
        return None

    close_for_calc = work["Close"].copy()
    close_for_calc.iloc[-1] = float(price_val)

    prev_close = close_for_calc.shift(1)
    open_ = work["Open"]
    high = work["High"]
    low = work["Low"]
    close_ = close_for_calc

    is_bullish_k = close_ >= open_

    bull_range = (
        (prev_close - open_).abs()
        + (open_ - low).abs()
        + (low - high).abs()
        + (high - close_).abs()
    )
    bear_range = (
        (prev_close - open_).abs()
        + (open_ - low).abs()
        + (high - low).abs()
        + (low - close_).abs()
    )

    daily_swing = bull_range.where(is_bullish_k, bear_range)
    avg_20_swing = daily_swing.rolling(window=window, min_periods=window).mean()
    ma20 = close_for_calc.rolling(window=window, min_periods=window).mean()

    latest_avg_20_swing = avg_20_swing.iloc[-1]
    latest_ma20 = ma20.iloc[-1]
    if pd.isna(latest_avg_20_swing) or pd.isna(latest_ma20) or latest_ma20 == 0:
        return None

    return float(latest_avg_20_swing / latest_ma20 * 100)

def calc_rs_raw_value(df, price_val):
    """
    計算單月 RS 原始值 (週加權報酬率)
    權重：最近1週40%，前1週20%，再前1週20%，最初1週20%
    一週大約 5 個交易日
    """
    if df is None or len(df) < 21:
        return None
    
    close = df["Close"].copy().reset_index(drop=True)
    # 用最新價格替換最後一筆收盤價，以貼近盤中即時狀態
    close.iloc[-1] = float(price_val)
    
    n = len(close)
    
    def get_return(start_idx, end_idx):
        start_idx = max(0, start_idx)
        if start_idx >= end_idx:
            return 0.0
        start_price = close.iloc[start_idx]
        end_price = close.iloc[end_idx]
        if start_price == 0 or pd.isna(start_price): 
            return 0.0
        return (end_price - start_price) / start_price

    end_idx = n - 1
    # W4 (最近1週/約 5 個交易日)
    w4_start = max(0, end_idx - 5)
    ret_w4 = get_return(w4_start, end_idx)
    
    # W3 (前1週)
    w3_start = max(0, w4_start - 5)
    ret_w3 = get_return(w3_start, w4_start)
    
    # W2 (再前1週)
    w2_start = max(0, w3_start - 5)
    ret_w2 = get_return(w2_start, w3_start)
    
    # W1 (最初1週)
    w1_start = max(0, w2_start - 5)
    ret_w1 = get_return(w1_start, w2_start)
    
    # 根據公式加權
    rs_raw = (ret_w4 * 0.4) + (ret_w3 * 0.2) + (ret_w2 * 0.2) + (ret_w1 * 0.2)
    return rs_raw * 100


def calc_kd_series(close, low, high, period: int = 9):
    """通用 KD 計算（日線／週線皆可共用），回傳 K、D 序列"""
    low_n = low.rolling(period).min()
    high_n = high.rolling(period).max()
    denominator = (high_n - low_n).replace(0, pd.NA)
    rsv = ((close - low_n) / denominator) * 100
    k = rsv.ewm(alpha=1/3, adjust=False).mean()
    d = k.ewm(alpha=1/3, adjust=False).mean()
    return k, d

def judge_kd_signal(k_t: float, k_y: float, d_t: float, d_y: float) -> str:
    """依據當期(t)與前一期(y)的 K/D 值判斷交叉訊號（日線／週線共用邏輯）"""
    if k_y <= d_y and k_t > d_t: return "黃金交叉"
    if k_y >= d_y and k_t < d_t: return "死亡交叉"
    if k_t < d_t and (d_t - k_t) < 3: return "即將黃金交叉"
    if k_t > d_t and (k_t - d_t) < 3: return "即將死亡交叉"
    if k_t < 25: return "超賣"
    return "-"

def resample_weekly_ohlc(df: pd.DataFrame) -> pd.DataFrame:
    """
    將日線 OHLC 依日期重採樣成週線。
    直接沿用同一批已下載的日線資料（df 需含 Date/High/Low/Close），
    不會為了週KD額外呼叫任何行情 API，維持原本的資料抓取時間範圍。
    """
    if df is None or df.empty or "Date" not in df.columns:
        return pd.DataFrame()
    weekly_src = df[["Date", "High", "Low", "Close"]].copy()
    weekly_src["Date"] = pd.to_datetime(weekly_src["Date"], errors="coerce")
    weekly_src = weekly_src.dropna(subset=["Date"]).set_index("Date").sort_index()
    if weekly_src.empty:
        return pd.DataFrame()
    weekly = weekly_src.resample("W").agg({"High": "max", "Low": "min", "Close": "last"}).dropna()
    return weekly


@dataclass
class SignalContext:
    """所有訊號共用的基礎資料與指標，只計算一次。"""
    symbol: str
    df: pd.DataFrame
    price: float
    close: pd.Series
    high: pd.Series
    low: pd.Series
    volume: pd.Series
    change_pct: float
    ma5: float
    ma10: float
    ma20: float
    ma_range: str
    ma_trend: str
    k_t: float
    d_t: float
    kd_signal: str
    week_k_t: float
    week_d_t: float
    week_kd_signal: str
    macd_hist_t: float
    macd_hist_y: float
    latest_volume: float
    volume_lots: float
    volatility_pct: float
    rs_raw: float
    extra: dict = field(default_factory=dict)


def build_signal_context(symbol: str, df: pd.DataFrame, price: float) -> SignalContext:
    """把原本散在 compute_indicators 裡的共用計算集中在這裡，各訊號函式共用同一份結果。"""
    if df is None or df.empty:
        raise ValueError("下載資料為空")
    if len(df) < 20:
        raise ValueError("歷史資料不足（至少需要 20 筆）")

    close = pd.to_numeric(df["Close"].squeeze(), errors="coerce")
    low = pd.to_numeric(df["Low"].squeeze(), errors="coerce")
    high = pd.to_numeric(df["High"].squeeze(), errors="coerce")
    volume = pd.to_numeric(df["Volume"].squeeze(), errors="coerce") if "Volume" in df.columns else pd.Series(dtype="float64")
    if close.isna().all() or low.isna().all() or high.isna().all():
        raise ValueError("OHLC 資料格式異常")

    yesterday_close = float(close.iloc[-2])
    if pd.isna(yesterday_close) or yesterday_close == 0:
        raise ValueError("昨收資料異常")

    price_val = float(price)
    change_pct = float((price_val / yesterday_close - 1) * 100)
    ma5 = float(close.tail(5).mean())
    ma10 = float(close.tail(10).mean())
    ma20 = float(close.tail(20).mean())

    if price_val > ma5: ma_range = ">MA5"
    elif ma5 >= price_val > ma10: ma_range = "MA5~10"
    elif ma10 >= price_val > ma20: ma_range = "MA10~20"
    else: ma_range = "<MA20"

    if ma5 > ma10 > ma20: ma_trend = "多頭"
    elif ma5 < ma10 < ma20: ma_trend = "空頭"
    else: ma_trend = "糾結"

    k, d = calc_kd_series(close, low, high, period=9)
    if len(k.dropna()) < 2 or len(d.dropna()) < 2:
        raise ValueError("KD 計算資料不足")
    k_t, d_t = float(k.iloc[-1]), float(d.iloc[-1])
    k_y, d_y = float(k.iloc[-2]), float(d.iloc[-2])
    kd_signal = judge_kd_signal(k_t, k_y, d_t, d_y)

    # ===== 週KD計算（沿用同一批日線資料重採樣，不多打API）=====
    week_k_t = week_d_t = None
    week_kd_signal = "-"
    weekly_df = resample_weekly_ohlc(df)
    if len(weekly_df) >= 10:
        wk, wd = calc_kd_series(weekly_df["Close"], weekly_df["Low"], weekly_df["High"], period=9)
        if len(wk.dropna()) >= 2 and len(wd.dropna()) >= 2:
            week_k_t, week_d_t = float(wk.iloc[-1]), float(wd.iloc[-1])
            week_k_y, week_d_y = float(wk.iloc[-2]), float(wd.iloc[-2])
            week_kd_signal = judge_kd_signal(week_k_t, week_k_y, week_d_t, week_d_y)

    # ===== MACD 計算 =====
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    macd_hist = dif - dea
    if len(macd_hist.dropna()) < 2:
        raise ValueError("MACD 計算資料不足")
    macd_hist_t = float(macd_hist.iloc[-1])
    macd_hist_y = float(macd_hist.iloc[-2])

    latest_volume = 0.0
    if not volume.empty and pd.notna(volume.iloc[-1]):
        latest_volume = float(volume.iloc[-1])
    volume_lots = latest_volume / 1000

    volatility_pct = calc_custom_volatility(df, price_val, window=20)
    rs_raw = calc_rs_raw_value(df, price_val)

    return SignalContext(
        symbol=symbol, df=df, price=price_val,
        close=close, high=high, low=low, volume=volume,
        change_pct=change_pct, ma5=ma5, ma10=ma10, ma20=ma20,
        ma_range=ma_range, ma_trend=ma_trend,
        k_t=k_t, d_t=d_t, kd_signal=kd_signal,
        week_k_t=week_k_t, week_d_t=week_d_t, week_kd_signal=week_kd_signal,
        macd_hist_t=macd_hist_t, macd_hist_y=macd_hist_y,
        latest_volume=latest_volume, volume_lots=volume_lots,
        volatility_pct=volatility_pct, rs_raw=rs_raw,
    )
