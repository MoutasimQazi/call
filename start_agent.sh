#!/bin/bash
# Keeps the LiveKit voice worker running.
#
# Passenger only runs the Flask app (passenger_wsgi.py). The worker in agent.py
# is a separate long-lived process that must register with LiveKit Cloud before
# any caller can be answered -- without it, callers join a room that no agent
# ever enters.
#
# Safe to run repeatedly: it exits immediately if the worker is already up, so
# it doubles as a cron keep-alive. Add to crontab:
#
#   SHELL="/usr/local/cpanel/bin/jailshell"
#   */5 * * * * /home/movenetics/public_html/call.moveneticsdigital.com/start_agent.sh >> /home/movenetics/agent-cron.log 2>&1
#
set -u

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
# Override for testing: PYTHON=/path/to/python ./start_agent.sh
PYTHON="${PYTHON:-/home/movenetics/virtualenv/public_html/call.moveneticsdigital.com/3.12/bin/python}"
PIDFILE="$APP_DIR/logs/agent.pid"

cd "$APP_DIR" || exit 1
mkdir -p logs

running() {
	# The PID file is the primary check: kill -0 is a shell builtin and works
	# inside cPanel's jailshell, where pgrep is often unavailable. Getting this
	# wrong would start a second worker on every cron tick.
	if [ -f "$PIDFILE" ]; then
		pid="$(cat "$PIDFILE" 2>/dev/null)"
		if [ -n "${pid:-}" ] && kill -0 "$pid" 2>/dev/null; then
			return 0
		fi
	fi
	if command -v pgrep > /dev/null 2>&1; then
		pgrep -f "agent\.py start" > /dev/null 2>&1 && return 0
	fi
	return 1
}

if running; then
	exit 0
fi

if [ ! -x "$PYTHON" ]; then
	echo "$(date -Is) start_agent: interpreter not found: $PYTHON" >> logs/agent-stdout.log
	exit 1
fi

rm -f "$PIDFILE"
nohup "$PYTHON" agent.py start >> logs/agent-stdout.log 2>&1 &
echo $! > "$PIDFILE"
echo "$(date -Is) start_agent: started worker pid $(cat "$PIDFILE")" >> logs/agent-stdout.log
