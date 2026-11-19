#!/usr/bin/env bash
# 把代码部署到跑生产的那台机器 —— 唯一入口, 别再手写 tar。
#
# 为什么要有这个脚本(2026-11-02 真实事故): 手写 tar 时把本地那份 19 字节的
# config.json 桩一起打了进去, 覆盖掉部署机上真正的配置。当晚 accounts 为空,
# 跑了 0 个好友却返回 rc=0, 巡检判健康, 告警通道也没配 —— 静默断火一整晚。
#
# 四条硬规矩:
#   1. 永远不传 config.json / .env / runtime/ / .venv / .git —— 配置、口令与
#      登录态各机独立。tar 不读 .gitignore, 排除项只能一条条写在这里。
#      打包完还会再验一遍压缩包内容, 混进敏感文件就直接停手(见 assert_clean)。
#   2. 远端每一步都在 `bash -s` + `set -euo pipefail` 里跑 —— 不许用 `;` 把命令链
#      断开, 否则 tar 失败也会被后面 chown 的成功掩盖, 一路 rc=0 静默上线半套代码。
#   3. 传完必须校验目标机 accounts 非空(走 src/config.py::preflight), 失败就回滚
#   4. 传完跑一次 import 冒烟, 失败也回滚
#
# 回滚不是万能的: 它能把被覆盖的文件还原成上一版、并把本次新增的文件删掉,
# 但删掉的文件和运行时副作用(已重启的服务、已发出去的消息)它管不了。
#
# 用法:
#   SPARK_HOST=user@host ./tools/deploy.sh
#   SPARK_HOST      远端(支持 ssh config 别名), 必填
#   SPARK_REMOTE_DIR 远端部署目录, 默认 /opt/douyin-spark
#   SPARK_REMOTE_USER 远端跑服务的用户, 默认当前用户名
set -euo pipefail

HOST="${SPARK_HOST:?请先设置 SPARK_HOST, 例如 SPARK_HOST=user@host ./tools/deploy.sh}"
REMOTE_DIR="${SPARK_REMOTE_DIR:-/opt/douyin-spark}"
REMOTE_USER="${SPARK_REMOTE_USER:-$(whoami)}"
LOCAL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAMP="$(date +%Y%m%d-%H%M%S)"
NEW_TGZ="/tmp/spark-deploy-$STAMP.tgz"
PREV_TGZ="/tmp/spark-prev-$STAMP.tgz"
PREV_LIST="/tmp/spark-prev-$STAMP.list"
NEW_LIST="/tmp/spark-new-$STAMP.list"
CONF_BAK="/tmp/spark-config-$STAMP.json"
SMOKE="$(mktemp)"
trap 'rm -f "$SMOKE" "$NEW_TGZ"' EXIT

cat >"$SMOKE" <<'PY'
import sys
sys.path.insert(0, '.')
from src.config import load_config, preflight

cfg = load_config()
preflight(cfg)                    # accounts 为空 / 条目缺 name 会抛 ConfigError
print('accounts:', [a.get('name') for a in cfg['accounts']])
print('notify_cmd:', 'yes' if cfg.get('notify_cmd') else 'NO(失败时不会有任何告警)')
import src.scheduler  # noqa: F401   冒烟: 主链路模块能 import
print('import ok')
PY

cd "$LOCAL_DIR"
echo "[deploy] 打包 (排除口令、配置、登录态与虚拟环境)"
tar --exclude=.venv --exclude=runtime --exclude=__pycache__ --exclude=.git \
    --exclude=runtime-dev --exclude='*.log' --exclude=config.json \
    --exclude=.env --exclude='.env.local' --exclude=storage.json \
    --exclude='*.tgz' --exclude=.pytest_cache --exclude=.ruff_cache \
    -czf "$NEW_TGZ" .

# 排除项写错一个字母就是把口令送上网, 所以不靠人眼复核, 直接问压缩包自己。
assert_clean() {
    local bad
    # .env.example / .env.sample / .env.template 是给人看模板, 必须上传;
    # 除此之外的 .env 与 .env.* 都可能带着真口令。
    #
    # 正则要点:
    #   .env 后面用 (\.[^/]*)? 而不是 \.[a-z]+ —— 多段名字(.env.production.local)
    #   是真实存在的写法, 老写法只匹配一段, 漏过去就是口令上网。
    #   凭据文件名也不止 .env/storage.json: Playwright 的 cookies.json、GCP 的
    #   credentials.json、各种私钥都是同一个后果。
    #
    #   私钥按两条规则一起卡, 缺哪条都能漏:
    #     a) 文件名 id_* 一律拦 —— 不再只列 id_rsa/id_dsa/id_ecdsa/id_ed25519 四个
    #        精确名。ssh-keygen -t ed25519-sk 生成的是 id_ed25519_sk, 开发者自己
    #        改名成 id_work 的也常见, 白名单式精确名对它们全失效。
    #     b) 扩展名 .pem / .key 一律拦 —— 证书私钥、导出的 kubeconfig 私钥、
    #        openssl 产物通常走这两个后缀, 文件名毫无规律。
    #   仓库里现在没有任何 .pem / .key 文件, 所以放宽这两条不会误伤已提交的文件;
    #   真需要放行某个模板时, 往下面那条白名单 grep 里加即可。
    local pat
    pat='(^|/)(\.env(\.[^/]*)?|storage\.json|state\.json|auth\.json'
    pat="$pat"'|credentials\.json|cookies?\.json|id_[a-z0-9_]*)\.?$'
    pat="$pat"'|\.(pem|key|p12|pfx)$'
    bad="$(tar -tzf "$1" | grep -Ei "$pat" \
        | grep -Eiv '(\.env\.(example|sample|template)|\.(pem|key)\.example)$' \
        || true)"
    if [ -n "$bad" ]; then
        echo "[deploy][ERROR] 压缩包里混进了不该上传的文件, 已中止:" >&2
        echo "$bad" | sed 's/^/    /' >&2
        exit 1
    fi
}
assert_clean "$NEW_TGZ"

