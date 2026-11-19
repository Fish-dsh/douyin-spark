# OrangePi Zero 3 GPU 加速建设方案（阶段一：实测审计 + 阶段二/三规划）

> 状态：**P1/P2 已执行并验收通过**（2026-11-19 15:05 CST）—— GPU 驱动真实启用，`GL_RENDERER` 已从 `llvmpipe` 变为 `Mali-G31 (Panfrost)`。
> P1 的唯一改动是 `/boot/orangepiEnv.txt` 追加一行 `overlays=gpu`；未安装任何软件包、未升级内核、未改 Xvfb、未触碰续火花生产代码。
> **P2.5（Headless 可行性）已完成并判定不满足生产要求（第 9 节）；P3/P4 冻结，不再投入。**

## 0. 一句话结论

**已落地**：内核自带 panfrost、设备树描述完整、官方镜像自带启用 GPU 的 overlay —— 在 `/boot/orangepiEnv.txt` 追加一行 `overlays=gpu` 并重启，BSP 定制版 panfrost 就 probe 成功，`GL_RENDERER` 从 `llvmpipe (LLVM 15.0.6)` 变成 `Mali-G31 (Panfrost)`，`/dev/dri/renderD128` 出现。

**剩下的门槛不在驱动，在显示栈**：Xvfb 拿不到 DRM，Chromium 仍走软件渲染。这一步留给 P3（第 3 节路线 C、第 7 节）。

**P2.5 已排除一条捷径**：原生 headless 在板端真实交互会话中无法完成首屏渲染（第 9 节，A/B/C 六组实测），不能替代 Xvfb；且 headless 默认走 SwiftShader，不吃 Panfrost。**P3 冻结，不再投入。**

---

## 1. 实测审计结果

板子：`root@BOARD_IP`（DHCP 换过 IP，原记录 `BOARD_IP` 已失效），hostname `orangepizero3`。

### 1.1 系统与内核

| 项 | 实测值 |
|---|---|
| 发行版 | Debian GNU/Linux 12 (bookworm) 12.15 |
| 内核 | `6.1.31-sun50iw9 #1.0.4 SMP Thu Jul 11 16:37:41 CST 2024 aarch64` |
| 内核性质 | **Allwinner BSP 内核**（`-sun50iw9`），非 mainline/`-sunxi64` |
| 内核包 | `linux-image-next-sun50iw9 1.0.4`、`linux-dtb-next-sun50iw9 1.0.4` |
| 可切换内核 | 只有 `vmlinuz-6.1.31-sun50iw9` 一个 |
| U-Boot | `2024.01-orangepi` |

> 与预案的关键差异：原方案把「5.4 BSP 只有闭源 mali」当作最坏情况，实测是 **6.1 BSP 且自带 panfrost.ko**，所以**不需要换内核**。

### 1.2 设备树（GPU 节点的完整度是决定性的）

`/proc/device-tree/soc/gpu@1800000` 与 dtb 反编译结果一致：

```dts
gpu@1800000 {
    compatible = "allwinner,sun50i-h616-mali\0arm,mali-bifrost";
    reg = <0x1800000 0x40000>;
    interrupts = <0x00 0x5f 0x04 0x00 0x60 0x04 0x00 0x61 0x04>;   /* 95/96/97，level-high */
    interrupt-names = "job\0mmu\0gpu";
    clocks = <0x02 0x23 0x02 0x24>;                                /* CCU: gpu core + bus */
    clock-names = "core\0bus";
    resets = <0x02 0x03>;
    status = "disabled";                 /* ← 唯一的障碍 */
    mali-supply = <0x0d>;                /* AXP313A DCDC1 */
    phandle = <0x4c>;
};
```

| 检查项 | 结果 | 对 panfrost 的意义 |
|---|---|---|
| compatible 含 `arm,mali-bifrost` | ✅ | 与 panfrost.ko 的 OF alias 完全匹配 |
| reg / interrupts / interrupt-names | ✅ job+mmu+gpu | 三段中断齐全 |
| clocks = core + bus | ✅ | panfrost 必需的两个时钟都在 |
| resets | ✅ | — |
| mali-supply | ✅ DCDC1 = 900000µV，enabled | 供电已就绪 |
| `operating-points-v2` | ❌ **节点里没有引用** | devfreq 拿不到 OPP → 大概率固定频率；Mesa/驱动可容忍 |
| `#cooling-cells` | ❌ | dmesg 有 thermal 绑定警告，不影响功能 |
| `gpu-opp-table` | ✅ 存在，5 档：125/250/432/600/800 MHz | 后续想调频时可自定义 overlay 补一行引用 |
| dtb 带 `__symbols__` | ✅ `gpu = "/soc/gpu@1800000"` | **overlay 能被 U-Boot 的 `fdt apply` 正确解析** |

