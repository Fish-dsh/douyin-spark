"""发送链路：表情包发送、文字发送、发送确认、好友定位点击。"""

from __future__ import annotations

from playwright.sync_api import Page

from logutil import log

from .debug import dump_page
from .finder import find

try:
    from . import perf
except ImportError:  # 以顶层模块方式加载时(src/ 在 sys.path 上)
    import perf  # type: ignore[no-redef]

try:
    from . import events
except ImportError:  # 以顶层模块方式加载时(src/ 在 sys.path 上)
    import events  # type: ignore[no-redef]

try:
    from . import vision
except ImportError:  # 以顶层模块方式加载时(src/ 在 sys.path 上)
    import vision  # type: ignore[no-redef]

try:
    from . import vision_cv
except ImportError:
    import vision_cv  # type: ignore[no-redef]

INPUT_SELECTORS = [
    "[contenteditable='true']",
    "div[contenteditable]:not([contenteditable='false'])",
    "[data-e2e*='input']",
    "[class*='input'] textarea",
    "textarea",
    "input[type='text']",
]

SEND_BUTTON_SELECTORS = [
    # 2026-11-01 板端 DOM 实测: 发送键是 <svg class="messageMsgInputpublishBtn
    # e2e-send-msg-btn">, 既没有「发送」文字也没有 data-e2e 属性 —— 原来的五条
    # 选择器一条都命中不了, 这就是文字层每晚「未找到发送按钮, 改用 Enter」的原因。
    "svg[class*='publishBtn']",
    "[class*='e2e-send-msg-btn']",
    "button:has-text('发送')",
    "[data-e2e*='send']",
    "button[class*='send']",
    "[class*='send'] button",
    "svg[data-e2e*='send']",
]

_JS_FIND_SEND_BTN = """
() => {
  const box = document.querySelector('[contenteditable="true"]')
    || document.querySelector('textarea');
  if (!box) return {cands: []};
  const ir = box.getBoundingClientRect();
  if (ir.width <= 0 || ir.height <= 0) return {cands: []};
  const cy = ir.y + ir.height / 2;
  const seen = new Set();
  const cands = [];
  for (const el of document.querySelectorAll('button, span, div, svg, img, i')) {
    const r = el.getBoundingClientRect();
    if (r.width < 14 || r.width > 72 || r.height < 14 || r.height > 72) continue;
    if (Math.abs(r.y + r.height / 2 - cy) > Math.max(ir.height, 28)) continue;
    if (r.x < ir.right - 6) continue;              // 必须在输入框右边界之外
    if (r.right > window.innerWidth + 2) continue;
    const meta = ((el.getAttribute('aria-label') || '') + '|' +
                  (el.getAttribute('title') || '') + '|' +
                  (el.getAttribute('data-e2e') || '') + '|' +
                  (el.className || '').toString()).toLowerCase();
    // 表情/图片/附件/提醒: 点了会弹面板, 不是发送键
    if (/表情|emoji|图片|image|photo|相册|upload|file|附件|mention/.test(meta)) continue;
    const x = Math.round(r.x + r.width / 2);
    const y = Math.round(r.y + r.height / 2);
    // 命中校验: 点下去真正吃到事件的是不是它(被浮层/兄弟节点盖住就跳过)
    const hit = document.elementFromPoint(x, y);
    if (!hit) continue;
    if (hit !== el && !el.contains(hit) && !hit.contains(el)) continue;
    const key = x + ',' + y;
    if (seen.has(key)) continue;
    seen.add(key);
    cands.push({x: x, y: y, tag: el.tagName,
                cls: (el.className || '').toString().slice(0, 40)});
  }
  cands.sort((a, b) => (b.x - a.x) || (b.y - a.y));   // 最靠右的优先
  return {cands: cands.slice(0, 3)};
}
"""


_JS_BOX_EMPTY = """
() => {
  const box = document.querySelector('[contenteditable="true"]')
    || document.querySelector('textarea[class*="input"]')
    || document.querySelector('textarea');
  if (!box) return false;
  const raw = box.value !== undefined ? (box.value || '') : (box.textContent || '');
  const cleaned = raw.replace(/[\\s\\u200b\\u200c\\u200d\\ufeff\\u200e\\u200f]/g, '');
  return cleaned === '';
}
"""

# 2026-10-07 审计修正: 原 _JS_LAST_MSG 只回「最后一条文本」, 于是 confirm_send 无法
# 区分「刚发出去的那条」与「昨晚残留的同一条」(固定文案 续火花 天天相同, 必然残留)。
# 现把消息列表的**尾部证据**一次性读全: 气泡数 + 最后两条文本。气泡数是否增加是
# 「这条消息是新出现的」的本地可证依据; 尾部两条一起比, 是因为列表可能虚拟渲染
# (新消息挂尾、旧消息摘头 → 数量不变但窗口会移动)。
_JS_MSG_TAIL = """
() => {
  const out = {n: 0, last: '', prev: ''};
  let list = document.querySelector(
    '[class*="messageMessageListlist"], [class*="messageMessageList"], [class*="MessageList"], [data-e2e*="message-list"]');
  if (!list) {
    // 结构兜底: 类名失效时取右侧最大滚动容器(消息区), 排除左侧会话栏
    let best = null;
    for (const d of document.querySelectorAll('div')) {
      const st = window.getComputedStyle(d);
      if (st.overflowY !== 'auto' && st.overflowY !== 'scroll') continue;
      const r = d.getBoundingClientRect();
      if (d.scrollHeight <= d.clientHeight + 50 || r.height < 200) continue;
      if (r.left < window.innerWidth * 0.2) continue;
      const score = d.scrollHeight * r.width;
      if (!best || score > best.score) best = {el: d, score};
    }
    list = best ? best.el : null;
  }
  if (!list) return out;
  const textOf = (n) => {
    const parts = [];
    for (const el of n.querySelectorAll('img[alt], img[title]')) {
      const alt = el.getAttribute('alt') || el.getAttribute('title');
      if (alt) parts.push(alt);
    }
    const txtEl = n.querySelector(
      '[class*="TextMessageTexttextInnerContent"], [class*="MessageItemTextbubbleTextContent"], [class*="bubbleText"], [data-e2e*="message"]');
    if (txtEl && (txtEl.innerText || '').trim()) parts.push(txtEl.innerText.trim());
    const raw = (n.innerText || n.textContent || '').trim();
    if (raw && !parts.length) parts.push(raw);
    return parts.join(' ');
  };
  let boxes = [...list.querySelectorAll(
    '[class*="messageMessageBoxmessageBox"], [class*="messageMessageBox"], [class*="MessageBox"], [class*="bubble"]')];
  if (!boxes.length) {
    boxes = [...list.children].filter(e => (e.innerText || '').trim() || e.querySelector('img[alt], img[title]'));
  }
  out.n = boxes.length;
  if (boxes.length) out.last = textOf(boxes[boxes.length - 1]);
  if (boxes.length > 1) out.prev = textOf(boxes[boxes.length - 2]);
  return out;
}
"""


