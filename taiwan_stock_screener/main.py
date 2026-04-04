# main.py
# 台股隔日沖選股系統 — 主程式入口
#
# 使用方式：
#   python main.py                        # 掃描最新資料（TWSE 上市）
#   python main.py --date 2026-04-01      # 指定日期
#   python main.py --include-tpex         # 同時掃描 TPEX 上櫃
#   python main.py --min-gain 6           # 臨時調整漲幅閾值
#   python main.py --csv reports/out.csv  # 輸出 CSV
#   python main.py --verbose              # 顯示除錯日誌

import argparse
import csv
import io
import json
import logging
import os
import sys
from datetime import datetime
from typing import Dict, List, Optional

# Windows 終端機 UTF-8 編碼修正（支援中文輸出）
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import config
import data_fetcher as df
import screener as sc

# ============================================================
# settings.json — 使用者可編輯的設定
# ============================================================

SETTINGS_FILE = os.path.join(os.path.dirname(__file__), "settings.json")

DEFAULT_SETTINGS = {
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
    "min_history_days":    config.MIN_HISTORY_DAYS,
}


def load_settings() -> Dict:
    """讀取 settings.json，不存在則自動建立預設檔。"""
    if not os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_SETTINGS, f, ensure_ascii=False, indent=2)
        print(f"已建立預設設定檔：{SETTINGS_FILE}")
        print("你可以直接編輯此檔案來調整篩選條件（無需修改程式碼）。\n")
        return dict(DEFAULT_SETTINGS)

    with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
        user_settings = json.load(f)

    # 合併：user_settings 覆蓋預設值
    merged = dict(DEFAULT_SETTINGS)
    merged.update(user_settings)
    return merged


# ============================================================
# 日誌設定
# ============================================================

def setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("screener.log", encoding="utf-8"),
        ]
    )
    # 抑制 requests 的詳細日誌
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)


# ============================================================
# CLI 參數
# ============================================================

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="台股隔日沖選股系統",
        formatter_class=argparse.RawTextHelpFormatter
    )
    p.add_argument(
        "--date", "-d", type=str, default=None,
        metavar="YYYYMMDD",
        help="查詢日期（預設：最新可用資料）"
    )
    p.add_argument(
        "--include-tpex", action="store_true", default=None,
        help="同時掃描 TPEX 上櫃（預設僅 TWSE 上市）"
    )
    p.add_argument(
        "--csv", type=str, default=None,
        metavar="PATH",
        help="將結果輸出為 CSV 檔案"
    )
    p.add_argument(
        "--verbose", "-v", action="store_true",
        help="顯示除錯日誌"
    )
    # 可覆蓋的篩選條件
    p.add_argument("--min-gain",    type=float, default=None, metavar="PCT",   help="最低漲幅 (%)")
    p.add_argument("--pre-filter",  type=float, default=None, metavar="PCT",   help="預篩選漲幅 (%)")
    p.add_argument("--cap-min",     type=float, default=None, metavar="NTD",   help="股本下限 (NTD)")
    p.add_argument("--cap-max",     type=float, default=None, metavar="NTD",   help="股本上限 (NTD)")
    p.add_argument("--vol-ratio",   type=float, default=None, metavar="X",     help="量比閾值")
    p.add_argument("--max-dt-ratio",type=float, default=None, metavar="RATIO", help="當沖率上限 (0~1)")
    p.add_argument("--ma-short",    type=int,   default=None, metavar="N",     help="短均線週期")
    p.add_argument("--ma-mid",      type=int,   default=None, metavar="N",     help="中均線週期")
    p.add_argument("--ma-long",     type=int,   default=None, metavar="N",     help="長均線週期")
    p.add_argument("--boll-window", type=int,   default=None, metavar="N",     help="布林通道週期")
    return p.parse_args()


def apply_cli_overrides(cfg: Dict, args: argparse.Namespace) -> Dict:
    """將 CLI 參數覆蓋到設定 dict。"""
    overrides = {
        "min_gain_pct":        args.min_gain,
        "pre_filter_gain_pct": args.pre_filter,
        "capital_min_ntd":     args.cap_min,
        "capital_max_ntd":     args.cap_max,
        "volume_multiplier":   args.vol_ratio,
        "max_day_trade_ratio": args.max_dt_ratio,
        "ma_short":            args.ma_short,
        "ma_mid":              args.ma_mid,
        "ma_long":             args.ma_long,
        "boll_window":         args.boll_window,
    }
    if args.include_tpex:
        cfg["include_tpex"] = True

    for key, val in overrides.items():
        if val is not None:
            cfg[key] = val

    return cfg


# ============================================================
# 報告輸出
# ============================================================

