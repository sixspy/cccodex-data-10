# cc-data-lab

CC_Codex 二期（26.11）**数据工程**主线基线：**采集任务控制台**（Flask 后端 + HTMX 前端页 + 站内 Mock 目录）。

## 领域

- **所属领域**：数据工程  
- **子方向**：数据采集、爬虫与 RPA  

## 技术栈

- Python 3.12+、Flask 3、SQLite  
- HTMX（CDN）+ Jinja 模板  
- pytest  

## 本地运行

```powershell
cd claude-workspace\cc-data-lab
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pytest
python run.py
```

浏览器打开 http://127.0.0.1:5590 ，登录 **admin / 123456**。

流程：创建任务 → 选种子 `/mock/catalog/alpha` 或 `beta` → 卡片上点「运行采集」→ 查看落库（alpha 含重复标题，会演示去重跳过）。

## 喊停 / 续上

采集是**逐条推进**的，卡片上有游标「下一条编码」，账本条数随采集上涨。

| 操作 | 何时可点 | 效果 |
|------|----------|------|
| 运行采集 | 待运行（draft） | 进入采集中，游标指到目录第一条 |
| **喊停** | 采集中（running） | 状态变为**已停下**，账本条数当场冻结，卡片标出下一条未处理编码 |
| **续上** | 已停下（paused） | 只从停下时记下的那条编码往后收，已入账编码不会出现第二遍 |

- 不是已停下时，卡片**不画**「续上」按钮；后台同样回 409 拒绝。
- 已停下时另起一趟（`/start` 或 `/run`）会被拒绝，只能走「续上」。
- 续上只认 `crawl_jobs.resume_sku` 记录的那条编码：目录后来新增了前置条目也不会回头重收整本。
- 落库有 `UNIQUE(job_id, item_key)` 与 `UNIQUE(job_id, item_code)` 双保险，同一编码不会写成第二条。

## Docker 一键启动（推荐用于快照前验收）

需已打开 **Docker Desktop**：

```powershell
cd claude-workspace\cc-data-lab
docker compose up -d --build
docker compose ps
```

- 页面：http://127.0.0.1:5590/login  
- Mock：http://127.0.0.1:5590/mock/catalog/alpha  
- 停止：`docker compose down`（保留库卷）· 清卷：`docker compose down -v`

`docker compose ps` 中 `STATUS` 含 **healthy** 表示服务可访问。线下表「环境可复现等级」可填 **已容器化，可一键起环境**。

## 前后端说明

| 层 | 说明 |
|----|------|
| 后端 | `app/` 路由、任务状态机（逐条采集 / 喊停 / 续上）、SQLite |
| 前端 | `templates/` + `static/`，卡片 HTMX 自动刷新 |
| Mock 源站 | `/mock/catalog/<id>` HTML，无需外网 |

环境变量：`COLLECT_STEP_DELAY`（每步间隔秒数，默认 2.0）、`AUTO_COLLECT=0` 关闭后台驱动线程（测试用）。

## 初始快照

在首次 Claude 单轮跑题前 commit + push 本树（不含 `.venv/`、`instance/*.db`）。后续题目在同 repo 上叠加调度、RPA 步骤、增量水位等能力。

## 基线 vs 跑题目录

| 路径 | 角色 |
|------|------|
| `claude-workspace/cc-data-lab`（本目录） | **基线**：改功能、docker 验收 |
| `ccproject/cc-data-lab/data-XX` | **单题**：复制基线后挂容器、出快照 |

复制：`..\scripts\Copy-BaselineToQuestion.ps1 -QuestionId data-01`

## 跑题 Harness

见 `CCcodex/docs/ClaudeCode_Windows使用说明.md`（镜像 2.0）。容器挂载 **ccproject 下该题目录**，不是本基线路径。
