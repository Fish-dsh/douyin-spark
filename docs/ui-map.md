# 页面锚点对照

抖音改版那天照这张表改。别的文档讲链路和设计, 这里只回答一个问题: **屏幕上哪个字、哪个类名、哪块像素在决定我们的判断。**

## 怎么用

1. 在 `runtime/spark.log` 里搜「症状」列的词（表里给的是代码里的日志原文, 搜得到就说明当场命中了这一行）。
2. 按「位置」找到那处 JS 或 Python。
3. 只改那一处, 改完 `python -m pytest -q` 全绿, 再在有头环境 `python spark.py --dry-run` 看一眼。
4. 顺手改这张表: **行号会往下漂, 表里的锚点原文比行号可靠** —— 按原文 grep。

三条纪律:

- 表里没有的页面依赖 = 代码里也没有。别凭记忆加锚点, 先 grep 确认。
- **加兜底不要换掉旧的**: 同一处多写一条候选, 旧的留在最后。旧类名在某些账号的页面上可能还在生效。
- 改选择器要回头看 [board-perf.md](board-perf.md): 找不到元素时每条候选都要白等一轮, 候选越多这一晚越慢。

## 全局前提

| 前提 | 值 | 为什么 |
| --- | --- | --- |
| 视口 | **800×600** | `src/login.py:343` 写死。几何点击的坐标、CV 的 ROI 全按它算, 换分辨率等于全偏 |
| 模式 | 有头 + Xvfb | 无头会被抖音拦, 弱机渲染也容易挂死 |
| 语言 | `zh-CN` | 下面一半锚点是中文文案（"续火花" "发送" "登录"）, 换语言全废 |
| 缩放 | 100% | 截图 `scale="css"` 让 1 图像像素 = 1 CSS 像素（`src/vision.py:280`） |

---

## 1. 登录态 — 坏了后面全不跑

| 症状（grep 这个） | 位置 | 现在锚的是 | 改法 |
| --- | --- | --- | --- |
| 整晚"未登录" | `src/login.py:84-90` | `#login-panel` `#login-modal` `.login-panel` `[data-e2e='login-modal']` `[data-e2e='login-qrcode']` | 在浏览器里 F12 看弹层真实属性, 加进候选组 |
| 同上 | `src/login.py:92-98` | `img[src*='qrcode']` `img[class*='qrcode']` `[class*='qrcode'] img` | 二维码换了容器/换了 `src` 关键词就补一条 |
| 同上 | `src/login.py:110-114` | 页面文案 "扫码登录" "验证码登录" "登录即代表同意用户协议" | 选择器全灭时靠文案兜底, 文案改了就换新文案 |
| 明明已登录却判未登录 | `src/login.py:101-106` | `[data-e2e='user-info']` `[data-e2e='user-avatar']` `img[src*='douyinpic.com']` `img[class*='avatar']` | 头像换 CDN 域名就补一条; cookie 仍能独立判定 |
| 掉线账号被当已登录, 当晚静默空跑 | `src/login.py:120` | cookie 名 `sessionid` / `sessionid_ss` / `uid_tt` | cookie 改名就要同步改这里和 `src/session.py` 的恢复逻辑 |
| 未找到可点击的『登录』入口 | `src/login.py:278-282` | 叶子节点 `innerText === '登录'` 且 `offsetParent !== null` | 入口按钮文案变了就换成新文案 |
| 未找到『密码登录』入口 | `src/login.py:395` | "密码登录" / "账号密码登录" / "使用密码登录" | 同上, 三个候选都失效才加新的 |
| 未找到账号输入框 / 密码输入框 | `src/login.py:397-410` | `input[placeholder*='手机号']` `input[type='password']` 等 | placeholder 文案锚最容易坏, 优先按 `type` 兜底 |
| 未找到登录按钮 | `src/login.py:411-414` | `button:has-text('登录')` / `'立即登录'`, 再退 JS 精确文本 | 抖音登录键是 `div` 不是 `button`, 保持 JS 兜底 |
| 保存弹窗挡路 | `src/login.py:567-573` | 文案 "是否保存登录信息" / 叶子 `innerText === '保存'` | 弹窗文案变就改, 否则它盖住页面后续点击全失效 |
| 撞滑块 | `src/login.py:416-423` | `iframe[src*='captcha']` `[class*='slider']` 等 | **只用于"识别到就放弃"**, 不破解 —— 别往这里加绕过的代码 |

## 2. 聊天页就绪 & 会话列表