### 1.3 内核侧驱动

| 项 | 实测值 |
|---|---|
| `CONFIG_DRM_PANFROST` | `=m` |
| `CONFIG_DRM_SCHED` | `=m` |
| `CONFIG_DRM_GEM_SHMEM_HELPER` | `=m` |
| `panfrost.ko` 文件 | `/lib/modules/6.1.31-sun50iw9/kernel/drivers/gpu/drm/panfrost/panfrost.ko` ✅ |
| 依赖 | `gpu-sched`、`drm_shmem_helper` —— 两个 `.ko` 都在 ✅ |
| OF alias | `of:N*T*Carm,mali-bifrost` ✅ |
| 当前是否加载 | ❌ 未加载，`/sys/bus/platform/drivers/` 下没有 panfrost |
| 闭源 mali | ❌ 没有 `mali_kbase`，也没有 `/dev/mali0` |
| devfreq governor | 全套（simple_ondemand / performance / powersave / userspace / passive） |

> dmesg 的相关报错只有两条，都是「GPU 节点被禁用」的连带结果，不是驱动缺陷：
> `OF: /thermal-zones/gpu-thermal/cooling-maps/map0: could not get #cooling-cells for /soc/gpu@1800000`、`thermal_zone0: binding zone gpu-thermal with cdev cpufreq-cpu0 failed:-2`。

### 1.4 用户态 Mesa / EGL

| 项 | 实测值 |
|---|---|
| Mesa | `22.3.6-1+deb12u2`（`libegl-mesa0` / `libgbm1` / `libgl1-mesa-dri`） |
| `panfrost_dri.so` | ✅ 在位 |
| libegl1 / libgles2 / libdrm2 | `1.6.0-1` / `1.6.0-1` / `2.4.114-1+b1` |
| EGL client 扩展 | `EGL_EXT_device_enumeration`、`EGL_EXT_platform_device`、`EGL_MESA_platform_surfaceless`、`EGL_KHR_platform_gbm`、`EGL_EXT_platform_x11` 全在 |
| 当前 EGL device 数 | **1**，`VENDOR = Mesa Project`，`VERSION = 1.5` → **软件设备** |
| `/dev/dri/` | 只有 `card0`（`sun4i-drm`，226:0），**无 `renderD128`** |
| glxinfo / eglinfo / es2_info | ❌ 都没装（所以我在板上放了 `/root/gpu_check.py`，用 ctypes 直接问 EGL，不需要装任何包） |

### 1.5 显示栈与 Chromium

| 项 | 实测值 |
|---|---|
| Xvfb | `/usr/bin/Xvfb :99 -screen 0 800x600x24 -nolisten tcp`，pid 1168，RSS 64MB |
| 交互 shell 的 DISPLAY | 空（cron 里由 `run_cron.sh` 显式设 `:99`） |
| 系统 Chromium | `154.0.8037.57`（Debian 包；生产用的是 Playwright 缓存里那份，需分别验证） |
| HDMI | `card0-HDMI-A-1/status = disconnected`，无 `/dev/fb*` |
| `disp_mode` | `1920x1080p60`（orangepiEnv.txt） |

### 1.6 资源、时钟、供电

| 项 | 实测值 |
|---|---|
| 内存 | MemTotal 1483MB，available 1256MB；Swap 741MB 未用 |
| CMA | 总 128MB，空闲 125MB |
| 温度 | 四个 thermal zone ≈ 28°C |
| GPU 时钟 | `pll-gpu` = 432MHz；`gpu0` = 432MHz（enable_cnt=0，未使能）；`bus-gpu` = 200MHz |
| GPU 供电 | `axp313a-dcdc1` = 900000µV，`enabled` |

### 1.7 启动链与 overlay 能力（决定性发现）

| 项 | 实测值 |
|---|---|
| `/boot/orangepiEnv.txt` | `overlay_prefix=sun50i-h616`，**但没有 `overlays=` 行** |
| boot.cmd overlay 逻辑 | 读 `orangepiEnv.txt` → 加载 dtb → `fdt resize 65536` → 遍历 `${overlays}` 逐个 `fdt apply` |
| 失败保护 | `fdt apply ... \|\| setenv overlay_error "true"` → 之后**重新加载原始 dtb** 继续启动 |
| 官方 GPU overlay | **`/boot/dtb/allwinner/overlay/sun50i-h616-gpu.dtbo` 已存在**（222 字节，与镜像同批次） |
| 该 dtbo 内容 | `fragment@0 { target = <&gpu>; __overlay__ { status = "okay"; }; }` —— **就是干这一件事** |
| 自定义 overlay 工具 | `orangepi-add-overlay`、`orangepi-config` 都在；用户 overlay 走 `/boot/overlay-user/`（目录尚未创建） |

