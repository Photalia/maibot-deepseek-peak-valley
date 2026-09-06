# 梁文峰／梁文谷：DeepSeek 峰谷價格播報

一個會每天核對官方價格、再讓 MaiBot Replyer 負責銳評的社群插件。

它將 DeepSeek API 的高峰／空閒時段分別稱為：

- 高峰：**梁文峰**
- 空閒：**梁文谷**

> 本專案是非官方社群插件，與 DeepSeek 官方無隸屬關係。價格與峰谷規則來源為 [DeepSeek API 官方價格文件](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)。

## 功能

- 每天北京時間 `00:00` 透過 MaiBot 已配置的 **Exa MCP** 抓取官方價格頁。
- 從官方內容解析 V4 Flash／Pro 的輸入、輸出、緩存命中價格及峰谷規則，不再硬編碼價格。
- 官方目前為**週一至週五** `09:00–12:00`、`14:00–18:00` 高峰；週六、週日全天梁文谷。
- 每日只呼叫一次 MaiBot 內置 `replyer`，分別生成當天峰／谷銳評。
- 時間、星期、價格、峰谷狀態及下次切換均由程式強制填寫，Replyer 不能修改事實欄位。
- `/峰谷`：即時查詢當前狀態。
- 峰谷切換時向配置的 QQ 群主動播報；週末不虛構切換。
- 使用者／聊天流雙層防刷、持久化去重及失敗群獨立重試。

## 輸出示例

```text
時間：2026/08/23/14/47 週日
當前時段：「梁文谷」
V4 Flash價格：輸入1.5／輸出4.5／緩存0.05
V4 Pro價格：輸入4.5／輸出13.5／緩存0.15
下次「梁文峰」在18小時13分鐘後（2026/08/24/09/00 週一）
銳評：週末沒有梁文峰，只有價格終於肯下班。
單位：元／百萬 tokens
```

## 工作流程

1. 插件啟動時讀取 `data/state.json` 中最近一次成功快照。
2. 若缺少當日快照，立即使用 Exa MCP 的 `web_fetch_exa` 抓取官方頁；之後每天 `00:00` 更新。
3. 只有在頁面成功抓取且所有必要價格／時間欄位均成功解析後，才覆蓋快照。
4. 將結構化官方資料交給 Replyer，一次生成 `peak`／`off_peak` 兩句每日銳評。
5. `/峰谷` 和定時播報根據同一份快照計算事實欄位。

若首次啟動時官方資料無法取得，插件會明確報錯，不會把可能已過期的內建價格冒充最新報價。若已有最近一次成功快照，暫時性抓取故障不會破壞該快照。

## 前置要求

- MaiBot `>= 1.2.0`
- MaiBot Plugin SDK `>= 2.5.0`
- MaiBot 全局 MCP 已啟用並配置可用的 Exa MCP；預設服務名稱為 `ExaSearchMCP`
- Exa MCP 提供 `web_fetch_exa` 工具
- MaiBot 已配置 `replyer` 任務模型（若關閉每日銳評生成則非必需）

範例全局 MCP 配置：

```toml
[mcp]
enable = true

[[mcp.servers]]
name = "ExaSearchMCP"
enabled = true
transport = "streamable_http"
url = "https://mcp.exa.ai/mcp?tools=web_fetch_exa"
```

認證資料仍保存在 MaiBot 全局配置中；本插件透過 `ctx.config.get` 使用現有設定，不另存 Exa Token。

## 安裝

### 插件廣場

在 MaiBot 插件廣場搜尋「DeepSeek 峰谷價格播報」並安裝。

### 手動安裝

在 MaiBot 的 `plugins` 目錄執行：

```bash
git clone https://github.com/Photalia/maibot-deepseek-peak-valley.git local_deepseek-peak-valley
```

隨後重啟 MaiBot。

## 配置

MaiBot 可自動生成 `config.toml`，也可以手動複製：

```bash
cp config.example.toml config.toml
```

主要配置：

```toml
[plugin]
enabled = true
config_version = "1.1.1"

[schedule]
enabled = true
target_group_ids = ["123456789", "987654321"]
grace_seconds = 120

[pricing]
source_url = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"
exa_server_name = "ExaSearchMCP"
refresh_hour = 0
refresh_minute = 0
fetch_max_characters = 20000

[replyer]
enabled = true
temperature = 0.9
max_tokens = 240
prompt = "根據今天的 DeepSeek 峰谷價格，各寫一句有趣、尖銳但不惡毒的短評。可以玩梁文峰／梁文谷的諧音梗。"

[rate_limit]
user_cooldown_seconds = 30
stream_cooldown_seconds = 5
```

冷卻期間的重複命令會被靜默吞掉，避免限流提示本身造成刷屏。

## 定時與週末邏輯

峰谷時間不是插件常量，而是每天從官方頁解析。依 2026-08-23 官方規則：

- 週一至週五 `09:00`：進入梁文峰
- 週一至週五 `12:00`：進入梁文谷
- 週一至週五 `14:00`：進入梁文峰
- 週一至週五 `18:00`：進入梁文谷
- 週六、週日：全天梁文谷，不播報不存在的峰谷切換
- 週五 `18:00` 後的下一次梁文峰為週一 `09:00`

插件使用固定 UTC+8 計算北京時間，不依賴宿主機時區。各群發送狀態及官方快照保存在 `data/state.json`。

## 與睡眠／宵禁插件共存

若另一個插件會在睡眠期間全局攔截入站命令或出站訊息，定時播報或 `/峰谷` 也可能被攔截。請在對應插件中為 `/峰谷` 和本插件的系統播報配置白名單；本倉庫不會自動修改其他插件。

## 隱私與網路

- 不讀取聊天歷史，不收集使用者資料。
- 僅向配置的 Exa MCP 傳送 DeepSeek 官方價格頁 URL。
- 每日銳評只向 MaiBot 既有 Replyer 傳送官方結構化價格、Bot 人格及表達風格。
- Exa 認證沿用 MaiBot 全局 MCP 配置，不寫入插件資料檔。

## 開發與測試

```bash
python -m py_compile plugin.py
python tests/test_pricing.py
```

測試涵蓋官方 Markdown 價格解析、工作日邊界、週末全天谷價、週五晚跳至週一及完整輸出欄位。

## License

[MIT](LICENSE)