def _norm_tail(r) -> tuple:
    """尾部证据归一化 → (气泡数|None, 最后一条, 倒数第二条)。

    返回 None 表示「数量读不到」(旧版页面/测试夹具给的是裸字符串), 调用方据此
    把判据降级为纯文本比对, 而不是把「读不到」当成「没增加」。
    """
    if isinstance(r, dict):
        n = r.get("n")
        count = (
            int(n) if isinstance(n, (int, float)) and not isinstance(n, bool) else None
        )
        return count, str(r.get("last") or ""), str(r.get("prev") or "")
    if isinstance(r, str):
        return None, r, ""
    return None, "", ""


_JS_LAST_BUBBLE_SELF = """
() => {
  let list = document.querySelector(
    '[class*="messageMessageListlist"], [class*="messageMessageList"], [class*="MessageList"], [data-e2e*="message-list"]');
  if (!list) return null;
  let boxes = [...list.querySelectorAll(
    '[class*="messageMessageBoxmessageBox"], [class*="messageMessageBox"], [class*="MessageBox"], [class*="bubble"]')];
  if (!boxes.length) {
    boxes = [...list.children].filter(e => (e.innerText || '').trim() || e.querySelector('img[alt], img[title]'));
  }
  if (!boxes.length) return null;
  const box = boxes[boxes.length - 1];
  const br = box.getBoundingClientRect();
  const lr = list.getBoundingClientRect();
  if (!br.width || !lr.width) return null;
  return (br.x + br.width / 2) > (lr.x + lr.width / 2);
}
"""


def last_bubble_is_self(page: Page):
    """最后一条可见消息是否我方发出; 读不到返回 None。

    2026-11-01 会话50: 续火花是**双方互发同一个表情**, 而表情层的成功判据是
    「最后一条文本里含表情名」——好友回一个同样的表情也会命中。此函数只提供
    旁证并写入事件, **不参与判定**(改判定会改变今晚的发送行为)。
    """
    try:
        v = page.evaluate(_JS_LAST_BUBBLE_SELF)
    except Exception:
        return None
    return v if isinstance(v, bool) else None


def read_tail(page: Page) -> tuple:
    """读消息列表尾部证据; 任何异常都归一成 (None, "", "") 交由调用方降级处理。"""
    try:
        return _norm_tail(page.evaluate(_JS_MSG_TAIL))
    except Exception:
        return None, "", ""


_JS_INJECT_AND_SEND = """
({text}) => {
  const box = document.querySelector('[contenteditable="true"]')
    || document.querySelector('textarea')
    || [...document.querySelectorAll('div')].find(e => e.isContentEditable);
  if (!box) return {ok: false, reason: 'no_box'};
  box.focus();
  try { box.click(); } catch (e) {}
  // 全选清空
  const sel = window.getSelection();
  const range = document.createRange();
  range.selectNodeContents(box);
  sel.removeAllRanges();
  sel.addRange(range);
  // 插入文字
  let inserted = false;
  try { inserted = document.execCommand('insertText', false, text); } catch (e) { inserted = false; }
  if (!inserted || !(box.textContent || '').includes(text)) {
    box.textContent = text;
  }
  // 2026-11-01 板端实测: 注入后输入框里会多出一个前导换行, 发出去气泡开头空一行
  // (消息原文是 "\n续火花"), 看起来像空白消息。这里把首尾的空 <br>/空块/纯空白
  // 文本节点清掉 —— 只动首尾, 不碰正文, 也不改任何判定(confirm 用的 innerText.trim()
  // 本来就忽略首尾空白, 所以行为不变, 只是气泡变整齐)。
  const trimEdge = () => {
    for (let i = 0; i < 8; i++) {
      const f = box.firstChild;
      if (!f) break;
      if (f.nodeName === 'BR') { box.removeChild(f); continue; }
      if (f.nodeType === 3 && !String(f.textContent || '').trim()) { box.removeChild(f); continue; }
      if (f.nodeType === 1 && !(f.textContent || '').trim() && !f.querySelector('img')) {
        box.removeChild(f);
        continue;
      }
      break;
    }
    for (let i = 0; i < 8; i++) {
      const l = box.lastChild;
      if (!l) break;
      if (l.nodeName === 'BR') { box.removeChild(l); continue; }
      if (l.nodeType === 3 && !String(l.textContent || '').trim()) { box.removeChild(l); continue; }
      if (l.nodeType === 1 && !(l.textContent || '').trim() && !l.querySelector('img')) {
        box.removeChild(l);
        continue;
      }
      break;
    }
  };
  trimEdge();
  // 清空首尾空行后必须复核: 正文还在才继续, 否则退回直接写 textContent
  // (宁可气泡难看, 也不能把要发的字弄丢)。
  if (!(box.innerText || '').replace(/\u200b/g, '').includes(text)) {
    box.textContent = text;
  }
  // 触发 input 事件(抖音依赖此事件更新内部状态)
  box.dispatchEvent(new Event('input', { bubbles: true }));
  box.dispatchEvent(new Event('change', { bubbles: true }));
  box.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: text }));
  // 查找并点击发送按钮(比 keyboard.press('Enter') 更可靠)
  const sendBtn = Array.from(document.querySelectorAll('button, span, div, svg'))
    .find(e => {
      const t = (e.textContent || '').trim();
      return t === '发送' || t === 'send' || t === '发送 »';
    });
  let btnClicked = false;
  if (sendBtn) {
    try { sendBtn.click(); btnClicked = true; }
    catch(e) {}
  }
  return { ok: true, btnClicked };
}
"""


def press_enter(page: Page) -> None:
    """发送文本后按 Enter 触发发送 (Lightpanda 无可见发送按钮)。"""
    try:
        page.keyboard.press("Enter")
        log("[发送] 已按 Enter 发送")
    except Exception as exc:
        log(f"[发送] 按 Enter 失败: {exc}")


def _click_geometry_send_button(page: Page) -> str:
    """按几何位置点输入行最右侧的图标按钮; 命中返回坐标描述, 全失败返回 ""。

    2026-11-01 板端取证: 抖音发送键是纯图标(没有「发送」文字, 也没有 data-e2e),
    SEND_BUTTON_SELECTORS 一条都不命中 → 退回 keyboard.press('Enter'), 而 JS 重写
    contenteditable 之后 Enter 不再触发发送 → send_message 依旧报「生效」, 真正的
    送达全靠 cv_fallback 截图点 (755,561)。这里补一条不依赖文案/类名的路径:
    取输入框右侧同高的候选 → elementFromPoint 校验命中 → 点它, 用「输入框是否清空」
    验证是否真的发出; 点错(例如弹出了表情面板)就按 Escape 关掉再试下一个候选。
    """
    try:
        got = page.evaluate(_JS_FIND_SEND_BTN)
    except Exception:
        return ""
    cands = got.get("cands") if isinstance(got, dict) else None
    if not isinstance(cands, list):
        return ""
    for cand in cands:
        if not isinstance(cand, dict) or "x" not in cand or "y" not in cand:
            continue
        try:
            page.mouse.click(cand["x"], cand["y"])
        except Exception:
            continue
        page.wait_for_timeout(perf.ms("ui_quiet_ms"))
        try:
            if page.evaluate(_JS_BOX_EMPTY):
                return f"({cand['x']},{cand['y']} {cand.get('tag', '')})"
        except Exception:
            pass
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        page.wait_for_timeout(perf.ms("escape_settle_ms"))
    return ""


