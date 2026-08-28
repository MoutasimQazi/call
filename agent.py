"""LiveKit voice worker for Frush ordering calls."""

from __future__ import annotations

import json
import os
import time

from livekit import agents
from livekit.agents import Agent, AgentSession, JobContext, function_tool
from livekit.plugins import elevenlabs, openai

import logging_setup

log = logging_setup.setup_logging("agent")

import app


DEFAULT_MODEL = "gpt-4o-mini"
MODEL_PREFIX = "frush-"


def model_key_from_room(room_name: str) -> str:
    parts = room_name.split("-")
    key = "-".join(parts[1:4]) if room_name.startswith(MODEL_PREFIX) else ""
    resolved = key if key in app.MODEL_OPTIONS else DEFAULT_MODEL
    if resolved != key:
        log.warning(
            "room %r did not map to a known model (parsed %r) - falling back to %s",
            room_name, key, DEFAULT_MODEL,
        )
    else:
        log.info("room %r resolved to model %s", room_name, resolved)
    return resolved


def make_llm(model_key: str):
    config = app.MODEL_OPTIONS[model_key]
    log.info("building LLM for %s: %s", model_key, logging_setup.dumps(config))
    if model_key == "realtime-mini-2.1":
        if not os.getenv("OPENAI_API_KEY"):
            log.error("OPENAI_API_KEY missing - cannot build realtime model %s", model_key)
            raise RuntimeError("OPENAI_API_KEY is not configured on the server")
        log.debug("using openai.realtime.RealtimeModel(model=%s)", config["api_model"])
        return openai.realtime.RealtimeModel(model=config["api_model"])
    if config["provider"] == "deepseek":
        if not os.getenv("DEEPSEEK_API_KEY"):
            log.error("DEEPSEEK_API_KEY missing - cannot build %s", model_key)
            raise RuntimeError("DEEPSEEK_API_KEY is not configured on the server")
        base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
        log.debug("using deepseek LLM model=%s base_url=%s", config["api_model"], base_url)
        return openai.LLM(
            model=config["api_model"],
            api_key=os.getenv("DEEPSEEK_API_KEY"),
            base_url=base_url,
        )
    if not os.getenv("OPENAI_API_KEY"):
        log.error("OPENAI_API_KEY missing - cannot build %s", model_key)
        raise RuntimeError("OPENAI_API_KEY is not configured on the server")
    log.debug("using openai.LLM(model=%s)", config["api_model"])
    return openai.LLM(model=config["api_model"])


class FoodOrderingAgent(Agent):
    def __init__(self) -> None:
        instructions = app.build_instructions()
        log.debug("agent instructions built (%d chars)", len(instructions))
        super().__init__(instructions=instructions)

    @function_tool()
    async def check_availability(self, item: str, quantity: int = 1) -> str:
        """Check whether a menu item is in stock."""
        log.info("[TOOL] check_availability item=%r quantity=%s", item, quantity)
        started = time.monotonic()
        try:
            result = app.check_availability({"item": item, "quantity": quantity})
        except Exception:
            log.exception("[TOOL] check_availability failed for item=%r", item)
            raise
        log.info(
            "[TOOL] check_availability -> %s (%.0f ms)",
            logging_setup.dumps(result), (time.monotonic() - started) * 1000,
        )
        return json.dumps(result)

    @function_tool()
    async def place_order(self, customer_name: str, fulfillment: str, items: list[dict]) -> str:
        """Place a confirmed pickup or delivery order."""
        log.info(
            "[TOOL] place_order customer=%r fulfillment=%r items=%s",
            customer_name, fulfillment, logging_setup.dumps(items),
        )
        started = time.monotonic()
        try:
            result = app.place(customer_name, fulfillment, items)
        except Exception:
            log.exception("[TOOL] place_order failed for customer=%r", customer_name)
            raise
        log.info(
            "[TOOL] place_order -> %s (%.0f ms)",
            logging_setup.dumps(result), (time.monotonic() - started) * 1000,
        )
        return json.dumps(result)


def _attach_session_logging(session: AgentSession, room_name: str) -> None:
    """Subscribe to every AgentSession event this livekit-agents build exposes.

    Event names differ between livekit-agents releases, so each subscription is
    attempted independently and a missing one is logged rather than fatal.
    """
    events = [
        "user_input_transcribed",
        "conversation_item_added",
        "agent_state_changed",
        "user_state_changed",
        "speech_created",
        "function_tools_executed",
        "metrics_collected",
        "agent_false_interruption",
        "input_audio_transcription_completed",
        "error",
        "close",
    ]

    def make_handler(name: str):
        def handler(event=None):
            level = log.error if name == "error" else log.info
            level("[SESSION:%s] %s | %s", room_name, name, _describe(event))
        return handler

    attached, missing = [], []
    for name in events:
        try:
            session.on(name, make_handler(name))
            attached.append(name)
        except Exception as exc:
            missing.append(f"{name} ({exc.__class__.__name__})")
    log.info("[SESSION:%s] event logging attached: %s", room_name, ", ".join(attached))
    if missing:
        log.warning(
            "[SESSION:%s] events not available in this livekit-agents build: %s",
            room_name, ", ".join(missing),
        )


