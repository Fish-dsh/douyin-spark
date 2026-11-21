#!/usr/bin/env python3
"""抖音网页版自动续火花脚本 - 精简入口。

用法:
    python spark.py            # 窗口内给全部好友发送文案
    python spark.py --force    # 忽略发送窗口与今日幂等限制
    python spark.py --dry-run  # 演练定位链路, 不真正发送
    python spark.py --status   # 最近一夜成了吗(只读日志, 不起浏览器)
"""

import sys

if __name__ == "__main__":
    if "--status" in sys.argv[1:]:
        from src.status import main as status_main

        sys.exit(status_main([a for a in sys.argv[1:] if a != "--status"]))

    from src.scheduler import run

    run()