---

## 2. 判定树落点

```
A2 内核 ≥ 6.1 ?                        → 是（6.1.31 BSP）
├─ A3 有 GPU 节点 ?                    → 是（完整，仅 status=disabled）
│   ├─ A5 panfrost 模块存在 ?          → 是（=m，依赖齐全）
│   │   ├─ 驱动已绑定 ?                → 否（DT 禁用，平台设备未创建）
│   │   └→ 【路线 A】只需启用 DT 节点 —— 本次落点
│   └─ 显示栈能吃到 GPU ?              → 否（Xvfb）→ 【路线 C】独立显示栈
└─ A9 Chromium 当前 renderer           → 软件（EGL device = Mesa Project 1.5）
```

---

## 3. 三条路线（按实测重排）

### 路线 A：官方 overlay 启用 panfrost（推荐，风险极低）

改一行配置 + 重启。用的是 **Allwinner 自己随镜像发布的 dtbo**，不是我们手搓的；即使 apply 失败，boot.cmd 会重新加载原始 dtb，**不会变砖**。

```bash
# 备份
cp /boot/orangepiEnv.txt /boot/orangepiEnv.txt.bak.$(date +%F)
# 加一行
echo 'overlays=gpu' >> /boot/orangepiEnv.txt
sync && reboot
```

**成本**：0 安装、0 磁盘、0 内存；**风险**：低（自动回滚）；**收益**：拿到 `renderD128` + 真 Mali 驱动，为后续所有 GPU 用途（不只续火花）打底。

### 路线 B：换主线内核（本机已不需要，仅作 A 失败后的备选）

只有当 BSP 的 panfrost probe 失败（dmesg 报 `Failed to get clock` / `unsupported core` 之类）才考虑。代价是要换 image+dtb、可能起不来，且 BSP→mainline 会牵动 eMMC/网卡/无线等一系列驱动，**与「不升级生产内核」的约束冲突**，所以排最后。

### 路线 C：显示栈替换（Chromium 用上 GPU 的真正门槛）

**Xvfb 是死结这一判断不变**：Xvfb 只有软件光栅，没有 DRI3、没有 GLX 硬件路径。panfrost 通了之后：

- 自带 X/EGL 的程序（`gpu_check.py`、Mesa demo、未来的本地图形应用）**立刻能用 GPU**；
- Chromium 仍然要换一个能访问 DRM 的显示服务。两个候选：
  1. **独立 Xorg + modesetting**（成熟路径，但板子 HDMI `disconnected`，需要一个 HDMI dummy 插头，或用 `video=HDMI-A-1:1280x720@60` 强制模式）；
  2. **Weston headless-backend + Xwayland**（不需要显示器，Weston 自己用 EGL/GBM 渲染，Xwayland 供 Chromium 走 GLX）。
     > 注意：这里的 headless 指 Weston 的**无显示器后端**，与第 9 节被排除的 Chromium `--headless=new` 是两件事 —— 前者照样跑**有头** Chromium，后者才是实测失败的那条路。

两条都在**独立 display 号**上验证，生产 `:99` 全程不动。

---

## 4. 实施规划（每步都有验证与回滚）

| 阶段 | 动作 | 验证 | 回滚 | 触碰生产？ |
|---|---|---|---|---|
| **P0 审计** | 已完成（本文档 1.x 节） | — | 无 | 否 |
| **P1 启用 GPU** ✅ **已完成 2026-11-19 15:05** | 三行命令，唯一改动是加一行 `overlays=gpu` | 全部达标：`renderD128` 出现、panfrost 加载、DT `okay`、EGL device 数 2 | `cp /boot/orangepiEnv.txt.bak-pre-gpu-20261119 /boot/orangepiEnv.txt && reboot` | 否（只重启一次） |
| **P2 GPU 真跑起来** ✅ **已完成同上时间** | `/root/gpu_check.py` + `/root/gl_probe.py` 双探测（GLES 与桌面 GL） | **`GL_RENDERER = Mali-G31 (Panfrost)`**、`GL_VENDOR = Panfrost`、GLES `3.1 Mesa 22.3.6`、桌面 GL `3.1`；非 root 用户同样达标 | 同 P1 | 否 |
| **P3 显示栈旁路** | 起独立 `Xorg :0`（modesetting）或 `Weston`，在独立 display 上跑独立 Chromium（`--user-data-dir=/tmp/gpucheck`） | `chrome://gpu` 的 GL_RENDERER 含 Mali；Rasterization/WebGL = Hardware accelerated | 杀掉独立 X/Weston 进程即可，`:99` 不受影响 | 否（旁路实例） |
| **P4 生产接入** | 改 `run_cron.sh` 的 DISPLAY / 启动参数 | CPU 时间、截图耗时、内存峰值、任务成功率对比基线 | 保留原脚本副本 + 开关变量 | **是（需你另行批准）** |

