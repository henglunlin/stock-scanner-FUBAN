"""
etf_db.py
=========
主動式ETF持股 / 每日異動資料庫存取工具 (獨立資料庫: etf_holdings.db)

刻意跟 twse_ohlcv.db 分開放，原因：
  1. twse_ohlcv.db 已經很大(~35MB)，職責是「個股OHLCV」，ETF持股是完全不同的
     資料領域，混在一起容易讓兩邊的排程/寫入互相影響 (例如 WAL 模式殘留問題)。
  2. 這個檔案的排程(update_etf_holdings.yml)跟 twse_ohlcv.db 的排程(update.yml)
     是兩條完全獨立的 GitHub Actions pipeline，分開的 db 檔案可以避免兩個 workflow
     同時 commit 同一個檔案造成 git 衝突。

資料表設計：
  etf_holdings        - 每日持股快照 (一列 = 某天某檔ETF持有某檔股票)
  etf_holding_changes  - 每日持股異動 (一列 = 某天某檔ETF對某檔股票的加碼/減碼/新納入/全數賣出)
  etf_fetch_log        - 每次抓取執行紀錄 (供網頁顯示「上次抓取狀態」、除錯用)

跟個股K線串接的方式：
  這個檔案完全不碰 twse_ohlcv.db，個股K線資料還是從 twse_ohlcv.db 讀。
  頁面(pages/7_📊_主動式ETF分析.py) 會同時開兩個資料庫連線，
  用「股票代碼(不含.TW/.TWO後綴的4碼)」+「日期」在 Python/pandas 這層做對應，
  不需要 SQL 跨資料庫 join。
"""
import os
import sqlite3
from typing import Optional

import pandas as pd


# --------------------------------------------------------------------------
# 連線與建表
# --------------------------------------------------------------------------
def get_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30.0)
    ensure_tables(conn)
    return conn


