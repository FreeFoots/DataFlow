# DataFlow

面向短视频运营人员的自然语言数据分析工具。无需编写 SQL，通过提问完成用户增长、渠道投放和内容表现分析，并查看表格、图表或分析报告。

## 用途

- 查询和对比业务数据，例如“按月统计新增用户数”“哪个渠道的投放效果最好”。
- 基于已有结果继续追问，例如“只看 4 月”，或生成图表和报告。
- 按运营角色限制数据访问；内置合成演示数据，方便本地体验。

## 基本技术

- **前端**：Vue 3、TypeScript、Vite。
- **后端**：Python、FastAPI、LangGraph。
- **数据查询**：DuckDB、SQLGlot，使用 CSV 演示数据。
- **Agent 能力**：聊天模型、Embedding、Rerank 和进程内 MCP 工具。

## Agent 处理流程

```text
用户提问 → 理解意图与会话上下文 → 检索相关表和字段
        → 生成查询并调用工具 → 校验权限、执行 SQL
        → 整理结果 → 展示表格、图表或报告
```

普通问题可直接回复；基于已有结果的分析进入数据问答流程。查询中需要补充信息时，Agent 会请求用户澄清，再继续处理。字段检索结合关键词、向量和重排，查询通过 MCP 数据库工具执行。

选择“深入分析”后，分析智能体会保存步骤计划，根据工具反馈继续查询、比较或澄清，最后引用实际结果生成数值事实和图表。当前受控指标支持新增注册用户与注册队列激活率，可按渠道、日或月查询；激活率必须明确注册后的观察天数。指标采用演示定义，尚未覆盖所有业务问题。自动模式由模型选择流程，也可选择“直接问数”。

分析复用持久任务与 checkpoint，默认最多 24 次决策、16 次工具调用；连续无进展时停止，取消或达到预算后保留已取得的数据依据。范围说明与尚未回答的事项分别显示，缺失证据时返回“部分完成”。

需求理解与澄清由独立组件负责。直接问数和深入分析对已支持的指标共用口径与执行服务；后续解读受控结果时，文字、图表和报告导出引用同一份服务端结果资产。其他查询继续使用原 SQL 路径。

## 快速启动

需要 **Python 3.11**、**Node.js 20.19+**，以及可用的聊天、Embedding 和 Rerank 服务。以下命令适用于 macOS / Linux。

### 1. 安装后端并生成演示数据

```bash
git clone https://github.com/FreeFoots/DataFlow.git
cd DataFlow

# 将示例路径替换为自己的虚拟环境存放路径，放在项目目录之外
export DATAFLOW_VENV=/path/to/python-envs/dataflow
python3.11 -m venv "$DATAFLOW_VENV"
source "$DATAFLOW_VENV/bin/activate"

cd backend
python -m pip install -r requirements.lock.txt
python scripts/generate_short_video_ops_data.py
python scripts/generate_short_video_ops_schema.py
```

生成步骤仅用于首次安装或重新生成演示数据，会写入 `backend/data`。

### 2. 配置模型

在本机创建 `backend/.env`，填入自己的密钥。下面是使用代码默认供应商的配置示例：

```dotenv
LLM_API_KEY=your_deepseek_key
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-flash

EMBEDDING_API_KEY=your_dashscope_key
EMBEDDING_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
EMBEDDING_MODEL=text-embedding-v4
EMBEDDING_DIMENSIONS=1024

RERANK_API_KEY=your_dashscope_key
RERANK_BASE_URL=https://dashscope.aliyuncs.com/compatible-api/v1
RERANK_MODEL=qwen3-rerank
```

也可换用兼容服务，调整对应地址、模型和向量维度。`.env` 已被 Git 忽略，请勿提交真实密钥。

### 3. 启动后端

在已激活 Python 环境的 `backend` 目录运行：

```bash
python -m scripts.prepare_schema_index
python run.py
```

索引准备会调用 Embedding 服务，可能产生费用。后端默认运行于 `http://127.0.0.1:8003`。

### 4. 启动前端

新开一个终端，从项目根目录执行：

```bash
cd frontend
npm ci --ignore-scripts
npm run dev
```

访问 **http://127.0.0.1:5174**，使用演示账号 `growth` / `growth123` 登录，即可开始提问。

当前版本用于本地演示和开发；采用演示认证，查询预览最多 200 行。更多实现说明见 [ARCHITECTURE.md](ARCHITECTURE.md)。

任务会先保存到本地 SQLite 运行时数据库，再由后台 worker 执行；刷新页面或服务重启后可以继续查看已提交任务。运行时数据库包含任务、事件、调用记录和 LangGraph checkpoint，可使用 `python -m scripts.backup_runtime /path/to/backup.db` 做在线备份。
