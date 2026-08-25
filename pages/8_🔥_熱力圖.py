"""
8_🔥_熱力圖.py
================
台股熱力圖：族群 × 漲跌幅/資金流向 × 大小(市值或成交金額)

資料來源:
  - 族群: stock_groups.json (自訂，多對多標籤式) 或 stock_meta 資料表的官方產業別
  - 大小: stock_meta.SharesOutstanding × 現價 = 市值；沒有股本資料則退回用成交金額
  - 顏色: 漲跌幅 / institutional_trading 資料表的三大法人買賣超 / OBV 資金流向代理指標
  - 價格: 沿用 common_fubon.py 既有的「本地DB / Yfinance / 富邦WebSocket」三選一架構，
    跟主掃描器、模擬器共用同一套價格來源邏輯，不重新寫一套。

執行方式: 屬於多頁應用程式的其中一頁，跟著 streamlit run Home.py（或主入口）自動出現在側邊欄。
"""
import json
import os
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import streamlit as st
from streamlit_echarts import st_pyecharts
from pyecharts.charts import TreeMap
from pyecharts import options as opts
from pyecharts.commons.utils import JsCode

import common_fubon as cf
import heatmap_utils

st.set_page_config(page_title="台股熱力圖", layout="wide")
st.title("🔥 台股熱力圖")

DB_PATH = "twse_ohlcv.db"
GROUPS_FILE = "stock_groups.json"
LIVE_MODE_WARN_THRESHOLD = 300  # 即時模式下逐檔打 API，股票數太多時提醒改用收盤模式
LABEL_MIN_AREA_FRACTION = 0.0012  # 格子面積佔整張熱力圖不到這個比例時，不顯示文字，只留顏色（改善2）

cf.ensure_fubon_session_state()
cf.render_fubon_login_sidebar()

tw_now = datetime.now(ZoneInfo("Asia/Taipei"))
active_source = cf.render_price_source_selector(tw_now)


# --------------------------------------------------------------------------
# 資料載入 (族群 / 公司基本資料 / 法人買賣超)
# --------------------------------------------------------------------------
@st.cache_data(ttl=3600)
def load_custom_groups() -> dict:
    if not os.path.exists(GROUPS_FILE):
        return {}
    with open(GROUPS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


@st.cache_data(ttl=3600)
def load_stock_meta() -> pd.DataFrame:
    if not os.path.exists(DB_PATH):
        return pd.DataFrame()
    try:
        with sqlite3.connect(DB_PATH) as conn:
            return pd.read_sql("SELECT * FROM stock_meta", conn)
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=1800)
def load_institutional_latest() -> pd.DataFrame:
    if not os.path.exists(DB_PATH):
        return pd.DataFrame()
    try:
        with sqlite3.connect(DB_PATH) as conn:
            latest = pd.read_sql(
                "SELECT MAX(Date) AS d FROM institutional_trading", conn
            )["d"].iloc[0]
            if not latest:
                return pd.DataFrame()
            return pd.read_sql(
                "SELECT * FROM institutional_trading WHERE Date = ?", conn, params=[latest]
            )
    except Exception:
        return pd.DataFrame()


custom_groups = load_custom_groups()
stock_meta = load_stock_meta()
insti_latest = load_institutional_latest()

with st.sidebar:
    st.markdown("---")
    st.subheader("🔥 熱力圖設定")
    group_source = st.radio(
        "族群來源", ["自訂族群 (stock_groups.json)", "TWSE 官方產業別"], key="hm_group_source"
    )
    color_metric = st.radio(
        "顏色依據", ["漲跌幅 (%)", "三大法人買賣超", "資金流向代理指標 (OBV)"], key="hm_color_metric"
    )
    data_mode = st.radio("資料模式", ["收盤資料 (較快)", "盤中即時 (較慢)"], key="hm_data_mode")
    size_metric = st.radio("方塊大小", ["市值 (股本 × 股價)", "成交金額"], key="hm_size_metric")
    min_group_size = st.slider(
        "族群最少股票數（改善5）", min_value=1, max_value=10, value=3, key="hm_min_group_size",
        help="族群裡股票數低於這個數字時，會被合併進「其他」分類，避免畫面被切成一堆看不清楚的碎格子。",
    )


# --------------------------------------------------------------------------
# 決定掃描範圍: Group -> [SecurityCode, ...]
# 自訂族群沿用掃描器主程式原本的邏輯：一檔股票可以同時出現在多個族群，
# 不強迫只能歸屬一個族群（stock_groups.json 本身就是多對多標籤式設計）。
# --------------------------------------------------------------------------
if group_source.startswith("自訂"):
    group_map = {g: [str(s).split(".")[0] for s in syms] for g, syms in custom_groups.items()}
    if not group_map:
        st.warning(f"讀不到 {GROUPS_FILE}，請確認檔案存在於同一個資料夾。")
