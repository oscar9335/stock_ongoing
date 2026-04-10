# 台股隔日沖回測系統 — Phase 2 Backtest

對「隔日沖選股策略」進行歷史回測：跑完指定時間區間內的每一個交易日，模擬選股→進場→隔日出場的損益，並輸出文字報告與損益折線圖。

> **前置條件：** 此系統為獨立資料夾，不依賴 `taiwan_stock_screener/`，可單獨執行。

---

## 安裝

```bash
cd backtest_stratagy_check
python -m pip install -r requirements.txt
```

**需求：** Python 3.8+、網路連線（首次執行需下載歷史資料）

---

## 快速開始

```bash
# 預設：近 3 個月、50 萬初始資金、每日選前 3 名
python backtest_main.py

# 指定日期區間
python backtest_main.py --start 2026-01-01 --end 2026-04-03

# 調整資金與持股數
python backtest_main.py --capital 1000000 --top-n 5

# 放寬選股條件（產生更多交易訊號）
python backtest_main.py --min-gain 5 --vol-ratio 1.5

# 只用快取，不重新下載（第二次執行後可用）
python backtest_main.py --skip-fetch
```

---

## 交易策略模擬邏輯

| 動作 | 模擬方式 |
|---|---|
| **進場 (買)** | 訊號日**收盤價**買入（代表 13:00~13:25 近漲停進場） |
| **出場 (賣)** | **隔交易日開盤價**賣出（代表 09:00~09:15 出清） |
| **資金分配** | 當日資金均等分配給前 N 支股票，向下取整為整數**張** |
| **手續費** | 買賣各 0.1425%（可設折扣） |
| **證交稅** | 賣出時 0.3% |
| **停損** | 若隔日開盤 < 買入價 × (1 + stop\_loss\_pct)，損失已反映在開盤賣出價 |
| **無訊號日** | 篩選器無候選股時，當日不交易，資金保留 |
| **漲停鎖死** | 收盤時「漲停鎖死」的股票預設**跳過不買**（收盤前無賣單，現實無法成交）。TWSE 以 `prev_close×1.1` 取 tick 計算漲停板比對；TPEX 使用次日漲停價與賣量精確判斷 |

---

## 首次執行時間說明

系統需要下載全市場 ~1,000 支股票的歷史月份資料：

| 情境 | 預估時間 |
|---|---|
| **首次執行**（3 個月回測，需下載 6 個月資料） | **20~40 分鐘** |
| **第二次以後**（全部使用快取） | **< 1 分鐘** |

快取存放於 `cache/` 目錄，按月份分檔，下次執行直接讀取。

> **技巧：** 加上 `--skip-fetch` 可強制只用快取，完全不打 API。

---

## 設定檔 `settings.json`

可調整兩類參數：

### 選股條件（與 Screener Phase 1 相同）

```json
{
  "min_gain_pct": 7.0,
  "capital_min_ntd": 1000000000,
  "capital_max_ntd": 5000000000,
  "volume_multiplier": 2.0,
  "ma_short": 5,
  "ma_mid": 10,
  "ma_long": 20,
  "boll_window": 20,
  "boll_std_mult": 2.0,
  "max_day_trade_ratio": 0.60
}
```

### 交易策略參數（回測專用）

```json
{
  "bt_initial_capital": 500000,
  "bt_top_n": 3,
  "bt_commission_rate": 0.001425,
  "bt_commission_discount": 1.0,
  "bt_tax_rate": 0.003,
  "bt_stop_loss_pct": -0.01,
  "bt_months": 3,
  "bt_skip_locked": true
}
```

| 參數 | 說明 |
|---|---|
| `bt_initial_capital` | 初始資金（NTD） |
| `bt_top_n` | 每日最多持股支數 |
| `bt_commission_rate` | 手續費率（單邊），0.001425 = 0.1425% |
| `bt_commission_discount` | 手續費折扣，1.0 = 無折扣，0.5 = 五折 |
| `bt_tax_rate` | 證交稅（賣出），0.003 = 0.3% |
| `bt_stop_loss_pct` | 停損觸發跌幅，-0.01 = 平盤下 1% |
| `bt_months` | 未指定 `--start` 時，預設往回幾個月 |
| `bt_skip_locked` | `true`（預設）= 跳過漲停鎖死股票；`false` = 還原舊行為允許買入 |

---

## CLI 參數完整列表