P1 建议安排在**没有任务要跑的时段**（当前 15:0x，距 00:00 主任务还有 9 小时，重启窗口充裕）。

---

## 5. 风险清单

| 风险 | 触发条件 | 影响 | 缓解 |
|---|---|---|---|
| overlay apply 失败 | dtbo 与 dtb 不匹配 | 无（boot.cmd 自动重载原始 dtb） | 观察串口/HDMI 打印，或直接看 `/proc/device-tree/soc/gpu@1800000/status` |
| **panfrost probe 失败** | BSP 定制驱动不认识 H616 时钟/复位 | 只是没有 GPU，系统正常 | `dmesg \| grep -i panfrost` 看具体 errno；仍不行则停在此步，不进 P3 |
| GPU 负载下 hang | 无 OPP 表导致频率/电压不当 | 页面卡死、任务超时 | 先只在 P2 空跑；接生产前做 30 分钟压力（glmark2-es2 或反复页面渲染） |
| 内存反向恶化 | 显示栈替换后 Chromium 更吃内存 | 1.5GB 上回收更频繁 | P3 用独立实例量峰值，净增 >150MB 就停在 P1 |
| 无显示器时 Xorg 起不来 | HDMI disconnected | P3 无法验证 | HDMI dummy 插头；或改走 Weston headless |
| 「装了 Mesa 就算启用」的误判 | 任何阶段 | 白折腾 | 只认 `renderD128` + `gpu_check.py` 的 renderer 字符串 + `chrome://gpu` |
| 重启打断定时任务 | 重启时间落在 00:00 附近 | 当晚漏续 | 选白天窗口；重启后 `systemctl list-timers` / `crontab -l` 复核 |

---

## 6. 验收标准

**L1 驱动层（必须全绿才继续）—— ✅ 全绿**
- ✅ `/dev/dri/renderD128` 存在（226,128）
- ✅ `cat /sys/kernel/debug/dri/128/name` = `panfrost dev=1800000.gpu`
- ✅ `dmesg | grep -i panfrost` 无 `fault` / `error` / `failed`
- ✅ EGL device 数 = 2

**L2 用户态 GL 层 —— ✅ 全绿**
- ✅ GLES `GL_RENDERER` = `Mali-G31 (Panfrost)`（device0）
- ✅ 桌面 GL `GL_VERSION` = `3.1 Mesa 22.3.6`（device0）
- ✅ 反例已排除：`llvmpipe` 只出现在 device1（Mesa 的软件兜底设备，属正常共存，不是当前后端）
- ✅ 非 root 用户（`orangepi`，已加入 `render` 组）同样拿到 Panfrost

**L3 Chromium 层（P3 后）**
- `chrome://gpu`：`GL_RENDERER` 含 `Mali`；`Rasterization: Hardware accelerated`；`WebGL: Hardware accelerated`

**L4 性能/稳定性层（P4 前）**
- 同页面加载到可交互的 CPU 时间下降 ≥ 30%
- 同一次截图/滚动耗时下降 ≥ 30%
- 进程内存峰值净增 ≤ 100MB
- 连续 72 小时：无 GPU fault、无崩溃、内存无单调增长

---

## 7. 暂停点（2026-11-19 决定：停在 P2；P3 已冻结）

P1、P2 已完成并验收（数据见第 8 节）。P2.5 已完成并排除 headless 捷径（数据见第 9 节）。**P3、P4 冻结**：不是"等维护窗口"，而是收益侧没有痛点、捷径已断，结论已定。

| 阶段 | 状态 |
|---|---|
| P1 启用 Panfrost | ✅ 已完成 |
| P2 本地 EGL/GLES 硬件加速验证 | ✅ 已完成 |
| P2.5 原生 Headless 可行性实测 | ✅ 已完成 —— **判定不满足生产要求**（第 9 节） |
| P3 独立 Chromium GPU 验证 | ⛔ 冻结（P2.5 已排除 headless 捷径，收益侧无痛点） |
| P4 评估续火花实际收益 | ⛔ 冻结 |

