from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

import nats
from nats.aio.msg import Msg
from nats.js.api import RetentionPolicy, StorageType, StreamConfig

from .settings import Settings

log = logging.getLogger(__name__)
STREAM = "CIA_BRAIN"
SUBJECTS = ["cia.fetched", "cia.extracted"]


async def connect(settings: Settings):
    delay = 1
    while True:
        try:
            nc = await nats.connect(settings.nats_url, max_reconnect_attempts=-1)
            js = nc.jetstream()
            try:
                await js.stream_info(STREAM)
            except Exception:
                await js.add_stream(
                    StreamConfig(
                        name=STREAM,
                        subjects=SUBJECTS,
                        retention=RetentionPolicy.WORK_QUEUE,
                        storage=StorageType.FILE,
                    )
                )
            return nc, js
        except Exception as exc:
            log.warning("NATS unavailable: %s", exc)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 10)


async def publish_json(js, subject: str, payload: dict) -> None:
    await js.publish(subject, json.dumps(payload, separators=(",", ":")).encode())


async def consume_forever(
    settings: Settings,
    subject: str,
    durable: str,
    handler: Callable[[dict], Awaitable[None]],
) -> None:
    nc, js = await connect(settings)

    async def cb(msg: Msg) -> None:
        try:
            payload = json.loads(msg.data)
            await handler(payload)
            await msg.ack()
        except Exception:
            log.exception("message handler failed", extra={"event": subject})
            await msg.nak(delay=30)

    await js.subscribe(subject, durable=durable, cb=cb, manual_ack=True)
    log.info("consumer ready: %s / %s", subject, durable)
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await nc.drain()
