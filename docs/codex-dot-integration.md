# ChatGPT dot / 外部 Agent 调用 DSA（Agent CLI）

本文说明如何让 ChatGPT dot 这类"自带电脑、可定时执行"的 Agent 主动调用 DSA：DSA 装在 Agent 的电脑上，Agent 通过 `python -m src.agent_cli` 拿到 JSON 结果，生成每日报告并维护持仓账本。

与 [Grok Bot 集成](grok-bot-integration.md) 的区别：Grok Bot 文档走已部署的 HTTP API；本文走本机命令行，不需要常驻服务、公网暴露或 API 鉴权。

## 能力边界

- Agent CLI 只包装现有服务：个股分析、大盘复盘、持仓账本（账户 / 成交 / 资金）、持仓快照与风险报告、决策信号查询，以及上海黄金交易所 Au99.99 行情（`gold`）。不新增数据库表、配置项或 HTTP 接口。
- `gold` 只返回行情数据（元/克），不做分析：黄金现货不进入个股分析流水线，`analyze` / `context` 不支持 `AU9999`、`Au99.99` 这类代码（会被当作 A 股代码而取数失败）。需要黄金 ETF 的技术面分析时，对 `518880` 等 ETF 代码使用 `context` 即可，但其价格是基金份额价格而非元/克。
- 推荐由 dot 自己充当分析模型：`context` 让 DSA 取数并生成与正常分析完全相同的提示词（不调用模型），dot 写出 JSON 结果后用 `analyze --responses` 交回，DSA 按标准流程解析、风控兜底、写入历史与决策信号。这样 DSA 不需要任何模型或搜索 key，也避免 "dot → DSA → 另一个模型" 的嵌套调用。
- 个股分析提示词不含持仓数量、成本与浮盈，与 `python main.py` 每日分析一致，报告中的仓位建议是"空仓者 / 持仓者"两套通用建议。结合真实持仓的建议由 dot 在每日汇总时基于 `portfolio snapshot` / `portfolio risk` 给出，见 Skill 示例。
- 持仓账本只记录**已确认的成交**，DSA 不对接券商下单。持仓写操作必须带 `--confirmed`，成交必须带 `--trade-uid` 以便重试去重。
- 持仓账本支持上金所 Au99.99 黄金：代码 `AU9999` / `Au99.99` 等价，数量以克、价格以元/克记录，市场使用 `cn`、币种使用 `CNY`。黄金 ETF 仍按基金份额记账，两者不合并。
- `market-review` 默认不推送通知，结果由 Agent 自己转达；个股分析同样不推送。需要 DSA 自带的通知渠道时，仍可用 `python main.py`。
- Agent 工具调用（问股 Agent）、图片识别不在本入口范围内。

命令、输出格式和硬规则见 Skill 示例：[`docs/examples/codex_dot/SKILL.md`](examples/codex_dot/SKILL.md)。

## 黄金记账与估值

复用现有 `cn` / `CNY` 账户和 `portfolio trade-add`，不需要 `gold` / `sge` 市场或新的数据库表。以下为成交参数示例，仅在用户确认实际成交后执行，替换账户 ID、日期、数量、价格与唯一成交号：

```bash
$DSA_PY -m src.agent_cli portfolio trade-add \
  --account-id 1 --symbol Au99.99 --trade-date 2026-11-16 \
  --side buy --quantity 1.5 --price 900 --market cn --currency CNY \
  --trade-uid gold-20261116-AU9999-buy-1 --confirmed
$DSA_PY -m src.agent_cli portfolio snapshot --account-id 1
```