```
回測期間：
  --start YYYY-MM-DD    回測起始日（預設：bt_months 個月前）
  --end   YYYY-MM-DD    回測結束日（預設：昨日）

資金設定：
  --capital NTD         初始資金（e.g. --capital 1000000）
  --top-n N             每日最多持股支數（e.g. --top-n 5）

輸出：
  --output DIR          報告輸出目錄（預設：reports/）
  --skip-fetch          跳過 API 下載，僅用快取
  --verbose             顯示 DEBUG 日誌

選股條件覆蓋（優先級高於 settings.json）：
  --min-gain PCT        最低日漲幅 %（e.g. --min-gain 5）
  --pre-filter PCT      預篩選漲幅 %
  --cap-min NTD         股本下限
  --cap-max NTD         股本上限
  --vol-ratio X         量比門檻（e.g. --vol-ratio 1.5）
  --max-dt-ratio R      最大當沖率
  --ma-short/mid/long N 均線週期
  --boll-window N       布林通道窗格
  --include-tpex        同時掃描上櫃股票

交易策略覆蓋：
  --commission-rate     手續費率
  --commission-discount 手續費折扣
  --tax-rate            證交稅率
  --stop-loss           停損跌幅（負值，e.g. --stop-loss -0.02）
  --skip-locked         跳過漲停鎖死股票（預設行為）
  --no-skip-locked      允許買入漲停鎖死股票（還原舊行為，回測結果偏樂觀）
```

---

## 報告輸出範例

執行後會在 `reports/` 目錄產生兩個檔案：

**`backtest_20260101_20260403.txt`（文字報告）**
```
============================================================
  台股隔日沖回測報告
  期間：2026-01-04 ~ 2026-04-03  (65 交易日)
  初始資金：500,000 NTD
============================================================

【績效摘要】
  最終資金：      523,480 NTD
  總損益：        +23,480 NTD  (+4.70%)
  有交易日數：         42 / 65 天
  交易次數：          126 筆
  勝率：             58.7%  (74 勝 / 52 負)
  平均每筆損益：      +186 NTD
  最大單日獲利：   +12,340 NTD  (2026-02-18)
  最大單日虧損：    -8,920 NTD  (2026-03-10)
  最大回撤：       -34,200 NTD  (-6.5%)

【每日交易明細】
2026-01-06
  ⬆ 2458 義隆   訊號漲幅+8.32%  3張@128.5 → 隔日開盤 132.0  +3,960 NTD (+1.35%)
  🔒 3661 世芯   訊號漲幅+9.77%  1張@580.0 → 隔日開盤 592.0  +7,200 NTD (+1.84%)
  ──────────────────────────────────────────────────────
  當日損益：+11,160 NTD   資金餘額：511,160 NTD

2026-01-07  — 無交易訊號
...
```

**`backtest_20260101_20260403.png`（圖表）**

雙子圖：
- 上圖：資金餘額折線圖（含初始資金基準線、最高/最低點標記）
- 下圖：每日損益長條圖（正值=綠、負值=紅）

---

## Grid Search 參數最佳化

對多組參數組合進行系統性回測，找出最佳策略設定。

### 安裝額外套件

```bash
python -m pip install plotly
```

### 執行方式

```bash
# 自動從 cache 推斷回測日期（推薦）
python grid_search.py

# 指定日期（cache 必須覆蓋該區間，且 start 前有 ≥ 20 個交易日的 MA 計算資料）
python grid_search.py --start 2026-01-01 --end 2026-04-03

# 完整網格（加入 max_day_trade_ratio，約 480 組，耗時較長）
python grid_search.py --full-grid
```

### 搜尋參數

| 參數 | 搜尋值 | 預設值 |
|------|--------|--------|
| `min_gain_pct`（最低漲幅%） | 5.0, 6.0, 7.0, 8.0, 9.0 | 7.0 |
| `volume_multiplier`（量比門檻） | 1.5, 2.0, 2.5, 3.0 | 2.0 |
| `bt_top_n`（每日最多持股數） | 1, 2, 3, 5 | 3 |
| `bt_skip_locked`（跳過漲停鎖死） | True, False | True |
| `max_day_trade_ratio`（--full-grid 才啟用） | 0.4, 0.6, 0.8 | 0.6 |

**預設組合數：** 5×4×4×2 = **160 組**（約 5~15 分鐘）
**完整組合數：** 160×3 = **480 組**（約 20~40 分鐘）

> Grid search 直接讀取 cache，不進行 API 下載，請確認已先執行 `backtest_main.py` 完成資料下載。

### 輸出目錄結構

```
grid_search_20260410_143052/
├── results/
│   └── results_20260410_143052.csv   ← 全量指標（160 筆，依損益率排序）
├── summary.txt                       ← 文字摘要報告
├── equity_curves.json                ← 每個組合的逐日資金曲線
└── report.html                       ← Plotly 互動式圖表（瀏覽器開啟）
```

### 報告內容

**`summary.txt`：**
- Top 10 最佳參數組合（損益率、勝率、最大回撤、交易次數）
- 單一參數敏感度分析：各參數值的平均/最佳損益率

