"""
update_signal_tracking.py
=========================
更新台股掃描器 signal_tracking.csv 的後續績效，並可選擇上傳到 GitHub Database 目錄。

預設 GitHub 目標：
https://github.com/henglunlin/stock-scanner-FUBAN/tree/main/Database

需要套件：
    pip install pandas yfinance requests

建議環境變數或 .streamlit/secrets.toml：
    GITHUB_TOKEN = "github_pat_xxx"
    GITHUB_OWNER = "henglunlin"
    GITHUB_REPO = "stock-scanner-FUBAN"
    GITHUB_BRANCH = "main"
    GITHUB_DATABASE_DIR = "Database"

用法：
    python update_signal_tracking.py
    python update_signal_tracking.py --upload-github
    python update_signal_tracking.py --download-github --upload-github
"""

from __future__ import annotations

import argparse
import base64
import os
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd
import requests
import yfinance as yf


DEFAULT_OWNER = "henglunlin"
DEFAULT_REPO = "stock-scanner-FUBAN"
DEFAULT_BRANCH = "main"
DEFAULT_DATABASE_DIR = "Database"
DEFAULT_TRACKING_FILENAME = "signal_tracking.csv"


def load_toml_secrets() -> Dict[str, Any]:
    """讀取本機 .streamlit/secrets.toml，讓本機執行也可沿用 Streamlit secrets。"""
    possible_paths = [
        Path.cwd() / ".streamlit" / "secrets.toml",
        Path.home() / ".streamlit" / "secrets.toml",
    ]
    for secrets_path in possible_paths:
        if not secrets_path.exists():
            continue
        try:
            try:
                import tomllib  # Python 3.11+
            except ImportError:  # pragma: no cover
                import tomli as tomllib
            with open(secrets_path, "rb") as f:
                return tomllib.load(f)
        except Exception:
            return {}
    return {}


SECRETS = load_toml_secrets()


def get_config_value(key: str, default: str = "") -> str:
    """優先順序：環境變數 > secrets.toml > default。"""
    value = os.getenv(key)
    if value not in [None, ""]:
        return str(value)
    if key in SECRETS and SECRETS[key] not in [None, ""]:
        return str(SECRETS[key])
    return default


def github_config() -> Dict[str, str]:
    return {
        "token": get_config_value("GITHUB_TOKEN", ""),
        "owner": get_config_value("GITHUB_OWNER", DEFAULT_OWNER),
        "repo": get_config_value("GITHUB_REPO", DEFAULT_REPO),
        "branch": get_config_value("GITHUB_BRANCH", DEFAULT_BRANCH),
        "database_dir": get_config_value("GITHUB_DATABASE_DIR", DEFAULT_DATABASE_DIR).strip("/"),
    }


def local_database_dir() -> Path:
    return Path(get_config_value("LOCAL_DATABASE_DIR", DEFAULT_DATABASE_DIR))


def tracking_file_path() -> Path:
    return local_database_dir() / DEFAULT_TRACKING_FILENAME


def safe_float(x: Any, default: float = 0.0) -> float:
    try:
        if x in ["-", "", None]:
            return default
        if pd.isna(x):
            return default
        return float(x)
    except Exception:
        return default


def github_headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def github_contents_url(owner: str, repo: str, path: str) -> str:
    return f"https://api.github.com/repos/{owner}/{repo}/contents/{path.strip('/')}"