- 新成交统一保存为 `AU9999`，已有 `AU99.99` / `AU9999` 记录在持仓重放、卖出校验和交易查询时合并识别，无需改写旧流水。小数克数、手续费、FIFO / 平均成本、部分卖出与 `trade_uid` 去重沿用原账本规则；股票分红和拆股操作不适用于黄金。
- 黄金交易及持仓输出附加 `asset_type=gold`、`quantity_unit=g`、`price_unit=CNY/g`。同样适用于现有 HTTP 持仓接口；旧客户端可忽略这些可选字段。交易必须是 `cn` / `CNY`；旧账中其他市场或币种的黄金不会被自动换义，快照以 `gold_requires_cn_cny` 标记，需要核对原始成交后更正。
- 默认的当日 `portfolio snapshot` / `portfolio risk` 为持有黄金的账户查询上金所报价，同时缓存接口返回的完整日线；多个账户共用本次黄金查询。全局 `ENABLE_REALTIME_QUOTE=false` 时不发起黄金查询。独立 `gold` 命令仍只取数，不写入账本行情缓存。
- `--no-realtime`、历史 `--as-of` 和只读快照只使用本地缓存；先获取一次默认当日快照可补齐黄金日线。历史估值只选指定日期及以前的收盘价，绝不拿当前金价回填过去。实时报价使用上游 `updated_at` 的日期，缺少有效日期或晚于估值日期的报价不参与估值，旧报价也不会覆盖较新的日线。
- 实时报价失败时使用缓存收盘价；无可用价格时 `price_available=false`、`price_source=missing`、`data_quality=partial`、`limitations` 含 `gold_price_unavailable`，市值占位为 0，浮盈比例为空。缺价不等于价值归零，不能据此解释收益或完整资产占比，也不会生成“亏损 100%”的止损告警。
- 黄金纳入市值、盈亏、集中度和基于成本的止损计算，行业分组显示为“黄金”，不查询股票板块或股票决策信号。持仓个股分析接口对黄金返回校验错误；dot 应使用 `gold` 自行分析，不能把黄金交给 `context` / `analyze`。
- 风险报告追加 `data_quality` / `limitations`。回撤计算排除黄金缺价的整个日期（`drawdown.excluded_gold_dates`），并标记 `historical_gold_price_unavailable`；补齐日线后会重建此前缺价的历史快照，避免沿用旧版本的零市值。
- 估值基准为上金所 Au99.99；不自动代表银行积存金赎回价、金饰回购价、国际金价或黄金期货。成交成本按用户实际价格与费用记录。

## 部署步骤

### 1. 持久目录

dot 的定时任务需要在**同一台能持久保存文件的电脑**上执行。先做持久性自检：建一个每 5 分钟运行一次的临时任务，往 `~/dsa-data/persist-check.txt` 追加一行时间戳；运行两次后文件应有两行，再删掉该任务。如果文件不保留（例如任务运行在每次重建的 Codex cloud environment 里），本方案的持仓账本会丢失，不要继续。

推荐布局：

```text
~/dsa-data/
  daily_stock_analysis/   # 仓库代码；报告写在其 reports/ 下
  venv/                   # Python 虚拟环境
  stock_analysis.db       # DATABASE_PATH
  logs/                   # LOG_DIR
```

```bash
cd ~/dsa-data
git clone https://github.com/ZhuLinsen/daily_stock_analysis.git
python3 -m venv venv
venv/bin/pip install -r daily_stock_analysis/requirements.txt
cp daily_stock_analysis/.env.example daily_stock_analysis/.env
```

在 `.env` 中设置：

```bash
STOCK_LIST=600519,hk00700,AAPL
DATABASE_PATH=/home/<user>/dsa-data/stock_analysis.db
LOG_DIR=/home/<user>/dsa-data/logs
```

### 2. 模型

- **推荐：不在 DSA 配置模型**，由 dot 通过 `context` → `analyze --responses` 完成分析（见 Skill 示例）。`.env` 中不设置任何 LLM Key / `GENERATION_BACKEND` 即可；大盘复盘会返回基于数据的模板报告，由 dot 自行解读。交接模式要求 `AGENT_MODE` 关闭且 `AGENT_SKILLS` 为空，否则返回 `agent_pipeline_unsupported`。交接文件默认写在 `reports/agent_handoff/<时间戳>/`，可定期清理。

如果希望 DSA 自己生成分析（`analyze` 不带 `--responses`），二选一：

- **使用 ChatGPT 订阅（Codex CLI）**：在该电脑上安装 Codex CLI，执行 `codex login --device-auth` 登录，然后设置 `GENERATION_BACKEND=codex_cli` 与 `GENERATION_FALLBACK_BACKEND=codex_cli`。单只股票的生成通常需要数分钟，可调大 `GENERATION_BACKEND_TIMEOUT_SECONDS`。细节与限制见 [LLM 配置指南](LLM_CONFIG_GUIDE.md)。
- **使用 API Key**：按 [LLM 配置指南](LLM_CONFIG_GUIDE.md) 配置任一渠道。

