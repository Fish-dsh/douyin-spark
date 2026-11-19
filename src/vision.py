"""视觉兜底(VISION FALLBACK): 选择器全失效时, 用多模态模型粗定位 + DOM 现场取证。

背景(2026-10-05 定案): 抖音改版会让 `src/spark_status.py` 的四条 DOM 策略
( CSS class / 文本特征 / 图标扫描 / 结构兜底 )同时失效 —— 它们都锚定「行容器」
的类名或结构。此时唯一还稳定的是**几何位置**: 会话行的像素坐标、火花图标的位置。
故本模块补一条**视觉兜底**:

    脱敏截图 → 多模态模型给「候选行坐标」→ DOM 现场取证(elementFromPoint +
    上溯到行 + 读真实文本) → 校验通过才采用

**红线: 绝不盲点模型坐标**(实测 AGNES 同图 3 次可偏出近一整行)。模型只负责
「哪一行 / 大致区域」这个粗判断; 最终坐标、昵称、是否有火花**全部由本地 DOM 现场
读出并核对**。坐标偏一行会让取证落到相邻的真实行上, 而该行若无火花图标即被丢弃,
不会「点错好友发错人」——这在结构上杜绝了错发的可能。

**脱敏**(用户要求「裁剪+打码后发」): 外发前用 DOM 注入黑色遮罩盖住**整个视口**的
全部文本(昵称/消息预览/时间/右侧已打开的会话记录)、**头像 img**(人脸), 以及不进
文本节点的像素: `input`/`textarea`/`select` 的值、`canvas`、`video`、`iframe`、
带 `background-image` 的元素、以及 `::before`/`::after` 有可见 `content` 的元素。
只保留火花图标与火花底图(判定需要, 按 src 关键词保留, 与类名无关)。

文本遍历会进 **open shadow root**(会话项里有 shadow host, 只从 `document.body` 起
`createTreeWalker` 走不进去, 那部分文本一个遮罩都生成不出来); 闭包 shadow root
拿不到, 属于已知残留风险。跨源 `iframe` 同理读不到 DOM, 按"读不到就整块盖"处理。

**覆盖闸门**: 注入后必须真的命中 ≥1 处才算成功。命中 0 意味着脚本什么都没遮住
(骨架屏 / 选择器改版 / 空白页), 此时放弃这次截图并退回 DOM 取证 —— 脱敏链上
"看着成功其实什么都没做"是最坏的一种失败, 它不报错。

历次修正都发生在 2026-11-19: 最初只盖左栏 45%(右侧会话明文外发) → 补输入框/
canvas/video/背景图 → 补 shadow DOM / iframe / 伪元素 / 覆盖闸门。昵称不来自模型,
它由本地 DOM 回填, 故打码不影响功能。以后新增任何可视元素, 都该按「会不会把用户
内容画进截图」重新过一遍。

**可用性**: 主用 AGNES(`agnes-2.5-flash`, 实测不限流、约 1.5s, 但坐标偏一行);
备用 GLM(`glm-4.6v-flash`, 坐标准但免费档常限流)。都失败/未配置 → 返回空表,
调用方回落原有策略(**绝不因视觉失败中断主流程**)。

**登录类故障绝不走本模块**(风控红线, 见 recovery.LOGIN → ALERT)。

对外接口:
    vision_enabled() -> bool
    locate_rows(page) -> list[dict]      # 视觉发现的会话行(DOM 校验过的真实昵称)
    find_row(page, name) -> dict | None  # 按昵称定位一行(供点击兜底)
"""

from __future__ import annotations

import base64
import datetime
import json
import os
import re
import time
import urllib.request
from pathlib import Path

from logutil import log

try:
    from . import perf
    from .config import BASE_DIR
except ImportError:  # 以顶层模块方式加载时(src/ 在 sys.path 上)
    import perf  # type: ignore[no-redef]
    from config import BASE_DIR  # type: ignore[no-redef]

# 视觉截图与原始页面截图分开放: 前者已脱敏, 便于事后复盘
_SHOT_DIR = BASE_DIR / "runtime" / "vision_shots"
_SHOT_KEEP = 20
_MAX_IMAGE_BYTES = 6 * 1024 * 1024

# 单次视觉兜底的总预算与单次 HTTP 超时都跟性能档位走(perf.vision_budget_ms /
# vision_http_ms): 视觉只在选择器已全失效时才触发, 但绝不能像旧 AI 自愈那样烧穿
# 发送窗口(2026-10-04 事故)。板端 45s/20s, 桌面档 20s/10s。

