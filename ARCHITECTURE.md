# DataFlow 架构

## 模块

| 路径 | 职责 |
| --- | --- |
| `frontend/src` | 登录、会话、字段确认、表格、图表与报告 |
| `backend/app/api` | FastAPI 路由、认证、任务与会话接口 |
| `backend/app/services` | 请求协调、记忆、会话上下文及可选归档 |
| `backend/app/workflows` | LangGraph 状态、节点调度和结果整理 |
| `backend/app/preprocessing.py` | 请求预处理与意图路由 |
| `backend/app/retrieval` | 字段索引、混合检索与 Schema 关系图 |
| `backend/app/querying` | 数据问答、查询智能体、SQL 执行与解释 |
| `backend/app/mcp_runtime` | 进程内工具客户端、服务及数据库/时间/展示工具 |
| `backend/app/security` | 演示认证与表级角色授权 |
| `backend/app/skills` | 运行时模型提示与能力配置 |
| `backend/app/model_client.py` | 聊天、Embedding、Rerank HTTP 客户端 |
| `backend/scripts` | 合成数据、Schema、索引准备与数据校验 |
| `backend/tests` / `backend/evaluation` | 离线回归、评测比较器与合成业务题集 |

## 请求流程

1. 前端携带认证令牌向 API 发起请求，服务关联用户、角色、会话及任务。
2. 预处理结合最近会话，将请求路由到数据库查询、数据问答或直接回复。
3. 数据库查询通过 BM25 与向量检索召回字段，RRF 融合后重排，并使用 Schema 关联补充上下文；需要时由用户确认字段。
4. 智能体使用运行时技能提示调用 MCP 工具。数据库工具通过 SQLGlot 校验查询来源和函数，只在 DuckDB 中注册角色允许访问的 CSV 表。
5. 工作流整理查询结果、解释与展示信息，传递截断标记并更新会话上下文。

## 数据与状态

演示数据覆盖用户增长、渠道投放、内容运营，由源码中的固定种子生成器产生。CSV、Schema 和字段向量索引均为本地衍生文件，仓库只保存其生成逻辑。

任务和 LangGraph checkpoint 目前使用进程内状态。会话归档可按配置启用，归档记录以用户归属限制访问；这不等同于持久任务调度和重启恢复。模型客户端的超时、重试与无效响应检查提供有限的故障处理，无法保证模型生成的业务口径正确。

## 可靠性边界

SQL 校验限制表级范围、外部数据读取和函数；这是查询防护的一部分，仍需配合正式身份系统、部署隔离和业务语义校验。可展示结果最多 200 行，前端保留截断提示。当前没有全量结果导出、确定性指标编译或生产级持久任务恢复。

## 仓库边界

根目录 README 与本文件是仓库内必要说明。详细内部 `docs`、环境配置、本机 AGNETS 工作记录、所有 `backend/data`、历史评测结果、依赖目录和构建产物均留在本机。后端技能提示属于运行代码的一部分，不属于内部文档。
