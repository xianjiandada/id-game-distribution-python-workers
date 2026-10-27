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

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
python -m app
```

`python -m app` 读取项目 `.env`，默认监听 `127.0.0.1:13371`，系统环境变量优先。Manager 回调地址为 `http://127.0.0.1:13367/internal/creative/runs`。Java 提交任务时指定的 `callbackUrl` 优先于 Worker 的默认回调配置。需要自动重载时，可运行 `uvicorn app.main:app --env-file .env --reload --host 127.0.0.1 --port 13371`。

任务完成后，Python 会把运行审计和素材结果写入 `data/worker.db`；Java Manager 回调成功后再写入 Java 主库的 `creative_run` 和 `creative_item`。

任务参数中的 `payload.maxResults` 用于控制每个搜索词的拉取数量：传入 `1000` 表示最多拉取 1000 条，传入 `0`、空值或不传表示不限制。不限数量时 Worker 会滚动公开结果页，直到页面不再加载新素材；平台未公开或需要验证的结果仍会返回人工处理状态。

提交一个 Mock 任务：

```powershell
Invoke-RestMethod http://127.0.0.1:13371/jobs -Method Post -ContentType 'application/json' -Body (@{
  worker = 'mock'
  runID = 10001
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

Meta 默认使用 `country=ALL`，Google 默认使用 `region=SG`。需要代理时，先配置逻辑 Profile，再传入 Profile 名称：

```powershell
$env:PROXY_PROFILE_GOOGLE_HK = 'socks5://user:password@proxy.example:1080'
python scripts/live_smoke_test.py --max-results 3 --proxy-profile google_hk
```

更完整的架构、接口、开发和上线说明见 [`docs/`](D:/IDEAWorkSpace/learn/id-game-distribution-python-workers/docs/)。
