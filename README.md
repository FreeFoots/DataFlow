# DataFlow

面向短视频运营人员的自然语言数据分析应用。Vue 工作台对接 FastAPI 与 LangGraph，完成问题路由、字段检索、受限 SQL 查询和结果展示。

此仓库保留应用源码、依赖锁文件、测试、合成数据生成器及必要的架构说明。环境文件、密钥、本机工作记录、详细内部文档、数据集、向量索引、会话归档和测试报告不进入版本控制。

## 架构

```text
Vue 工作台 → FastAPI / 认证与角色 → LangGraph 工作流
                                   ├─ 问题预处理与意图路由
                                   ├─ 字段检索：BM25 / Embedding / RRF / Rerank
                                   ├─ Schema 关系图与查询智能体
                                   └─ 进程内 MCP 工具 → DuckDB → CSV 视图
结果与截断状态 → 表格 / 图表 / 报告 / 会话上下文
```

模块边界与可靠性限制见 [ARCHITECTURE.md](ARCHITECTURE.md)。`backend/app/skills/*/SKILL.md` 是应用运行所需的模型提示，随源码保留。

## 本地启动

需要 Python 3.11 和 Node.js 20.19+。先为项目建立独立 Python 环境并激活，再从项目根目录执行：

```bash
cd backend
python -m pip install -r requirements.lock.txt
python scripts/generate_short_video_ops_data.py
python scripts/generate_short_video_ops_schema.py
```

生成器使用固定种子生成演示 CSV 和 Schema，不调用模型接口。首次安装必须按上述顺序生成；数据位于 `backend/data`，不上传。生成器会写入这些路径，仅在新安装或明确需要重新生成演示数据时运行。

在本机通过环境变量或自行创建的 `backend/.env` 提供配置。不要将真实凭据写入源码或提交到 Git：

| 配置 | 用途 |
| --- | --- |
| `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` | 聊天模型 |
| `EMBEDDING_API_KEY` / `EMBEDDING_BASE_URL` / `EMBEDDING_MODEL` | 字段向量检索 |
| `EMBEDDING_DIMENSIONS` | 向量维度，与模型输出一致 |
| `RERANK_API_KEY` / `RERANK_BASE_URL` / `RERANK_MODEL` | 检索重排 |
| `SERVER_HOST` / `SERVER_PORT` / `CORS_ORIGINS` | 服务监听与跨域 |
| `VITE_DEV_API_TARGET` / `VITE_API_BASE` | 前端代理或独立部署 API 地址 |

供应商默认值和其他可选配置以 `backend/app/config.py` 为准；跨供应商时分别设置密钥。完整问数需要可用的聊天、Embedding 和 Rerank 服务。缺少模型配置时不能完成完整问数；离线测试不需要真实凭据。配置完成后可在 `backend` 运行 `python scripts/prepare_schema_index.py` 建立字段索引，此步骤会调用所配置的 Embedding 服务。

两个终端分别启动：

```bash
# 终端一，从项目根目录执行
cd backend
python run.py
```

```bash
# 终端二，从项目根目录执行
cd frontend
npm ci --ignore-scripts
npm run dev
```

默认前端地址 `http://127.0.0.1:5174`，后端 `http://127.0.0.1:8003`。默认监听本机回环地址。

## 验证

生成演示数据后运行：

```bash
cd backend
python -m unittest discover -s tests -p 'test_*.py'
python scripts/validate_short_video_ops.py
```

```bash
cd frontend
npm run build -- --emptyOutDir=false
```

`backend/evaluation` 保留评测代码及合成数据题集，不包含历史结果。模型评测需要显式配置模型服务，可能产生费用。离线测试与标准 SQL 验证不能代表真实模型答题正确率。

## 当前边界

当前版本用于演示和开发，包含固定的演示账号与口令，见 `backend/app/security/auth.py` 和登录页；它们不是真实业务凭据。认证令牌和任务状态主要存于进程内，服务重启后会失效或丢失。部署给真实用户前需要正式认证、任务持久化和业务指标校验。

查询结果最多展示 200 行，截断状态随结果传递；预览导出不等于全量导出。当前仍由模型生成查询，尚未实现确定性业务指标编译器。
