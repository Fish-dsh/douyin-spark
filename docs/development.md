# 本地开发

## 环境

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
```

`requirements.txt` 里的版本是锁定的（playwright / ruff / opencv-python-headless）。`requirements.txt` 只放运行与测试必需的东西。

## 跑测试

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/ruff check .
```

多数用例用桩数据驱动选择器/DOM，不需要真浏览器；涉及 Playwright 的用例要先 `playwright install chromium`。

## 本地开发入口

`./run_dev.sh --dry-run` 读 `config.dev.json`，把日志、登录态、账本全落到 `runtime-dev/`，不碰正式 `runtime/`。

## 调试

- 页面结构看 `src/debug.py` 的截图与 dump。
- 定位链路卡住时先跑 `spark.py --dry-run`，它会打印每一步选择了哪个元素、为什么放弃。
- 视觉兜底（`src/vision.py`）默认**开**（无人值守时它是选择器全失效的唯一活路），但没有模型 key 时自动空转返回空表，行为等同关闭，不会崩。要显式关掉用 `SPARK_VISION=0` 或 `vision_fallback: false`。

## 提交前自检

1. `python -m compileall -q src tests tools spark.py login.py logutil.py` 通过。
2. `.venv/bin/python -m unittest discover -s tests` 全绿（涉及浏览器的用例允许在无浏览器环境下 skip）。
3. `.venv/bin/ruff check .` 干净。
4. 改了日志文案/退出码/配置字段的，同步改 `docs/` 里对应那篇（退出码在 `operations.md`，配置在 `configuration.md`）。

## 几个容易踩的坑

- **别把 `config.json`、`.env`、`runtime/` 提交上来**：都已在 `.gitignore` 里，新增敏感文件记得补规则。
- **`headless` 必须是 `false`**：写成 true 的提交等于把整个项目废掉，`health_check` 会直接 FAIL。
- **别在 crontab 里写 `$(( RANDOM % 600 ))`**：Debian cron 会把未转义的 `%` 当换行。抖动逻辑放 `spark.py` 里。
- **改超时/时间预算要成对看**：`run_cron.sh` 的 `timeout 90m` 与硬截止 01:35、巡检 01:45 是一组，动一个必须回头看另外两个。
