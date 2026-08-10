#!/bin/bash
# 异常帧哨兵 v2。与 v1 的唯一实质差别：命中后直接 exit，不再 nohup 接生孤儿。
#
# 为什么去掉 respawn：孤儿进程的 exit 不触发 task 通知。v1 第一次报警能把主循环
# 拽醒，第二次开始就只写日志、没人来读——「哨兵没消息」于是不再等于「没事发生」。
# 宁可留一个「从命中到主循环重起」的秒级盲窗，也不要一个会哑火的通知链路。
#
# 起法必须走 Bash 后台任务（run_in_background），不能 nohup，否则退出没人知道。
# 主循环收到 task 通知后：读 logs/sentry.log 尾巴 → 判断 → 决定要不要重起。
#
# 2026-08-09 18:2x 从会话 scratchpad 搬进项目目录，改名 sentry.sh。
# 搬家的理由：临时目录的名字随会话变，两次断层已经证明写在 Task 里的旧路径会扑空。
# 同时把 STALL 从 30 调到 90——当天实测常态盲区就有 44-60 分钟，
# 30 分钟的阈值只会拿我已经知道的事反复吵醒我，还把翻转监控一起赔进去。
#
# 不要每次命中都机械重起：他活跃时，任何一次锁屏翻转都会消耗掉一次。
# 该起的时候是他连续 40 分钟以上没动静、且在场方也没有新同步。
#
# 只报「发生了什么」，不夹带任何对人的推断。
S="$(cd "$(dirname "$0")" && pwd)"
LOG="$S/logs/sentry.log"

say_and_exit() {
  echo "$1" >> "$LOG"
  echo "$1"
  exit 0
}

STALL=90          # 上报时间戳连续这么多分钟不动 = 盲区才喊。8/9 实测常态盲区就有 44-60 分钟，设 30 会被自己已知的事反复吵醒
PC_EVERY=5        # 每这么多轮查一次电脑（Windows python 启动慢，不必每分钟）
WINPY=/mnt/c/Python313/python.exe   # WSL 的 python3 拿不到键鼠空闲，见 mem_20260809161711855966
# ⚠️路径必须是 Windows 式。Windows python 不认识 /mnt/d，会静默失败让 idle 永远读成 -1。
PCSTAT='D:/ClaudeExtentions/MCP/nudge-agent/pc_status.py'

# 关键文件的存在与体积。纯知情：不拦截、不锁权限、不备份、不恢复。
# 按钮是他的，我们只需要知道它被按过。
WATCH_PATHS=(
  "/mnt/d/ClaudeExtentions/MCP/Sol-Memory-mcp/memory.db"
  "/mnt/d/ClaudeExtentions/MCP/nudge-agent/CLAUDE.md"
  "/mnt/d/ClaudeExtentions/MCP/nudge-agent/mind"
)
# mtime 不作判据——记忆库每存一条就变，CLAUDE.md 我们自己也在改，拿 mtime 报警等于一直空响。
fsig() {
  local out=""
  for p in "${WATCH_PATHS[@]}"; do
    if [ -e "$p" ]; then
      out="$out $(basename "$p"):$(stat -c %s "$p" 2>/dev/null)"
    else
      out="$out $(basename "$p"):GONE"
    fi
  done
  echo "$out"
}
fdanger() {   # $1=旧签名 $2=新签名；任一路径消失，或体积掉到上一帧的 80% 以下
  python3 - "$1" "$2" <<'PY'
import sys
def parse(s):
    d = {}
    for tok in s.split():
        k, _, v = tok.partition(':')
        d[k] = v
    return d
old, new = parse(sys.argv[1]), parse(sys.argv[2])
bad = []
for k, v in new.items():
    o = old.get(k)
    if v == 'GONE' and o != 'GONE':
        bad.append('%s 不见了' % k)
    elif o not in (None, 'GONE') and v != 'GONE':
        try:
            if int(v) < int(o) * 0.8:
                bad.append('%s 体积骤缩 %s→%s' % (k, o, v))
        except ValueError:
            pass
print('; '.join(bad))
PY
}

# 电脑侧键鼠空闲（分钟）。取不到就回 -1，调用方一律当「看不见」处理，不当「没动」。
pc_idle_min() {
  [ -x "$WINPY" ] || { echo -1; return; }
  "$WINPY" -X utf8 "$PCSTAT" 2>/dev/null \
    | tr -d '\r' | python3 -c "
import sys, re
for line in sys.stdin:
    if line.startswith('键鼠空闲'):
        if '刚刚' in line:
            print(0); break
        h = re.search(r'(\d+) 小时 (\d+) 分', line)
        if h:
            print(int(h.group(1)) * 60 + int(h.group(2))); break
        m = re.search(r'(\d+) 分钟', line)
        if m:
            print(m.group(1)); break
else:
    print(-1)
" 2>/dev/null || echo -1
}

