"""LiveKit voice worker for Frush ordering calls."""

from __future__ import annotations

import json
import os

from livekit import agents
from livekit.agents import Agent, AgentSession, JobContext, function_tool
from livekit.plugins import elevenlabs, openai

import app


DEFAULT_MODEL = "gpt-4o-mini"
MODEL_PREFIX = "frush-"


def model_key_from_room(room_name: str) -> str:
    parts = room_name.split("-")
    key = "-".join(parts[1:4]) if room_name.startswith(MODEL_PREFIX) else ""
    return key if key in app.MODEL_OPTIONS else DEFAULT_MODEL


def make_llm(model_key: str):
    config = app.MODEL_OPTIONS[model_key]
    if model_key == "realtime-mini-2.1":
        if not os.getenv("OPENAI_API_KEY"):
            raise RuntimeError("OPENAI_API_KEY is not configured on the server")
        return openai.realtime.RealtimeModel(model=config["api_model"])
    if config["provider"] == "deepseek":
        if not os.getenv("DEEPSEEK_API_KEY"):
            raise RuntimeError("DEEPSEEK_API_KEY is not configured on the server")
        return openai.LLM(
            model=config["api_model"],
            api_key=os.getenv("DEEPSEEK_API_KEY"),
            base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
        )
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is not configured on the server")
    return openai.LLM(model=config["api_model"])


class FoodOrderingAgent(Agent):
    def __init__(self) -> None:
        super().__init__(instructions=app.build_instructions())

    @function_tool()
    async def check_availability(self, item: str, quantity: int = 1) -> str:
        """Check whether a menu item is in stock."""
        return json.dumps(app.check_availability({"item": item, "quantity": quantity}))

    @function_tool()
    async def place_order(self, customer_name: str, fulfillment: str, items: list[dict]) -> str:
        """Place a confirmed pickup or delivery order."""
        return json.dumps(app.place(customer_name, fulfillment, items))


async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect()
    model_key = model_key_from_room(ctx.room.name)
    config = app.MODEL_OPTIONS[model_key]
    session_kwargs = {"llm": make_llm(model_key)}
    if model_key != "realtime-mini-2.1":
        if not os.getenv("ELEVENLABS_API_KEY"):
            raise RuntimeError("ELEVENLABS_API_KEY is not configured on the server")
        session_kwargs.update({
            "stt": openai.STT(),
            "tts": elevenlabs.TTS(
                voice_id=app.ELEVENLABS_VOICE_ID,
                model="eleven_flash_v2_5",
            ),
        })

    session = AgentSession(**session_kwargs)
    await session.start(room=ctx.room, agent=FoodOrderingAgent())
    await session.generate_reply(
        instructions=f"Greet the caller as Mario using the {config['label']} model."
    )


if __name__ == "__main__":
    agents.cli.run_app(
        agents.WorkerOptions(
            entrypoint_fnc=entrypoint,
            ws_url=os.getenv("LIVEKIT_URL"),
            api_key=os.getenv("LIVEKIT_API_KEY"),
            api_secret=os.getenv("LIVEKIT_API_SECRET"),
        )
    )
