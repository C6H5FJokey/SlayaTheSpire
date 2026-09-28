# 10 部署

Laya 可以跑在**本机**，也可以跑在远端：agent 只看 `[laya].base_url`，两种部署完全等价。

## 本机部署（Windows）

一条命令把独立环境建好（`.venv-laya`，与 agent 的 `.venv` 分开 —— agent 只需要
`httpx`，没必要背上 torch）：

```powershell
powershell -File tools\setup_laya.ps1
```

它做三件事：

1. 建 `.venv-laya`。**优先用 `uv` 的独立 CPython 3.12**（没有 uv 才回退 `python -m venv`），
   见下面的"Anaconda 坑"；
2. 装 **CUDA 版 torch**。注意 PyPI 上 Windows 的 `torch` 轮子是 **CPU-only**（124 MB），
   CUDA 轮子只在 `download.pytorch.org`，所以脚本走
   `--index-url https://download.pytorch.org/whl/cu128`（可 `-Cuda cu130` 换版本）；
3. 把 `convaiinnovations/laya` 整仓拉到仓库内 `.cache\huggingface`（约 2.4 GB：三个
   checkpoint 都在同一个仓库里，之后切 checkpoint 不必再联网）。

起服务：

```powershell
powershell -File tools\serve_laya.ps1                       # 127.0.0.1:8000，只预加载 english
powershell -File tools\serve_laya.ps1 -Models english,multilingual
powershell -File tools\serve_laya.ps1 -ApiKey <key>         # 设了之后 agent 侧要填同一个 key
```

脚本固定 `LAYA_DEVICE=cuda`、`LAYA_PRELOAD=1`，且**只监听 loopback**。要跨机访问必须自己
改 `-Bind`，并且**一定**配上 `-ApiKey`。

上面这些脚本用系统自带的 **Windows PowerShell 5.1** 就能跑（`powershell -File ...`），不需要另装 PowerShell 7。
`.ps1` 带 UTF-8 BOM 正是为了这个：5.1 读**无 BOM** 的 .ps1 会按系统 ANSI（中文 Windows = GBK）解码，
中文注释会直接把字符串截断、报「字符串缺少终止符」；PowerShell 7 有 BOM 也照读。
若提示执行策略，用 `powershell -ExecutionPolicy Bypass -File ...`。

### 进程归属：谁起的归谁

服务归**启动它的那个终端**。在编辑器 / agent / 一次性脚本的 shell 里跑 `serve_laya.ps1`，那个
shell 一关（或它的进程树被回收），服务跟着一起消失 —— 表面现象就是"端口突然空出来了"。
要让它常驻，**自己开一个 PowerShell 窗口**执行 `serve_laya.ps1`：这样它归你，关别的东西不影响它。

`Ctrl-C` 能否送达也取决于启动方式：以 `CREATE_NEW_PROCESS_GROUP` 起的子进程不在当前控制台的
处理组里，`Ctrl-C` 根本不会派发给它。停不掉时按 PID 停：

```powershell
netstat -ano | Select-String ':8000'          # 最后一列是 PID
Get-Process -Id <pid> | Select-Object Path    # 确认是 .cache\uv-python\... 下的那个 python
Stop-Process -Id <pid> -Force
```

代理是这台机器的常见坑：脚本会先清掉 `PIP_NO_INDEX`，再依次取 `SLASPIRE_PROXY`、
注册表 `ProxyServer` 作为代理；要显式指定就 `-Proxy http://127.0.0.1:7897`。

### Anaconda 坑（必读）

**不要用 Anaconda 的 python 建 `.venv-laya`。** venv 的 `python.exe` 会去 `pyvenv.cfg` 指的
base 里加载 `python312.dll`；base 是 Anaconda 时，它会把 `C:\ProgramData\anaconda3` 下那份
**旧的** `VCRUNTIME140.dll`（98 KB，System32 是 123 KB）先载进进程。MSVC 运行库按"基名"复用，
于是 torch 的 `c10.dll` 只能挂在这个旧版本上，初始化失败：

```
OSError: [WinError 1114] 动态链接库(DLL)初始化例程失败。
Error loading "...\.venv-laya\Lib\site-packages\torch\lib\c10.dll" or one of its dependencies.
```

把 Anaconda 从 `PATH` 里删掉**不管用**（`python312.dll` 照样从 Anaconda 目录来）。
`tools\setup_laya.ps1` 因此优先用 `uv venv --python-preference only-managed`，让解释器和运行库
都来自独立 CPython。

## 远端要求

| 项 | 要求 |
|---|---|
| GPU | >= 8 GB 显存（可让 421M 的 `english` checkpoint 常驻） |
| Python | >= 3.10（`laya 0.3.20` 要求 `torch>=2.0`、`transformers>=4.48`） |
| 内存 | >= 8 GB（模型 + 服务） |
| 网络 | agent 能访问到该机器的 `base_url`；**v1 不要求公网**，内网/VPN 即可 |
| 端口 | 一个 HTTP 端口（默认 8000） |

v1 **不做 TLS**：要么在内网/VPN 里跑，要么前置反向代理做 TLS。`Authorization: Bearer` 仍然必须启用——它是防误用的最低要求，不是安全边界。

## 手动安装（Linux / 远端）

