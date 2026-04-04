# app.py
# 台股隔日沖選股系統 — Streamlit 圖形介面
#
# 啟動方式：
#   streamlit run app.py

import json
import os
import time
import requests
from datetime import datetime
from typing import Dict, List, Optional, Tuple

# ── 確保工作目錄為此檔案所在位置（快取路徑為相對路徑）──────────
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import streamlit as st

import config
import data_fetcher as df
import screener as sc

# ============================================================
# 頁面基本設定
# ============================================================

st.set_page_config(
    page_title="台股隔日沖選股",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

SETTINGS_FILE = "settings.json"

# ============================================================
# 設定管理
# ============================================================

_DEFAULTS = {
    "min_gain_pct":        config.MIN_GAIN_PCT,
    "pre_filter_gain_pct": config.PRE_FILTER_GAIN_PCT,
    "near_limit_up_pct":   config.NEAR_LIMIT_UP_PCT,
    "capital_min_ntd":     config.CAPITAL_MIN_NTD,
    "capital_max_ntd":     config.CAPITAL_MAX_NTD,
    "volume_multiplier":   config.VOLUME_MULTIPLIER,
    "ma_short":            config.MA_SHORT,
    "ma_mid":              config.MA_MID,
    "ma_long":             config.MA_LONG,
    "boll_window":         config.BOLL_WINDOW,
    "boll_std_mult":       config.BOLL_STD_MULT,
    "max_day_trade_ratio": config.MAX_DAY_TRADE_RATIO,
    "include_tpex":        config.INCLUDE_TPEX,
    "finmind_token":       "",
}


def load_settings() -> Dict:
    if not os.path.exists(SETTINGS_FILE):
        return dict(_DEFAULTS)
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        merged = dict(_DEFAULTS)
        merged.update(data)
        return merged
    except Exception:
        return dict(_DEFAULTS)


def save_settings(cfg: Dict) -> None:
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
    except Exception as e:
        st.error(f"設定儲存失敗：{e}")


# ============================================================
# 即時行情抓取（TWSE MIS API）
# ============================================================

def _parse_mis_float(s) -> Optional[float]:
    """解析 MIS API 回傳的數字字串，'-' 或空值回傳 None。"""
    if not s or str(s).strip() in ("-", ""):
        return None
    try:
        return float(str(s).replace(",", ""))
    except ValueError:
        return None


def fetch_realtime_quotes(watch_list: List[Tuple[str, str]]) -> List[Dict]:
    """
    呼叫 TWSE MIS 即時行情 API，一次查詢多支股票。

    Args:
        watch_list: [(code, exchange), ...]  exchange = 'TWSE' or 'TPEX'

    Returns:
        list of dict，每筆含即時行情資訊
    """
    if not watch_list:
        return []

    # 組合 ex_ch 參數，TWSE 用 tse_，TPEX 用 otc_
    parts = []
    for code, exchange in watch_list:
        prefix = "otc" if exchange == "TPEX" else "tse"
        parts.append(f"{prefix}_{code}.tw")
    ex_ch = "|".join(parts)

    url = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
    try:
        resp = requests.get(
            url,
            params={"ex_ch": ex_ch, "_": int(time.time() * 1000)},
            headers=config.REQUEST_HEADERS,
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return []

    results = []
    for item in data.get("msgArray", []):
        code = str(item.get("c", "")).strip()
        name = str(item.get("n", "")).strip()

        current  = _parse_mis_float(item.get("z"))   # 現價
        prev     = _parse_mis_float(item.get("y"))   # 昨收
        limit_up = _parse_mis_float(item.get("u"))   # 漲停價
        limit_dn = _parse_mis_float(item.get("w"))   # 跌停價
        open_p   = _parse_mis_float(item.get("o"))   # 開盤
        high     = _parse_mis_float(item.get("h"))   # 最高
        low      = _parse_mis_float(item.get("l"))   # 最低
        volume   = _parse_mis_float(item.get("v"))   # 成交量（千股）

        # 計算漲幅
        gain_pct = None
        if current is not None and prev and prev > 0:
            gain_pct = round((current - prev) / prev * 100, 2)

        # 距漲停 %
        dist_limit_pct = None
        if current is not None and limit_up and limit_up > 0:
            dist_limit_pct = round((limit_up - current) / limit_up * 100, 2)

        # 判斷漲停狀態
        # 賣一揭示價/量 → a / f (pipe-separated)
        ask_str = str(item.get("a", ""))
        ask_vol_str = str(item.get("f", ""))
        asks = [_parse_mis_float(x) for x in ask_str.split("|") if x and x != "-"]
        ask_vols = [_parse_mis_float(x) for x in ask_vol_str.split("|") if x and x != "-"]

        status = "NORMAL"
        if current is not None and limit_up is not None:
            at_limit = abs(current - limit_up) < 0.02
            if at_limit:
                # 賣一是否有掛單：有賣單 = 接近漲停，無賣單 = 鎖漲停
                has_sellers = any(v and v > 0 for v in ask_vols[:1])
                status = "NEAR_LIMIT" if has_sellers else "LOCKED"
            elif gain_pct is not None and gain_pct >= config.NEAR_LIMIT_UP_PCT:
                status = "NEAR_LIMIT"

        # 買一/賣一
        bid_str = str(item.get("b", ""))
        bid_vol_str = str(item.get("g", ""))
        bids = [_parse_mis_float(x) for x in bid_str.split("|") if x and x != "-"]
        bid_vols = [_parse_mis_float(x) for x in bid_vol_str.split("|") if x and x != "-"]

        bid1 = bids[0] if bids else None
        bid1_vol = int(bid_vols[0]) if bid_vols else None
        ask1 = asks[0] if asks else None
        ask1_vol = int(ask_vols[0]) if ask_vols else None

        results.append({
            "code":           code,
            "name":           name,
            "current":        current,
            "prev":           prev,
            "gain_pct":       gain_pct,
            "limit_up":       limit_up,
            "limit_dn":       limit_dn,
            "dist_limit_pct": dist_limit_pct,
            "open":           open_p,
            "high":           high,
            "low":            low,
            "volume_k":       volume,               # 千股
            "volume_zhang":   int(volume / 1000) if volume else None,  # 張
            "bid1":           bid1,
            "bid1_vol":       bid1_vol,
            "ask1":           ask1,
            "ask1_vol":       ask1_vol,
            "status":         status,
            "data_time":      item.get("t", ""),
        })

    return results


# ============================================================
# Session State 初始化
# ============================================================

if "results" not in st.session_state:
    st.session_state.results = None
if "trade_date" not in st.session_state:
    st.session_state.trade_date = None
if "scan_count" not in st.session_state:
    st.session_state.scan_count = 0
if "watchlist" not in st.session_state:
    # [(code, exchange), ...]
    st.session_state.watchlist = []


# ============================================================
# 側邊欄：選股設定面板
# ============================================================

with st.sidebar:
    st.title("⚙️ 選股設定")
    st.caption("調整後點「儲存設定」再執行選股")

    current = load_settings()

    st.subheader("📊 漲幅條件")
    min_gain = st.slider(
        "日漲幅門檻 (%)",
        min_value=3.0, max_value=15.0, step=0.5,
        value=float(current["min_gain_pct"]),
        help="主篩選條件，股票當日漲幅需≥此值",
    )
    pre_filter = st.slider(
        "預篩選門檻 (%) ℹ️",
        min_value=2.0, max_value=10.0, step=0.5,
        value=float(current["pre_filter_gain_pct"]),
        help="寬鬆門檻，用來減少 API 呼叫量。應低於日漲幅門檻。",
    )
    near_limit = st.number_input(
        "近漲停判定 (%)",
        min_value=5.0, max_value=10.0, step=0.1,
        value=float(current["near_limit_up_pct"]),
        help="超過此漲幅視為接近漲停（標記 NEAR_LIMIT）",
    )

    st.subheader("🏢 股本條件")
    cap_min_yi = st.number_input(
        "股本下限 (億 NTD)",
        min_value=1, max_value=100, step=1,
        value=int(current["capital_min_ntd"] / 1e8),
    )
    cap_max_yi = st.number_input(
        "股本上限 (億 NTD)",
        min_value=10, max_value=500, step=5,
        value=int(current["capital_max_ntd"] / 1e8),
    )

    st.subheader("📦 量能條件")
    vol_mult = st.slider(
        "量比門檻 (倍)",
        min_value=1.0, max_value=6.0, step=0.5,
        value=float(current["volume_multiplier"]),
    )
    max_dt = st.slider(
        "當沖率上限 (%)",
        min_value=20, max_value=90, step=5,
        value=int(current["max_day_trade_ratio"] * 100),
    )

    st.subheader("📉 均線設定")
    col1, col2, col3 = st.columns(3)
    with col1:
        ma_short = st.number_input("MA短", min_value=3, max_value=10, value=int(current["ma_short"]))
    with col2:
        ma_mid = st.number_input("MA中", min_value=5, max_value=30, value=int(current["ma_mid"]))
    with col3:
        ma_long = st.number_input("MA長", min_value=15, max_value=60, value=int(current["ma_long"]))

    st.subheader("〰️ 布林通道")
    col4, col5 = st.columns(2)
    with col4:
        boll_window = st.number_input("窗格天數", min_value=10, max_value=30, value=int(current["boll_window"]))
    with col5:
        boll_std = st.number_input("標準差倍數", min_value=1.0, max_value=3.0, step=0.5, value=float(current["boll_std_mult"]))

    st.subheader("🔧 其他")
    include_tpex = st.checkbox(
        "包含上櫃 (TPEX)",
        value=bool(current["include_tpex"]),
    )

    st.divider()
    if st.button("💾 儲存設定", use_container_width=True, type="primary"):
        new_cfg = {
            "min_gain_pct":        min_gain,
            "pre_filter_gain_pct": pre_filter,
            "near_limit_up_pct":   near_limit,
            "capital_min_ntd":     int(cap_min_yi * 1e8),
            "capital_max_ntd":     int(cap_max_yi * 1e8),
            "volume_multiplier":   vol_mult,
            "ma_short":            int(ma_short),
            "ma_mid":              int(ma_mid),
            "ma_long":             int(ma_long),
            "boll_window":         int(boll_window),
            "boll_std_mult":       boll_std,
            "max_day_trade_ratio": max_dt / 100.0,
            "include_tpex":        include_tpex,
            "finmind_token":       current.get("finmind_token", ""),
        }
        save_settings(new_cfg)
        st.success("✅ 設定已儲存！")

    st.divider()
    st.caption("設定同步存至 `settings.json`")
    if st.button("↩️ 恢復預設值", use_container_width=True):
        save_settings(dict(_DEFAULTS))
        st.success("已恢復預設值，請重新整理頁面")
        st.rerun()


# 目前生效的 cfg dict（供選股使用）
cfg = {
    "min_gain_pct":        min_gain,
    "pre_filter_gain_pct": pre_filter,
    "near_limit_up_pct":   near_limit,
    "capital_min_ntd":     int(cap_min_yi * 1e8),
    "capital_max_ntd":     int(cap_max_yi * 1e8),
    "volume_multiplier":   vol_mult,
    "ma_short":            int(ma_short),
    "ma_mid":              int(ma_mid),
    "ma_long":             int(ma_long),
    "boll_window":         int(boll_window),
    "boll_std_mult":       boll_std,
    "max_day_trade_ratio": max_dt / 100.0,
    "include_tpex":        include_tpex,
    "finmind_token":       current.get("finmind_token", ""),
    "min_history_days":    config.MIN_HISTORY_DAYS,
}


# ============================================================
# 主頁面：雙分頁
# ============================================================

st.title("📈 台股隔日沖選股系統")

tab_screen, tab_monitor = st.tabs(["🔍 選股篩選", "📡 盤中監控"])


# ════════════════════════════════════════════════════════════
# Tab 1：選股篩選（原有功能）
# ════════════════════════════════════════════════════════════

with tab_screen:
    st.caption("每日收盤後執行，篩選符合隔日沖條件的候選股票")

    with st.expander("📋 目前篩選條件一覽", expanded=False):
        col_a, col_b, col_c = st.columns(3)
        with col_a:
            st.write(f"**日漲幅門檻：** ≥ {min_gain}%")
            st.write(f"**近漲停判定：** ≥ {near_limit}%")
            st.write(f"**量比門檻：** ≥ {vol_mult}x")
            st.write(f"**當沖率上限：** < {max_dt}%")
        with col_b:
            st.write(f"**股本範圍：** {cap_min_yi}億 ~ {cap_max_yi}億")
            st.write(f"**均線多頭排列：** MA{int(ma_short)} > MA{int(ma_mid)} > MA{int(ma_long)}")
            st.write(f"**布林通道：** {int(boll_window)} 日 ± {boll_std} σ")
        with col_c:
            st.write(f"**預篩選門檻：** ≥ {pre_filter}%")
            st.write(f"**包含上櫃：** {'是' if include_tpex else '否'}")

    st.divider()

    col_run, col_clear = st.columns([3, 1])
    with col_run:
        run_clicked = st.button("🔍 執行選股", type="primary", use_container_width=True)
    with col_clear:
        if st.button("🗑️ 清除結果", use_container_width=True):
            st.session_state.results = None
            st.session_state.trade_date = None
            st.session_state.scan_count = 0
            st.rerun()

    # ── 選股執行邏輯 ──────────────────────────────────────────

    def run_screener(cfg: Dict):
        progress_placeholder = st.empty()
        status_placeholder   = st.empty()

        with progress_placeholder.container():
            st.info("📡 步驟 1/4：取得全市場行情...")

        twse_stocks, actual_date_iso = df.fetch_twse_all_stocks()
        if not twse_stocks:
            st.error("❌ 無法取得 TWSE 資料，請確認網路連線。")
            return None, None, 0

        all_stocks = list(twse_stocks)
        if cfg.get("include_tpex"):
            with progress_placeholder.container():
                st.info("📡 步驟 1/4：取得全市場行情（含上櫃）...")
            all_stocks.extend(df.fetch_tpex_all_stocks())

        trade_date    = actual_date_iso or datetime.now().strftime("%Y-%m-%d")
        total_scanned = len(all_stocks)

        with progress_placeholder.container():
            st.info("📡 步驟 2/4：取得三大法人 & 當沖資料...")

        td_yyyymmdd = trade_date.replace("-", "")
        institution_map = df.fetch_institution_flows(td_yyyymmdd)
        day_trade_map   = df.fetch_day_trade_data(td_yyyymmdd)

        with progress_placeholder.container():
            st.info("📡 步驟 3/4：取得公司股本資料...")
        capital_map = df.fetch_company_capital(include_tpex=cfg.get("include_tpex", False))

        pre_filtered = [
            s for s in all_stocks
            if s.get("gain_pct", 0) >= cfg["pre_filter_gain_pct"]
            and s.get("close", 0) > 0
        ]
        total_pre = len(pre_filtered)

        with progress_placeholder.container():
            st.info(f"📡 步驟 4/4：取得個股歷史資料並篩選（{total_pre} 支預篩選候選）...")

        prog_bar    = st.progress(0)
        fetch_count = [0]

        def history_fn(code, exchange):
            h = df.get_stock_history(code, exchange)
            fetch_count[0] += 1
            if total_pre > 0:
                prog_bar.progress(min(fetch_count[0] / total_pre, 1.0))
            status_placeholder.caption(f"歷史資料：{fetch_count[0]}/{total_pre}  [{code}]")
            return h

        results = sc.screen_stocks(
            all_stocks=pre_filtered,
            history_fn=history_fn,
            capital_map=capital_map,
            institution_map=institution_map,
            day_trade_map=day_trade_map,
            cfg=cfg,
            trade_date_iso=trade_date,
        )

        progress_placeholder.empty()
        status_placeholder.empty()
        prog_bar.empty()
        return results, trade_date, total_scanned

    if run_clicked:
        with st.spinner("選股進行中，請稍候..."):
            results, trade_date, total_scanned = run_screener(cfg)
        if results is not None:
            st.session_state.results    = results
            st.session_state.trade_date = trade_date
            st.session_state.scan_count = total_scanned
            # 選股完成後，自動把結果載入監控清單
            if results:
                st.session_state.watchlist = [
                    (r["代號"], r["市場"]) for r in results
                ]
            st.rerun()

    # ── 結果顯示 ──────────────────────────────────────────────

    if st.session_state.results is not None:
        results       = st.session_state.results
        trade_date    = st.session_state.trade_date
        total_scanned = st.session_state.scan_count

        st.subheader(f"📅 交易日：{trade_date}")

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("篩選通過", f"{len(results)} 檔")
        m2.metric("掃描總數", f"{total_scanned:,} 檔")
        m3.metric("通過率", f"{len(results)/total_scanned*100:.1f}%" if total_scanned else "N/A")
        m4.metric("最高評分", f"{results[0]['評分']:.1f}" if results else "—")

        st.divider()

        if not results:
            st.warning("⚠️ 今日無符合條件的候選股票。可嘗試放寬左側設定後重新執行。")
        else:
            st.subheader(f"🏆 候選標的（共 {len(results)} 支）")
            st.caption("✅ 選股完成後，候選股已自動載入「盤中監控」分頁")

            import pandas as pd

            table_rows = []
            for r in results:
                limit_icon = "🔒" if r["漲停狀態"] == "LOCKED" else ("⬆️" if r["漲停狀態"] == "NEAR_LIMIT" else "")
                table_rows.append({
                    "排名":       f"#{len(table_rows)+1}",
                    "代號":       r["代號"],
                    "名稱":       r["名稱"],
                    "市場":       r["市場"],
                    "收盤價":     r["收盤價"],
                    "漲幅(%)":    f"+{r['漲幅(%)']:.2f}%",
                    "漲停":       f"{limit_icon} {r['漲停狀態']}",
                    "量比":       f"{r['量比(5日)']:.1f}x" if r.get("量比(5日)") else "—",
                    "均線多頭":   "✅" if r.get("均線多頭排列") is True else ("❌" if r.get("均線多頭排列") is False else "❓"),
                    "BB突破":     "✅" if r.get("BB突破") is True else ("❌" if r.get("BB突破") is False else "❓"),
                    "當沖率":     f"{r['當沖率(%)']:.1f}%" if r.get("當沖率(%)") else "—",
                    "法人淨買(張)": r.get("三大法人淨買(張)", "—"),
                    "股本(億)":   r.get("股本(億)", "—"),
                    "評分":       r["評分"],
                })

            st.dataframe(
                pd.DataFrame(table_rows),
                use_container_width=True,
                hide_index=True,
                column_config={
                    "評分":    st.column_config.NumberColumn(format="%.1f"),
                    "收盤價":  st.column_config.NumberColumn(format="%.1f"),
                },
            )

            st.divider()
            st.subheader("🔍 個股詳細資訊")

            for idx, r in enumerate(results, 1):
                limit_status = r["漲停狀態"]
                limit_icon   = "🔒" if limit_status == "LOCKED" else ("⬆️" if limit_status == "NEAR_LIMIT" else "📊")

                with st.expander(
                    f"{limit_icon}  #{idx}  {r['代號']} {r['名稱']}  ({r['市場']})  "
                    f"+{r['漲幅(%)']:.2f}%  評分：{r['評分']:.1f}",
                    expanded=(idx == 1),
                ):
                    c1, c2, c3 = st.columns(3)
                    with c1:
                        st.markdown("**📈 行情**")
                        st.write(f"收盤價：**{r['收盤價']:.2f}**")
                        st.write(f"漲幅：**+{r['漲幅(%)']:.2f}%**")
                        st.write(f"漲停狀態：**{limit_icon} {limit_status}**")
                        st.write(f"成交量：**{r.get('成交量(張)', '—')}** 張")
                        st.write(f"量比(5日)：**{r['量比(5日)']:.2f}x**" if r.get("量比(5日)") else "量比：—")
                        st.write(f"股本：**{r.get('股本(億)', '—')}** 億")
                    with c2:
                        st.markdown("**📉 技術指標**")
                        aligned = r.get("均線多頭排列")
                        st.write(f"MA{int(ma_short)}：{r.get(f'MA{int(ma_short)}', '—')}")
                        st.write(f"MA{int(ma_mid)}：{r.get(f'MA{int(ma_mid)}', '—')}")
                        st.write(f"MA{int(ma_long)}：{r.get(f'MA{int(ma_long)}', '—')}")
                        st.write(f"均線多頭排列：{'✅' if aligned is True else ('❌' if aligned is False else '❓')}")
                        st.write(f"BB上軌：{r.get('BB上軌', '—')}")
                        st.write(f"BB突破：{'✅' if r.get('BB突破') else ('❌' if r.get('BB突破') is False else '❓')}")
                    with c3:
                        st.markdown("**📋 籌碼**")
                        dt_ratio = r.get("當沖率(%)")
                        dt_ok    = r.get("當沖率正常")
                        if dt_ratio is not None:
                            st.write(f"當沖率：{'✅' if dt_ok else '⚠️'} {dt_ratio:.1f}%{'（偏高）' if not dt_ok else ''}")
                        else:
                            st.write("當沖率：— （T+2 延遲）")
                        inst = r.get("三大法人淨買(張)")
                        if inst is not None:
                            icon = "🟢" if inst > 0 else ("🔴" if inst < 0 else "⚪")
                            st.write(f"三大法人：{icon} {inst:+,} 張")
                        else:
                            st.write("三大法人：—")
                        if r.get("資料旗標"):
                            st.warning(f"⚠️ {r['資料旗標']}")

                    st.divider()
                    st.markdown("**⚠️ 交易提醒**")
                    tips = []
                    if limit_status == "LOCKED":
                        tips.append("🔒 確認鎖漲停後進場（13:00~13:25），若無法鎖住請放棄")
                    elif limit_status == "NEAR_LIMIT":
                        tips.append("⬆️ 接近漲停，13:25 前若未鎖住漲停，建議放棄")
                    tips.append("📅 隔日 09:00~09:15 開高即出清，不要等待")
                    tips.append("🛑 停損：若隔日開盤低於買入價 1% 以上，09:10 前立即砍倉")
                    tips.append("🌙 睡前觀察美股夜盤：若納指/費半大跌，隔日開盤即市價賣出")
                    if r.get("當沖率正常") is False:
                        tips.append("⚠️ 當沖率偏高，籌碼較不穩定，注意隔日開盤壓力")
                    for t in tips:
                        st.write(f"- {t}")

        st.divider()
        with st.expander("ℹ️ 資料說明與注意事項"):
            st.markdown(f"""
**資料來源**
- 全市場行情：TWSE STOCK\\_DAY\\_ALL（永遠回傳最新交易日，不支援歷史日期查詢）
- 三大法人：TWSE T86 ／ 當沖資料：TWSE TWTB4U（T+2 延遲）
- 個股歷史：TWSE/TPEX 月份 API（用於計算 MA、Bollinger、量比）

**快取：** 個股月份資料當月 1 小時 / 歷史永久；公司股本 30 天

**限制：** 漲停確認以漲幅≥{near_limit:.1f}% 作代理指標；券商分點需 FinMind 付費 API
            """)

    elif not run_clicked:
        st.info("👈 在左側調整篩選條件後，點擊「🔍 執行選股」開始掃描。")
        st.markdown("""
### 如何使用
1. **調整左側設定**：依市況調整漲幅門檻、量比等條件（修改後記得點「💾 儲存設定」）
2. **點擊執行選股**：系統掃描全市場 ~1,000 支股票，首次約需 2~5 分鐘
3. **查看結果**：展開個股詳細資訊，確認技術指標與交易提醒
4. **切換「盤中監控」分頁**：候選股已自動載入，盤中 13:00~13:25 即時確認是否進場

### 隔日沖交易守則
| 動作 | 時間 | 要點 |
|---|---|---|
| **買入** | 13:00~13:25 | 確認即將鎖漲停，13:25 還未鎖住則放棄 |
| **賣出** | 09:00~09:15 | 開高 2~3% 後第一根黑K即出清 |
| **停損** | 09:10 前 | 若開盤低於平盤 1%，立即市價賣出 |
| **夜間觀察** | 睡前 | 美股納指/費半大跌 → 隔日開盤即市價賣出 |
        """)


# ════════════════════════════════════════════════════════════
# Tab 2：盤中監控
# ════════════════════════════════════════════════════════════

with tab_monitor:
    st.caption("盤中 13:00~13:25 使用，即時確認候選股是否接近漲停")

    st.info(
        "📌 **使用流程：** 收盤後先到「選股篩選」分頁執行選股 → 候選股自動載入此頁 → "
        "隔日盤中開啟此頁，開啟自動更新，於 13:00~13:25 確認進場時機。",
        icon="💡",
    )

    # ── 監控清單管理 ──────────────────────────────────────────
    st.subheader("📋 監控清單")

    col_load, col_manual = st.columns([1, 2])

    with col_load:
        if st.session_state.results:
            n = len(st.session_state.watchlist)
            st.success(f"✅ 已載入 {n} 支（來自最近一次選股）")
            loaded_codes = ", ".join(
                f"{code}({exch})" for code, exch in st.session_state.watchlist
            )
            st.caption(loaded_codes)
            if st.button("🔄 重新從選股結果載入", use_container_width=True):
                st.session_state.watchlist = [
                    (r["代號"], r["市場"]) for r in st.session_state.results
                ]
                st.rerun()
        else:
            st.warning("尚無選股結果，請先到「選股篩選」執行選股，或手動輸入股票代號。")

    with col_manual:
        st.markdown("**手動新增監控標的**")
        manual_input = st.text_input(
            "輸入股票代號（多支用逗號隔開，上櫃加 -OTC 後綴）",
            placeholder="例：2330, 2454, 2308-OTC",
            help="上市股票直接輸入代號；上櫃股票在代號後加 -OTC（例：6547-OTC）",
        )
        if st.button("➕ 新增到監控清單", use_container_width=True):
            if manual_input.strip():
                new_entries = []
                for raw in manual_input.split(","):
                    raw = raw.strip()
                    if not raw:
                        continue
                    if raw.upper().endswith("-OTC"):
                        code = raw[:-4].strip()
                        exchange = "TPEX"
                    else:
                        code = raw
                        exchange = "TWSE"
                    if code and (code, exchange) not in st.session_state.watchlist:
                        new_entries.append((code, exchange))

                st.session_state.watchlist.extend(new_entries)
                st.success(f"已新增 {len(new_entries)} 支")
                st.rerun()

    # 顯示可移除的清單
    if st.session_state.watchlist:
        with st.expander("📝 編輯監控清單（點展開可移除個股）", expanded=False):
            to_remove = []
            for i, (code, exch) in enumerate(st.session_state.watchlist):
                col_info, col_btn = st.columns([4, 1])
                with col_info:
                    st.write(f"**{code}** ({exch})")
                with col_btn:
                    if st.button("移除", key=f"rm_{i}"):
                        to_remove.append(i)
            if to_remove:
                st.session_state.watchlist = [
                    v for i, v in enumerate(st.session_state.watchlist)
                    if i not in to_remove
                ]
                st.rerun()
            if st.button("🗑️ 清空全部", use_container_width=True):
                st.session_state.watchlist = []
                st.rerun()

    st.divider()

    # ── 即時行情面板 ──────────────────────────────────────────
    st.subheader("📡 即時行情")

    if not st.session_state.watchlist:
        st.warning("監控清單為空，請先執行選股或手動輸入股票代號。")
    else:
        # 重新整理控制列
        ctrl1, ctrl2, ctrl3 = st.columns([1, 1, 2])
        with ctrl1:
            refresh_btn = st.button("🔄 立即更新", use_container_width=True, type="primary")
        with ctrl2:
            auto_refresh = st.checkbox("⏱ 自動更新", value=False)
        with ctrl3:
            refresh_interval = st.select_slider(
                "更新間隔",
                options=[10, 15, 20, 30, 60],
                value=30,
                format_func=lambda x: f"{x} 秒",
                disabled=not auto_refresh,
            )

        # 執行即時查詢
        quotes = fetch_realtime_quotes(st.session_state.watchlist)
        refresh_time = datetime.now().strftime("%H:%M:%S")

        if not quotes:
            st.error("❌ 無法取得即時行情，可能原因：盤後時段 API 無資料、網路錯誤。")
            st.caption("注意：TWSE MIS 即時 API 僅在交易時段（09:00~13:30）有資料。")
        else:
            st.caption(f"資料時間：{refresh_time}　（最後更新：{quotes[0].get('data_time', '—')}）")

            # ── 監控卡片（每支股票） ──────────────────────────
            for q in quotes:
                status     = q["status"]
                gain       = q["gain_pct"]
                current    = q["current"]
                limit_up   = q["limit_up"]
                dist       = q["dist_limit_pct"]

                # 狀態顏色與圖示
                if status == "LOCKED":
                    icon = "🔒"
                    card_color = "#ff4b4b"
                    suggestion = "✅ **可考慮進場** — 已鎖漲停，快速確認籌碼後進場"
                elif status == "NEAR_LIMIT":
                    icon = "⬆️"
                    card_color = "#ffa500"
                    suggestion = "👀 **觀察中** — 接近漲停，距漲停 {:.2f}%，持續觀察是否鎖住".format(dist or 0)
                elif gain is not None and gain < 0:
                    icon = "🔴"
                    card_color = "#888888"
                    suggestion = "❌ **建議放棄** — 已轉跌，策略失效"
                elif gain is not None and gain < 5:
                    icon = "📊"
                    card_color = "#888888"
                    suggestion = "⏳ **尚未發動** — 漲幅不足，繼續觀察"
                else:
                    icon = "📊"
                    card_color = "#1f77b4"
                    suggestion = "⏳ **觀察中** — 漲幅達 {:.1f}%，等待接近漲停".format(gain or 0)

                with st.container(border=True):
                    h1, h2, h3 = st.columns([2, 2, 3])
                    with h1:
                        gain_str = f"+{gain:.2f}%" if gain and gain >= 0 else (f"{gain:.2f}%" if gain else "—")
                        st.markdown(
                            f"### {icon} {q['code']} {q['name']}\n"
                            f"現價：**{current:.2f}**　漲幅：**{gain_str}**"
                            if current else f"### {icon} {q['code']} {q['name']}\n現價：—（尚無成交）"
                        )
                    with h2:
                        if limit_up:
                            st.metric("漲停價", f"{limit_up:.2f}", delta=f"距 {dist:.2f}%" if dist is not None else None)
                        c_bid, c_ask = st.columns(2)
                        with c_bid:
                            bid1 = q.get("bid1")
                            bid1v = q.get("bid1_vol")
                            st.caption(f"買一　{bid1:.2f} / {bid1v}張" if bid1 and bid1v else "買一　—")
                        with c_ask:
                            ask1 = q.get("ask1")
                            ask1v = q.get("ask1_vol")
                            st.caption(f"賣一　{ask1:.2f} / {ask1v}張" if ask1 and ask1v else "賣一　—（已鎖停）")
                    with h3:
                        st.markdown(f"**進場建議：** {suggestion}")
                        vol = q.get("volume_zhang")
                        hi  = q.get("high")
                        lo  = q.get("low")
                        extra = []
                        if vol:
                            extra.append(f"成交：{vol:,}張")
                        if hi and lo:
                            extra.append(f"高{hi:.2f} / 低{lo:.2f}")
                        if extra:
                            st.caption("　".join(extra))

            st.divider()
            # ── 快速摘要表 ────────────────────────────────────
            import pandas as pd
            rows = []
            for q in quotes:
                status = q["status"]
                icon = "🔒" if status == "LOCKED" else ("⬆️" if status == "NEAR_LIMIT" else ("🔴" if (q["gain_pct"] or 0) < 0 else "📊"))
                rows.append({
                    "狀態": f"{icon} {status}",
                    "代號": q["code"],
                    "名稱": q["name"],
                    "現價": q["current"],
                    "漲幅(%)": q["gain_pct"],
                    "漲停價": q["limit_up"],
                    "距漲停(%)": q["dist_limit_pct"],
                    "成交量(張)": q["volume_zhang"],
                    "資料時間": q["data_time"],
                })
            st.dataframe(
                pd.DataFrame(rows),
                use_container_width=True,
                hide_index=True,
                column_config={
                    "現價":      st.column_config.NumberColumn(format="%.2f"),
                    "漲幅(%)":   st.column_config.NumberColumn(format="%.2f"),
                    "漲停價":    st.column_config.NumberColumn(format="%.2f"),
                    "距漲停(%)": st.column_config.NumberColumn(format="%.2f"),
                },
            )

        # ── 自動更新倒數計時 ──────────────────────────────────
        if auto_refresh:
            st.divider()
            countdown_ph = st.empty()
            for sec in range(refresh_interval, 0, -1):
                countdown_ph.caption(f"⏱ {sec} 秒後自動更新...")
                time.sleep(1)
            countdown_ph.empty()
            st.rerun()