| 症状（grep 这个） | 位置 | 现在锚的是 | 改法 |
| --- | --- | --- | --- |
| 每个好友都要重载一次整页（变慢不漏发） | `src/finder.py:14-17` | URL 含 `/chat` + `[class*="conversation"]` `[class*="semi-list-item"]` 非空且 **≥3 条** | 阈值 3 是权衡: 只有 1-2 个会话的账号会永远判"没渲染好" |
| 等待会话列表内容超时 | `src/finder.py:122-123` | `[class*=conversation]` 且 `innerText` 非空, 预算 150s | 列表容器类名变了就换; 这条超时 = 当晚 0 火花 |
| 主聊天区白等 9~15s | `src/finder.py:35-42` | 6 条候选: `[data-e2e='chat']` … `a[href*='/chat']` | 每条候选都要等 1.5s, 加候选前先删掉确定不中的 |
| 有内容页被判"blank" | `src/finder.py:99-104` | `body.innerText.length >= 50` | 阈值太低会误判 → 只重开会话不自愈; 页面标题化就要调大 |
| 深处好友从未被扫到 | `src/spark_status.py:248-260` | 滚动容器候选 `[class*="conversationList"]` `[class*="chatList"]` `[class*="sessionList"]`, 溢出样式兜底 | 滚不动就只扫首屏, 列表深处的好友等于不存在 |

## 3. 火花判定 — 四策略, 断了会漏续或重复发

四条策略按顺序跑, 前一条全灭才走下一条（`_DISCOVERY_STRATEGIES`）。**只有策略 4 不依赖混淆类名**, 抗改版全靠它。

| 症状（grep 这个） | 位置 | 现在锚的是 | 改法 |
| --- | --- | --- | --- |
| 检测到会话列表但无任何火花标记 | `src/spark_status.py:39-65` | 策略 1(类名): 行 `conversationConversationItemwrapper`、火花 `commonStreakstreakContainer` / `commonStreakicon` / `commonStreaknormalText` | 抖音改版时这串哈希类名最先变。逐条在 F12 里核对后替换 |
| 同上 | `src/spark_status.py:113` | 策略 2(文本+图标): 图标 `src` 含 `flame` `fire` `streak` `spark` `hot` `light` `blue` | 图标名改成 hash 就判"无火花" → 漏续。补新关键词 |
| 同上 | `src/spark_status.py:139-166` | 策略 3(图标反查): 先找 `commonStreakicon` 的 `img`, 再 `closest()` 回行 | 与策略 1 共用类名, 类名一改一起灭 |
| 同上 | `src/spark_status.py:181-238` | 策略 4(结构兜底): `KEYWORDS=['flame','fire','streak','spark']` + React `__reactFiber$` 取昵称 + `img[alt]` 定位行 | **最后一道。图标关键词变了先改这里**; React 去键则先丢名字 |
| 火花天数恒 0 / 账单显示"0天" | `src/spark_status.py:64` `121` `164` `235` | 天数取 `commonStreaknormalText` 或 `/^[0-9]{1,4}$/` | 只影响显示与周报, 不影响发不发 |
| 置顶好友没被强制续 | `src/spark_status.py:65` `125` `166` `238` | `isStickOnTop` 类名 / 行 `className` 含 `stick` \| `pinned` | 置顶判不出 = 失去"无条件续"这道保险 |
| 该续的跳过 / 已续的重复发 | `src/spark_status.py:313-315` | 图标 `src` 含 `gray`/`grey` → 需续; 不含 `active`/`fire`/`streak` → 打 warn 按已续处理 | **判反的代价最大**: 灰图标改名 → 该续的断火。改这里必须真机验证一轮 |

## 4. 打开会话 & 身份核对

| 症状（grep 这个） | 位置 | 现在锚的是 | 改法 |
| --- | --- | --- | --- |
| 好友找不到, 该好友跳过 | `src/sender.py:939-946` | 三策略: `get_by_text(name, exact=True)` → `xpath=//*[text()=name]` → `[title=name]` / `[data-name=name]` | 名字匹配是精确的。抖音给昵称加后缀（如粉丝数）就会全灭 |
| 同上 | `src/sender.py:1093-1110` | 合成事件点击: 叶子 `innerText === name`, 行高 40~180、宽 ≥200, 再依次发 `pointerover/pointerdown/mousedown/…/click` | 事件序列少一个, 面板就不挂载。行高限制跟着 UI 改 |
| 上面全不中 | `src/vision.py` | 视觉兜底（见第 8 节） | 先修前两条, 视觉是最后一道 |
| 身份核对冲突, 拦截发送 | `src/sender.py:989-1012` | 输入框 `[contenteditable="true"]` → 取最大的面板祖先（宽比 0.4~0.9）→ 面板内叶子文本含昵称 | **读不到就放行**（宁可发出去也不断火）。布局改了会让这道闸门形同不存在 |
| 会话打开了但发错人 | 同上 | 上面那条的 `unreadable` 分支 | 若频繁出现, 说明面板祖先的宽比范围不再命中 |