```bash
python3 -m venv .venv-laya
. .venv-laya/bin/activate
pip install torch --index-url https://download.pytorch.org/whl/cu128
pip install "laya[serve]"
```

checkpoint 第一次用到时会自动下到 `~/.cache/huggingface`。想提前拉全：

```bash
python -c "from huggingface_hub import snapshot_download; snapshot_download('convaiinnovations/laya')"
```

## 启动

```bash
export LAYA_HOST=0.0.0.0
export LAYA_PORT=8000
export LAYA_DEVICE=cuda
export LAYA_PRELOAD=1
export LAYA_MODELS=english
export LAYA_API_KEY="$(openssl rand -hex 24)"
export LAYA_LOG_LEVEL=info
laya-serve            # 等价于 python -m laya.serve
```

## 环境变量

| 变量 | 含义 | 默认 | 本项目建议 |
|---|---|---|---|
| `LAYA_HOST` | 绑定地址 | `0.0.0.0` | `0.0.0.0`（内网） |
| `LAYA_PORT` | 端口 | `8000` | `8000` |
| `LAYA_DEVICE` | torch 设备 | auto | `cuda` |
| `LAYA_PRELOAD` | 启动时即加载 checkpoint | `1` | `1`（避免第一步决策等几十秒） |
| `LAYA_MODELS` | 预加载哪些 | 全部 | `english,multilingual,typed-decisions` |
| `LAYA_THREADS` | CPU 推理的 torch 线程数 | torch 默认 | GPU 部署不需要；纯 CPU 时设 <= 物理核数，**不要超过物理核** |
| `LAYA_AUTO_TASK` | 自动路由到 typed-decisions | `0` | `0`（我们显式指定 shell checkpoint） |
| `LAYA_API_KEY` | 设置后要求 Bearer | 无 | **必须设置** |
| `LAYA_LOG_LEVEL` | uvicorn 日志级别 | `info` | `info` |

没有并发上限这个旋钮：`laya-serve` 用**单 worker 线程池 + 一把锁**串行跑推理，请求只会排队，
不会 503。单 agent 本来就是串行的，正好。

## Docker

Laya 仓库自带 compose 文件（`compose.yaml`、`compose.http.yaml`、`compose.cuda.yaml`、`compose.spark.yaml`）。GPU 用 `compose.cuda.yaml`：

```bash
docker compose -f compose.cuda.yaml up -d
```

注意：容器首次启动会下载 checkpoint（几百 MB 到 ~1.7 GB），要在健康检查前留出时间。

## 健康检查

```bash
# 在本仓库根目录跑（先 powershell -File tools\setup_dev.ps1 建好 venv）
.\.venv\Scripts\python.exe tools\laya_health.py --base-url http://<host>:8000 --api-key <key>
```

做两件事（见 [07-laya-contract](07-laya-contract.md#健康检查)）：

1. `GET /health`；
2. 一次真实的 `POST /v1/systemone` 简单 `choice` 请求。

期望输出：

```
[ok]   /health                200  3ms
[ok]   /v1/systemone          200  41ms  model=laya-rl-agent checkpoint=english answer=play:h0->m0
```

任一步失败都返回非 0 退出码，便于脚本化验收。

## agent 侧配置

```toml
[laya]
base_url = "http://10.0.0.12:8000"
api_key  = "…"
model    = "english"
timeout_sec = 5
retries = 3
```

对应 `.env.example`：

```
LAYA_BASE_URL=http://127.0.0.1:8000
LAYA_API_KEY=change-me
```

## 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| 首次请求 30s+ | `LAYA_PRELOAD=0` 或未预加载该 checkpoint | 设 `LAYA_PRELOAD=1` |
| 超时频繁 | 状态太长 / 走的 checkpoint 不对 | 确认 `LAYA_DEVICE=cuda` 真的生效（见下一条） |
| 纯 CPU 上反而更慢 | `LAYA_THREADS` 设成了逻辑核数（超线程） | 设成物理核数 |
| 慢得不像 GPU | 装成了 PyPI 的 CPU-only 轮子 | `python -c "import torch; print(torch.cuda.is_available())"`；是 `False` 就按上面用 `--index-url https://download.pytorch.org/whl/cu128` 重装 |
| `pip install laya` 报 No matching distribution | 环境里有 `PIP_NO_INDEX=1`，或代理指向死端口 | 清掉 `PIP_NO_INDEX`；`--proxy` 指向能用的代理（`tools\setup_laya.ps1` 已处理） |
| 401 | key 不一致 | 对齐两侧配置 |
| 413 | 客户端预算校验漏了 | 这是**实现缺陷**，见 [07-laya-contract](07-laya-contract.md#错误处理) |
| 显存不足 | 三个 checkpoint 全预加载 | `LAYA_MODELS=english` |
| 下载时警告 "cache-system uses symlinks ... does not support them" | Windows 没开开发者模式，HF 缓存退化成复制 | 可忽略（只多占磁盘）；想消掉就开开发者模式，或设 `HF_HUB_DISABLE_SYMLINKS_WARNING=1` |

## 相关文档

- 调用契约与重试 -> [07-laya-contract](07-laya-contract.md)
- 验收 -> [11-testing](11-testing.md#端到端验收)
