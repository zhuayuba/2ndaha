#!/bin/bash
# 二手顿悟 · 定时任务安装脚本
# 用法: bash setup_cron.sh

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$(which python3)"
SCRIPT_PATH="$SCRIPT_DIR/curator.py"
PLIST_NAME="com.ershou.c curator"
PLIST_PATH="$HOME/Library/LaunchAgents/$PLIST_NAME.plist"

echo "🪵 二手顿悟 · 定时任务安装"
echo "=============================="
echo ""

# 检查 config.json
if ! grep -q "请替换" "$SCRIPT_DIR/config.json" 2>/dev/null; then
    echo "✅ config.json 已配置"
else
    echo "⚠️  请先编辑 config.json，填入你的 Lark Webhook URL 和 LLM API Key"
    echo "   文件路径: $SCRIPT_DIR/config.json"
    echo ""
fi

# 写入 launchd plist
echo "📝 创建定时任务（周二、周五 早 7:00 执行）..."

cat > "$PLIST_PATH" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$PLIST_NAME</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>$SCRIPT_PATH</string>
    </array>
    <key>StartCalendarInterval</key>
    <array>
        <dict>
            <key>Weekday</key>
            <integer>2</integer>
            <key>Hour</key>
            <integer>7</integer>
            <key>Minute</key>
            <integer>0</integer>
        </dict>
        <dict>
            <key>Weekday</key>
            <integer>5</integer>
            <key>Hour</key>
            <integer>7</integer>
            <key>Minute</key>
            <integer>0</integer>
        </dict>
    </array>
    <key>StandardOutPath</key>
    <string>$SCRIPT_DIR/cron.log</string>
    <key>StandardErrorPath</key>
    <string>$SCRIPT_DIR/cron_error.log</string>
    <key>WorkingDirectory</key>
    <string>$SCRIPT_DIR</string>
</dict>
</plist>
EOF

# 加载到 launchd
launchctl unload "$PLIST_PATH" 2>/dev/null
launchctl load "$PLIST_PATH"

echo ""
echo "✅ 定时任务已安装"
echo ""
echo "📅 执行时间: 每周二、周五 早上 7:00"
echo "📋 日志文件: $SCRIPT_DIR/cron.log"
echo ""
echo "常用命令:"
echo "  手动运行一次:  python3 $SCRIPT_PATH"
echo "  查看运行日志:  cat $SCRIPT_DIR/cron.log"
echo "  停止定时任务:  launchctl unload $PLIST_PATH"
echo "  查看任务状态:  launchctl list | grep ershou"
