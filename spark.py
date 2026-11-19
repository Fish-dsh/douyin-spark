#!/usr/bin/env python3
"""抖音网页版自动续火花脚本 - 精简入口。

用法:
    python spark.py            # 窗口内给全部好友发送文案
    python spark.py --force    # 忽略发送窗口与今日幂等限制
    python spark.py --dry-run  # 演练定位链路，不真正发送
"""

from src.scheduler import run

if __name__ == "__main__":
    run()
