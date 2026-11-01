"""网络层旁路嗅探：被动捕获抖音 IM 相关 JSON 响应与 WS 帧, 定位为诊断金丝雀。

动机(2026-10-03 调研): 同类抓取项目里, 杀死链路的往往不是 DOM 改版而是私有
API 轮换; 反过来, 我们 DOM 链路的单点依赖(混淆类名)理论上可用『读取站点自拉的
结构化数据』绕开。实盘判定(2026-10-03 板端两跑): 抖音 chat 的**实时会话列表+
火花天数走 frontier-im WS 的二进制 protobuf**, HTTP 侧只有 im/user/info(身份,
无火花态)与 im/strategy/config(静态资源)。故网络路径**不能**作为发现主策略
(逆向 protobuf = 私有 API 军备竞赛, 1.5GB cron 盒维护成本高且 ToS 风险大, 正是
调研中 IG/TikTok 抓取潮的死因); 抗改版主策略仍是 spark_status 的结构性 DOM+
React fiber。本嗅探器保留为**诊断金丝雀**: 存下 IM bootstrap 样本+枚举 API 面,
若抖音哪天把会话数据改回明文 JSON, 可第一时间察觉并升级。全被动, 异常吞掉。
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from logutil import log

from .config import RUNTIME_DIR

# URL 信号: 只关心 IM/会话相关请求, 其余一概不看
# 2026-10-03 实盘判定: 抖音 chat 的实时会话列表+火花天数走 frontier-im WS 的
# 二进制 protobuf, HTTP 侧(imapi get_conversation_list / im/user/info)多为身份
# 或静态资源。故本嗅探器定位为『诊断金丝雀』(存下 IM bootstrap, 若哪天改回
# 明文 JSON 可第一时间察觉), 不作为发现主策略(逆向 protobuf = 私有 API 军备竞赛,
# 1.5GB cron 盒不值得, 且 ToS 风险高)。
_URL_PAT = re.compile(
    r"(?i)(conversation|session_list|sessionlist|/im[/.-]|imapi|/v1/message|"
    r"streak|spark|flame|friend|/v1/stranger)"
)
# 端点枚举信号: 比 _URL_PAT 更宽, 用于观察期一次性摸清 API 面
_ENDPOINT_PAT = re.compile(r"(?i)(/im/|/aweme/|imapi|/v1/|conversation|message)")
# 响应体信号: 命中 URL 后再看正文前缀, 双保险防止误采无关 JSON
_BODY_HINT = re.compile(
    r"(?i)(conversation|streak|spark|flame|nickname|session|core_data|remark_name)"
)

_MAX_BODY_BYTES = 256 * 1024  # 单条样本上限
_MAX_PAYLOADS = 80  # 单会话采集条数上限
_MAX_FILE_BYTES = 2 * 1024 * 1024  # 单样本文件上限
_KEEP_FILES = 8  # 目录内保留最近文件数
_MEM_KEEP = 6  # 内存中保留已解析对象数(供提取器用)

_SAMPLES_DIR = RUNTIME_DIR / "network_samples"


def _rotate_samples(samples_dir: Path) -> None:
    """只保留最近 _KEEP_FILES 个样本文件, 其余删除。"""
    try:
        files = sorted(samples_dir.glob("net_*.jsonl"), key=lambda p: p.name)
        for old in files[:-_KEEP_FILES]:
            old.unlink(missing_ok=True)
    except Exception as exc:
        log(f"[sniff] 样本轮转失败: {exc}")


class NetSniffer:
    """挂在 Page 上的被动嗅探器; 所有异常一律吞掉, 绝不影响主链路。"""

    def __init__(self, account_name: str = "default", samples_dir: Path | None = None):
        self.account = re.sub(r"[^\w.-]", "_", account_name or "default")
        self.dir = Path(samples_dir) if samples_dir else _SAMPLES_DIR
        self.recorded = 0
        self.responses_seen = 0
        self.ws_frames_seen = 0
        # 诊断: URL 命中但被过滤/失败的记录(去重, 封顶)与 WS 连接清单
        self.misses: list[str] = []
        self.ws_urls: list[str] = []
        # 端点枚举: 所有 IM/aweme 相关 xhr/fetch 的 path(去 query), 供一次性摸清 API 面
        self.endpoints: set[str] = set()
        self._parsed: list[dict] = []  # {url, kind, obj}
        self._file: Path | None = None

    # ---- 挂载 ----

    def attach(self, page) -> None:
        page.on("response", self._on_response)
        page.on("websocket", self._on_ws)
        # 暴露给发现链路(spark_status)与调度器
        page._spark_sniffer = self

    def _miss(self, reason: str, url: str) -> None:
        if len(self.misses) < 40:
            self.misses.append(f"{reason} {url[:120]}")

    # ---- 事件处理(全部防御式) ----

    def _on_response(self, resp) -> None:
        try:
            if self.recorded >= _MAX_PAYLOADS:
                return
            url = resp.url or ""
            rt = resp.request.resource_type or ""
            if rt not in ("xhr", "fetch", "document"):
                return
            if _ENDPOINT_PAT.search(url) and len(self.endpoints) < 60:
                p = urlparse(url)
                self.endpoints.add(f"{rt} {p.netloc}{p.path}")
            if not _URL_PAT.search(url):
                return
            self.responses_seen += 1
            try:
                body = resp.body()
            except Exception as exc:
                self._miss(f"body失败({type(exc).__name__})", url)
                return
            if not body:
                self._miss("空body", url)
                return
            if len(body) > _MAX_BODY_BYTES:
                self._miss(f"超大body({len(body)}B)", url)
                return
            text = body.decode("utf-8", "ignore")
            if not _BODY_HINT.search(text[:2000]):
                self._miss("body无信号", url)
                return
            self._record("resp", url, text)
        except Exception:
            pass

    def _on_ws(self, ws) -> None:
        try:
            if len(self.ws_urls) < 20 and ws.url not in self.ws_urls:
                self.ws_urls.append(ws.url)

            def _frame(frame):
                try:
                    if self.recorded >= _MAX_PAYLOADS:
                        return
                    self.ws_frames_seen += 1
                    try:
                        text = frame.text
                    except Exception:
                        return  # 二进制帧(protobuf), 文本嗅探不覆盖
                    if not text or len(text) > _MAX_BODY_BYTES:
                        return
                    if not _URL_PAT.search(ws.url or "") and not _BODY_HINT.search(
                        text[:2000]
                    ):
                        return
                    self._record("ws", ws.url or "", text)
                except Exception:
                    pass

            ws.on("framereceived", _frame)
        except Exception:
            pass

    # ---- 落盘 + 内存缓存 ----

    def _record(self, kind: str, url: str, text: str) -> None:
        obj = None
        try:
            obj = json.loads(text)
        except Exception:
            if kind != "ws":  # 响应以 JSON 为主, 解析失败多半是加密/压缩体
                return
        self.recorded += 1
        self._write_sample(kind, url, text)
        if obj is not None:
            self._parsed.append({"url": url, "kind": kind, "obj": obj})
            if len(self._parsed) > _MEM_KEEP:
                self._parsed.pop(0)

    def _write_sample(self, kind: str, url: str, text: str) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            if self._file is None:
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                self._file = self.dir / f"net_{stamp}_{self.account}.jsonl"
                self._file.touch()
                _rotate_samples(self.dir)
            if self._file.exists() and self._file.stat().st_size > _MAX_FILE_BYTES:
                return
            with self._file.open("a", encoding="utf-8") as f:
                f.write(
                    json.dumps(
                        {
                            "ts": datetime.now().isoformat(timespec="seconds"),
                            "kind": kind,
                            "url": url,
                            "body": text,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        except Exception as exc:
            log(f"[sniff] 样本写入失败: {exc}")

    # ---- 供上层使用 ----

    def parsed_payloads(self) -> list[dict]:
        return list(self._parsed)

    def summary(self) -> str:
        return (
            f"响应命中 {self.responses_seen} 条/WS帧 {self.ws_frames_seen} 条, "
            f"记录 {self.recorded} 条样本"
            + (f" -> {self._file.name}" if self._file else "(无样本落盘)")
        )

    def diagnostics(self) -> list[str]:
        """观察期专用: 端点清单 + WS 连接 + 过滤原因, 每类封顶若干行。"""
        lines = [f"endpoint: {e}" for e in sorted(self.endpoints)[:40]]
        lines += [f"ws: {u}" for u in self.ws_urls[:10]]
        lines += [f"miss: {m}" for m in self.misses[:20]]
        return lines


def sniff_report(page) -> None:
    """主链路安全调用点: 输出嗅探摘要; 任何异常吞掉。"""
    try:
        sniffer = getattr(page, "_spark_sniffer", None)
        if sniffer is not None:
            log(f"[sniff] {sniffer.summary()}")
    except Exception:
        pass