**决定理由（记录在案，避免以后重复讨论）**

1. P3 要引入独立显示栈（`Xorg :0` 或 Weston + Xwayland），还要先解决 HDMI 无显示器，复杂度明显上一台阶。
2. 独立 Chromium 会额外吃 300MB+，1.5GB 的板子上并不轻松。
3. **Panfrost 已启用 ≠ Chromium 已经能用它**；即使跑通，也不等于续火花真的会变快。收益没证明之前不引入复杂度。

**硬原则：不要为了让 Chromium 用上 GPU 就急着改造现有 Xvfb 环境。先证明收益，再决定要不要动它。**

**2026-11-19 追加**：原设想的"用原生 headless 替掉 Xvfb"这条捷径已在 P2.5 实测排除（第 9 节）。P3 因此冻结，不是延后 —— 结论已定，无需重测。

将来若真要重开 P3，做法（约束已定）：用独立 Chromium 实例，记录 CPU、RSS、截图耗时与 `chrome://gpu`，全程不碰生产实例、不碰 `:99`；且必须从"有头 + 独立 Xorg/Weston"入手，不要再用 headless 试。

**可选的低成本后续（都不需要现在做）**

- 想给 GPU 开动态调频：自定义 overlay 补 `operating-points-v2` 引用（5 档 OPP 表已在 dtb 里）。
- 想消掉 thermal 绑定告警：补 `#cooling-cells`。
- 这两项都会动 DT，需单独评估，且当前 432MHz 固定频率对 2D/UI 已够用。

---

## 8. P1 执行记录与验收（已完成，2026-11-19 15:05 CST）

### 8.1 执行动作（唯一改动）

```bash
# 1) 备份（原 md5 2f420de9ac60255095d2d37733b13089，156 字节，LF 行尾，无 overlays= 行）
cp -a /boot/orangepiEnv.txt /boot/orangepiEnv.txt.bak-pre-gpu-20261119

# 2) 追加一行，不覆盖任何已有设置
printf 'overlays=gpu\n' >> /boot/orangepiEnv.txt
#    新 md5 361ac0885a8d5737e57ad2b430eb2d06；diff 只有一行：7a8 > overlays=gpu

# 3) 重启（约 45 秒后 SSH 恢复，IP 未变：BOARD_IP）
nohup sh -c 'sleep 2; systemctl reboot' &
```

回滚（一条命令，随时可用）：

```bash
cp /boot/orangepiEnv.txt.bak-pre-gpu-20261119 /boot/orangepiEnv.txt && reboot
# 或：sed -i '/^overlays=gpu$/d' /boot/orangepiEnv.txt && reboot
```

### 8.2 执行前预演（为什么一次就成）

没有靠猜。重启前用 ctypes 调 `libfdt`，跑了 U-Boot `fdt apply` 的同一套流程：

- `fdt_open_into → 0`（无错误）
- `fdt_overlay_apply → 0`（无错误）
- overlay 后 `gpu@1800000/status → okay`

也就是说，启动时 dtb 的合并结果是可预期的，不是碰运气。另外确认了 `boot.scr`(3636B) = `boot.cmd`(3564B) + 72B mkimage 头，二者同源 —— 官方那套「apply 失败就重载原 dtb 继续启动」的保护确实生效，不会变砖。

### 8.3 重启前后对比（实测）

| 指标 | 重启前 | 重启后 |
|---|---|---|
| `/dev/dri/` | `card0` | `card0` + **`card1`**(226,1) + **`renderD128`**(226,128) |
| panfrost 模块 | 未加载 | `panfrost 73728 0` + `gpu_sched` + `drm_shmem_helper` |
| dmesg | 无 panfrost 记录 | `mali-g31 id 0x7093`、`clock rate = 432000000`、`Initialized panfrost 1.2.0 for 1800000.gpu on minor 1`，**无 fault / 无 error** |
| `dri/128/name` | 不存在 | `panfrost dev=1800000.gpu unique=1800000.gpu` |
| DT `gpu@1800000/status` | `disabled` | **`okay`** |
| EGL device 数 | 1 | **2** |
| **GL_RENDERER（GLES）** | **`llvmpipe (LLVM 15.0.6, 128 bits)`** | **`Mali-G31 (Panfrost)`** |
| GL_VENDOR | `Mesa/X.org` | `Panfrost` |
| GLES 版本 | `OpenGL ES 3.2` | `OpenGL ES 3.1`（Bifrost v7 硬件上限，非退步） |
| 桌面 GL 版本 | `4.5 (Compatibility)` | `3.1`（同上） |
| GPU 时钟 | `gpu0` 432MHz，enable_cnt=0 | `pll-gpu` / `gpu0` / `bus-gpu` 全部 enable=Y，432 / 432 / 200 MHz |
| devfreq | 无设备 | 仍无设备（DT 无 OPP 引用 → 固定 432MHz） |
| 温度 | 4 区 ≈ 28°C | 启动瞬时 31.7 / 31.9 / 36.4 / 33.0 → **稳态 26.2–28.0°C**（与基线一致） |
| CMA | 空闲 128752kB | 空闲 **128752kB**（GPU 几乎不占 CMA） |
| MemAvailable（稳态） | 1252MB | **1247MB**（重启后 2 分钟，净变化在噪声内 → GPU 驱动几乎不吃内存） |
| Xvfb `:99` | pid 1168 | pid 1193，参数不变，正常 |
| `/opt/douyin-spark` | 完好 | 完好（全程未触碰） |
| 进程内存首名 | Xvfb 64MB | Xvfb 64MB（无新增常驻进程） |
| 系统负载（2 分钟） | ~0.1 | 1.55（启动期，正在回落） |

