# 常用动作的快捷入口。没有 make 也不影响: 直接跑 tools/verify.sh 等价。
.PHONY: help verify verify-fast test lint clean

help:
	@echo 'make verify       提交前自检: 语法 + lint + 单测'
	@echo 'make verify-fast  只做语法 + lint (约 1 秒)'
	@echo 'make test         只跑单测'
	@echo 'make lint         只跑 ruff'

verify:
	@./tools/verify.sh

verify-fast:
	@./tools/verify.sh --fast

test:
	@.venv/bin/python -m pytest tests -q

lint:
	@.venv/bin/python -m ruff check .

clean:
	@find . -name __pycache__ -type d -prune -exec rm -rf {} + ; \
	 rm -rf .pytest_cache .ruff_cache