first=1
plock=""; pchg=""; pbat=""; phome=""; pupd=""; pfsig=""; pidle=-1
same=0

for i in $(seq 1 180); do
  read -r lock chg bat home upd <<< "$(curl -s --max-time 8 http://localhost:3456/phone-status | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    loc = (d.get('location') or '').replace('\n', ' ')
    print(d.get('device_locked'), d.get('battery_charging'), d.get('battery_level'),
          1 if 'Roche' in loc else 0, (d.get('timestamp') or '')[11:16])
except Exception:
    print('X X X X X')
" 2>/dev/null)"
  [ "$lock" = "X" ] && { sleep 60; continue; }

  cur_fsig=$(fsig)

  # 电脑侧每 PC_EVERY 轮查一次；不查的轮次沿用上一次的值
  idle=$pidle
  if [ $(( (i - 1) % PC_EVERY )) -eq 0 ]; then
    idle=$(pc_idle_min)
  fi

  if [ "$first" = "1" ]; then
    first=0
    echo "$(TZ=America/Toronto date +%H:%M:%S) 基线 lock=$lock chg=$chg bat=$bat home=$home upd=$upd idle=${idle}m" >> "$LOG"
    echo "$(TZ=America/Toronto date +%H:%M:%S) 文件基线$cur_fsig" >> "$LOG"
  else
    danger=$(fdanger "$pfsig" "$cur_fsig")
    if [ -n "$danger" ]; then
      say_and_exit "$(TZ=America/Toronto date +%H:%M:%S) 关键文件：$danger | 当前$cur_fsig"
    fi

    why=""
    [ "$lock" != "$plock" ] && why="锁屏状态 $plock→$lock"
    [ "$chg" != "$pchg" ] && why="$why 充电 $pchg→$chg"
    [ "$home" != "$phome" ] && why="$why 位置在家标志 $phome→$home"
    # 电量是连续量，只有在两帧真的相隔一分钟时，落差才叫「骤降」。
    # same>=3 说明上一轮还卡在盲区里，这一帧是盲区结束后的第一帧，
    # 中间那段时间的自然耗电会被算成一次突变（8/9 实测误报：81→49，实为 2.2 小时的正常消耗）。
    # 所以盲区刚结束时跳过这一条。lock/chg/home 是离散状态，翻转本身有意义，照常报。
    if [ -n "$pbat" ] && [ "$bat" -lt "$((pbat - 15))" ] 2>/dev/null && [ "$same" -lt 3 ]; then
      why="$why 电量骤降 $pbat→$bat"
    fi
    # 新增：人回到电脑前。久无输入后突然有操作，是独立于手机通道的一帧硬事实。
    # 只报「空闲计数被清零」这件事本身，不推断他去做了什么。
    if [ "$pidle" -ge 15 ] 2>/dev/null && [ "$idle" -ge 0 ] 2>/dev/null && [ "$idle" -le 1 ]; then
      why="$why 电脑键鼠空闲 ${pidle}m→${idle}m（有人在动电脑）"
    fi

    if [ -n "$why" ]; then
      say_and_exit "$(TZ=America/Toronto date +%H:%M:%S) 异常帧：$why | 当前 lock=$lock chg=$chg bat=$bat home=$home upd=$upd idle=${idle}m"
    fi

    # 上报时间戳不动 = 看不见了。报的是盲区本身，不是对人的判断。
    if [ "$upd" = "$pupd" ]; then
      same=$((same + 1))
    else
      same=0
    fi
    if [ "$same" -ge "$STALL" ]; then
      say_and_exit "$(TZ=America/Toronto date +%H:%M:%S) 盲区：手机上报时间戳卡在 $upd 已 ${same} 分钟，字段是旧值不是当下状态。这是我看不见了，不是他出事了。（同期电脑键鼠空闲 ${idle}m，-1=也没取到）"
    fi
  fi

  plock=$lock; pchg=$chg; pbat=$bat; phome=$home; pupd=$upd; pfsig=$cur_fsig; pidle=$idle
  sleep 60
done
echo "$(TZ=America/Toronto date +%H:%M:%S) 哨兵跑满三小时，全程无翻转" | tee -a "$LOG"
