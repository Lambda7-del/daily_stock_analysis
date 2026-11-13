# ChatGPT dot / 外部 Agent 调用 DSA（Agent CLI）

本文说明如何让 ChatGPT dot 这类"自带电脑、可定时执行"的 Agent 主动调用 DSA：DSA 装在 Agent 的电脑上，Agent 通过 `python -m src.agent_cli` 拿到 JSON 结果，生成每日报告并维护持仓账本。

与 [Grok Bot 集成](grok-bot-integration.md) 的区别：Grok Bot 文档走已部署的 HTTP API；本文走本机命令行，不需要常驻服务、公网暴露或 API 鉴权。

## 能力边界

- Agent CLI 只包装现有服务：个股分析、大盘复盘、持仓账本（账户 / 成交 / 资金）、持仓快照与风险报告、决策信号查询。不新增数据库表、配置项或 HTTP 接口。
- 持仓账本只记录**已确认的成交**，DSA 不对接券商下单。持仓写操作必须带 `--confirmed`，成交必须带 `--trade-uid` 以便重试去重。
- `market-review` 默认不推送通知，结果由 Agent 自己转达；个股分析同样不推送。需要 DSA 自带的通知渠道时，仍可用 `python main.py`。
- Agent 工具调用（问股 Agent）、图片识别不在本入口范围内。

命令、输出格式和硬规则见 Skill 示例：[`docs/examples/codex_dot/SKILL.md`](examples/codex_dot/SKILL.md)。

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

二选一：

- **使用 ChatGPT 订阅（Codex CLI）**：在该电脑上安装 Codex CLI，执行 `codex login --device-auth` 登录，然后设置 `GENERATION_BACKEND=codex_cli` 与 `GENERATION_FALLBACK_BACKEND=codex_cli`。单只股票的生成通常需要数分钟，可调大 `GENERATION_BACKEND_TIMEOUT_SECONDS`。细节与限制见 [LLM 配置指南](LLM_CONFIG_GUIDE.md)。
- **使用 API Key**：按 [LLM 配置指南](LLM_CONFIG_GUIDE.md) 配置任一渠道。

已知限制：本地 CLI backend 的子进程环境只透传最小 allowlist，`HTTP(S)_PROXY` 不在其中。如果该电脑需要通过环境变量代理才能访问 OpenAI，`codex_cli` 会以非零退出失败（诊断可能被归类为 `approval_required`）；海外云电脑通常不受影响。

### 3. 数据源

海外机房访问东方财富 / akshare 部分接口可能被断开，DSA 会自动回退，但筹码、板块等字段可能缺失。A 股建议配置 `TICKPLUS_API_KEY` 或 `TUSHARE_TOKEN`，见 [数据源配置](full-guide.md#数据源配置)。新闻质量取决于搜索服务配置，见 [搜索服务配置](full-guide.md#搜索服务配置)。

### 4. 首次验证

```bash
cd ~/dsa-data/daily_stock_analysis
PY=~/dsa-data/venv/bin/python
$PY -m src.agent_cli portfolio accounts
$PY -m src.agent_cli analyze --stocks 600519
```

两条命令的 stdout 都应是 `"ok": true` 的 JSON；第二条的 `data.report_path` 指向生成的 Markdown 报告。

### 5. 交给 dot

1. 把 [`docs/examples/codex_dot/SKILL.md`](examples/codex_dot/SKILL.md) 作为 dot 的 Skill，并告诉它 `$DSA_HOME` 与 `$DSA_PY` 的实际路径。
2. 首次让 dot 在你确认后创建持仓账户，并录入当前持仓（逐笔 `trade-add`，或在 Web 端用 CSV 导入后再交给 dot 维护）。
3. 创建定时任务，例如"工作日 18:00（Asia/Shanghai）按 Skill 的每日例行流程执行，把汇总发给我；不要在未经我确认时记录任何成交"，并让 dot 复述保存的计划。

## 运行注意

- SQLite 是单写者。dot 任务执行期间不要同时在 Web 端修改持仓，否则可能返回 `portfolio_busy`。
- 报告文件名为 `reports/agent_report_<时间戳>.md`，不会覆盖 `main.py` 的 `report_<日期>.md`。
- `--no-realtime` 只用本地已存的历史收盘价计算市值；若本地还没有该股的日线数据，市值可能显示为 0。每日例行流程建议使用默认的实时行情，或先对持仓代码跑一次 `analyze` 以写入日线。

## 回滚

Agent CLI 不引入数据迁移。停用 dot 定时任务即可停止调用；持仓数据仍在 `DATABASE_PATH` 中，可继续通过 Web 端或 API 使用。