_PROVIDERS: dict[str, dict] = {
    "agnes": {
        "url": "https://api.agnes-ai.cn/v1/chat/completions",
        "model": "agnes-2.5-flash",
        "env": "AGNES_API_KEY",
    },
    "glm": {
        "url": "https://open.bigmodel.cn/api/paas/v4/chat/completions",
        "model": "glm-4.6v-flash",
        "env": "ZHIPU_API_KEY",
    },
}
# 凭据来源(按优先级): 环境变量 AGNES_API_KEY / ZHIPU_API_KEY > 本文件。
# 默认值指向某台开发机上私有 CLI 的登录缓存, 对陌生人是无意义的路径;
# 用自己的凭据文件就设 SPARK_VISION_AUTH_FILE=/path/to/auth.json(或 config.json
# 的 vision_auth_file), 格式 {"agnes": {"key": "..."}, "glm": {"key": "..."}}。
# 都拿不到 key 时本模块自动空转, 主链路不受影响。
_DEFAULT_AUTH_FILE = Path.home() / ".pi" / "agent" / "auth.json"


def _auth_file() -> Path:
    """凭据文件路径: env SPARK_VISION_AUTH_FILE > config.vision_auth_file > 默认。"""
    env = os.environ.get("SPARK_VISION_AUTH_FILE", "").strip()
    if env:
        return Path(env).expanduser()
    cfg_path = str(_config().get("vision_auth_file") or "").strip()
    if cfg_path:
        return Path(cfg_path).expanduser()
    return _DEFAULT_AUTH_FILE

_PROMPT = (
    "这是抖音网页版聊天页截图, 左侧是会话列表。图中敏感文字与头像已被黑色遮罩盖住, "
    "只有火花/连击图标保持原样。请只输出左侧会话列表中**每一行**的位置, 不要任何解释。\n"
    '输出严格 JSON 数组, 每个元素形如 {"x": <整数>, "y": <整数>}, '
    "x/y 为该会话行在图片内的像素坐标(原点在左上角), 建议取行内火花图标或行中部的点。\n"
    "按从上到下顺序输出所有能看到的会话行; 若无法判断则输出 []。"
)

