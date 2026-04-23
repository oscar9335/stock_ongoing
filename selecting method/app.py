# -*- coding: utf-8 -*-
"""
台股資金流向 & 類股輪動 Dashboard
主程式：Streamlit UI，6 個分頁
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from config import (
    CONCEPT_GROUPS, STAGE_COLORS, STAGE_LABELS_ZH,
    TTL_INTRADAY, TTL_HISTORICAL, THRESHOLDS,
)
from data_fetcher import (
    FetchResult,
    fetch_all_stocks_today,
    fetch_concept_group_prices,
    fetch_institutional_investors,
    fetch_market_total_turnover,
    fetch_price_history,
    fetch_sector_indices,
    fetch_taiex_history,
    get_last_trading_day,
    map_stocks_to_sectors,
)
from analyzer import (
    build_sector_leader_table,
    compute_momentum_scores,
    compute_sector_momentum,
    compute_sector_money_flow,
    compute_sector_money_flow_history,
    identify_sector_stages,
    run_all_anomaly_detection,
)

logging.basicConfig(level=logging.WARNING)

# ── 頁面基本設定 ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="台股資金流向 Dashboard",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── 自訂 CSS ──────────────────────────────────────────────────────────────────
st.markdown("""
<style>
    .alert-high   { border-left: 4px solid #FF3333; padding: 8px 12px; background: #fff0f0; border-radius: 4px; margin-bottom: 8px; }
    .alert-medium { border-left: 4px solid #FF9900; padding: 8px 12px; background: #fff8e6; border-radius: 4px; margin-bottom: 8px; }
    .alert-low    { border-left: 4px solid #FFD700; padding: 8px 12px; background: #fffde6; border-radius: 4px; margin-bottom: 8px; }
    .badge-ok     { background: #00CC44; color: white; border-radius: 12px; padding: 2px 8px; font-size: 0.75em; }
    .badge-warn   { background: #FF9900; color: white; border-radius: 12px; padding: 2px 8px; font-size: 0.75em; }
    .badge-err    { background: #FF3333; color: white; border-radius: 12px; padding: 2px 8px; font-size: 0.75em; }
    .kpi-value    { font-size: 1.6em; font-weight: bold; }
    .kpi-label    { font-size: 0.85em; color: #888; }
</style>
""", unsafe_allow_html=True)


# ── 快取資料載入 ───────────────────────────────────────────────────────────────

@st.cache_data(ttl=TTL_INTRADAY, show_spinner="正在載入市場資料…")
def load_intraday_data() -> dict[str, FetchResult]:
    """一次載入所有即時資料，避免切換 Tab 重複請求。"""
    sector_idx   = fetch_sector_indices()
    stocks_today = fetch_all_stocks_today()
    market_total = fetch_market_total_turnover()
    institutional = fetch_institutional_investors()
    return {
        "sector_idx":    sector_idx,
        "stocks_today":  stocks_today,
        "market_total":  market_total,
        "institutional": institutional,
    }


@st.cache_data(ttl=TTL_HISTORICAL, show_spinner="正在載入歷史價格資料…")
def load_historical_data(tickers: tuple[str, ...], period: str = "1y") -> dict[str, FetchResult]:
    """歷史價格（含大盤）快取 24 小時。tickers 需為 tuple（可雜湊）。"""
    price_hist = fetch_price_history(list(tickers), period=period)
    taiex_hist = fetch_taiex_history(period=period)
    return {"price_hist": price_hist, "taiex_hist": taiex_hist}


def _all_tickers(stocks_df: pd.DataFrame) -> tuple[str, ...]:
    """
    只收集概念股 ticker（約 15 支），避免下載全市場 1500+ 支個股導致頁面卡住。
    動能/階段分析以概念股為主；官方類股的漲跌幅改用 MI_INDEX 類股指數。
    """
    codes: set[str] = set()
    for group_codes in CONCEPT_GROUPS.values():
        codes.update(group_codes)
    return tuple(sorted(codes))


# ── Sidebar ────────────────────────────────────────────────────────────────────

def render_sidebar(
    sector_list: list[str],
    fetch_results: dict[str, FetchResult],
) -> dict:
    """渲染側邊欄控制項，回傳篩選設定 dict。"""
    with st.sidebar:
        st.title("⚙️ 篩選設定")

        show_concept = st.toggle("顯示概念股分群", value=True)

        st.markdown("---")
        st.subheader("類股篩選")
        selected_sectors = st.multiselect(
            "選擇類股（留空 = 全部）",
            options=sector_list,
            default=[],
            placeholder="全部類股",
        )

        st.markdown("---")
        st.subheader("階段篩選")
        selected_stages = st.multiselect(
            "選擇階段",
            options=list(STAGE_LABELS_ZH.values()),
            default=[],
            placeholder="全部階段",
        )

        st.markdown("---")
        st.subheader("資料來源狀態")
        _render_source_badges(fetch_results)

        st.markdown("---")
        st.caption(f"最後更新：{pd.Timestamp.now().strftime('%Y-%m-%d %H:%M')}")
        st.caption(f"交易日：{get_last_trading_day()}")

    return {
        "show_concept":    show_concept,
        "selected_sectors": selected_sectors,
        "selected_stages":  selected_stages,
    }


def _render_source_badges(fetch_results: dict[str, FetchResult]) -> None:
    labels = {
        "sector_idx":    "類股指數",
        "stocks_today":  "個股成交",
        "market_total":  "大盤成交值",
        "institutional": "三大法人",
    }
    source_icon = {
        "openapi":        ("✅ OpenAPI",  "badge-ok"),
        "rwd_json":       ("✅ JSON API", "badge-ok"),
        "scraped":        ("⚠️ 爬蟲",     "badge-warn"),
        "derived":        ("⚠️ 推算值",   "badge-warn"),
        "yfinance":       ("✅ yfinance", "badge-ok"),
        "empty":          ("❌ 無資料",   "badge-err"),
    }
    for key, label in labels.items():
        res = fetch_results.get(key)
        if res is None:
            continue
        src  = res.source
        text, css = source_icon.get(src, (src, "badge-warn"))
        st.markdown(f"{label}：<span class='{css}'>{text}</span>", unsafe_allow_html=True)
        if res.warnings:
            for w in res.warnings:
                st.caption(f"　↳ {w}")


# ── Header Bar ─────────────────────────────────────────────────────────────────

def render_header(
    taiex_hist:  pd.DataFrame,
    flow_df:     pd.DataFrame,
    anomalies:   list,
    stocks_today: pd.DataFrame,
) -> None:
    """頂部 KPI 列：大盤指數、成交值、警示數、市場強弱。"""
    col1, col2, col3, col4 = st.columns(4)

    # 大盤指數
    taiex_val, taiex_chg = "N/A", 0.0
    if not taiex_hist.empty and "Close" in taiex_hist.columns:
        closes = taiex_hist["Close"].dropna()
        if len(closes) >= 2:
            taiex_val = f"{closes.iloc[-1]:,.0f}"
            taiex_chg = (closes.iloc[-1] - closes.iloc[-2]) / closes.iloc[-2] * 100
    col1.metric("加權指數 (TAIEX)", taiex_val, f"{taiex_chg:+.2f}%" if taiex_val != "N/A" else "")

    # 成交值
    total_val = "N/A"
    if not stocks_today.empty and "trade_value" in stocks_today.columns:
        tv = stocks_today["trade_value"].sum()
        total_val = f"NT${tv / 1e12:.2f} 兆" if tv >= 1e12 else f"NT${tv / 1e8:.0f} 億"
    col2.metric("今日大盤成交值", total_val)

    # 警示數
    high_cnt = sum(1 for a in anomalies if a.severity == "high")
    col3.metric("異常警示", f"{len(anomalies)} 則", f"高優先：{high_cnt} 則" if high_cnt else "")

    # 市場強弱
    mood, mood_delta = "N/A", ""
    if not flow_df.empty and "flow_pct" in flow_df.columns:
        n_sectors = len(flow_df[~flow_df["sector"].str.startswith("【概念】")])
        mood = f"{n_sectors} 個類股" if n_sectors else "N/A"
    if not stocks_today.empty and "change" in stocks_today.columns:
        up   = (stocks_today["change"] > 0).sum()
        down = (stocks_today["change"] < 0).sum()
        mood = "多頭" if up > down else "空頭" if down > up else "平盤"
        mood_delta = f"上漲 {up} / 下跌 {down}"
    col4.metric("市場氛圍", mood, mood_delta)


# ── Tab 1：總覽 ────────────────────────────────────────────────────────────────

def render_overview_tab(
    flow_df:     pd.DataFrame,
    anomalies:   list,
    taiex_hist:  pd.DataFrame,
) -> None:
    st.subheader("今日資金流向概覽")

    if flow_df.empty:
        st.warning("個股成交資料尚未取得，請稍後重新整理。")
        return

    # 前 5 大類股 KPI
    top5 = flow_df[~flow_df["sector"].str.startswith("【概念】")].head(5)
    cols = st.columns(5)
    for i, (_, row) in enumerate(top5.iterrows()):
        with cols[i]:
            st.markdown(f"**{row['sector']}**")
            st.markdown(f"<div class='kpi-value'>{row['flow_pct']:.2f}%</div>", unsafe_allow_html=True)
            st.markdown(f"<div class='kpi-label'>成交比重</div>", unsafe_allow_html=True)

    st.markdown("---")

    # 前 10 類股成交比重長條圖
    top10 = flow_df[~flow_df["sector"].str.startswith("【概念】")].head(10)
    fig = px.bar(
        top10,
        x="flow_pct",
        y="sector",
        orientation="h",
        color="flow_pct",
        color_continuous_scale="Blues",
        labels={"flow_pct": "成交比重 (%)", "sector": "類股"},
        title="前 10 類股成交比重",
        text=top10["flow_pct"].map(lambda x: f"{x:.2f}%"),
    )
    fig.update_layout(height=380, yaxis={"categoryorder": "total ascending"},
                      coloraxis_showscale=False)
    fig.update_traces(textposition="outside")
    st.plotly_chart(fig, use_container_width=True)

    # 警示摘要
    if anomalies:
        st.markdown("---")
        st.subheader(f"⚠️ 今日警示摘要（共 {len(anomalies)} 則）")
        for a in anomalies[:5]:
            css = f"alert-{a.severity}"
            sev_zh = {"high": "🔴 高", "medium": "🟠 中", "low": "🟡 低"}[a.severity]
            st.markdown(
                f"<div class='{css}'><b>{sev_zh} ｜ {a.sector}</b><br>{a.description}</div>",
                unsafe_allow_html=True,
            )
    else:
        st.info("今日暫無異常警示。")


# ── Tab 2：產業熱圖 ────────────────────────────────────────────────────────────

def render_heatmap_tab(
    flow_df:     pd.DataFrame,
    sector_df:   pd.DataFrame,
    stage_df:    pd.DataFrame,
    show_concept: bool,
    filters:     dict,
) -> None:
    st.subheader("產業熱圖（成交比重 × 漲跌幅）")

    if flow_df.empty:
        st.warning("成交資料不足，無法繪製熱圖。")
        return

    # 合併類股指數漲跌幅
    display_df = flow_df.copy()
    if not sector_df.empty and "sector_zh" in sector_df.columns and "chg_pct" in sector_df.columns:
        display_df = display_df.merge(
            sector_df[["sector_zh", "chg_pct"]].rename(columns={"sector_zh": "sector"}),
            on="sector", how="left",
        )
    else:
        display_df["chg_pct"] = 0.0

    # 概念股開關
    if not show_concept:
        display_df = display_df[~display_df["sector"].str.startswith("【概念】")]

    # 類股篩選
    if filters.get("selected_sectors"):
        display_df = display_df[display_df["sector"].isin(filters["selected_sectors"])]

    if display_df.empty:
        st.info("篩選後無資料。")
        return

    # Treemap：大小 = 成交比重，顏色 = 漲跌幅
    display_df["chg_pct"] = display_df["chg_pct"].fillna(0)
    display_df["hover"] = display_df.apply(
        lambda r: f"成交比重：{r['flow_pct']:.2f}%<br>漲跌幅：{r.get('chg_pct', 0):+.2f}%", axis=1
    )

    fig = px.treemap(
        display_df,
        path=["sector"],
        values="flow_pct",
        color="chg_pct",
        color_continuous_scale=[
            [0.0, "#CC0000"], [0.5, "#FFFFFF"], [1.0, "#00AA44"]
        ],
        color_continuous_midpoint=0,
        custom_data=["hover"],
        title="類股成交比重熱圖（紅跌 / 綠漲）",
    )
    fig.update_traces(
        hovertemplate="<b>%{label}</b><br>%{customdata[0]}<extra></extra>",
        textinfo="label+value",
        texttemplate="<b>%{label}</b><br>%{value:.2f}%",
    )
    fig.update_layout(height=600, margin=dict(t=50, b=10, l=10, r=10))
    st.plotly_chart(fig, use_container_width=True)

    # 數據表
    with st.expander("查看原始數據"):
        show_cols = [c for c in ["sector", "flow_pct", "flow_value_ntd", "chg_pct", "stock_count"] if c in display_df.columns]
        st.dataframe(
            display_df[show_cols].rename(columns={
                "sector": "類股", "flow_pct": "成交比重%",
                "flow_value_ntd": "成交值(元)", "chg_pct": "漲跌%", "stock_count": "股票數"
            }),
            use_container_width=True,
        )


# ── Tab 3：Bubble Chart ────────────────────────────────────────────────────────

def render_bubble_tab(
    flow_history_df: pd.DataFrame,
    sector_momentum: pd.DataFrame,
    stage_df:        pd.DataFrame,
) -> None:
    st.subheader("資金聚集泡泡圖")
    st.caption("X軸：成交比重變化（vs 5日均值） ｜ Y軸：5日漲跌幅 ｜ 圓圈大小：成交值")

    if flow_history_df.empty:
        st.warning("歷史成交資料不足，無法繪製泡泡圖。")
        return

    # 合併動能與階段資料
    bubble = flow_history_df.copy()
    if not sector_momentum.empty and "sector" in sector_momentum.columns:
        bubble = bubble.merge(sector_momentum[["sector", "return_5d", "return_20d"]], on="sector", how="left")
    else:
        bubble["return_5d"] = 0.0

    if not stage_df.empty:
        bubble = bubble.merge(stage_df[["sector", "stage_zh"]], on="sector", how="left")
    bubble["stage_zh"] = bubble.get("stage_zh", pd.Series(["未知"] * len(bubble))).fillna("未知")
    bubble["return_5d"] = bubble.get("return_5d", pd.Series([0.0] * len(bubble))).fillna(0)

    # 過濾概念股（只顯示官方類股）
    bubble = bubble[~bubble["sector"].str.startswith("【概念】")]
    if bubble.empty:
        st.info("無可顯示的類股資料。")
        return

    color_map = {v: STAGE_COLORS.get(k, "#888888") for k, v in STAGE_LABELS_ZH.items()}
    color_map["未知"] = "#AAAAAA"

    fig = px.scatter(
        bubble,
        x="delta_pct",
        y="return_5d",
        size="today_pct",
        size_max=60,
        color="stage_zh",
        color_discrete_map=color_map,
        text="sector",
        hover_data={
            "delta_pct":  ":.2f",
            "return_5d":  ":.2f",
            "today_pct":  ":.2f",
            "avg_5d_pct": ":.2f",
            "stage_zh":   True,
        },
        labels={
            "delta_pct":  "成交比重變化 (%，vs 5日均)",
            "return_5d":  "5日漲跌幅 (%)",
            "today_pct":  "今日比重 (%)",
            "stage_zh":   "類股階段",
        },
        title="資金聚集泡泡圖",
    )

    # 參考線與象限標籤
    fig.add_vline(x=0, line_dash="dash", line_color="gray", opacity=0.5)
    fig.add_hline(y=0, line_dash="dash", line_color="gray", opacity=0.5)

    x_range = [bubble["delta_pct"].min() * 1.1, bubble["delta_pct"].max() * 1.1]
    y_range = [bubble["return_5d"].min() * 1.1, bubble["return_5d"].max() * 1.1]
    quad_x = max(abs(x_range[0]), abs(x_range[1])) * 0.85
    quad_y = max(abs(y_range[0]), abs(y_range[1])) * 0.85

    for (qx, qy, label) in [
        (quad_x,  quad_y,  "🔥 資金流入 + 上漲"),
        (-quad_x, quad_y,  "⬆️ 上漲但資金流出"),
        (-quad_x, -quad_y, "❄️ 資金流出 + 下跌"),
        (quad_x,  -quad_y, "💧 資金流入但弱勢"),
    ]:
        fig.add_annotation(
            x=qx, y=qy, text=label, showarrow=False,
            font=dict(size=10, color="#666666"), opacity=0.7,
        )

    fig.update_traces(textposition="top center", textfont_size=9)
    fig.update_layout(height=580, legend_title="類股階段")
    st.plotly_chart(fig, use_container_width=True)


# ── Tab 4：領頭羊表 ────────────────────────────────────────────────────────────

def render_leaders_tab(
    leader_df:   pd.DataFrame,
    price_hist:  pd.DataFrame,
) -> None:
    st.subheader("類股領頭羊")
    st.caption(f"資金前 {THRESHOLDS['top_sectors_for_leaders']} 大類股，各取漲幅前 {THRESHOLDS['leaders_per_sector']} 名")

    if leader_df.empty:
        st.warning("領頭羊資料不足（可能為非交易日或歷史價格尚未載入）。")
        return

    if "類股" not in leader_df.columns:
        st.dataframe(leader_df, use_container_width=True)
        return

    for sector, grp in leader_df.groupby("類股", sort=False):
        stage_zh = grp["階段"].iloc[0] if "階段" in grp.columns and grp["階段"].iloc[0] else ""
        title = f"**{sector}**" + (f"　`{stage_zh}`" if stage_zh else "")
        with st.expander(title, expanded=True):
            display = grp.drop(columns=["類股"], errors="ignore").reset_index(drop=True)

            # 數值顏色標記
            def color_pct(val):
                if pd.isna(val):
                    return ""
                return "color: #00AA44; font-weight:bold" if val > 0 else "color: #CC0000; font-weight:bold" if val < 0 else ""

            styled = display.style.map(color_pct, subset=["5日漲幅%", "20日漲幅%", "RS分數"])
            st.dataframe(styled, use_container_width=True, hide_index=True)

            # 個股 5 日走勢迷你圖（若有歷史資料）
            if not price_hist.empty and isinstance(price_hist.columns, pd.MultiIndex):
                mini_cols = st.columns(len(display))
                for i, (_, row) in enumerate(display.iterrows()):
                    code = str(row.get("代碼", ""))
                    tk = f"{code}.TW"
                    if tk in price_hist.columns.get_level_values(0):
                        sub = price_hist[tk]["Close"].dropna().tail(20)
                        if len(sub) > 1:
                            with mini_cols[i]:
                                st.caption(f"{code} 走勢")
                                st.line_chart(sub, height=80, use_container_width=True)


# ── Tab 5：警示面板 ────────────────────────────────────────────────────────────

def render_alerts_tab(anomalies: list) -> None:
    st.subheader("⚠️ 異常警示面板")

    if not anomalies:
        st.success("🟢 今日無異常警示，市場資金分布正常。")
        return

    sev_zh = {"high": "🔴 高優先", "medium": "🟠 中優先", "low": "🟡 低優先"}
    type_zh = {
        "volume_surge":         "成交量暴增",
        "institutional_inflow": "法人資金流入",
        "concept_breakout":     "概念股領漲",
    }

    for sev_level in ["high", "medium", "low"]:
        level_alerts = [a for a in anomalies if a.severity == sev_level]
        if not level_alerts:
            continue
        st.markdown(f"### {sev_zh[sev_level]}（{len(level_alerts)} 則）")
        for a in level_alerts:
            css = f"alert-{sev_level}"
            type_label = type_zh.get(a.alert_type, a.alert_type)
            st.markdown(
                f"""<div class='{css}'>
                    <b>{type_label}｜{a.sector}</b><br>
                    {a.description}<br>
                    <small>閾值：{a.threshold:.1f}｜當前值：{a.value:.1f}</small>
                </div>""",
                unsafe_allow_html=True,
            )


# ── Tab 6：原始資料 ────────────────────────────────────────────────────────────

def render_raw_data_tab(
    stocks_df:        pd.DataFrame,
    flow_df:          pd.DataFrame,
    institutional_df: pd.DataFrame,
    leader_df:        pd.DataFrame,
) -> None:
    st.subheader("原始資料下載")

    datasets = [
        ("個股成交資料",   stocks_df),
        ("類股資金流向",   flow_df),
        ("三大法人買賣超", institutional_df),
        ("類股領頭羊",     leader_df),
    ]

    for name, df in datasets:
        with st.expander(f"📋 {name}（{len(df)} 筆）"):
            if df.empty:
                st.info("此資料集為空。")
                continue
            st.dataframe(df.head(200), use_container_width=True)
            csv = df.to_csv(index=False, encoding="utf-8-sig")
            st.download_button(
                label=f"下載 {name} CSV",
                data=csv,
                file_name=f"{name}.csv",
                mime="text/csv",
                key=f"dl_{name}",
            )


# ── 主程式 ────────────────────────────────────────────────────────────────────

def main() -> None:
    st.title("📊 台股資金流向 & 類股輪動 Dashboard")

    # ── 載入即時資料 ──
    intraday = load_intraday_data()
    stocks_res    = intraday["stocks_today"]
    sector_res    = intraday["sector_idx"]
    market_res    = intraday["market_total"]
    instit_res    = intraday["institutional"]

    # 市場關閉偵測
    if not stocks_res.ok:
        st.warning("⚠️ 今日個股成交資料尚未更新（可能為非交易日或資料尚未發布）。")
        st.info(f"最近交易日：{get_last_trading_day()}")

    # 對個股加入類股/概念股欄位
    stocks_df = pd.DataFrame()
    if stocks_res.ok:
        stocks_df = map_stocks_to_sectors(stocks_res.df)

    # 大盤總成交值
    market_total_value = 0.0
    if market_res.ok and "total_value" in market_res.df.columns:
        market_total_value = float(market_res.df["total_value"].iloc[0])
    elif not stocks_df.empty and "trade_value" in stocks_df.columns:
        market_total_value = float(stocks_df["trade_value"].sum())

    # ── 計算 Money Flow（即時，不需要歷史資料）──
    flow_df = compute_sector_money_flow(stocks_df, market_total_value, include_concept=True)

    # ── Sidebar（在歷史資料載入前先渲染，確保頁面可互動）──
    sector_list = flow_df["sector"].tolist() if not flow_df.empty else []
    filters = render_sidebar(sector_list, intraday)

    # ── 載入概念股歷史資料（~15 支，約 5-10 秒）──
    tickers    = _all_tickers(stocks_df)
    hist_data  = load_historical_data(tickers, period="1y")
    price_hist = hist_data["price_hist"]
    taiex_hist = hist_data["taiex_hist"]

    # ── 動能分析（概念股層級）──
    momentum_df     = compute_momentum_scores(price_hist.df, taiex_hist.df)
    # 概念股的 stocks_df 子集（供 sector_momentum 使用）
    concept_codes   = {c for codes in CONCEPT_GROUPS.values() for c in codes}
    concept_stocks  = stocks_df[stocks_df["code"].isin(concept_codes)] if not stocks_df.empty else pd.DataFrame()
    sector_momentum = compute_sector_momentum(momentum_df, concept_stocks) if not momentum_df.empty else pd.DataFrame()

    # ── 階段判斷（概念股）──
    stage_df = identify_sector_stages(price_hist.df, concept_stocks)

    # ── 歷史成交比重趨勢（用於 Bubble Chart X 軸）──
    flow_history_df = compute_sector_money_flow_history(price_hist.df, stocks_df, market_total_value)

    # ── 異常偵測 ──
    anomalies = run_all_anomaly_detection(
        flow_history_df, instit_res.df, momentum_df, stocks_df
    )

    # ── 領頭羊表 ──
    leader_df = build_sector_leader_table(
        flow_df=flow_df,
        momentum_df=momentum_df,
        stocks_df=stocks_df,
        stage_df=stage_df,
    )

    # ── Header ──
    render_header(taiex_hist.df, flow_df, anomalies, stocks_df)
    st.markdown("---")

    # ── 6 個分頁 ──
    tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs([
        "📈 總覽",
        "🌡️ 產業熱圖",
        "💹 資金聚集",
        "🏆 領頭羊",
        "⚠️ 警示",
        "🗂️ 原始資料",
    ])

    with tab1:
        render_overview_tab(flow_df, anomalies, taiex_hist.df)

    with tab2:
        render_heatmap_tab(flow_df, sector_res.df, stage_df, filters["show_concept"], filters)

    with tab3:
        render_bubble_tab(flow_history_df, sector_momentum, stage_df)

    with tab4:
        render_leaders_tab(leader_df, price_hist.df)

    with tab5:
        render_alerts_tab(anomalies)

    with tab6:
        render_raw_data_tab(stocks_df, flow_df, instit_res.df, leader_df)

    # 資料品質日誌（側邊欄底部）
    all_warnings = []
    for res in intraday.values():
        all_warnings.extend(res.warnings)
    for res in hist_data.values():
        all_warnings.extend(res.warnings)
    if all_warnings:
        with st.sidebar:
            with st.expander("📋 資料品質日誌"):
                for w in all_warnings:
                    st.caption(w)


if __name__ == "__main__":
    main()