**`report.html`（互動式）：**
- Top 10 組合資金曲線（hover 查看詳情，點擊 legend 開/關）
- 各參數敏感度長條圖（dropdown 切換「平均損益率 / 最佳損益率 / 平均勝率」）

**`results/results_*.csv`：**
- 全量 160+ 筆結果，可用 Excel 自行篩選特定參數組合做進階分析

---

## 使用情境：比較不同參數組合

修改 `settings.json` 後重新執行，對照兩份報告的績效摘要：

```bash
# 測試 1：嚴格選股（預設）
python backtest_main.py --start 2026-01-01 --end 2026-04-03 --output reports/strict

# 測試 2：放寬選股
python backtest_main.py --start 2026-01-01 --end 2026-04-03 --min-gain 5 --vol-ratio 1.5 --output reports/relaxed

# 測試 3：增加持股數
python backtest_main.py --start 2026-01-01 --end 2026-04-03 --top-n 5 --output reports/top5
```

---

## 快取機制

| 快取檔案 | 說明 | 有效期 |
|---|---|---|
| `cache/twse_{code}_{YYYYMM}.json` | 個股月份歷史（OHLCV） | 當月 1 小時 / 過去月份永久 |
| `cache/stock_name_map.json` | 股票代號→名稱對照表 | 1 小時 |
| `cache/company_capital.json` | 公司股本資料 | 30 天 |
| `cache/institution_{YYYYMMDD}.json` | 三大法人買賣超 | 當日 1 小時 / 歷史永久 |
| `cache/daytrade_{YYYYMMDD}.json` | 當沖交易量 | 當日 1 小時 / 歷史永久 |

> **注意：** 此資料夾的快取（`backtest_stratagy_check/cache/`）與 `taiwan_stock_screener/cache/` **完全獨立**，不共用。

---

## 檔案結構

```
backtest_stratagy_check/
├── backtest_main.py      ← CLI 入口點（執行這個）
├── backtest_engine.py    ← 回測核心（重建快照、模擬交易、計算損益）
├── backtest_report.py    ← 產生文字報告 + matplotlib 圖表
├── screener.py           ← 選股邏輯（與 Phase 1 相同）
├── data_fetcher.py       ← API 呼叫 & 快取（新增批次歷史下載）
├── config.py             ← 預設參數（含 BT_* 交易策略預設值）
├── settings.json         ← 使用者自訂設定
├── requirements.txt      ← 套件需求（含 matplotlib）
├── cache/                ← 自動建立的資料快取目錄
└── reports/              ← 自動建立的報告輸出目錄
```

---

## 常見問題

**Q: 第一次執行要等很久嗎？**
A: 是的，首次需下載全市場 ~1,000 支股票的歷史資料，約 20~40 分鐘。之後全部使用快取，< 1 分鐘完成。可先用短期間 `--start 2026-03-01 --end 2026-04-03` 測試系統是否正常。

**Q: 回測期間「無交易訊號」的天數很多，正常嗎？**
A: 正常。預設選股條件（漲幅≥7%、量比≥2x、MA+BB 全過）本來就很嚴格。試試 `--min-gain 5 --vol-ratio 1.5` 可產生更多訊號。

**Q: 如何知道哪些參數組合最好？**
A: 修改 `settings.json` 或用 CLI 覆蓋，每次指定不同 `--output` 目錄，對照生成的報告中「勝率」與「最大回撤」兩個指標。

**Q: 圖表沒有產生？**
A: 確認 matplotlib 已安裝：`python -m pip install matplotlib`。圖表中的中文字型會自動偵測系統字型（優先使用微軟正黑體），無需額外設定。

**Q: 想模擬有手續費折扣的情況（例如券商優惠 6 折）？**
A: 在 `settings.json` 設定 `"bt_commission_discount": 0.6`，或執行時加 `--commission-discount 0.6`。

**Q: 為什麼報告中有 🔒 標記的股票不見了？**
A: 🔒 代表「漲停鎖死」，預設會跳過不買（`bt_skip_locked: true`）。這是為了讓回測更符合現實——漲停鎖死時收盤前幾乎沒有賣單，實際上無法成交。判斷方式：TWSE 以 `prev_close × 1.1` 計算漲停板價格後與收盤價比對；TPEX 使用次日漲停價與揭示賣量判斷。若要還原舊行為，執行時加 `--no-skip-locked`，或在 `settings.json` 設定 `"bt_skip_locked": false`。

**Q: 報告的交易明細中「訊號漲幅」是什麼？**
A: 訊號日（買入當天）的股票漲幅，計算公式為 `(收盤價 - 前日收盤) / 前日收盤 × 100%`。可用來確認被選入的股票是否確實符合漲幅門檻，以及 🔒 鎖死股票的漲幅是否接近漲停板（約 9.7~10%）。
