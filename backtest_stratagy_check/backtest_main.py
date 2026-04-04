# backtest_main.py
# 台股隔日沖回測系統 — CLI 入口點
#
# 用法：
#   python backtest_main.py                                     # 近 3 個月，50 萬，選前 3 名
#   python backtest_main.py --start 2026-02-01 --end 2026-04-03
#   python backtest_main.py --capital 1000000 --top-n 5
#   python backtest_main.py --min-gain 5 --vol-ratio 1.5       # 放寬選股門檻
#   python backtest_main.py --output reports/my_test

import argparse
import io
import json
import logging
import os
import sys
from datetime import date, datetime, timedelta
from typing import Dict

# ── Windows UTF-8 console fix ──────────────────────────────
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import config
import data_fetcher as df
import backtest_engine as engine
import backtest_report as report

# ============================================================
# 設定載入
# ============================================================

SETTINGS_FILE = "settings.json"

_DEFAULT_SETTINGS = {
    "min_gain_pct":         config.MIN_GAIN_PCT,
    "pre_filter_gain_pct":  config.PRE_FILTER_GAIN_PCT,
    "near_limit_up_pct":    config.NEAR_LIMIT_UP_PCT,
    "capital_min_ntd":      config.CAPITAL_MIN_NTD,
    "capital_max_ntd":      config.CAPITAL_MAX_NTD,
    "volume_multiplier":    config.VOLUME_MULTIPLIER,
    "ma_short":             config.MA_SHORT,
    "ma_mid":               config.MA_MID,
    "ma_long":              config.MA_LONG,
    "boll_window":          config.BOLL_WINDOW,
    "boll_std_mult":        config.BOLL_STD_MULT,
    "max_day_trade_ratio":  config.MAX_DAY_TRADE_RATIO,
    "include_tpex":         config.INCLUDE_TPEX,
    "finmind_token":        "",
    "bt_initial_capital":   config.BT_INITIAL_CAPITAL,
    "bt_top_n":             config.BT_TOP_N,
    "bt_commission_rate":   config.BT_COMMISSION_RATE,
    "bt_commission_discount": config.BT_COMMISSION_DISCOUNT,
    "bt_tax_rate":          config.BT_TAX_RATE,
    "bt_stop_loss_pct":     config.BT_STOP_LOSS_PCT,
    "bt_months":            config.BT_MONTHS,
    "min_history_days":     config.MIN_HISTORY_DAYS,
}


def load_settings() -> Dict:
    """讀取 settings.json，不存在時自動建立預設值。"""
    if not os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(_DEFAULT_SETTINGS, f, indent=2, ensure_ascii=False)
        print(f"[設定] 已建立 {SETTINGS_FILE}，可依需求修改後重新執行。")
        return dict(_DEFAULT_SETTINGS)

    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            user = json.load(f)
        merged = dict(_DEFAULT_SETTINGS)
        merged.update(user)

        # 若 settings.json 缺少新增的 bt_* 鍵，寫回補齊
        missing = [k for k in _DEFAULT_SETTINGS if k not in user]
        if missing:
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(merged, f, indent=2, ensure_ascii=False)

        return merged
    except Exception as e:
        print(f"[設定] 讀取 {SETTINGS_FILE} 失敗：{e}，使用預設值")
        return dict(_DEFAULT_SETTINGS)


