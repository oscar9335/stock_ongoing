# backtest_engine.py
# 隔日沖回測核心引擎
#
# 邏輯：
#   1. 重建歷史每日快照（從已快取的月份資料）
#   2. 每天跑選股篩選器，取前 N 名
#   3. 模擬進場（買入收盤價）→ 隔日出場（賣出開盤價）
#   4. 計算損益、更新資金

import logging
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import data_fetcher as df
import screener as sc

logger = logging.getLogger(__name__)


# ============================================================
# 資料結構
# ============================================================

@dataclass
class TradeRecord:
    """單筆交易紀錄。"""
    code: str
    name: str
    buy_date: str          # 訊號日 ISO (進場)
    sell_date: str         # 出場日 ISO
    buy_price: float       # 買入價（訊號日收盤）
    sell_price: float      # 賣出價（次交易日開盤）
    zhang: int             # 買入張數
    shares: int            # 買入股數 = zhang * 1000
    cost: float            # 實際花費 NTD（含手續費）
    revenue: float         # 實際收入 NTD（扣手續費+稅）
    pnl: float             # 損益 NTD
    pnl_pct: float         # 損益率 %（相對 cost）
    limit_up_status: str   # LOCKED / NEAR_LIMIT / NORMAL
    score: float           # 選股評分


@dataclass
class DailyResult:
    """單日回測結果。"""
    date: str
    trades: List[TradeRecord] = field(default_factory=list)
    daily_pnl: float = 0.0
    portfolio_value: float = 0.0   # 當日結算後資金
    no_signal: bool = False        # True = 篩選器無候選標的
    skipped: bool = False          # True = 資金不足跳過


@dataclass
class BacktestSummary:
    """回測結果摘要。"""
    start_date: str
    end_date: str
    initial_capital: float
    final_capital: float
    daily_results: List[DailyResult] = field(default_factory=list)
    cfg: Dict = field(default_factory=dict)        # 選股參數快照
    trading_cfg: Dict = field(default_factory=dict)  # 交易策略參數快照

    @property
    def total_pnl(self) -> float:
        return self.final_capital - self.initial_capital

    @property
    def total_pnl_pct(self) -> float:
        if self.initial_capital == 0:
            return 0.0
        return self.total_pnl / self.initial_capital * 100

    @property
    def all_trades(self) -> List[TradeRecord]:
        trades = []
        for dr in self.daily_results:
            trades.extend(dr.trades)
        return trades

    @property
    def win_trades(self) -> List[TradeRecord]:
        return [t for t in self.all_trades if t.pnl > 0]

    @property
    def lose_trades(self) -> List[TradeRecord]:
        return [t for t in self.all_trades if t.pnl <= 0]

    @property
    def win_rate(self) -> Optional[float]:
        total = len(self.all_trades)
        if total == 0:
            return None
        return len(self.win_trades) / total * 100

    @property
    def trade_days(self) -> int:
        return sum(1 for dr in self.daily_results if dr.trades)

    @property
    def max_daily_gain(self) -> Optional[float]:
        pnls = [dr.daily_pnl for dr in self.daily_results if dr.trades]
        return max(pnls) if pnls else None

    @property
    def max_daily_gain_date(self) -> Optional[str]:
        best = None
        best_date = None
        for dr in self.daily_results:
            if dr.trades and (best is None or dr.daily_pnl > best):
                best = dr.daily_pnl
                best_date = dr.date
        return best_date

    @property
    def max_daily_loss(self) -> Optional[float]:
        pnls = [dr.daily_pnl for dr in self.daily_results if dr.trades]
        return min(pnls) if pnls else None

    @property
    def max_daily_loss_date(self) -> Optional[str]:
        worst = None
        worst_date = None
        for dr in self.daily_results:
            if dr.trades and (worst is None or dr.daily_pnl < worst):
                worst = dr.daily_pnl
                worst_date = dr.date
        return worst_date

    @property
    def max_drawdown(self) -> float:
        """最大回撤（NTD，負值）。"""
        peak = self.initial_capital
        max_dd = 0.0
        for dr in self.daily_results:
            if dr.portfolio_value > peak:
                peak = dr.portfolio_value
            dd = dr.portfolio_value - peak
            if dd < max_dd:
                max_dd = dd
        return max_dd

    @property
    def avg_trade_pnl(self) -> Optional[float]:
        trades = self.all_trades
        if not trades:
            return None
        return sum(t.pnl for t in trades) / len(trades)