echo "[deploy] 备份远端当前代码与配置"
ssh "$HOST" "REMOTE_DIR='$REMOTE_DIR' PREV_TGZ='$PREV_TGZ' PREV_LIST='$PREV_LIST' \
    CONF_BAK='$CONF_BAK' bash -s" <<'SH'
set -euo pipefail
cd "$REMOTE_DIR"
tar --exclude=.venv --exclude=runtime --exclude=__pycache__ --exclude=.git \
    --exclude=config.json -czf "$PREV_TGZ" .
cp -a config.json "$CONF_BAK"
tar -tzf "$PREV_TGZ" | sort >"$PREV_LIST"
echo backed-up
SH

rollback() {
    echo "[deploy][ERROR] 校验失败, 回滚代码与配置" >&2
    ssh "$HOST" "REMOTE_DIR='$REMOTE_DIR' REMOTE_USER='$REMOTE_USER' \
        PREV_TGZ='$PREV_TGZ' PREV_LIST='$PREV_LIST' NEW_LIST='$NEW_LIST' \
        CONF_BAK='$CONF_BAK' bash -s" <<'SH' || echo "[deploy][ERROR] 回滚本身也失败了, 需要人工介入" >&2
set -euo pipefail
cd "$REMOTE_DIR"
tar -xzf "$PREV_TGZ"
cp -a "$CONF_BAK" config.json
# 上一版没有、这一版新增的文件不会被 tar 覆盖还原, 得显式删掉,
# 否则回滚完的工作区是「旧代码 + 残留新文件」的混合体。
if [ -f "$NEW_LIST" ] && [ -f "$PREV_LIST" ]; then
    comm -13 "$PREV_LIST" "$NEW_LIST" | while IFS= read -r f; do
        case "$f" in
            ''|'.'|'./') continue ;;
            *..*) continue ;;
        esac
        rm -rf -- "${f#./}" 2>/dev/null || true
    done
fi
chown -R "$REMOTE_USER:$REMOTE_USER" . || true
echo rolled-back
SH
    exit 1
}

echo "[deploy] 上传并解包到 $HOST:$REMOTE_DIR"
scp -q "$NEW_TGZ" "$HOST:$NEW_TGZ"
# 解包后必须收拾权限: 从 Windows 挂载盘(WSL /mnt/c)或某些 tar 打包出来的成员带着
# 0777, 于是 src/ tests/ 下每个 .py 都变成可执行 —— 板端 ruff 会刷出一整屏
# EXE002 The file is executable but no shebang is present, 真正的 lint 错误被淹掉。
#
# 规则是「按位置决定」, 不是「保留 git 里的原样」(tar 出来的 mode 本来就不可信):
#   1. 全仓 .py 先统一去掉 x 位(src/ tests/ 里那些都是被 import 的, 不该可执行);
#   2. 再给两个入口 shell 脚本加回 x 位;
#   3. tools/ 下的 .sh 与 .py 一律赋 x 位 —— 它们都是 #!/usr/bin/env ... 开头的
#      可执行工具。注意 git 索引里 tools/check_js.py 等是 100644(不是 100755),
#      但都有 shebang, 所以这里赋 x 不会触发 EXE002; 反过来说, 谁新加一个 tools/
#      下的 .py 却不写 shebang, 部署后就会在 lint 里冒出来。
# 注意整段是 `set -euo pipefail` 的一条链, 任何一步失败都会让 ssh 返回非 0 从而回滚;
# 千万不要再用 `;` 把 chmod 和 tar 拆成互不相关的独立命令。
ssh "$HOST" "REMOTE_DIR='$REMOTE_DIR' REMOTE_USER='$REMOTE_USER' \
    NEW_TGZ='$NEW_TGZ' NEW_LIST='$NEW_LIST' bash -s" <<'SH' || rollback
set -euo pipefail
cd "$REMOTE_DIR"
# 先出清单, 后解包 —— 顺序反了的话, 解包中途失败(磁盘满/中途断连)时
# $NEW_LIST 还不存在, rollback 里那段 comm 会被整段跳过, 于是"旧代码 +
# 残留新文件"的混合工作区被留在板上, 而回滚还报成功。
tar -tzf "$NEW_TGZ" | sort >"$NEW_LIST"
tar -xzf "$NEW_TGZ"
find . -path ./.venv -prune -o -name '*.py' -exec chmod a-x {} +
for f in run_cron.sh run_dev.sh; do
    [ -f "$f" ] && chmod +x "$f"
done
for f in tools/*; do
    case "$f" in
        *.sh|*.py) chmod +x "$f" ;;
    esac
done
chown -R "$REMOTE_USER:$REMOTE_USER" .
echo unpacked
SH

echo "[deploy] 校验配置与可导入性"
ssh "$HOST" "cd $REMOTE_DIR && sudo -u $REMOTE_USER .venv/bin/python -" \
    <"$SMOKE" || rollback

echo "[deploy] 完成: $STAMP"
echo "[deploy] 提醒: 登录态(runtime/storage.json)不走这个脚本, 首次部署请单独传(见 docs/deployment.md)"