else:
    if stock_meta.empty:
        st.warning("找不到 stock_meta 資料表，請先在有網路的環境執行一次 `python fetch_stock_meta.py` 建立官方產業別資料。")
        group_map = {}
    else:
        group_map = {
            industry: sub["SecurityCode"].tolist()
            for industry, sub in stock_meta.groupby("IndustryName")
        }

all_codes = sorted({c for codes in group_map.values() for c in codes})

if not all_codes:
    st.stop()

# 改善5：股票數太少的族群合併成「其他」，避免畫面被切成一堆看不清楚的碎格子
if group_map:
    merged_group_map, leftover_codes = {}, []
    for g, codes in group_map.items():
        if len(codes) < min_group_size:
            leftover_codes.extend(codes)
        else:
            merged_group_map[g] = codes
    if leftover_codes:
        merged_group_map["其他"] = sorted(set(leftover_codes))
    group_map = merged_group_map

if data_mode.startswith("盤中即時") and len(all_codes) > LIVE_MODE_WARN_THRESHOLD:
    st.warning(
        f"目前範圍共 {len(all_codes)} 檔股票，即時模式是逐檔打報價 API，數量較多時會偏慢、"
        f"也容易被來源端限流。建議：TWSE 全官方族群模式先用「收盤資料」，"
        f"想看盤中即時變化時改用範圍較小的「自訂族群」。"
    )

st.caption(
    f"掃描範圍：{len(group_map)} 個族群、共 {len(all_codes)} 檔股票"
    f"（{data_mode}，價格來源：{active_source}）"
)


# --------------------------------------------------------------------------
# 抓價格資料
# --------------------------------------------------------------------------
today_str = tw_now.strftime("%Y-%m-%d")


@st.cache_data(ttl=1800)
def bulk_close_mode(symbols: tuple) -> dict:
    """收盤模式：一次從本地 DB 撈全部股票近期資料，取最後兩天算漲跌幅，速度快很多。"""
    return cf.bulk_download_db_history(symbols, today_str)


symbols = tuple(f"{c}.TW" for c in all_codes)

if data_mode.startswith("收盤"):
    price_data = {sym.split(".")[0]: df for sym, df in bulk_close_mode(symbols).items()}
else:
    yf_today_map = (
        cf.bulk_download_yfinance_today(symbols, today_str) if active_source == "Yfinance" else {}
    )
    price_data = {}
    progress = st.progress(0.0, text="正在抓取即時價格...")
    for i, code in enumerate(all_codes):
        sym = f"{code}.TW"
        try:
            df = cf.download_stock_data_by_source(
                sym, st.session_state.fubon_sdk, active_source, today_str, yf_today_map=yf_today_map,
            )
        except Exception:
            df = pd.DataFrame()
        price_data[code] = df
        if i % 20 == 0:
            progress.progress(min(1.0, i / max(1, len(all_codes))), text=f"正在抓取即時價格... ({i}/{len(all_codes)})")
    progress.empty()


# --------------------------------------------------------------------------
# 組成每檔股票的漲跌幅 / 成交金額 / 市值 / 資金流向欄位
# --------------------------------------------------------------------------
insti_map = insti_latest.set_index("SecurityCode")["TotalNet"].to_dict() if not insti_latest.empty else {}
meta_map = stock_meta.set_index("SecurityCode").to_dict("index") if not stock_meta.empty else {}

rows = []
for code in all_codes:
    df = price_data.get(code)
    if df is None or df.empty or len(df) < 2:
        continue

    pct = heatmap_utils.pct_change_today(df)
    last_close = float(df["Close"].iloc[-1]) if "Close" in df.columns else 0.0
    last_vol = float(df["Volume"].iloc[-1]) if "Volume" in df.columns else 0.0
    trade_value = last_close * last_vol * 1000  # Volume 單位為「張」，1張=1000股

    meta = meta_map.get(code, {})
    shares = meta.get("SharesOutstanding") or 0.0
    market_cap = shares * last_close
    full_name = meta.get("SecurityName") or code
    short_name = meta.get("ShortName") or full_name

    rows.append({
        "SecurityCode": code,
        "ShortName": short_name,
        "FullName": full_name,
        "Close": last_close,
        "PctChange": pct,
        "TradeValue": trade_value,
        "MarketCap": market_cap if market_cap > 0 else trade_value,  # 沒股本資料就退回用成交金額頂替
        "MoneyFlowProxy": heatmap_utils.money_flow_proxy(df),
        "InstiNet": insti_map.get(code),
    })

