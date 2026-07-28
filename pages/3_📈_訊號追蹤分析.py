"""
pages/1_📈_訊號追蹤分析.py
================================
Streamlit page for tracking and analyzing all signal_tracking*.csv files under:
https://github.com/henglunlin/stock-scanner-FUBAN/tree/main/Database

Features:
- Lists and downloads all Database/signal_tracking*.csv files from GitHub.
- Combines daily tracking snapshots into one deduplicated tracking table.
- Updates forward price performance using yfinance.
- Shows post-signal price trend charts for selected stocks.
- Optional: runs root update_signal_tracking.py via subprocess.
- Optional: uploads the combined / updated CSV back to GitHub using a date filename.

Place this file under your repo's pages/ folder:
    pages/3_📈_訊號追蹤分析.py

Required Streamlit secrets:
    GITHUB_TOKEN = "github_pat_xxx"  # or a token with Contents read/write
    GITHUB_OWNER = "henglunlin"
    GITHUB_REPO = "stock-scanner-FUBAN"
    GITHUB_BRANCH = "main"
    GITHUB_DATABASE_DIR = "Database"
    LOCAL_DATABASE_DIR = "Database"
"""

from __future__ import annotations

import base64
import io
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st
import yfinance as yf

try:
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
except Exception:  # pragma: no cover
    go = None
    make_subplots = None


# =========================
# Page config
# =========================
st.set_page_config(page_title="訊號追蹤分析", layout="wide")


# =========================
# Config helpers
# =========================
DEFAULT_OWNER = "henglunlin"
DEFAULT_REPO = "stock-scanner-FUBAN"
DEFAULT_BRANCH = "main"
DEFAULT_DATABASE_DIR = "Database"
DEFAULT_LOCAL_DATABASE_DIR = "Database"
TRACKING_PREFIX = "signal_tracking"


def get_secret_or_default(key: str, default: str = "") -> str:
    try:
        value = st.secrets.get(key, default)
        return str(value) if value not in [None, ""] else default
    except Exception:
        return default


def github_config() -> Dict[str, str]:
    return {
        "token": get_secret_or_default("GITHUB_TOKEN", ""),
        "owner": get_secret_or_default("GITHUB_OWNER", DEFAULT_OWNER),
        "repo": get_secret_or_default("GITHUB_REPO", DEFAULT_REPO),
        "branch": get_secret_or_default("GITHUB_BRANCH", DEFAULT_BRANCH),
        "database_dir": get_secret_or_default("GITHUB_DATABASE_DIR", DEFAULT_DATABASE_DIR).strip("/"),
        "local_database_dir": get_secret_or_default("LOCAL_DATABASE_DIR", DEFAULT_LOCAL_DATABASE_DIR),
    }


def tw_now() -> datetime:
    return datetime.now(ZoneInfo("Asia/Taipei"))


def today_tracking_filename() -> str:
    return f"signal_tracking_{tw_now().strftime('%Y%m%d')}.csv"


def safe_float(x: Any, default: float = 0.0) -> float:
    try:
        if x in ["-", "", None]:
            return default
        if pd.isna(x):
            return default
        return float(x)
    except Exception:
        return default


def normalize_symbol(symbol: str) -> str:
    s = str(symbol).strip().upper()
    if not s:
        return s
    if "." in s:
        return s
    if s.isdigit():
        if s.startswith(("3", "6", "8")):
            return f"{s}.TWO"
        return f"{s}.TW"
    return s


# =========================
# GitHub API helpers
# =========================
def github_headers(token: str) -> Dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def github_contents_url(owner: str, repo: str, path: str) -> str:
    return f"https://api.github.com/repos/{owner}/{repo}/contents/{path.strip('/')}"


@st.cache_data(ttl=180)
def list_database_files(owner: str, repo: str, branch: str, database_dir: str, token: str) -> List[Dict[str, Any]]:
    url = github_contents_url(owner, repo, database_dir)
    res = requests.get(url, headers=github_headers(token), params={"ref": branch}, timeout=25)
    if res.status_code != 200:
        raise RuntimeError(f"讀取 GitHub Database 失敗：{res.status_code} {res.text}")
    data = res.json()
    if not isinstance(data, list):
        raise RuntimeError("GitHub Database 回傳格式不是資料夾清單。")
    return data


