# 台股隔日沖選股系統 — Phase 1 Screener

每日收盤後，自動從全市場掃描符合「隔日沖」條件的候選股票，並輸出排名報告。

提供兩種使用方式：**圖形介面（建議）** 或 **命令列（CLI）**。

---

## 安裝

```bash
cd taiwan_stock_screener
python -m pip install -r requirements.txt
```

**需求：** Python 3.8+、網路連線（呼叫 TWSE / TPEX 免費 API）

---

## 方式一：圖形介面（Streamlit）

瀏覽器操作，可視覺化調整所有篩選條件，**建議使用此方式**。

```bash
streamlit run app.py
```

執行後瀏覽器自動開啟 `http://localhost:8501`。

圖形介面分為兩個分頁：

### 分頁一：選股篩選

| 區域 | 功能 |
|---|---|
| 左側邊欄 | 所有篩選條件的滑桿與輸入欄，即時調整 |
| 「💾 儲存設定」按鈕 | 將目前設定寫回 `settings.json`，下次執行自動套用 |
| 「↩️ 恢復預設值」按鈕 | 一鍵還原所有條件至預設值 |
| 「🔍 執行選股」按鈕 | 開始掃描，含 4 步驟進度顯示與每支個股下載進度條 |
| 結果總覽表格 | 所有候選股的關鍵指標，可點欄位標題排序 |
| 個股詳細展開 | 行情 / 技術指標 / 籌碼 / 交易提醒，第一名預設展開 |
| 「📡 載入盤中監控」 | 執行選股後，一鍵將候選股清單送至盤中監控分頁 |

### 分頁二：盤中監控

收盤前（13:00~13:25）即時追蹤候選股狀態，輔助判斷進場時機。

| 區域 | 功能 |
|---|---|
| 監控清單來源 | 可從選股結果自動載入，或手動輸入股票代號（逗號分隔） |
| 手動輸入格式 | `2330, 2454, 2308-OTC`（上市直接輸入代號，上櫃加 `-OTC` 後綴） |
| 個股刪除 | 每支股票卡片右側有 ✕ 按鈕，可個別移除 |
| 手動刷新 | 點「🔄 立即刷新」取得最新報價 |
| 自動刷新 | 勾選後選擇間隔（10 / 15 / 20 / 30 / 60 秒），頁面自動倒數更新 |
| 個股狀態 | 🔒 漲停鎖死 / ⚡ 接近漲停 / ✅ 正常，附進場建議文字 |
| 委買委賣 | 顯示最優一檔買價/賣價及量 |
| 摘要表格 | 所有監控股的報價一覽 |

**注意：** 盤中報價來源為 TWSE MIS 即時 API，僅在交易時間（09:00~13:30）有效。收盤後資料不會更新。

---

## 方式二：命令列（CLI）

適合排程自動化或需要 CSV 輸出的情境。

```bash
# 掃描最新交易日（每天收盤後執行）
python main.py

# 掃描特定日期
python main.py --date 20260320

# 同時掃描上櫃 (TPEX)
python main.py --include-tpex

# 匯出 CSV
python main.py --csv reports/output.csv

# 顯示 DEBUG 日誌
python main.py --verbose
```

---

## 篩選條件

所有數值均可透過 `settings.json` 或 CLI 參數調整：

| 條件 | 預設值 | 說明 |
|---|---|---|
| 日漲幅 | ≥ 7% | 主篩選條件 |
| 近漲停判定 | ≥ 9.5% | 超過此值標記為 NEAR\_LIMIT |
| 股本 | 10億 ~ 50億 NTD | 鎖定中小型股 |
| 量比 | ≥ 2x（5日均量） | 爆量確認 |
| 均線多頭排列 | 收盤 > MA5 > MA10 > MA20 | 趨勢確認 |
| 布林通道突破 | 收盤 ≥ 上軌 | 動能確認 |
| 當沖率 | < 60% | 過高代表籌碼不穩 |
| 三大法人 | 標注（不過濾） | 顯示外資/投信/自營商淨買賣 |

---

## 設定檔 `settings.json`

首次執行自動建立，可直接編輯後重新執行：

```json
{
  "min_gain_pct": 7.0,
  "pre_filter_gain_pct": 5.0,
  "near_limit_up_pct": 9.5,
  "capital_min_ntd": 1000000000,
  "capital_max_ntd": 5000000000,
  "volume_multiplier": 2.0,
  "ma_short": 5,
  "ma_mid": 10,
  "ma_long": 20,
  "boll_window": 20,
  "boll_std_mult": 2.0,
  "max_day_trade_ratio": 0.60,
  "include_tpex": false,
  "finmind_token": ""
}
```

修改範例：想把漲幅門檻降到 6%，只需把 `"min_gain_pct": 7.0` 改為 `"min_gain_pct": 6.0`。

---

## CLI 參數完整列表