> **判据是 renderer 字符串，不是 `lsmod`。** `llvmpipe (LLVM 15.0.6, 128 bits)` → `Mali-G31 (Panfrost)` 才算真的启用。

### 8.4 权限：非 root 应用也能用 GPU

`/dev/dri/renderD128` 初始是 `root:render 660`，而 `render:x:105:` 组内**没有任何成员** —— 只有 root 能用 GPU。已执行：

```bash
usermod -aG render orangepi
```

以 `orangepi` 身份（非 root）复验：`GL_RENDERER = Mali-G31 (Panfrost)`；直接 `open("/dev/dri/renderD128", O_RDWR)` 返回 fd=3 成功。
udev 已带 `TAGS=:uaccess:`，本地登录的 seat 用户会自动拿到 ACL；`render` 组是给 systemd 服务、远程会话和 cron 的兜底。

### 8.5 明确未做（边界）

| 项 | 状态 | 原因 |
|---|---|---|
| Chromium 是否用上 GPU | **未测**，预期仍是软件 | Xvfb 无 DRI3，X11 下拿不到硬件 GL；且启动一个 Chromium 会吃 300MB+，不拿生产环境试。留到 P3 用独立实例测 |
| GPU 动态调频 | **不可用** | DT 节点缺 `operating-points-v2` 引用，`/sys/class/devfreq/` 无设备 → 固定 432MHz。5 档 OPP 表（125/250/432/600/800MHz）已在 dtb 里，想开需自定义 overlay 补引用 |
| thermal 绑定告警 | 仍在 | `could not get #cooling-cells for /soc/gpu@1800000`，不影响功能，不为它冒风险 |
| 内核 | 未动 | BSP `6.1.31-sun50iw9` 原样，未升级、未换 mainline |

---
---

## 9. P2.5：Headless 模式可行性实测 —— 不满足生产要求

**动机**：如果板端的 Chromium 能在原生 headless 模式下完成首屏渲染并正常登录，就可以把 `Xvfb :99` 整层去掉，省掉约 64MB 常驻内存和一套显示栈。P3 之前先把这个"看起来很划算"的替代方案验证掉，避免以后重复投入。

**测试对象**：`orangepizero3`（root@BOARD_IP），Debian 12，Chromium **154.0.8037.57**（`apt-cache policy` 的 Candidate 同版本，`apt list --upgradable` 无 chromium/mesa/libegl → **无更新可装**）。全程**未伪造 UA、未登录、未发送任何消息**。

### 9.1 A 组：一次性渲染（`--dump-dom`）

```bash
unset DISPLAY
chromium --headless=new --no-sandbox --disable-dev-shm-usage \
  --user-data-dir=/tmp/hlprobe/p1 --window-size=1280,900 --lang=zh-CN --hide-scrollbars \
  --virtual-time-budget=12000 --dump-dom https://www.douyin.com/chat > dom1.html
```