def try_send_button_or_enter(
    page: Page, *, attempt: str | None = None, friend: str = ""
) -> str:
    """触发发送; 返回做了什么的描述(调用方写进日志, 不再谎报「已发送」)。

    顺序: 几何定位(命中校验) → 具名选择器 → Enter 兜底。Enter 只作为最后手段,
    因为它在本项目历史上多次「按了但没发出」。
    """
    aid = attempt or events.attempt_id()
    hit = _click_geometry_send_button(page)
    if hit:
        log(f"[发送] 已点击发送按钮(几何命中: {hit})")
        events.emit(
            "send_button",
            "click",
            "ok",
            friend=friend,
            attempt=aid,
            verified=None,
            extra={"via": "geometry"},
        )
        return f"已点击发送按钮(几何 {hit})"
    for sel in SEND_BUTTON_SELECTORS:
        try:
            b = page.locator(sel).first
            b.wait_for(state="visible", timeout=perf.ms("btn_visible_ms"))
            b.click(timeout=perf.ms("click_small_ms"))
            # 2026-11-01: 具名选择器点下去不一定真发出去(板端探针实测: 点完输入框
            # 还留着文字, 消息列表也不变)。用「输入框是否被清空」复核, 没清空就关掉
            # 可能被误打开的弹层, 交给下一个候选/Enter —— 宁可多试一步, 不谎报已发送。
            page.wait_for_timeout(perf.ms("ui_quiet_ms"))
            try:
                cleared = page.evaluate(_JS_BOX_EMPTY)
            except Exception:
                cleared = True
            if not cleared:
                log(f"[发送] 点击 {sel} 后输入框未清空, 视为未生效, 试下一个候选")
                try:
                    page.keyboard.press("Escape")
                except Exception:
                    pass
                page.wait_for_timeout(perf.ms("escape_settle_ms"))
                continue
            log(f"[发送] 已点击发送按钮(选择器: {sel})")
            events.emit(
                "send_button",
                "click",
                "ok",
                friend=friend,
                attempt=aid,
                verified=None,
                extra={"via": "selector", "sel": sel[:40]},
            )
            return f"已点击发送按钮(选择器 {sel})"
        except Exception:
            continue
    log("[发送] 未找到发送按钮, 改用 Enter")
    page.keyboard.press("Enter")
    events.emit(
        "send_button",
        "click",
        "unknown",
        friend=friend,
        attempt=aid,
        verified=None,
        error_class="no_send_button",
    )
    return "已按 Enter(未找到发送按钮, 未验证)"


def send_message(
    page: Page,
    text: str,
    *,
    attempt: str | None = None,
    parent: str | None = None,
    friend: str = "",
    account: str = "",
):
    """发送一条消息; 返回 (是否已尝试发送, 生效策略描述)。

    优先使用 JS 注入 + 发送按钮点击(比 keyboard.type 更可靠, 尤其在表情按钮点击后焦点漂移的场景)。

    attempt 由调用方与 confirm_send 共用 —— 同一次尝试里先 ok 后被验证否掉,
    才统计得出 false_positive(谎报)。
    """
    aid = attempt or events.attempt_id()
    # 先确保输入框可见可用
    box = find(page, INPUT_SELECTORS)
    if box is None:
        events.emit(
            "input",
            "find_box",
            "failed",
            friend=friend,
            account=account,
            attempt=aid,
            parent=parent,
            error_class="no_input_box",
        )
        return False, "未定位到输入框"

    # 策略A: JS 注入 + 发送按钮点击 (最可靠)
    try:
        result = page.evaluate(_JS_INJECT_AND_SEND, {"text": text})
        events.emit(
            "input",
            "inject",
            "ok" if result.get("ok") else "failed",
            friend=friend,
            account=account,
            attempt=aid,
            verified=None,
            extra={
                "btn_clicked": bool(result.get("btnClicked")),
                "reason": result.get("reason"),
            },
        )
        if result.get("ok"):
            if result.get("btnClicked"):
                log("[发送] 策略A 生效: JS注入+发送按钮点击")
                return True, "策略A 生效: JS注入+发送按钮点击"
            log("[发送] 策略A 生效: JS注入+文本(input事件已触发)")
            # JS 找不到含「发送」文案的按钮是常态(纯图标), 走命中校验的几何点击
            act = try_send_button_or_enter(page, attempt=aid, friend=friend)
            return True, f"策略A 生效: JS注入+{act}"
        else:
            log(f"[发送] 策略A JS注入失败: {result.get('reason', 'unknown')}")
    except Exception as exc:
        log(f"[发送] 策略A JS注入异常: {exc}")
        events.emit(
            "input",
            "inject",
            "failed",
            friend=friend,
            account=account,
            attempt=aid,
            error_class=type(exc).__name__,
        )

    # 策略B: 点击输入框 + keyboard.type (兜底)
    try:
        box.click(timeout=perf.ms("input_click_ms"))
        page.wait_for_timeout(perf.ms("type_settle_ms"))
        log("[发送] 策略B: 已点击输入框, 开始键盘输入")
        page.keyboard.type(text, delay=30)
        act = try_send_button_or_enter(page, attempt=aid, friend=friend)
        return True, f"策略B 生效: click + keyboard.type + {act}"
    except Exception as exc:
        log(f"[发送] 策略B 失败: {exc}")
        events.emit(
            "input",
            "type",
            "failed",
            friend=friend,
            account=account,
            attempt=aid,
            error_class=type(exc).__name__,
        )

    events.emit(
        "text",
        "send",
        "failed",
        friend=friend,
        account=account,
        attempt=aid,
        verified=None,
        error_class="strategy_ab_failed",
    )
    return False, "策略A/B均失败"


def _evidence(base: tuple, n1, last1: str, prev1: str) -> tuple:
    """把「发送前基线」与「当前尾部」比成一句结论 → (结论, 说明)。

    结论 True=确有新增消息 / False=毫无新增 / None=读不到, 无法判定(调用方须降级,
    绝不能把「读不到」当成「没发出去」)。数量增加是第一证据; 数量不变但**尾部窗口
    移动**也算, 因为消息列表可能虚拟渲染(挂尾摘头, 数量恒定)。
    """
    n0, last0, prev0 = base
    if n0 is None or n1 is None:
        return None, f"尾部证据不可读(基线数量={n0}, 当前数量={n1})"
    if n0 == 0 and n1 == 0:
        # 一个已渲染的消息列表不可能一条气泡都没有 —— 数量恒 0 = 列表容器没定位到
        # (类名失效/页面未挂载)。此时既不能证明新增、也不能证明没新增, 只能降级。
        return None, "消息列表读不到气泡(数量恒 0)"
    if n1 < n0:
        # 数量收缩 = 列表被重渲染/虚拟滚动换了窗口, 基线已不可比(不当成「没新增」失败)
        return None, f"气泡数收缩 {n0}->{n1}(列表重渲染, 基线不可比)"
    if n1 > n0:
        return True, f"新增气泡 {n0}->{n1} (尾部 {last1[:24]!r})"
    if (last1, prev1) != (last0, prev0):
        return True, f"尾部窗口移动 {last0[:16]!r}->{last1[:16]!r} (数量 {n1})"
    return False, f"无任何新增(数量仍 {n0}, 尾部仍 {last1[:24]!r})"


