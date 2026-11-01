#!/usr/bin/env python3
"""一次性观察工具: 开一个真实会话, 用 NetSniffer 旁路捕获 IM 流量并打印结构摘要。

用途: network 提取器(第二期)开发前, 摸清抖音 chat 页自拉的 JSON/WS 数据形状。
只读观察, 不发送任何消息。输出刻意只给『路径+键名+条数』级别的摘要,
完整样本另存在 runtime/network_samples/(gitignored)。

用法(仓库根目录):
    .venv/bin/python tools/observe_net.py [--account 13800000001] [--wait 25]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.sync_api import sync_playwright

from src.config import RUNTIME_DIR, load_config
from src.net_sniff import NetSniffer
from src.session import close_session, open_session

_NAME_KEYS = ("nickname", "nick_name", "nickName", "name", "title", "remark")


def _looks_like_user_dict(d: dict) -> bool:
    return isinstance(d, dict) and any(
        k in d for k in _NAME_KEYS + ("conversation_id", "user_id", "uid", "sec_uid")
    )


def _walk(obj, path="$", depth=0, hits=None):
    """找『元素像用户/会话对象的数组』: 打印路径、条数、首元素键名。"""
    if hits is None:
        hits = []
    if depth > 12:
        return hits
    if isinstance(obj, list):
        objs = [x for x in obj if _looks_like_user_dict(x)]
        if objs:
            keys = sorted(objs[0].keys())
            hits.append({"path": path, "count": len(objs), "sample_keys": keys[:25]})
        for i, x in enumerate(obj[:5]):
            _walk(x, f"{path}[{i}]", depth + 1, hits)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            _walk(v, f"{path}.{k}", depth + 1, hits)
    return hits


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", default="13800000001")
    ap.add_argument("--wait", type=int, default=25, help="页面停留秒数")
    args = ap.parse_args()

    cfg = load_config()
    account = next(
        (a for a in cfg.get("accounts", []) if a.get("name") == args.account), None
    )
    if account is None:
        account = {
            "name": args.account,
            "storage_file": f"storage_{args.account}.json",
            "state_file": f"state_{args.account}.json",
        }
    storage = RUNTIME_DIR / account["storage_file"]
    if not storage.is_file():
        print(f"缺少登录态文件 {storage}, 无法观察")
        sys.exit(1)

    with sync_playwright() as p:
        browser, context, page = open_session(p, cfg, account)
        sniffer = getattr(page, "_spark_sniffer", None) or NetSniffer(args.account)
        try:
            print(f"会话已开, 停留 {args.wait}s 采集流量…")
            page.wait_for_timeout(args.wait * 1000)
            print(f"[sniff] {sniffer.summary()}")
            print("== 诊断 ==")
            for d in sniffer.diagnostics():
                print(f"  {d}")
            for item in sniffer.parsed_payloads():
                url = item["url"]
                hits = _walk(item["obj"])
                print(f"\n== payload kind={item['kind']} url={url[:120]}")
                if not hits:
                    top = (
                        sorted(item["obj"].keys())
                        if isinstance(item["obj"], dict)
                        else type(item["obj"]).__name__
                    )
                    print(f"   (无用户数组) 顶层键: {top}")
                for h in hits:
                    print(
                        f"   数组@{h['path']} 条数={h['count']} 键={h['sample_keys']}"
                    )
        finally:
            close_session(browser, context)
    print("\n完整样本: runtime/network_samples/")


if __name__ == "__main__":
    main()
