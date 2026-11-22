#!/usr/bin/env python3
"""抖音网页版自动续火花脚本 - 精简入口。

用法:
    python spark.py            # 窗口内给全部好友发送文案
    python spark.py --force    # 忽略发送窗口与今日幂等限制
    python spark.py --dry-run  # 演练定位链路, 不真正发送
    python spark.py --status   # 最近一夜成了吗(只读日志, 不起浏览器)
    python spark.py --doctor   # 今晚能不能跑(只读检查, 不起浏览器)
"""

import sys

# 查询子命令在 import scheduler 之前分流: 只看状态/体检的路径不该加载 playwright。
# 两者退出码语义不同 —— status 恒 0(它只报告), doctor 有 fail 时 1(它下判断)。
_QUERY = {"--status": "status", "--doctor": "doctor"}

if __name__ == "__main__":
    for flag, module in _QUERY.items():
        if flag in sys.argv[1:]:
            mod = __import__(f"src.{module}", fromlist=["main"])
            sys.exit(mod.main([a for a in sys.argv[1:] if a != flag]))

    from src.scheduler import run

    run()
