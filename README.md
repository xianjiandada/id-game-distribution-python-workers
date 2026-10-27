# id-game-distribution-python-workers

可扩展的 Python Worker 服务，负责承接需要 Python 运行时的素材抓取、网页自动化和数据处理任务。

## 当前能力

- FastAPI 内部任务 API：提交任务后立即返回 `jobID`。
- Worker 注册和路由：`mock`、`meta_library`、`google_transparency`。
- 异步执行、任务状态查询、失败回调和运行结果回调。
- Meta Ads Library 公开网页抓取和 Google Ads Transparency 网页搜索抓取。
- Java Manager 回调契约，以及 Ubuntu + 宝塔面板的原生进程部署方案。

当前两个平台都先使用公开网页 + Playwright，不调用需要授权的官方 API。网页结构、验证码和访问限制可能变化，生产运行要保留 `WAITING_MANUAL` 和失败证据。

## 本地运行

**服务启动入口是 `app/__main__.py`，推荐统一用 `python -m app` 启动。** `app/main.py` 定义 FastAPI 应用，不要单独右键运行它；`app/workers/` 下的文件由服务分派任务，也不需要分别启动。

### 首次准备（Windows / IDEA 终端）

先进入项目根目录（包含 `requirements.txt` 的目录）。使用 Python 3.10 或以上版本；你的宝塔 Python 3.14.4 可以使用，建议直接选它，若依赖安装异常再回退 Python 3.12。下列命令不依赖 PowerShell 的虚拟环境激活权限。已有 `.venv` 时跳过创建，已有 `.env` 时保留原配置：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
if (!(Test-Path .env)) { Copy-Item .env.example .env }
# Meta 和 Google 真实网页采集都需要 Chromium；仅启动 API / Mock 时可跳过。
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe -m app
```

后文中的 `python` 命令均使用同一个虚拟环境解释器；在未激活环境的 PowerShell 中，将 `python` 替换成 `.\.venv\Scripts\python.exe`。

### IDEA 运行 / 调试配置

在 IDEA 的 **Run → Edit Configurations（运行 → 编辑配置）→ + → Python** 中新增配置：

| 配置项 | 填写内容 |
| --- | --- |
| 名称 | `Python Workers` |
| 运行目标类型 | **Module name（模块名称）**，不要选 Script path |
| 模块名称 | `app`（不是 `app.main`，不需要再填 `-m`） |
| Python interpreter / SDK | 项目根目录下的 `.venv\Scripts\python.exe` |
| Working directory（工作目录） | `D:\IDEAWorkSpace\learn\id-game-distribution-python-workers`；移动项目后改成新根目录 |
| Parameters（参数） | 留空 |
| 环境变量 | 通常留空，入口自动读取项目 `.env`；这里设置的同名变量会覆盖 `.env` |
| Add content roots to PYTHONPATH | 保持勾选，让项目根目录可被导入 |

保存后选中 `Python Workers`，点击运行或调试即可。截图中选中的 `app/__main__.py` 是正确的启动文件，但右键自动创建的脚本配置可能把工作目录设成 `app` 子目录；建议按上表改为模块启动，避免 `No module named 'app'` 和数据路径不一致。

如果没有 Python 配置类型，先确认当前 IDEA 版本支持并已启用 Python 插件，再添加上述解释器。也可以直接在 IDEA 终端运行前面的命令。

### 启动检查与 Java 联调

`python -m app` 读取项目 `.env`，默认监听 `127.0.0.1:13371`，系统环境变量优先。启动后保持进程运行，在另一个终端检查：

```powershell
Invoke-RestMethod http://127.0.0.1:13371/health
Invoke-RestMethod http://127.0.0.1:13371/workers
```

健康检查应返回 `status: UP`，Worker 列表应包含 `mock`、`meta_library` 和 `google_transparency`。接口调试页：<http://127.0.0.1:13371/docs>。`/health` 只说明 API 存活，不代表 Chromium、代理或 Java 回调已经验证。

**单独启动 Python 服务不需要先启动 Java、MySQL 或 Redis**，本服务使用 SQLite。Java 联调复用 Java 项目现有测试数据库和依赖，不另建 MySQL / Redis，也不使用旧 `docker-compose.local.yml` 覆盖 Java 配置。完整 CMS 验收还需要 Quartz，步骤见[本地联调与验收](docs/本地验收.md)。默认 Manager 回调地址为 `http://127.0.0.1:13367/internal/creative/runs`，任务指定的 `callbackUrl` 优先。下方 Mock 不传 `runID` / `callbackUrl`，仅验证 Python。