df_all = pd.DataFrame(rows)
if df_all.empty:
    st.warning("沒有抓到任何股票的價格資料，請確認資料來源設定，或稍後再試一次。")
    st.stop()

# 展開成 (族群, 股票) 長表 —— 同一檔股票若屬於多個自訂族群，會在每個族群底下各出現一次
records = []
for group, codes in group_map.items():
    sub = df_all[df_all["SecurityCode"].isin(codes)]
    for _, r in sub.iterrows():
        rec = r.to_dict()
        rec["Group"] = group
        records.append(rec)

df_plot = pd.DataFrame(records)
if df_plot.empty:
    st.warning("目前族群清單裡的股票都沒有抓到資料。")
    st.stop()


# --------------------------------------------------------------------------
# 決定顏色欄位與大小欄位
# --------------------------------------------------------------------------
if color_metric.startswith("三大法人"):
    if insti_latest.empty:
        st.info("資料庫裡目前沒有三大法人買賣超資料，請先在有網路的環境執行 `python fetch_institutional_trading.py`，這裡暫時改用漲跌幅上色。")
        color_col, color_label = "PctChange", "漲跌幅(%)"
    else:
        df_plot["InstiNet"] = df_plot["InstiNet"].fillna(0)
        color_col, color_label = "InstiNet", "三大法人買賣超(股)"
elif color_metric.startswith("資金流向代理"):
    color_col, color_label = "MoneyFlowProxy", "資金流向代理分數(元)"
else:
    color_col, color_label = "PctChange", "漲跌幅(%)"

size_col = "MarketCap" if size_metric.startswith("市值") else "TradeValue"
df_plot[size_col] = df_plot[size_col].clip(lower=1)  # treemap 面積不可為 0 或負值

# 改善1：面積開根號壓縮 —— 台積電市值(~26兆)跟其他個股差了兩三個數量級，
# 純線性大小會讓它一檔就佔掉快一半畫面，其他族群全部被壓成看不清楚的細條。
# 開根號後，極端值跟中位數個股的「視覺面積差距」會被大幅壓縮，其他股票才有機會被看見。
df_plot["AreaValue"] = np.sqrt(df_plot[size_col])

# --------------------------------------------------------------------------
# 顏色計算：把 color_col 的值換算成 hex 色碼 (台股慣例：紅漲、綠跌、白色=持平)
# 上下限用「分位數」而不是最大/最小值 —— 少數離群值 (例如漲停/跌停個股) 才不會把
# 其他正常漲跌 1~2% 的股票全部拉成幾乎無色的白色。
# 改善4：t 再開根號一次 (非線性)，人眼對顏色深淺的感知本來就不是線性的，
# 這樣中段漲跌幅 (例如 1~3%) 也會有明顯可辨識的顏色差異，不用等到接近漲跌停才看得出紅綠差別。
# --------------------------------------------------------------------------
def color_scale_cap(series: pd.Series) -> float:
    cap = series.abs().quantile(0.9)
    if pd.isna(cap) or cap <= 0:
        cap = series.abs().max()
    return max(float(cap), 1e-9)


def pct_to_color(value: float, cap: float) -> str:
    v = max(-cap, min(cap, value))
    t = (abs(v) / cap) ** 0.5  # 開根號讓中段差異也看得出來 (改善4)
    if v >= 0:  # 紅
        r = int(255 - t * (255 - 192))
        g = int(255 - t * (255 - 57))
        b = int(255 - t * (255 - 43))
    else:  # 綠
        r = int(255 - t * (255 - 30))
        g = int(255 - t * (255 - 132))
        b = int(255 - t * (255 - 73))
    return f"#{r:02x}{g:02x}{b:02x}"


color_cap = color_scale_cap(df_plot[color_col])
df_plot["NodeColor"] = df_plot[color_col].apply(lambda v: pct_to_color(v, color_cap))

# 改善2：格子標籤改用「公司簡稱」而不是「股份有限公司」全名，全名留給 tooltip 顯示
df_plot["Label"] = df_plot.apply(
    lambda r: f"{r['SecurityCode']} {r['ShortName']}\n{r['PctChange']:+.2f}%", axis=1
)

# 改善2（續）：格子面積佔整張圖不到 LABEL_MIN_AREA_FRACTION 的，不顯示文字，只留顏色，
# 避免小格子文字擠在一起、被截斷看不清楚；完整資訊留給 tooltip (改善6) 補回來。
total_area = df_plot["AreaValue"].sum()
df_plot["ShowLabel"] = (df_plot["AreaValue"] / total_area) >= LABEL_MIN_AREA_FRACTION


