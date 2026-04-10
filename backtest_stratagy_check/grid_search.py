# grid_search.py
# 台股隔日沖回測 — Grid Search 參數最佳化工具
#
# 用途：系統性測試多組參數組合，找出最佳策略設定
# 特點：直接讀取 cache（不呼叫 API），資料載入一次後重複使用
#
# 使用方式：
#   python grid_search.py                             # 自動偵測日期
#   python grid_search.py --start 2026-01-01 --end 2026-04-03
#   python grid_search.py --full-grid                 # 完整網格（480 組）

import argparse
import copy
import csv
import glob
import io
import itertools
import json
import logging
import os
import re
import sys
from collections import defaultdict
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

import config
from backtest_engine import BacktestSummary, run_backtest
from backtest_main import load_settings

logger = logging.getLogger(__name__)

# ============================================================
# 參數網格定義
# ============================================================

# 預設網格：5×4×4×2 = 160 組
PARAM_GRID_DEFAULT: Dict[str, list] = {
    "min_gain_pct":      [5.0, 6.0, 7.0, 8.0, 9.0],
    "volume_multiplier": [1.5, 2.0, 2.5, 3.0],
    "bt_top_n":          [1, 2, 3, 5],
    "bt_skip_locked":    [True, False],
}

# 完整網格（--full-grid）：另加 max_day_trade_ratio，160×3 = 480 組
PARAM_GRID_FULL: Dict[str, list] = {
    **PARAM_GRID_DEFAULT,
    "max_day_trade_ratio": [0.4, 0.6, 0.8],
}

# 參數顯示標籤（中文）
PARAM_LABELS = {
    "min_gain_pct":        "最低漲幅(%)",
    "volume_multiplier":   "量比門檻",
    "bt_top_n":            "最多持股數",
    "bt_skip_locked":      "跳過鎖死",
    "max_day_trade_ratio": "最大當沖率",
}


# ============================================================
# Cache 工具
# ============================================================