def tail_evidence(before: tuple, page: Page) -> tuple:
    """读一次当前尾部并与基线比较; 供表情层这类「自带成功判据」的路径复核用。"""
    return _evidence(before, *read_tail(page))


def confirm_send(
    page: Page,
    text: str,
    retries: int = 5,
    baseline: tuple | None = None,
    *,
    attempt: str | None = None,
    friend: str = "",
    account: str = "",
):
    """发送后确认; 返回 (是否确认成功, 描述)。

    重试多次以提高准确率: 有时消息刚发出, DOM 尚未更新。首拍等待与轮询间隔跟性能
    档位走(perf.confirm_first_ms / confirm_poll_ms), 板端档 300ms + 每轮 1s。

    **baseline 是这道确认有没有用的分水岭**(2026-10-07 审计): 文案固定为「续火花」,
    所以昨晚那条永远还挂在列表尾部。不带基线时只能判「尾部含文案/输入框已清空」,
    二者在「今晚一次都没发出去」时同样成立 → 假成功 → 火花断了没人知道。调用方应在
    动手前用 read_tail(page) 取基线传进来。无基线(老调用方/测试)时保持旧行为, 并在
    说明里标注降级, 让 strat_stats 看得见降级比例, 而不是把它当成验证过。

    每个 return 前一行的 events.emit 是「凭什么认为发好了」的唯一机读证据;
    verified=None 表示降级放行(不是验证过), False 表示确证没发出去。
    """
    aid = attempt or events.attempt_id()
    # 性能(2026-11-01): 原实现无条件先睡 1.5s 再查, 而判据本身是硬信号(气泡新增/
    # 尾部窗口移动), 早查一次不会引入误判。改为 300ms 首拍 + 之后每轮 1s,
    # 总窗口仍覆盖原 5 轮, 只是不再白等。
    page.wait_for_timeout(perf.ms("confirm_first_ms"))
    for i in range(retries):
        if i > 0:
            page.wait_for_timeout(perf.ms("confirm_poll_ms"))
        try:
            empty = bool(page.evaluate(_JS_BOX_EMPTY))
            n1, last, prev = _norm_tail(page.evaluate(_JS_MSG_TAIL))
            if baseline is None:
                ev, ev_msg = None, "无发送前基线"
            else:
                ev, ev_msg = _evidence(baseline, n1, last, prev)

            if ev is None:
                # 判据不足以证明新增 → 退回旧口径(不放行任何「读得到但没证据」的自信)
                if empty and last and text in last:
                    events.emit(
                        "text",
                        "verify",
                        "ok",
                        friend=friend,
                        account=account,
                        attempt=aid,
                        verified=None,
                        extra={"degraded": ev_msg, "text_match": True},
                    )
                    return (
                        True,
                        (
                            f"确认成功(降级 {ev_msg}): 输入框清空且最新消息匹配"
                            f" (最新消息: {last[:30]!r})"
                        ),
                    )
                if empty:
                    events.emit(
                        "text",
                        "verify",
                        "ok",
                        friend=friend,
                        account=account,
                        attempt=aid,
                        verified=None,
                        extra={"degraded": ev_msg, "text_match": False},
                    )
                    return (
                        True,
                        f"确认成功(降级 {ev_msg}): 输入框已清空 (最新消息: {last[:30]!r})",
                    )
                if last and text in last:
                    events.emit(
                        "text",
                        "verify",
                        "ok",
                        friend=friend,
                        account=account,
                        attempt=aid,
                        verified=None,
                        extra={"degraded": ev_msg, "text_match": True},
                    )
                    return (
                        True,
                        (
                            f"确认成功(降级 {ev_msg}): 最新消息包含文案"
                            f" (最新消息: {last[:30]!r})"
                        ),
                    )
            elif ev is True:
                if text in last:
                    events.emit(
                        "text",
                        "verify",
                        "ok",
                        friend=friend,
                        account=account,
                        attempt=aid,
                        verified=True,
                        extra={"ev": ev_msg, "text_match": True},
                    )
                    return True, f"确认成功: {ev_msg}, 文案相符"
                if empty:
                    events.emit(
                        "text",
                        "verify",
                        "ok",
                        friend=friend,
                        account=account,
                        attempt=aid,
                        verified=True,
                        extra={"ev": ev_msg, "text_match": False},
                    )
                    return True, f"确认成功: {ev_msg}, 输入框已清空"
                log(f"[确认] 已有新增消息但文案/输入框对不上: {ev_msg}")
            elif empty:
                # 读得到、证明毫无新增、输入框却又不是还留着字 —— 字是被抖音清的还是
                # 压根没打进去? 无法区分, 但没有「新增」这条硬证据就不能算发出。
                log(f"[确认] 输入框已空但{ev_msg}")

            # 到这里 = 没有「确证新增」。继续轮询(新气泡可能渲染晚), 末轮仍无证据即判失败。
            if i < retries - 1:
                log(
                    f"[确认] 第{i + 1}次检查未确认({ev_msg}), 等待后重试"
                    f" (最新消息: {last[:30]!r})"
                )
            else:
                preview = last[:30] if last else "(空)"
                log(f"[确认] {retries}次检查均未确认({ev_msg}) (最新消息: {preview!r})")
                events.emit(
                    "text",
                    "verify",
                    "failed",
                    friend=friend,
                    account=account,
                    attempt=aid,
                    verified=False,
                    extra={"ev": ev_msg},
                )
                return False, f"未能确认发送 ({ev_msg}, 最新消息: {preview!r})"
        except Exception as exc:
            if i < retries - 1:
                log(f"[确认] 第{i + 1}次检查异常: {exc}, 重试")
            else:
                events.emit(
                    "text",
                    "verify",
                    "failed",
                    friend=friend,
                    account=account,
                    attempt=aid,
                    error_class=type(exc).__name__,
                )
                return False, f"确认发送时出错: {exc}"
    events.emit(
        "text",
        "verify",
        "unknown",
        friend=friend,
        account=account,
        attempt=aid,
        error_class="timeout",
    )
    return False, "确认发送超时"


def xpath_text_literal(text: str) -> str:
    sep = '"' if "'" in text else "'"
    return sep + text.replace(sep, sep + sep) + sep


def _scroll_conversation_list(page: Page) -> None:
    """滚动页面上所有可滚动容器, 促使列表渲染更深处的会话条目。"""
    try:
        page.evaluate(
            """() => {
            const els = [...document.querySelectorAll('*')].filter(
                (e) => e.scrollHeight > e.clientHeight + 50 && e.clientHeight > 200
            );
            els.forEach((e) => e.scrollBy(0, e.clientHeight * 0.7));
        }"""
        )
    except Exception:
        pass


