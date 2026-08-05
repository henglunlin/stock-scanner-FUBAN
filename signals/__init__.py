"""
signals 套件（2026 改版）
=========================
訊號判斷邏輯已全部移交給 signal_module/ 下的可編輯訊號模組：
  漲幅達標、KD高腳、周1K、三白兵、布林縮窄突破、3K反轉、巧妙點、
  雙跳空、單跳空、漲停、雙漲停、跌停、移動停利、
  廣義上升三法、廣義下降三法、島狀反轉、反向島狀。
（原本的 跳空 / 黃金交叉 / 即將黃金交叉 / 週黃金交叉 / 週即將黃金交叉 / MACD翻正 / 趨勢突破 已移除）

這個套件保留給主程式(app)呼叫的固定入口，讓主程式不用管訊號實作細節：
  - compute_indicators(df, price, symbol, name, rise_threshold) -> dict
  - get_signal_registry() -> 目前註冊的訊號清單 (可在「🛠️ 訊號編輯」頁面新增/修改後即時反映)
  - build_signal_chart_figure / render_signal_detail_panel -> 個股 K線 + 訊號說明
"""
import pandas as pd

from signal_module import module_loader
from signal_module.base import SIGNAL_REGISTRY, SignalContext as ModuleSignalContext, SignalResult
from signal_module.indicators import add_indicators

from .context import build_base_context
from .chart import build_signal_chart_figure, render_signal_detail_panel  # noqa: F401  (re-export)

# 啟動時（本 process 第一次 import 這個套件時）載入一次預設訊號模組。
# 之後若在「🛠️ 訊號編輯」頁面存檔，會直接呼叫 module_loader.load_default_signal_modules()
# 重新載入 —— 因為 SIGNAL_REGISTRY 是同一個 dict 物件，全站都會立即看到最新版本。
if not SIGNAL_REGISTRY:
    module_loader.load_default_signal_modules()


def get_signal_registry():
    """回傳目前已註冊的訊號清單：{key: {"label","description","kind","func"}}"""
    return SIGNAL_REGISTRY


def _prepare_indicator_df(df: pd.DataFrame, price: float) -> pd.DataFrame:
    """
    把主程式抓回來的 df (含 Date 欄位 + OHLCV) 轉成 signal_module 需要的格式：
    index=Date字串、由舊到新排序，並附上 K/D/MA/Bias/BBand 等技術指標欄位。
    最後一筆收盤價用即時價覆蓋，貼近盤中即時狀態 (與原本 context.py 邏輯一致)。
    """
    work = df.copy()
    work["Date"] = pd.to_datetime(work["Date"], errors="coerce")
    work = work.dropna(subset=["Date"]).sort_values("Date").reset_index(drop=True)
    if work.empty:
        raise ValueError("下載資料為空")

    work.loc[work.index[-1], "Close"] = float(price)

    work = work.set_index(work["Date"].dt.strftime("%Y-%m-%d"))[["Open", "High", "Low", "Close", "Volume"]]
    work.index.name = "Date"
    work = add_indicators(work)
    return work


def run_signal_registry(symbol: str, name: str, df_ind: pd.DataFrame, scan_date: str, rise_threshold: float = 5.0) -> dict:
    """對單一股票已含指標的 df 跑過全部已註冊訊號，回傳 {key: SignalResult}"""
    ctx = ModuleSignalContext(
        code=symbol, name=name, df=df_ind, scan_date=scan_date,
        params={"rise_threshold": rise_threshold},
    )
    results = {}
    for key, cfg in SIGNAL_REGISTRY.items():
        try:
            results[key] = cfg["func"](ctx)
        except Exception as e:
            results[key] = SignalResult(hit=False, detail=f"訊號執行發生錯誤：{e}")
    return results


def compute_indicators(df, price, symbol="", name="", rise_threshold=5.0):
    """
    主流程呼叫入口：
    1. 計算表格/評分共用的基礎數值 (價格/漲跌/MA位置/成交量/波動率/RS)
    2. 補上今日即時價並計算 K/D/MA/Bias/BBand 等技術指標
    3. 跑過訊號登記表，收集所有命中的訊號
    """
    base = build_base_context(df, price)
    df_ind = _prepare_indicator_df(df, price)
    scan_date = df_ind.index[-1]

    signal_results = run_signal_registry(symbol, name, df_ind, scan_date, rise_threshold)

    hit_keys = [key for key, res in signal_results.items() if res.hit]
    signal_types = [SIGNAL_REGISTRY[key]["label"] for key in hit_keys]
    signal_kinds = {SIGNAL_REGISTRY[key]["label"]: SIGNAL_REGISTRY[key].get("kind", "buy") for key in hit_keys}
    signal_details = {SIGNAL_REGISTRY[key]["label"]: signal_results[key].detail for key in hit_keys}
    signal_marks = {SIGNAL_REGISTRY[key]["label"]: signal_results[key].marks for key in hit_keys}

    chart_df = df_ind.reset_index()  # 欄位: Date, Open, High, Low, Close, Volume, MA5.../K/D/BB_... 等

    return {
        "price": round(base["price"], 2),
        "pct": round(base["pct"], 2),
        "ma_range": base["ma_range"],
        "ma_trend": base["ma_trend"],
        "volume": int(base["latest_volume"]),
        "volume_lots": round(base["volume_lots"], 1),
        "volatility_pct": round(base["volatility_pct"], 2) if base["volatility_pct"] is not None else "-",
        "rs_raw": round(base["rs_raw"], 2) if base["rs_raw"] is not None else "-",
        "signal_types": signal_types,
        "signal_kinds": signal_kinds,
        "signal_details": signal_details,
        "signal_marks": signal_marks,
        "signal_results": signal_results,
        "chart_df": chart_df,
        "scan_date": scan_date,
    }
