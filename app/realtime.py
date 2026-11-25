"""WebSocket fan-out: every browser viewing an event's seat map gets live updates.

Endpoints are sync (they run in a threadpool), so publish() hands the broadcast
over to the event loop with run_coroutine_threadsafe.
"""
import asyncio
import logging
from collections import defaultdict

from fastapi import WebSocket

log = logging.getLogger("ticketdhaka.ws")


class Hub:
    def __init__(self) -> None:
        self._rooms: dict[int, set[WebSocket]] = defaultdict(set)
        self.loop: asyncio.AbstractEventLoop | None = None

    async def join(self, event_id: int, ws: WebSocket) -> None:
        await ws.accept()
        self._rooms[event_id].add(ws)

    def leave(self, event_id: int, ws: WebSocket) -> None:
        self._rooms[event_id].discard(ws)

    async def _broadcast(self, event_id: int, message: dict) -> None:
        dead = []
        for ws in list(self._rooms.get(event_id, ())):
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.leave(event_id, ws)

    def publish(self, event_id: int, message: dict) -> None:
        if self.loop is None or not self._rooms.get(event_id):
            return
        try:
            asyncio.run_coroutine_threadsafe(self._broadcast(event_id, message), self.loop)
        except RuntimeError as e:
            log.warning("ws publish failed: %s", e)


hub = Hub()
