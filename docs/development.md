# 本地开发

## 环境

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt   # 开发环境: 运行时 + ruff + pytest
.venv/bin/playwright install chromium
sudo apt install -y nodejs                      # 可选, 但完整自检需要它(见下)
```

> **Node 只在开发机需要，生产不需要。** `tools/check_js.py` 靠 `node --check` 检查写在 Python 三引号里的 JS 常量，没装 node 时 `tools/verify.sh` 会**跳过**这一步并以**退出码 2** 收尾 —— 2 是「没验证到」，不是「通过」。所以：只想把程序跑起来的话不用装 node；想跑完整自检（提 PR 前）就得装。各平台装法：Debian/Ubuntu `sudo apt install -y nodejs`，macOS `brew install node`，Windows 装官方 LTS 包或 `winget install OpenJS.NodeJS.LTS`（Windows 上跑这套 shell 脚本还得先进 WSL）。

依赖分成三份，按「跑起来到底需要什么」切，不是按喜好切：

| 文件 | 什么时候装 | 内容 |
| --- | --- | --- |
| `requirements.txt` | **任何机器都要** | `playwright`（主链路唯一硬依赖） |
| `requirements-vision.txt` | 想要 CV 粗定位那层才装 | 上面那份 + `opencv-python-headless` |
| `requirements-dev.txt` | 开发机 / CI | 上面第一份 + `ruff` + `pytest` |

为什么拆：`opencv-python-headless` 解压出来比整份代码还大，而板子只有 1.5G 内存。以前它写在 `requirements.txt` 里，于是「只跑生产」的机器也被迫装一个可选库。现在不装 `requirements-vision.txt` 时 `src/vision_cv.py` 里 `cv2` 为 `None`，该层自动放弃，主链路不受影响 —— 代价只是选择器全失效时的兜底能力弱一层，而这个代价本来就不该由内存说算。

生产板子上 vision 那份是装了的（`docs/operations.md` 的部署步骤用它）。

## 跑测试

一条命令跑完全部自检（语法 → JS 语法 → lint → 单测）：

```bash
./tools/verify.sh           # 全跑
./tools/verify.sh --fast    # 只做语法、JS 语法和 lint
make verify                 # 等价，装了 make 就能用
PY=/usr/bin/python3.11 ./tools/verify.sh   # 换一个解释器交叉验证
```

退出码分三种：`0` 全过；`1` 有检查真失败；`2` **没有失败但有步骤被跳过**（干净机器上没装 ruff 或 node 就属于这类）。`2` 不是绿 —— 它表示「没验证到」，别在脚本里把它当通过。`--fast` 主动跳过单测**不算**跳过检查（那是使用者按需裁剪），所以装了 ruff + node 的机器上 `--fast` 会返回 `0`。

第 2 步 `tools/check_js.py` 值得单独说一句：`src/` 里大量 JS 是写在 Python 三引号里的字符串，语法错只有在浏览器里 `page.evaluate` 执行时才炸，生产日志里表现为 `Page.evaluate: SyntaxError`，本地单测和 `compileall` 都看不见。这个脚本用 `ast` 把那些常量抠出来交给 `node --check`。

它扫两类东西：`src/` 下所有 `.py` 与仓库根的几个脚本里**赋值给名字**的 JS 常量（`_JS_REDACT = """..."""` 这种），以及**直接写进调用参数**的内联脚本（`page.evaluate("""() => {...}""")`）。第二类是重点：内联脚本没有名字，靠人工盯永远会漏，而它们和常量一样只在浏览器里才会炸。当前一次扫 24 个文件、37 段 JS。node 不是本项目的依赖（这不是 Node 项目），没装就跳过该步。

分开跑也行：

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/ruff check .
.venv/bin/python tools/check_js.py     # 需要 node
```

多数用例用桩数据驱动选择器/DOM，不需要真浏览器；涉及 Playwright 的用例要先 `playwright install chromium`。

没装 opencv-python-headless 也能跑：需要真画图的几条（tests/test_vision_cv.py、tests/test_sender.py 里标了 skipUnless(vision_cv.available()) 的）会自己 skip，剩下的全过——这也顺带验证了 opencv 确实只是可选依赖。装了只会多跑几条，不会挂。

## 本地开发入口

`./run_dev.sh --dry-run` 读 `config.dev.json`（`extends: config.json` + `perf_profile: high` + `headless: false` + `display: null`），把日志、登录态、账本全落到 `runtime-dev/`，不碰正式 `runtime/`。

全新 clone 直接跑也行：脚本先查 `.venv/bin/python`，缺了就打印装环境的命令并退出；`config.json` 被 `.gitignore` 排除、clone 里没有，脚本会按 `config.example.json` 补一份 `chmod 600` 的占位配置并提示去改 `accounts` / `send_window`、用 `login.py --manual` 生成登录态。

## 调试

- 页面结构看 `src/debug.py` 的截图与 dump。
- 定位链路卡住时先跑 `spark.py --dry-run`，它会打印每一步选择了哪个元素、为什么放弃。
- 视觉兜底（`src/vision.py`）默认**开**（无人值守时它是选择器全失效的唯一活路），但没有模型 key 时自动空转返回空表，行为等同关闭，不会崩。要显式关掉用 `SPARK_VISION=0` 或 `vision_fallback: false`。

## 提交前自检

1. `./tools/verify.sh` 通过（依次是 `compileall`、`tools/check_js.py` 的 `node --check`、`ruff check .`、单测；涉及浏览器的用例允许在无浏览器环境下 skip）。
2. 改了日志文案/退出码/配置字段的，同步改 `docs/` 里对应那篇（退出码在 `operations.md`，配置在 `configuration.md`）。

> 这个仓库**没有远端 CI**（推送令牌没有 workflow 权限，往 `.github/workflows/` 写文件会被 GitHub 拒掉），所以 `tools/verify.sh` 是唯一闸门。想要推送前自动拦一道，`git config core.hooksPath .githooks` 开一次 pre-push 钩子（钩子不随 clone 自动生效，也能被 `--no-verify` 绕过，只是提醒）。

> 本地通过不等于远端通过：这里只有一台机器、一个 Python 版本、一个非干净环境。`README` 承诺的最低版本是 3.9，本机没装就用 `ast.parse(..., feature_version=(3, 9))` 扫一遍语法，别只靠 `compileall`。

## 几个容易踩的坑

- **别把 `config.json`、`.env`、`runtime/` 提交上来**：都已在 `.gitignore` 里，新增敏感文件记得补规则。
- **`headless` 必须是 `false`**：写成 true 的提交等于把整个项目废掉，`health_check` 会直接 FAIL。
- **别在 crontab 里写 `$(( RANDOM % 600 ))`**：Debian cron 会把未转义的 `%` 当换行。抖动逻辑放 `spark.py` 里。
- **改超时/时间预算要成对看**：`run_cron.sh` 的 `timeout 90m` 与硬截止 01:35、巡检 01:45 是一组，动一个必须回头看另外两个。
