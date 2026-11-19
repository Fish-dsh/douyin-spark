# 常用动作的快捷入口。没有 make 也不影响: 直接跑 tools/verify.sh 等价。
.PHONY: help verify verify-fast test lint js check-js clean

help:
	@echo 'make verify       提交前自检: 语法 + JS 语法 + lint + 单测'
	@echo 'make verify-fast  只做语法 + JS 语法 + lint (约 2 秒)'
	@echo 'make test         只跑单测'
	@echo 'make lint         只跑 ruff'
	@echo 'make js           只扫 JS 常量语法 (需要 node)'

verify:
	@./tools/verify.sh

verify-fast:
	@./tools/verify.sh --fast

test:
	@.venv/bin/python -m pytest tests -q

lint:
	@.venv/bin/python -m ruff check .

js check-js:
	@.venv/bin/python tools/check_js.py

clean:
	@find . -name __pycache__ -type d -prune -exec rm -rf {} + ; \
	 rm -rf .pytest_cache .ruff_cache