| 观测项 | 实测值 |
|---|---|
| `--virtual-time-budget` | 12000 ms |
| `timeout` | 110 s |
| DOM 字节数 | **1227023** |
| 去标签正文长度 | **113** 字符 |
| 正文首段 | `开启读屏标签 读屏标签已关闭 抖音聊天 进入全屏 直播加载中 登录后免费畅享高清视频 扫码登录 如何扫码 打开「抖音APP」点击左上角 扫一扫 验证码登录 密码登录 获取验证码 登录 登录即代表同意 用户协议 和 隐私政策` |
| `扫码登录` / `验证码登录` / `密码登录` / `请输入手机号` | 各 1 次（**全在**） |
| `请输入密码` | 0（默认停在「验证码登录」tab，正常） |
| `type="password"` 输入框 | 0 |
| `placeholder` 列表 | `请输入手机号`、`请输入验证码` |
| `验证码中间页` / `安全验证` | 各 0 次（**未被跳转拦截页**） |
| DOM 中是否含 `HeadlessChrome` | **True** |
| 页面 UA 字符串 | `Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) HeadlessChrome/154.0.0.0 Safari/537.36` |
| 截图 | `opi-headless-chat.png`，1280x900，**50937** 字节，登录框视觉完整（扫码区、+86 手机号、验证码、登录按钮、协议行俱全） |
| `chrome://gpu` 抓取字节 | 0（本次未取到） |

**A 组表面结论**：DOM 完整、截图正常 —— 看起来 headless 完全可用。

### 9.2 B 组：真实交互会话（CDP，等价 Playwright 的实际用法）

流程：起 `--headless=new --remote-debugging-port=9222` → WebSocket 直连 → `Page.enable` / `Runtime.enable` → `Page.navigate` 到 `https://www.douyin.com/chat` → 等待 → `Runtime.evaluate` 取 `document.body.innerText` 与输入框 → 尝试点击「密码登录」→ 再等 4 s 复查。

| # | 首次采样等待 | 追加参数 | 采样 A 正文长度 | 复查 B 正文长度 | 正文内容 | `password` 输入框 | 「密码登录」tab | 点击「密码登录」 |
|---|---|---|---|---|---|---|---|---|
| B1 | **11 s** | 仅 `--headless=new --no-sandbox --disable-dev-shm-usage --window-size=1280,900 --lang=zh-CN` | **14** | **14** | `开启读屏标签 / 读屏标签已关闭` | 0 | 无（`hasPwdTab=false`） | `{ok:false}` |
| B2 | **26 s** | 同上 + `Page.bringToFront` | **14** | **19** | 多出 `抖音聊天` | 0 | 无 | `{ok:false}` |
| B3 | **75 s** | 同上 + `Page.bringToFront` | **14** | **14** | `开启读屏标签 / 读屏标签已关闭` | 0 | 无 | `{ok:false}` |
| B4 | 26 s + 虚拟时间 15 s | + `--disable-background-timer-throttling --disable-backgrounding-occluded-windows --disable-renderer-backgrounding --disable-features=CalculateNativeWinOcclusion`；用 `Emulation.setVirtualTimePolicy{policy:pauseIfNetworkFetchesPending, budget:15000}` 替换长实等（随后再等 8 s） | **14** | **19** | 多出 `抖音聊天` | 0 | 无 | `{ok:false}` |
| B5 | **30 s**（沿用虚拟时间策略） | + `--enable-unsafe-swiftshader --use-gl=angle --use-angle=swiftshader`（显式软件 GL） | **14** | **19** | `开启读屏标签 / 读屏标签已关闭 / 抖音聊天` | 0 | 无 | `{ok:false}` |
| B6 | **30 s**（沿用虚拟时间策略） | + `--run-all-compositor-stages-before-draw --disable-new-content-rendering-timeout --disable-gpu-vsync --disable-frame-rate-limit --force-device-scale-factor=1`（强制出帧） | **3**（仅 `加载中`） | **14** | 先「加载中」，再退回骨架屏 | 0 | 无 | `{ok:false}` |

六个组合的 `url` 全部停在 `https://www.douyin.com/chat`，`title` 为空，UA 全部含 `HeadlessChrome/154.0.0.0`；`document.querySelectorAll('input')` 全部为 0。

**B 组结论**：等待 11 / 26 / 30 / 75 秒、禁用后台节流与遮挡降频、强制合成器出帧、`bringToFront`、显式 SwiftShader、CDP 虚拟时间策略 —— **没有任何一组把页面推出骨架屏**；正文长度在 3～19 之间摆动，从未出现登录框。

### 9.3 C 组：GPU 观察

A 组 stderr 只出现两类记录，无一条 panfrost/Mali：

```
ERROR:gpu/command_buffer/service/gles2_cmd_decoder_passthrough.cc:1210
  Automatic fallback to software WebGL has been deprecated.
  Please use the --enable-unsafe-swiftshader flag to opt in to lower security guarantees for trusted content.
ERROR:gpu/command_buffer/service/gl_utils.cc:431 [.WebGL-...] GL Driver Message
  (OpenGL, Performance, GL_CLOSE_PATH_NV, High): GPU stall due to ReadPixels
```