## 5. 输入框与发送键

| 症状（grep 这个） | 位置 | 现在锚的是 | 改法 |
| --- | --- | --- | --- |
| 未定位到输入框 | `src/sender.py:17-22` | 6 条: `[contenteditable='true']` → `div[contenteditable]:not([contenteditable='false'])` → `[data-e2e*='input']` → `[class*='input'] textarea` → `textarea` → `input[type='text']` | 输入框换成 Canvas/富文本组件就全灭, 那时要改的是注入方式不是选择器 |
| 未找到发送按钮, 改用 Enter | `src/sender.py:29-35` | 7 条候选, **板端实测只有 `svg[class*='publishBtn']` 真命中** | Enter 在抖音里常无效 → 假失败 → 落到 CV 兜底。这条别删 |
| 同上 | `src/sender.py:40-59` | 几何找键: 与输入框同一行、尺寸 14~72px、在输入框右侧、排除表情/图片/附件 | 按钮尺寸或位置改了就调这几个数 |
| 点到表情键被当"发送未生效" | `src/sender.py:59` | 排除词 `表情|emoji|图片|image|photo|相册|upload|file|附件|mention` | 排除词漏了就点错键, 后面还有 Escape 兜底但会重复试 |
| 发送判空失效 → 重复发 | `src/sender.py:80-82` | 输入框清空 = 发出; 兜底看 `[contenteditable="true"]` / `textarea[class*="input"]` | 判空失败会多试一次, 可能重复发。改了要真机验 |

## 6. 已发送确认 — 假成功的根源

这套判据证的是「**客户端画出了气泡**」, 不是「服务器收到了」。判据被迫降级时宁可报失败也别报成功。

| 症状（grep 这个） | 位置 | 现在锚的是 | 改法 |
| --- | --- | --- | --- |
| 尾部证据不可读 | `src/sender.py:217-231` | 消息列表 4 条类名 + 几何兜底（`overflowY` 可滚、高 ≥200、在右半屏） | **全灭 = 判据降级 = 可能假成功**, 优先级最高的锚点之一 |
| 消息列表读不到气泡 | `src/sender.py:247-250` | 气泡 `messageMessageBox` / `MessageBox` / `bubble` 族 + 子节点 `innerText` 兜底 | 数到 0 个就退回"输入框已清空"这种弱证据 |
| 表情发出去判不出 | `src/sender.py:236-238` | 气泡 `img[alt]` / `img[title]` | 表情气泡没有 alt 时只能靠"面板已关闭"判成功 |
| 好友回了同样的表情, 被当我方发出 | `src/sender.py:279-292` `705-706` | 最后气泡中心在列表右半侧 + 尾部窗口是否移动 | 我方/对方靠左右半侧区分, 已知局限。**别当硬判据** |

## 7. 表情面板 — 五环链条, 断一环只降级不报错

按钮语义 → 类名 → 像素 → 面板类名 → 项名文本。任何一环断了都只是「降级到文字」, 所以日志里往往只看到一行「表情面板未打开」。

| 症状（grep 这个） | 位置 | 现在锚的是 | 改法 |
| --- | --- | --- | --- |
| 表情面板未打开 | `src/sender.py:1140-1146` | `[title]` / `[aria-label]` / `[data-e2e]` 里匹配 `表情|emoji` | 标签改英文别的词就漏, 落下一环 |
| 同上 | `src/sender.py:1154-1158` | 类名 `[class*="iconAction"]`, 尺寸 12~48, 排除 `upload|file|publish` | **板端实测唯一活路**。类名改 → 落像素启发 → 常点偏到上传键 |
| 同上 | `src/sender.py:1167` | 像素启发: `\|r.y - ir.y\| < 60` 且 `r.x > ir.right - 5` 且宽 12~48 | 工具栏移位就点空 |
| 表情面板中未找到『续火花』 | `src/sender.py:1181-1194` | 面板 `emojiPanel`/`stickerPanel`/`data-e2e*='emoji'`; 项 `emojiItem:not([Desc])`; 项名 `innerText` 含 `name` | **表情改名 = 表情层成功率直接 0**, 只降级到文字。名字来自 `SEND_STICKER`（`src/config.py:47`, 默认"续火花"） |
| 表情面板未关闭, 发送可能失败 | `src/sender.py:1220-1239` | 面板可见 = 宽高 >100, 或可见项 ≥8 | 面板不再自动关 → 会再打一条文字, 变成重复消息 |
| 表情面板内容未加载 | `src/sender.py:1284-1286` | `emojiItem` / `stickerItem` 可见 | 超时就走文字 |