def _vision_click_friend(page: Page, name: str):
    """文本策略全失败时的视觉兜底: 视觉粗定位 + DOM 昵称核对, 一致才点击。

    只点「DOM 现场读出的真实昵称 == name」的那一行, 故坐标偏一行也只会落到相邻的
    真实好友上并被昵称核对挡下 —— **不会点错好友**。未启用/未命中返回 None。
    """
    if not vision.vision_enabled():
        return None
    row = vision.find_row(page, name)
    if not row:
        return None
    try:
        page.mouse.click(row["x"], row["y"])
    except Exception as exc:
        log(f"[定位] 视觉兜底点击『{name}』失败: {exc}")
        return None
    log(f"[定位] 视觉兜底: DOM 校验昵称『{name}』一致, 点击 ({row['x']},{row['y']})")
    return row


def click_friend(
    page: Page,
    name: str,
    rounds: int | None = None,
    round_wait_ms: int | None = None,
):
    """按昵称定位并点击好友; 返回点击到的 locator, 失败返回 None。

    三策略: locator("text=") 精确文本 / xpath contains(text()) 模糊文本 /
    title·data 属性兜底。会话列表为渐进渲染, 目标条目(尤其排在深处时)
    可能晚于首条数秒甚至数十秒才挂载, 故整组策略轮询多轮, 轮间滚动列表。
    三策略全失败后再走一次视觉兜底(取版式变化时的最后活路)。
    """
    # 轮次与轮间等待跟性能档位走(板端 4 轮 × 2500ms, 桌面档 3 轮 × 1200ms)。
    if rounds is None:
        rounds = int(perf.get("click_friend_rounds"))
    if round_wait_ms is None:
        round_wait_ms = perf.ms("click_friend_round_ms")
    strategies = [
        ("get_by_text(exact)", page.get_by_text(name, exact=True).first),
        (
            "xpath 文本匹配",
            page.locator(f"xpath=//*[text()={xpath_text_literal(name)}]").first,
        ),
        (
            "title/data 属性",
            page.locator(f"[title='{name}'], [data-name='{name}']").first,
        ),
    ]
    for rnd in range(1, rounds + 1):
        for label, loc in strategies:
            try:
                loc.wait_for(state="attached", timeout=perf.ms("friend_attach_ms"))
                loc.scroll_into_view_if_needed(timeout=perf.ms("friend_attach_ms"))
                loc.click(timeout=perf.ms("friend_click_ms"))
                log(f"[定位] 策略『{label}』命中并点击了『{name}』(第{rnd}轮)")
                return loc
            except Exception as exc:
                log(f"[定位] 第{rnd}轮 策略『{label}』未命中: {exc}")
        if rnd < rounds:
            _scroll_conversation_list(page)
            page.wait_for_timeout(round_wait_ms)
    clicked = _vision_click_friend(page, name)
    if clicked is not None:
        return clicked
    dump_page(page)
    return None


# 2026-10-06 (ChatGPT 评审 #1/#3 落地): 发送前核对「打开的确实是目标会话」。
# 三态: 名字出现在右侧面板=确认; 右侧面板已渲染但找不到名字=冲突(拦截,
# 假发送/点错行的最后闸门); 面板读不懂(改版/未渲染)=不改变现状, 放行。
# 2026-10-06 评审: 不用屏幕坐标(x>=380 这类, 列表宽度一变即失效), 改用
# DOM 包含关系——「当前会话面板」定义为消息输入框向上找到的第一个宽祖先,
# 目标昵称只需出现在该祖先内。祖先取宽不取窄, 判读偏差只会倒向放行而非误拦。
# 2026-10-07 板端实测把这条判据收紧: 「第一个够宽的祖先」实测停在输入行容器上
# (componentsRightPanel 之外, 0.41 视口宽 × 0.07 高, 叶子文本 0 字), 于是 panel_len
# 恒为 0 → 昨晚 17/17 次核对全部「面板未渲染或读不到文本, 无法判定, 放行」= 这道闸从
# 上线起就是瞎的。实测祖先链: 输入行(0.41w/0.07h, leaf0) → notHeaderArea(0.62w/0.9h,
# leaf102, 不含标题栏昵称) → RightPanelwrapper(0.62w/1.0h, leaf106, **昵称命中 1**) →
# 整页(1.0w, leaf281, 命中 2 = 把会话列表里所有昵称都算进来, 假确认)。故改为在
# 「够宽但不整页」的祖先里取**最高**的那个: 既不会窄到只剩输入框, 也不会宽到吞掉列表。
_JS_CHAT_IDENTITY = """(name) => {
    const out = {box: false, hits: 0, panel_len: 0, top_texts: []};
    try {
        const visible = (e) => {
            const r = e.getBoundingClientRect();
            return r.width > 0 && r.height > 0;
        };
        const ed = Array.from(document.querySelectorAll('[contenteditable="true"]'))
            .find(visible);
        if (!ed) return out;
        out.box = true;
        let root = null, rootH = 0, p = ed;
        for (let i = 0; i < 12; i++) {
            p = p.parentElement;
            if (!p) break;
            const r = p.getBoundingClientRect();
            const wr = r.width / window.innerWidth;
            if (wr >= 0.4 && wr <= 0.9 && r.height > rootH) { root = p; rootH = r.height; }
        }
        if (!root) return out;
        let scanned = 0;
        for (const el of root.querySelectorAll('*')) {
            if (++scanned > 3000) break;
            if (el.children.length > 0) continue;
            const t = (el.innerText || '').trim();
            if (!t) continue;
            const r = el.getBoundingClientRect();
            if (r.width <= 0 || r.height <= 0) continue;
            out.panel_len += t.length;
            if (t.includes(name)) out.hits++;
            if (out.top_texts.length < 3 && t.length < 40)
                out.top_texts.push(t.slice(0, 40));
        }
    } catch (e) {}
    return out;
}"""


def _identity_state(page: Page, name: str) -> tuple:
    """单次身份核对, 返回 (状态, 说明)。状态: confirmed / conflict / unreadable。"""
    try:
        r = page.evaluate(_JS_CHAT_IDENTITY, name)
    except Exception as exc:
        return "unreadable", f"身份核对不可用({type(exc).__name__})"
    if not isinstance(r, dict):
        return "unreadable", "身份核对返回异常"
    if r.get("hits", 0) > 0:
        return "confirmed", f"身份核对: 目标『{name}』在当前会话面板确认"
    if r.get("box") and r.get("panel_len", 0) > 0:
        tops = " / ".join(r.get("top_texts", [])) or "(无文本)"
        return (
            "conflict",
            f"身份核对冲突: 当前会话面板未出现『{name}』, 面板文本=『{tops}』",
        )
    return "unreadable", "身份核对: 面板未渲染或读不到文本"


def verify_chat_opened(page: Page, name: str) -> tuple:
    """单次身份核对。返回 (是否放行, 说明)。

    「读不到就放行」是有意设计(读不懂 ≠ 读错人), 但它不该被用在**发送主链路**上:
    2026-10-07 叶文j/小淇淇 正是核对在骨架期放行 → 表情面板未打开 → 文字回车没生效
    → cv 兜底复检才判出冲突, 白烧 25s 加一次浏览器重启。主链路请用
    verify_chat_opened_ready; 本函数保留给「已经等过、只差一次裁决」的场合(cv 层)。
    """
    state, detail = _identity_state(page, name)
    if state == "conflict":
        return False, f"{detail}, 拦截发送"
    if state == "confirmed":
        return True, detail
    return True, f"{detail}, 无法判定, 放行"