**结论：本次 Headless 全程使用 SwiftShader 软件渲染，Panfrost / Mali-G31 完全没有参与。** 也就是说，即使 headless 可用，它也不会自动吃到 P1/P2 建成的 GPU 能力；要吃到还得再叠 `--use-angle=vulkan`，而 Mesa 22.3.6 对 G31 的 Vulkan 支持本身就残缺 —— 这条路比原先估计的更窄。

### 9.4 最终判定

> **原生 Headless 在真实交互会话中无法正常完成首屏渲染，不能替代现有 Xvfb。**

判据以 B 组为准：真实交互会话中页面停在骨架屏，如果切换到 headless，生产上就是白屏。A 组的完整 DOM 与截图**不构成可用性证明**，只能证明"页面代码能跑完"，不能证明"渲染能完成"。

### 9.5 现象解释（待验证）

> A 与 B 的差异**可能**与 `--virtual-time-budget` 改变页面执行时序有关：虚拟时间策略会快进定时器，拦截/校验逻辑的时间窗口被压缩，DOM 因此在渲染完成前就已完整。**Headless UA 是已被观察到的一个触发条件**（Windows 侧同一站点对 `HeadlessChrome` 直接跳转「验证码中间页」，bodyLen=4），但**现有实验尚不能证明 A/B 差异完全由 UA 检测造成**。

这一条仍是假设，不是结论。以后若有人重新研究 headless，请从"如何在不依赖虚拟时间的前提下复现 A 组的完整渲染"入手，而不是直接把 UA 结论当既定事实。

### 9.6 生产保护（本次测试的边界）

| 项 | 状态 |
|---|---|
| `/usr/bin/chromium` | 未安装、未升级、未打补丁（154.0.8037.57 原样） |
| Xvfb `:99` | **未触碰**，实测仍在跑：`1193 /usr/bin/Xvfb :99 -screen 0 800x600x24 -nolisten tcp` |
| `crontab` | 逐条核对，与测试前一致（含 `/opt/douyin-spark/tools/net_watchdog.sh`、wtq-checkin、glm47-nightly） |
| `/boot/orangepiEnv.txt` | md5 `361ac0885a8d5737e57ad2b430eb2d06`（P1 后状态，未变） |
| 备份 `/boot/orangepiEnv.txt.bak-pre-gpu-20261119` | md5 `2f420de9ac60255095d2d37733b13089`，完好 |
| `/dev/dri/` | `card0` `card1` `renderD128` 均在，Panfrost 未受影响 |
| `/opt/douyin-spark` | 未触碰 |
| 临时 profile | `/tmp/hlprobe`、`/tmp/cdpprof`、`/tmp/org.chromium.Chromium.*` **已全部删除**（删除后复核：两者均不存在） |
| 遗留脚本 | `/tmp/probe2.sh`、`/tmp/analyze_probe.py`、`/tmp/cdp_probe_board.py`、`/tmp/patch_cdp*.py` 为只读探测脚本，不含凭据，可随时 `rm` |

全程使用独立 `--user-data-dir` 与独立 `--remote-debugging-port=9222`，未与生产实例共用 profile。

### 9.7 后续状态

**P3 暂停，不继续投入。**

理由已由实测给出，不再是推测：

1. headless 这条路在真实会话里走不通（B 组六种组合全部失败）；
2. 就算走通，headless 默认也不吃 Panfrost（C 组 SwiftShader），还要额外背 Vulkan 风险；
3. 现有 Xvfb + 有头 Chromium 每晚稳定发出 3 条 / 5 条，**收益侧没有痛点**。

这次实验的价值是**排除**了一个看起来很有吸引力、实际不满足生产要求的替代方案。结论保留在第 9 节，以后不必重跑。

---

*第 9 节为 P2.5 实测记录，追加于 2026-11-19。第 7 节的结论据此收紧：P1/P2 已完成并验收，P3/P4 由"暂缓"改为**冻结**（不是延后）。*

---

*本文档不含密钥、账号或内网凭据。板上新增文件只有 `/root/gpu_check.py`、`/root/gl_probe.py`（EGL/GL 探测脚本）与 `/boot/orangepiEnv.txt.bak-pre-gpu-20261119`（配置备份），均可随时删除或还原。第 9 节的 headless 实测另留下 `/tmp/probe2.sh`、`/tmp/analyze_probe.py`、`/tmp/cdp_probe_board.py`、`/tmp/patch_cdp*.py` 四个只读探测脚本与 `/tmp/board_cleanup.sh`，不含凭据，随时可删；临时 Chromium profile 已全部清理。*