@st.cache_data(ttl=180)
def download_github_file(owner: str, repo: str, branch: str, path: str, token: str) -> bytes:
    url = github_contents_url(owner, repo, path)
    res = requests.get(url, headers=github_headers(token), params={"ref": branch}, timeout=25)
    if res.status_code != 200:
        raise RuntimeError(f"下載 GitHub 檔案失敗：{path}｜{res.status_code} {res.text}")
    payload = res.json()
    content = payload.get("content", "")
    if not content:
        raise RuntimeError(f"GitHub 檔案內容為空：{path}")
    return base64.b64decode(content)


def upload_file_to_github(file_bytes: bytes, github_path: str, commit_message: str) -> bool:
    cfg = github_config()
    token = cfg["token"]
    if not token:
        st.error("GITHUB_TOKEN 尚未設定，無法上傳。")
        return False

    url = github_contents_url(cfg["owner"], cfg["repo"], github_path)
    headers = github_headers(token)

    sha = None
    get_res = requests.get(url, headers=headers, params={"ref": cfg["branch"]}, timeout=25)
    if get_res.status_code == 200:
        sha = get_res.json().get("sha")
    elif get_res.status_code != 404:
        st.error(f"讀取既有 GitHub 檔案失敗：{get_res.status_code} {get_res.text}")
        return False

    payload: Dict[str, Any] = {
        "message": commit_message,
        "content": base64.b64encode(file_bytes).decode("utf-8"),
        "branch": cfg["branch"],
    }
    if sha:
        payload["sha"] = sha

    put_res = requests.put(url, headers=headers, json=payload, timeout=35)
    if put_res.status_code not in [200, 201]:
        st.error(f"上傳 GitHub 失敗：{put_res.status_code} {put_res.text}")
        return False

    html_url = put_res.json().get("content", {}).get("html_url", "")
    st.success(f"已上傳：{html_url}")
    return True


def is_tracking_csv(filename: str) -> bool:
    lower = filename.lower()
    return lower.startswith(TRACKING_PREFIX) and lower.endswith(".csv")


# =========================
# Data loading and combining
# =========================
def read_tracking_csv(file_bytes: bytes, source_name: str) -> pd.DataFrame:
    df = pd.read_csv(io.BytesIO(file_bytes), encoding="utf-8-sig")
    df["source_file"] = source_name
    return df


def load_all_tracking_from_github() -> Tuple[pd.DataFrame, List[str]]:
    cfg = github_config()
    files = list_database_files(
        cfg["owner"],
        cfg["repo"],
        cfg["branch"],
        cfg["database_dir"],
        cfg["token"],
    )
    tracking_files = sorted(
        [f for f in files if f.get("type") == "file" and is_tracking_csv(f.get("name", ""))],
        key=lambda x: x.get("name", ""),
    )
    if not tracking_files:
        return pd.DataFrame(), []

    dfs = []
    loaded_names = []
    for f in tracking_files:
        path = f.get("path") or f"{cfg['database_dir']}/{f.get('name')}"
        name = f.get("name", path)
        try:
            b = download_github_file(cfg["owner"], cfg["repo"], cfg["branch"], path, cfg["token"])
            dfs.append(read_tracking_csv(b, name))
            loaded_names.append(name)
        except Exception as e:
            st.warning(f"讀取 {name} 失敗：{e}")

    if not dfs:
        return pd.DataFrame(), loaded_names

    combined = pd.concat(dfs, ignore_index=True)
    combined = normalize_tracking_df(combined)
    return combined, loaded_names