def verify_chat_opened_ready(
    page: Page,
    name: str,
    retries: int = 2,
    wait_ms: int | None = None,
    strict: bool = False,
) -> tuple:
    """发送主链路的身份核对: 结论不稳定就先复检, 尽量拿到可判定的结论。

    复检条件是「不是 confirmed」而不是「读不到」: 现在 root 修好了, conflict 是**真会
    拦下发送**的判决, 所以它必须是连续两读都成立 —— 半渲染的面板(消息区已有文本、标题栏
    昵称还没挂上)不该变成漏发。confirmed 首读即放行, 正常好友零额外耗时。
    终态语义与 verify_chat_opened 一致(只有 conflict 拦截), 不新增中止分支。

    strict=True 时「读不到」也拦截: 用于**复用上一个好友页面**的场合 —— 那时右栏
    可能还挂着上一位的会话, 「读不到」正是半渲染瞬间, 放行等于把消息发给不确定的人。
    全新页面的路径保持放行(昨夜 9/9 已验证), 由调用方决定是否 strict。
    """
    if wait_ms is None:
        wait_ms = perf.ms("identity_retry_ms")
    state, detail = _identity_state(page, name)
    waited = 0
    while state != "confirmed" and waited < retries:
        waited += 1
        page.wait_for_timeout(wait_ms)
        state, detail = _identity_state(page, name)
    if state == "confirmed":
        return True, detail
    if state == "conflict":
        return False, f"{detail}, 复检后仍判冲突, 拦截发送"
    extra = f"(已复检 {waited} 次仍未渲染)" if waited else ""
    if strict:
        return False, f"{detail}, 无法判定, 复用页面下不放行{extra}"
    return True, f"{detail}, 无法判定, 放行{extra}"


_JS_SYNTH_ROW_CLICK = """(name) => {
    let t = null;
    for (const el of document.querySelectorAll('span,div')) {
        if ((el.innerText||'').trim() === name) { t = el; break; }
    }
    if (!t) return null;
    let row = t;
    for (let i = 0; i < 8; i++) {
        const rr = row.getBoundingClientRect();
        if (rr.height >= 40 && rr.height <= 180 && rr.width >= 200) break;
        if (!row.parentElement) return null;
        row = row.parentElement;
    }
    const r = row.getBoundingClientRect();
    const x = (r.x + r.width / 2) | 0, y = (r.y + r.height / 2) | 0;
    const target = document.elementFromPoint(x, y) || row;
    for (const type of ['pointerover','pointerdown','mousedown','pointerup','mouseup','click']) {
        const Ev = type.startsWith('pointer') ? PointerEvent : MouseEvent;
        target.dispatchEvent(new Ev(type, {bubbles: true, cancelable: true, clientX: x, clientY: y}));
    }
    return {x, y};
}"""


def synthetic_click_row(page: Page, name: str):
    """在昵称文本上定位「会话行」容器, 向行中心元素派发合成鼠标/指针事件序列。

    2026-10-06 好友A取证(探针 v1-v6, 板子实测): 该重燃会话对一切可信点击
    (Playwright .click / page.mouse.click 文本中心/行中心)均 90s 不挂载面板,
    唯独行容器上的合成事件序列**立即**打开会话(截图含完整面板+输入框)。
    与可信点击的差异原理上不该存在(isTrusted), 但实盘只此一条路, 按证据行动。
    返回命中坐标或 None(找不到昵称)。
    """
    try:
        return page.evaluate(_JS_SYNTH_ROW_CLICK, name)
    except Exception:
        return None


