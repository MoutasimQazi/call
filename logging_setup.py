"""Central logging configuration for the Frush voice-ordering stack.

Both processes -- the Flask web app (app.py) and the LiveKit voice worker
(agent.py) -- call setup_logging() so everything lands in ./logs with the same
format, rotation and redaction rules.

Environment knobs
-----------------
LOG_LEVEL          DEBUG (default) | INFO | WARNING | ERROR
LOG_DIR            override the log directory (default: <project>/logs)
LOG_CONSOLE        1 (default) also print to stderr, 0 to disable
LOG_JSON           1 (default) also write logs/<component>.jsonl
LOG_MAX_BYTES      rotation size per file (default 5242880)
LOG_BACKUP_COUNT   rotated files kept (default 5)
LOG_VERBOSE_LIBS   1 to unmute the very chatty libs (asyncio, httpcore, urllib3)
LOG_VIEW_TOKEN     if set, enables the /api/logs read-back endpoint
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = Path(os.getenv("LOG_DIR") or (BASE_DIR / "logs"))

TEXT_FORMAT = (
    "%(asctime)s %(levelname)-8s [%(name)s] %(processName)s/%(threadName)s "
    "%(filename)s:%(lineno)d - %(message)s"
)

# Keys whose values must never reach a log file.
REDACT_HINTS = (
    "key", "secret", "token", "password", "passwd", "authorization",
    "auth", "credential", "sdp", "jwt", "client_secret", "api_key",
)

_configured: set[str] = set()
_lock = threading.Lock()


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


def mask(value: object) -> str:
    """Show only enough of a secret to tell two secrets apart."""
    text = str(value or "")
    if not text:
        return "<empty>"
    if len(text) <= 8:
        return "*" * len(text)
    return f"{text[:4]}...{text[-4:]} (len={len(text)})"


def redact(obj, _depth: int = 0):
    """Recursively replace secret-looking values so payloads are safe to log."""
    if _depth > 6:
        return "<...>"
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            lowered = str(key).lower()
            if any(hint in lowered for hint in REDACT_HINTS):
                out[key] = mask(value)
            else:
                out[key] = redact(value, _depth + 1)
        return out
    if isinstance(obj, (list, tuple)):
        return [redact(item, _depth + 1) for item in list(obj)[:50]]
    if isinstance(obj, str) and len(obj) > 2000:
        return obj[:2000] + f"...<truncated {len(obj) - 2000} chars>"
    return obj


def dumps(obj, safe: bool = False) -> str:
    """json.dumps that never raises and, unless safe=True, never leaks secrets.

    safe=True is for payloads that are already sanitised (environment_report),
    where a second pass would mask the "MISSING" markers we want to see.
    """
    try:
        return json.dumps(obj if safe else redact(obj), ensure_ascii=False, default=str)
    except Exception as exc:  # logging must never break its caller
        return f"<unserializable: {exc}>"


class JsonLineFormatter(logging.Formatter):
    """One JSON object per line -- easy to grep, tail and ship elsewhere."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "file": f"{record.filename}:{record.lineno}",
            "func": record.funcName,
            "process": record.processName,
            "pid": record.process,
            "thread": record.threadName,
        }
        for key in ("request_id", "room", "model", "action", "component", "session_id"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exception"] = "".join(traceback.format_exception(*record.exc_info))
        if record.stack_info:
            payload["stack"] = record.stack_info
        return json.dumps(payload, ensure_ascii=False, default=str)


def _rotating(path: Path, level: int, formatter: logging.Formatter) -> logging.Handler:
    handler = logging.handlers.RotatingFileHandler(
        path,
        maxBytes=_int("LOG_MAX_BYTES", 5 * 1024 * 1024),
        backupCount=_int("LOG_BACKUP_COUNT", 5),
        encoding="utf-8",
        delay=True,
    )
    handler.setLevel(level)
    handler.setFormatter(formatter)
    return handler


def setup_logging(component: str = "app") -> logging.Logger:
    """Configure root logging for this process. Safe to call more than once."""
    global LOG_DIR
    with _lock:
        if _configured:
            # First caller in the process wins: agent.py imports app.py, and a
            # second setup would move every handler over to the other log file.
            logging.getLogger(component).debug(
                "logging already configured by %s; reusing its handlers", ", ".join(sorted(_configured))
            )
            return logging.getLogger(component)

        # Re-resolve here: callers load their .env after importing this module.
        LOG_DIR = Path(os.getenv("LOG_DIR") or (BASE_DIR / "logs"))
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        level = getattr(logging, os.getenv("LOG_LEVEL", "DEBUG").upper(), logging.DEBUG)
        text = logging.Formatter(TEXT_FORMAT)

        root = logging.getLogger()
        root.setLevel(level)
        for handler in list(root.handlers):
            root.removeHandler(handler)

        # Everything, at the configured level.
        root.addHandler(_rotating(LOG_DIR / f"{component}.log", level, text))
        # Warnings and worse, isolated, so real problems are never buried.
        root.addHandler(_rotating(LOG_DIR / "errors.log", logging.WARNING, text))

        if _flag("LOG_JSON", True):
            root.addHandler(_rotating(LOG_DIR / f"{component}.jsonl", level, JsonLineFormatter()))

        if _flag("LOG_CONSOLE", True):
            console = logging.StreamHandler(sys.stderr)
            console.setLevel(level)
            console.setFormatter(text)
            root.addHandler(console)

        logging.captureWarnings(True)
        _tune_libraries(level)
        install_excepthooks()

        _configured.add(component)
        log = logging.getLogger(component)
        log.info(
            "logging initialised: component=%s level=%s dir=%s files=%s",
            component,
            logging.getLevelName(level),
            LOG_DIR,
            f"{component}.log, {component}.jsonl, errors.log",
        )
        return log


def attach_file(logger_name: str, filename: str, level: int = logging.DEBUG) -> logging.Logger:
    """Give one logger its own file on top of the shared handlers."""
    logger = logging.getLogger(logger_name)
    target = str((LOG_DIR / filename).resolve())
    for handler in logger.handlers:
        if getattr(handler, "baseFilename", None) == target:
            return logger
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger.addHandler(_rotating(LOG_DIR / filename, level, logging.Formatter(TEXT_FORMAT)))
    logger.setLevel(level)
    return logger


def _tune_libraries(level: int) -> None:
    """Turn third-party loggers up so provider/transport failures are visible."""
    verbose = [
        "livekit", "livekit.agents", "livekit.plugins", "livekit.api", "livekit.rtc",
        "openai", "elevenlabs", "httpx", "werkzeug", "flask", "flask.app",
        "py.warnings", "deepseek",
    ]
    for name in verbose:
        logging.getLogger(name).setLevel(level)
        logging.getLogger(name).propagate = True

    # These emit a line per socket read at DEBUG; opt in with LOG_VERBOSE_LIBS=1.
    chatty = ["asyncio", "httpcore", "urllib3", "urllib3.connectionpool", "websockets", "aiohttp"]
    floor = level if _flag("LOG_VERBOSE_LIBS", False) else max(level, logging.INFO)
    for name in chatty:
        logging.getLogger(name).setLevel(floor)


def install_excepthooks() -> None:
    """Route crashes that bypass try/except into the log files too."""
    log = logging.getLogger("uncaught")

    def _sys_hook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        log.critical("uncaught exception", exc_info=(exc_type, exc_value, exc_tb))

    sys.excepthook = _sys_hook

    def _thread_hook(args):
        if issubclass(args.exc_type, SystemExit):
            return
        log.critical(
            "uncaught exception in thread %s",
            getattr(args.thread, "name", "?"),
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    threading.excepthook = _thread_hook

    def _unraisable(args):
        log.error(
            "unraisable exception in %r",
            args.object,
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.unraisablehook = _unraisable


def install_asyncio_logging(loop=None) -> None:
    """Log asyncio task failures that would otherwise be swallowed."""
    import asyncio

    log = logging.getLogger("asyncio.uncaught")
    try:
        loop = loop or asyncio.get_running_loop()
    except RuntimeError:
        return

    loop.set_debug(_flag("LOG_ASYNCIO_DEBUG", False))

    def handler(_loop, context):
        exc = context.get("exception")
        details = {key: str(value) for key, value in context.items() if key != "exception"}
        log.error("asyncio error: %s | %s", context.get("message"), dumps(details), exc_info=exc)

    loop.set_exception_handler(handler)


def environment_report() -> dict:
    """Which credentials/settings this process actually sees (values masked)."""
    watched = [
        "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "ELEVENLABS_API_KEY",
        "ELEVENLABS_VOICE_ID", "LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET",
        "OPENAI_REALTIME_MODEL", "OPENAI_REALTIME_VOICE", "RESTAURANT_NAME",
        "LOG_LEVEL", "LOG_DIR",
    ]
    report = {}
    for name in watched:
        value = os.getenv(name)
        if value is None:
            report[name] = "MISSING"
        elif any(hint in name.lower() for hint in REDACT_HINTS):
            report[name] = mask(value)
        else:
            report[name] = value
    report["python"] = sys.version.split()[0]
    report["cwd"] = os.getcwd()
    report["log_dir"] = str(LOG_DIR)
    return report


def tail(path: Path, lines: int = 200) -> list[str]:
    """Cheap `tail -n` for the log read-back endpoint."""
    if not path.exists():
        return []
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        end = handle.tell()
        data = b""
        while end > 0 and data.count(b"\n") <= lines:
            step = min(8192, end)
            end -= step
            handle.seek(end)
            data = handle.read(step) + data
    return data.decode("utf-8", errors="replace").splitlines()[-lines:]