def download_tracking_from_github() -> bool:
    """從 GitHub Database/signal_tracking.csv 下載到本機 Database/signal_tracking.csv。"""
    cfg = github_config()
    token = cfg["token"]
    if not token:
        print("[WARN] GITHUB_TOKEN 未設定，略過 GitHub 下載。")
        return False

    github_path = f"{cfg['database_dir']}/{DEFAULT_TRACKING_FILENAME}"
    url = github_contents_url(cfg["owner"], cfg["repo"], github_path)
    res = requests.get(
        url,
        headers=github_headers(token),
        params={"ref": cfg["branch"]},
        timeout=20,
    )
    if res.status_code == 404:
        print(f"[INFO] GitHub 尚無追蹤檔：{github_path}")
        return False
    if res.status_code != 200:
        raise RuntimeError(f"GitHub 下載失敗：{res.status_code} {res.text}")

    payload = res.json()
    encoded = payload.get("content", "")
    if not encoded:
        raise RuntimeError("GitHub 回傳內容為空，無法下載追蹤檔。")

    data = base64.b64decode(encoded)
    local_database_dir().mkdir(parents=True, exist_ok=True)
    tracking_file_path().write_bytes(data)
    print(f"[OK] 已從 GitHub 下載：{github_path} -> {tracking_file_path()}")
    return True


def upload_file_to_github(file_bytes: bytes, github_path: str, commit_message: str) -> bool:
    cfg = github_config()
    token = cfg["token"]
    if not token:
        print("[WARN] GITHUB_TOKEN 未設定，略過 GitHub 上傳。")
        return False

    url = github_contents_url(cfg["owner"], cfg["repo"], github_path)
    headers = github_headers(token)

    sha: Optional[str] = None
    get_res = requests.get(url, headers=headers, params={"ref": cfg["branch"]}, timeout=20)
    if get_res.status_code == 200:
        sha = get_res.json().get("sha")
    elif get_res.status_code != 404:
        raise RuntimeError(f"讀取 GitHub 既有檔案失敗：{get_res.status_code} {get_res.text}")

    payload: Dict[str, Any] = {
        "message": commit_message,
        "content": base64.b64encode(file_bytes).decode("utf-8"),
        "branch": cfg["branch"],
    }
    if sha:
        payload["sha"] = sha

    put_res = requests.put(url, headers=headers, json=payload, timeout=30)
    if put_res.status_code not in [200, 201]:
        raise RuntimeError(f"上傳 GitHub 失敗：{put_res.status_code} {put_res.text}")

    html_url = put_res.json().get("content", {}).get("html_url", "")
    print(f"[OK] 已上傳到 GitHub：{html_url}")
    return True


def upload_tracking_to_github() -> bool:
    path = tracking_file_path()
    if not path.exists():
        print(f"[WARN] 找不到追蹤檔：{path}")
        return False
    cfg = github_config()
    github_path = f"{cfg['database_dir']}/{DEFAULT_TRACKING_FILENAME}"
    return upload_file_to_github(
        path.read_bytes(),
        github_path,
        "Update signal tracking performance",
    )


def calc_return_after_days(closes: pd.Series, entry_price: float, days: int) -> Optional[float]:
    if len(closes) < days:
        return None
    close_price = float(closes.iloc[days - 1])
    return round((close_price / entry_price - 1) * 100, 2)


def calc_max_gain(highs: pd.Series, entry_price: float) -> Optional[float]:
    if highs.empty:
        return None
    return round((float(highs.max()) / entry_price - 1) * 100, 2)


def calc_max_drawdown(lows: pd.Series, entry_price: float) -> Optional[float]:
    if lows.empty:
        return None
    return round((float(lows.min()) / entry_price - 1) * 100, 2)


def classify_success(max_gain: float, max_drawdown: float, close_return: float) -> int:
    """預設成功定義：5日內最高漲幅 >= 5%、最大回撤 > -5%、5日收盤報酬 >= 2%。"""
    if max_gain >= 5 and max_drawdown > -5 and close_return >= 2:
        return 1
    return 0


def normalize_yfinance_columns(hist: pd.DataFrame) -> pd.DataFrame:
    if isinstance(hist.columns, pd.MultiIndex):
        hist.columns = hist.columns.get_level_values(0)
    return hist


