# i2stream_backend 结构说明

## 范围

这个目录是 Hermes WebUI 内置的 50091 兼容后端，负责给前端扩展和 node_backend
提供稳定 API，并通过 Gateway Bridge 把插件请求交给 Hermes Agent。它不提供新的可见管理界面；
用户界面由父目录 Hermes WebUI 的 8787 服务承担。

## 文件结构

- `main.py`：FastAPI 应用入口与路由定义，对外 API 路径稳定。拆分出的辅助逻辑见 `file_state`/`file_store`/`sse_proxy`。
- `file_state.py`：文件/inbox 注册表、inbox 锁与 MEDIA 标记正则，全局状态根。
- `file_store.py`：文件注册、查找、删除、inbox 上传、元数据读写与 URL 构造。
- `sse_proxy.py`：SSE 代理、payload 构造、流式转发与出站 HTTP。
- `config.py`：环境变量配置和 `settings` 单例。不要在其他文件重新实例化 `Settings`。
- `models.py`：Pydantic 请求/响应和内部数据模型，例如 `CommunicateRequest`、`FileRecord`、`ChatMessageRecord`。
- `auth.py`：代理鉴权、Hermes API key 检查，以及 client/request/report token 等输入校验。
- `db.py`：SQLite 连接和聊天表 schema 初始化。这里不放具体业务读写逻辑。
- `chat_store.py`：聊天客户端 ID、聊天消息写入、历史消息读取、可见历史清空。
- `progress.py`：Agent 执行进度状态、进度事件记录，以及工具调用进度摘要。
- `knowledge_store.py`：知识库上传校验和 vector service 转发；不保存文件、不写 SQLite、不注入 metadata。
- `dashboard_page.py`：Backend 承载的 Dashboard HTML，包含知识库上传、Report 管理和对话历史查询视图。
- `test_report_processing.py`：report token 相关行为测试，同时覆盖用户消息 payload 持久化。

## 修改约束

- 不要把内部文件路径返回给前端；报告路径只能在服务端补到发给 Agent 的 prompt 里。
- 对外 Backend API 使用 RESTful 资源路径；新增前端调用应优先复用 `/api/agent/*`、`/api/conversations/*`、`/api/chat-clients/*`、`/api/reports/*`、`/api/generated-files/*`、`/api/knowledge/*`。
- 涉及 `.env`、token、密码时只查看 key 结构，不读取密钥值。
- 修改后至少运行：

```bash
UV_CACHE_DIR=/tmp/uv-cache PYTHONPYCACHEPREFIX=/tmp/agent-console-pycache uv run python -m unittest discover -v
```
