#!/usr/bin/env bash
# 从 WSL 一条命令重启 Windows 侧 nudge 注入器：查锁 → 杀旧 → 起新 → 验单例。
# 注入器必须跑在 Windows，不能搬进 WSL：
#   ① 它直连 memory.db 生成 context 记忆段，WSL 经 drvfs 开 WAL 库会 disk I/O error；
#   ② 它 import 的 pc_status 在 WSL python 下键鼠空闲/前台窗口字段静默消失。
# 用法：bash restart_injector.sh
set -e
NETSTAT=/mnt/c/Windows/System32/netstat.exe
TASKKILL=/mnt/c/Windows/System32/taskkill.exe
PS=/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe

old_pid=$($NETSTAT -ano | grep -E '127\.0\.0\.1:48765.+LISTENING' | awk '{print $NF}' | head -1)
if [ -n "$old_pid" ]; then
    echo "杀旧注入器 PID $old_pid"
    $TASKKILL /PID "$old_pid" /F
    sleep 2
else
    echo "48765 无监听，直接起新"
fi

$PS -NoProfile -Command "Start-Process -FilePath 'py' -ArgumentList 'nudge_inject.py' -WorkingDirectory 'D:\\ClaudeExtentions\\MCP\\nudge-agent' -WindowStyle Minimized"
sleep 5

listening=$($NETSTAT -ano | grep -E '127\.0\.0\.1:48765.+LISTENING' || true)
if [ -z "$listening" ]; then
    echo "失败：48765 未见监听，注入器没起来（看 logs/ 下最新日志）"
    exit 1
fi
echo "$listening"
count=$(echo "$listening" | wc -l)
if [ "$count" -eq 1 ]; then
    echo "锁绑定 OK，单实例"
else
    echo "警告：48765 有 $count 个监听，疑似双实例！"
    exit 2
fi
