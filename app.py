from __future__ import annotations

import csv
import json
import logging
import os
import re
import secrets
import string
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock

from dotenv import load_dotenv
from flask import Flask, jsonify, request, Response, g
from openai import OpenAI
from livekit.api import AccessToken, VideoGrants
from werkzeug.exceptions import HTTPException

import logging_setup


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
load_dotenv(override=True)

log = logging_setup.setup_logging("web")
browser_log = logging_setup.attach_file("browser", "browser.log")
usage_log = logging_setup.attach_file("usage", "usage.log")


def _load_htaccess_env() -> list[str]:
		"""Fill missing env vars from the SetEnv lines in .htaccess.

		cPanel/CloudLinux stores credentials there, but SetEnv only reaches the
		web server: a worker started from SSH or cron inherits none of them and
		cannot register with LiveKit. Real environment variables and .env always
		win - this only fills in what is still missing.
		"""
		filled = []
		path = Path(__file__).resolve().parent / ".htaccess"
		try:
				if not path.exists():
						return filled
				for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
						match = re.match(r"^\s*SetEnv\s+(\S+)\s+(.*?)\s*$", line)
						if not match:
								continue
						name = match.group(1)
						value = match.group(2).strip().strip("\"'")
						if value and not os.getenv(name):
								os.environ[name] = value
								filled.append(name)
		except Exception:
				log.exception("could not read env vars from %s", path)
		return filled


_htaccess_vars = _load_htaccess_env()
if _htaccess_vars:
		log.info("loaded %d env var(s) from .htaccess SetEnv: %s", len(_htaccess_vars), ", ".join(_htaccess_vars))

MODEL = os.getenv("OPENAI_REALTIME_MODEL", "gpt-realtime-2.1-mini")
VOICE = os.getenv("OPENAI_REALTIME_VOICE", "shimmer")
TTS_VOICE = os.getenv("OPENAI_TTS_VOICE", VOICE)
# Speech for the LiveKit pipeline models comes from LiveKit Inference, which
# bills against the LiveKit Cloud project - no separate speech vendor key.
LIVEKIT_STT_MODEL = os.getenv("LIVEKIT_STT_MODEL", "deepgram/nova-3")
LIVEKIT_TTS_MODEL = os.getenv("LIVEKIT_TTS_MODEL", "cartesia/sonic-3")
LIVEKIT_TTS_VOICE = os.getenv("LIVEKIT_TTS_VOICE", "")
RESTAURANT = os.getenv("RESTAURANT_NAME", "Frush")

MODEL_OPTIONS = {
		"realtime-mini-2.1": {
				"label": "Realtime Mini 2.1",
				"provider": "openai",
				"api_model": MODEL,
				"realtime": True,
		},
		"gpt-4o-mini": {
				"label": "GPT-4o Mini",
				"provider": "openai",
				"api_model": "gpt-4o-mini",
				"realtime": False,
		},
		"deepseek-v4-flash": {
				"label": "DeepSeek V4 Flash",
				"provider": "deepseek",
				"api_model": "deepseek-v4-flash",
				"realtime": False,
		},
}

VOICES = ["alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer", "verse", "marin", "cedar"]
SAMPLE_MENU_SIZE = "small"

app = Flask(__name__)

try:
		client = OpenAI()
		log.info("OpenAI client ready")
except Exception:
		client = None
		log.exception("OpenAI client could not be created - realtime sessions will fail")

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
MENU_FILE = DATA_DIR / "menu.csv"
ORDERS_FILE = DATA_DIR / "orders.csv"
ITEMS_FILE = DATA_DIR / "order_items.csv"
# One log directory for the whole stack (honours the LOG_DIR env var).
LOG_DIR = logging_setup.LOG_DIR
LOG_FILE = LOG_DIR / "realtime_cost.log"
LIVEKIT_URL = os.getenv("LIVEKIT_URL", "")

LOG_DIR.mkdir(exist_ok=True)

MENU_FIELDS = ["id", "name", "category", "price", "sized", "stock"]
ORDER_FIELDS = ["order_number", "customer_name", "fulfillment", "total", "eta_minutes", "status", "created_at", "updated_at"]
ITEM_FIELDS = ["order_number", "item_id", "name", "size", "quantity", "unit_price", "line_total"]

SIZE_UPCHARGE = {"small": 0, "medium": 0, "large": 3}
STATUSES = ["confirmed", "preparing", "ready", "completed", "cancelled"]
PICKUP_ETA = 15
DELIVERY_ETA = 35

DEFAULT_ROWS = [
		("margherita", "Margherita Pizza", "Pizzas", 10, True, 8),
		("pepperoni", "Pepperoni Pizza", "Pizzas", 12, True, 8),
		("veggie", "Veggie Supreme Pizza", "Pizzas", 13, True, 5),
		("bolognese", "Spaghetti Bolognese", "Pasta", 11, False, 6),
		("alfredo", "Fettuccine Alfredo", "Pasta", 12, False, 6),
		("garlic_bread", "Garlic Bread", "Sides", 4, False, 12),
		("caesar", "Caesar Salad", "Sides", 6, False, 7),
		("coke", "Coke", "Drinks", 2, False, 20),
		("sprite", "Sprite", "Drinks", 2, False, 20),
		("water", "Water", "Drinks", 2, False, 30),
		("tiramisu", "Tiramisu", "Desserts", 5, False, 6),
]

MENU: dict[str, dict] = {}
_menu_lock = Lock()
_order_lock = Lock()


# ---------------------------------------------------------------------------
# Menu store
# ---------------------------------------------------------------------------
def _save_menu() -> None:
		DATA_DIR.mkdir(exist_ok=True)
		with MENU_FILE.open("w", newline="", encoding="utf-8") as f:
				writer = csv.DictWriter(f, fieldnames=MENU_FIELDS)
				writer.writeheader()
				for item_id, item in MENU.items():
						writer.writerow(
								{
										"id": item_id,
										"name": item["name"],
										"category": item["category"],
										"price": item["price"],
										"sized": "yes" if item["sized"] else "no",
										"stock": item["stock"],
								}
						)


def _load_menu() -> None:
		MENU.clear()
		if MENU_FILE.exists():
				with MENU_FILE.open(newline="", encoding="utf-8") as f:
						for row in csv.DictReader(f):
								item_id = (row.get("id") or "").strip()
								if not item_id:
										continue
								try:
										MENU[item_id] = {
												"name": row["name"].strip(),
												"category": row["category"].strip(),
												"price": int(float(row["price"])),
												"sized": row["sized"].strip().lower() in ("yes", "true", "1"),
												"stock": max(0, int(float(row["stock"]))),
										}
								except (KeyError, ValueError, AttributeError):
										continue
				if MENU:
						return

		for item_id, name, category, price, sized, stock in DEFAULT_ROWS:
				MENU[item_id] = {"name": name, "category": category, "price": price, "sized": sized, "stock": stock}
		_save_menu()


def reload_menu() -> None:
		with _menu_lock:
				_load_menu()


def resolve(item_ref: str):
		if not item_ref:
				return None
		key = item_ref.strip().lower()
		if key in MENU:
				return key
		for item_id, item in MENU.items():
				if item["name"].lower() == key:
						return item_id
		for item_id, item in MENU.items():
				if key in item["name"].lower() or key in item_id:
						return item_id
		return None


def unit_price(item_id: str, size: str | None = None) -> int:
		item = MENU[item_id]
		price = item["price"]
		if item["sized"] and size:
				price += SIZE_UPCHARGE.get(size.lower(), 0)
		return price


def in_stock(item_id: str, quantity: int) -> bool:
		return MENU[item_id]["stock"] >= quantity


class StockError(Exception):
		pass


def price_order(raw_items: list[dict]) -> tuple[list[dict], int]:
		lines = []
		total = 0
		wanted: dict[str, int] = {}

		for raw in raw_items:
				item_id = resolve(raw.get("item", ""))
				if not item_id:
						raise ValueError(f"'{raw.get('item')}' is not on the menu")
				quantity = int(raw.get("quantity", 1) or 1)
				if quantity < 1:
						raise ValueError("Quantity must be at least 1")
				wanted[item_id] = wanted.get(item_id, 0) + quantity

		for item_id, quantity in wanted.items():
				if not in_stock(item_id, quantity):
						raise StockError(f"{MENU[item_id]['name']} is out of stock (only {MENU[item_id]['stock']} left)")

		for raw in raw_items:
				item_id = resolve(raw["item"])
				item = MENU[item_id]
				size = (raw.get("size") or "").lower() or None
				if not item["sized"]:
						size = None
				quantity = int(raw.get("quantity", 1) or 1)
				price = unit_price(item_id, size)
				line_total = price * quantity
				total += line_total
				lines.append(
						{
								"id": item_id,
								"name": item["name"],
								"size": size,
								"quantity": quantity,
								"unit_price": price,
								"line_total": line_total,
						}
				)
		return lines, total


def commit_stock(lines: list[dict]) -> None:
		with _menu_lock:
				needed: dict[str, int] = {}
				for line in lines:
						needed[line["id"]] = needed.get(line["id"], 0) + line["quantity"]
				for item_id, quantity in needed.items():
						if not in_stock(item_id, quantity):
								raise StockError(f"{MENU[item_id]['name']} just sold out")
				for item_id, quantity in needed.items():
						MENU[item_id]["stock"] -= quantity
				_save_menu()


