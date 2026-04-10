# backtest_report.py
# 隔日沖回測報告產生器
# 輸出：文字摘要報告 + matplotlib 雙子圖（資金折線圖 + 每日損益長條圖）

import io
import os
from datetime import datetime
from typing import List, Optional

from backtest_engine import BacktestSummary, DailyResult, TradeRecord


# ============================================================
# 文字報告
# ============================================================

def _fmt_ntd(v: Optional[float]) -> str:
    """格式化 NTD 金額，加千分位逗號與正負號。"""
    if v is None:
        return "N/A"
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:,.0f}"


def _fmt_pct(v: Optional[float]) -> str:
    if v is None:
        return "N/A"
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.2f}%"


def generate_text_report(summary: BacktestSummary) -> str:
    """產生完整文字報告，回傳字串。"""
    buf = io.StringIO()
    w = buf.write

    total_days = len(summary.daily_results)
    trade_days = summary.trade_days
    all_trades = summary.all_trades
    wins = summary.win_trades
    loses = summary.lose_trades

    # ── 標題 ──────────────────────────────────────────────────
    w("=" * 60 + "\n")
    w("  台股隔日沖回測報告\n")
    w(f"  期間：{summary.start_date} ~ {summary.end_date}  ({total_days} 交易日)\n")
    w(f"  初始資金：{summary.initial_capital:,.0f} NTD\n")
    w("=" * 60 + "\n\n")

    # ── 選股參數 ──────────────────────────────────────────────
    cfg = summary.cfg
    w("【選股參數】\n")
    w(f"  日漲幅門檻：≥ {cfg.get('min_gain_pct', 'N/A')}%\n")
    w(f"  股本範圍：{cfg.get('capital_min_ntd', 0)/1e8:.0f}億 ~ {cfg.get('capital_max_ntd', 0)/1e8:.0f}億 NTD\n")
    w(f"  量比門檻：≥ {cfg.get('volume_multiplier', 'N/A')}x\n")
    w(f"  均線週期：MA{cfg.get('ma_short')}/MA{cfg.get('ma_mid')}/MA{cfg.get('ma_long')}\n")
    w(f"  布林通道：{cfg.get('boll_window')} 日 ± {cfg.get('boll_std_mult')} 標準差\n")
    w(f"  當沖率上限：< {cfg.get('max_day_trade_ratio', 0)*100:.0f}%\n")
    w("\n")

    # ── 交易策略參數 ──────────────────────────────────────────
    tcfg = summary.trading_cfg
    w("【交易策略參數】\n")
    w(f"  每日最多持股：{tcfg.get('bt_top_n', 'N/A')} 支\n")
    w(f"  手續費：{tcfg.get('bt_commission_rate', 0)*100:.4f}% × 折扣 {tcfg.get('bt_commission_discount', 1.0):.1f}\n")
    w(f"  證交稅：{tcfg.get('bt_tax_rate', 0)*100:.2f}%（賣出）\n")
    w(f"  停損設定：平盤下 {abs(tcfg.get('bt_stop_loss_pct', -0.01))*100:.0f}%\n")
    w("\n")

    # ── 績效摘要 ──────────────────────────────────────────────
    w("【績效摘要】\n")
    w(f"  最終資金：   {summary.final_capital:>12,.0f} NTD\n")
    w(f"  總損益：     {_fmt_ntd(summary.total_pnl):>12}  ({_fmt_pct(summary.total_pnl_pct)})\n")
    w(f"  有交易日數： {trade_days:>12} / {total_days} 天\n")
    w(f"  交易次數：   {len(all_trades):>12} 筆\n")

    if summary.win_rate is not None:
        w(f"  勝率：       {summary.win_rate:>11.1f}%  ({len(wins)} 勝 / {len(loses)} 負)\n")
    else:
        w("  勝率：          無交易\n")

    if summary.avg_trade_pnl is not None:
        w(f"  平均每筆損益：{_fmt_ntd(summary.avg_trade_pnl):>11}\n")

    if summary.max_daily_gain is not None:
        w(f"  最大單日獲利：{_fmt_ntd(summary.max_daily_gain):>11}  ({summary.max_daily_gain_date})\n")

    if summary.max_daily_loss is not None:
        w(f"  最大單日虧損：{_fmt_ntd(summary.max_daily_loss):>11}  ({summary.max_daily_loss_date})\n")

    dd = summary.max_drawdown
    dd_pct = dd / summary.initial_capital * 100 if summary.initial_capital else 0.0
    w(f"  最大回撤：   {_fmt_ntd(dd):>12}  ({dd_pct:.1f}%)\n")
    w("\n")

    # ── 每日交易明細 ──────────────────────────────────────────
    w("【每日交易明細】\n")
    w("-" * 60 + "\n")

    for dr in summary.daily_results:
        if dr.skipped:
            w(f"{dr.date}  ⚠ 資金不足，回測提早結束（餘額 {dr.portfolio_value:,.0f} NTD）\n")
            continue

        if dr.no_signal and not dr.trades:
            w(f"{dr.date}  — 無交易訊號\n")
            continue

        if not dr.trades:
            w(f"{dr.date}  — 無成交（買入張數為 0 或無次日開盤價）\n")
            continue

        w(f"{dr.date}\n")
        for t in dr.trades:
            pnl_str = _fmt_ntd(t.pnl)
            pct_str = _fmt_pct(t.pnl_pct)
            status_icon = "🔒" if t.limit_up_status == "LOCKED" else ("⬆" if t.limit_up_status == "NEAR_LIMIT" else " ")
            gain_str = _fmt_pct(t.buy_gain_pct) if t.buy_gain_pct else ""
            w(f"  {status_icon} {t.code} {t.name:<6} "
              f"訊號漲幅{gain_str}  "
              f"{t.zhang}張@{t.buy_price:.1f} → 隔日開盤 {t.sell_price:.1f}  "
              f"{pnl_str} NTD ({pct_str})\n")

        w(f"  {'─'*50}\n")
        w(f"  當日損益：{_fmt_ntd(dr.daily_pnl)} NTD   資金餘額：{dr.portfolio_value:,.0f} NTD\n\n")

    w("=" * 60 + "\n")
    w(f"  報告產生時間：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
    w("=" * 60 + "\n")

    return buf.getvalue()


# ============================================================
# 圖表
# ============================================================

def generate_chart(summary: BacktestSummary, output_path: str) -> bool:
    """
    產生雙子圖 PNG。

    上圖：資金變化折線圖（相對初始資金的比較線）
    下圖：每日損益長條圖（正值=綠，負值=紅）

    Returns:
        True = 成功；False = matplotlib 不可用
    """
    try:
        import matplotlib
        matplotlib.use("Agg")  # 無 GUI 後端，避免 Windows 視窗問題
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
        from datetime import datetime as _dt
    except ImportError:
        return False

    # 設定中文字型，避免亂碼
    from matplotlib import font_manager
    _zh_fonts = ["Microsoft JhengHei", "Microsoft YaHei", "SimHei", "PingFang TC", "Noto Sans CJK TC"]
    _available = {f.name for f in font_manager.fontManager.ttflist}
    _chosen = next((f for f in _zh_fonts if f in _available), None)
    if _chosen:
        matplotlib.rcParams["font.family"] = _chosen
    matplotlib.rcParams["axes.unicode_minus"] = False  # 修正負號顯示

    # 整理資料
    dates = []
    portfolio_values = []
    daily_pnls = []

    for dr in summary.daily_results:
        if dr.skipped:
            break
        try:
            d = _dt.strptime(dr.date, "%Y-%m-%d")
        except ValueError:
            continue
        dates.append(d)
        portfolio_values.append(dr.portfolio_value)
        daily_pnls.append(dr.daily_pnl)

    if not dates:
        return False

    # ── 繪圖 ──────────────────────────────────────────────────
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 8),
                                    gridspec_kw={"height_ratios": [2, 1]})
    fig.suptitle(
        f"台股隔日沖回測報告  {summary.start_date} ~ {summary.end_date}\n"
        f"初始資金 {summary.initial_capital:,.0f} NTD  →  最終 {summary.final_capital:,.0f} NTD"
        f"  (總損益 {_fmt_ntd(summary.total_pnl)} NTD / {_fmt_pct(summary.total_pnl_pct)})",
        fontsize=11,
    )

    # 上圖：資金折線
    ax1.plot(dates, portfolio_values, color="#2979FF", linewidth=1.8, label="資金")
    ax1.axhline(y=summary.initial_capital, color="gray", linestyle="--",
                linewidth=1.0, alpha=0.6, label=f"初始資金 {summary.initial_capital:,.0f}")

    # 最高/最低點標記
    if portfolio_values:
        max_val = max(portfolio_values)
        min_val = min(portfolio_values)
        max_idx = portfolio_values.index(max_val)
        min_idx = portfolio_values.index(min_val)
        ax1.annotate(f"{max_val:,.0f}",
                     xy=(dates[max_idx], max_val),
                     xytext=(0, 8), textcoords="offset points",
                     ha="center", fontsize=8, color="#00C853")
        ax1.annotate(f"{min_val:,.0f}",
                     xy=(dates[min_idx], min_val),
                     xytext=(0, -14), textcoords="offset points",
                     ha="center", fontsize=8, color="#D50000")

    ax1.set_ylabel("資金 (NTD)", fontsize=10)
    ax1.legend(fontsize=9)
    ax1.yaxis.set_major_formatter(
        matplotlib.ticker.FuncFormatter(lambda x, _: f"{x:,.0f}")
    )
    ax1.grid(axis="y", linestyle=":", alpha=0.5)
    ax1.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
    ax1.xaxis.set_major_locator(mdates.WeekdayLocator(interval=1))
    plt.setp(ax1.xaxis.get_majorticklabels(), rotation=45, ha="right", fontsize=8)

    # 下圖：每日損益長條
    colors = ["#00C853" if v >= 0 else "#D50000" for v in daily_pnls]
    ax2.bar(dates, daily_pnls, color=colors, width=0.8, alpha=0.85)
    ax2.axhline(y=0, color="black", linewidth=0.8)
    ax2.set_ylabel("每日損益 (NTD)", fontsize=10)
    ax2.yaxis.set_major_formatter(
        matplotlib.ticker.FuncFormatter(lambda x, _: f"{x:+,.0f}")
    )
    ax2.grid(axis="y", linestyle=":", alpha=0.5)
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
    ax2.xaxis.set_major_locator(mdates.WeekdayLocator(interval=1))
    plt.setp(ax2.xaxis.get_majorticklabels(), rotation=45, ha="right", fontsize=8)

    # 圖表統計標注（右下角）
    stats_text = (
        f"交易次數: {len(summary.all_trades)}\n"
        f"勝率: {summary.win_rate:.1f}%" if summary.win_rate is not None
        else "無交易"
    )
    ax2.text(0.99, 0.02, stats_text,
             transform=ax2.transAxes,
             ha="right", va="bottom", fontsize=8,
             bbox=dict(boxstyle="round,pad=0.3", facecolor="white", alpha=0.7))

    plt.tight_layout()

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    try:
        plt.savefig(output_path, dpi=150, bbox_inches="tight")
        plt.close(fig)
        return True
    except Exception as e:
        plt.close(fig)
        return False


# ============================================================
# 儲存報告
# ============================================================

def save_report(
    summary: BacktestSummary,
    output_dir: str = "reports",
) -> dict:
    """
    儲存文字報告與圖表。

    Returns:
        {"txt": path_or_None, "png": path_or_None}
    """
    os.makedirs(output_dir, exist_ok=True)

    start_slug = summary.start_date.replace("-", "")
    end_slug   = summary.end_date.replace("-", "")
    base_name  = f"backtest_{start_slug}_{end_slug}"

    txt_path = os.path.join(output_dir, base_name + ".txt")
    png_path = os.path.join(output_dir, base_name + ".png")

    # 文字報告
    txt_ok = False
    try:
        report_text = generate_text_report(summary)
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(report_text)
        txt_ok = True
    except Exception as e:
        print(f"[報告] 文字報告儲存失敗：{e}")
        txt_path = None

    # 圖表
    png_ok = generate_chart(summary, png_path)
    if not png_ok:
        png_path = None

    return {"txt": txt_path if txt_ok else None, "png": png_path}