# ── 脱敏: 用固定定位的黑色遮罩盖住左栏文本与头像(非破坏性, 截图后立即移除) ──
_JS_REDACT = """
() => {
  const vw = window.innerWidth, vh = window.innerHeight;
  // 2026-11-19 修正: 原先只脱敏左侧 45%(limitX 取视口宽度的四成半), 右侧已打开的会话
  // 内容会明文外发 —— 点击好友前/定位失败时右侧往往正挂着上一个会话的记录。
  // 现在覆盖整个视口: 所有文本 + 所有非火花头像图片一律打码, 只保留火花图标
  // (行定位必需)。行定位只看左栏版式, 多打码不影响候选点质量。
  const limitX = vw;
  const boxes = [];
  // 遍历入口: 除了 document.body, 还要进 open shadow root —— 抖音的会话项里确实
  // 有 shadow host, createTreeWalker(document.body) 走不进去, 于是那部分文本
  // 一个遮罩都不会生成(而脚本本身返回的计数还算漂亮, 掩盖了这个漏)。
  const roots = [document.body];
  for (let i = 0; i < roots.length; i++) {
    for (const el of roots[i].querySelectorAll('*')) {
      if (el.shadowRoot) roots.push(el.shadowRoot);   // 闭包 shadow 拿不到, 尽力
    }
  }
  for (const root of roots) {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
    let node;
    while ((node = walker.nextNode())) {
      if (!(node.textContent || '').trim()) continue;
      const p = node.parentElement || node.parentNode;
      if (!p || !p.getBoundingClientRect) continue;
      const r = p.getBoundingClientRect();
      if (r.width < 1 || r.height < 1) continue;
      if (r.left > limitX || r.bottom < 0 || r.top > vh) continue;
      boxes.push([r.left, r.top, r.width, r.height]);
    }
  }
  const flame = /(flame|fire|streak|spark)/i;
  const push = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) return;
    if (r.left > limitX || r.bottom < 0 || r.top > vh) return;
    boxes.push([r.left, r.top, r.width, r.height]);
  };
  for (const img of document.querySelectorAll('img')) {
    const s = String(img.src || img.getAttribute('src') || '');
    if (flame.test(s)) continue;      // 火花图标保留: 判定需要, 且与类名无关
    push(img);
  }
  // 2026-11-19 补: 上面只盖了文本节点和 img, 以下四类像素同样能泄露内容 ——
  //   input/textarea/select 的值不在文本节点里(输入框里正打着的话会被截进去);
  //   canvas 是自绘(含"正在输入"动画等), video 是真画面;
  //   CSS background-image 承载的头像/表情/贴纸在 SPA 里很常见。
  // 一律盖住; 只对 background-image 放行 URL 命中火花关键词的(与 img 同一判据)。
  for (const el of document.querySelectorAll(
    'input, textarea, select, canvas, video, iframe'
  )) {
    push(el);
  }
  // iframe 也在上面那行: 跨源 iframe 里的 DOM 我们读不到(浏览器不允许), 那就
  // 整块盖住 —— 读不到内容时「盖住」比「猜它是空的」安全。
  for (const el of document.querySelectorAll('*')) {
    if (el.closest('#__spark_redact__')) continue;
    let bg = '';
    try {
      bg = getComputedStyle(el).backgroundImage || '';
    } catch (e) {
      bg = '';
    }
    // ::before / ::after 的 content 也是可见文字, 但它在伪元素里, 文本节点遍历
    // 拿不到, 只能按宿主元素整块盖。
    let pseudo = '';
    try {
      const b = getComputedStyle(el, '::before').content || '';
      const a = getComputedStyle(el, '::after').content || '';
      pseudo = b + a;
    } catch (e) {
      pseudo = '';
    }
    const hasPseudo =
      pseudo && pseudo !== 'none' && pseudo !== 'normal' &&
      pseudo.replace(/["']/g, '').trim().length > 0;
    const hasBg = bg && bg !== 'none' && bg.indexOf('url(') >= 0;
    if (!hasBg && !hasPseudo) continue;
    if (hasBg && !hasPseudo && flame.test(bg)) continue;  // 火花底图保留
    push(el);
  }
  const wrap = document.createElement('div');
  wrap.id = '__spark_redact__';
  wrap.style.cssText =
    'position:fixed;inset:0;z-index:2147483647;pointer-events:none;';
  for (const b of boxes) {
    const d = document.createElement('div');
    d.style.cssText = 'position:fixed;background:#000;border-radius:2px;' +
      `left:${b[0]}px;top:${b[1]}px;width:${b[2]}px;height:${b[3]}px;`;
    wrap.appendChild(d);
  }
  document.body.appendChild(wrap);
  return boxes.length;
}
"""

_JS_UNREDACT = """
() => {
  const el = document.getElementById('__spark_redact__');
  if (el && el.parentNode) el.parentNode.removeChild(el);
}
"""

# ── DOM 取证: 把模型给的粗坐标落到真实行上, 读出真实昵称/火花 ──
_JS_ROWS_AT = """
(points) => {
  const FLAME = /(flame|fire|streak|spark)/i;
  const out = [];
  const seen = new Set();
  const rowOf = (el) => {
    let n = el;
    for (let d = 0; d < 8 && n; d++) {
      const r = n.getBoundingClientRect();
      if (r.width >= 140 && r.height >= 28 && r.height <= 140) return n;
      n = n.parentElement;
    }
    return null;
  };
  const flameIn = (row) => {
    for (const img of row.querySelectorAll('img')) {
      const s = String((img.src && typeof img.src === 'string' ? img.src : '')
                        || img.getAttribute('src') || '');
      if (FLAME.test(s)) return s;
    }
    return '';
  };
  for (const p of (points || [])) {
    const x = Math.max(0, Math.min(window.innerWidth - 1, p.x | 0));
    const y = Math.max(0, Math.min(window.innerHeight - 1, p.y | 0));
    const hit = document.elementFromPoint(x, y);
    if (!hit) continue;
    const row = rowOf(hit);
    if (!row) continue;
    const r = row.getBoundingClientRect();
    if (r.left > window.innerWidth * 0.45) continue;   // 只认左栏, 排除消息区
    const key = Math.round(r.top) + ':' + Math.round(r.height);
    if (seen.has(key)) continue;
    seen.add(key);

    const lines = (row.innerText || '').split('\\n')
                    .map((s) => s.trim()).filter(Boolean);
    const name = lines.length ? lines[0] : '';
    if (!name || name.length > 40) continue;           // 无文本/超长 → 非会话行

    const iconUrl = flameIn(row);
    let streakNum = 0;
    if (iconUrl) {
      for (const el of row.querySelectorAll('*')) {
        if (el.children.length) continue;
        const t = (el.textContent || '').trim();
        if (/^\\d{1,4}$/.test(t)) { streakNum = parseInt(t, 10); break; }
      }
    }
    const cls = String(row.className || '');
    out.push({
      name: name,
      hasStreak: !!iconUrl,
      iconSrc: iconUrl ? iconUrl.split('/').pop() : '',
      streakNum: streakNum,
      isPinned: /stick|pinned/i.test(cls),
      method: 'vision',
      x: Math.round(r.left + r.width / 2),
      y: Math.round(r.top + r.height / 2),
    });
  }
  return out;
}
"""


