# 本地开发

## 环境

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
```

`requirements.txt` 里版本全部锁死，分两类：

- **运行时必需**：`playwright`（浏览器自动化）。
- **可选**：`opencv-python-headless`——只有视觉兜底的 CV 粗定位层（`src/vision_cv.py`）用；没装时 `cv2` 为 `None`，该层自动放弃，主链路不受影响。
- **开发用**：`ruff`（lint），跑 `ruff check .` 才需要。

所以「`pip install -r requirements.txt` 装全」= 开发环境；弱机只跑生产可以省掉 ruff 甚至 opencv。

## 跑测试

一条命令跑完全部自检（语法 → lint → 单测）：

```bash
./tools/verify.sh           # 全跑
./tools/verify.sh --fast    # 只做语法 + lint
make verify                 # 等价，装了 make 就能用
```

分开跑也行：

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/ruff check .
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

1. `./tools/verify.sh` 通过（依次是 `compileall`、`ruff check .`、单测；涉及浏览器的用例允许在无浏览器环境下 skip）。
2. 改了日志文案/退出码/配置字段的，同步改 `docs/` 里对应那篇（退出码在 `operations.md`，配置在 `configuration.md`）。

> 这个仓库**没有远端 CI**（推送令牌没有 workflow 权限，往 `.github/workflows/` 写文件会被 GitHub 拒掉），所以 `tools/verify.sh` 是唯一闸门。想要推送前自动拦一道，`git config core.hooksPath .githooks` 开一次 pre-push 钩子（钩子不随 clone 自动生效，也能被 `--no-verify` 绕过，只是提醒）。

> 本地通过不等于远端通过：这里只有一台机器、一个 Python 版本、一个非干净环境。`README` 承诺的最低版本是 3.9，本机没装就用 `ast.parse(..., feature_version=(3, 9))` 扫一遍语法，别只靠 `compileall`。

## 几个容易踩的坑

- **别把 `config.json`、`.env`、`runtime/` 提交上来**：都已在 `.gitignore` 里，新增敏感文件记得补规则。
- **`headless` 必须是 `false`**：写成 true 的提交等于把整个项目废掉，`health_check` 会直接 FAIL。
- **别在 crontab 里写 `$(( RANDOM % 600 ))`**：Debian cron 会把未转义的 `%` 当换行。抖动逻辑放 `spark.py` 里。
- **改超时/时间预算要成对看**：`run_cron.sh` 的 `timeout 90m` 与硬截止 01:35、巡检 01:45 是一组，动一个必须回头看另外两个。