def update_tracking_result() -> pd.DataFrame:
    path = tracking_file_path()
    if not path.exists():
        raise FileNotFoundError(f"找不到追蹤檔：{path}。請先由 Streamlit 掃描器產生 signal_tracking.csv。")

    df = pd.read_csv(path, encoding="utf-8-sig")
    if df.empty:
        print("[INFO] tracking file is empty")
        return df

    required_cols = ["scan_date", "代碼", "entry_price"]
    for col in required_cols:
        if col not in df.columns:
            raise ValueError(f"追蹤檔缺少必要欄位：{col}")

    symbols = sorted(df["代碼"].dropna().astype(str).unique().tolist())
    price_map: Dict[str, pd.DataFrame] = {}

    for symbol in symbols:
        try:
            hist = yf.download(symbol, period="1mo", interval="1d", progress=False, auto_adjust=False)
            if hist.empty:
                print(f"[WARN] {symbol} 無 yfinance 資料")
                continue
            price_map[symbol] = normalize_yfinance_columns(hist)
        except Exception as e:
            print(f"[WARN] {symbol} 下載失敗：{e}")

    result_rows = []
    for _, r in df.iterrows():
        symbol = str(r["代碼"])
        scan_date = pd.to_datetime(r["scan_date"])
        entry_price = safe_float(r["entry_price"])
        hist = price_map.get(symbol)

        if hist is None or hist.empty or entry_price <= 0:
            result_rows.append(r)
            continue

        hist = hist.copy()
        hist.index = pd.to_datetime(hist.index).tz_localize(None)
        future = hist[hist.index > scan_date].head(10)

        if future.empty:
            result_rows.append(r)
            continue

        closes = future["Close"]
        highs = future["High"]
        lows = future["Low"]

        r["days_tracked"] = len(future)
        r["return_3d%"] = calc_return_after_days(closes, entry_price, 3)
        r["return_5d%"] = calc_return_after_days(closes, entry_price, 5)
        r["return_10d%"] = calc_return_after_days(closes, entry_price, 10)
        r["max_gain_5d%"] = calc_max_gain(highs.head(5), entry_price)
        r["max_drawdown_5d%"] = calc_max_drawdown(lows.head(5), entry_price)
        r["max_gain_10d%"] = calc_max_gain(highs.head(10), entry_price)
        r["max_drawdown_10d%"] = calc_max_drawdown(lows.head(10), entry_price)
        r["is_success_5d"] = classify_success(
            safe_float(r.get("max_gain_5d%", 0)),
            safe_float(r.get("max_drawdown_5d%", 0)),
            safe_float(r.get("return_5d%", 0)),
        )
        r["status"] = "done" if len(future) >= 10 else "tracking"
        result_rows.append(r)

    out = pd.DataFrame(result_rows)
    local_database_dir().mkdir(parents=True, exist_ok=True)
    out.to_csv(path, index=False, encoding="utf-8-sig")
    print(f"[OK] tracking updated：{path}")

    done = out[out.get("status", "") == "done"].copy()
    if not done.empty and "is_success_5d" in done.columns:
        print("\n===== 追蹤績效摘要 =====")
        print(f"整體 5D 成功率：{done['is_success_5d'].mean():.2%} / 樣本數：{len(done)}")
        if "追蹤等級" in done.columns:
            print("\n依追蹤等級：")
            print(done.groupby("追蹤等級")["is_success_5d"].agg(["count", "mean"]))
        if "MA排列" in done.columns:
            print("\n依 MA 排列：")
            print(done.groupby("MA排列")["is_success_5d"].agg(["count", "mean"]))
        if "MA位置" in done.columns:
            print("\n依 MA 位置：")
            print(done.groupby("MA位置")["is_success_5d"].agg(["count", "mean"]))

    return out


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="更新台股掃描器 signal_tracking.csv 追蹤績效")
    parser.add_argument("--download-github", action="store_true", help="更新前先從 GitHub Database 下載 signal_tracking.csv")
    parser.add_argument("--upload-github", action="store_true", help="更新完成後上傳 signal_tracking.csv 到 GitHub Database")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.download_github:
        download_tracking_from_github()
    update_tracking_result()
    if args.upload_github:
        upload_tracking_to_github()


if __name__ == "__main__":
    main()