def _config() -> dict:
    """读 config.json(含机器私有配置, 不入 git); 读取/解析失败返回空 dict。"""
    try:
        return json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def vision_enabled() -> bool:
    """视觉兜底总开关: env `SPARK_VISION=1/0` > config.json 的 `vision_fallback` > 默认开。

    默认开是为了无人值守(选择器全失效时无人可救, 视觉兜底是唯一活路); 关闭后
    行为与引入本模块前完全一致(直接回落 ALERT)。未配置任何模型 key 时自动空转。
    """
    env = os.environ.get("SPARK_VISION", "").strip()
    if env == "1":
        return True
    if env == "0":
        return False
    cfg = _config()
    if "vision_fallback" in cfg:
        return bool(cfg["vision_fallback"])
    return True


def _api_key(provider: str) -> str:
    """取 provider 的 API key: 环境变量优先, 其次 pi 的 auth.json(凭据不入仓库)。"""
    info = _PROVIDERS.get(provider) or {}
    env_key = os.environ.get(info.get("env", ""), "").strip()
    if env_key:
        return env_key
    try:
        data = json.loads(_auth_file().read_text(encoding="utf-8"))
        return str((data.get(provider) or {}).get("key") or "").strip()
    except Exception:
        return ""


def _provider_chain() -> list[str]:
    """可用 provider 链: config 指定的主用排第一, 其余押后; 无 key 者剔除。"""
    primary = str(_config().get("vision_provider") or "agnes").strip().lower()
    if primary not in _PROVIDERS:
        primary = "agnes"
    chain = [primary] + [p for p in _PROVIDERS if p != primary]
    return [p for p in chain if _api_key(p)]


def _inject_redaction(page) -> bool:
    """注入脱敏遮罩; 只返回 True 的条件是**真的遮到了东西**。

    "脚本没抛异常" 不等于"遮住了": 页面还在骨架屏、选择器改版、或者落在一个
    空白页上时, 遍历找不到任何文本节点和头像, 返回 0 —— 以前这里照样 return
    True, 于是 /_redacted_screenshot 拍下一张**完全没打码**的整屏, 直接发给外部
    模型。脱敏链路上"看着成功其实什么都没做"是最坏的一种失败: 它静默。

    所以 0 必须当失败处理: 放弃这次截图, 让调用方退回 DOM 取证。
    """
    try:
        n = page.evaluate(_JS_REDACT)
    except Exception as exc:
        log(f"[vision] 注入脱敏遮罩失败: {exc}")
        return False
    try:
        count = int(n)
    except (TypeError, ValueError):
        log(f"[vision] 脱敏脚本返回非数字 {n!r}, 无法确认覆盖, 放弃本次截图")
        return False
    if count <= 0:
        log("[vision] 脱敏遮罩命中 0 处, 无法确认覆盖, 放弃本次截图")
        return False
    log(f"[vision] 已注入脱敏遮罩 {count} 处(文本+头像, 保留火花图标)")
    return True


def _remove_redaction(page) -> None:
    try:
        page.evaluate(_JS_UNREDACT)
    except Exception:
        pass


def _prune() -> None:
    try:
        for old in sorted(_SHOT_DIR.glob("*.png"))[:-_SHOT_KEEP]:
            old.unlink()
    except OSError:
        pass


def _redacted_screenshot(page) -> str:
    """注入遮罩 → 截图 → 移除遮罩; 返回(已脱敏的)截图路径, 失败返回空串。"""
    if not _inject_redaction(page):
        return ""
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    path = _SHOT_DIR / f"{ts}-vision.png"
    ok = False
    try:
        _SHOT_DIR.mkdir(parents=True, exist_ok=True)
        # scale="css": 图像像素 == CSS 像素, 模型给的坐标才能直接喂给 elementFromPoint
        page.screenshot(
            path=str(path),
            full_page=False,
            timeout=perf.ms("screenshot_ms"),
            scale="css",
        )
        ok = path.is_file()
    except Exception as exc:
        log(f"[vision] 脱敏截图失败: {exc}")
    finally:
        _remove_redaction(page)
    if not ok:
        return ""
    _prune()
    log(f"[vision] 脱敏截图已保存: {path}")
    return str(path)


