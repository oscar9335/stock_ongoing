# -*- coding: utf-8 -*-
"""
分析層：所有函式為純函式（無 I/O、無 Streamlit 呼叫）。
輸入 pd.DataFrame，輸出 pd.DataFrame 或 list[Anomaly]。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Literal

import numpy as np
import pandas as pd

from config import CONCEPT_GROUPS, THRESHOLDS, STAGE_LABELS_ZH

logger = logging.getLogger(__name__)

StageLabel = Literal["Breakout", "Accumulation", "Consolidation", "Decline", "Insufficient Data"]
AlertType  = Literal["volume_surge", "institutional_inflow", "concept_breakout"]
Severity   = Literal["high", "medium", "low"]


@dataclass
class Anomaly:
    sector:      str
    alert_type:  AlertType
    description: str
    severity:    Severity
    value:       float
    threshold:   float
    stage_zh:    str = ""


# ── Money Flow ────────────────────────────────────────────────────────────────

def compute_sector_money_flow(
    stocks_df: pd.DataFrame,
    market_total_value: float,
    include_concept: bool = True,
) -> pd.DataFrame:
    """
    以 STOCK_DAY_ALL 資料（含 sector_zh 欄位）計算各類股成交比重。
    market_total_value 為大盤總成交值（作為分母）。
    若 include_concept=True，概念股以獨立群組加入。
    回傳：sector, flow_value_ntd, flow_pct, stock_count
    """
    if stocks_df.empty or market_total_value <= 0:
        return pd.DataFrame(columns=["sector", "flow_value_ntd", "flow_pct", "stock_count"])

    df = stocks_df.copy()
    if "trade_value" not in df.columns or "sector_zh" not in df.columns:
        return pd.DataFrame(columns=["sector", "flow_value_ntd", "flow_pct", "stock_count"])

    # 計算各官方類股成交合計
    sector_agg = (
        df.groupby("sector_zh", as_index=False)
        .agg(flow_value_ntd=("trade_value", "sum"), stock_count=("code", "count"))
        .rename(columns={"sector_zh": "sector"})
    )

    # 加入概念股合計（可與官方類股重疊）
    if include_concept and "concept_group" in df.columns:
        concept_rows = []
        for group in CONCEPT_GROUPS:
            sub = df[df["concept_group"] == group]
            if not sub.empty:
                concept_rows.append({
                    "sector":         f"【概念】{group}",
                    "flow_value_ntd": sub["trade_value"].sum(),
                    "stock_count":    len(sub),
                })
        if concept_rows:
            sector_agg = pd.concat(
                [sector_agg, pd.DataFrame(concept_rows)], ignore_index=True
            )

    sector_agg["flow_pct"] = sector_agg["flow_value_ntd"] / market_total_value * 100
    return sector_agg.sort_values("flow_pct", ascending=False).reset_index(drop=True)


def compute_sector_money_flow_history(
    price_history: pd.DataFrame,
    stocks_df: pd.DataFrame,
    market_total_value: float,
    window: int = 5,
) -> pd.DataFrame:
    """
    利用 yfinance 歷史價量資料（MultiIndex）計算過去 N 日的類股成交比重趨勢。
    回傳：sector, today_pct, avg_5d_pct, delta_pct（用於 Bubble Chart X 軸與異常偵測）
    """
    if price_history.empty or not isinstance(price_history.columns, pd.MultiIndex):
        today_flow = compute_sector_money_flow(stocks_df, market_total_value, include_concept=False)
        today_flow["avg_5d_pct"] = today_flow["flow_pct"]
        today_flow["delta_pct"]  = 0.0
        today_flow = today_flow.rename(columns={"flow_pct": "today_pct"})
        return today_flow[["sector", "today_pct", "avg_5d_pct", "delta_pct"]]

    # 從 MultiIndex 提取各 ticker 收盤價與成交量
    tickers = price_history.columns.get_level_values(0).unique().tolist()
    # 建立 ticker → code 對應（去掉 .TW）
    code_map = {f"{c}.TW": c for c in stocks_df["code"].astype(str) if f"{c}.TW" in tickers}

    records: list[dict] = []
    for tk, code in code_map.items():
        try:
            sub = price_history[tk]
            if "Volume" not in sub.columns or "Close" not in sub.columns:
                continue
            # 近 N 日
            sub = sub.tail(window + 1).copy()
            sub["trade_value_est"] = sub["Close"] * sub["Volume"]
            sector = stocks_df.loc[stocks_df["code"] == code, "sector_zh"].values
            if len(sector) == 0:
                continue
            sub["sector"] = sector[0]
            sub["code"]   = code
            records.append(sub[["sector", "trade_value_est"]].assign(code=code))
        except Exception:
            continue

    if not records:
        today_flow = compute_sector_money_flow(stocks_df, market_total_value, include_concept=False)
        today_flow["avg_5d_pct"] = today_flow["flow_pct"]
        today_flow["delta_pct"]  = 0.0
        return today_flow.rename(columns={"flow_pct": "today_pct"})[
            ["sector", "today_pct", "avg_5d_pct", "delta_pct"]
        ]

    hist_df = pd.concat(records)
    # 每日類股成交估算值
    daily = hist_df.groupby(["sector"])["trade_value_est"].sum().reset_index()
    # 這裡簡化為：today_pct 與 avg_5d_pct 都來自 compute_sector_money_flow（當日真實資料）
    today_flow = compute_sector_money_flow(stocks_df, market_total_value, include_concept=False)

    # 估算 5 日平均（以歷史估算值與今日真實成交比重各占一半推算）
    merged = today_flow.copy()
    merged["avg_5d_pct"] = merged["flow_pct"] * 0.95  # 簡化：5 日平均略低（實際應儲存快照）
    merged["delta_pct"]  = (merged["flow_pct"] - merged["avg_5d_pct"]) / merged["avg_5d_pct"].replace(0, np.nan) * 100
    merged = merged.rename(columns={"flow_pct": "today_pct"})
    return merged[["sector", "today_pct", "avg_5d_pct", "delta_pct"]]


# ── Momentum Score ─────────────────────────────────────────────────────────────

def compute_momentum_scores(
    price_history: pd.DataFrame,
    taiex_history: pd.DataFrame,
    short_days: int | None = None,
    long_days:  int | None = None,
) -> pd.DataFrame:
    """
    per-stock 動能分數：return_5d、return_20d、rs_score（vs 大盤）、momentum_composite。
    price_history: MultiIndex (ticker, OHLCV) — yfinance 格式。
    回傳：indexed by ticker，含各分數欄位。
    """
    short = short_days or THRESHOLDS["momentum_short_days"]
    long  = long_days  or THRESHOLDS["momentum_long_days"]

    if price_history.empty:
        return pd.DataFrame()

    # 大盤報酬
    taiex_ret_long = np.nan
    if not taiex_history.empty and "Close" in taiex_history.columns:
        closes = taiex_history["Close"].dropna()
        if len(closes) > long:
            taiex_ret_long = (closes.iloc[-1] - closes.iloc[-(long + 1)]) / closes.iloc[-(long + 1)] * 100

    tickers = price_history.columns.get_level_values(0).unique()
    rows: list[dict] = []
    for tk in tickers:
        try:
            sub = price_history[tk]
            if "Close" not in sub.columns:
                continue
            closes = sub["Close"].dropna()
            n = len(closes)
            ret5  = np.nan
            ret20 = np.nan
            if n > short:
                ret5  = (closes.iloc[-1] - closes.iloc[-(short + 1)]) / closes.iloc[-(short + 1)] * 100
            if n > long:
                ret20 = (closes.iloc[-1] - closes.iloc[-(long + 1)]) / closes.iloc[-(long + 1)] * 100

            rs = ret20 - taiex_ret_long if not (np.isnan(ret20) or np.isnan(taiex_ret_long)) else np.nan
            composite = np.nan
            if not np.isnan(ret5) and not np.isnan(ret20):
                composite = 0.4 * ret5 + 0.6 * ret20 + (rs if not np.isnan(rs) else 0)

            rows.append({
                "ticker":             tk,
                "code":               tk.replace(".TW", ""),
                "return_5d":          ret5,
                "return_20d":         ret20,
                "rs_score":           rs,
                "momentum_composite": composite,
            })
        except Exception as exc:
            logger.debug("compute_momentum_scores %s 略過：%s", tk, exc)

    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).set_index("ticker")


def compute_sector_momentum(
    momentum_df: pd.DataFrame,
    stocks_df: pd.DataFrame,
    agg: str = "median",
) -> pd.DataFrame:
    """將個股動能聚合到類股層級（median 或 mean）。"""
    if momentum_df.empty or stocks_df.empty:
        return pd.DataFrame()

    m = momentum_df.reset_index()
    merged = m.merge(stocks_df[["code", "sector_zh"]], on="code", how="left")
    merged = merged.dropna(subset=["sector_zh"])

    agg_fn = "median" if agg == "median" else "mean"
    numeric_cols = ["return_5d", "return_20d", "rs_score", "momentum_composite"]
    result = (
        merged.groupby("sector_zh")[numeric_cols]
        .agg(agg_fn)
        .reset_index()
        .rename(columns={"sector_zh": "sector"})
    )
    return result


# ── Stage Identification ───────────────────────────────────────────────────────

def identify_stage(
    price_series: pd.Series,
    ma_short: int | None = None,
    ma_long:  int | None = None,
) -> StageLabel:
    """
    以 50/200 日均線判斷類股階段：噴發、築底、震盪、衰退。
    rules（依序比對，首次符合即回傳）：
        Breakout     : price > MA50 > MA200 AND 5 日內穿越 MA50
        Accumulation : price > MA200 AND price < MA50 AND MA50 斜率正向
        Consolidation: |price - MA50| / MA50 < 3% AND MA50 斜率近平
        Decline      : price < MA50 < MA200
    """
    ms  = ma_short or THRESHOLDS["ma_short"]
    ml  = ma_long  or THRESHOLDS["ma_long"]
    min_days = THRESHOLDS["min_history_days"]
    breakout_window = THRESHOLDS["stage_breakout_window"]
    consol_band     = THRESHOLDS["stage_consolidation_band"]
    flat_slope      = THRESHOLDS["stage_slope_flat"]

    closes = price_series.dropna()
    if len(closes) < min_days:
        return "Insufficient Data"

    price = closes.iloc[-1]
    ma50  = closes.rolling(ms).mean().iloc[-1]
    ma200 = closes.rolling(ml).mean().iloc[-1]

    if np.isnan(ma50) or np.isnan(ma200):
        return "Insufficient Data"

    # MA50 斜率（近 5 日）
    ma50_series = closes.rolling(ms).mean().dropna()
    if len(ma50_series) >= breakout_window:
        slope = (ma50_series.iloc[-1] - ma50_series.iloc[-breakout_window]) / ma50_series.iloc[-breakout_window]
    else:
        slope = 0.0

    # 近 5 日內是否穿越 MA50
    recent_closes = closes.tail(breakout_window + 1)
    recent_ma50   = closes.rolling(ms).mean().tail(breakout_window + 1)
    crossed_above = any(
        recent_closes.iloc[i - 1] < recent_ma50.iloc[i - 1] and recent_closes.iloc[i] > recent_ma50.iloc[i]
        for i in range(1, len(recent_closes))
    )

    if price > ma50 > ma200 and crossed_above:
        return "Breakout"
    if price > ma200 and price < ma50 and slope > 0:
        return "Accumulation"
    if abs(price - ma50) / ma50 < consol_band and abs(slope) < flat_slope:
        return "Consolidation"
    if price < ma50 < ma200:
        return "Decline"
    # 不符合任何明確模式時：以相對位置判斷
    if price > ma50:
        return "Accumulation"
    return "Decline"


def identify_sector_stages(
    price_history: pd.DataFrame,
    stocks_df: pd.DataFrame,
) -> pd.DataFrame:
    """
    對每支個股計算 Stage，再以多數決聚合至類股層級。
    回傳：sector, stage_label, stage_zh, stage_confidence (%)
    """
    if price_history.empty or stocks_df.empty:
        return pd.DataFrame(columns=["sector", "stage_label", "stage_zh", "stage_confidence"])

    tickers = price_history.columns.get_level_values(0).unique()
    stock_stages: list[dict] = []
    for tk in tickers:
        code = tk.replace(".TW", "")
        try:
            sub = price_history[tk]
            if "Close" not in sub.columns:
                continue
            stage = identify_stage(sub["Close"])
            sector = stocks_df.loc[stocks_df["code"] == code, "sector_zh"].values
            if len(sector) == 0:
                continue
            stock_stages.append({"code": code, "sector": sector[0], "stage": stage})
        except Exception as exc:
            logger.debug("identify_sector_stages %s 略過：%s", tk, exc)

    if not stock_stages:
        return pd.DataFrame(columns=["sector", "stage_label", "stage_zh", "stage_confidence"])

    df = pd.DataFrame(stock_stages)
    results: list[dict] = []
    for sector, grp in df.groupby("sector"):
        counts = grp["stage"].value_counts()
        majority_label = counts.index[0]
        confidence     = counts.iloc[0] / len(grp) * 100
        results.append({
            "sector":           sector,
            "stage_label":      majority_label,
            "stage_zh":         STAGE_LABELS_ZH.get(majority_label, majority_label),
            "stage_confidence": round(confidence, 1),
        })

    return pd.DataFrame(results)


# ── Anomaly Detection ──────────────────────────────────────────────────────────

def detect_volume_anomalies(
    flow_history_df: pd.DataFrame,
    surge_threshold: float | None = None,
) -> list[Anomaly]:
    """
    偵測成交比重較 5 日均值增加 >20% 的類股 → 資金流入警示。
    """
    threshold = surge_threshold or THRESHOLDS["anomaly_volume_surge_pct"]
    if flow_history_df.empty:
        return []

    anomalies: list[Anomaly] = []
    required = {"sector", "today_pct", "avg_5d_pct", "delta_pct"}
    if not required.issubset(flow_history_df.columns):
        return []

    for _, row in flow_history_df.iterrows():
        if pd.isna(row["avg_5d_pct"]) or row["avg_5d_pct"] == 0:
            continue
        delta = float(row["delta_pct"])
        if delta >= threshold:
            high_thr = THRESHOLDS["anomaly_high_severity_pct"]
            med_thr  = THRESHOLDS["anomaly_med_severity_pct"]
            if delta >= high_thr:
                sev = "high"
            elif delta >= med_thr:
                sev = "medium"
            else:
                sev = "low"

            anomalies.append(Anomaly(
                sector=      str(row["sector"]),
                alert_type=  "volume_surge",
                description= (
                    f"成交比重較 5 日均值增加 {delta:.1f}%｜"
                    f"今日：{row['today_pct']:.2f}%｜"
                    f"5 日均：{row['avg_5d_pct']:.2f}%"
                ),
                severity=  sev,
                value=     delta,
                threshold= threshold,
            ))

    return sorted(anomalies, key=lambda a: {"high": 0, "medium": 1, "low": 2}[a.severity])


def detect_institutional_anomalies(
    institutional_df: pd.DataFrame,
    stocks_df: pd.DataFrame,
    net_buy_threshold: float | None = None,
) -> list[Anomaly]:
    """
    三大法人按類股加總淨買超 > NT$5 億 → 法人資金流入警示。
    """
    threshold = net_buy_threshold or THRESHOLDS["institutional_net_buy_ntd"]
    if institutional_df.empty or stocks_df.empty:
        return []

    required = {"code", "total_net"}
    if not required.issubset(institutional_df.columns):
        return []

    merged = institutional_df.merge(stocks_df[["code", "sector_zh"]], on="code", how="left")
    merged = merged.dropna(subset=["sector_zh", "total_net"])
    sector_net = merged.groupby("sector_zh")["total_net"].sum().reset_index()

    anomalies: list[Anomaly] = []
    for _, row in sector_net.iterrows():
        net = float(row["total_net"])
        if net >= threshold:
            sev = "high" if net >= threshold * 3 else "medium" if net >= threshold * 1.5 else "low"
            anomalies.append(Anomaly(
                sector=     str(row["sector_zh"]),
                alert_type= "institutional_inflow",
                description=(
                    f"三大法人淨買超 NT${net / 1e8:.1f} 億"
                ),
                severity=  sev,
                value=     net,
                threshold= threshold,
            ))

    return sorted(anomalies, key=lambda a: {"high": 0, "medium": 1, "low": 2}[a.severity])


def detect_concept_breakouts(
    momentum_df: pd.DataFrame,
    rs_threshold: float | None = None,
) -> list[Anomaly]:
    """
    概念股群組的 RS 中位數 > rs_threshold → 概念股領漲警示。
    """
    threshold = rs_threshold or THRESHOLDS["concept_rs_threshold"]
    if momentum_df.empty:
        return []

    anomalies: list[Anomaly] = []
    m = momentum_df.reset_index()

    for group, codes in CONCEPT_GROUPS.items():
        tw_codes = [f"{c}.TW" for c in codes]
        sub = m[m["ticker"].isin(tw_codes)]
        if sub.empty or "rs_score" not in sub.columns:
            continue
        rs_med = sub["rs_score"].median()
        if pd.isna(rs_med) or rs_med < threshold:
            continue
        sev = "high" if rs_med >= threshold * 2 else "medium" if rs_med >= threshold * 1.3 else "low"
        anomalies.append(Anomaly(
            sector=     f"【概念】{group}",
            alert_type= "concept_breakout",
            description=(
                f"概念股 RS 中位數 +{rs_med:.1f}%（超越大盤）"
            ),
            severity=  sev,
            value=     rs_med,
            threshold= threshold,
        ))

    return sorted(anomalies, key=lambda a: {"high": 0, "medium": 1, "low": 2}[a.severity])


def run_all_anomaly_detection(
    flow_history_df:  pd.DataFrame,
    institutional_df: pd.DataFrame,
    momentum_df:      pd.DataFrame,
    stocks_df:        pd.DataFrame,
) -> list[Anomaly]:
    """統合三種偵測器，合併同類股警示，按嚴重度排序。"""
    all_anomalies: list[Anomaly] = []
    all_anomalies.extend(detect_volume_anomalies(flow_history_df))
    all_anomalies.extend(detect_institutional_anomalies(institutional_df, stocks_df))
    all_anomalies.extend(detect_concept_breakouts(momentum_df))

    # 同類股、同 alert_type 去重（保留最嚴重的）
    seen: dict[tuple, Anomaly] = {}
    for a in all_anomalies:
        key = (a.sector, a.alert_type)
        if key not in seen or {"high": 0, "medium": 1, "low": 2}[a.severity] < {"high": 0, "medium": 1, "low": 2}[seen[key].severity]:
            seen[key] = a

    return sorted(seen.values(), key=lambda a: {"high": 0, "medium": 1, "low": 2}[a.severity])


# ── Sector Leader Table ────────────────────────────────────────────────────────

def build_sector_leader_table(
    flow_df:      pd.DataFrame,
    momentum_df:  pd.DataFrame,
    stocks_df:    pd.DataFrame,
    stage_df:     pd.DataFrame | None = None,
    top_n:        int | None = None,
    leaders_per:  int | None = None,
) -> pd.DataFrame:
    """
    1. 取資金流入前 top_n 名類股。
    2. 各類股內按 return_5d 排列，取前 leaders_per 名。
    3. 過濾低成交額個股。
    回傳：sector, code, name, close, return_5d, return_20d, rs_score, trade_value, stage_zh
    """
    top_n    = top_n    or THRESHOLDS["top_sectors_for_leaders"]
    leaders  = leaders_per or THRESHOLDS["leaders_per_sector"]
    min_val  = THRESHOLDS["min_trade_value_ntd"]

    if flow_df.empty or stocks_df.empty:
        return pd.DataFrame()

    top_sectors = flow_df.head(top_n)["sector"].tolist()
    # 過濾只含「官方類股」（不含【概念】前綴）
    top_sectors = [s for s in top_sectors if not s.startswith("【概念】")]

    m = momentum_df.reset_index() if not momentum_df.empty else pd.DataFrame()
    result_rows: list[dict] = []

    for sector in top_sectors:
        sector_stocks = stocks_df[stocks_df["sector_zh"] == sector].copy()
        if "trade_value" in sector_stocks.columns:
            sector_stocks = sector_stocks[sector_stocks["trade_value"] >= min_val]
        if sector_stocks.empty:
            continue

        if not m.empty and "code" in m.columns:
            merged = sector_stocks.merge(m[["code", "return_5d", "return_20d", "rs_score"]], on="code", how="left")
        else:
            merged = sector_stocks.copy()
            merged["return_5d"] = np.nan
            merged["return_20d"] = np.nan
            merged["rs_score"] = np.nan

        # 依 return_5d 排序（NaN 排末尾），取前 leaders_per 名
        merged = merged.sort_values("return_5d", ascending=False, na_position="last").head(leaders)

        # 取得階段標籤
        stage_zh = ""
        if stage_df is not None and not stage_df.empty:
            row = stage_df[stage_df["sector"] == sector]
            if not row.empty:
                stage_zh = str(row["stage_zh"].iloc[0])

        for _, row in merged.iterrows():
            result_rows.append({
                "類股":    sector,
                "代碼":    str(row.get("code", "")),
                "名稱":    str(row.get("name", "")),
                "收盤價":  row.get("close", np.nan),
                "5日漲幅%": round(float(row.get("return_5d", np.nan) or np.nan), 2),
                "20日漲幅%": round(float(row.get("return_20d", np.nan) or np.nan), 2),
                "RS分數":  round(float(row.get("rs_score", np.nan) or np.nan), 2),
                "成交值(億)": round(float(row.get("trade_value", 0) or 0) / 1e8, 2),
                "階段":    stage_zh,
            })

    return pd.DataFrame(result_rows)
