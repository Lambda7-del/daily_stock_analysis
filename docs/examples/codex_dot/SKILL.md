---
name: daily-stock-analysis-cli
description: 通过本机 DSA Agent CLI 分析股票与黄金、从新闻发现候选并维护关注、复盘持仓和检查风险，在用户确认实际成交后记账。用于每日复盘、关注管理、股票分析及黄金行情和账本查询。
---

# daily_stock_analysis Agent CLI

本 Skill 供 ChatGPT dot 等在自有电脑上运行的 Agent 使用。DSA 已安装在本机，通过命令行调用，不经 HTTP。

分工：**DSA 负责数据、指标、提示词、解析、风控兜底和入库，你负责分析本身。** 使用下述数据接口和 `context` → `analyze --responses` 时不调用 DSA 模型。未配置新闻源或搜索 key 时，由你联网补充证据。

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
| 大盘结构化行情（不调用模型） | `market-review --data-only [--region cn]` |
| 新闻情报（可刷新已启用源） | `news [--refresh] [--market cn] [--days 3] [--page 1]` |
| 关注列表、移除记录和版本 | `watchlist list` |
| 用户明确要求添加 / 移除关注 | `watchlist add AAPL [--reason 理由]` / `watchlist remove AAPL` |
| 保存候选复查并自动维护关注 | `watchlist apply --file review.json` |
| 上轮候选、证据和关注变化 | `watchlist history [--limit 20]` |
| 上金所 Au99.99 黄金行情（元/克，只取数不分析） | `gold [--days 30] [--no-realtime]` |
| 列出持仓账户 | `portfolio accounts` |
| 持仓快照与盈亏 | `portfolio snapshot [--account-id N] [--no-realtime]` |
| 持仓风险报告 | `portfolio risk [--account-id N] [--no-realtime]` |
| 交易流水 | `portfolio trades [--account-id N] [--symbol 600519]` |
| 某股最新决策信号 | `signal latest 600519` |
| 创建账户（写） | `portfolio account-create --name X --market cn --base-currency CNY --confirmed` |
| 记录成交（写） | `portfolio trade-add --account-id N --symbol 600519 --trade-date 2026-11-13 --side buy --quantity 100 --price 1435.26 [--fee F] [--tax T] --trade-uid UID --confirmed` |
| 记录黄金成交（写，克 / 元每克） | `portfolio trade-add --account-id N --symbol AU9999 --market cn --currency CNY --trade-date 2026-11-16 --side buy --quantity 1.5 --price 900 --trade-uid UID --confirmed` |
| 记录资金转入/转出（写） | `portfolio cash-add --account-id N --event-date 2026-11-13 --direction in --amount 100000 --confirmed` |

## 个股分析交接（context → 你分析 → analyze --responses）

1. 运行 `context --stocks <代码>`。`data.stocks[]` 每项给出 `system_prompt_path`、`prompt_path`、`response_path`；`data.failed` / `data.missing` 是没能生成上下文的代码，如实告诉用户。
2. 对每只股票：读 `system_prompt_path`（分析规则与输出 JSON 格式）和 `prompt_path`（DSA 整理好的行情、均线、趋势、基本面与大盘上下文）。
   - 提示词**不含**用户的持仓数量、成本和浮盈，这与 DSA 原有分析流程一致。`position_advice` 按提示词要求分别写空仓者和持仓者的通用建议，不要在 JSON 里写入用户的成本或仓位。
   - 提示词里"舆情情报"缺失时，用你自己的联网能力补查近 3 日的公告与新闻，并遵守提示词中的时间合规要求；查不到就按提示词说明"数据缺失"，不要编造。
   - 遵守提示词里的数据限制与置信度规则，不要编造未提供的数据。
3. 把**一个**符合 system prompt 输出格式的 JSON 对象写入对应的 `response_path`（只写 JSON，不要 Markdown 代码块外的说明文字）。
4. 执行 `data.next_command`（即 `analyze --stocks ... --responses <handoff_dir>`），可追加 `--model-label dot`。DSA 会重新取最新行情，用你的 JSON 走标准解析、完整性校验、风控兜底，写入历史与决策信号，并生成 Markdown 报告（`data.report_path`）。
5. 某只股票失败时看 `error_details`：`invalid_json` / `schema_validation_failed` 表示 JSON 不合格，按 `message` 修正文件后只重跑该股票；`response_file_missing` 表示文件没写到 `response_path`。

`analyze` 的 `data.results[]` 里，结论读 `operation_advice` / `action`（八态 `buy|add|hold|reduce|sell|watch|avoid|alert`）、`sentiment_score`、`trend_prediction`，点位读 `battle_plan`。

## 黄金行情与记账

- 用户问金价（元/克）时调用 `gold`，基于 `data.quote.price`、`data.change_pct`、`data.history` 自己分析。不要把 `AU9999`、`Au99.99` 传给 `context` / `analyze`，它们只支持股票、ETF 和指数代码。
- `data.quote` 为 `null` 或 `data.warnings` 非空时，向用户说明实时价缺失、当前用的是最近收盘价（`last_close_date`）。`change_basis` 说明涨跌幅的计算口径。
- 已确认的黄金成交用 `portfolio trade-add`，代码 `Au99.99` / `AU9999` 等价，输出统一为 `AU9999`；使用 `cn` / `CNY`，`quantity` 是克，`price` 是元/克，费用单独记录。不要按手数、盎司或 ETF 份额填入，也不要把黄金 ETF 合并到现货黄金。
- 黄金持仓输出 `asset_type=gold`、`quantity_unit=g`、`price_unit=CNY/g`。当日默认 `portfolio snapshot` 会查询金价并缓存日线；历史 `--as-of` / `--no-realtime` 只使用缓存，独立 `gold` 查询不会写缓存。
- `price_available=false` 表示缺价，不能把占位市值 0 解释为黄金价值归零，不能据此下完整仓位或收益结论；`price_stale=true` 时注明 `price_date`。`gold_requires_cn_cny` 表示旧流水币种或市场不兼容，先向用户核对，不自行换算或重写。
- 账本以 Au99.99 作为估值基准；银行积存金、实物金回购价及工费可能不同，向用户明确所用基准。