def menu_public() -> dict:
		groups: dict[str, list] = {}
		for item_id, item in MENU.items():
				groups.setdefault(item["category"], []).append(
						{
								"id": item_id,
								"name": item["name"],
								"price": item["price"],
								"sized": item["sized"],
								"stock": item["stock"],
								"available": item["stock"] > 0,
						}
				)
		return {"categories": [{"name": category, "items": items} for category, items in groups.items()]}


def inventory_public() -> dict:
		return {item_id: {"name": item["name"], "stock": item["stock"]} for item_id, item in MENU.items()}


def menu_for_prompt() -> str:
		lines = []
		categories = list(dict.fromkeys(item["category"] for item in MENU.values()))
		for category in categories:
				parts = []
				for item_id, item in MENU.items():
						if item["category"] != category:
								continue
						sold_out = " (SOLD OUT)" if item["stock"] <= 0 else ""
						sized = " [S/M/L, large +$3]" if item["sized"] else ""
						parts.append(f"{item['name']} (id: {item_id}) ${item['price']}{sized}{sold_out}")
				lines.append(f"{category}: " + "; ".join(parts))
		return "\n".join(lines)


def restock(item_id: str, stock: int) -> bool:
		item_id = resolve(item_id)
		if not item_id or stock < 0:
				return False
		with _menu_lock:
				MENU[item_id]["stock"] = stock
				_save_menu()
		return True


# ---------------------------------------------------------------------------
# Order store
# ---------------------------------------------------------------------------
def _read_csv(path: Path) -> list[dict]:
		if not path.exists():
				return []
		with path.open(newline="", encoding="utf-8") as f:
				return list(csv.DictReader(f))


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
		DATA_DIR.mkdir(exist_ok=True)
		with path.open("w", newline="", encoding="utf-8") as f:
				writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
				writer.writeheader()
				writer.writerows(rows)


def _append_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
		DATA_DIR.mkdir(exist_ok=True)
		new_file = not path.exists()
		with path.open("a", newline="", encoding="utf-8") as f:
				writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
				if new_file:
						writer.writeheader()
				writer.writerows(rows)


def load_orders() -> list[dict]:
		items_by_order: dict[str, list[dict]] = {}
		for row in _read_csv(ITEMS_FILE):
				items_by_order.setdefault(row["order_number"], []).append(
						{
								"id": row["item_id"],
								"name": row["name"],
								"size": row["size"] or None,
								"quantity": int(row["quantity"]),
								"unit_price": int(float(row["unit_price"])),
								"line_total": int(float(row["line_total"])),
						}
				)

		orders_list = []
		for row in _read_csv(ORDERS_FILE):
				orders_list.append(
						{
								"order_number": row["order_number"],
								"customer_name": row["customer_name"],
								"fulfillment": row["fulfillment"],
								"total": int(float(row["total"])),
								"eta_minutes": int(float(row["eta_minutes"])),
								"status": row["status"],
								"created_at": row["created_at"],
								"updated_at": row.get("updated_at") or "",
								"items": items_by_order.get(row["order_number"], []),
						}
				)
		return orders_list


def _order_row(order: dict) -> dict:
		return {key: order.get(key, "") for key in ORDER_FIELDS}


def _item_rows(order: dict) -> list[dict]:
		return [
				{
						"order_number": order["order_number"],
						"item_id": line["id"],
						"name": line["name"],
						"size": line["size"] or "",
						"quantity": line["quantity"],
						"unit_price": line["unit_price"],
						"line_total": line["line_total"],
				}
				for line in order["items"]
		]


def place(customer_name: str, fulfillment: str, items: list[dict]) -> dict:
		name = (customer_name or "").strip() or "Guest"
		fulfillment = fulfillment if fulfillment in ("pickup", "delivery") else "pickup"

		try:
				lines, total = price_order(items or [])
				if not lines:
						raise ValueError("The order is empty")
				commit_stock(lines)
		except (StockError, ValueError) as exc:
				return {"success": False, "error": str(exc)}

		with _order_lock:
				count = len(_read_csv(ORDERS_FILE))
				order = {
						"order_number": f"FR-{1001 + count}",
						"customer_name": name,
						"fulfillment": fulfillment,
						"items": lines,
						"total": total,
						"eta_minutes": DELIVERY_ETA if fulfillment == "delivery" else PICKUP_ETA,
						"status": "confirmed",
						"created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
						"updated_at": "",
				}
				_append_csv(ORDERS_FILE, ORDER_FIELDS, [_order_row(order)])
				_append_csv(ITEMS_FILE, ITEM_FIELDS, [_item_rows(order)[0]])
				if len(order["items"]) > 1:
						_append_csv(ITEMS_FILE, ITEM_FIELDS, _item_rows(order)[1:])
		return {"success": True, **order}


