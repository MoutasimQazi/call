# Logging

Every layer of the stack now writes to `logs/`: the Flask app, the LiveKit
worker, and the browser page. All of it is configured in `logging_setup.py`.

## Files

| File | Written by | Contents |
| --- | --- | --- |
| `logs/web.log` | `app.py` (Passenger process) | Every request, response, status code, timing, and traceback |
| `logs/web.jsonl` | `app.py` | The same records as one JSON object per line |
| `logs/agent.log` | `agent.py` (LiveKit worker) | Job lifecycle, model resolution, room/session events, tool calls |
| `logs/agent.jsonl` | `agent.py` | Same, as JSON lines |
| `logs/browser.log` | browser, via `POST /` `action=client_log` | WebRTC/LiveKit state, JS errors, unhandled promise rejections |
| `logs/errors.log` | both processes | WARNING and above only — check this first |
| `logs/usage.log` | `app.py` | Per-response token usage and cost |
| `logs/realtime_cost.log` | `app.py` | The original cost ledger, unchanged |

Each file rotates at 5 MB and keeps 5 backups, so the directory cannot grow
without bound.

Note: the first module to call `setup_logging()` in a process wins. `agent.py`
imports `app.py`, so in the worker process everything lands in `agent.log`.

## Environment variables

| Variable | Default | Effect |
| --- | --- | --- |
| `LOG_LEVEL` | `DEBUG` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `LOG_DIR` | `./logs` | Where the files go |
| `LOG_CONSOLE` | `1` | Also write to stderr (Passenger captures this) |
| `LOG_JSON` | `1` | Write the `.jsonl` files |
| `LOG_MAX_BYTES` | `5242880` | Rotation size |
| `LOG_BACKUP_COUNT` | `5` | Rotated files kept |
| `LOG_VERBOSE_LIBS` | `0` | Unmute `asyncio`, `httpcore`, `urllib3` — very noisy |
| `LOG_ASYNCIO_DEBUG` | `0` | asyncio debug mode in the worker |
| `LOG_VIEW_TOKEN` | unset | Set to enable `/api/logs`; unset = endpoint returns 404 |

Set `LOG_LEVEL=INFO` once things are stable — `DEBUG` logs every request payload.

## Reading the logs

Over SSH:

```bash
tail -f logs/errors.log          # problems only
tail -f logs/web.log             # request flow
tail -f logs/agent.log           # voice worker
tail -f logs/browser.log         # what the caller's browser saw
```

Without SSH, set `LOG_VIEW_TOKEN` to a long random string and use:

```
GET /api/health
GET /api/logs?token=<LOG_VIEW_TOKEN>&file=web.log&lines=500
```

`/api/logs` lists the available files in its `available` field. It is disabled
entirely while `LOG_VIEW_TOKEN` is unset.

## In the browser

The page exposes `window.CLOG`. Open DevTools during a call and everything is
mirrored to the console as it is sent to the server:

```js
CLOG.history()     // every entry this page has recorded
CLOG.session       // the id that prefixes this page's lines in browser.log
CLOG.flush()       // push queued entries to the server now
```

## Redaction

Values under keys containing `key`, `secret`, `token`, `password`,
`authorization`, `credential`, `jwt` or `sdp` are masked as `abcd...wxyz
(len=N)` before anything is written. Log payloads freely; credentials will not
land in the files.