# --------------------------------------------------------------------------
# 組成 ECharts Treemap 要的巢狀資料結構: [{name, children:[{name, value, itemStyle, label, ...}]}]
# 每個葉節點額外帶上 code / fullName / close / pctChange / 金額顯示字串等欄位，
# 專門給下面的 tooltip formatter 用 —— 這樣 tooltip 顯示的是完整資訊，不會跟著格子標籤一起被截斷 (改善6)。
# --------------------------------------------------------------------------
def build_tree_data(df: pd.DataFrame, group_map: dict) -> list:
    tree = []
    for group in group_map.keys():
        sub = df[df["Group"] == group]
        if sub.empty:
            continue
        children = []
        for _, r in sub.iterrows():
            node = {
                "name": r["Label"],
                "value": round(float(r["AreaValue"]), 4),
                "itemStyle": {"color": r["NodeColor"]},
                "label": {
                    "show": bool(r["ShowLabel"]),
                    "color": "#ffffff",
                    "textBorderColor": "rgba(0,0,0,0.55)",
                    "textBorderWidth": 2,
                    "fontSize": 12,
                },
                # tooltip 專用的完整資訊欄位
                "code": r["SecurityCode"],
                "fullName": r["FullName"],
                "close": round(float(r["Close"]), 2),
                "pctChange": round(float(r["PctChange"]), 2),
                "marketCapDisplay": heatmap_utils.format_twd(r["MarketCap"]),
                "tradeValueDisplay": heatmap_utils.format_twd(r["TradeValue"]),
            }
            if pd.notna(r["InstiNet"]):
                node["instiNetDisplay"] = heatmap_utils.format_shares_as_lots(r["InstiNet"])
            children.append(node)
        tree.append({"name": group, "children": children})
    return tree


tree_data = build_tree_data(df_plot, group_map)

# 改善6：tooltip 用自訂 JS formatter，顯示不受格子大小限制的完整資訊
# (代碼、全名、現價、漲跌幅、市值、成交金額，三大法人買賣超則視資料是否存在才顯示)
tooltip_formatter = JsCode(
    """
    function (params) {
        var d = params.data;
        if (!d || d.code === undefined) { return params.name; }
        var pct = d.pctChange >= 0 ? ('+' + d.pctChange) : d.pctChange;
        var lines = [
            d.code + ' ' + d.fullName,
            '現價: ' + d.close,
            '漲跌幅: ' + pct + '%',
            '市值: ' + d.marketCapDisplay,
            '成交金額: ' + d.tradeValueDisplay
        ];
        if (d.instiNetDisplay) {
            lines.push('三大法人買賣超: ' + d.instiNetDisplay);
        }
        return lines.join('<br/>');
    }
    """
)

treemap = (
    TreeMap(init_opts=opts.InitOpts(bg_color="#ffffff"))  # 改善3：固定白色背景，不受 App 深色主題影響
    .add(
        series_name="heatmap",
        data=tree_data,
        pos_top="0%", pos_bottom="0%", pos_left="0%", pos_right="0%",
        width="100%", height="100%",
        leaf_depth=2,
        roam=False,
        breadcrumb_opts=opts.TreeMapBreadcrumbOpts(is_show=False),
        levels=[
            opts.TreeMapLevelsOpts(),  # 根節點：不特別上色
            opts.TreeMapLevelsOpts(  # 族群節點：淺灰底 + 左上角族群名稱標題列，模仿 TradingView 的分組標頭
                treemap_itemstyle_opts=opts.TreeMapItemStyleOpts(
                    border_color="#f0f0f0", border_width=4, gap_width=4, color="#e8e8e8",
                ),
                upper_label_opts=opts.LabelOpts(
                    is_show=True, position="insideTopLeft", font_size=13, color="#333333",
                ),
            ),
            opts.TreeMapLevelsOpts(  # 個股節點
                treemap_itemstyle_opts=opts.TreeMapItemStyleOpts(border_color="#ffffff", border_width=1, gap_width=1),
            ),
        ],
        tooltip_opts=opts.TooltipOpts(formatter=tooltip_formatter),
    )
    .set_global_opts(
        title_opts=opts.TitleOpts(title=""),
        legend_opts=opts.LegendOpts(is_show=False),  # 改善3：關掉左上角「heatmap」圖例
    )
)

st_pyecharts(treemap, height="800px")

with st.expander("查看原始資料表"):
    show_cols = ["Group", "SecurityCode", "ShortName", "Close", "PctChange", "TradeValue", "MarketCap", "MoneyFlowProxy", "InstiNet"]
    st.dataframe(
        df_plot[show_cols].sort_values("PctChange", ascending=False),
        use_container_width=True, hide_index=True,
    )
