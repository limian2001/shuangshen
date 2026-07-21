#!/bin/bash
# ─────────────────────────────────────────────────────────────
# 生产环境 .env 更新 —— 阿里云百炼 CosyVoice 声音复刻
# 用法（服务器上）：
#   cd /app/shuangshen && sudo bash scripts/update_env_aliyun.sh
#
# 幂等：已存在的键会被更新，不存在的追加；执行前自动备份。
# ─────────────────────────────────────────────────────────────
set -e

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$APP_DIR/.env"

if [ ! -f "$ENV_FILE" ]; then
  echo "❌ 找不到 $ENV_FILE"; exit 1
fi

# ── 1. 备份 ──
BACKUP="$ENV_FILE.bak.$(date +%Y%m%d%H%M%S)"
cp "$ENV_FILE" "$BACKUP"
echo "📦 已备份原配置 → $BACKUP"

# ── 2. 待写入的配置 ──
set_env() {
  local key="$1" val="$2"
  if grep -q "^${key}=" "$ENV_FILE"; then
    # 用 python 替换，避免 sed 对特殊字符（/ . &）的转义问题
    python3 - "$ENV_FILE" "$key" "$val" <<'PY'
import sys
f, k, v = sys.argv[1], sys.argv[2], sys.argv[3]
lines = open(f).read().splitlines()
out = [(k + "=" + v) if l.startswith(k + "=") else l for l in lines]
open(f, "w").write("\n".join(out) + "\n")
PY
    echo "  ✏️  更新 ${key}"
  else
    printf '%s=%s\n' "$key" "$val" >> "$ENV_FILE"
    echo "  ➕ 新增 ${key}"
  fi
}

echo "⚙️  写入阿里云语音配置..."
set_env "TTS_PROVIDER"        "aliyun"
set_env "DASHSCOPE_API_KEY"   "sk-ws-H.EHILPDE.Xdhb.MEQCIFBMCrLERScWV6rnHavS51Sv9k9HHpWLyKY_mebETvgVAiBF73IATR2jxO_w4bPvnR2A11PaD-Xt6jygaZ9qIKDunA"
set_env "DASHSCOPE_WORKSPACE" "ws-6zgz89kg1ebf60b5"
set_env "COSYVOICE_MODEL"     "cosyvoice-v3.5-flash"
set_env "PUBLIC_BASE_URL"     "https://app.mianmianlife.com"
set_env "VOICE_CLONE_COST"    "1000"
set_env "VOICE_CLONE_FREE"    "1"

# ── 3. 校验 ──
echo ""
echo "🔍 当前语音相关配置："
grep -E "^(TTS_PROVIDER|DASHSCOPE_WORKSPACE|COSYVOICE_MODEL|PUBLIC_BASE_URL|VOICE_CLONE_)" "$ENV_FILE" | sed 's/^/   /'
if grep -q "^DASHSCOPE_API_KEY=sk-" "$ENV_FILE"; then
  echo "   DASHSCOPE_API_KEY=sk-ws-...（已设置，隐藏）"
else
  echo "   ❌ DASHSCOPE_API_KEY 异常"; exit 1
fi

# ── 4. 重建容器使新环境变量生效 ──
echo ""
read -p "▶️  现在重新部署使配置生效？(y/N) " yn
if [ "$yn" = "y" ] || [ "$yn" = "Y" ]; then
  bash "$APP_DIR/deploy.sh"
  echo ""
  echo "✅ 部署完成。验证："
  echo "   docker logs shuangshen --tail 20"
  echo "   小程序 → 我的声音 → 复刻测试"
else
  echo "⏸  已跳过部署。稍后手动执行：sudo bash deploy.sh"
fi