def ensure_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS etf_holdings (
            snapshot_date TEXT NOT NULL,
            etf_code TEXT NOT NULL,
            stock_code TEXT NOT NULL,
            stock_name TEXT,
            weight REAL,
            weight_text TEXT,
            shares REAL,
            shares_text TEXT,
            fetched_at TEXT,
            PRIMARY KEY (snapshot_date, etf_code, stock_code)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_holdings_etf_date ON etf_holdings(etf_code, snapshot_date)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_holdings_stock ON etf_holdings(stock_code)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS etf_holding_changes (
            change_date TEXT NOT NULL,
            etf_code TEXT NOT NULL,
            stock_code TEXT NOT NULL,
            stock_name TEXT,
            change_type TEXT,
            direction TEXT,
            weight_prev REAL,
            weight_curr REAL,
            weight_change REAL,
            shares_prev REAL,
            shares_curr REAL,
            shares_change REAL,
            compare_base_date TEXT,
            created_at TEXT,
            PRIMARY KEY (change_date, etf_code, stock_code)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_changes_etf_date ON etf_holding_changes(etf_code, change_date)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_changes_stock_date ON etf_holding_changes(stock_code, change_date)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_changes_date ON etf_holding_changes(change_date)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS etf_fetch_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT NOT NULL,
            etf_code TEXT NOT NULL,
            status TEXT NOT NULL,
            row_count INTEGER,
            message TEXT,
            created_at TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fetch_log_date_etf ON etf_fetch_log(run_date, etf_code)"
    )
    conn.commit()


# --------------------------------------------------------------------------
# 持股快照 (etf_holdings)
# --------------------------------------------------------------------------
def save_holdings_snapshot(
    db_path: str,
    etf_code: str,
    snapshot_date: str,
    holdings_df: pd.DataFrame,
    fetched_at: Optional[str] = None,
) -> int:
    """
    寫入某檔ETF在某天的完整持股快照(先刪除該ETF該天的舊資料再寫入，避免重複)。

    holdings_df 需含欄位：股票代碼, 股票名稱, 權重, 權重數值, 股數, 股數數值
    (即 standardize_holdings_for_compare() 的輸出格式)。
    """
    import datetime as _dt

    if fetched_at is None:
        fetched_at = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    records = []
    for _, row in holdings_df.iterrows():
        stock_code = str(row.get("股票代碼", "")).strip()
        if not stock_code:
            continue
        records.append((
            snapshot_date,
            etf_code,
            stock_code,
            row.get("股票名稱", ""),
            row.get("權重數值"),
            row.get("權重", ""),
            row.get("股數數值"),
            row.get("股數", ""),
            fetched_at,
        ))

    with sqlite3.connect(db_path, timeout=30.0) as conn:
        ensure_tables(conn)
        conn.execute(
            "DELETE FROM etf_holdings WHERE etf_code = ? AND snapshot_date = ?",
            (etf_code, snapshot_date),
        )
        if records:
            conn.executemany(
                """
                INSERT INTO etf_holdings
                    (snapshot_date, etf_code, stock_code, stock_name,
                     weight, weight_text, shares, shares_text, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                records,
            )
        conn.commit()
    return len(records)


def get_holdings_snapshot(conn: sqlite3.Connection, etf_code: str, snapshot_date: str) -> pd.DataFrame:
    q = """
        SELECT snapshot_date, etf_code, stock_code, stock_name, weight, weight_text,
               shares, shares_text, fetched_at
        FROM etf_holdings
        WHERE etf_code = ? AND snapshot_date = ?
        ORDER BY weight DESC
    """
    return pd.read_sql(q, conn, params=[etf_code, snapshot_date])


def get_available_snapshot_dates(conn: sqlite3.Connection, etf_code: Optional[str] = None) -> list:
    """回傳資料庫內已有持股快照的日期清單(新到舊)。"""
    if etf_code:
        q = "SELECT DISTINCT snapshot_date FROM etf_holdings WHERE etf_code = ? ORDER BY snapshot_date DESC"
        cur = conn.cursor()
        cur.execute(q, (etf_code,))
    else:
        q = "SELECT DISTINCT snapshot_date FROM etf_holdings ORDER BY snapshot_date DESC"
        cur = conn.cursor()
        cur.execute(q)
    return [r[0] for r in cur.fetchall()]


def get_previous_snapshot_date(conn: sqlite3.Connection, etf_code: str, current_date: str) -> Optional[str]:
    """找出資料庫裡「該ETF在 current_date 之前」最近一次有快照的日期，取代原本讀本機檔案的做法。"""
    q = """
        SELECT MAX(snapshot_date) FROM etf_holdings
        WHERE etf_code = ? AND snapshot_date < ?
    """
    cur = conn.cursor()
    cur.execute(q, (etf_code, current_date))
    row = cur.fetchone()
    return row[0] if row and row[0] else None


# --------------------------------------------------------------------------
# 每日異動 (etf_holding_changes)
# --------------------------------------------------------------------------
def save_holding_changes(db_path: str, etf_code: str, change_date: str, changes_df: pd.DataFrame) -> int:
    """
    寫入某檔ETF在 change_date 的異動明細(先刪除該ETF該天的舊異動資料再寫入)。

    changes_df 欄位參考 compare_etf_holdings() 的輸出：
    異動類型, 調整方向, 股票代碼, 股票名稱_昨日, 股票名稱_今日,
    權重_昨日, 權重_今日, 權重變化, 股數_昨日, 股數_今日, 股數變化, 比較基準檔案(這裡改存比較基準日期)
    """
    import datetime as _dt

    now_str = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _num(row, prev_key, curr_key):
        # 權重變化/股數變化欄位在 compare_etf_holdings() 裡已算成字串，這裡另外保留數值版本
        return None

    def _first_valid_name(*vals):
        # ⚠️ 2026-08-24修正：原本用 `row.get("股票名稱_今日") or row.get("股票名稱_昨日") or ""`，
        # 但當「股票名稱_今日」是pandas的NaN(浮點數)時，Python裡 `nan or x` 會回傳nan本身
        # (因為float('nan')本身是truthy的)，不會照預期退回去用「股票名稱_昨日」。
        # 這在「全數賣出」(股票今天已經不在持股裡，今日名稱欄位自然是NaN)這種情況下
        # 會導致存進資料庫的股票名稱是NaN，用實際資料測試(2026-08-21 00982A的7769
        # 鴻勁精密全數賣出)時發現「共同異動」清單裡這檔股票名稱顯示不出來。
        # 改成明確檢查NaN/空字串，才會正確退回去用昨日的名稱。
        for v in vals:
            if v is None:
                continue
            if isinstance(v, float) and pd.isna(v):
                continue
            s = str(v).strip()
            if s and s.lower() != "nan":
                return s
        return ""

    records = []
    for _, row in changes_df.iterrows():
        stock_code = str(row.get("股票代碼", "")).strip()
        if not stock_code:
            continue
        stock_name = _first_valid_name(row.get("股票名稱_今日"), row.get("股票名稱_昨日"))
        records.append((
            change_date,
            etf_code,
            stock_code,
            stock_name,
            row.get("異動類型", ""),
            row.get("調整方向", ""),
            row.get("_權重數值_昨日"),
            row.get("_權重數值_今日"),
            row.get("_權重變化數值"),
            row.get("_股數數值_昨日"),
            row.get("_股數數值_今日"),
            row.get("_股數變化數值"),
            row.get("比較基準日期", ""),
            now_str,
        ))

    with sqlite3.connect(db_path, timeout=30.0) as conn:
        ensure_tables(conn)
        conn.execute(
            "DELETE FROM etf_holding_changes WHERE etf_code = ? AND change_date = ?",
            (etf_code, change_date),
        )
        if records:
            conn.executemany(
                """
                INSERT INTO etf_holding_changes
                    (change_date, etf_code, stock_code, stock_name, change_type, direction,
                     weight_prev, weight_curr, weight_change,
                     shares_prev, shares_curr, shares_change,
                     compare_base_date, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                records,
            )
        conn.commit()
    return len(records)


def get_holding_changes(
    conn: sqlite3.Connection,
    change_date: Optional[str] = None,
    etf_code: Optional[str] = None,
    etf_codes: Optional[list] = None,
    stock_code: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> pd.DataFrame:
    """彈性查詢異動明細，供「指定ETF買賣狀況」與K線標記共用。"""
    q = "SELECT * FROM etf_holding_changes WHERE 1=1"
    params = []
    if change_date:
        q += " AND change_date = ?"
        params.append(change_date)
    if start_date:
        q += " AND change_date >= ?"
        params.append(start_date)
    if end_date:
        q += " AND change_date <= ?"
        params.append(end_date)
    if etf_code:
        q += " AND etf_code = ?"
        params.append(etf_code)
    if etf_codes:
        placeholders = ",".join("?" * len(etf_codes))
        q += f" AND etf_code IN ({placeholders})"
        params.extend(etf_codes)
    if stock_code:
        q += " AND stock_code = ?"
        params.append(stock_code)
    q += " ORDER BY change_date DESC, stock_code ASC"
    return pd.read_sql(q, conn, params=params)


def get_common_changes(
    conn: sqlite3.Connection,
    change_date: str,
    etf_codes: list,
    min_etf_count: int = 2,
) -> pd.DataFrame:
    """
    找出 change_date 當天，「異動的ETF數 >= min_etf_count」的股票清單，
    彙整每檔股票的加碼/減碼ETF清單、共同方向。取代原本 find_common_daily_changes()
    + build_common_daily_change_summary() 兩步，改成直接對資料庫查詢。
    """
    changes = get_holding_changes(conn, change_date=change_date, etf_codes=etf_codes)
    if changes.empty:
        return pd.DataFrame()

    bullish = {"加碼", "新納入"}
    bearish = {"減碼", "全數賣出"}

    rows = []
    for stock_code, group in changes.groupby("stock_code"):
        etf_involved = sorted(group["etf_code"].unique().tolist())
        if len(etf_involved) < min_etf_count:
            continue

        stock_name = ""
        for v in group["stock_name"].tolist():
            if v and str(v).strip():
                stock_name = v
                break

        add_list = []
        reduce_list = []
        directions = []
        for _, r in group.iterrows():
            direction = r.get("direction")
            if not direction:
                continue
            directions.append(direction)
            shares_change = r.get("shares_change")
            if shares_change is not None and not pd.isna(shares_change) and shares_change != 0:
                info = f"{r['etf_code']}({shares_change:+,.0f})"
            elif direction == "全數賣出":
                info = f"{r['etf_code']}(全數賣出)"
            elif direction == "新納入":
                info = f"{r['etf_code']}(新納入)"
            else:
                info = r["etf_code"]

            if direction in bullish:
                add_list.append(info)
            elif direction in bearish:
                reduce_list.append(info)

        if directions and all(d in bullish for d in directions):
            common_direction = "全部加碼"
        elif directions and all(d in bearish for d in directions):
            common_direction = "全部減碼"
        else:
            common_direction = "混合"

        rows.append({
            "股票代碼": stock_code,
            "股票名稱": stock_name,
            "異動ETF數": len(etf_involved),
            "異動ETF清單": "、".join(etf_involved),
            "共同方向": common_direction,
            "加碼ETF": "、".join(add_list) if add_list else "-",
            "減碼ETF": "、".join(reduce_list) if reduce_list else "-",
        })

    if not rows:
        return pd.DataFrame()

    result = pd.DataFrame(rows)
    result["_code_sort"] = pd.to_numeric(result["股票代碼"], errors="coerce")
    result = (
        result.sort_values(by=["異動ETF數", "_code_sort", "股票代碼"], ascending=[False, True, True])
        .drop(columns=["_code_sort"])
        .reset_index(drop=True)
    )
    return result


def get_stock_etf_events(
    conn: sqlite3.Connection,
    stock_code: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    etf_codes: Optional[list] = None,
) -> pd.DataFrame:
    """
    取得「單一股票」在指定期間內、被哪些ETF加碼/減碼過，供個股K線疊加標記使用。
    每一列 = 某天某檔ETF對這檔股票的一次異動。
    """
    return get_holding_changes(
        conn,
        stock_code=stock_code,
        start_date=start_date,
        end_date=end_date,
        etf_codes=etf_codes,
    ).sort_values("change_date")


def get_etf_held_stocks(conn: sqlite3.Connection, etf_code: str) -> pd.DataFrame:
    """
    取得「某檔ETF」在資料庫裡曾經出現過(任何一天快照裡)的全部持股清單，
    供頁面「選了ETF代碼後，股票代碼下拉選單只列出這檔ETF持有過的股票」做連動篩選用。

    回傳欄位: stock_code, stock_name (每檔股票一列，去重；股票名稱取最新一筆快照的名稱)
    """
    q = """
        SELECT stock_code, stock_name
        FROM etf_holdings
        WHERE etf_code = ?
        AND snapshot_date = (
            SELECT MAX(snapshot_date) FROM etf_holdings h2
            WHERE h2.etf_code = etf_holdings.etf_code AND h2.stock_code = etf_holdings.stock_code
        )
        ORDER BY stock_code
    """
    return pd.read_sql(q, conn, params=[etf_code])


# --------------------------------------------------------------------------
# 抓取執行紀錄 (etf_fetch_log)
# --------------------------------------------------------------------------
def log_fetch_run(db_path: str, run_date: str, etf_code: str, status: str, row_count: int = 0, message: str = "") -> None:
    import datetime as _dt

    now_str = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(db_path, timeout=30.0) as conn:
        ensure_tables(conn)
        conn.execute(
            """
            INSERT INTO etf_fetch_log (run_date, etf_code, status, row_count, message, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (run_date, etf_code, status, row_count, message, now_str),
        )
        conn.commit()


def get_latest_fetch_log(conn: sqlite3.Connection, limit: int = 50) -> pd.DataFrame:
    q = "SELECT * FROM etf_fetch_log ORDER BY id DESC LIMIT ?"
    return pd.read_sql(q, conn, params=[limit])
