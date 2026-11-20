# 本地开发

## 环境

Linux / macOS：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/playwright install chromium
```

Windows（PowerShell）：

```powershell
py -3 -m venv .venv
.venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\playwright install chromium
```

`requirements-dev.txt` = 运行时依赖 + pytest + ruff；版本全部锁定。只想跑起来、不跑测试的人装 `requirements.txt` 就够。

## 一条命令自检

```bash
python tools/verify.py          # 语法 -> JS 语法 -> lint -> 单测
python tools/verify.py --fast   # 只做前三步（pre-push 用的就是这个）
```

两个平台都能跑（纯 Python，不依赖 bash 或 make）。退出码：**0 全过 / 1 有检查失败 / 2 没失败但有步骤因缺工具被跳过**——2 是"没验证到"，不是"通过"。缺 node / ruff / pytest 记跳过，不记失败，干净 clone 不会因此卡住。

习惯 `./tools/verify.sh` 或 `make verify` 的照旧能用，它们只是转发到同一个脚本。

单跑某一项：

```bash
python -m pytest tests -q        # 单测
python -m ruff check .           # 静态检查
python tools/check_js.py         # src/ 里三引号 JS 常量的语法（需要 node）
```

多数用例用桩数据驱动选择器/DOM，不需要真浏览器；涉及 Playwright 的用例要先 `playwright install chromium`。

## 本地开发入口

`./run_dev.sh --dry-run` 读 `config.dev.json`，把日志、登录态、账本全落到 `runtime-dev/`，不碰正式 `runtime/`。

## 调试

- 页面结构看 `src/debug.py` 的截图与 dump。
- 定位链路卡住时先跑 `spark.py --dry-run`，它会打印每一步选择了哪个元素、为什么放弃。
- 视觉兜底（`src/vision.py`）默认关掉也不会崩：没有模型 key 时自动跳过。

## 提交前自检

1. `python tools/verify.py` 退出码 0（涉及浏览器的用例允许在无浏览器环境下 skip）。
2. 改了日志文案 / 退出码 / 配置字段的，同步改 `docs/` 里对应那篇（退出码在 `operations.md`，配置在 `configuration.md`）。
3. 一次提交只做一件事，提交信息说清"改了什么、为什么"。

## 几个容易踩的坑

- **别把 `config.json`、`.env`、`runtime/` 提交上来**：都已在 `.gitignore` 里，新增敏感文件记得补规则。
- **`headless` 必须是 `false`**：写成 true 的提交等于把整个项目废掉，`health_check` 会直接 FAIL。
- **别在 crontab 里写 `$(( RANDOM % 600 ))`**：Debian cron 会把未转义的 `%` 当换行。抖动逻辑放 `spark.py` 里。
- **改超时/时间预算要成对看**：`run_cron.sh` 的 `timeout 90m` 与硬截止 01:35、巡检 01:45 是一组，动一个必须回头看另外两个。
