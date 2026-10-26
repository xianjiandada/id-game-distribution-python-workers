# id-game-distribution-python-workers

可扩展的 Python Worker 服务，负责承接需要 Python 运行时的素材抓取、网页自动化和数据处理任务。

## 当前能力

- FastAPI 内部任务 API：提交任务后立即返回 `jobId`。
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
uvicorn app.main:app --reload --host 127.0.0.1 --port 8091
```

任务完成后，Python 会把运行审计和素材结果写入 `data/worker.db`；Java Manager 回调成功后再写入 Java 主库的 `creative_run` 和 `creative_item`。

提交一个 Mock 任务：

```powershell
Invoke-RestMethod http://127.0.0.1:8091/jobs -Method Post -ContentType 'application/json' -Body (@{
  worker = 'mock'
  runId = 10001
  payload = @{ items = @(@{ externalAdId = 'demo-1'; copyText = 'demo' }) }
} | ConvertTo-Json -Depth 8)
```

## 两个平台快速烟测

不启动 FastAPI 也可以直接执行固定查询词的小规模真实网页测试。脚本默认每个平台最多抓取 3 条，成功时退出码为 `0`，遇到验证码、页面变化或没有结果时会打印错误并返回非 `0`：

```powershell
python scripts/live_smoke_test.py
```

固定查询为 Meta 的“螺丝”（`country=ALL`）和 Google 的 `SOFISH TECHNOLOGY PTE. LTD.`（`region=SG`）。需要代理时，先配置逻辑 Profile，再传入 Profile 名称：

```powershell
$env:PROXY_PROFILE_GOOGLE_HK = 'socks5://user:password@proxy.example:1080'
python scripts/live_smoke_test.py --max-results 3 --proxy-profile google_hk
```

更完整的架构、接口、开发和上线说明见 [`docs/`](D:/IDEAWorkSpace/learn/id-game-distribution-python-workers/docs/)。