def _attach_room_logging(room, room_name: str) -> None:
    """Log room-level transport events (participants, tracks, disconnects)."""
    events = [
        "participant_connected",
        "participant_disconnected",
        "track_published",
        "track_subscribed",
        "track_unsubscribed",
        "track_muted",
        "track_unmuted",
        "connection_quality_changed",
        "connection_state_changed",
        "reconnecting",
        "reconnected",
        "disconnected",
    ]

    def make_handler(name: str):
        def handler(*args):
            log.info("[ROOM:%s] %s | %s", room_name, name, " | ".join(_describe(a) for a in args))
        return handler

    for name in events:
        try:
            room.on(name, make_handler(name))
        except Exception as exc:
            log.debug("[ROOM:%s] cannot subscribe to %s: %s", room_name, name, exc)


def _describe(obj) -> str:
    """Best-effort readable rendering of a LiveKit event payload."""
    if obj is None:
        return "<no payload>"
    for attr in ("model_dump", "dict", "asdict"):
        method = getattr(obj, attr, None)
        if callable(method):
            try:
                return logging_setup.dumps(method())
            except Exception:
                break
    if isinstance(obj, (dict, list, str, int, float, bool)):
        return logging_setup.dumps(obj)
    fields = {
        key: value
        for key, value in vars(obj).items()
        if not key.startswith("_")
    } if hasattr(obj, "__dict__") else {}
    return logging_setup.dumps(fields) if fields else f"{type(obj).__name__}({obj!r})"


async def entrypoint(ctx: JobContext) -> None:
    started = time.monotonic()
    room_name = getattr(ctx.room, "name", "<unknown>")
    log.info("=" * 70)
    log.info("[JOB] entrypoint start room=%s job=%s", room_name, getattr(getattr(ctx, "job", None), "id", "?"))
    log.info("[JOB] environment: %s", logging_setup.dumps(logging_setup.environment_report(), safe=True))
    logging_setup.install_asyncio_logging()

    try:
        await ctx.connect()
        room_name = getattr(ctx.room, "name", room_name)
        log.info("[JOB] connected to room=%s in %.0f ms", room_name, (time.monotonic() - started) * 1000)
        _attach_room_logging(ctx.room, room_name)

        model_key = model_key_from_room(room_name)
        config = app.MODEL_OPTIONS[model_key]
        session_kwargs = {"llm": make_llm(model_key)}
        if model_key != "realtime-mini-2.1":
            if not os.getenv("ELEVENLABS_API_KEY"):
                log.error("ELEVENLABS_API_KEY missing - TTS cannot start for model %s", model_key)
                raise RuntimeError("ELEVENLABS_API_KEY is not configured on the server")
            log.info(
                "[JOB] pipeline mode: STT=openai.STT TTS=elevenlabs(voice=%s, model=eleven_flash_v2_5)",
                app.ELEVENLABS_VOICE_ID,
            )
            session_kwargs.update({
                "stt": openai.STT(),
                "tts": elevenlabs.TTS(
                    voice_id=app.ELEVENLABS_VOICE_ID,
                    model="eleven_flash_v2_5",
                ),
            })
        else:
            log.info("[JOB] realtime mode: speech handled end-to-end by %s", config["api_model"])

        session = AgentSession(**session_kwargs)
        _attach_session_logging(session, room_name)

        log.info("[JOB] starting session for room=%s model=%s", room_name, model_key)
        await session.start(room=ctx.room, agent=FoodOrderingAgent())
        log.info("[JOB] session started (%.0f ms since entry)", (time.monotonic() - started) * 1000)

        await session.generate_reply(
            instructions=f"Greet the caller as Mario using the {config['label']} model."
        )
        log.info("[JOB] greeting dispatched for room=%s", room_name)
    except Exception:
        log.exception("[JOB] entrypoint failed for room=%s", room_name)
        raise


if __name__ == "__main__":
    log.info("[WORKER] starting LiveKit worker")
    log.info("[WORKER] environment: %s", logging_setup.dumps(logging_setup.environment_report(), safe=True))
    if not os.getenv("LIVEKIT_URL"):
        log.error("[WORKER] LIVEKIT_URL is not set - the worker cannot register")
    agents.cli.run_app(
        agents.WorkerOptions(
            entrypoint_fnc=entrypoint,
            ws_url=os.getenv("LIVEKIT_URL"),
            api_key=os.getenv("LIVEKIT_API_KEY"),
            api_secret=os.getenv("LIVEKIT_API_SECRET"),
        )
    )
