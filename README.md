# 替身 (Shuangshen) — AI 数字替身平台

用 AI 模拟真实人物的说话风格与个性，让亲友能与你的"数字替身"自然对话。

---

## 快速启动

### 1. 环境准备

需要 Python 3.10+，进入项目目录后安装依赖：

```bash
cd shuangshen
pip3 install flask python-dotenv PyJWT
```

### 2. 配置环境变量

```bash
cp .env.example .env
```

打开 `.env`，确认以下配置（默认已设置 DeepSeek）：

```
LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=你的Key
```

### 3. 初始化数据库

首次运行前执行一次：

```bash
python3 scripts/init_db.py
```

### 4. 启动服务

```bash
python3 -m backend.app
```

服务启动后访问：
- API 服务：http://localhost:5000
- 前端页面：http://localhost:5000/app
- 健康检查：http://localhost:5000/

---

## 停止服务

在终端按 `Ctrl + C` 即可停止。

如果在后台运行，找到进程后 kill：

```bash
# 查找占用 5000 端口的进程
lsof -ti :5000

# 终止进程（替换 PID 为实际数字）
kill <PID>

# 或者一行搞定
kill $(lsof -ti :5000)
```

---

## 后台运行（可选）

如需关闭终端后保持运行：

```bash
# 后台启动，日志写入 server.log
nohup python3 -m backend.app > server.log 2>&1 &

# 查看日志
tail -f server.log

# 停止
kill $(lsof -ti :5000)
```

---

## 常用命令速查

| 操作 | 命令 |
|------|------|
| 启动服务 | `python3 -m backend.app` |
| 初始化数据库 | `python3 scripts/init_db.py` |
| 测试 DeepSeek Key | `python3 scripts/test_deepseek.py` |
| 查看服务是否运行 | `lsof -i :5000` |
| 停止服务 | `kill $(lsof -ti :5000)` |
| 查看实时日志 | `tail -f server.log`（后台运行时） |

---

## 切换 LLM 模型

编辑 `.env` 中的 `LLM_PROVIDER`，重启服务生效：

```bash
# 使用 DeepSeek（当前默认）
LLM_PROVIDER=deepseek

# 使用 Anthropic Claude
LLM_PROVIDER=anthropic

# 使用 OpenAI
LLM_PROVIDER=openai

# 使用本地模型（Ollama）
LLM_PROVIDER=local
```

---

## API 端点概览

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | /api/auth/register | 注册 |
| POST | /api/auth/login | 登录，返回 JWT |
| GET | /api/avatars | 我创建的替身列表 |
| POST | /api/avatars | 创建替身 |
| GET | /api/avatars/:id | 替身详情 |
| PUT | /api/avatars/:id | 更新替身 |
| POST | /api/avatars/:id/share | 获取共享码 |
| POST | /api/avatars/join/:code | 接收者绑定替身 |
| GET | /api/avatars/received | 我绑定的替身列表 |
| POST | /api/memories/:avatar_id | 添加记忆/观点 |
| GET | /api/memories/:avatar_id | 记忆列表 |
| POST | /api/ingest/:avatar_id/wechat | 上传微信聊天记录 |
| POST | /api/chat/:avatar_id | 与替身对话 |
| GET | /api/chat/:avatar_id/history | 对话历史 |
| POST | /api/avatars/:id/topics | 添加敏感话题预设 |
| GET | /api/avatars/:id/topics | 查看敏感话题预设 |

---

## 项目结构

```
shuangshen/
├── backend/
│   ├── app.py                  # Flask 入口
│   ├── core/config.py          # 配置（从 .env 读取）
│   ├── db/database.py          # SQLite 连接与 Schema
│   ├── api/routes/             # 路由层
│   │   ├── auth.py             # 注册/登录
│   │   ├── avatars.py          # 替身 CRUD
│   │   ├── memories.py         # 记忆管理
│   │   ├── chat.py             # 对话接口
│   │   ├── ingest.py           # 微信记录导入
│   │   └── topics.py           # 敏感话题预设
│   └── services/
│       ├── llm_provider.py     # LLM 接口（DeepSeek/Claude/OpenAI/本地）
│       ├── persona_builder.py  # 人格 Prompt 构建
│       ├── memory_store.py     # 记忆存取与检索
│       ├── wechat_parser.py    # 微信聊天记录解析
│       └── topic_guard.py      # 敏感话题拦截
├── frontend/index.html         # 前端页面
├── scripts/
│   ├── init_db.py              # 数据库初始化
│   └── test_deepseek.py        # DeepSeek API 连通测试
├── data/uploads/               # 上传文件目录
├── .env                        # 环境变量（不提交 Git）
└── .env.example                # 环境变量模板
```

---

## 技术栈

- **Web 框架**：Flask 3.x
- **数据库**：SQLite（开发）→ PostgreSQL（生产）
- **LLM**：DeepSeek（默认），可切换 Claude / OpenAI / 本地 Ollama
- **认证**：JWT (PyJWT)
- **解析**：微信聊天记录（.txt 格式）