## 8. 视觉兜底 — 不认 DOM, 只认像素

| 症状（grep 这个） | 位置 | 现在锚的是 | 改法 |
| --- | --- | --- | --- |
| 模型点位全错 / 取证 0 行 | `src/vision.py:71-77` | 提示词里的版式描述（"左侧是会话列表…只有火花图标保持原样"） | 页面版式变了先改这段描述, 模型按它找 |
| 昵称没打码就外发 | `src/vision.py:83` | `const limitX = vw * 0.45`（左栏比例） | 左栏变宽就要调大, 否则遮不住 |
| 火花图标被遮罩盖住 | `src/vision.py:98` | 脱敏时保留 `/(flame|fire|streak|spark)/i` 的图标 | 与策略 4 的关键词同源, 一起改 |
| 取证取不到行 | `src/vision.py:139-168` | 行高 28~140、宽 ≥140、左栏 `left <= vw*0.45`、首行即昵称 | 首行变成时间戳（"昨天 20:31"）昵称就读错, 好在点前还有身份核对挡一次 |
| 放弃视觉抢救 / 输入框不可见 | `src/sender.py:1436-1441` | 输入框 `getBoundingClientRect()` 算 ROI | ROI 为空就没得救 |
| ROI 内未找到红色↑按钮 | `src/vision_cv.py:31-35` `74-97` | 面积 200~5000、圆形度 ≥0.7、宽高比 0.8~1.25、HSV 红双区间 `(0,120,120)`~`(10,…)` + `(170,…)`~`(180,…)` | **跨 0° 必须两个区间**。按钮改主题色（非红）就整层失效 |
| 同上 | `src/sender.py:1523-1530` | 等按钮由灰变红（`perf.btn_ready_ms`）, ROI = 输入框右侧条带 | 等待不够按钮还灰 → 找不到 |

## 9. 错误分类 — 锚的是我们自己的日志文案, 不是页面

`src/recovery.py` 按**异常与日志里的词**分类, 决定「重试 / 重开会话 / 告警」。**改上方任何一条报错措辞, 都要回来看这里还认不认得**; 认不得就归 `UNKNOWN` → 只告警不重试 → 当晚直接放弃。

| 分类 | 位置 | 认的词 |
| --- | --- | --- |
| `render_hang` → 重开会话 | `src/recovery.py:43` | 渲染挂死 / body 全空 / blank / page_render_state |
| `login` → 告警 | `src/recovery.py:44` | 未登录 / 登录态 / logged_out / login |
| `resource` → 重开会话 | `src/recovery.py:47` | out of memory / oom / 内存不足 / memoryerror |
| `transient` → 原样重试 | `src/recovery.py:53-61` | timeout / 超时 / target closed / net:: / 发送未确认 |
| `selector` → reload 重试 | `src/recovery.py:67-77` | 未找到 / 定位 / 选择器 / 按钮 / 表情 / 失效 / 改版 |

---

## 改版后的排查顺序

抖音改版通常一晚上打掉一串。按这个顺序看, 别一上来就翻 1500 行的 `sender.py`:

1. `python spark.py --status` —— 昨晚发了几个、失败在哪一段。
2. `python spark.py --doctor` —— 前提还在不在（登录态、cron、显示、时区）。
3. `grep '\(未找到\|超时\|未打开\|读不到\)' runtime/spark.log` —— 拿这行日志回上面的表里对, 找到 `文件:行`。
4. 只改那一处, 真机 `--dry-run` 看一遍, 再改这张表。

## 不要动的东西

- **视口 800×600** 与截图 `scale="css"`: 一动全偏。
- **四策略的顺序**: 策略 1 是全类名、末策略是结构兜底。顺序换了会让"类名精确但慢"的路挡在前面。
- **CV 的双红区间**: 单区间会在 `0°` 处断开, 红色按钮一半像素判不出。
- **"读不到就放行"的身份核对**: 宁可发出去也不要断火 —— 这是取舍, 不是漏洞。
- **发送键的排除词表**: 少了就点表情/图片键, 弹面板被当发送失败。