def normalize_tracking_df(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()

    # Standard columns that should exist
    for col in [
        "scan_date", "代碼", "股票名稱", "entry_price", "訊號類型", "訊號分數", "追蹤等級",
        "RS加權報酬%", "MA位置", "MA排列", "K值", "D值", "週K值", "週D值",
        "MACD柱", "趨勢突破", "量能倍數", "status",
    ]:
        if col not in out.columns:
            out[col] = ""

    out["scan_date"] = pd.to_datetime(out["scan_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    out["代碼"] = out["代碼"].apply(normalize_symbol)
    out["entry_price"] = out["entry_price"].apply(lambda x: safe_float(x, None))
    out["訊號分數"] = out["訊號分數"].apply(lambda x: safe_float(x, None))

    # Sort by source then drop duplicates by date + symbol, keeping latest source_file row
    out = out.sort_values(["scan_date", "代碼", "source_file"], na_position="last")
    out = out.drop_duplicates(subset=["scan_date", "代碼"], keep="last").reset_index(drop=True)
    return out


# =========================
# yfinance performance update
# =========================
@st.cache_data(ttl=3600, show_spinner=False)
def download_price_history(symbol: str, period: str = "3mo") -> pd.DataFrame:
    hist = yf.download(symbol, period=period, interval="1d", progress=False, auto_adjust=False)
    if hist.empty:
        return hist
    if isinstance(hist.columns, pd.MultiIndex):
        hist.columns = hist.columns.get_level_values(0)
    hist = hist.copy()
    hist.index = pd.to_datetime(hist.index).tz_localize(None)
    return hist


def calc_return_after_days(closes: pd.Series, entry_price: float, days: int) -> Optional[float]:
    if len(closes) < days:
        return None
    return round((float(closes.iloc[days - 1]) / entry_price - 1) * 100, 2)


def calc_max_gain(highs: pd.Series, entry_price: float) -> Optional[float]:
    if highs.empty:
        return None
    return round((float(highs.max()) / entry_price - 1) * 100, 2)


def calc_max_drawdown(lows: pd.Series, entry_price: float) -> Optional[float]:
    if lows.empty:
        return None
    return round((float(lows.min()) / entry_price - 1) * 100, 2)


def classify_success(max_gain: float, max_drawdown: float, close_return: float) -> int:
    return int(max_gain >= 5 and max_drawdown > -5 and close_return >= 2)


def update_forward_performance(df: pd.DataFrame, max_days: int = 20, period: str = "6mo") -> pd.DataFrame:
    if df.empty:
        return df

    out = df.copy()
    result_rows = []
    symbols = sorted(out["代碼"].dropna().unique().tolist())

    progress = st.progress(0, text="準備下載股價資料...")
    price_map: Dict[str, pd.DataFrame] = {}
    for i, symbol in enumerate(symbols, start=1):
        progress.progress(i / max(len(symbols), 1), text=f"下載股價：{symbol} ({i}/{len(symbols)})")
        try:
            price_map[symbol] = download_price_history(symbol, period=period)
        except Exception as e:
            st.warning(f"{symbol} 股價下載失敗：{e}")
    progress.empty()

    for _, r in out.iterrows():
        symbol = r.get("代碼", "")
        scan_date = pd.to_datetime(r.get("scan_date"), errors="coerce")
        entry_price = safe_float(r.get("entry_price"), default=0)
        hist = price_map.get(symbol)

        if scan_date is pd.NaT or hist is None or hist.empty or entry_price <= 0:
            result_rows.append(r)
            continue

        future = hist[hist.index > scan_date].head(max_days)
        if future.empty:
            result_rows.append(r)
            continue

        closes = future["Close"]
        highs = future["High"]
        lows = future["Low"]

        r["days_tracked"] = len(future)
        for d in [1, 3, 5, 10, 20]:
            if d <= max_days:
                r[f"return_{d}d%"] = calc_return_after_days(closes, entry_price, d)
        r["max_gain_5d%"] = calc_max_gain(highs.head(5), entry_price)
        r["max_drawdown_5d%"] = calc_max_drawdown(lows.head(5), entry_price)
        r["max_gain_10d%"] = calc_max_gain(highs.head(10), entry_price)
        r["max_drawdown_10d%"] = calc_max_drawdown(lows.head(10), entry_price)
        r["max_gain_20d%"] = calc_max_gain(highs.head(20), entry_price)
        r["max_drawdown_20d%"] = calc_max_drawdown(lows.head(20), entry_price)
        r["is_success_5d"] = classify_success(
            safe_float(r.get("max_gain_5d%", 0)),
            safe_float(r.get("max_drawdown_5d%", 0)),
            safe_float(r.get("return_5d%", 0)),
        )
        r["status"] = "done" if len(future) >= 10 else "tracking"
        result_rows.append(r)

    updated = pd.DataFrame(result_rows)
    return updated


# =========================
# Chart helpers
# =========================
def calc_kd(df: pd.DataFrame, n: int = 9) -> pd.DataFrame:
    """Calculate stochastic K/D lines with 9-day RSV and EWMA smoothing."""
    out = df.copy()
    low_n = out["Low"].rolling(n, min_periods=1).min()
    high_n = out["High"].rolling(n, min_periods=1).max()
    denom = (high_n - low_n).replace(0, pd.NA)
    out["RSV"] = ((out["Close"] - low_n) / denom * 100).fillna(50)
    out["K"] = out["RSV"].ewm(alpha=1/3, adjust=False).mean()
    out["D"] = out["K"].ewm(alpha=1/3, adjust=False).mean()
    return out


def add_moving_averages(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for n in [5, 10, 20, 60]:
        out[f"MA{n}"] = out["Close"].rolling(n, min_periods=1).mean()
    return out


def build_60bar_signal_window(symbol: str, scan_date: str, bars: int = 60, period: str = "1y") -> pd.DataFrame:
    """Return exactly up to 60 trading bars around the signal date.

    Priority:
    - Keep roughly 20 bars before the signal and 39 bars after the signal.
    - If future bars are not enough, fill with earlier bars.
    - If earlier bars are not enough, fill with later bars.
    """
    hist = download_price_history(symbol, period=period)
    if hist.empty:
        return pd.DataFrame()

    sd = pd.to_datetime(scan_date)
    hist = hist.sort_index().copy()

    # First position whose date is >= signal date. If signal date is not trading day, use next trading day.
    pos_candidates = [i for i, dt in enumerate(hist.index) if dt >= sd]
    if pos_candidates:
        sig_pos = pos_candidates[0]
    else:
        sig_pos = len(hist) - 1

    before_target = 20
    after_target = bars - before_target - 1
    start_pos = max(0, sig_pos - before_target)
    end_pos = min(len(hist), sig_pos + after_target + 1)

    # Fill shortage to keep 60 bars when possible.
    shortage = bars - (end_pos - start_pos)
    if shortage > 0:
        extra_start = max(0, start_pos - shortage)
        shortage -= start_pos - extra_start
        start_pos = extra_start
    if shortage > 0:
        end_pos = min(len(hist), end_pos + shortage)

    window = hist.iloc[start_pos:end_pos].copy()
    window = add_moving_averages(window)
    window = calc_kd(window)
    window["relative_bar"] = range(len(window))
    window["is_signal_bar"] = window.index >= sd
    return window


def find_swing_highs(df: pd.DataFrame, left: int = 2, right: int = 2) -> List[Tuple[int, pd.Timestamp, float]]:
    highs: List[Tuple[int, pd.Timestamp, float]] = []
    if df.empty or "High" not in df.columns:
        return highs
    for i in range(left, len(df) - right):
        val = float(df["High"].iloc[i])
        prev_max = float(df["High"].iloc[i-left:i].max())
        next_max = float(df["High"].iloc[i+1:i+right+1].max())
        if val >= prev_max and val >= next_max:
            highs.append((i, df.index[i], val))
    return highs


def detect_descending_trendline(df: pd.DataFrame, scan_date: str) -> Optional[Dict[str, Any]]:
    """Find a simple descending resistance line from two descending swing highs.

    Uses swing highs before or on signal date when possible. Falls back to all displayed bars.
    """
    if df.empty or len(df) < 8:
        return None
    sd = pd.to_datetime(scan_date)
    base = df[df.index <= sd]
    if len(base) < 8:
        base = df
    swings = find_swing_highs(base, left=2, right=2)
    if len(swings) < 2:
        # fallback: use top historical highs in chronological order if still descending
        candidate_idx = base["High"].nlargest(min(5, len(base))).index.tolist()
        swings = sorted([(base.index.get_loc(idx), idx, float(base.loc[idx, "High"])) for idx in candidate_idx], key=lambda x: x[0])

    best = None
    # Prefer wider, descending pair.
    for i in range(len(swings)):
        for j in range(i + 1, len(swings)):
            p1 = swings[i]
            p2 = swings[j]
            if p2[0] <= p1[0]:
                continue
            if p2[2] < p1[2]:
                width = p2[0] - p1[0]
                drop = p1[2] - p2[2]
                score = width * max(drop, 0.01)
                if best is None or score > best["score"]:
                    best = {"p1": p1, "p2": p2, "score": score}
    if best is None:
        return None

    p1 = best["p1"]
    p2 = best["p2"]
    slope = (p2[2] - p1[2]) / max((p2[0] - p1[0]), 1)
    x0 = 0
    x1 = len(df) - 1
    y0 = p1[2] + slope * (x0 - p1[0])
    y1 = p1[2] + slope * (x1 - p1[0])
    return {
        "x": [df.index[x0], df.index[x1]],
        "y": [y0, y1],
        "p1_date": p1[1],
        "p1_val": p1[2],
        "p2_date": p2[1],
        "p2_val": p2[2],
        "slope": slope,
    }


def detect_horizontal_resistance(df: pd.DataFrame, scan_date: str) -> Optional[Dict[str, Any]]:
    """Use highest high before/on signal date as a horizontal resistance line."""
    if df.empty:
        return None
    sd = pd.to_datetime(scan_date)
    base = df[df.index <= sd]
    if base.empty:
        base = df
    idx = base["High"].idxmax()
    level = float(base.loc[idx, "High"])
    return {"level": level, "date": idx}


def show_price_chart(row: pd.Series) -> None:
    symbol = row.get("代碼", "")
    scan_date = row.get("scan_date", "")
    entry_price = safe_float(row.get("entry_price", 0))
    stock_name = row.get("股票名稱", "")

    st.markdown("#### 圖表設定")
    ma_cols = st.columns(4)
    with ma_cols[0]:
        show_ma5 = st.checkbox("5MA", value=False, key=f"ma5_{symbol}_{scan_date}")
    with ma_cols[1]:
        show_ma10 = st.checkbox("10MA", value=False, key=f"ma10_{symbol}_{scan_date}")
    with ma_cols[2]:
        show_ma20 = st.checkbox("20MA", value=False, key=f"ma20_{symbol}_{scan_date}")
    with ma_cols[3]:
        show_ma60 = st.checkbox("60MA", value=True, key=f"ma60_{symbol}_{scan_date}")

    trend = build_60bar_signal_window(symbol, scan_date, bars=60, period="1y")
    if trend.empty:
        st.info("此股票目前沒有足夠股價資料可畫圖。")
        return

    if go is None or make_subplots is None:
        st.line_chart(trend[["Close", "MA60"]])
        st.line_chart(trend[["K", "D"]])
        return

    fig = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.06,
        row_heights=[0.72, 0.28],
        subplot_titles=(f"{symbol} {stock_name}｜60個交易日K線 / 均線 / 趨勢線", "KD 指標"),
    )

    # Taiwan convention: close up = red, close down = green.
    fig.add_trace(
        go.Candlestick(
            x=trend.index,
            open=trend["Open"],
            high=trend["High"],
            low=trend["Low"],
            close=trend["Close"],
            name="K線",
            increasing=dict(line=dict(color="#ef4444", width=1.4), fillcolor="rgba(239,68,68,0.55)"),
            decreasing=dict(line=dict(color="#10b981", width=1.4), fillcolor="rgba(16,185,129,0.55)"),
        ),
        row=1,
        col=1,
    )

    ma_config = [
        ("MA5", show_ma5, "#f97316"),
        ("MA10", show_ma10, "#8b5cf6"),
        ("MA20", show_ma20, "#0ea5e9"),
        ("MA60", show_ma60, "#111827"),
    ]
    for ma_name, enabled, color in ma_config:
        if enabled and ma_name in trend.columns:
            fig.add_trace(
                go.Scatter(x=trend.index, y=trend[ma_name], mode="lines", name=ma_name, line=dict(color=color, width=1.8)),
                row=1,
                col=1,
            )

    # Entry / signal lines
    if entry_price > 0:
        fig.add_hline(y=entry_price, line_dash="dot", line_color="#6366f1", annotation_text=f"訊號價 {entry_price}", row=1, col=1)
    fig.add_vline(x=pd.to_datetime(scan_date), line_dash="dash", line_color="#ef4444", annotation_text="訊號日", row=1, col=1)

    # Descending trendline and horizontal resistance
    trendline = detect_descending_trendline(trend, scan_date)
    if trendline:
        fig.add_trace(
            go.Scatter(
                x=trendline["x"],
                y=trendline["y"],
                mode="lines",
                name="下降趨勢線",
                line=dict(color="#dc2626", width=2, dash="dash"),
            ),
            row=1,
            col=1,
        )
        fig.add_trace(
            go.Scatter(
                x=[trendline["p1_date"], trendline["p2_date"]],
                y=[trendline["p1_val"], trendline["p2_val"]],
                mode="markers",
                name="趨勢高點",
                marker=dict(color="#dc2626", size=8, symbol="circle"),
            ),
            row=1,
            col=1,
        )

    horizontal = detect_horizontal_resistance(trend, scan_date)
    if horizontal:
        fig.add_hline(
            y=horizontal["level"],
            line_dash="dot",
            line_color="#2563eb",
            annotation_text=f"水平壓力 {horizontal['level']:.2f}",
            row=1,
            col=1,
        )

    # KD panel
    fig.add_trace(
        go.Scatter(x=trend.index, y=trend["K"], mode="lines", name="K", line=dict(color="#f59e0b", width=1.8)),
        row=2,
        col=1,
    )
    fig.add_trace(
        go.Scatter(x=trend.index, y=trend["D"], mode="lines", name="D", line=dict(color="#3b82f6", width=1.8)),
        row=2,
        col=1,
    )
    fig.add_hline(y=80, line_dash="dot", line_color="#94a3b8", row=2, col=1)
    fig.add_hline(y=20, line_dash="dot", line_color="#94a3b8", row=2, col=1)

    fig.update_layout(
        title=f"{symbol} {stock_name}｜訊號後股價走勢",
        height=780,
        margin=dict(l=20, r=20, t=75, b=25),
        xaxis_rangeslider_visible=False,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        hovermode="x unified",
    )
    fig.update_yaxes(title_text="價格", row=1, col=1)
    fig.update_yaxes(title_text="KD", range=[0, 100], row=2, col=1)
    st.plotly_chart(fig, use_container_width=True)

    info_cols = st.columns(3)
    with info_cols[0]:
        st.caption(f"顯示K線數：{len(trend)} 個交易日")
    with info_cols[1]:
        if trendline:
            st.caption(f"下降趨勢線：{trendline['p1_date'].strftime('%Y-%m-%d')} {trendline['p1_val']:.2f} → {trendline['p2_date'].strftime('%Y-%m-%d')} {trendline['p2_val']:.2f}")
        else:
            st.caption("下降趨勢線：目前找不到明確的兩個下降高點")
    with info_cols[2]:
        if horizontal:
            st.caption(f"水平壓力：{horizontal['level']:.2f}（{horizontal['date'].strftime('%Y-%m-%d')} 高點）")


# =========================
# Optional subprocess runner
# =========================
def run_update_signal_tracking_script() -> None:
    script_path = Path("update_signal_tracking.py")
    if not script_path.exists():
        st.error("找不到 repo 根目錄的 update_signal_tracking.py。請確認檔案已放在根目錄。")
        return

    env = os.environ.copy()
    for key in [
        "GITHUB_TOKEN", "GITHUB_OWNER", "GITHUB_REPO", "GITHUB_BRANCH",
        "GITHUB_DATABASE_DIR", "LOCAL_DATABASE_DIR",
    ]:
        val = get_secret_or_default(key, "")
        if val:
            env[key] = val

    cmd = [sys.executable, str(script_path), "--download-github", "--upload-github"]
    with st.spinner("執行 update_signal_tracking.py 中..."):
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=420, env=env)
        except subprocess.TimeoutExpired:
            st.error("update_signal_tracking.py 執行逾時。")
            return
        except Exception as e:
            st.error(f"執行 update_signal_tracking.py 發生錯誤：{e}")
            return

    if result.returncode == 0:
        st.success("update_signal_tracking.py 執行完成。")
        st.code(result.stdout or "完成，但沒有 stdout。", language="bash")
    else:
        st.error("update_signal_tracking.py 執行失敗。")
        st.code(result.stderr or result.stdout, language="bash")


# =========================
# UI
# =========================
st.title("📈 訊號追蹤分析")
st.caption("分析 GitHub Database 內所有 signal_tracking*.csv，更新訊號後 1/3/5/10/20 日走勢與績效。")

cfg = github_config()
with st.sidebar:
    st.header("GitHub 設定")
    st.write(f"Repo：`{cfg['owner']}/{cfg['repo']}`")
    st.write(f"Branch：`{cfg['branch']}`")
    st.write(f"Database：`{cfg['database_dir']}`")
    st.write(f"Token：{'✅ 已設定' if cfg['token'] else '⚠️ 未設定'}")
    st.divider()
    st.header("分析設定")
    period = st.selectbox("下載股價期間", ["3mo", "6mo", "1y", "2y"], index=1)
    max_days = st.selectbox("追蹤天數", [5, 10, 20], index=2)

col_a, col_b, col_c = st.columns([1.1, 1.2, 1.2])
with col_a:
    load_btn = st.button("📥 讀取 GitHub 全部追蹤檔", use_container_width=True)
with col_b:
    update_btn = st.button("🔄 讀取並更新股價績效", use_container_width=True)
with col_c:
    run_script_btn = st.button("▶️ 執行 update_signal_tracking.py", use_container_width=True)

if run_script_btn:
    run_update_signal_tracking_script()

if load_btn or update_btn or "tracking_df" not in st.session_state:
    try:
        with st.spinner("讀取 GitHub Database 中的 signal_tracking*.csv..."):
            tracking_df, loaded_files = load_all_tracking_from_github()
        st.session_state.tracking_df = tracking_df
        st.session_state.loaded_tracking_files = loaded_files
        if tracking_df.empty:
            st.warning("GitHub Database 內尚未找到 signal_tracking*.csv。")
        else:
            st.success(f"已讀取 {len(loaded_files)} 個追蹤檔，合併去重後 {len(tracking_df)} 筆訊號。")
    except Exception as e:
        st.error(f"讀取失敗：{e}")

if update_btn and "tracking_df" in st.session_state and not st.session_state.tracking_df.empty:
    with st.spinner("更新訊號後股價績效..."):
        updated_df = update_forward_performance(st.session_state.tracking_df, max_days=max_days, period=period)
    st.session_state.tracking_df = updated_df
    st.success("股價績效更新完成。")

tracking_df = st.session_state.get("tracking_df", pd.DataFrame())
loaded_files = st.session_state.get("loaded_tracking_files", [])

if tracking_df.empty:
    st.stop()

with st.expander("已讀取檔案", expanded=False):
    st.write(loaded_files)

# Filters
st.subheader("篩選條件")
f1, f2, f3, f4 = st.columns(4)
with f1:
    min_score = st.number_input("最低訊號分數", min_value=0.0, max_value=100.0, value=0.0, step=5.0)
with f2:
    grades = sorted([x for x in tracking_df.get("追蹤等級", pd.Series(dtype=str)).dropna().unique().tolist() if str(x)])
    selected_grades = st.multiselect("追蹤等級", grades, default=grades)
with f3:
    signal_text = st.text_input("訊號類型包含", value="")
with f4:
    symbols_text = st.text_input("指定代碼，多檔逗號分隔", value="")

filtered = tracking_df.copy()
if "訊號分數" in filtered.columns:
    filtered = filtered[filtered["訊號分數"].fillna(0).astype(float) >= float(min_score)]
if selected_grades and "追蹤等級" in filtered.columns:
    filtered = filtered[filtered["追蹤等級"].isin(selected_grades)]
if signal_text.strip() and "訊號類型" in filtered.columns:
    filtered = filtered[filtered["訊號類型"].astype(str).str.contains(signal_text.strip(), na=False)]
if symbols_text.strip():
    symbols = [normalize_symbol(x) for x in symbols_text.replace("，", ",").split(",") if x.strip()]
    filtered = filtered[filtered["代碼"].isin(symbols)]

# KPI cards
k1, k2, k3, k4, k5 = st.columns(5)
k1.metric("訊號筆數", f"{len(filtered):,}")
k2.metric("股票數", f"{filtered['代碼'].nunique():,}" if "代碼" in filtered.columns else "0")
if "return_5d%" in filtered.columns:
    k3.metric("5D平均報酬", f"{filtered['return_5d%'].dropna().mean():.2f}%" if filtered["return_5d%"].notna().any() else "-")
else:
    k3.metric("5D平均報酬", "未更新")
if "max_gain_5d%" in filtered.columns:
    k4.metric("5D平均最高漲幅", f"{filtered['max_gain_5d%'].dropna().mean():.2f}%" if filtered["max_gain_5d%"].notna().any() else "-")
else:
    k4.metric("5D平均最高漲幅", "未更新")
if "is_success_5d" in filtered.columns and filtered["is_success_5d"].notna().any():
    k5.metric("5D成功率", f"{filtered['is_success_5d'].mean():.2%}")
else:
    k5.metric("5D成功率", "未更新")

# Summary tables
st.subheader("績效摘要")
tab1, tab2, tab3, tab4 = st.tabs(["依追蹤等級", "依訊號類型", "依MA排列", "明細資料"])

def summary_group(df: pd.DataFrame, group_col: str) -> pd.DataFrame:
    if group_col not in df.columns:
        return pd.DataFrame()
    agg_dict: Dict[str, Tuple[str, str]] = {"筆數": ("代碼", "count")}
    for col in ["return_3d%", "return_5d%", "return_10d%", "max_gain_5d%", "max_drawdown_5d%", "is_success_5d"]:
        if col in df.columns:
            agg_dict[col] = (col, "mean")
    return df.groupby(group_col, dropna=False).agg(**agg_dict).reset_index().sort_values("筆數", ascending=False)

with tab1:
    st.dataframe(summary_group(filtered, "追蹤等級"), use_container_width=True)
with tab2:
    # explode signal types for better analysis
    if "訊號類型" in filtered.columns:
        exploded = filtered.copy()
        exploded["single_signal"] = exploded["訊號類型"].astype(str).str.split("、")
        exploded = exploded.explode("single_signal")
        exploded["single_signal"] = exploded["single_signal"].str.strip()
        st.dataframe(summary_group(exploded, "single_signal"), use_container_width=True)
    else:
        st.info("沒有訊號類型欄位。")
with tab3:
    st.dataframe(summary_group(filtered, "MA排列"), use_container_width=True)
with tab4:
    show_cols = [c for c in [
        "scan_date", "代碼", "股票名稱", "entry_price", "訊號分數", "追蹤等級", "訊號類型",
        "return_1d%", "return_3d%", "return_5d%", "return_10d%", "return_20d%",
        "max_gain_5d%", "max_drawdown_5d%", "max_gain_10d%", "max_drawdown_10d%",
        "MA位置", "MA排列", "RS加權報酬%", "source_file",
    ] if c in filtered.columns]
    st.dataframe(filtered[show_cols].sort_values(["scan_date", "訊號分數"], ascending=[False, False]), use_container_width=True)

# Upload/export
st.subheader("匯出 / 回寫")
out_csv = filtered.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
col_d1, col_d2 = st.columns(2)
with col_d1:
    st.download_button(
        "下載目前篩選結果 CSV",
        data=out_csv,
        file_name=f"tracking_analysis_{tw_now().strftime('%Y%m%d_%H%M%S')}.csv",
        mime="text/csv",
        use_container_width=True,
    )
with col_d2:
    if st.button("上傳目前更新結果到 GitHub 今日日期檔", use_container_width=True):
        github_path = f"{cfg['database_dir']}/signal_tracking_{tw_now().strftime('%Y%m%d')}.csv"
        upload_file_to_github(out_csv, github_path, f"Update tracking analysis {tw_now().strftime('%Y-%m-%d %H:%M:%S')}")

# Chart
st.subheader("個股訊號後走勢")
if filtered.empty:
    st.info("目前篩選條件下沒有資料。")
else:
    filtered_for_select = filtered.copy()
    filtered_for_select["label"] = filtered_for_select.apply(
        lambda r: f"{r.get('scan_date', '')}｜{r.get('代碼', '')}｜{r.get('股票名稱', '')}｜分數 {r.get('訊號分數', '')}｜{r.get('訊號類型', '')}",
        axis=1,
    )
    selected_label = st.selectbox("選擇一筆訊號", filtered_for_select["label"].tolist())
    selected_row = filtered_for_select[filtered_for_select["label"] == selected_label].iloc[0]
    show_price_chart(selected_row)