def send_sticker(
    page: Page,
    sticker_name: str,
    *,
    attempt: str | None = None,
    parent: str | None = None,
    friend: str = "",
    account: str = "",
) -> tuple:
    """发送抖音内置表情包; 返回 (是否已尝试, 生效策略描述)。

    流程: 找表情按钮 → 打开面板 → 等待内容加载 → 找指定表情 → 点击(自动发送)。

    attempt/parent 用于结构化事件(2026-11-01 会话50): 同一好友的同一轮尝试共用
    一个 attempt id, 上一层失败的 id 作为下一层的 parent, 才能算出「独立救回」。
    """
    aid = attempt or events.attempt_id()
    JS_FIND_EMOJI_BTN = """() => {
        const editable = document.querySelector('[contenteditable="true"]');
        if (!editable) return null;
        const ir = editable.getBoundingClientRect();
        // 语义锚点优先: title/aria-label/data-e2e 比混淆类名与像素位置更抗改版
        for (const el of document.querySelectorAll('[title], [aria-label], [data-e2e]')) {
            const m = ((el.getAttribute('title') || '') + '|' +
                       (el.getAttribute('aria-label') || '') + '|' +
                       (el.getAttribute('data-e2e') || '')).toLowerCase();
            if (!/表情|emoji/.test(m)) continue;
            const r = el.getBoundingClientRect();
            if (r.width > 0 && r.height > 0 && r.y > ir.y - 200) {
                return {x: Math.round(r.x + r.width / 2),
                        y: Math.round(r.y + r.height / 2)};
            }
        }
        // 2026-11-01 板端 DOM 实测: 表情键是 <svg class="messageMsgInputiconAction">,
        // 既无 title/aria-label/data-e2e, 只能靠类名 —— 像素启发会先扫到它左边的
        // 上传键, 坐标偏一格就点空(面板不开), 所以类名优先于像素。
        for (const el of document.querySelectorAll('[class*="iconAction"]')) {
            const r = el.getBoundingClientRect();
            if (r.width >= 12 && r.width <= 48 && r.height >= 12 && r.height <= 48
                && r.y > ir.y - 200
                && !/upload|file|publish/i.test((el.className || '').toString())) {
                return {x: Math.round(r.x + r.width / 2),
                        y: Math.round(r.y + r.height / 2)};
            }
        }
        // 像素启发兜底: 先 svg 后 img, 容忍度放宽
        for (const sel of ['svg', 'img']) {
            for (const el of document.querySelectorAll(sel)) {
                const r = el.getBoundingClientRect();
                if (Math.abs(r.y - ir.y) < 60 && r.x > ir.right - 5
                    && r.width >= 12 && r.width <= 48) {
                    const p = el.parentElement;
                    const cls = p ? (p.className || '').toString() : '';
                    if (/upload|file/i.test(cls)) continue;
                    return {x: Math.round(r.x + r.width / 2),
                            y: Math.round(r.y + r.height / 2)};
                }
            }
        }
        return null;
    }"""
    JS_FIND_STICKER = """(name) => {
        const panel = document.querySelector(
            '[class*="emojiPanel"], [class*="stickerPanel"], [data-e2e*="emoji"]');
        let items = [];
        if (panel) {
            items = panel.querySelectorAll('[class*="emojiItem"]:not([class*="Desc"])');
        }
        if (!items.length) {
            // 面板类名失效: 全页搜索表情项(仅面板打开时才有可见尺寸)
            items = document.querySelectorAll(
                '[class*="emojiItem"]:not([class*="Desc"]), [class*="stickerItem"]');
        }
        let occluded = null;
        for (const item of items) {
            if (!(item.innerText || '').trim().includes(name)) continue;
            let r = item.getBoundingClientRect();
            if (!(r.width > 0 && r.height > 0)) continue;
            // 面板是滚动容器: 目标可能在可视区外但矩形仍有尺寸, 直接点会点到面板外
            try { item.scrollIntoView({block: 'nearest', inline: 'nearest'}); } catch (e) {}
            r = item.getBoundingClientRect();
            const x = Math.round(r.x + r.width / 2);
            const y = Math.round(r.y + r.height / 2);
            if (x <= 0 || y <= 0 || x >= window.innerWidth || y >= window.innerHeight) continue;
            const hit = document.elementFromPoint(x, y);
            if (!hit) continue;
            if (hit !== item && !item.contains(hit) && !hit.contains(item)) {
                if (!occluded) occluded = {x: x, y: y, tag: hit.tagName,
                    cls: (hit.className || '').toString().slice(0, 40)};
                continue;
            }
            return {x: x, y: y};
        }
        if (occluded) return {error: 'sticker_occluded', at: occluded};
        return {error: 'sticker_not_found'};
    }"""
    JS_PANEL_VISIBLE = """() => {
        for (const p of document.querySelectorAll(
            '[class*="emojiPanel"], [class*="stickerPanel"], [data-e2e*="emoji"]')) {
            const r = p.getBoundingClientRect();
            if (r.width > 100 && r.height > 100) return true;
        }
        let n = 0;
        for (const it of document.querySelectorAll('[class*="emojiItem"], [class*="stickerItem"]')) {
            const r = it.getBoundingClientRect();
            if (r.width > 0 && r.height > 0 && ++n >= 8) return true;
        }
        return false;
    }"""
    JS_PANEL_CLOSED = """() => {
        for (const p of document.querySelectorAll(
            '[class*="emojiPanel"], [class*="stickerPanel"], [data-e2e*="emoji"]')) {
            const r = p.getBoundingClientRect();
            if (r.width >= 100 && r.height >= 100) return false;
        }
        let n = 0;
        for (const it of document.querySelectorAll('[class*="emojiItem"], [class*="stickerItem"]')) {
            const r = it.getBoundingClientRect();
            if (r.width > 0 && r.height > 0 && ++n >= 8) return false;
        }
        return true;
    }"""

    try:
        btn = page.evaluate(JS_FIND_EMOJI_BTN)
        if not btn:
            events.emit(
                "sticker",
                "find_button",
                "failed",
                friend=friend,
                account=account,
                attempt=aid,
                error_class="no_emoji_btn",
            )
            return False, "未找到表情按钮"
        log(f"[表情] 表情按钮: ({btn['x']},{btn['y']})")
        events.emit(
            "sticker",
            "find_button",
            "ok",
            friend=friend,
            account=account,
            attempt=aid,
            extra={"x": btn["x"], "y": btn["y"]},
        )
        page.mouse.click(btn["x"], btn["y"])

        # 面板打开等待: 循环内一检测到即 break, 故加大上限只影响「本会失败」的场景,
        # 对正常好友(<3s 打开)零延迟。2026-10-06 好友A实测: 经 synthetic_click_row
        # 抢救刚挂载的面板, 其工具栏初始化更慢, 表情面板在点击后 >3s 才渲染出
        # (截图证实最终打开且 续火花 表情可见), 旧 6×500ms 窗口过早判失败退到文字兜底,
        # 而兜底又因面板此时刚弹出发起 Enter 被拦截 → 假发送。放宽到 12×500ms=6s。
        opened = False
        _rounds = max(1, perf.ms("panel_open_ms") // max(1, perf.ms("panel_poll_ms")))
        for _ in range(_rounds):
            page.wait_for_timeout(perf.ms("panel_poll_ms"))
            if page.evaluate(JS_PANEL_VISIBLE):
                opened = True
                break
        if not opened:
            events.emit(
                "sticker",
                "open_panel",
                "failed",
                friend=friend,
                account=account,
                attempt=aid,
                error_class="panel_not_open",
            )
            return False, "表情面板未打开"

        try:
            page.wait_for_selector(
                '[class*="emojiItem"]:not([class*="Desc"]), [class*="stickerItem"]',
                state="visible",
                timeout=perf.ms("panel_items_ms"),
            )
        except Exception:
            events.emit(
                "sticker",
                "load_panel",
                "failed",
                friend=friend,
                account=account,
                attempt=aid,
                error_class="panel_content_timeout",
            )
            return False, "表情面板内容未加载"

        target = page.evaluate(JS_FIND_STICKER, sticker_name)
        if "error" in target:
            events.emit(
                "sticker",
                "find_item",
                "failed",
                friend=friend,
                account=account,
                attempt=aid,
                error_class=target.get("error"),
            )
            if target.get("error") == "sticker_occluded":
                at = target.get("at") or {}
                return False, (
                    f"表情项被遮挡, 点不到: ({at.get('x')},{at.get('y')}) 处是 "
                    f"{at.get('tag', '')}.{at.get('cls', '')}"
                )
            return False, f"表情面板中未找到『{sticker_name}』"
        log(f"[表情] 目标表情: ({target['x']},{target['y']})")
        events.emit(
            "sticker",
            "find_item",
            "ok",
            friend=friend,
            account=account,
            attempt=aid,
            extra={"x": target["x"], "y": target["y"]},
        )

        # 基线必须在**点击表情之前**取: 文案固定为「续火花」, 昨晚那条表情还挂在列表
        # 尾部, 只比文本会把残留读成本次发送。
        base = read_tail(page)
        page.mouse.click(target["x"], target["y"])
        # 点击本身不是证据(没抛异常≠发出去了), verified 只能是 None
        events.emit(
            "sticker",
            "click_item",
            "ok",
            friend=friend,
            account=account,
            attempt=aid,
            verified=None,
            extra={"x": target["x"], "y": target["y"], "n0": base[0]},
        )

        # 点击表情后抖音自动发送并关闭面板。确认信号(2026-10-07 审计后重排):
        # 1) 有「新增气泡/尾部窗口移动」的硬证据 -> 明确成功(最强)
        # 2) 面板关闭但证据读不到(列表容器定位失败) -> 沿用旧口径判成功, 标注「降级」
        #    让 strat_stats 能统计这一层的失证比例, 不把它当成验证过
        # 3) 面板关闭但证据可读且**毫无新增** -> 判未发送(表情点了个空)
        # 注意: 表情发送成功后不要再走文字兜底, 避免重复发送。
        closed_no_ev = None
        for _ in range(6):
            # 性能(2026-11-01): 首拍 250ms(气泡通常立刻出现), 之后维持 500ms。
            page.wait_for_timeout(
                perf.ms("tail_first_ms") if _ == 0 else perf.ms("tail_poll_ms")
            )
            n1, last, prev = read_tail(page)
            ev, ev_msg = _evidence(base, n1, last, prev)
            if ev is True:
                if sticker_name in last or page.evaluate(JS_PANEL_CLOSED):
                    events.emit(
                        "sticker",
                        "verify",
                        "ok",
                        friend=friend,
                        account=account,
                        attempt=aid,
                        verified=True,
                        extra={
                            "ev": ev_msg,
                            "bubble": n1,
                            "text_match": sticker_name in last,
                            "last_is_self": last_bubble_is_self(page),
                        },
                    )
                    return True, f"表情已发送: {sticker_name} ({ev_msg})"
                continue
            if ev is None:
                if sticker_name in last:
                    events.emit(
                        "sticker",
                        "verify",
                        "ok",
                        friend=friend,
                        account=account,
                        attempt=aid,
                        verified=None,
                        extra={
                            "degraded": ev_msg,
                            "text_match": True,
                            "last_is_self": last_bubble_is_self(page),
                        },
                    )
                    return (
                        True,
                        f"表情已发送(降级 {ev_msg}): {sticker_name} (最新消息匹配 {last[:30]!r})",
                    )
                if page.evaluate(JS_PANEL_CLOSED):
                    events.emit(
                        "sticker",
                        "verify",
                        "ok",
                        friend=friend,
                        account=account,
                        attempt=aid,
                        verified=None,
                        extra={"degraded": ev_msg},
                    )
                    return (
                        True,
                        f"表情已发送(降级 {ev_msg}): {sticker_name} (面板已关闭, 抖音自动发送)",
                    )
                continue
            if page.evaluate(JS_PANEL_CLOSED):
                # 不立即返回: 新气泡可能渲染滞后, 用满本轮窗口再下结论。
                closed_no_ev = ev_msg
        if closed_no_ev is not None:
            # 证据可读且零新增 = 确证没发出去(表情点了个空)
            events.emit(
                "sticker",
                "verify",
                "failed",
                friend=friend,
                account=account,
                attempt=aid,
                verified=False,
                extra={"ev": closed_no_ev},
            )
            return False, f"表情面板已关闭但{closed_no_ev}, 判未发送"
        # 面板没关、窗口用尽: 分不清「没发出」与「发出但面板没关」→ unknown 更诚实
        events.emit(
            "sticker",
            "verify",
            "unknown",
            friend=friend,
            account=account,
            attempt=aid,
            verified=None,
        )
        return (
            False,
            f"表情面板未关闭, 发送可能失败 (最新消息 {read_tail(page)[1][:30]!r})",
        )
    except Exception as exc:
        events.emit(
            "sticker",
            "verify",
            "failed",
            friend=friend,
            account=account,
            attempt=aid,
            error_class=type(exc).__name__,
        )
        return False, f"表情发送异常: {exc}"


_JS_EDITABLE_RECT = """() => {
    const el = document.querySelector('[contenteditable="true"]');
    if (!el) return null;
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return null;
    return {x: r.x, y: r.y, w: r.width, h: r.height, vw: window.innerWidth};
}"""

_JS_SET_BOX_TEXT = """(text) => {
    const el = document.querySelector('[contenteditable="true"]');
    if (!el) return false;
    el.focus();
    el.textContent = text;
    el.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: text ? 'insertText' : 'deleteContentBackward', data: text || null}));
    return true;
}"""


def cv_send_fallback(
    page: Page,
    text: str,
    name: str,
    *,
    attempt: str | None = None,
    parent: str | None = None,
    account: str = "",
):
    """Emergency Visual Fallback: 表情+文字各层全部失败后, 用 OpenCV 定位红色↑
    发送按钮抢救一次点击。只走「本来就已失败」的路径, 不改变任何现有层顺序。

    点击前安全闸: ①候选坐标限定在输入框右侧条带(ROI, 不扫全页); ②页面非近黑
    渲染异常(vision_cv 内); ③点击前重跑 verify_chat_opened 身份复核(表情尝试
    期间面板可能已切换)。失败路径一律清空输入框, 不留草稿。
    返回 (发送已确认, 说明); 统计行 [cv_fallback] used/found/clicked/verified
    供 strat_stats 判健康度(找到多验证少 → 立即禁用本层)。
    """
    aid = attempt or events.attempt_id()
    stats = {"used": 1, "found": 0, "clicked": 0, "verified": 0}

    def _done(
        detail: str,
        result: str = "failed",
        verified: bool | None = None,
        error_class: str | None = None,
    ) -> None:
        fields = " ".join(f"{k}={v}" for k, v in stats.items())
        log(f"[cv_fallback] {fields} detail={detail}")
        # 7 个出口都汇到这一处: 只打一行, 不逐候选灌事件
        events.emit(
            "vision",
            "cv_fallback",
            result,
            friend=name,
            account=account,
            attempt=aid,
            parent=parent,
            verified=verified,
            error_class=error_class,
            extra={**stats, "detail": detail[:60]},
        )

    def _clear() -> None:
        try:
            page.evaluate(_JS_SET_BOX_TEXT, "")
        except Exception:
            pass

    if not vision_cv.available():
        _done("cv2_unavailable")
        return False, "cv2 未安装, 视觉兜底不可用"
    try:
        er = page.evaluate(_JS_EDITABLE_RECT)
        if not er:
            _done("no_editable")
            return False, "输入框不可见, 放弃视觉抢救"
        # 表情面板可能遮挡发送区: Escape 收起(未打开时无副作用), 再注入文本
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(perf.ms("escape_settle_ms"))
        except Exception:
            pass
        if not page.evaluate(_JS_SET_BOX_TEXT, text):
            _done("inject_failed")
            return False, "视觉兜底: 文本注入失败"
        # 基线取在注入之后、点击发送之前: confirm_send 需要「这条消息确实新出现」的证据,
        # 而固定文案「续火花」昨晚那条一直挂在列表尾部。
        base = read_tail(page)
        page.wait_for_timeout(perf.ms("btn_ready_ms"))  # 等按钮由灰变红
        roi_x = int(er["x"] + er["w"] * 0.5)
        roi = (
            roi_x,
            max(0, int(er["y"] - 40)),
            max(1, int(er["vw"] - roi_x)),
            int(er["h"] + 80),
        )
        cand = vision_cv.find_send_button(page.screenshot(), roi=roi)
        if not cand:
            _clear()
            _done("not_found")
            return False, "视觉兜底: ROI 内未找到红色↑按钮"
        stats["found"] = 1
        ok_id, id_detail = verify_chat_opened(page, name)
        if not ok_id:
            _clear()
            _done(
                f"identity_conflict:{id_detail[:40]}",
                "failed",
                True,
                "identity_conflict",
            )
            return False, "视觉兜底: 点击前身份复核未通过, 放弃"
        page.mouse.click(cand["x"], cand["y"])
        stats["clicked"] = 1
        okc, detail = confirm_send(page, text, baseline=base)
        stats["verified"] = 1 if okc else 0
        if not okc:
            _clear()
        _done(
            f"cand=({cand['x']},{cand['y']}) conf={cand['conf']} "
            f"area={cand['area']} {detail[:40]}",
            "ok" if okc else "failed",
            okc,
            None if okc else "not_confirmed",
        )
        return okc, f"视觉兜底点击({'确认' if okc else '未确认'}): {detail}"
    except Exception as exc:
        _clear()
        _done(
            f"exception:{type(exc).__name__}:{exc}",
            "failed",
            None,
            type(exc).__name__,
        )
        return False, f"视觉兜底异常: {exc}"
