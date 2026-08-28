#!/bin/bash
# Keeps the LiveKit voice worker running.
#
# Passenger only runs the Flask app (passenger_wsgi.py). The worker in agent.py
# is a separate long-lived process that must register with LiveKit Cloud before
# any caller can be answered -- without it, callers join a room that no agent
# ever enters.
#
# Safe to run repeatedly: it exits immediately if the worker is already up, so
# it doubles as a cron keep-alive. Add this to crontab:
#
#   */5 * * * * /home/movenetics/public_html/call.moveneticsdigital.com/start_agent.sh
#
set -u

APP_DIR="/home/movenetics/public_html/call.moveneticsdigital.com"
PYTHON="/home/movenetics/virtualenv/public_html/call.moveneticsdigital.com/3.12/bin/python"

cd "$APP_DIR" || exit 1

if pgrep -f "agent\.py start" > /dev/null; then
	exit 0
fi

mkdir -p logs
nohup "$PYTHON" agent.py start >> logs/agent-stdout.log 2>&1 &
echo "started livekit worker pid $! at $(date -Is)" >> logs/agent-stdout.log