def _load_json_file(path: str) -> Optional[object]:
    """直接讀取 JSON 檔案（繞過 TTL 檢查）。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _yyyymm_range_list(start_d: date, end_d: date, buffer_months: int = 2) -> List[str]:
    """
    回傳從 (start_d - buffer_months) 到 end_d 之間所有 YYYYMM 字串。

    例：start=2026-01-15, end=2026-04-03, buffer=2
      → ["202511", "202512", "202601", "202602", "202603", "202604"]
    """
    y, m = start_d.year, start_d.month
    for _ in range(buffer_months):
        if m == 1:
            y, m = y - 1, 12
        else:
            m -= 1

    result = []
    ey, em = end_d.year, end_d.month
    while (y, m) <= (ey, em):
        result.append(f"{y}{m:02d}")
        if m == 12:
            y, m = y + 1, 1
        else:
            m += 1
    return result


# ============================================================
# 日期區間偵測與驗證
# ============================================================

def _collect_cache_dates(cache_dir: str, max_files_per_month: int = 30) -> List[str]:
    """
    掃描 twse_*_YYYYMM.json 快取，取樣讀取後回傳所有出現的交易日（排序）。
    每個月份最多取 max_files_per_month 個檔案做樣本，避免掃描過多檔案。
    """
    pattern = os.path.join(cache_dir, "twse_*_*.json")
    files = glob.glob(pattern)
    if not files:
        return []

    # 按月份分組
    monthly: Dict[str, List[str]] = defaultdict(list)
    for fpath in files:
        m = re.search(r"_(\d{6})\.json$", os.path.basename(fpath))
        if m:
            monthly[m.group(1)].append(fpath)

    all_dates: set = set()
    for yyyymm in sorted(monthly.keys()):
        sample = monthly[yyyymm][:max_files_per_month]
        for fpath in sample:
            data = _load_json_file(fpath)
            if isinstance(data, list):
                for row in data:
                    if isinstance(row, dict) and "date" in row:
                        all_dates.add(row["date"])

    return sorted(all_dates)


def validate_date_range(
    start_date: date,
    end_date: date,
    cache_dir: str,
) -> Tuple[bool, str]:
    """
    驗證使用者指定的日期區間是否有足夠的 cache 資料（含 MA20 buffer）。
    Returns: (is_valid, error_message)
    """
    ma_long = config.MA_LONG  # 20 個交易日

    print("  掃描 cache 可用日期範圍...")
    dates = _collect_cache_dates(cache_dir)
    if not dates:
        return False, (
            "[錯誤] cache 目錄中沒有任何歷史資料。\n"
            "       請先執行: python backtest_main.py 下載資料。"
        )

    earliest_cache = dates[0]
    latest_cache = dates[-1]
    start_iso = start_date.isoformat()
    end_iso = end_date.isoformat()

    print(f"  cache 可用範圍：{earliest_cache} ~ {latest_cache}（共 {len(dates)} 個交易日）")

    # 檢查 end_date
    if end_iso > latest_cache:
        return False, (
            f"[錯誤] 指定結束日 {end_iso} 超出 cache 最晚資料 {latest_cache}。\n"
            f"       請先執行: python backtest_main.py --end {end_iso} 下載資料。"
        )

    # 檢查 start_date
    if start_iso < earliest_cache:
        return False, (
            f"[錯誤] 指定起始日 {start_iso} 早於 cache 最早資料 {earliest_cache}。\n"
            f"       請先執行: python backtest_main.py --start {start_iso} 下載資料。"
        )

    # 檢查 MA buffer：start_date 之前必須有 >= ma_long 個交易日
    dates_before_start = [d for d in dates if d < start_iso]
    if len(dates_before_start) < ma_long:
        # 建議安全起始日（cache 中第 ma_long+1 個交易日）
        safe_start = dates[ma_long] if len(dates) > ma_long else dates[-1]
        return False, (
            f"[錯誤] MA{ma_long} 計算需要 {start_iso} 前至少 {ma_long} 個交易日的歷史，\n"
            f"       但 cache 最早只有 {earliest_cache}（目前僅有 {len(dates_before_start)} 個前置交易日）。\n"
            f"       建議將起始日改為: python grid_search.py --start {safe_start} --end {end_iso}"
        )

    return True, ""


def auto_detect_date_range(cache_dir: str) -> Tuple[Optional[str], Optional[str]]:
    """
    自動從 cache 推斷安全的回測起始日（跳過 MA20 buffer）與結束日。
    Returns: (start_iso, end_iso) or (None, None)
    """
    ma_long = config.MA_LONG  # 20
    min_backtest_days = 20  # 至少要有 20 個可回測的交易日

    dates = _collect_cache_dates(cache_dir)
    total = len(dates)

    if total < ma_long + min_backtest_days:
        return None, None

    # 跳過前 ma_long 個交易日（作為 MA 計算用的歷史 lookback）
    start_iso = dates[ma_long]
    end_iso = dates[-1]
    return start_iso, end_iso


# ============================================================
# 資料載入（純 cache，不呼叫 API）
# ============================================================

def load_name_map_from_cache(cache_dir: str) -> Dict[str, str]:
    """從 cache/stock_name_map.json 讀取名稱對照表。"""
    path = os.path.join(cache_dir, "stock_name_map.json")
    data = _load_json_file(path)
    return data if isinstance(data, dict) else {}


def load_capital_map_from_cache(cache_dir: str) -> Dict[str, int]:
    """從 cache/company_capital.json 讀取股本資料。"""
    path = os.path.join(cache_dir, "company_capital.json")
    data = _load_json_file(path)
    if isinstance(data, dict):
        result = {}
        for k, v in data.items():
            try:
                result[k] = int(v)
            except (TypeError, ValueError):
                pass
        return result
    return {}


def load_all_histories_from_cache(
    cache_dir: str,
    code_list: List[str],
    start_date: date,
    end_date: date,
    ma_buffer: int = 2,
) -> Dict[str, List[Dict]]:
    """
    直接掃描 cache 目錄讀取歷史資料（不呼叫 API）。
    讀取範圍：(start_date - ma_buffer 個月) 到 end_date 的所有月份。
    """
    yyyymm_set = set(_yyyymm_range_list(start_date, end_date, buffer_months=ma_buffer))
    code_set = set(code_list)

    raw: Dict[str, List[Dict]] = defaultdict(list)

    pattern = os.path.join(cache_dir, "twse_*_*.json")
    files = glob.glob(pattern)

    loaded_files = 0
    for fpath in files:
        basename = os.path.basename(fpath)
        m = re.match(r"twse_(\w+)_(\d{6})\.json$", basename)
        if not m:
            continue
        code, yyyymm = m.group(1), m.group(2)

        if code not in code_set or yyyymm not in yyyymm_set:
            continue

        data = _load_json_file(fpath)
        if isinstance(data, list):
            raw[code].extend(data)
            loaded_files += 1

    print(f"  讀取 {loaded_files} 個月份 cache 檔，覆蓋 {len(raw)} 支股票")

    # 去重 + 排序
    result: Dict[str, List[Dict]] = {}
    for code, records in raw.items():
        seen: set = set()
        unique: List[Dict] = []
        for r in records:
            d = r.get("date", "")
            if d and d not in seen and r.get("close") is not None:
                seen.add(d)
                unique.append(r)
        unique.sort(key=lambda x: x["date"])
        if unique:
            result[code] = unique

    return result


# ============================================================
# Grid Search 執行
# ============================================================

def _summary_to_row(params: Dict, summary: BacktestSummary) -> Dict:
    """將一次回測結果轉成 dict（供 CSV / 報告使用）。"""
    dd = summary.max_drawdown
    dd_pct = (dd / summary.initial_capital * 100) if summary.initial_capital else 0.0
    row: Dict = dict(params)
    row.update({
        "total_pnl":        round(summary.total_pnl, 0),
        "total_pnl_pct":    round(summary.total_pnl_pct, 2),
        "win_rate":         round(summary.win_rate, 1) if summary.win_rate is not None else 0.0,
        "max_drawdown":     round(dd, 0),
        "max_drawdown_pct": round(dd_pct, 2),
        "trade_count":      len(summary.all_trades),
        "trade_days":       summary.trade_days,
        "avg_trade_pnl":    round(summary.avg_trade_pnl, 0) if summary.avg_trade_pnl is not None else 0.0,
        "final_capital":    round(summary.final_capital, 0),
    })
    return row


def _make_progress_bar(current: int, total: int, width: int = 28) -> str:
    filled = int(width * current / total)
    return "█" * filled + "░" * (width - filled)


def run_grid_search(
    base_cfg: Dict,
    param_grid: Dict,
    all_histories: Dict,
    name_map: Dict,
    capital_map: Dict,
    start_iso: str,
    end_iso: str,
) -> Tuple[List[Dict], List[Dict]]:
    """
    執行所有參數組合的回測。

    Returns:
        (result_rows, equity_curves)
        - result_rows:   每個組合的績效指標 list
        - equity_curves: 每個組合的逐日資金曲線 list
    """
    param_keys = list(param_grid.keys())
    combos = list(itertools.product(*[param_grid[k] for k in param_keys]))
    total = len(combos)

    print(f"  共 {total} 個組合，開始執行...\n")

    result_rows: List[Dict] = []
    equity_curves: List[Dict] = []
    failed = 0

    for i, combo_vals in enumerate(combos, 1):
        params = dict(zip(param_keys, combo_vals))
        cfg = copy.deepcopy(base_cfg)
        cfg.update(params)

        # 進度列
        bar = _make_progress_bar(i, total)
        pct = i / total * 100
        short_params = " ".join(
            f"{k.split('_')[-1]}={'Y' if v is True else ('N' if v is False else v)}"
            for k, v in params.items()
        )
        print(f"\r  [{bar}] {pct:5.1f}% ({i}/{total}) {short_params:<55}", end="", flush=True)

        try:
            summary = run_backtest(
                start_date=start_iso,
                end_date=end_iso,
                cfg=cfg,
                trading_cfg=cfg,
                all_histories=all_histories,
                name_map=name_map,
                capital_map=capital_map,
            )
        except Exception as e:
            logger.warning("組合 %d 執行失敗：%s", i, e)
            failed += 1
            continue

        row = _summary_to_row(params, summary)
        row["combo_id"] = i
        result_rows.append(row)

        # 逐日資金曲線
        curve = [
            {"date": dr.date, "portfolio_value": round(dr.portfolio_value, 0)}
            for dr in summary.daily_results
            if not dr.skipped
        ]
        equity_curves.append({"combo_id": i, "params": params, "equity_curve": curve})

    print(f"\r  {'':80}")
    success = len(result_rows)
    print(f"  完成：{success}/{total} 個組合成功" + (f"（{failed} 個失敗）" if failed else "") + "\n")
    return result_rows, equity_curves


# ============================================================
# 報告生成
# ============================================================

def _fmt_pct(v: float) -> str:
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.2f}%"


def generate_summary_txt(
    results: List[Dict],
    param_grid: Dict,
    start_iso: str,
    end_iso: str,
    output_path: str,
) -> None:
    """產生文字摘要報告（summary.txt）。"""
    buf = io.StringIO()
    w = buf.write
    total = len(results)

    if not results:
        w("[警告] 無回測結果。\n")
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(buf.getvalue())
        return

    sorted_res = sorted(results, key=lambda x: x["total_pnl_pct"], reverse=True)
    param_keys = list(param_grid.keys())

    w("=" * 68 + "\n")
    w("  Grid Search 回測報告\n")
    w(f"  期間：{start_iso} ~ {end_iso}\n")
    w(f"  掃描組合數：{total}  |  完成：{total}\n")
    w("=" * 68 + "\n\n")

    # ── Top 10 ──────────────────────────────────────────────────
    w("【最佳參數組合 Top 10】（依總損益率排序）\n")
    # 動態計算欄寬
    param_widths = [max(len(PARAM_LABELS.get(k, k)), 8) + 2 for k in param_keys]

    # 標頭
    hdr = "#   "
    for k, pw in zip(param_keys, param_widths):
        hdr += PARAM_LABELS.get(k, k).ljust(pw)
    hdr += "損益率    勝率     回撤%    交易數"
    w(hdr + "\n")
    w("-" * 68 + "\n")

    for rank, row in enumerate(sorted_res[:10], 1):
        line = f"{rank:<4}"
        for k, pw in zip(param_keys, param_widths):
            v = row.get(k, "")
            if v is True:
                line += "Y".ljust(pw)
            elif v is False:
                line += "N".ljust(pw)
            elif isinstance(v, float):
                line += f"{v:.1f}".ljust(pw)
            else:
                line += str(v).ljust(pw)
        line += (
            f"{_fmt_pct(row['total_pnl_pct']):<10}"
            f"{row['win_rate']:<9.1f}"
            f"{row['max_drawdown_pct']:<9.2f}"
            f"{row['trade_count']}\n"
        )
        w(line)
    w("\n")

    # ── 單一參數敏感度分析 ───────────────────────────────────────
    w("【單一參數敏感度分析】（彙總所有含該值的組合，非固定其他參數）\n")
    w("  → 若需自訂固定值分析，請篩選 results/results_*.csv\n\n")

    for key, values in param_grid.items():
        label = PARAM_LABELS.get(key, key)
        n_per_val = total // len(values) if values else 0
        w(f"◆ {key}（{label}）—— 各值約有 {n_per_val} 個組合\n")
        w(f"  {'值':<12}{'平均損益率':<13}{'最佳損益率':<13}{'平均勝率':<11}{'平均回撤%':<12}{'平均交易數'}\n")
        w("  " + "-" * 66 + "\n")

        rows_by_val = {}
        for v in values:
            subset = [r for r in results if r.get(key) == v]
            if not subset:
                continue
            rows_by_val[v] = {
                "avg_pnl":  sum(r["total_pnl_pct"] for r in subset) / len(subset),
                "best_pnl": max(r["total_pnl_pct"] for r in subset),
                "avg_wr":   sum(r["win_rate"] for r in subset) / len(subset),
                "avg_dd":   sum(r["max_drawdown_pct"] for r in subset) / len(subset),
                "avg_tc":   sum(r["trade_count"] for r in subset) / len(subset),
            }

        # 找平均損益率最高的值
        best_val = max(rows_by_val, key=lambda v: rows_by_val[v]["avg_pnl"]) if rows_by_val else None

        for v, stats in rows_by_val.items():
            marker = "  ★ 平均最高" if v == best_val else ""
            v_str = "Y" if v is True else ("N" if v is False else str(v))
            w(
                f"  {v_str:<12}"
                f"{_fmt_pct(stats['avg_pnl']):<13}"
                f"{_fmt_pct(stats['best_pnl']):<13}"
                f"{stats['avg_wr']:<11.1f}"
                f"{stats['avg_dd']:<12.2f}"
                f"{stats['avg_tc']:.0f}"
                f"{marker}\n"
            )
        w("\n")

    w("=" * 68 + "\n")
    w(f"完整結果  → results/results_*.csv（{total} 筆，含全部參數與指標）\n")
    w("互動圖表  → report.html（用瀏覽器開啟）\n")
    w("資金曲線  → equity_curves.json\n")
    w("=" * 68 + "\n")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(buf.getvalue())


def generate_results_csv(results: List[Dict], output_path: str) -> None:
    """產生 CSV 全量結果（依損益率降序排列）。"""
    if not results:
        return

    sorted_res = sorted(results, key=lambda x: x.get("total_pnl_pct", 0), reverse=True)
    # 欄位順序：combo_id, params..., metrics...
    param_keys = [k for k in sorted_res[0] if k not in (
        "combo_id", "total_pnl", "total_pnl_pct", "win_rate",
        "max_drawdown", "max_drawdown_pct", "trade_count",
        "trade_days", "avg_trade_pnl", "final_capital"
    )]
    metric_keys = [
        "total_pnl_pct", "win_rate", "max_drawdown_pct", "trade_count",
        "trade_days", "avg_trade_pnl", "total_pnl", "max_drawdown", "final_capital",
    ]
    fieldnames = ["combo_id"] + param_keys + metric_keys

    with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(sorted_res)


def generate_equity_json(equity_curves: List[Dict], output_path: str) -> None:
    """儲存逐日資金曲線 JSON。"""
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(equity_curves, f, ensure_ascii=False, separators=(",", ":"))


def generate_plotly_html(
    results: List[Dict],
    equity_curves: List[Dict],
    param_grid: Dict,
    start_iso: str,
    end_iso: str,
    initial_capital: float,
    output_path: str,
) -> bool:
    """
    產生 Plotly 互動式 HTML 報告。
    包含：Top 10 資金曲線 + 各參數敏感度長條圖（含 dropdown 切換指標）。
    若 plotly 未安裝回傳 False。
    """
    try:
        import plotly.graph_objects as go
        import plotly.io as pio
    except ImportError:
        print("  [跳過 HTML] plotly 未安裝，執行: python -m pip install plotly")
        return False

    if not results:
        return False

    sorted_res = sorted(results, key=lambda x: x["total_pnl_pct"], reverse=True)
    top10_ids = {r["combo_id"] for r in sorted_res[:10]}
    param_keys = list(param_grid.keys())

    html_parts: List[str] = []

    # ─── 圖表 1：Top 10 資金曲線 ──────────────────────────────
    COLORS = [
        "#2979FF", "#E53935", "#43A047", "#FB8C00", "#8E24AA",
        "#00ACC1", "#F4511E", "#6D4C41", "#546E7A", "#039BE5",
    ]

    fig1 = go.Figure()
    # 初始資金基準線
    fig1.add_hline(
        y=initial_capital, line_dash="dash", line_color="gray", opacity=0.6,
        annotation_text=f"初始資金 {initial_capital:,.0f}",
        annotation_position="bottom right",
    )

    # 找 Top 10 的曲線
    top10_curves = sorted(
        [ec for ec in equity_curves if ec["combo_id"] in top10_ids],
        key=lambda ec: next(
            (r["total_pnl_pct"] for r in results if r["combo_id"] == ec["combo_id"]), 0
        ),
        reverse=True,
    )

    for idx, ec in enumerate(top10_curves):
        curve = ec["equity_curve"]
        if not curve:
            continue
        res_row = next((r for r in results if r["combo_id"] == ec["combo_id"]), {})
        pnl_pct = res_row.get("total_pnl_pct", 0)
        params = ec["params"]

        # 組合標籤
        param_str = " | ".join(
            f"{k.split('_')[-1]}={'Y' if v is True else ('N' if v is False else v)}"
            for k, v in params.items()
        )
        trace_name = f"#{idx+1} {_fmt_pct(pnl_pct)}  {param_str}"

        fig1.add_trace(go.Scatter(
            x=[r["date"] for r in curve],
            y=[r["portfolio_value"] for r in curve],
            mode="lines",
            name=trace_name,
            line=dict(color=COLORS[idx % len(COLORS)], width=2),
            hovertemplate=(
                "<b>" + trace_name + "</b><br>"
                "日期: %{x}<br>"
                "資金: %{y:,.0f} NTD<extra></extra>"
            ),
        ))

    fig1.update_layout(
        title=dict(text=f"Top 10 資金曲線  ({start_iso} ~ {end_iso})", font=dict(size=14)),
        xaxis_title="日期",
        yaxis=dict(title="資金 (NTD)", tickformat=",.0f"),
        hovermode="x unified",
        legend=dict(x=1.01, y=1, xanchor="left", font=dict(size=10)),
        height=520,
        margin=dict(r=380, t=60),
    )

    html_parts.append(pio.to_html(fig1, include_plotlyjs="cdn", full_html=False))

    # ─── 圖表 2~N：各參數敏感度長條圖 ─────────────────────────
    for key, values in param_grid.items():
        label = PARAM_LABELS.get(key, key)

        x_labels, avg_pnls, best_pnls, avg_wrs = [], [], [], []
        n_counts = []

        for v in values:
            subset = [r for r in results if r.get(key) == v]
            if not subset:
                continue
            x_labels.append("Y" if v is True else ("N" if v is False else str(v)))
            avg_pnls.append(round(sum(r["total_pnl_pct"] for r in subset) / len(subset), 2))
            best_pnls.append(round(max(r["total_pnl_pct"] for r in subset), 2))
            avg_wrs.append(round(sum(r["win_rate"] for r in subset) / len(subset), 1))
            n_counts.append(len(subset))

        if not x_labels:
            continue

        def _bar_colors(vals):
            return ["#43A047" if v >= 0 else "#E53935" for v in vals]

        fig_p = go.Figure()

        # Trace 0：平均損益率（預設顯示）
        fig_p.add_trace(go.Bar(
            x=x_labels, y=avg_pnls,
            name="平均損益率 (%)",
            marker_color=_bar_colors(avg_pnls),
            text=[f"{v:+.2f}%" for v in avg_pnls],
            textposition="outside",
            visible=True,
            hovertemplate="%{x}<br>平均損益率: %{y:+.2f}%<extra></extra>",
        ))
        # Trace 1：最佳損益率（dropdown 切換）
        fig_p.add_trace(go.Bar(
            x=x_labels, y=best_pnls,
            name="最佳損益率 (%)",
            marker_color="#2979FF",
            text=[f"{v:+.2f}%" for v in best_pnls],
            textposition="outside",
            visible=False,
            hovertemplate="%{x}<br>最佳損益率: %{y:+.2f}%<extra></extra>",
        ))
        # Trace 2：平均勝率（dropdown 切換）
        fig_p.add_trace(go.Bar(
            x=x_labels, y=avg_wrs,
            name="平均勝率 (%)",
            marker_color="#FB8C00",
            text=[f"{v:.1f}%" for v in avg_wrs],
            textposition="outside",
            visible=False,
            hovertemplate="%{x}<br>平均勝率: %{y:.1f}%<extra></extra>",
        ))

        # 各值的組合數標注
        annotations = [
            dict(
                x=x_labels[i], y=0,
                text=f"n={n_counts[i]}",
                showarrow=False,
                yshift=-20,
                font=dict(size=10, color="gray"),
            )
            for i in range(len(x_labels))
        ]

        fig_p.update_layout(
            title=dict(text=f"參數敏感度：{key}（{label}）", font=dict(size=13)),
            xaxis_title=label,
            yaxis_title="平均損益率 (%)",
            annotations=annotations,
            updatemenus=[dict(
                type="dropdown",
                x=1.0, xanchor="right",
                y=1.15, yanchor="top",
                buttons=[
                    dict(label="平均損益率",
                         method="update",
                         args=[{"visible": [True, False, False]},
                               {"yaxis.title.text": "平均損益率 (%)"}]),
                    dict(label="最佳損益率",
                         method="update",
                         args=[{"visible": [False, True, False]},
                               {"yaxis.title.text": "最佳損益率 (%)"}]),
                    dict(label="平均勝率",
                         method="update",
                         args=[{"visible": [False, False, True]},
                               {"yaxis.title.text": "平均勝率 (%)"}]),
                ],
            )],
            height=400,
            margin=dict(t=80),
        )

        html_parts.append(pio.to_html(fig_p, include_plotlyjs=False, full_html=False))

    # ─── 組合成完整 HTML ─────────────────────────────────────
    full_html = (
        "<!DOCTYPE html>\n"
        "<html lang=\"zh-TW\">\n"
        "<head>\n"
        "  <meta charset=\"utf-8\">\n"
        f"  <title>Grid Search 回測報告 {start_iso}~{end_iso}</title>\n"
        "  <style>\n"
        "    body{font-family:'Microsoft JhengHei','PingFang TC',sans-serif;"
        "margin:24px;background:#f0f2f5;color:#333}\n"
        "    h1{color:#1a237e;margin-bottom:4px}\n"
        "    .meta{color:#666;font-size:14px;margin-bottom:20px}\n"
        "    .card{background:#fff;border-radius:10px;padding:16px;"
        "margin-bottom:20px;box-shadow:0 2px 8px rgba(0,0,0,.08)}\n"
        "  </style>\n"
        "</head>\n"
        "<body>\n"
        "  <h1>Grid Search 回測報告</h1>\n"
        f"  <div class=\"meta\">期間：{start_iso} ~ {end_iso}"
        f"  |  組合數：{len(results)}</div>\n"
        + "".join(f'  <div class="card">{p}</div>\n' for p in html_parts)
        + "</body>\n</html>"
    )

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(full_html)

    return True


# ============================================================
# CLI 入口
# ============================================================

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="台股隔日沖回測 Grid Search",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
範例：
  python grid_search.py                              # 自動偵測日期（推薦）
  python grid_search.py --start 2026-01-01 --end 2026-04-03
  python grid_search.py --full-grid                  # 完整網格（480 組）
  python grid_search.py --output my_grid_result      # 指定輸出資料夾名稱
        """,
    )
    p.add_argument("--start",      metavar="YYYY-MM-DD", help="回測起始日")
    p.add_argument("--end",        metavar="YYYY-MM-DD", help="回測結束日")
    p.add_argument("--full-grid",  action="store_true",  help="完整網格（加入 max_day_trade_ratio，480 組）")
    p.add_argument("--output",     metavar="DIR",        help="輸出資料夾名稱（預設：grid_search_YYYYMMDD_HHMMSS）")
    p.add_argument("--verbose",    action="store_true",  help="顯示 DEBUG 日誌")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    cache_dir = config.CACHE_DIR
    param_grid = PARAM_GRID_FULL if args.full_grid else PARAM_GRID_DEFAULT
    total_combos = 1
    for v in param_grid.values():
        total_combos *= len(v)

    print("=" * 60)
    print("  台股隔日沖 Grid Search 回測系統")
    print(f"  參數網格：{'完整' if args.full_grid else '預設'}（{total_combos} 個組合）")
    print("=" * 60)
    print()

    # ── 決定日期區間 ──────────────────────────────────────────
    if args.start or args.end:
        if not (args.start and args.end):
            print("[錯誤] 請同時指定 --start 與 --end，或兩者都不指定（自動推斷）。")
            return 1
        try:
            start_date = datetime.strptime(args.start, "%Y-%m-%d").date()
            end_date   = datetime.strptime(args.end,   "%Y-%m-%d").date()
        except ValueError as e:
            print(f"[錯誤] 日期格式應為 YYYY-MM-DD：{e}")
            return 1

        print("步驟 1/5：驗證日期區間...")
        is_valid, err_msg = validate_date_range(start_date, end_date, cache_dir)
        if not is_valid:
            print(err_msg)
            return 1
        start_iso = start_date.isoformat()
        end_iso   = end_date.isoformat()
        print(f"       驗證通過：{start_iso} ~ {end_iso}")
    else:
        print("步驟 1/5：自動偵測 cache 可用日期區間...")
        start_iso, end_iso = auto_detect_date_range(cache_dir)
        if not start_iso:
            print(
                "[錯誤] cache 資料不足，無法自動推斷回測日期。\n"
                "       請先執行: python backtest_main.py 下載資料。"
            )
            return 1
        print(f"       自動推斷回測期間：{start_iso} ~ {end_iso}")

    print()

    # ── 載入設定（固定參數基準值）────────────────────────────
    base_cfg = load_settings()

    # ── 建立輸出目錄 ──────────────────────────────────────────
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = args.output if args.output else f"grid_search_{timestamp}"
    results_dir = os.path.join(out_dir, "results")
    os.makedirs(results_dir, exist_ok=True)

    txt_path  = os.path.join(out_dir, "summary.txt")
    json_path = os.path.join(out_dir, "equity_curves.json")
    html_path = os.path.join(out_dir, "report.html")
    csv_path  = os.path.join(results_dir, f"results_{timestamp}.csv")

    # ── 載入名稱與股本 ────────────────────────────────────────
    print("步驟 2/5：從 cache 載入股票資料...")
    name_map = load_name_map_from_cache(cache_dir)
    if not name_map:
        print(
            "[錯誤] 找不到 cache/stock_name_map.json。\n"
            "       請先執行: python backtest_main.py"
        )
        return 1
    capital_map = load_capital_map_from_cache(cache_dir)
    print(f"       股票清單：{len(name_map)} 支  |  股本資料：{len(capital_map)} 家")
    print()

    # ── 載入歷史資料（一次，重複使用）────────────────────────
    print("步驟 3/5：從 cache 載入歷史資料（含 MA buffer）...")
    start_d = datetime.strptime(start_iso, "%Y-%m-%d").date()
    end_d   = datetime.strptime(end_iso,   "%Y-%m-%d").date()

    all_histories = load_all_histories_from_cache(
        cache_dir, list(name_map.keys()), start_d, end_d, ma_buffer=2
    )
    if not all_histories:
        print(
            "[錯誤] 無法從 cache 載入任何歷史資料。\n"
            "       請先執行: python backtest_main.py"
        )
        return 1
    print()

    # ── 執行 Grid Search ─────────────────────────────────────
    print("步驟 4/5：執行 Grid Search 回測...")
    result_rows, equity_curves = run_grid_search(
        base_cfg=base_cfg,
        param_grid=param_grid,
        all_histories=all_histories,
        name_map=name_map,
        capital_map=capital_map,
        start_iso=start_iso,
        end_iso=end_iso,
    )

    if not result_rows:
        print("[錯誤] 所有組合均執行失敗，請確認 cache 資料完整性。")
        return 1

    # ── 產生報告 ──────────────────────────────────────────────
    print("步驟 5/5：產生報告...")
    initial_capital = float(base_cfg.get("bt_initial_capital", 500_000))

    generate_summary_txt(result_rows, param_grid, start_iso, end_iso, txt_path)
    print(f"  summary.txt     → {txt_path}")

    generate_results_csv(result_rows, csv_path)
    print(f"  results.csv     → {csv_path}")

    generate_equity_json(equity_curves, json_path)
    print(f"  equity_curves   → {json_path}")

    ok = generate_plotly_html(
        result_rows, equity_curves, param_grid,
        start_iso, end_iso, initial_capital, html_path,
    )
    if ok:
        print(f"  report.html     → {html_path}")

    print()
    print("=" * 60)
    print(f"  完成！輸出目錄：{os.path.abspath(out_dir)}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
