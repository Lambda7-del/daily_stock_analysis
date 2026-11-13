---
name: daily-stock-analysis-cli
description: 在本机终端调用 daily_stock_analysis（DSA）的 Agent CLI：由 DSA 取数并组装分析上下文，你自己完成个股分析后交回 DSA 入库，再做持仓快照与风险检查，并在用户确认成交后记账。当用户要求分析股票、做每日复盘、查看或调整持仓记录时使用。
---

# daily_stock_analysis Agent CLI

本 Skill 供 ChatGPT dot 等在自有电脑上运行的 Agent 使用。DSA 已安装在本机，通过命令行调用，不经 HTTP。

分工：**DSA 负责数据、指标、提示词、解析、风控兜底和入库，你负责分析本身。** DSA 不需要配置任何模型或搜索 key，也不会替你调用模型。

## 运行约定

- 工作目录：DSA 仓库根目录（下文记为 `$DSA_HOME`），使用其虚拟环境里的 Python（下文记为 `$DSA_PY`）。
- 统一入口：`cd $DSA_HOME && $DSA_PY -m src.agent_cli <子命令>`。
- stdout 只有一个 JSON 对象，日志在 stderr。只解析 stdout，不要把 stderr 当结果。
  - 成功：`{"ok": true, "command": "...", "data": {...}}`
  - 失败：`{"ok": false, "command": "...", "error": {"type": "...", "message": "...", "details": {...}}}`（`details` 可选）
  - `analyze` 返回 `analysis_failed` 时，逐只读取 `error.details.results[].error_details`（`error_code`、`reason`、`message`、`response_path`），按原因修正后重试，不要只转述 `message`。
- 退出码：`0` 成功；`2` 参数、校验或确认类错误（不要原样重试，先修正参数或征求用户确认）；`1` 运行错误（可以稍后重试一次）。
- `context` 和 `analyze` 每只股票取数约 1～2 分钟，给命令留足超时；股票多时分批执行（每批不超过 5 只）。

## 命令速查

| 目的 | 命令 |
| --- | --- |
| 取数并生成分析提示词（不调用模型） | `context --stocks 600519,hk00700,AAPL [--handoff-dir DIR]` |
| 提交你写好的分析并入库、生成报告 | `analyze --stocks <同上> --responses <handoff_dir> [--model-label dot]` |
| 大盘复盘数据（不推送通知） | `market-review` |
| 列出持仓账户 | `portfolio accounts` |
| 持仓快照与盈亏 | `portfolio snapshot [--account-id N] [--no-realtime]` |
| 持仓风险报告 | `portfolio risk [--account-id N] [--no-realtime]` |
| 交易流水 | `portfolio trades [--account-id N] [--symbol 600519]` |
| 某股最新决策信号 | `signal latest 600519` |
| 创建账户（写） | `portfolio account-create --name X --market cn --base-currency CNY --confirmed` |
| 记录成交（写） | `portfolio trade-add --account-id N --symbol 600519 --trade-date 2026-11-13 --side buy --quantity 100 --price 1435.26 [--fee F] [--tax T] --trade-uid UID --confirmed` |
| 记录资金转入/转出（写） | `portfolio cash-add --account-id N --event-date 2026-11-13 --direction in --amount 100000 --confirmed` |

## 个股分析交接（context → 你分析 → analyze --responses）

1. 运行 `context --stocks <代码>`。`data.stocks[]` 每项给出 `system_prompt_path`、`prompt_path`、`response_path`；`data.failed` / `data.missing` 是没能生成上下文的代码，如实告诉用户。
2. 对每只股票：读 `system_prompt_path`（分析规则与输出 JSON 格式）和 `prompt_path`（DSA 整理好的行情、均线、趋势、基本面、大盘与持仓上下文）。
   - 提示词里"舆情情报"缺失时，用你自己的联网能力补查近 3 日的公告与新闻，并遵守提示词中的时间合规要求；查不到就按提示词说明"数据缺失"，不要编造。
   - 遵守提示词里的数据限制与置信度规则，不要编造未提供的数据。
3. 把**一个**符合 system prompt 输出格式的 JSON 对象写入对应的 `response_path`（只写 JSON，不要 Markdown 代码块外的说明文字）。
4. 执行 `data.next_command`（即 `analyze --stocks ... --responses <handoff_dir>`），可追加 `--model-label dot`。DSA 会重新取最新行情，用你的 JSON 走标准解析、完整性校验、风控兜底，写入历史与决策信号，并生成 Markdown 报告（`data.report_path`）。
5. 某只股票失败时看 `error_details`：`invalid_json` / `schema_validation_failed` 表示 JSON 不合格，按 `message` 修正文件后只重跑该股票；`response_file_missing` 表示文件没写到 `response_path`。

`analyze` 的 `data.results[]` 里，结论读 `operation_advice` / `action`（八态 `buy|add|hold|reduce|sell|watch|avoid|alert`）、`sentiment_score`、`trend_prediction`，点位读 `battle_plan`。

## 每日例行流程

1. `market-review`，基于其中的数据给出你自己的大盘判断（未配置模型时 DSA 返回数据模板报告）。
2. `portfolio snapshot` 取出所有持仓代码，与用户的自选股合并去重。
3. 按上面的交接流程完成个股分析（多于 5 只时分批）。
4. `portfolio risk`，关注集中度、回撤、止损告警。
5. 汇总发给用户：大盘一句话结论；每个持仓的建议动作、关键点位和理由；风险告警；新机会（自选股中非持仓且建议买入的）。明确标注这些是建议，不是已执行的操作。

## 硬规则

- **没有用户明确确认的成交，不得调用 `trade-add`、`cash-add` 或 `account-create`。** 分析建议不是成交；用户说"想买"也不是成交。只有用户给出（或确认）实际成交的方向、数量、价格、日期后，才可记账。
- `trade-uid` 用 `券商-成交日期-代码-方向-序号` 生成，例如 `futu-20261113-600519-buy-1`。同一笔成交重试时必须复用同一个 uid。
- 返回 `already_recorded` 表示这笔已经记过，不要换 uid 重写；如用户确认是另一笔成交，才用新的序号。
- 返回 `portfolio_oversell`、`validation_error` 时如实告诉用户，不要自行改数量或价格。
- `cash-add` 没有去重，执行前先用 `portfolio snapshot` 核对现金，避免重复记录。
- 返回 `agent_pipeline_unsupported` 时，说明 DSA 开启了 `AGENT_MODE` 或 `AGENT_SKILLS`，请用户关闭后再用交接流程，不要自行修改 `.env`。
- 不修改、不删除 DSA 的数据库和 `.env`，不尝试代替用户下单。
