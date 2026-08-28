#!/bin/bash
# Diagnose why no agent joins the room. Run it on the server:
#
#   cd /home/movenetics/public_html/call.moveneticsdigital.com && ./check_agent.sh
#
set -u

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
export APP_DIR
# Override for local testing: PYTHON=/path/to/python ./check_agent.sh
PYTHON="${PYTHON:-/home/movenetics/virtualenv/public_html/call.moveneticsdigital.com/3.12/bin/python}"
cd "$APP_DIR" || exit 1

hr() { printf '\n== %s ==\n' "$1"; }

hr "interpreter"
if [ -x "$PYTHON" ]; then
	printf '  OK    %s\n' "$PYTHON"
else
	printf '  FAIL  not found: %s (falling back to python3)\n' "$PYTHON"
	PYTHON=python3
fi
"$PYTHON" -V 2>&1 | sed 's/^/        /'

hr "packages"
"$PYTHON" - <<'PY'
import importlib.metadata as md
for pkg in ["livekit-agents", "livekit-api", "livekit-plugins-openai",
            "livekit-plugins-silero", "openai", "flask", "python-dotenv"]:
    try:
        print(f"  OK    {pkg} {md.version(pkg)}")
    except Exception:
        print(f"  FAIL  {pkg} NOT INSTALLED")
try:
    from livekit.agents import inference  # noqa: F401
    print("  OK    livekit.agents.inference available (LiveKit-hosted speech)")
except Exception as exc:
    print(f"  FAIL  livekit.agents.inference unavailable -> {exc}")
try:
    from livekit.plugins import silero  # noqa: F401
    print("  OK    livekit.plugins.silero importable")
except Exception as exc:
    print(f"  FAIL  silero VAD unavailable -> {exc}")
PY

hr "credentials the worker will see"
# Imports app.py so this reports exactly what agent.py resolves at startup:
# real env vars first, then .env, then the SetEnv lines in .htaccess.
# LOG_CONSOLE=0 keeps app.py's startup banner out of this report.
LOG_CONSOLE=0 "$PYTHON" - <<'PYEOF'
import os
import sys
sys.path.insert(0, os.environ.get("APP_DIR", "."))
try:
    import app  # noqa: F401  (loads .env and .htaccess exactly as the worker does)
except Exception as exc:
    print(f"  FAIL  app.py failed to import -> {exc!r}")
    raise SystemExit(1)

required = ["LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET", "OPENAI_API_KEY"]
missing = []
for name in required:
    value = os.getenv(name)
    shown = value if (name == "LIVEKIT_URL" and value) else ("set" if value else "MISSING")
    print(f"  {'OK   ' if value else 'FAIL '} {name}: {shown}")
    if not value:
        missing.append(name)

if missing:
    print()
    print("  -> The worker cannot register with LiveKit without these.")
    print("     Add them as SetEnv lines in .htaccess via the cPanel env var UI,")
    print("     or create a .env file here - see .env.example.")
PYEOF

hr "worker process"
if pgrep -af "agent\.py start" 2>/dev/null; then
	printf '  OK    worker is running (above)\n'
else
	printf '  FAIL  no worker running -> start it with ./start_agent.sh\n'
fi

hr "logs/agent.log (last 25 lines)"
if [ -f logs/agent.log ]; then
	tail -25 logs/agent.log
else
	printf '  FAIL  logs/agent.log does not exist - the worker has never started\n'
fi

hr "logs/agent-stdout.log (last 25 lines)"
if [ -f logs/agent-stdout.log ]; then
	tail -25 logs/agent-stdout.log
else
	printf '  (none yet)\n'
fi

printf '\nDone. Fix every FAIL above, then run ./start_agent.sh and place a test call.\n'