# ============================================================
# 輔助函式
# ============================================================

def _reconstruct_daily_snapshot(
    all_histories: Dict[str, List[Dict]],
    date_iso: str,
    name_map: Dict[str, str],
) -> List[Dict]:
    """
    從批次歷史資料重建某一天的全市場快照。

    每筆包含 code, name, exchange, open, high, low, close,
             change, volume, gain_pct, prev_close
    """
    snapshot = []
    for code, history in all_histories.items():
        # 在已排序的 history 中找到 date_iso 當天的資料
        day_data = None
        for row in history:
            if row.get("date") == date_iso:
                day_data = row
                break

        if day_data is None:
            continue

        close  = day_data.get("close")
        change = day_data.get("change")
        volume = day_data.get("volume")

        if close is None or change is None:
            continue
        if close <= 0:
            continue

        prev_close = round(close - change, 2)
        if prev_close == 0:
            continue

        gain_pct = round(change / prev_close * 100, 2)

        snapshot.append({
            "code":       code,
            "name":       name_map.get(code, code),
            "exchange":   "TWSE",
            "open":       day_data.get("open"),
            "high":       day_data.get("high"),
            "low":        day_data.get("low"),
            "close":      close,
            "change":     change,
            "volume":     volume,
            "prev_close": prev_close,
            "gain_pct":   gain_pct,
        })

    return snapshot


def _simulate_trade(
    result: Dict,
    alloc: float,
    buy_date: str,
    sell_date: str,
    sell_open: float,
    trading_cfg: Dict,
) -> TradeRecord:
    """
    模擬單筆交易，回傳 TradeRecord。

    Args:
        result:       screener.screen_stocks() 回傳的單支股票結果 dict
        alloc:        分配到此支股票的資金上限 (NTD)
        buy_date:     訊號日 ISO
        sell_date:    出場日 ISO
        sell_open:    出場日開盤價
        trading_cfg:  交易策略參數 dict
    """
    buy_price = result["收盤價"]
    commission = trading_cfg["bt_commission_rate"] * trading_cfg["bt_commission_discount"]
    tax = trading_cfg["bt_tax_rate"]

    # 計算可買張數（整數，向下取整）
    cost_per_zhang = buy_price * 1000 * (1 + commission)
    zhang = int(math.floor(alloc / cost_per_zhang))
    if zhang <= 0:
        zhang = 0

    shares = zhang * 1000
    cost    = buy_price * shares * (1 + commission)
    revenue = sell_open * shares * (1 - commission - tax)
    pnl     = revenue - cost
    pnl_pct = (pnl / cost * 100) if cost > 0 else 0.0

    return TradeRecord(
        code=result["代號"],
        name=result["名稱"],
        buy_date=buy_date,
        sell_date=sell_date,
        buy_price=buy_price,
        sell_price=sell_open,
        zhang=zhang,
        shares=shares,
        cost=cost,
        revenue=revenue,
        pnl=pnl,
        pnl_pct=round(pnl_pct, 2),
        limit_up_status=result.get("漲停狀態", ""),
        score=result.get("評分", 0.0),
    )


# ============================================================
# 主回測函式
# ============================================================