def _encode(path: str) -> str:
    try:
        p = Path(path)
        if not p.is_file() or p.stat().st_size > _MAX_IMAGE_BYTES:
            return ""
        return base64.b64encode(p.read_bytes()).decode("utf-8")
    except Exception:
        return ""


def _extract_text(body: dict) -> str:
    choices = body.get("choices") or []
    if not choices:
        return ""
    msg = choices[0].get("message") or {}
    return str(msg.get("content") or "").strip()


_ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)


def _parse_points(text: str) -> list[dict]:
    """从模型输出里取出点列表; 容忍 ```json 围栏与前后废话, 失败返回空表。"""
    if not text:
        return []
    m = _ARRAY_RE.search(text)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    pts: list[dict] = []
    for it in data:
        if not isinstance(it, dict):
            continue
        try:
            pts.append({"x": int(it.get("x", 0)), "y": int(it.get("y", 0))})
        except (TypeError, ValueError):
            continue
    return pts


def _call(provider: str, img_b64: str, timeout: int) -> list[dict]:
    info = _PROVIDERS[provider]
    payload = {
        "model": info["model"],
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _PROMPT},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{img_b64}"},
                    },
                ],
            }
        ],
        "max_tokens": 800,
        "temperature": 0,
    }
    if provider == "glm":
        payload["thinking"] = {"type": "disabled"}  # 免费视觉档开思考会挤空 content
    req = urllib.request.Request(
        info["url"],
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {_api_key(provider)}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        log(f"[vision] {provider} 调用失败: {exc}")
        return []
    pts = _parse_points(_extract_text(body))
    log(f"[vision] {provider} 返回 {len(pts)} 个候选点")
    return pts


def _ask_points(img_b64: str) -> list[dict]:
    """AGNES 主 / GLM 备, 依次尝试; 全部失败返回空表。"""
    deadline = time.monotonic() + perf.sec("vision_budget_ms")
    for provider in _provider_chain():
        remaining = int(deadline - time.monotonic())
        if remaining <= 1:
            log("[vision] 视觉兜底预算耗尽, 停止尝试")
            break
        pts = _call(provider, img_b64, min(perf.sec("vision_http_ms"), remaining))
        if pts:
            return pts
    return []


def locate_rows(page) -> list[dict]:
    """视觉粗定位 + DOM 取证, 返回会话行列表(含 DOM 读出的真实昵称)。

    任何失败(未启用/无 key/截图失败/模型无输出/DOM 无命中)一律返回 `[]`,
    **绝不抛异常到主流程**。返回项形如::

        {"name": "张三", "hasStreak": True, "iconSrc": "flame_gray.png",
         "streakNum": 5, "isPinned": False, "method": "vision", "x": 96, "y": 320}
    """
    try:
        if not vision_enabled():
            log("[vision] 视觉兜底未启用, 跳过")
            return []
        if not _provider_chain():
            log(
                "[vision] 未配置视觉模型 key(AGNES_API_KEY/ZHIPU_API_KEY, 或 "
                "SPARK_VISION_AUTH_FILE/config.vision_auth_file 指向的凭据文件), 跳过"
            )
            return []
        shot = _redacted_screenshot(page)
        if not shot:
            return []
        img_b64 = _encode(shot)
        if not img_b64:
            return []
        points = _ask_points(img_b64)
        if not points:
            log("[vision] 模型未给出候选点, 视觉兜底放弃")
            return []
        rows = page.evaluate(_JS_ROWS_AT, points)
        if not isinstance(rows, list):
            return []
        log(f"[vision] DOM 取证命中 {len(rows)} 行")
        return [r for r in rows if isinstance(r, dict) and r.get("name")]
    except Exception as exc:
        log(f"[vision] 视觉兜底异常(已忽略, 回落原策略): {exc}")
        return []


def find_row(page, name: str) -> dict | None:
    """按昵称定位一行(供点击兜底); 未命中返回 None。

    只在 DOM 现场读出的真实昵称**完全等于** `name` 时才返回 —— 视觉坐标仅用于
    把候选行拿到眼前, 真正的身份判定在本地 DOM, 故不存在「点错好友」。
    """
    for row in locate_rows(page):
        if row.get("name") == name:
            return row
    return None
