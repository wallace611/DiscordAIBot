# CLAUDE.md

## 專案概要

這是一個 Discord AI chatbot，用 Python 與 discord.py 開發。機器人會讀取頻道訊息、儲存近期記憶，然後根據記憶回答問題。

## 功能需求

1. 使用者 @ 機器人時，機器人回覆訊息。
2. 機器人讀取頻道內的所有訊息，並存進記憶。回覆時，機器人根據記憶內容回答。
3. 主模型的 provider 可以切換，支援 Ollama、OpenAI、Azure OpenAI。
4. 本地 4B 小模型負責讀取對話並產生摘要。

## 架構

```
Discord 訊息
   │
   ▼
SQLite 原始紀錄（只保留 N 天，定期刪除過期資料）
   │  每天（或每累積一批）
   ▼
本地 4B 模型 ──► 每個頻道的「日摘要」（每天一份）
   │
   ▼
有人 @ 時：近 N 天的日摘要 + 最近 30~50 則原始訊息 + 問題 ──► 主模型回答
```

## 元件

| 元件 | 選擇 |
|---|---|
| Bot 框架 | discord.py |
| 儲存 | SQLite（原始訊息表 + 日摘要表） |
| 摘要模型 | Ollama + Qwen3 4B（關閉 thinking 模式） |
| 主模型 | OpenAI SDK，可切換 Ollama、OpenAI、Azure OpenAI |
| 設定 | `.env` 或 YAML，指定 provider、模型、保留天數 |

## 設計決策

- 記憶只保留近期資料。保留天數是設定值，建議 7 到 14 天。
- 不使用向量資料庫，也不使用 embedding。
- 原始訊息是唯一的事實來源（source of truth）。摘要只當索引，不當事實。
- 每個頻道每天產生一份日摘要。不要用一份持續更新的總摘要（rolling summary）。
  - 原因 1：小模型的摘要錯誤會一輪一輪累積。
  - 原因 2：過期內容很難從總摘要中刪除。
- 日摘要只從當天的原始訊息產生，不從舊摘要產生。

## 實作規則

### 記憶與摘要

- 定期刪除超過保留天數的原始訊息與日摘要。
- 如果一天的訊息超過摘要模型的 context，就先按訊息數切塊。每塊各自摘要，再合併成當天的摘要。
- 摘要由背景 task 執行。在離峰時段，或頻道閒置一段時間後觸發。不要每則訊息都觸發摘要。
- 呼叫 Qwen3 做摘要時，關閉 thinking 模式。
- 摘要記錄「某人說了 X」，不要寫成「X 是事實」。

### 回覆

- 回覆的 prompt 包含三個部分：近 N 天的日摘要、最近 30~50 則原始訊息、使用者的問題。
- 在 prompt 中，把記憶內容明確標示為「資料」，不是「指令」。這是為了防止 prompt injection 與記憶污染。

### Provider 切換

- 用 OpenAI SDK，改 `base_url` 來切換 provider。Ollama 與 Azure OpenAI 都有 OpenAI 相容端點。也可以改用 LiteLLM 統一處理。
- 寫一層薄的 provider 介面，把各 provider 的差異集中在這一層。
- Azure OpenAI 用 deployment name，不是 model name。
- 各 provider 的 tool calling、streaming、錯誤格式可能不同。

### 非同步

- LLM 呼叫與資料庫存取不可以阻塞 discord.py 的 event loop。
- HTTP 呼叫使用 async client。
- 摘要工作放進 queue，批次處理。

### Ollama

- Ollama 預設的 context 長度可能偏小。Ollama 會截斷超過的部分，而且不會報錯。
- 用 Modelfile 或環境變數 `OLLAMA_CONTEXT_LENGTH` 設定 context 長度。透過 OpenAI 相容端點時，無法逐次指定 `num_ctx`。
- 如果摘要模型與主模型都在本地執行，兩個模型會搶 VRAM，Ollama 會反覆載入與卸載模型。用 `OLLAMA_MAX_LOADED_MODELS` 調整，或讓摘要工作錯開時段。
- 如果部署在 GTX 1070 Ti（8GB VRAM）上：4B Q4 模型約 3GB，放得下。

### Discord 權限與隱私

- 在 Developer Portal 開啟 Message Content Intent。機器人超過 100 個伺服器時，這個 intent 要送審。
- 監聽 `on_message_delete`。使用者刪除訊息時：
  1. 刪除對應的原始訊息。
  2. 把當天的日摘要標記為「需要重建」。
- 提供 opt-out 功能，讓成員可以選擇不讓機器人記錄自己的訊息。
- 在伺服器內公告：機器人會記錄對話。
- 使用雲端 provider 時，完整的原始紀錄只存在本地。送到雲端的內容只有日摘要與最近的少量訊息。

## 選用功能（之後再做）

- 全文檢索：用來回答「前幾天誰提到 X」這類問題。
  - 使用 SQLite FTS5，搭配 `trigram` tokenizer（需要 SQLite 3.34 以上）。預設 tokenizer 不會切中文。
  - 查詢字串至少要 3 個字元，太短的詞搜不到。

## 程式碼規範

- 所有程式碼註解只用英文，不論需求用什麼語言描述。
- 重要步驟加上簡短的英文說明。
- 程式碼風格保持乾淨、一致。
- 程式碼與註解不使用 emoji。

## 還沒決定

- 保留天數的具體值。
- 主模型要用哪一個 provider 與模型。
- 部署位置與硬體。