# ============================================================
# CLI 參數
# ============================================================

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="台股隔日沖回測系統",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
範例：
  python backtest_main.py
  python backtest_main.py --start 2026-01-01 --end 2026-04-03
  python backtest_main.py --capital 1000000 --top-n 5
  python backtest_main.py --min-gain 5 --vol-ratio 1.5 --output reports/relaxed
        """,
    )

    # 回測期間
    p.add_argument("--start",    metavar="YYYY-MM-DD", help="回測起始日（預設：近 bt_months 個月）")
    p.add_argument("--end",      metavar="YYYY-MM-DD", help="回測結束日（預設：昨日）")

    # 資金設定
    p.add_argument("--capital",  type=int, metavar="NTD",  help="初始資金（預設：500000）")
    p.add_argument("--top-n",    type=int, metavar="N",    help="每日最多持股支數（預設：3）")

    # 輸出
    p.add_argument("--output",   metavar="DIR",            help="報告輸出目錄（預設：reports）")
    p.add_argument("--skip-fetch", action="store_true",    help="跳過歷史資料重新下載，僅用快取")
    p.add_argument("--verbose",  action="store_true",      help="顯示 DEBUG 日誌")

    # 選股參數（覆蓋 settings.json）
    p.add_argument("--min-gain",   type=float, metavar="PCT", help="最低日漲幅 %%（預設：7.0）")
    p.add_argument("--pre-filter", type=float, metavar="PCT", help="預篩選漲幅 %%（預設：5.0）")
    p.add_argument("--cap-min",    type=int,   metavar="NTD", help="股本下限（預設：1000000000）")
    p.add_argument("--cap-max",    type=int,   metavar="NTD", help="股本上限（預設：5000000000）")
    p.add_argument("--vol-ratio",  type=float, metavar="X",   help="量比門檻（預設：2.0）")
    p.add_argument("--max-dt-ratio", type=float, metavar="R", help="最大當沖率（預設：0.6）")
    p.add_argument("--ma-short",   type=int,                  help="MA 短週期（預設：5）")
    p.add_argument("--ma-mid",     type=int,                  help="MA 中週期（預設：10）")
    p.add_argument("--ma-long",    type=int,                  help="MA 長週期（預設：20）")
    p.add_argument("--boll-window", type=int,                 help="布林通道窗格（預設：20）")
    p.add_argument("--include-tpex", action="store_true",     help="同時掃描 TPEX 上櫃股票")

    # 交易策略參數覆蓋
    p.add_argument("--commission-rate",     type=float, help="手續費率（預設：0.001425）")
    p.add_argument("--commission-discount", type=float, help="手續費折扣（預設：1.0）")
    p.add_argument("--tax-rate",            type=float, help="證交稅率（預設：0.003）")
    p.add_argument("--stop-loss",           type=float, help="停損觸發跌幅，負值（預設：-0.01）")

    return p.parse_args()


def apply_cli_overrides(cfg: Dict, args: argparse.Namespace) -> Dict:
    """將 CLI 參數合併進 cfg，CLI 優先級最高。"""
    overrides = {
        "min_gain_pct":         args.min_gain,
        "pre_filter_gain_pct":  args.pre_filter,
        "capital_min_ntd":      args.cap_min,
        "capital_max_ntd":      args.cap_max,
        "volume_multiplier":    args.vol_ratio,
        "max_day_trade_ratio":  args.max_dt_ratio,
        "ma_short":             args.ma_short,
        "ma_mid":               args.ma_mid,
        "ma_long":              args.ma_long,
        "boll_window":          args.boll_window,
        "include_tpex":         True if args.include_tpex else None,
        "bt_initial_capital":   args.capital,
        "bt_top_n":             args.top_n,
        "bt_commission_rate":   args.commission_rate,
        "bt_commission_discount": args.commission_discount,
        "bt_tax_rate":          args.tax_rate,
        "bt_stop_loss_pct":     args.stop_loss,
    }
    for key, val in overrides.items():
        if val is not None:
            cfg[key] = val
    return cfg


# ============================================================
# 主程式
# ============================================================

def setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def main() -> int:
    args = parse_args()
    setup_logging(args.verbose)

    # ── 載入設定 ──────────────────────────────────────────────
    cfg = load_settings()
    cfg = apply_cli_overrides(cfg, args)

    # ── 決定回測期間 ──────────────────────────────────────────
    today = date.today()
    yesterday = today - timedelta(days=1)

    bt_months = int(cfg.get("bt_months", config.BT_MONTHS))

    if args.end:
        try:
            end_date = datetime.strptime(args.end, "%Y-%m-%d").date()
        except ValueError:
            print(f"[錯誤] 結束日期格式應為 YYYY-MM-DD，收到：{args.end}")
            return 1
    else:
        end_date = yesterday

    if args.start:
        try:
            start_date = datetime.strptime(args.start, "%Y-%m-%d").date()
        except ValueError:
            print(f"[錯誤] 起始日期格式應為 YYYY-MM-DD，收到：{args.start}")
            return 1
    else:
        # 預設：向前推 bt_months 個月
        start_date = date(end_date.year, end_date.month, 1)
        for _ in range(bt_months - 1):
            start_date = start_date - timedelta(days=1)
            start_date = date(start_date.year, start_date.month, 1)

    start_iso = start_date.isoformat()
    end_iso   = end_date.isoformat()

    print(f"回測期間：{start_iso} ~ {end_iso}")
    print(f"初始資金：{cfg['bt_initial_capital']:,} NTD  |  每日最多 {cfg['bt_top_n']} 支")
    print(f"選股門檻：漲幅≥{cfg['min_gain_pct']}%  量比≥{cfg['volume_multiplier']}x  "
          f"股本 {cfg['capital_min_ntd']/1e8:.0f}~{cfg['capital_max_ntd']/1e8:.0f}億")
    print()

    # ── 計算需要幾個月的歷史資料 ──────────────────────────────
    # 回測期間長度（月） + MA lookback (1個月) + 2個月緩衝，從 end_date 往前算
    delta_months = (end_date.year - start_date.year) * 12 + (end_date.month - start_date.month)
    history_months = delta_months + 3  # 3 = MA lookback 1個月 + 緩衝 2個月
    print(f"歷史資料：需取 {history_months} 個月（從 {end_date.year}/{end_date.month:02d} 往前推）")
    print()

    # ── 取得股票代號清單與名稱 ────────────────────────────────
    print("步驟 1/4：取得股票代號清單...")
    name_map = df.fetch_stock_name_map()
    if not name_map:
        print("[錯誤] 無法取得股票清單，請確認網路連線。")
        return 1
    print(f"         共 {len(name_map)} 支 TWSE 股票\n")

    # ── 批次下載歷史資料 ──────────────────────────────────────
    print("步驟 2/4：批次下載歷史資料（已快取者直接讀取）...")
    print("         首次執行約需 15~20 分鐘，後續執行使用快取（< 1 分鐘）\n")

    code_list = list(name_map.keys())

    if args.skip_fetch:
        # 僅從快取讀取（不新增 API 呼叫）
        print("  [skip-fetch] 跳過 API 下載，僅使用快取中的資料\n")
        all_histories = {}
        for code in code_list:
            h = df.get_stock_history(code, "TWSE", history_months, end_date=end_date)
            if h:
                all_histories[code] = h
        print(f"  快取讀取完成：{len(all_histories)} 支有資料\n")
    else:
        all_histories = df.fetch_bulk_histories(
            code_list=code_list,
            exchange="TWSE",
            months=history_months,
            progress=True,
            end_date=end_date,
        )

    if not all_histories:
        print("[錯誤] 無歷史資料可用，請確認快取或網路連線。")
        return 1

    # ── 取得股本資料 ──────────────────────────────────────────
    print("步驟 3/4：取得公司股本資料...")
    include_tpex = bool(cfg.get("include_tpex", False))
    capital_map = df.fetch_company_capital(include_tpex=include_tpex)
    print(f"         共 {len(capital_map)} 家公司股本資料\n")

    # ── 執行回測 ──────────────────────────────────────────────
    print("步驟 4/4：執行回測模擬...")
    print(f"         選股篩選器將對每個交易日的全市場快照（~{len(all_histories)} 支）進行篩選\n")

    summary = engine.run_backtest(
        start_date=start_iso,
        end_date=end_iso,
        cfg=cfg,
        trading_cfg=cfg,  # 選股 + 交易參數共用同一個 dict
        all_histories=all_histories,
        name_map=name_map,
        capital_map=capital_map,
    )

    # ── 輸出結果 ──────────────────────────────────────────────
    output_dir = args.output if args.output else "reports"

    # 先印到 console
    report_text = report.generate_text_report(summary)
    print(report_text)

    # 儲存報告與圖表
    paths = report.save_report(summary, output_dir)

    print("\n" + "=" * 50)
    if paths["txt"]:
        print(f"[報告] 文字報告已儲存：{paths['txt']}")
    if paths["png"]:
        print(f"[報告] 圖表已儲存：{paths['png']}")
    else:
        print("[報告] 圖表未產生（請確認 matplotlib 已安裝：python -m pip install matplotlib）")
    print("=" * 50)

    return 0


if __name__ == "__main__":
    sys.exit(main())
