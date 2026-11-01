"""pytest 全局夹具: 把主流程心跳写到临时区, 防止测试跑起来踩真实 runtime/heartbeat.json。"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

# 必须早于任何 src.* 导入: logutil.LOG_FILE 在导入时求值, 晚设就来不及。
# 裸 pytest(.venv/bin/pytest)不在 _offline_entry 的识别范围内, 不兜住会往
# 生产监控面 runtime/spark.log 里掺行。
os.environ.setdefault(
    "SPARK_RUNTIME_DIR", tempfile.mkdtemp(prefix="spark-test-runtime-")
)


@pytest.fixture(autouse=True)
def _reset_perf_profile():
    """每个用例前把性能档位复位成生产档 low。

    perf 的档位是进程级全局状态, 而 load_config() 会写它(test_config 里有 high 的
    用例)。不复位就会串味: 后跑的用例拿到 high 的等待值, 断言 4000 实际 2500。
    这也是生产的真实语义 —— 每次启动由 load_config 一次性落档。
    """
    from src import perf

    for key in [k for k in os.environ if k.startswith("SPARK_PERF_")]:
        del os.environ[key]
    perf.set_profile(perf.DEFAULT_PROFILE)
    yield
    perf.set_profile(perf.DEFAULT_PROFILE)


@pytest.fixture(autouse=True)
def _redirect_heartbeat(tmp_path_factory):
    from src import scheduler

    tmp = Path(tempfile.mkdtemp(prefix="hb-")) / "heartbeat.json"
    old = scheduler._HEARTBEAT_FILE
    scheduler._HEARTBEAT_FILE = tmp
    yield
    scheduler._HEARTBEAT_FILE = old
