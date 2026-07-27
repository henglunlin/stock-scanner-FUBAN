"""
signals 套件
=============
把每一種掃描條件(訊號)獨立成一個 check_xxx_signal(ctx, params) 函式，
再用 SIGNAL_REGISTRY 這張「登記表」把它們串起來。

★ 新增一個掃描條件：
    1. 在對應的模組寫一個 check_xxx_signal(ctx, params) -> dict
       （回傳格式：{"labels": [...], "fields": {...}, "extra": {...}或None}）
    2. 在下面的 SIGNAL_REGISTRY 多加一行
    3. app.py 完全不用修改

★ 修改某個掃描條件的邏輯：
    只需要改對應模組裡的 check_xxx_signal 函式本體，不會牽動其他訊號。

★ 暫時關閉某個掃描條件：
    把 SIGNAL_REGISTRY 裡那一行刪掉或註解掉即可。
"""

from .context import SignalContext, build_signal_context
from .rise_threshold import check_rise_threshold_signal
from .gap import check_gap_signal
from .kd import check_kd_golden_cross_signal, check_week_kd_signal
from .macd import check_macd_signal
from .trend_breakout import (
    check_trend_breakout_signal,
    detect_downtrend_breakout,
    plot_trend_breakout_chart,
    TREND_VOL_RATIO_MIN,
)


SIGNAL_REGISTRY = {
    "漲幅達標": {
        "func": check_rise_threshold_signal,
        "default_enabled": True,
        "ui_help": "當日漲幅 ≥ 上方「儀表板漲幅達標門檻」",
    },
    "跳空": {
        "func": check_gap_signal,
        "default_enabled": True,
        "ui_help": "今天最低價 > 昨天最高價",
    },
    "黃金交叉": {
        "func": check_kd_golden_cross_signal,
        "default_enabled": True,
        "ui_help": "日KD 黃金交叉 / 即將黃金交叉",
    },
    "週黃金交叉": {
        "func": check_week_kd_signal,
        "default_enabled": True,
        "ui_help": "以同一批日線資料重採樣成週線計算KD，不會多打API",
    },
    "MACD翻正": {
        "func": check_macd_signal,
        "default_enabled": True,
        "ui_help": "MACD 柱狀圖由負翻正",
    },
    "趨勢突破": {
        "func": check_trend_breakout_signal,
        "default_enabled": True,
        "ui_help": "上凸包(upper convex hull)找下降趨勢壓力線 + 可選量能確認",
    },
}


def run_signal_registry(ctx: SignalContext, rise_threshold: float, require_trend_volume_confirm: bool = False) -> dict:
    """依序執行 SIGNAL_REGISTRY 裡的每個訊號函式，回傳 {訊號名稱: 結果dict}。"""
    runtime_params = {
        "漲幅達標": {"threshold": rise_threshold},
        "趨勢突破": {"require_volume_confirm": require_trend_volume_confirm},
    }
    results = {}
    for name, cfg in SIGNAL_REGISTRY.items():
        params = runtime_params.get(name, {})
        results[name] = cfg["func"](ctx, params)
    return results


def compute_indicators(df, price, symbol="", rise_threshold=5.0, require_trend_volume_confirm=False):
    ctx = build_signal_context(symbol, df, price)
    signal_results = run_signal_registry(ctx, rise_threshold, require_trend_volume_confirm)

    gap_fields = signal_results["跳空"]["fields"]
    macd_fields = signal_results["MACD翻正"]["fields"]
    trend_fields = signal_results["趨勢突破"]["fields"]
    trend_extra = signal_results["趨勢突破"]["extra"]
    trend_signal = "趨勢突破" if signal_results["趨勢突破"]["labels"] else "-"

    return {
        "price": round(ctx.price, 2),
        "pct": round(ctx.change_pct, 2),
        "ma_range": ctx.ma_range,
        "ma_trend": ctx.ma_trend,
        "k": round(ctx.k_t, 1),
        "d": round(ctx.d_t, 1),
        "kd_signal": ctx.kd_signal,
        "week_k": round(ctx.week_k_t, 1) if ctx.week_k_t is not None else "-",
        "week_d": round(ctx.week_d_t, 1) if ctx.week_d_t is not None else "-",
        "week_kd_signal": ctx.week_kd_signal,
        "gap_signal": gap_fields.get("跳空訊號", "-"),
        "macd_hist": macd_fields.get("MACD柱", 0.0),
        "macd_signal": macd_fields.get("MACD訊號", "-"),
        "volume": int(ctx.latest_volume),
        "volume_lots": round(ctx.volume_lots, 1),
        "volatility_pct": round(ctx.volatility_pct, 2) if ctx.volatility_pct is not None else "-",
        "rs_raw": round(ctx.rs_raw, 2) if ctx.rs_raw is not None else "-",
        "trend_signal": trend_signal,
        "p1_date": trend_fields.get("P1日期", "-"),
        "p1_val": trend_fields.get("區高P1", "-"),
        "p2_date": trend_fields.get("P2日期", "-"),
        "p2_val": trend_fields.get("近高P2", "-"),
        "slope_pct": trend_fields.get("坡度%", "-"),
        "tl_val": trend_fields.get("趨勢價", "-"),
        "trend_touch_count": trend_fields.get("貼線數", "-"),
        "trend_violations": trend_fields.get("穿線數", "-"),
        "trend_vol_ratio": trend_fields.get("量能倍數", "-"),
        "trend_chart_df": trend_extra.get("chart_df") if trend_extra else None,
        "trend_p1_pos": trend_extra.get("p1_pos") if trend_extra else None,
        "trend_p2_pos": trend_extra.get("p2_pos") if trend_extra else None,
        "trend_slope": trend_extra.get("slope") if trend_extra else None,
        # 保留完整訊號結果，主迴圈可直接彙整 labels，新增訊號時不用改主迴圈
        "signal_results": signal_results,
    }