def set_status(order_number: str, status: str) -> dict:
		if status not in STATUSES:
				return {"success": False, "error": f"unknown status '{status}'"}
		with _order_lock:
				rows = _read_csv(ORDERS_FILE)
				for row in rows:
						if row["order_number"] == order_number:
								row["status"] = status
								row["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
								_write_csv(ORDERS_FILE, ORDER_FIELDS, rows)
								return {"success": True, "order": row}
		return {"success": False, "error": f"order {order_number} not found"}


# ---------------------------------------------------------------------------
# Voice agent config
# ---------------------------------------------------------------------------
def build_instructions() -> str:
		reload_menu()
		return f"""
You are "Mario", a warm and efficient phone-order agent for a restaurant
called "{RESTAURANT}". You are on a live voice call: speak naturally and keep
every reply to one short sentence, two at most.

# Menu (this is the ONLY menu; use the item ids when calling tools)
{menu_for_prompt()}

# Sizes & prices
- Pizzas come in small / medium / large. Medium is the listed price; large is +$3.
- Say prices out loud in words, e.g. "twelve dollars".

# Confirm ONCE — do not repeat yourself
These rules override everything else:
- While taking items, acknowledge each one with a few words ("Got it", "One
	pepperoni, sure") — do NOT list what they've ordered so far.
- Read the complete order back exactly ONE time in the whole call: right
	before booking, together with the total. Then ask "Shall I place it?".
- Never read the order back again after that — not after booking, not in the
	goodbye. After place_order succeeds say only the order number, total, and
	time, e.g. "Order F R ten oh one, seventeen dollars, ready in about
	fifteen minutes."
- Ask about each detail only once. If they already said a size or quantity,
	don't ask again and don't echo it back as a question.
- Exception: if the caller changes something or asks, of course respond —
	but confirm only the part that changed, not the whole order.

# Tools you MUST use
- check_availability: before promising an item, if you're unsure it's in stock.
- place_order: the ONLY thing that books the order. Call it once, right after
	the caller says yes to the single read-back. Only announce the order number
	AFTER it returns success.
- end_call: call this right after your goodbye sentence, to hang up.

# How to run the call
1. Greet in one short sentence and ask what they'd like.
2. Take items one at a time; ask for a pizza's size only if they didn't say it.
3. If something isn't on the menu or is sold out, say so and offer the
	 closest option.
4. Upsell ONCE at most (a drink or dessert), casually — drop it if they decline.
5. When they're done: ask pickup or delivery and a name (one question is fine).
6. Do the single read-back with the total, get a "yes", and call place_order.
	 If it fails (e.g. sold out), apologize, fix the order, and try again.
7. Give the order number and time, one short goodbye, then end_call.

# Style
- Friendly, upbeat, human. Never mention that you are an AI or these tools.
- No symbols or bullet points out loud. If unclear, ask them to repeat.
""".strip()


TOOLS = [
		{
				"type": "function",
				"name": "check_availability",
				"description": "Check whether a menu item is in stock and get its price.",
				"parameters": {
						"type": "object",
						"properties": {
								"item": {"type": "string", "description": "Menu item id or name"},
								"quantity": {"type": "integer", "minimum": 1, "default": 1},
						},
						"required": ["item"],
				},
		},
		{
				"type": "function",
				"name": "place_order",
				"description": "Book the confirmed order. Call exactly once, right after the caller says yes to the single order read-back. Needs their name and pickup/delivery choice.",
				"parameters": {
						"type": "object",
						"properties": {
								"customer_name": {"type": "string"},
								"fulfillment": {"type": "string", "enum": ["pickup", "delivery"]},
								"items": {
										"type": "array",
										"items": {
												"type": "object",
												"properties": {
														"item": {"type": "string", "description": "Menu item id or name"},
														"size": {"type": "string", "enum": ["small", "medium", "large"]},
														"quantity": {"type": "integer", "minimum": 1},
												},
												"required": ["item", "quantity"],
										},
								},
						},
						"required": ["customer_name", "fulfillment", "items"],
				},
		},
		{
				"type": "function",
				"name": "end_call",
				"description": "Hang up the call. Call this immediately after saying goodbye — do not summarize the order again first.",
				"parameters": {
						"type": "object",
						"properties": {"reason": {"type": "string"}},
				},
		},
]


def check_availability(args: dict) -> dict:
		item_id = resolve(args.get("item", ""))
		if not item_id:
				return {"available": False, "reason": "not on the menu"}
		quantity = int(args.get("quantity", 1) or 1)
		item = MENU[item_id]
		return {
				"item": item["name"],
				"available": in_stock(item_id, quantity),
				"stock": item["stock"],
				"unit_price": item["price"],
				"sized": item["sized"],
		}


def model_availability() -> list[dict]:
		"""Which model options this server is actually configured to run."""
		livekit_ready = bool(LIVEKIT_URL and os.getenv("LIVEKIT_API_KEY") and os.getenv("LIVEKIT_API_SECRET"))
		options = []
		for key, config in MODEL_OPTIONS.items():
				reason = ""
				if config["provider"] == "deepseek" and not os.getenv("DEEPSEEK_API_KEY"):
						reason = "DEEPSEEK_API_KEY is not configured on the server"
				elif config["provider"] == "openai" and not os.getenv("OPENAI_API_KEY"):
						reason = "OPENAI_API_KEY is not configured on the server"
				if not reason and not config["realtime"] and not livekit_ready:
						reason = "LiveKit is not configured on the server"
				options.append({
						"key": key,
						"label": config["label"],
						"available": not reason,
						"reason": reason,
				})
		return options


def worker_status() -> dict:
		"""Whether the LiveKit worker has run, from the log it writes.

		The worker is a separate process, so the web app cannot see it directly;
		the age of logs/agent.log is the closest honest signal.
		"""
		path = LOG_DIR / "agent.log"
		if not path.exists():
				return {
						"log_present": False,
						"hint": "logs/agent.log does not exist - agent.py has never started on this server",
				}
		last = logging_setup.tail(path, 1)
		return {
				"log_present": True,
				"log_age_seconds": round(time.time() - path.stat().st_mtime),
				"last_line": last[0] if last else "",
		}


def _openai_version() -> str:
		try:
				from importlib.metadata import version

				return version("openai")
		except Exception:
				return "unknown"


def calculate_cost(usage: dict) -> float:
		INPUT_TEXT = 5.0 / 1_000_000
		OUTPUT_TEXT = 20.0 / 1_000_000
		INPUT_AUDIO = 40.0 / 1_000_000
		OUTPUT_AUDIO = 80.0 / 1_000_000

		return round(
				usage.get("input_text_tokens", 0) * INPUT_TEXT
				+ usage.get("output_text_tokens", 0) * OUTPUT_TEXT
				+ usage.get("input_audio_tokens", 0) * INPUT_AUDIO
				+ usage.get("output_audio_tokens", 0) * OUTPUT_AUDIO,
				6,
		)


# ---------------------------------------------------------------------------
# HTML pages
# ---------------------------------------------------------------------------
INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
	<meta charset="UTF-8" />
	<meta name="viewport" content="width=device-width, initial-scale=1.0" />
	<title>Frush · Order by Voice</title>
	<style>
		:root {
			--bg: #14100c;
			--panel: #201913;
			--panel-2: #2a2118;
			--line: #3a2f22;
			--text: #f4ece0;
			--muted: #b6a892;
			--red: #d43f3a;
			--green: #3fae5a;
			--gold: #e0b04a;
			--user: #3fae5a;
		}
		* { box-sizing: border-box; }
		html, body {
			margin: 0;
			min-height: 100%;
			font-family: "Segoe UI", system-ui, -apple-system, sans-serif;
			color: var(--text);
			background:
				radial-gradient(1000px 500px at 15% -10%, #35251a 0, transparent 55%),
				radial-gradient(900px 500px at 110% 0%, #2a1a14 0, transparent 55%),
				var(--bg);
		}
		.wrap { max-width: 960px; margin: 0 auto; padding: 24px 20px 40px; }
		.hero { display: flex; align-items: center; gap: 16px; padding: 8px 4px 22px; }
		.hero-badge {
			width: 58px; height: 58px; display: grid; place-items: center; font-size: 1.8rem;
			border-radius: 16px; background: linear-gradient(145deg, var(--red), #8f231f);
			box-shadow: 0 8px 24px rgba(212, 63, 58, .35);
		}
		.hero h1 { margin: 0; font-size: 1.7rem; font-family: Georgia, "Times New Roman", serif; letter-spacing: .3px; }
		.tagline { margin: 2px 0 0; color: var(--muted); font-size: .9rem; }
		.model-pill {
			margin-left: auto; align-self: flex-start; font-size: .75rem; color: var(--muted);
			background: var(--panel); border: 1px solid var(--line); padding: 5px 11px; border-radius: 999px;
		}
		.grid { display: grid; grid-template-columns: 1fr 1.15fr; gap: 18px; }
		@media (max-width: 760px) { .grid { grid-template-columns: 1fr; } }
		.card {
			background: linear-gradient(180deg, var(--panel), #1b150f); border: 1px solid var(--line);
			border-radius: 18px; padding: 20px;
		}
		.card h2 { margin: 0 0 14px; font-family: Georgia, serif; font-size: 1.25rem; }
		.menu-group { margin-bottom: 16px; }
		.menu-group h3 {
			margin: 0 0 8px; font-size: .98rem; display: flex; align-items: baseline; gap: 8px;
		}
		.menu-group .note { font-size: .72rem; color: var(--muted); font-weight: 400; }
		.menu-group ul { list-style: none; margin: 0; padding: 0; }
		.menu-group li {
			display: flex; justify-content: space-between; padding: 6px 0; border-bottom: 1px dashed var(--line); font-size: .92rem;
		}
		.menu-group li:last-child { border-bottom: none; }
		.price { color: var(--gold); font-weight: 600; }
		.call { display: flex; flex-direction: column; }
		.call-top { display: flex; align-items: center; justify-content: space-between; min-height: 24px; }
		.status { color: var(--muted); font-size: .9rem; }
		.timer {
			font-variant-numeric: tabular-nums; font-weight: 600; color: var(--green);
			background: rgba(63,174,90,.12); border: 1px solid rgba(63,174,90,.3); padding: 3px 10px;
			border-radius: 999px; font-size: .85rem;
		}
		.voice-row {
			display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-top: 14px;
			font-size: .85rem; color: var(--muted);
		}
		.voice-row select {
			background: var(--panel-2); color: var(--text); border: 1px solid var(--line); border-radius: 10px;
			padding: 7px 10px; font-size: .9rem; cursor: pointer; min-width: 140px;
		}
		.voice-row select:disabled { opacity: .5; cursor: not-allowed; }
		.model-row { margin-top: 10px; }
		.model-row select { min-width: 190px; }
		.model-note { display: block; margin-top: 5px; color: var(--muted); font-size: .72rem; }
		.phone-stage { display: flex; flex-direction: column; align-items: center; gap: 12px; padding: 26px 0 22px; }
		.callbtn {
			position: relative; width: 96px; height: 96px; border: none; border-radius: 50%; cursor: pointer;
			background: linear-gradient(145deg, var(--green), #2b8544); box-shadow: 0 10px 30px rgba(63,174,90,.45);
			display: grid; place-items: center; transition: transform .15s ease, box-shadow .2s ease, background .2s ease;
		}
		.callbtn:hover { transform: translateY(-2px); }
		.callbtn-icon { font-size: 2rem; }
		.callbtn-label { font-size: .9rem; color: var(--muted); }
		.mutebtn {
			background: var(--panel-2); color: var(--text); border: 1px solid var(--line); border-radius: 999px;
			padding: 6px 16px; font-size: .82rem; cursor: pointer;
		}
		.mutebtn:hover { border-color: var(--gold); }
		.mutebtn.muted { background: rgba(212,63,58,.15); border-color: rgba(212,63,58,.5); color: #ff9a94; }
		.foot a { color: var(--muted); }
		.callbtn.connecting { animation: ring 0.6s ease-in-out infinite; }
		@keyframes ring { 0%,100% { transform: rotate(0); } 25% { transform: rotate(-12deg); } 75% { transform: rotate(12deg); } }
		.callbtn.live { background: linear-gradient(145deg, var(--red), #9a2420); box-shadow: 0 10px 30px rgba(212,63,58,.5); }
		.callbtn.live::before,
		.callbtn.live::after {
			content: ""; position: absolute; inset: 0; border-radius: 50%; border: 2px solid rgba(212,63,58,.5); animation: pulse 1.8s ease-out infinite;
		}
		.callbtn.live::after { animation-delay: .9s; }
		@keyframes pulse { 0% { transform: scale(1); opacity: .7; } 100% { transform: scale(1.6); opacity: 0; } }
		.transcript {
			flex: 1; min-height: 220px; max-height: 340px; overflow-y: auto; background: var(--panel-2);
			border: 1px solid var(--line); border-radius: 14px; padding: 16px; display: flex; flex-direction: column; gap: 10px;
		}
		.transcript .hint { color: var(--muted); text-align: center; margin: auto; }
		.msg {
			max-width: 85%; padding: 9px 13px; border-radius: 13px; line-height: 1.45; font-size: .94rem;
			white-space: pre-wrap; word-wrap: break-word;
		}
		.msg.user { align-self: flex-end; background: linear-gradient(135deg, var(--green), #2b8544); color: #fff; border-bottom-right-radius: 4px; }
		.msg.assistant { align-self: flex-start; background: #241c14; border: 1px solid var(--line); border-bottom-left-radius: 4px; }
		.msg .who { display: block; font-size: .68rem; opacity: .75; margin-bottom: 3px; letter-spacing: .4px; text-transform: uppercase; }
		.msg.error { align-self: center; background: #3a1614; border: 1px solid #6b241f; color: #ffb4ad; font-size: .86rem; }
		.msg.system { align-self: center; background: rgba(224,176,74,.12); border: 1px solid rgba(224,176,74,.35); color: var(--gold); font-size: .84rem; text-align: center; }
		.stock { font-size: .68rem; padding: 1px 7px; border-radius: 999px; vertical-align: middle; margin-left: 4px; }
		.stock.low { background: rgba(224,176,74,.18); color: var(--gold); }
		.stock.sold { background: rgba(212,63,58,.18); color: #ff9a94; }
		.menu-group li.out span:first-child { opacity: .55; }
		.order-card {
			margin-top: 14px; border: 1px solid rgba(63,174,90,.4); background: rgba(63,174,90,.08);
			border-radius: 14px; padding: 14px 16px;
		}
		.order-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; }
		.order-title { font-weight: 700; font-size: 1rem; }
		.order-badge {
			font-size: .7rem; text-transform: uppercase; letter-spacing: .5px; background: var(--green); color: #06210f;
			padding: 3px 9px; border-radius: 999px; font-weight: 700;
		}
		.order-items { list-style: none; margin: 0 0 10px; padding: 0; }
		.order-items li { display: flex; justify-content: space-between; padding: 4px 0; font-size: .9rem; border-bottom: 1px dashed var(--line); }
		.order-items li:last-child { border-bottom: none; }
		.order-foot { display: flex; align-items: center; justify-content: space-between; padding-top: 8px; border-top: 1px solid var(--line); }
		.order-meta { color: var(--muted); font-size: .82rem; }
		.order-total { font-size: .95rem; }
		.order-total strong { color: var(--green); }
		.foot { margin-top: 22px; text-align: center; color: var(--muted); font-size: .76rem; }
	</style>
</head>
<body>
	<div class="wrap">
		<header class="hero">
			<div class="hero-badge">🍕</div>
			<div>
				<h1>Frush</h1>
				<p class="tagline">Authentic Italian · Order by voice with <strong>Mario</strong></p>
			</div>
			<span id="model" class="model-pill" title="Model in use">—</span>
		</header>

		<div class="grid">
			<section class="card menu">
				<h2>Menu <span id="menuNote" class="note">live stock</span></h2>
				<div id="menuList"><p class="hint">Loading menu…</p></div>
			</section>

			<section class="card call">
				<div class="call-top">
					<span id="status" class="status">Ready to take your order</span>
					<span id="timer" class="timer" hidden>00:00</span>
				</div>

				<label class="voice-row">
					<span>Mario's voice</span>
					<select id="voice">
						<option value="alloy">Alloy</option>
						<option value="ash">Ash</option>
						<option value="ballad">Ballad</option>
						<option value="coral">Coral</option>
						<option value="echo">Echo</option>
						<option value="sage" selected>Sage</option>
						<option value="shimmer">Shimmer</option>
						<option value="verse">Verse</option>
						<option value="marin">Marin</option>
						<option value="cedar">Cedar</option>
					</select>
				</label>
				<label class="voice-row model-row">
					<span>Language model</span>
					<select id="modelSelect" aria-describedby="modelNote">
						<option value="realtime-mini-2.1" selected>Realtime Mini 2.1</option>
						<option value="gpt-4o-mini">GPT-4o Mini</option>
						<option value="deepseek-v4-flash">DeepSeek V4 Flash</option>
					</select>
				</label>
				<span id="modelNote" class="model-note">Voice calls use OpenAI Realtime WebRTC.</span>

				<div class="phone-stage">
					<button id="orb" class="callbtn" aria-label="Start or end call">
						<span class="callbtn-icon" id="orbIcon">📞</span>
					</button>
					<span id="orbLabel" class="callbtn-label">Call to order</span>
					<button id="muteBtn" class="mutebtn" hidden>🎙️ Mute</button>
				</div>

				<div class="transcript" id="transcript">
					<p class="hint">Tap <strong>Call to order</strong> and Mario will pick up.</p>
				</div>

				<div class="order-card" id="orderCard" hidden>
					<div class="order-head">
						<span class="order-title">Order <span id="orderNo"></span></span>
						<span class="order-badge" id="orderBadge">confirmed</span>
					</div>
					<ul class="order-items" id="orderItems"></ul>
					<div class="order-foot">
						<span id="orderMeta" class="order-meta"></span>
						<span class="order-total">Total: <strong id="orderTotal"></strong></span>
					</div>
				</div>
			</section>
		</div>

		<footer class="foot">Powered by OpenAI Realtime · Your mic audio streams securely; your API key never leaves the server. · <a href="/?view=admin">Kitchen view</a></footer>
	</div>

	<audio id="remoteAudio" autoplay></audio>
	<script src="https://unpkg.com/livekit-client@2.15.6/dist/livekit-client.umd.min.js"></script>
	<script>
		// ---- diagnostics ------------------------------------------------
		// Mirrors every client event to the console and ships it to logs/browser.log.
		const CLOG = (() => {
			const history = [];
			const queue = [];
			const session = Math.random().toString(36).slice(2, 10);
			let timer = null;
			function flush() {
				timer = null;
				if (!queue.length) return;
				const entries = queue.splice(0, queue.length);
				try {
					fetch("/", {
						method: "POST",
						headers: { "Content-Type": "application/json" },
						body: JSON.stringify({ action: "client_log", entries }),
						keepalive: true,
					}).catch(() => {});
				} catch {}
			}
			function emit(level, scope, message, data) {
				const entry = {
					level, scope, session,
					message: String(message),
					data: data === undefined ? null : data,
					ts: new Date().toISOString(),
				};
				history.push(entry);
				if (history.length > 500) history.shift();
				const fn = console[level === "warning" ? "warn" : level] || console.log;
				fn.call(console, `[${scope}] ${message}`, data === undefined ? "" : data);
				queue.push(entry);
				if (!timer) timer = setTimeout(flush, 800);
			}
			window.addEventListener("error", (e) => emit("error", "window", e.message, {
				file: e.filename, line: e.lineno, col: e.colno, stack: e.error && e.error.stack,
			}));
			window.addEventListener("unhandledrejection", (e) => emit("error", "promise",
				String((e.reason && e.reason.message) || e.reason), { stack: e.reason && e.reason.stack }));
			window.addEventListener("beforeunload", flush);
			return {
				session, flush,
				debug: (s, m, d) => emit("debug", s, m, d),
				info: (s, m, d) => emit("info", s, m, d),
				warn: (s, m, d) => emit("warning", s, m, d),
				error: (s, m, d) => emit("error", s, m, d),
				history: () => history.slice(),
			};
		})();
		window.CLOG = CLOG;
		CLOG.info("boot", "page loaded", { session: CLOG.session, url: location.href, ua: navigator.userAgent });

		const orb = document.getElementById("orb");
		const orbIcon = document.getElementById("orbIcon");
		const orbLabel = document.getElementById("orbLabel");
		const statusEl = document.getElementById("status");
		const timerEl = document.getElementById("timer");
		const voiceSel = document.getElementById("voice");
		const modelSel = document.getElementById("modelSelect");
		const modelNote = document.getElementById("modelNote");
		const transcriptEl = document.getElementById("transcript");
		const modelPill = document.getElementById("model");
		const remoteAudio = document.getElementById("remoteAudio");
		const menuList = document.getElementById("menuList");
		const orderCard = document.getElementById("orderCard");
		const muteBtn = document.getElementById("muteBtn");

		let pc = null;
		let dc = null;
		let micStream = null;
		let active = false;
		let timerId = null;
		let callStart = 0;
		let hangupTimer = null;
		let pendingResponse = false;
		let responseActive = false;
		let muted = false;
		let livekitRoom = null;
		let livekitClient = null;
		let livekitAgentAudio = false;

		function setStatus(text) { statusEl.textContent = text; }

		function updateModelNote() {
			const realtime = modelSel.value === "realtime-mini-2.1";
			voiceSel.disabled = realtime ? active : true;
			modelNote.textContent = realtime
				? "Voice calls use OpenAI Realtime WebRTC."
				: "Speech-to-speech pipeline with LiveKit-hosted voice.";
		}

		modelSel.addEventListener("change", updateModelNote);
		updateModelNote();

		// Grey out models this server has no credentials for, so a call cannot be
		// started against one and fail at the token step.
		async function loadModelAvailability() {
			try {
				const data = await api("models");
				let switched = false;
				(data.models || []).forEach((m) => {
					const opt = [...modelSel.options].find((o) => o.value === m.key);
					if (!opt) return;
					opt.disabled = !m.available;
					opt.textContent = m.available ? m.label : `${m.label} (unavailable)`;
					opt.title = m.reason || "";
					if (!m.available) {
						CLOG.warn("models", `${m.key} unavailable`, { reason: m.reason });
						if (modelSel.value === m.key) switched = true;
					}
				});
				if (switched) {
					const first = [...modelSel.options].find((o) => !o.disabled);
					if (first) {
						modelSel.value = first.value;
						updateModelNote();
						CLOG.info("models", `switched selection to ${first.value}`);
					}
				}
			} catch (err) {
				CLOG.warn("models", `could not load availability: ${err.message}`);
			}
		}

		async function loadLiveKitClient() {
			if (livekitClient) return livekitClient;
			livekitClient = window.LivekitClient || window.LiveKitClient;
			if (!livekitClient) {
				livekitClient = await import("https://cdn.jsdelivr.net/npm/livekit-client@2.15.6/+esm");
			}
			return livekitClient;
		}

		async function api(action, payload) {
			const started = performance.now();
			CLOG.debug("api", `POST ${action}`, payload || null);
			try {
				const r = await fetch("/", {
					method: "POST",
					headers: { "Content-Type": "application/json" },
					body: JSON.stringify({ action, ...(payload || {}) }),
				});
				const data = await r.json();
				const ms = Math.round(performance.now() - started);
				if (!r.ok) CLOG.error("api", `${action} -> HTTP ${r.status} (${ms}ms)`, data);
				else CLOG.debug("api", `${action} -> ok (${ms}ms)`, data);
				return data;
			} catch (err) {
				CLOG.error("api", `${action} request failed: ${err.message}`, { stack: err.stack });
				throw err;
			}
		}

		const CAT_ICON = { Pizzas: "🍕", Pasta: "🍝", Sides: "🥗", Drinks: "🥤", Desserts: "🍰" };

		async function loadMenu() {
			try {
				const data = await api("menu");
				menuList.innerHTML = "";
				for (const cat of data.categories) {
					const group = document.createElement("div");
					group.className = "menu-group";
					const sized = cat.items.some((i) => i.sized);
					group.innerHTML =
						`<h3>${CAT_ICON[cat.name] || "•"} ${cat.name}` +
						(sized ? ` <span class="note">S / M / L · large +$3</span>` : ``) +
						`</h3>`;
					const ul = document.createElement("ul");
					for (const it of cat.items) {
						const li = document.createElement("li");
						const out = !it.available;
						li.className = out ? "out" : "";
						const stockTag = out
							? `<span class="stock sold">Sold out</span>`
							: it.stock <= 3
							? `<span class="stock low">${it.stock} left</span>`
							: ``;
						li.innerHTML = `<span>${it.name} ${stockTag}</span>` + `<span class="price">$${it.price}</span>`;
						ul.appendChild(li);
					}
					group.appendChild(ul);
					menuList.appendChild(group);
				}
			} catch {
				menuList.innerHTML = `<p class="hint">Could not load the menu.</p>`;
			}
		}

		function renderOrder(o) {
			document.getElementById("orderNo").textContent = o.order_number;
			const badge = document.getElementById("orderBadge");
			badge.textContent = o.status;
			document.getElementById("orderTotal").textContent = `$${o.total}`;
			document.getElementById("orderMeta").textContent = `${o.customer_name} · ${o.fulfillment} · ~${o.eta_minutes} min`;
			const ul = document.getElementById("orderItems");
			ul.innerHTML = "";
			for (const ln of o.items) {
				const li = document.createElement("li");
				const size = ln.size ? ` (${ln.size})` : "";
				li.innerHTML = `<span>${ln.quantity}× ${ln.name}${size}</span>` + `<span>$${ln.line_total}</span>`;
				ul.appendChild(li);
			}
			orderCard.hidden = false;
			orderCard.scrollIntoView({ behavior: "smooth", block: "nearest" });
		}

		function setOrbState(state, label, icon) {
			orb.classList.remove("connecting", "live");
			if (state) orb.classList.add(state);
			if (label) orbLabel.textContent = label;
			if (icon) orbIcon.textContent = icon;
		}

		function startTimer() {
			callStart = Date.now();
			timerEl.hidden = false;
			timerEl.textContent = "00:00";
			timerId = setInterval(() => {
				const s = Math.floor((Date.now() - callStart) / 1000);
				const mm = String(Math.floor(s / 60)).padStart(2, "0");
				const ss = String(s % 60).padStart(2, "0");
				timerEl.textContent = `${mm}:${ss}`;
			}, 1000);
		}
		function stopTimer() {
			if (timerId) clearInterval(timerId);
			timerId = null;
			timerEl.hidden = true;
		}

		function clearHint() {
			const hint = transcriptEl.querySelector(".hint");
			if (hint) hint.remove();
		}

		function addMessage(who, text, cls) {
			clearHint();
			const el = document.createElement("div");
			el.className = `msg ${cls}`;
			el.innerHTML = `<span class="who">${who}</span>`;
			el.appendChild(document.createTextNode(text));
			transcriptEl.appendChild(el);
			transcriptEl.scrollTop = transcriptEl.scrollHeight;
			return el;
		}

		let liveAssistant = null;
		let assistantBuffer = "";
		let assistantFlushScheduled = false;

		function flushAssistantDelta() {
			assistantFlushScheduled = false;
			if (!assistantBuffer) return;
			if (!liveAssistant) liveAssistant = addMessage("Mario", "", "assistant");
			liveAssistant.appendChild(document.createTextNode(assistantBuffer));
			assistantBuffer = "";
			transcriptEl.scrollTop = transcriptEl.scrollHeight;
		}

		function appendAssistantDelta(delta) {
			assistantBuffer += delta;
			if (assistantFlushScheduled) return;
			assistantFlushScheduled = true;
			requestAnimationFrame(flushAssistantDelta);
		}

		async function start() {
			try {
				CLOG.info("call", "start requested", { model: modelSel.value, voice: voiceSel.value });
				setOrbState("connecting", "Dialing…", "📞");
				setStatus("Connecting the call…");
				voiceSel.disabled = true;
				modelSel.disabled = true;
				if (modelSel.value !== "realtime-mini-2.1") {
					CLOG.info("livekit", `requesting token for model ${modelSel.value}`);
					const tokenResponse = await fetch(`/api/livekit/token?model=${encodeURIComponent(modelSel.value)}`);
					const tokenData = await tokenResponse.json();
					if (!tokenResponse.ok) {
						CLOG.error("livekit", `token request failed (HTTP ${tokenResponse.status})`, tokenData);
						throw new Error(tokenData.error || "Failed to get LiveKit token");
					}
					CLOG.info("livekit", "token received", { room: tokenData.roomName, identity: tokenData.identity, serverUrl: tokenData.serverUrl });
					livekitClient = await loadLiveKitClient();
					CLOG.debug("livekit", "client library loaded", { source: window.LivekitClient ? "umd" : "esm" });
					livekitRoom = new livekitClient.Room();
					["ParticipantConnected", "ParticipantDisconnected", "TrackPublished",
					 "ConnectionStateChanged", "Reconnecting", "Reconnected",
					 "MediaDevicesError", "SignalConnected", "TrackSubscriptionFailed",
					 "ConnectionQualityChanged"].forEach((name) => {
						const evt = livekitClient.RoomEvent[name];
						if (!evt) { CLOG.debug("livekit", `event ${name} not in this client build`); return; }
						livekitRoom.on(evt, (...args) => CLOG.info("livekit", `event ${name}`,
							args.map((a) => (a && a.identity) || (a && a.name) || (a && a.sid) || String(a)).slice(0, 4)));
					});
					livekitRoom.on(livekitClient.RoomEvent.TrackSubscribed, (track) => {
						CLOG.info("livekit", `track subscribed (${track.kind})`, { sid: track.sid });
						if (track.kind === livekitClient.Track.Kind.Audio) {
							track.attach(remoteAudio);
							livekitAgentAudio = true;
							setStatus("Mario is on the line — go ahead and order.");
						}
					});
					livekitRoom.on(livekitClient.RoomEvent.TrackUnsubscribed, (track) => {
						if (track.kind === livekitClient.Track.Kind.Audio) track.detach(remoteAudio);
					});
					livekitRoom.on(livekitClient.RoomEvent.Disconnected, (reason) => {
						// Expected when the caller hangs up; only a drop mid-call is a problem.
						(active ? CLOG.warn : CLOG.info)("livekit", "room disconnected", { reason: String(reason), active });
						if (active) {
							addMessage("System", "LiveKit disconnected.", "error");
							stop(true);
						}
					});
					CLOG.info("livekit", `connecting to ${tokenData.serverUrl}`);
					await livekitRoom.connect(tokenData.serverUrl, tokenData.token);
					CLOG.info("livekit", "room connected", { room: livekitRoom.name, participants: livekitRoom.numParticipants });
					await livekitRoom.localParticipant.setMicrophoneEnabled(true);
					CLOG.info("livekit", "microphone published");
					setTimeout(() => {
						if (active && !livekitAgentAudio) {
							CLOG.error("livekit", "no agent audio after 15s - is the agent.py worker running and registered to this LiveKit project?",
								{ room: livekitRoom && livekitRoom.name, participants: livekitRoom && livekitRoom.numParticipants });
							addMessage("System", "⚠️ No agent joined the room. Check the worker logs.", "error");
						}
					}, 15000);
					active = true;
					modelPill.textContent = `${modelSel.options[modelSel.selectedIndex].text} · LiveKit`;
					setOrbState("live", "End call", "📵");
					setStatus("Connected to LiveKit — waiting for Mario…");
					muteBtn.hidden = false;
					setMuted(false);
					startTimer();
					return;
				}
				const r = await fetch("/", {
					method: "POST",
					headers: { "Content-Type": "application/json" },
					body: JSON.stringify({ action: "session", voice: voiceSel.value, model: modelSel.value }),
				});
				const data = await r.json();
				if (!r.ok) {
					CLOG.error("realtime", `session request failed (HTTP ${r.status})`, data);
					throw new Error(data.error || "Failed to get session token");
				}
				CLOG.info("realtime", "ephemeral session created", { model: data.model, voice: data.voice });
				const EPHEMERAL_KEY = data.client_secret;
				modelPill.textContent = `${data.model_label || data.model} · ${data.voice}`;

				setStatus("Requesting microphone…");
				micStream = await navigator.mediaDevices.getUserMedia({ audio: true });

				CLOG.info("mic", "microphone granted", { tracks: micStream.getAudioTracks().map((t) => t.label) });

				pc = new RTCPeerConnection();
				pc.ontrack = (e) => {
					CLOG.info("webrtc", `remote track received (${e.track.kind})`);
					remoteAudio.srcObject = e.streams[0];
				};
				micStream.getTracks().forEach((t) => pc.addTrack(t, micStream));
				["iceconnectionstatechange", "icegatheringstatechange", "signalingstatechange"].forEach((name) => {
					pc.addEventListener(name, () => CLOG.debug("webrtc", name, {
						ice: pc.iceConnectionState, gathering: pc.iceGatheringState, signaling: pc.signalingState,
					}));
				});
				pc.addEventListener("icecandidateerror", (e) => CLOG.warn("webrtc", "ICE candidate error", {
					errorCode: e.errorCode, errorText: e.errorText, url: e.url,
				}));
				pc.addEventListener("connectionstatechange", () => {
					CLOG.info("webrtc", `connection state: ${pc && pc.connectionState}`);
					if (pc && ["failed", "disconnected", "closed"].includes(pc.connectionState) && active) {
						addMessage("System", "📴 Connection lost.", "system");
						stop(true);
						setStatus("Connection lost — tap to call again.");
					}
				});

				dc = pc.createDataChannel("oai-events");
				dc.addEventListener("message", (e) => {
					let evt;
					try {
						evt = JSON.parse(e.data);
					} catch (err) {
						CLOG.error("datachannel", `unparseable event: ${err.message}`, { raw: String(e.data).slice(0, 500) });
						return;
					}
					if (evt.type === "error") CLOG.error("realtime", "server error event", evt.error || evt);
					else CLOG.debug("realtime", `event ${evt.type}`,
						evt.type && evt.type.includes("delta") ? undefined : evt);
					try {
						handleEvent(evt);
					} catch (err) {
						CLOG.error("realtime", `handleEvent(${evt.type}) threw: ${err.message}`, { stack: err.stack });
					}
				});
				dc.addEventListener("open", () => {
					CLOG.info("datachannel", "open - requesting first response");
					dc.send(JSON.stringify({ type: "response.create" }));
				});
				dc.addEventListener("close", () => CLOG.warn("datachannel", "closed"));
				dc.addEventListener("error", (e) => CLOG.error("datachannel", "error", { detail: String(e && e.error) }));

				setStatus("Ringing…");
				const offer = await pc.createOffer();
				await pc.setLocalDescription(offer);

				const sdpResp = await fetch(
					`https://api.openai.com/v1/realtime/calls?model=${encodeURIComponent(data.model)}`,
					{
						method: "POST",
						body: offer.sdp,
						headers: {
							Authorization: `Bearer ${EPHEMERAL_KEY}`,
							"Content-Type": "application/sdp",
						},
					}
				);
				if (!sdpResp.ok) {
					const detail = await sdpResp.text();
					CLOG.error("realtime", `SDP exchange failed (HTTP ${sdpResp.status})`, { detail: detail.slice(0, 800) });
					throw new Error(`Call failed (${sdpResp.status}): ${detail}`);
				}
				CLOG.info("realtime", "SDP answer received");
				await pc.setRemoteDescription({ type: "answer", sdp: await sdpResp.text() });

				active = true;
				setOrbState("live", "End call", "📵");
				setStatus("Mario is on the line — go ahead and order.");
				muteBtn.hidden = false;
				setMuted(false);
				startTimer();
			} catch (err) {
				CLOG.error("call", `start() failed: ${err.message}`, { stack: err.stack, model: modelSel.value });
				addMessage("Error", err.message, "error");
				setStatus("Couldn't connect the call.");
				stop(true);
			}
		}

		function stop(failed = false) {
			CLOG.info("call", `stop(failed=${failed})`, {
				pc: pc && pc.connectionState, dc: dc && dc.readyState,
				livekit: livekitRoom ? livekitRoom.state : null, agentAudio: livekitAgentAudio,
			});
			CLOG.flush();
			active = false;
			pendingResponse = false;
			responseActive = false;
			stopTimer();
			setMuted(false);
			if (hangupTimer) { clearTimeout(hangupTimer); hangupTimer = null; }
			if (dc) { try { dc.close(); } catch {} dc = null; }
			if (pc) { try { pc.close(); } catch {} pc = null; }
			if (livekitRoom) { try { livekitRoom.disconnect(); } catch {} livekitRoom = null; }
			livekitAgentAudio = false;
			if (micStream) { micStream.getTracks().forEach((t) => t.stop()); micStream = null; }
			liveAssistant = null;
			assistantBuffer = "";
			voiceSel.disabled = false;
			modelSel.disabled = false;
			updateModelNote();
			muteBtn.hidden = true;
			setOrbState(null, "Call to order", "📞");
			if (!failed) setStatus("Call ended. Thanks for ordering!");
		}

		function setMuted(m) {
			muted = m;
			if (micStream) micStream.getAudioTracks().forEach((t) => (t.enabled = !m));
			if (livekitRoom) livekitRoom.localParticipant.setMicrophoneEnabled(!m);
			muteBtn.textContent = m ? "🔇 Unmute" : "🎙️ Mute";
			muteBtn.classList.toggle("muted", m);
		}

		async function runTool(name, args) {
			CLOG.info("tool", `call ${name}`, args);
			if (name === "check_availability") {
				return await api("check_availability", args);
			}
			if (name === "place_order") {
				const result = await api("place_order", args);
				if (result.success) {
					addMessage("System", `✅ Order ${result.order_number} placed · $${result.total}`, "system");
					renderOrder(result);
					loadMenu();
				} else {
					addMessage("System", `⚠️ ${result.error}`, "system");
				}
				return result;
			}
			if (name === "end_call") {
				if (!hangupTimer) hangupTimer = setTimeout(() => stop(), 4000);
				setStatus("Wrapping up the call…");
				return { ok: true };
			}
			return { error: `unknown tool ${name}` };
		}

		async function handleFunctionCall(item) {
			let args = {};
			try {
				args = JSON.parse(item.arguments || "{}");
			} catch (err) {
				CLOG.error("tool", `bad arguments for ${item.name}: ${err.message}`, { raw: item.arguments });
			}
			let result;
			try {
				result = await runTool(item.name, args);
				CLOG.info("tool", `result ${item.name}`, result);
			} catch (err) {
				CLOG.error("tool", `${item.name} threw: ${err.message}`, { stack: err.stack });
				result = { error: err.message };
			}
			if (!dc || dc.readyState !== "open") {
				CLOG.warn("tool", `cannot return ${item.name} result - data channel is ${dc ? dc.readyState : "gone"}`);
			}
			if (!dc || dc.readyState !== "open") return;
			dc.send(JSON.stringify({
				type: "conversation.item.create",
				item: { type: "function_call_output", call_id: item.call_id, output: JSON.stringify(result) },
			}));
			if (item.name !== "end_call") {
				if (responseActive) {
					pendingResponse = true;
				} else {
					dc.send(JSON.stringify({ type: "response.create" }));
				}
			}
		}

		function handleEvent(evt) {
			switch (evt.type) {
				case "input_audio_buffer.speech_started":
					setStatus("Listening…");
					break;
				case "conversation.item.input_audio_transcription.completed":
					addMessage("You", (evt.transcript || "").trim(), "user");
					break;
				case "response.output_audio_transcript.delta":
					appendAssistantDelta(evt.delta || "");
					break;
				case "response.output_audio_transcript.done":
					flushAssistantDelta();
					liveAssistant = null;
					if (active && !hangupTimer) setStatus("Mario is on the line — go ahead and order.");
					break;
				case "response.output_item.done":
					if (evt.item && evt.item.type === "function_call") handleFunctionCall(evt.item);
					break;
				case "response.created":
					responseActive = true;
					if (active && !hangupTimer) setStatus("Mario is speaking…");
					break;
				case "response.done":
					responseActive = false;

					if (evt.response && evt.response.usage) {
						const usage = evt.response.usage;

						const cost =
							(usage.input_text_tokens || 0) * (5 / 1000000) +
							(usage.output_text_tokens || 0) * (20 / 1000000) +
							(usage.input_audio_tokens || 0) * (40 / 1000000) +
							(usage.output_audio_tokens || 0) * (80 / 1000000);

						fetch("/", {
							method: "POST",
							headers: {
								"Content-Type": "application/json",
							},
							body: JSON.stringify({
								action: "log_usage",
								conversation_id: evt.response.id,
								model: modelPill.textContent,
								duration: Math.floor((Date.now() - callStart) / 1000),
								usage: usage,
								cost: cost,
							}),
						}).catch((err) => CLOG.warn("usage", `could not report usage: ${err.message}`));
						CLOG.info("usage", `response.done cost=$${cost.toFixed(6)}`, usage);
					}
					if (pendingResponse && dc && dc.readyState === "open") {
						pendingResponse = false;
						dc.send(JSON.stringify({ type: "response.create" }));
					}
					break;
				case "error":
					CLOG.error("realtime", "session error", evt.error);
					addMessage("Error", JSON.stringify(evt.error), "error");
					break;
			}
		}

		orb.addEventListener("click", () => (active ? stop() : start()));
		muteBtn.addEventListener("click", () => setMuted(!muted));
		const savedVoice = localStorage.getItem("frush_voice");
		if (savedVoice && [...voiceSel.options].some((o) => o.value === savedVoice)) {
			voiceSel.value = savedVoice;
		}
		voiceSel.addEventListener("change", () => localStorage.setItem("frush_voice", voiceSel.value));
		loadMenu();
		loadModelAvailability();
	</script>
</body>
</html>"""


ADMIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
	<meta charset="UTF-8" />
	<meta name="viewport" content="width=device-width, initial-scale=1.0" />
	<title>Frush · Kitchen</title>
	<style>
		:root { --bg: #14100c; --panel: #201913; --panel-2: #2a2118; --line: #3a2f22; --text: #f4ece0; --muted: #b6a892; --red: #d43f3a; --green: #3fae5a; --gold: #e0b04a; }
		* { box-sizing: border-box; }
		html, body { margin: 0; min-height: 100%; font-family: "Segoe UI", system-ui, sans-serif; color: var(--text); background: var(--bg); }
		.wrap { max-width: 1080px; margin: 0 auto; padding: 24px 20px 40px; }
		header { display: flex; align-items: center; gap: 12px; padding-bottom: 18px; }
		header h1 { margin: 0; font-family: Georgia, serif; font-size: 1.4rem; }
		header .dot { width: 10px; height: 10px; border-radius: 50%; background: var(--green); animation: blink 2s infinite; }
		@keyframes blink { 50% { opacity: .3; } }
		header a { margin-left: auto; color: var(--muted); font-size: .85rem; }
		.cols { display: grid; grid-template-columns: 2fr 1fr; gap: 18px; }
		@media (max-width: 820px) { .cols { grid-template-columns: 1fr; } }
		.panel { background: var(--panel); border: 1px solid var(--line); border-radius: 16px; padding: 18px; }
		.panel h2 { margin: 0 0 12px; font-size: 1.05rem; font-family: Georgia, serif; }
		.empty { color: var(--muted); text-align: center; padding: 30px 0; }
		.order { background: var(--panel-2); border: 1px solid var(--line); border-radius: 12px; padding: 12px 14px; margin-bottom: 12px; }
		.order-head { display: flex; align-items: center; gap: 10px; margin-bottom: 6px; }
		.order-head strong { font-size: 1rem; }
		.order-head .meta { color: var(--muted); font-size: .8rem; }
		.badge { margin-left: auto; font-size: .68rem; text-transform: uppercase; letter-spacing: .5px; padding: 3px 9px; border-radius: 999px; font-weight: 700; }
		.badge.confirmed { background: rgba(224,176,74,.2); color: var(--gold); }
		.badge.preparing { background: rgba(109,139,255,.2); color: #9db1ff; }
		.badge.ready { background: rgba(63,174,90,.25); color: #7ee0a0; }
		.badge.completed { background: rgba(182,168,146,.15); color: var(--muted); }
		.badge.cancelled { background: rgba(212,63,58,.2); color: #ff9a94; }
		.order ul { list-style: none; margin: 6px 0; padding: 0; font-size: .88rem; }
		.order li { display: flex; justify-content: space-between; padding: 2px 0; }
		.order-foot { display: flex; align-items: center; gap: 8px; padding-top: 8px; border-top: 1px dashed var(--line); }
		.total { font-weight: 700; color: var(--green); margin-right: auto; }
		button { background: var(--panel); color: var(--text); border: 1px solid var(--line); border-radius: 8px; padding: 5px 11px; font-size: .78rem; cursor: pointer; }
		button:hover { border-color: var(--gold); }
		button.done { border-color: rgba(63,174,90,.5); color: #7ee0a0; }
		button.cancel { border-color: rgba(212,63,58,.45); color: #ff9a94; }
		.inv { width: 100%; border-collapse: collapse; font-size: .88rem; }
		.inv td { padding: 6px 4px; border-bottom: 1px dashed var(--line); }
		.inv td:last-child { text-align: right; }
		.inv .low { color: var(--gold); font-weight: 700; }
		.inv .zero { color: #ff9a94; font-weight: 700; }
		.inv input { width: 54px; background: var(--bg); color: var(--text); border: 1px solid var(--line); border-radius: 6px; padding: 3px 6px; font-size: .82rem; text-align: right; }
	</style>
</head>
<body>
	<div class="wrap">
		<header>
			<span class="dot"></span>
			<h1>🍕 Frush — Kitchen</h1>
			<a href="/">← storefront</a>
		</header>

		<div class="cols">
			<section class="panel">
				<h2>Orders</h2>
				<div id="orders"><p class="empty">No orders yet.</p></div>
			</section>

			<section class="panel">
				<h2>Inventory</h2>
				<table class="inv"><tbody id="inv"></tbody></table>
			</section>
		</div>
	</div>

	<script>
		const NEXT = { confirmed: "preparing", preparing: "ready", ready: "completed" };
		window.addEventListener("error", (e) => console.error("[admin]", e.message, e.filename, e.lineno));
		window.addEventListener("unhandledrejection", (e) => console.error("[admin] unhandled rejection", e.reason));
		async function api(action, body) {
			console.debug("[admin] POST", action, body || null);
			const r = await fetch("/", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action, ...(body || {}) }) });
			if (!r.ok) console.error("[admin]", action, "HTTP", r.status);
			return r.json();
		}
		async function setStatus(orderNo, status) { await api("set_status", { order_number: orderNo, status }); refresh(); }
		async function restock(item, stock) { await api("restock", { item, stock: Number(stock) }); refresh(); }
		function esc(s) { const d = document.createElement("div"); d.textContent = s ?? ""; return d.innerHTML; }
		function renderOrders(list) {
			const root = document.getElementById("orders");
			const open = list.filter(o => o.status !== "completed" && o.status !== "cancelled");
			const closed = list.filter(o => o.status === "completed" || o.status === "cancelled");
			const ordered = [...open.reverse(), ...closed.reverse()];
			if (!ordered.length) { root.innerHTML = '<p class="empty">No orders yet.</p>'; return; }
			root.innerHTML = ordered.map(o => {
				const items = o.items.map(l =>
					`<li><span>${l.quantity}× ${esc(l.name)}${l.size ? ` (${l.size})` : ""}</span><span>$${l.line_total}</span></li>`
				).join("");
				const t = new Date(o.created_at).toLocaleTimeString([], {hour:"2-digit", minute:"2-digit"});
				const next = NEXT[o.status];
				const actions =
					(next ? `<button class="done" onclick="setStatus('${o.order_number}','${next}')">→ ${next}</button>` : "") +
					(o.status === "confirmed" ? `<button class="cancel" onclick="setStatus('${o.order_number}','cancelled')">cancel</button>` : "");
				return `<div class="order">
					<div class="order-head">
						<strong>${o.order_number}</strong>
						<span class="meta">${esc(o.customer_name)} · ${o.fulfillment} · ${t}</span>
						<span class="badge ${o.status}">${o.status}</span>
					</div>
					<ul>${items}</ul>
					<div class="order-foot"><span class="total">$${o.total}</span>${actions}</div>
				</div>`;
			}).join("");
		}
		function renderInv(inv) {
			const tb = document.getElementById("inv");
			tb.innerHTML = Object.entries(inv).map(([id, it]) => {
				const cls = it.stock === 0 ? "zero" : it.stock <= 3 ? "low" : "";
				return `<tr>
					<td>${esc(it.name)}</td>
					<td class="${cls}">${it.stock}</td>
					<td><input type="number" min="0" value="${it.stock}" onchange="restock('${id}', this.value)" title="Set stock"></td>
				</tr>`;
			}).join("");
		}
		async function refresh() {
			try {
				const [orders, inv] = await Promise.all([api("orders"), api("inventory")]);
				renderOrders(orders);
				renderInv(inv);
			} catch {}
		}
		refresh();
		setInterval(refresh, 4000);
	</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
reload_menu()
log.info("=" * 70)
log.info("Frush web app starting: restaurant=%s menu_items=%d", RESTAURANT, len(MENU))
log.info("environment: %s", logging_setup.dumps(logging_setup.environment_report(), safe=True))
log.info("openai sdk: %s", _openai_version())
log.info("models: %s", logging_setup.dumps({k: v["label"] for k, v in MODEL_OPTIONS.items()}))
log.info("=" * 70)


# ---------------------------------------------------------------------------
# Request / error logging
# ---------------------------------------------------------------------------
@app.before_request
def _log_request_start():
		g.request_id = uuid.uuid4().hex[:8]
		g.request_started = time.monotonic()
		body = request.get_json(silent=True) if request.is_json else None
		g.request_action = body.get("action") if isinstance(body, dict) else None
		log.info(
				"--> [%s] %s %s action=%s ip=%s ua=%s",
				g.request_id,
				request.method,
				request.path,
				g.request_action,
				request.headers.get("X-Forwarded-For", request.remote_addr),
				(request.headers.get("User-Agent") or "")[:120],
		)
		if request.args:
				log.debug("    [%s] query=%s", g.request_id, logging_setup.dumps(request.args.to_dict()))
		if body is not None:
				log.debug("    [%s] payload=%s", g.request_id, logging_setup.dumps(body))


@app.after_request
def _log_request_end(response):
		rid = getattr(g, "request_id", "--------")
		elapsed = (time.monotonic() - getattr(g, "request_started", time.monotonic())) * 1000
		log.info(
				"<-- [%s] %s %s -> %s (%.0f ms)",
				rid, request.method, request.path, response.status_code, elapsed,
		)
		if response.status_code >= 400 and response.is_json and not response.direct_passthrough:
				try:
						log.warning("    [%s] error body=%s", rid, response.get_data(as_text=True)[:1000])
				except Exception:
						log.debug("    [%s] error body unavailable", rid)
		return response


@app.errorhandler(Exception)
def _log_unhandled(exc):
		rid = getattr(g, "request_id", "--------")
		if isinstance(exc, HTTPException):
				log.warning("[%s] %s %s -> %s %s", rid, request.method, request.path, exc.code, exc.name)
				return exc
		log.exception("!!! [%s] unhandled exception on %s %s", rid, request.method, request.path)
		return jsonify({
				"error": str(exc),
				"type": exc.__class__.__name__,
				"request_id": rid,
		}), 500


@app.route("/", methods=["GET", "POST"])
def endpoint():
		if request.method == "GET":
				if request.args.get("view") == "admin":
						return Response(ADMIN_HTML, mimetype="text/html")
				return Response(INDEX_HTML, mimetype="text/html")

		body = request.get_json(silent=True) or {}
		action = body.get("action", "")
		log.debug("[%s] dispatching action=%r keys=%s", getattr(g, "request_id", "?"), action, sorted(body.keys()))

		if action == "menu":
				return jsonify(menu_public())
		if action == "models":
				return jsonify({"models": model_availability()})
		if action == "inventory":
				return jsonify(inventory_public())
		if action == "orders":
				return jsonify(load_orders())
		if action == "set_status":
				return jsonify(set_status(body.get("order_number", ""), body.get("status", "")))
		if action == "restock":
				ok = restock(body.get("item", ""), int(body.get("stock", -1)))
				return jsonify({"success": ok})
		if action == "check_availability":
				return jsonify(check_availability(body))
		if action == "log_usage":
				usage = body.get("usage") or {}
				total_cost = body.get("cost")
				if total_cost is None:
						total_cost = calculate_cost(usage)

				data = {
						"timestamp": datetime.utcnow().isoformat(),
						"conversation_id": body.get("conversation_id"),
						"duration": body.get("duration"),
						"model": body.get("model"),
						"usage": usage,
						"cost": total_cost,
				}

				try:
						with LOG_FILE.open("a", encoding="utf-8") as f:
								f.write(json.dumps(data) + "\n")
				except Exception:
						log.exception("could not append to %s", LOG_FILE)
				usage_log.info("call usage %s", logging_setup.dumps(data))

				return jsonify({"success": True})
		if action == "client_log":
				entries = body.get("entries")
				if not isinstance(entries, list):
						entries = [entries] if entries else []
				for entry in entries[:200]:
						if not isinstance(entry, dict):
								entry = {"message": entry}
						level = getattr(logging, str(entry.get("level", "info")).upper(), logging.INFO)
						if not isinstance(level, int):
								level = logging.INFO
						browser_log.log(
								level,
								"[%s/%s] %s | %s",
								entry.get("session", "?"),
								entry.get("scope", "client"),
								entry.get("message", ""),
								logging_setup.dumps(entry.get("data")),
						)
				return jsonify({"success": True, "received": len(entries)})
		if action == "place_order":
				return jsonify(place(body.get("customer_name", ""), body.get("fulfillment", "pickup"), body.get("items") or []))
		if action == "session":
				voice = body.get("voice") if body.get("voice") in VOICES else VOICE
				model_key = body.get("model", "realtime-mini-2.1")
				model_config = MODEL_OPTIONS.get(model_key)
				if model_config is None:
						return jsonify({"error": "Unknown model selection"}), 400
				if not model_config["realtime"]:
						return jsonify({
								"error": f'{model_config["label"]} is not supported by the current OpenAI Realtime WebRTC call. '
								"A LiveKit agent or a separate audio gateway is required for this model."
						}), 400
				if client is None:
						log.error("realtime session requested but the OpenAI client failed to initialise")
						return jsonify({"error": "OpenAI client is not configured on the server"}), 503
				log.info("creating realtime session model=%s voice=%s", model_config["api_model"], voice)
				try:
						secret = client.realtime.client_secrets.create(
								session={
										"type": "realtime",
										"model": model_config["api_model"],
										"instructions": build_instructions(),
										"output_modalities": ["audio"],
										"tools": TOOLS,
										"tool_choice": "auto",
										"audio": {
												"input": {
														"turn_detection": {"type": "server_vad"},
														"transcription": {"model": "whisper-1"},
												},
												"output": {"voice": voice},
										},
								}
						)
						token = str(getattr(secret, "value", "") or "")
						# An ephemeral token starts with "ek_". Anything starting with "sk-"
						# means the project key would be handed to the browser, which the
						# realtime call endpoint rejects with a 401.
						log.info(
								"realtime session created: token_prefix=%r length=%d expires_at=%s",
								token[:3], len(token), getattr(secret, "expires_at", "?"),
						)
						if not token.startswith("ek_"):
								log.error(
										"expected an ephemeral ek_ token from client_secrets.create, got prefix %r "
										"(openai sdk %s) - the browser will be rejected with 401",
										token[:3], _openai_version(),
								)
						return jsonify({
								"client_secret": token,
								"model": model_config["api_model"],
								"model_label": model_config["label"],
								"voice": voice,
						})
				except Exception as exc:
						log.exception("realtime session creation failed for model=%s", model_config["api_model"])
						return jsonify({"error": str(exc), "type": exc.__class__.__name__}), 500
		log.warning("unknown action %r (payload keys: %s)", action, sorted(body.keys()))
		return jsonify({"error": f"unknown action '{action}'"}), 400


@app.get("/api/livekit/token")
def livekit_token():
		if not LIVEKIT_URL or not os.getenv("LIVEKIT_API_KEY") or not os.getenv("LIVEKIT_API_SECRET"):
				log.error(
						"LiveKit config incomplete: url=%s api_key=%s api_secret=%s",
						LIVEKIT_URL or "MISSING",
						"set" if os.getenv("LIVEKIT_API_KEY") else "MISSING",
						"set" if os.getenv("LIVEKIT_API_SECRET") else "MISSING",
				)
				return jsonify({"error": "LiveKit server configuration is incomplete"}), 503

		room_name = (request.args.get("room") or "").strip()
		identity = (request.args.get("username") or "").strip()
		model_key = request.args.get("model", "gpt-4o-mini")
		if model_key not in MODEL_OPTIONS:
				return jsonify({"error": "Unknown model selection"}), 400
		if model_key == "deepseek-v4-flash" and not os.getenv("DEEPSEEK_API_KEY"):
				return jsonify({"error": "DEEPSEEK_API_KEY is not configured on the server"}), 503
		if model_key == "gpt-4o-mini" and not os.getenv("OPENAI_API_KEY"):
				return jsonify({"error": "OPENAI_API_KEY is not configured on the server"}), 503
		if not room_name:
				alphabet = string.ascii_lowercase + string.digits
				room_name = "frush-" + model_key + "-" + "".join(secrets.choice(alphabet) for _ in range(16))
		if not identity:
				identity = "guest-" + secrets.token_hex(6)

		try:
				token = (
						AccessToken(os.getenv("LIVEKIT_API_KEY"), os.getenv("LIVEKIT_API_SECRET"))
						.with_identity(identity)
						.with_name(identity)
						.with_ttl(timedelta(hours=1))
						.with_grants(VideoGrants(
								room_join=True,
								room=room_name,
								can_publish=True,
								can_subscribe=True,
								can_publish_data=True,
						))
						.to_jwt()
				)
				log.info(
						"issued LiveKit token: room=%s identity=%s model=%s server=%s",
						room_name, identity, model_key, LIVEKIT_URL,
				)
				return jsonify({"token": token, "serverUrl": LIVEKIT_URL, "roomName": room_name, "identity": identity})
		except Exception as exc:
				log.exception("LiveKit token generation failed for room=%s model=%s", room_name, model_key)
				return jsonify({"error": str(exc), "type": exc.__class__.__name__}), 500


def _log_view_authorised() -> bool:
		token = os.getenv("LOG_VIEW_TOKEN", "")
		if not token:
				return False
		supplied = request.args.get("token") or request.headers.get("X-Log-Token", "")
		return secrets.compare_digest(supplied, token)


FAVICON = (
		'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
		'<text y=".9em" font-size="90">🍕</text></svg>'
)


@app.get("/favicon.ico")
def favicon():
		response = Response(FAVICON, mimetype="image/svg+xml")
		response.headers["Cache-Control"] = "public, max-age=86400"
		return response


@app.get("/api/health")
def health():
		info = {
				"ok": True,
				"restaurant": RESTAURANT,
				"openai_client": client is not None,
				"menu_items": len(MENU),
				"models": model_availability(),
				"worker": worker_status(),
				"log_dir": str(LOG_DIR),
		}
		if _log_view_authorised():
				info["environment"] = logging_setup.environment_report()
		return jsonify(info)


@app.get("/api/logs")
def read_logs():
		if not os.getenv("LOG_VIEW_TOKEN"):
				return jsonify({"error": "log viewing disabled; set LOG_VIEW_TOKEN to enable"}), 404
		if not _log_view_authorised():
				log.warning("rejected /api/logs request from %s", request.remote_addr)
				return jsonify({"error": "unauthorized"}), 403
		name = request.args.get("file", "web.log")
		if "/" in name or "\\" in name or name.startswith("."):
				return jsonify({"error": "invalid file name"}), 400
		try:
				lines = max(1, min(int(request.args.get("lines", 200)), 5000))
		except ValueError:
				lines = 200
		return jsonify({
				"file": name,
				"available": sorted(p.name for p in LOG_DIR.glob("*") if p.is_file()),
				"lines": logging_setup.tail(LOG_DIR / name, lines),
		})


if __name__ == "__main__":
		if not os.getenv("OPENAI_API_KEY"):
				raise SystemExit("OPENAI_API_KEY is not set. Put it in a .env file.")
		print("Open http://localhost:5000 in your browser")
		app.run(host="127.0.0.1", port=5000, debug=True)