需要自动重载时，在项目根目录运行（仅用于开发，重载会丢失内存任务）：

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --env-file .env --reload --host 127.0.0.1 --port 13371
```

### 常见启动问题

| 现象 | 处理 |
| --- | --- |
| `No module named 'app'` | 按上表使用模块 `app`，工作目录设为项目根目录 |
| `attempted relative import with no known parent package` | 不要直接运行 `app/main.py`，改用 `python -m app` |
| 缺少 `uvicorn`、`fastapi`、`dotenv` 等模块 | 用 IDEA 选中的同一个解释器执行 `-m pip install -r requirements.txt` |
| Playwright 提示浏览器 executable 不存在 | 用同一个解释器执行 `-m playwright install chromium` |
| 端口占用 / `WinError 10048` | 停止之前的 Worker 实例，或修改 `.env` 的 `PORT`，并同步调用地址 |
| `CALLBACK_FAILED` | 检查 Java 是否启动、回调地址和网络；服务健康检查成功不代表回调成功 |

任务完成后，Python 将运行审计和最新素材写入 `DATABASE_PATH`（默认 `data/worker.db`），相同 `media + externalAdID` 只保留一条，素材上的查询上下文随最新结果更新。正常 Java 链路由 Quartz 先创建 `advert_creative_run`，Manager 收到回调后更新运行状态、upsert 素材；Python 抓取成功不代表 Java 入库成功。

任务参数 `payload.maxResults` 为结果条数上限；`0`、空值或不传表示不设条数上限，但仍受最多 100 轮滚动、连续 3 轮卡片数量不增长及任务超时限制，不保证拿到平台全部素材。验证挑战可能返回 `WAITING_MANUAL`，无结果或其他异常也可能返回 `FAILED`。

提交一个 Mock 任务：

```powershell
Invoke-RestMethod http://127.0.0.1:13371/jobs -Method Post -ContentType 'application/json' -Body (@{
  worker = 'mock'
  payload = @{ items = @(@{ externalAdID = 'demo-1'; copyText = 'demo' }) }
} | ConvertTo-Json -Depth 8)
```

## 两个平台快速烟测

不启动 FastAPI 也可以直接执行小规模真实网页测试。脚本默认使用 Meta“螺丝”和 Google `SOFISH TECHNOLOGY PTE. LTD.`，每个平台最多抓取 3 条；成功时退出码为 `0`，遇到验证码、页面变化或没有结果时会打印错误并返回非 `0`：

```powershell
python scripts/live_smoke_test.py
```

也可以传入其他词语和竞品公司名称进行模拟：

```powershell
python scripts/live_smoke_test.py --meta-keyword "手机壳" --google-advertiser "Supercell Oy" --google-region HK --max-results 2
```

测试不限数量的公开结果：

```powershell
python scripts/live_smoke_test.py --max-results 0
```

烟测脚本默认使用 Meta `country=ALL`、Google `region=SG`；普通 API 任务的 Google 地区缺省为 `ANYWHERE`。脚本不自动读取 `.env`，也不写 SQLite 或回调 Java。需要代理时，在当前终端设置环境变量，再传逻辑 Profile 名称；若配置了自定义 `PLAYWRIGHT_BROWSERS_PATH`，也需在该终端设置：

```powershell
$env:PROXY_PROFILE_GOOGLE_HK = 'socks5://user:password@proxy.example:1080'
python scripts/live_smoke_test.py --max-results 3 --proxy-profile google_hk
```

## 线上运行（Ubuntu + 宝塔）

**不需要编译打包，直接上传源码即可；压缩成 ZIP 上传解压也可以。** 必需文件是 `app/`、`requirements.txt` 和项目根目录的 `production.env`。当前仓库提供的 `production.env` 不含真实密码或代理凭据，可以随代码上传；不要上传本地 `.venv`、`.idea`、缓存或 `data/`。服务器仍需单独安装依赖和 Chromium。

首次发布，直接上传项目根目录的 `production.env`，核对回调地址和共享目录后使用生产命令；后续发布更新代码时一并上传该文件即可。若在其中填写真实密码或代理凭据，应改为服务器单独管理并从 Git 提交中移除。本地启动仍读取 `.env`：

```text
本地：python -m app
生产：python -m app --env-file production.env
```

指定生产文件不存在时会报错，不会回退使用 `.env`；系统 / 面板环境变量优先于文件。

已有按宝塔截图编写的[部署上线说明](docs/部署上线.md)。在**网站 → Python 项目 → 添加项目**中选择“命令行启动”，由该面板管理启停、重启和日志，使用一个进程。

以下为文档约定目录下的关键配置：

```text
Python 环境：独立的 Python 3.14.4 环境（不选宝塔 pyenv 3.7；异常时可用 3.12）
启动方式：命令行启动
启动用户：www
项目路径：/data/opt/game/server/id-game-distribution-python-workers
启动命令：python -m app --env-file production.env（python 推荐替换为所选环境的实际解释器绝对路径）
安装依赖包：/data/opt/game/server/id-game-distribution-python-workers/requirements.txt
环境变量：无（入口加载指定的 production.env）
项目初始化命令：留空
```

按部署文档准备 `production.env`、共享数据库路径并安装 Chromium，创建后核实项目实际解释器、启动命令及自启动 / 自动重启设置。检查 `/health`，再验证 Java 回调。线上不要加 `--reload` 或开启多进程，也不要同时用 Supervisor 管理同一个服务。

Chromium 使用项目解释器在宝塔**终端**中通过 Playwright 安装，**不用安装软件商店的 `chromium（Docker应用）`**。组件区别、安装命令和验证步骤见[部署文档第 5 节](docs/部署上线.md#5-在宝塔终端安装-chromium)。

其他文档：[架构设计](docs/架构设计.md)、[接口契约](docs/接口契约.md)、[本地验收](docs/本地验收.md)、[部署上线](docs/部署上线.md)。