def run_backtest(
    start_date: str,
    end_date: str,
    cfg: Dict,
    trading_cfg: Dict,
    all_histories: Dict[str, List[Dict]],
    name_map: Dict[str, str],
    capital_map: Dict[str, int],
) -> BacktestSummary:
    """
    執行回測。

    Args:
        start_date:    回測起始日 ISO (YYYY-MM-DD)
        end_date:      回測結束日 ISO (YYYY-MM-DD)
        cfg:           選股參數 dict（含 min_gain_pct, volume_multiplier 等）
        trading_cfg:   交易策略參數 dict（含 bt_initial_capital, bt_top_n 等）
        all_histories: {code: history_list}，由 fetch_bulk_histories() 取得
        name_map:      {code: name}，由 fetch_stock_name_map() 取得
        capital_map:   {code: capital_ntd}，由 fetch_company_capital() 取得

    Returns:
        BacktestSummary
    """
    initial_capital = float(trading_cfg["bt_initial_capital"])
    top_n = int(trading_cfg["bt_top_n"])
    min_trade_capital = 5_000  # 最低可操作資金（低於此值視為資金耗盡）

    # 從 all_histories 中推導有效交易日清單（所有股票出現過的日期聯集，篩選範圍內）
    all_dates = set()
    for history in all_histories.values():
        for row in history:
            d = row.get("date", "")
            if start_date <= d <= end_date:
                all_dates.add(d)
    trading_days = sorted(all_dates)

    if not trading_days:
        logger.warning("指定區間內無交易日資料（%s ~ %s）", start_date, end_date)
        return BacktestSummary(
            start_date=start_date,
            end_date=end_date,
            initial_capital=initial_capital,
            final_capital=initial_capital,
            cfg=cfg,
            trading_cfg=trading_cfg,
        )

    logger.info("回測交易日：%d 天（%s ~ %s）", len(trading_days), trading_days[0], trading_days[-1])

    capital = initial_capital
    daily_results: List[DailyResult] = []

    for day_idx, today in enumerate(trading_days):
        # 找下一個交易日
        next_day = trading_days[day_idx + 1] if day_idx + 1 < len(trading_days) else None

        # 資金不足時提早結束
        if capital < min_trade_capital:
            logger.info("資金不足 %.0f NTD，回測提早結束（%s）", capital, today)
            daily_results.append(DailyResult(
                date=today,
                portfolio_value=capital,
                skipped=True,
            ))
            break

        # 如果是最後一個交易日，無法進場（沒有隔日可以出場）
        if next_day is None:
            daily_results.append(DailyResult(
                date=today,
                portfolio_value=capital,
                no_signal=True,
            ))
            continue

        # 取三大法人 & 當沖資料（含日期快取）
        today_yyyymmdd = today.replace("-", "")
        institution_map = df.fetch_institution_flows(today_yyyymmdd)
        day_trade_map   = df.fetch_day_trade_data(today_yyyymmdd)

        # 重建今日全市場快照
        daily_snapshot = _reconstruct_daily_snapshot(all_histories, today, name_map)

        if not daily_snapshot:
            logger.debug("%s 無快照資料，跳過", today)
            daily_results.append(DailyResult(
                date=today,
                portfolio_value=capital,
                no_signal=True,
            ))
            continue

        # 執行選股篩選
        history_fn = lambda code, exchange: all_histories.get(code, [])

        screened = sc.screen_stocks(
            all_stocks=daily_snapshot,
            history_fn=history_fn,
            capital_map=capital_map,
            institution_map=institution_map,
            day_trade_map=day_trade_map,
            cfg=cfg,
            trade_date_iso=today,
        )

        if not screened:
            daily_results.append(DailyResult(
                date=today,
                portfolio_value=capital,
                no_signal=True,
            ))
            continue

        # 取前 N 支（依評分降冪已排序）
        candidates = screened[:top_n]
        actual_n = len(candidates)

        # 均等分配資金
        alloc_per_stock = capital / actual_n

        trades: List[TradeRecord] = []
        for result in candidates:
            code = result["代號"]

            # 取次交易日開盤價
            next_history = all_histories.get(code, [])
            sell_open = None
            for row in next_history:
                if row.get("date") == next_day:
                    sell_open = row.get("open")
                    break

            if sell_open is None or sell_open <= 0:
                logger.debug("%s %s 無次日開盤價，跳過此交易", today, code)
                continue

            trade = _simulate_trade(
                result=result,
                alloc=alloc_per_stock,
                buy_date=today,
                sell_date=next_day,
                sell_open=sell_open,
                trading_cfg=trading_cfg,
            )

            if trade.zhang > 0:
                trades.append(trade)

        # 計算當日損益（僅加計實際有成交的部分）
        daily_pnl = sum(t.pnl for t in trades)
        capital += daily_pnl

        daily_results.append(DailyResult(
            date=today,
            trades=trades,
            daily_pnl=daily_pnl,
            portfolio_value=capital,
            no_signal=(len(trades) == 0 and not screened),
        ))

    summary = BacktestSummary(
        start_date=start_date,
        end_date=end_date,
        initial_capital=initial_capital,
        final_capital=capital,
        daily_results=daily_results,
        cfg=cfg,
        trading_cfg=trading_cfg,
    )

    logger.info(
        "回測完成：總損益 %.0f NTD（%.2f%%），交易 %d 筆，勝率 %.1f%%",
        summary.total_pnl,
        summary.total_pnl_pct,
        len(summary.all_trades),
        summary.win_rate or 0.0,
    )

    return summary