```
--date YYYYMMDD       指定查詢日期（預設：最新交易日）
--include-tpex        同時掃描上櫃股票（預設：僅上市）
--csv PATH            將結果匯出為 CSV 檔案
--verbose             顯示詳細 DEBUG 日誌

選股條件覆蓋（優先級高於 settings.json）：
--min-gain PCT        最低日漲幅 % （e.g. --min-gain 6）
--pre-filter PCT      預篩選漲幅 %
--cap-min NTD         股本下限（e.g. --cap-min 500000000）
--cap-max NTD         股本上限
--vol-ratio X         量比門檻（e.g. --vol-ratio 1.5）
--max-dt-ratio R      最大當沖率（e.g. --max-dt-ratio 0.5）
--ma-short N          MA 短週期
--ma-mid N            MA 中週期
--ma-long N           MA 長週期
--boll-window N       布林通道計算窗格（天數）
```

---

## 輸出格式範例

```
==================================================
  台股隔日沖選股報告  2026-03-20
  篩選通過: 3 檔 / 掃描: 1,082 檔
==================================================

[1] 2458  義隆  (TWSE)  ⬆ NEAR_LIMIT
  收盤: 128.5  漲幅: +9.6%
  成交量: 8,432張  量比: 3.2x  [OK]
  MA5=121.3  MA10=115.8  MA20=108.4  [多頭排列 ✓]
  BB上軌=126.9  [突破 ✓]  [量確認 ✓]
  當沖率: 41.2%  [OK]
  股本: 28.3億 NTD  [OK]
  三大法人淨買: +1,234 張

  ⚠ 交易提醒：
    - 進場時間：13:00~13:25，確認接近漲停才買
    - 出場時間：隔日 09:00~09:15，開高即出清
    - 停損：平盤下 1%（09:10 前砍倉）
    - 注意美股夜盤：若大跌，隔日開盤即市價賣出
```

---

## 快取機制

第一次執行需要 2~5 分鐘（下載約 20~50 支個股月份資料）。
之後重複執行使用快取，< 10 秒完成。

| 快取檔案 | 說明 | 有效期 |
|---|---|---|
| `cache/twse_{code}_{YYYYMM}.json` | 個股月份歷史 | 當月 1 小時 / 過去月份永久 |
| `cache/company_capital.json` | 公司股本資料 | 30 天 |

---

## 檔案結構

```
taiwan_stock_screener/
├── app.py            ← 圖形介面入口點（streamlit run app.py）
├── main.py           ← CLI 入口點（python main.py）
├── screener.py       ← 選股邏輯（純計算，無 I/O）
├── data_fetcher.py   ← API 呼叫 & 快取管理（含重試/Jitter 保護）
├── config.py         ← 預設參數設定
├── settings.json     ← 使用者自訂設定（自動建立）
├── requirements.txt  ← 套件需求（含 streamlit）
└── cache/            ← 自動建立的資料快取目錄
```

---

## 資料來源

全部使用 TWSE / TPEX 官方免費 API，無需任何帳號或金鑰：

| 資料 | 來源 |
|---|---|
| 全市場日行情 | TWSE STOCK\_DAY\_ALL |
| 個股月份歷史 | TWSE/TPEX STOCK\_DAY |
| 三大法人買賣超 | TWSE T86 |
| 當沖交易量 | TWSE TWTB4U |
| 公司股本 | TWSE OpenAPI |

> **注意：** TWSE `STOCK_DAY_ALL` 不支援查詢歷史日期，永遠回傳最新交易日資料。
> `--date` 參數僅影響三大法人與當沖資料的查詢日期。

---

## 常見問題

**Q: 執行後顯示「篩選通過: 0 檔」？**
A: 當日市場無符合條件的股票（例如大跌日）。可用 `--min-gain 5 --vol-ratio 1.0` 放寬條件確認系統正常運作。

**Q: 當沖率顯示 `DAY_TRADE_DATA_UNAVAILABLE`？**
A: 當沖資料有 T+2 延遲，查詢當日或前兩日資料時正常出現。

**Q: 如何加入上櫃股票？**
A: 圖形介面：左側勾選「包含上櫃 (TPEX)」後儲存設定。CLI：加 `--include-tpex` 參數，或在 `settings.json` 設定 `"include_tpex": true`。

**Q: 圖形介面啟動後顯示錯誤？**
A: 確認已安裝 streamlit：`python -m pip install streamlit`，然後在 `taiwan_stock_screener/` 目錄內執行 `streamlit run app.py`（不能在其他目錄執行）。

**Q: 圖形介面修改設定後要儲存嗎？**
A: 必須點「💾 儲存設定」才會寫回 `settings.json`。若只是想這次用不同條件但不永久儲存，可直接調整後執行，不點儲存即可。

**Q: 盤中監控分頁顯示「無法取得報價」？**
A: 可能原因：① 非交易時間（盤前/盤後/假日）② 股票代號輸入錯誤 ③ TWSE MIS API 暫時無回應。可等幾秒後重新刷新。

**Q: 盤中監控如何使用最有效率？**
A: 建議流程：① 先在「選股篩選」分頁執行選股 → ② 點「📡 載入盤中監控」 → ③ 切換至「盤中監控」分頁 → ④ 開啟自動刷新（15~20 秒間隔）→ ⑤ 等待 🔒 漲停鎖死訊號後在 13:00~13:25 進場。