## 每日例行流程

1. `watchlist list`、`watchlist history` 和 `portfolio snapshot`，读取当前关注、上轮待验证候选及持仓，保留关注的 `version`。持仓保留数量、单位、成本、浮盈、市值和价格可用性。
2. `market-review --data-only` 获取相关市场行情；`news --refresh` 获取新闻，核对 `published_at`、来源和刷新失败项。`days` 是抓取时间窗口，不代表旧新闻重新发布。没有配置源、新闻不足或刷新失败时，用自己的联网能力补查公告、行业和宏观新闻；记录链接及事件时间，查不到则明确缺失。
3. **先分析新闻，再产生临时候选**：说明事件如何影响公司，核实代码与公司对应关系，排除重复旧消息和缺乏关联依据的标的。候选不是正式关注，不因出现一条新闻就立即添加。
4. 合并“持仓 ∪ 现有关注 ∪ 本轮候选”，去重后深入验证行情、公司新闻及风险。股票 / ETF 走 `context` → 自己分析 → `analyze --responses`（每批不超过 5 只）；黄金通过 `gold` 单独分析。即使没有新候选，也要继续跟踪持仓与现有关注。
5. `portfolio risk` 检查集中度、回撤与止损；结合真实持仓给出个性化判断，区分 DSA 通用结论与自己的持仓调整。
6. 写出下面格式的复查 JSON。只有完成验证的对象才标记 `complete`；缺数据或分析失败记为 `incomplete`。关注价值与立即买入不同：可以关注有催化但尚未满足买入条件的标的。移除应说明原关注理由为何失效，不能因今天没有新闻或一次获取失败而移除。
7. 用户已授权自动维护关注时，执行 `watchlist apply`；普通分析请求只报告拟议变化。`watchlist_conflict` / `config_conflict` 时重新读取、复核，使用新 `run_id`，不能仅替换版本盲目重试。同一请求重试复用原文件和 `run_id`，`already_recorded=true` 表示已经提交；返回的是原轮结果，继续操作前重新读取列表。
8. 汇总市场判断、持仓建议、风险、新关注与移除理由、待验证候选和未覆盖项。关注变化不是成交，不据此记账。

## 候选与关注复查格式

```json
{
  "run_id": "daily-20261116-1",
  "version": "替换为本轮 watchlist list 返回的 version",
  "news_summary": "已核对事件时间和来源；记录缺失的数据及覆盖范围",
  "decisions": [
    {
      "symbol": "AAPL",
      "action": "add",
      "evaluation": "complete",
      "reason": "替换为事件关联依据、验证结论及等待条件",
      "evidence": ["https://example.com/replace-with-real-source"],
      "review_after": "2026-11-20"
    }
  ]
}
```

- `action`：`add` 加入关注、`keep` 更新自动关注理由、`remove` 移除自动关注、`reject` 否决候选、`defer` 留待验证。`reject/defer` 只记录，不改变列表；遗漏的标的不变。`review_after` 可省略，是下次复查日期，不会到期自动删除。
- `evaluation=incomplete` 的决定只留记录、不改变关注；新增自动关注必须带真实来源链接。候选结论与外部新闻证据保存在复查历史中。
- 手动添加（包括 Web 添加）默认受保护；手动移除后禁止自动重新添加，直到用户明确重新添加。`watchlist add/remove` 仅用于执行用户明确指令，不用于绕过自动维护限制。
- 自动关注默认最多 20 个，自动移除后 3 天内不重新添加；可按用户约定传入 `--max-auto-items` / `--cooldown-days`。检查返回 `changes[].applied` 及未应用原因，不把跳过项报告成成功。
- `watchlist add Au99.99` 可关注未买入的黄金；`gold_codes` 和黄金持仓都走 `gold`，不传给股票分析。现有 Web 自选页面仍只展示股票。

## 硬规则

- **没有用户明确确认的成交，不得调用 `trade-add`、`cash-add` 或 `account-create`。** 分析建议不是成交；用户说"想买"也不是成交。只有用户给出（或确认）实际成交的方向、数量、价格、日期后，才可记账。
- `trade-uid` 用 `券商-成交日期-代码-方向-序号` 生成，例如 `futu-20261113-600519-buy-1`。同一笔成交重试时必须复用同一个 uid。
- 返回 `already_recorded` 表示这笔已经记过，不要换 uid 重写；如用户确认是另一笔成交，才用新的序号。
- 返回 `portfolio_oversell`、`validation_error` 时如实告诉用户，不要自行改数量或价格。
- `cash-add` 没有去重，执行前先用 `portfolio snapshot` 核对现金，避免重复记录。
- 返回 `agent_pipeline_unsupported` 时，说明 DSA 开启了 `AGENT_MODE` 或 `AGENT_SKILLS`，请用户关闭后再用交接流程，不要自行修改 `.env`。
- 不直接修改、删除 DSA 的数据库和 `.env`；通过上述 CLI 管理关注和记账，不尝试代替用户下单。