def _limit_label(status: str) -> str:
    if status == "LOCKED":
        return " 🔒 漲停鎖死"
    if status == "NEAR_LIMIT":
        return " ⭐ 接近漲停"
    return ""


def _ok_str(val: Optional[bool], ok_text: str = "OK", fail_text: str = "FAIL") -> str:
    if val is True:
        return f"[{ok_text}]"
    if val is False:
        return f"[{fail_text}]"
    return "[?]"


def print_report(results: List[Dict], cfg: Dict, total_scanned: int) -> None:
    ms  = cfg["ma_short"]
    mm  = cfg["ma_mid"]
    ml  = cfg["ma_long"]

    border = "=" * 65

    print()
    print(border)
    print("  台股隔日沖選股報告")
    print(f"  執行時間：{datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"  篩選通過：{len(results)} 檔  /  掃描：{total_scanned} 檔")
    print(f"  篩選條件：漲幅≥{cfg['min_gain_pct']}%  量比≥{cfg['volume_multiplier']}x")
    print(f"            股本 {cfg['capital_min_ntd']//1e8:.0f}億~{cfg['capital_max_ntd']//1e8:.0f}億")
    print(f"            MA 多頭排列({ms}/{mm}/{ml})  BB 突破上軌")
    print(border)

    if not results:
        print()
        print("  今日無符合條件的隔日沖標的。")
        print()
        print(border)
        return

    for rank, r in enumerate(results, 1):
        limit_lbl = _limit_label(r["漲停狀態"])
        flags     = r.get("資料旗標", "")
        flag_note = f"  ⚠ 資料警告: {flags}" if flags else ""

        # 當沖警告
        dt_warn = ""
        if "HIGH_DAY_TRADE_RATIO" in flags:
            dt_warn = "  ⚠ 當沖率過高，隔日開高賣壓沉重，謹慎操作！"

        # 三大法人
        inst_net = r.get("三大法人淨買(張)")
        inst_str = f"+{inst_net:,}張" if inst_net and inst_net > 0 else \
                   f"{inst_net:,}張" if inst_net is not None else "無資料"

        # 量比
        vol_r = r.get("量比(5日)")
        vol_str = f"{vol_r}x" if vol_r is not None else "無資料"

        # 當沖率
        dt_pct = r.get("當沖率(%)")
        dt_str = f"{dt_pct}%" if dt_pct is not None else "無資料(T+2)"

        # 股本
        cap_b = r.get("股本(億)")
        cap_str = f"{cap_b}億" if cap_b is not None else "無資料"

        print()
        print(f"  [{rank:02d}] {r['代號']}  {r['名稱']}  ({r['市場']}){limit_lbl}")
        print(f"       收盤：{r['收盤價']:.2f}  漲幅：{r['漲幅(%)']:+.2f}%  評分：{r['評分']}")
        print(f"       成交量：{r['成交量(張)']}張  量比：{vol_str}  股本：{cap_str}")
        print(f"       MA{ms}={r[f'MA{ms}']}  MA{mm}={r[f'MA{mm}']}  MA{ml}={r[f'MA{ml}']}  "
              f"多頭排列：{_ok_str(r['均線多頭排列'], '✓', '✗')}")
        print(f"       BB上軌={r['BB上軌']}  突破：{_ok_str(r['BB突破'], '✓', '✗')}  "
              f"量能確認：{_ok_str(r['BB量能確認'], '✓', '✗')}")
        print(f"       當沖率：{dt_str}  三大法人淨買：{inst_str}")
        cap_status = _ok_str(r["股本符合"], "OK", "FAIL") if r["股本符合"] is not None else "[無資料]"
        print(f"       股本篩選：{cap_status}")
        print(f"       產業別：請人工確認熱門題材")
        print(f"       券商分點：可在 settings.json 填入 finmind_token 後啟用")
        if flag_note:
            print(flag_note)
        if dt_warn:
            print(dt_warn)

    print()
    print(border)
    print()
    print("  【交易守則提醒】")
    print("  進場：13:00~13:25 確認即將鎖漲停或收在當日最高點")
    print("  出場：隔日 09:00~09:15，開高後第一根 K 線收黑即全數出清")
    print("  停損：隔日未開高，09:10 前斷然砍倉")
    print("  獲利目標：1.5%~3%（扣除手續費後淨利 2% 即為成功交易）")
    print()
    print("  【重要注意事項】")
    print("  • 注意美股盤後！費城半導體/那斯達克若大跌，")
    print("    隔日開盤不管賺賠，09:00 搓合那一刻立即市價出場")
    print("  • 漲停鎖死鎖越死（外盤委買越多），隔天開高機率越高")
    print("  • 本報告僅供參考，交易前請自行判斷風險")
    print()
    print(border)


def write_csv(results: List[Dict], path: str) -> None:
    """將結果寫入 CSV 檔案。"""
    if not results:
        return

    os.makedirs(os.path.dirname(path) if os.path.dirname(path) else ".", exist_ok=True)

    fieldnames = list(results[0].keys())
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in results:
            # None 值轉空字串
            cleaned = {k: ("" if v is None else v) for k, v in row.items()}
            writer.writerow(cleaned)

    print(f"\n  💾 CSV 報告已儲存：{path}")


# ============================================================
# 主程式
# ============================================================

def main() -> int:
    args = parse_args()
    setup_logging(args.verbose)
    logger = logging.getLogger(__name__)

    # 載入設定
    cfg = load_settings()
    cfg = apply_cli_overrides(cfg, args)

    include_tpex = cfg.get("include_tpex", False)

    logger.info("=== 台股隔日沖選股系統啟動 ===")
    logger.info("設定：漲幅≥%.1f%%  量比≥%.1f  股本 %.0f億~%.0f億  "
                "當沖率<%.0f%%  TPEX=%s",
                cfg["min_gain_pct"], cfg["volume_multiplier"],
                cfg["capital_min_ntd"] / 1e8, cfg["capital_max_ntd"] / 1e8,
                cfg["max_day_trade_ratio"] * 100,
                "是" if include_tpex else "否")

    # ---- Step 1: 取全市場資料 ----
    print("\n[1/5] 取得今日市場行情...")
    twse_stocks, actual_date_iso = df.fetch_twse_all_stocks(args.date)
    print(f"  TWSE 上市：{len(twse_stocks)} 支  （資料日期：{actual_date_iso or '未知'}）")

    tpex_stocks = []
    if include_tpex:
        tpex_stocks = df.fetch_tpex_all_stocks()
        print(f"  TPEX 上櫃：{len(tpex_stocks)} 支")

    all_stocks = twse_stocks + tpex_stocks
    if not all_stocks:
        print("\n❌ 無法取得市場資料。請確認網路連線，或今日為休市日。")
        return 2

    total_scanned = len(all_stocks)

    # ---- Step 2: 公司股本資料 ----
    print("\n[2/5] 取得公司股本資料（30天快取）...")
    capital_map = df.fetch_company_capital(include_tpex=include_tpex)
    if capital_map:
        print(f"  取得 {len(capital_map)} 家公司股本資料")
    else:
        print("  ⚠ 無法取得股本資料，將跳過股本篩選")

    # ---- Step 3: 歷史資料（在 screener 內批次取得）----
    print("\n[3/5] 取得個股歷史資料（僅針對預篩選股票）...")
    pre_count = sum(
        1 for s in all_stocks
        if s.get("gain_pct", 0) >= cfg["pre_filter_gain_pct"]
    )
    print(f"  預篩選（漲幅≥{cfg['pre_filter_gain_pct']}%）：約 {pre_count} 支需取歷史資料")
    print(f"  預估時間：{pre_count * 2 * 1.1:.0f} 秒（首次執行，後續有快取）")

    def history_fn(code: str, exchange: str):
        return df.get_stock_history(code, exchange, months=2)

    # ---- Step 4: 取三大法人 & 當沖資料 ----
    print("\n[4/5] 取得三大法人與當沖資料...")
    institution_map = df.fetch_institution_flows(args.date)
    day_trade_map   = df.fetch_day_trade_data(args.date)

    if not institution_map:
        print("  ⚠ 三大法人資料不可用")
    else:
        print(f"  三大法人：{len(institution_map)} 支")

    if not day_trade_map:
        print("  ⚠ 當沖資料不可用（T+2 延遲屬正常，不影響篩選）")
    else:
        print(f"  當沖統計：{len(day_trade_map)} 支")

    # ---- Step 5: 選股篩選 ----
    print("\n[5/5] 執行選股篩選...")
    # 使用 API 回傳的實際交易日（排除歷史資料中的今日資料）
    trade_date_iso = actual_date_iso or datetime.now().strftime("%Y-%m-%d")

    results = sc.screen_stocks(
        all_stocks=all_stocks,
        history_fn=history_fn,
        capital_map=capital_map,
        institution_map=institution_map,
        day_trade_map=day_trade_map,
        cfg=cfg,
        trade_date_iso=trade_date_iso,
    )

    # ---- 輸出報告 ----
    print_report(results, cfg, total_scanned)

    # ---- CSV 輸出 ----
    if args.csv:
        write_csv(results, args.csv)

    return 0


if __name__ == "__main__":
    sys.exit(main())