已知限制：本地 CLI backend 的子进程环境只透传最小 allowlist，`HTTP(S)_PROXY` 不在其中。如果该电脑需要通过环境变量代理才能访问 OpenAI，`codex_cli` 会以非零退出失败（`error_details.reason` 为 `network_unreachable`）；海外云电脑通常不受影响。

排障：`analyze` 全部失败时返回 `analysis_failed`，逐只诊断在 `error.details.results[].error_details`，重点看 `reason`、`returncode` 和 `stderr_tail_preview`（CLI 输出的脱敏尾部，真实错误通常在这里，开头的 `stderr_preview` 多是会话横幅）。

### 3. 数据源

海外机房访问东方财富 / akshare 部分接口可能被断开，DSA 会自动回退，但筹码、板块等字段可能缺失。A 股建议配置 `TICKPLUS_API_KEY` 或 `TUSHARE_TOKEN`，见 [数据源配置](full-guide.md#数据源配置)。新闻质量取决于搜索服务配置，见 [搜索服务配置](full-guide.md#搜索服务配置)；交接模式下可以不配搜索服务，由 dot 自行联网补查新闻。

### 4. 首次验证

```bash
cd ~/dsa-data/daily_stock_analysis
PY=~/dsa-data/venv/bin/python
$PY -m src.agent_cli portfolio accounts
$PY -m src.agent_cli context --stocks 600519
```

两条命令的 stdout 都应是 `"ok": true` 的 JSON；第二条的 `data.stocks[0].prompt_path` 指向生成的提示词。让 dot 按 Skill 写出 `response_path` 后执行 `data.next_command`，返回的 `data.report_path` 指向 Markdown 报告，`signal latest 600519` 能查到对应决策信号。若 DSA 自己配置了模型，也可以直接 `analyze --stocks 600519` 验证。

### 5. 交给 dot

1. 把 [`docs/examples/codex_dot/SKILL.md`](examples/codex_dot/SKILL.md) 作为 dot 的 Skill，并告诉它 `$DSA_HOME` 与 `$DSA_PY` 的实际路径。
2. 首次让 dot 在你确认后创建持仓账户，并录入当前持仓（逐笔 `trade-add`，或在 Web 端用 CSV 导入后再交给 dot 维护）。
3. 创建定时任务，例如"工作日 18:00（Asia/Shanghai）按 Skill 的每日例行流程执行，把汇总发给我；不要在未经我确认时记录任何成交"，并让 dot 复述保存的计划。

## 运行注意

- SQLite 是单写者。dot 任务执行期间不要同时在 Web 端修改持仓，否则可能返回 `portfolio_busy`。
- 报告文件名为 `reports/agent_report_<时间戳>.md`，不会覆盖 `main.py` 的 `report_<日期>.md`。
- `analyze --responses` 会重新拉取行情再套用 dot 的结论，盘中两次运行之间价格可能变化；每日例行任务建议放在收盘后。历史记录中的 `model_used` 为 `--model-label` 的值（默认 `external_agent`）。
- `gold` 数据来自 akshare 的上金所接口（`spot_quotations_sge` 实时分钟报价、`spot_hist_sge` 日线），不依赖任何 key。返回字段：`quote`（`price`、`time`、`updated_at`，未取到为 `null`）、`last_close` / `last_close_date`（最近日线收盘）、`change_pct` 与 `change_basis`（`realtime_vs_last_close` 或 `last_two_closes`）、`history`（最近 `--days` 条日线，默认 30，上限 250）、`warnings`（降级原因）。两路都失败时返回 `data_unavailable`（退出码 1）。上金所有夜盘，`quote.time` 只有时分秒，日期以 `updated_at` 为准。
- `--no-realtime` 只用本地已存的历史收盘价计算市值；若本地还没有该股的日线数据，市值可能显示为 0。每日例行流程建议使用默认的实时行情，或先对持仓代码跑一次 `analyze` 以写入日线。

## 回滚

Agent CLI 不引入数据迁移。停用 dot 定时任务即可停止调用；持仓数据仍在 `DATABASE_PATH` 中，可继续通过 Web 端或 API 使用。回退黄金兼容代码也不需要删除流水或缓存，但旧版本不再合并黄金别名或获取金价，黄金估值和风险结果不应继续使用。
