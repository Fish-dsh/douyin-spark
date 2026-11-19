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
全部文本(昵称/消息预览/时间/右侧已打开的会话记录), 以及一切可能把用户内容画进
截图、但**不在文本节点里**的载体:

  - 位图: `img`、SVG 的 `image`/`use`(同样能加载位图, 只扫 `img` 会整块漏)
  - 值/自绘/画面: `input`/`textarea`/`select` 的值、`canvas`(含"正在输入"动画)、
    `video`, 以及读不到 DOM 的跨源 `iframe`(读不到就整块盖, 不猜它是空的)
  - CSS 通道: `background-image`、`mask-image`/`-webkit-mask-image`、
    `border-image-source`, 以及 `::before`/`::after` 有可见 `content` 的宿主元素

只保留火花图标与火花底图(行定位必需)。放行条件是三条**同时**成立: URL/样式命中
`flame|fire|streak|spark`、渲染尺寸落在图标区间(8~48px)、且不在 `a[href*="/user/"]`
内(头像通常包在用户主页链接里)。任一条不满足就走打码 —— 失败方向永远是「盖住」;
旧版只看关键词, 普通头像 URL 撞上 `spark` 就被整块放行, 等于没打码。

**所有扫描都进 open shadow root**(会话项里有 shadow host, 只从 `document.body` 起
`querySelectorAll` 会把整片 shadow DOM 漏掉, 而返回的计数照样漂亮)。

**读不到的组件整块盖住**: `el.shadowRoot` 为 null 无法区分「没有 shadow DOM」与
「用了闭合(closed) shadow root」。对能确认内容不来自 light DOM 的宿主 —— 自定义
元素(标签名含连字符)、无 open shadow root、自身没有子节点、却有渲染尺寸 —— 一律
整块遮罩并计入 `unreadable`。闭合 root 里若有火花图标会被一起盖掉, 这是有意的:
宁可这次定位失败退回 DOM 取证, 也不把读不到的内容发出去。

