"""
訊號模組基礎架構
所有訊號模組都應該:
1. from signal_module.base import SignalContext, register_signal
2. 用 @register_signal(key, label, description) 裝飾一個函式
3. 函式簽名: def fn(ctx: SignalContext) -> SignalResult
"""
from dataclasses import dataclass, field
from typing import Optional
import pandas as pd

# 全域訊號註冊表: { key: {"label": str, "description": str, "func": callable, "kind": str} }
# kind: "buy" (買進/多方訊號，預設) 或 "sell" (賣出/風險訊號)
SIGNAL_REGISTRY = {}


@dataclass
class SignalContext:
    """傳遞給每個訊號判斷函式的上下文"""
    code: str                  # 股票代碼
    name: str                  # 股票名稱
    df: pd.DataFrame           # 該股票完整 OHLCV 資料 (index=Date字串, 由舊到新排序), columns: Open High Low Close Volume
    scan_date: str             # 掃描日期 (YYYY-MM-DD)，訊號判斷是否成立以此日為基準


@dataclass
class SignalResult:
    """訊號判斷結果"""
    hit: bool                          # 是否觸發訊號
    detail: str = ""                   # 說明文字
    marks: list = field(default_factory=list)   # 需要在圖上標記的日期清單 (YYYY-MM-DD)


def register_signal(key: str, label: str, description: str = "", kind: str = "buy"):
    """裝飾器: 註冊一個訊號判斷函式

    kind: "buy" (買進/多方訊號，預設不填即為此) 或 "sell" (賣出/風險訊號)，
          用於「訊號編輯」頁面「買賣方向」欄位的顯示分類。
    """
    def deco(func):
        SIGNAL_REGISTRY[key] = {
            "label": label,
            "description": description,
            "func": func,
            "kind": kind,
        }
        return func
    return deco