**覆盖闸门**(四道, 任一不过就放弃截图并退回 DOM 取证 —— 脱敏链上"看着成功其实
什么都没做"是最坏的一种失败, 因为它不报错):

  1. 脚本必须回报结构化结果
     `{masked, textNodes, uncovered, unverified, unreadable, keptFlame}`;
  2. `masked > 0`: 一处都没遮, 说明这次遍历根本没找到东西(骨架屏/改版/空白页);
  3. `uncovered == 0`: 拿遮罩元素**渲染后**的矩形复核对**每一个**文本矩形 ——
     遮罩矩形栅格化到 2px 网格, 要求矩形(内缩 1px)覆盖到的网格点全部被占住。
     `position:fixed` 落在被 `transform`/`filter` 的祖先里时会相对那个祖先定位 ——
     那时数组是对的、屏幕上是错的。只判 `masked > 0` 抓不到这种「加了没盖上」,
     只判矩形中心点也抓不到「中间盖住了、上下露着」;
  4. `unverified == 0`: 覆盖复核有计算预算(防超大 DOM 卡死主线程), 预算耗尽时
     剩下的文本块是「没验证过」而不是「盖住了」。无法证明就必须放弃截图。

历次修正都发生在 2026-11-19: 最初只盖左栏 45%(右侧会话明文外发) → 补输入框/
canvas/video/背景图 → 补 shadow DOM / iframe / 伪元素 / 覆盖闸门 → 非文本扫描也走
shadow root、补 mask/border-image 与 SVG 位图、火花放行收窄、加覆盖自检 → 覆盖自检
从「抽样看中心点」改成「全量逐点 + 预算兜底」、闭合 shadow 宿主整块盖。昵称不来自
模型, 它由本地 DOM 回填, 故打码不影响功能。以后新增任何可视元素, 都该按「会不会把
用户内容画进截图」重新过一遍。

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

from . import perf
from .config import BASE_DIR

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

# ── 脱敏: 用固定定位的黑色遮罩盖住整屏文本与图像载体(非破坏性, 截图后立即移除) ──
_JS_REDACT = r"""
() => {
  const vw = window.innerWidth, vh = window.innerHeight;
  // 2026-11-19 修正: 原先只脱敏左侧 45%(limitX 取视口宽度的四成半), 右侧已打开的
  // 会话内容会明文外发 —— 点击好友前/定位失败时右侧往往正挂着上一个会话的记录。
  // 现在覆盖整个视口。
  const limitX = vw;
  const boxes = [];
  // 遍历入口: 除了 document.body, 还要进 open shadow root —— 抖音的会话项里确实
  // 有 shadow host, createTreeWalker(document.body) 走不进去, 于是那部分内容
  // 一个遮罩都不会生成(而脚本本身返回的计数还算漂亮, 掩盖了这个漏)。
  //
  // 2026-11-19 补: 以前只有「文本遍历」走 roots, img / 表单与媒体元素 / CSS 承载
  // 图像三条扫描还在用 document.querySelectorAll —— shadow root 里的头像、canvas、
  // 背景图因此一个都没盖。现在**所有**扫描都走 roots。
  const roots = [document.body];
  for (let i = 0; i < roots.length; i++) {
    for (const el of roots[i].querySelectorAll('*')) {
      if (el.shadowRoot) roots.push(el.shadowRoot);   // 闭包 shadow 拿不到, 尽力
    }
  }
  const visible = (r) =>
    r.width >= 1 && r.height >= 1 &&
    r.right > 0 && r.left < vw && r.bottom > 0 && r.top < vh;
  const push = (r) => boxes.push([r.left, r.top, r.width, r.height]);
  const pushEl = (el) => {
    const r = el.getBoundingClientRect();
    if (visible(r)) push(r);
  };
  const inOverlay = (el) => !!(el.closest && el.closest('#__spark_redact__'));
  // 火花图标要留着(行定位靠它), 但「URL 里含 flame|fire|streak|spark」这一条太宽:
  // 普通头像/贴纸的 URL 只要撞上关键词就被放行, 那等于没打码。现在的放行条件是
  // 三条同时成立:
  //   1) URL/样式里命中关键词;
  //   2) 渲染出来是图标尺寸(8~48px) —— 火花是固定小图标, 头像/贴纸很少这么小;
  //   3) 不在用户主页链接里 —— 头像通常包在 a[href*="/user/"] 内。
  // 缺一条就走打码。失败方向永远是「盖住」。
  const flame = /(flame|fire|streak|spark)/i;
  const iconSized = (el) => {
    const r = el.getBoundingClientRect();
    return r.width >= 8 && r.width <= 48 && r.height >= 8 && r.height <= 48;
  };
  const inUserLink = (el) => {
    let p = el;
    for (let i = 0; i < 4 && p; i++) {
      if (p.tagName === 'A' && /\/user\//.test(String(p.getAttribute('href') || ''))) {
        return true;
      }
      p = p.parentElement;
    }
    return false;
  };
  const keepFlame = (el, s) =>
    !!s && flame.test(s) && iconSized(el) && !inUserLink(el);
  let keptFlame = 0;
  // 1) 文本节点(含 open shadow root)
  const textRects = [];
  for (const root of roots) {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
    let node;
    while ((node = walker.nextNode())) {
      if (!(node.textContent || '').trim()) continue;
      const p = node.parentElement || node.parentNode;
      if (!p || !p.getBoundingClientRect) continue;
      if (inOverlay(p)) continue;
      const r = p.getBoundingClientRect();
      if (!visible(r)) continue;
      textRects.push(r);
      push(r);
    }
  }
  // 2) 图像载体: img 与 SVG 里的 image/use(同样能加载位图, 以前完全没扫),
  //    以及输入控件/画布/视频/跨源 iframe。
  //    input/textarea/select 的值不在文本节点里(输入框里正打着的话会被截进去);
  //    canvas 是自绘(含"正在输入"动画等), video 是真画面; iframe 跨源读不到内容,
  //    读不到时「整块盖住」比「猜它是空的」安全。
  for (const root of roots) {
    for (const node of root.querySelectorAll('img, svg image, svg use')) {
      const s = String(
        node.getAttribute('src') || node.getAttribute('href') ||
        node.getAttribute('xlink:href') || node.src || ''
      );
      if (keepFlame(node, s)) { keptFlame++; continue; }
      pushEl(node);
    }
    for (const el of root.querySelectorAll(
      'input, textarea, select, canvas, video, iframe'
    )) {
      pushEl(el);
    }
  }
  // 3) CSS 承载的图像与伪元素文字。
  //    background-image 在 SPA 里很常见; mask-image / border-image 同样是位图通道
  //    (以前没扫, 用它们做头像/贴纸的节点会整块漏掉); ::before/::after 的 content
  //    是可见文字, 文本节点遍历拿不到, 只能按宿主元素整块盖。
  for (const root of roots) {
    for (const el of root.querySelectorAll('*')) {
      if (inOverlay(el)) continue;
      let cs;
      try { cs = getComputedStyle(el); } catch (e) { continue; }
      if (!cs) continue;
      let pseudo = '';
      try {
        const b = getComputedStyle(el, '::before').content || '';
        const a = getComputedStyle(el, '::after').content || '';
        pseudo = b + a;
      } catch (e) { pseudo = ''; }
      const hasPseudo =
        !!pseudo && pseudo !== 'none' && pseudo !== 'normal' &&
        pseudo.replace(/["']/g, '').trim().length > 0;
      const carriers = [
        cs.backgroundImage, cs.maskImage, cs.webkitMaskImage, cs.borderImageSource
      ];
      let hasCarrier = false, allFlame = true;
      for (const c of carriers) {
        if (!c || c === 'none') continue;
        hasCarrier = true;
        if (c.indexOf('url(') < 0 || !flame.test(c)) allFlame = false;
      }
      if (!hasCarrier && !hasPseudo) continue;
      if (hasCarrier && !hasPseudo && allFlame && keepFlame(el, 'flame')) {
        keptFlame++;
        continue;   // 火花底图保留
      }
      pushEl(el);
    }
  }
  // 4) 读不到的组件。`el.shadowRoot` 为 null 既可能是「根本没有 shadow DOM」,
  //    也可能是「用了闭合(closed) shadow root」—— 两者在脚本里无法区分, 而闭合 root
  //    里的昵称/头像不会进 roots, 前面三条扫描一个都命中不了它, 计数却照样漂亮。
  //    兜底只针对能确认「内容不是来自 light DOM」的宿主: 自定义元素(标签名含连字符)、
  //    没有 open shadow root、自身没有子节点、却有渲染尺寸。这种宿主整块盖住。
  //    闭合 root 里若藏着火花图标, 它会一起被盖掉 —— 这是有意的选择: 宁可这次定位
  //    失败退回 DOM 取证, 也不把读不到的内容发出去。
  let unreadable = 0;
  for (const root of roots) {
    for (const el of root.querySelectorAll('*')) {
      if (inOverlay(el)) continue;
      if (String(el.tagName || '').indexOf('-') < 0) continue;
      if (el.shadowRoot) continue;
      if (el.childNodes && el.childNodes.length > 0) continue;
      const r = el.getBoundingClientRect();
      if (!visible(r) || r.width < 4 || r.height < 4) continue;
      unreadable++;
      push(r);
    }
  }
  const wrap = document.createElement('div');
  wrap.id = '__spark_redact__';
  wrap.style.cssText =
    'position:fixed;inset:0;z-index:2147483647;pointer-events:none;';
  for (const b of boxes) {
    const d = document.createElement('div');
    d.style.cssText = 'position:fixed;background:#000;border-radius:2px;' +
      'left:' + b[0] + 'px;top:' + b[1] + 'px;' +
      'width:' + b[2] + 'px;height:' + b[3] + 'px;';
    wrap.appendChild(d);
  }
  document.body.appendChild(wrap);
  // 覆盖自检: 拿遮罩元素**渲染后**的矩形去复核文本矩形, 而不是拿 boxes 数组自证。
  // position:fixed 落在被 transform/filter 的祖先里时会相对那个祖先定位 —— 那时
  // 数组是对的、屏幕上是错的。这一步把这种「看着加了遮罩其实没盖上」抓出来。
  //
  // 2026-11-19 再收紧: 旧版只复核前 400 个文本矩形、且只判矩形**中心点**落没落在
  // 某个遮罩里 —— 超出 400 的那批完全不验证, 而矩形中心被盖住、上下却露出同样算通过。
  // 现在把遮罩矩形栅格化到 2px 网格, 对**每一个**文本矩形要求(内缩 1px 容忍亚像素
  // 圆整)其覆盖范围内的网格点**全部**被遮罩占住。工作量用预算兜底: 预算耗尽不是
  // 「通过」而是 unverified —— 脱敏链上「没法证明盖住了」必须等于「放弃这次截图」。
  const maskRects = [];
  for (const d of wrap.children) maskRects.push(d.getBoundingClientRect());
  const CELL = 2, INSET = 1;
  const gw = Math.max(1, Math.ceil(vw / CELL)), gh = Math.max(1, Math.ceil(vh / CELL));
  const grid = new Uint8Array(gw * gh);
  for (const m of maskRects) {
    const x0 = Math.max(0, Math.floor(m.left / CELL));
    const x1 = Math.min(gw - 1, Math.ceil(m.right / CELL) - 1);
    const y0 = Math.max(0, Math.floor(m.top / CELL));
    const y1 = Math.min(gh - 1, Math.ceil(m.bottom / CELL) - 1);
    if (x1 < x0 || y1 < y0) continue;              // 整块在视口外
    for (let y = y0; y <= y1; y++) {
      const row = y * gw;
      for (let x = x0; x <= x1; x++) grid[row + x] = 1;
    }
  }
  let uncovered = 0, unverified = 0, budget = 4000000;
  const fullyCovered = (r) => {
    let x0 = Math.floor((r.left + INSET) / CELL);
    let x1 = Math.floor((r.right - INSET) / CELL);
    let y0 = Math.floor((r.top + INSET) / CELL);
    let y1 = Math.floor((r.bottom - INSET) / CELL);
    if (x1 < x0 || y1 < y0) {                      // 比 INSET 还小的碎块: 退回中心点
      x0 = x1 = Math.floor((r.left + r.width / 2) / CELL);
      y0 = y1 = Math.floor((r.top + r.height / 2) / CELL);
    }
    x0 = Math.max(0, x0); x1 = Math.min(gw - 1, x1);
    y0 = Math.max(0, y0); y1 = Math.min(gh - 1, y1);
    if (x1 < x0 || y1 < y0) return true;           // 整块在视口外, 看不见不算漏
    for (let y = y0; y <= y1; y++) {
      const row = y * gw;
      for (let x = x0; x <= x1; x++) {
        if (budget-- <= 0) return null;            // 预算耗尽, 交给调用方记 unverified
        if (!grid[row + x]) return false;
      }
    }
    return true;
  };
  for (let i = 0; i < textRects.length; i++) {
    const ok = fullyCovered(textRects[i]);
    if (ok === null) { unverified = textRects.length - i; break; }
    if (!ok) uncovered++;
  }
  return {
    masked: boxes.length,
    textNodes: textRects.length,
    uncovered: uncovered,
    unverified: unverified,
    unreadable: unreadable,
    keptFlame: keptFlame
  };
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
    """注入脱敏遮罩; 只返回 True 的条件是**真的遮到了东西且复核通过**。

    "脚本没抛异常" 不等于"遮住了": 页面还在骨架屏、选择器改版、或者落在一个
    空白页上时, 遍历找不到任何文本节点和头像, 返回 0 —— 以前这里照样 return
    True, 于是 /_redacted_screenshot 拍下一张**完全没打码**的整屏, 直接发给外部
    模型。脱敏链路上"看着成功其实什么都没做"是最坏的一种失败: 它静默。

    所以这里有四道闸门, 任何一道不过就放弃截图, 让调用方退回 DOM 取证:
      1) 脚本必须回报一个结构化结果
         (masked/textNodes/uncovered/unverified/unreadable/keptFlame);
      2) masked 必须 > 0 —— 一处都没遮说明这次遍历根本没找到东西;
      3) uncovered 必须 == 0 —— 遮罩元素**渲染后**的矩形必须覆盖每个文本矩形
         (2px 栅格逐点判定, 不是只看中心点), 最常见的原因是 position:fixed 的
         遮罩落在了被 transform/filter 的祖先里, 于是"数组里加了、屏幕上没盖";
      4) unverified 必须 == 0 —— 覆盖复核有计算预算, 预算耗尽时剩下的文本块是
         "没验证过"而不是"盖住了"。旧版直接把超出 400 个的部分当通过, 等于在
         最该保守的地方乐观。脱敏链上"没法证明盖住了"必须等于"放弃这次截图"。
    """
    try:
        n = page.evaluate(_JS_REDACT)
    except Exception as exc:
        log(f"[vision] 注入脱敏遮罩失败: {exc}")
        return False
    if not isinstance(n, dict):
        log(f"[vision] 脱敏脚本返回了非结构化结果 {n!r}, 无法确认覆盖, 放弃本次截图")
        return False
    try:
        masked = int(n.get("masked", 0))
        uncovered = int(n.get("uncovered", 0))
        unverified = int(n.get("unverified", 0))
        unreadable = int(n.get("unreadable", 0))
        text_nodes = int(n.get("textNodes", 0))
        kept = int(n.get("keptFlame", 0))
    except (TypeError, ValueError):
        log(f"[vision] 脱敏脚本的计数不是整数 {n!r}, 无法确认覆盖, 放弃本次截图")
        return False
    if masked <= 0:
        log("[vision] 脱敏遮罩命中 0 处, 无法确认覆盖, 放弃本次截图")
        return False
    if uncovered > 0:
        log(
            f"[vision] 有 {uncovered} 个文本块没被遮罩盖住"
            "(常见原因: 遮罩落在 transform/filter 祖先里, fixed 定位偏移), "
            "放弃本次截图"
        )
        return False
    if unverified > 0:
        log(
            f"[vision] 有 {unverified} 个文本块的覆盖没能在预算内复核完"
            "(会话列表过大), 无法证明已盖住, 放弃本次截图"
        )
        return False
    log(
        f"[vision] 已注入脱敏遮罩 {masked} 处"
        f"(文本节点 {text_nodes}, 读不到的组件 {unreadable}, 保留火花图标 {kept}), "
        "复核每个文本矩形都被盖住"
    )
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
