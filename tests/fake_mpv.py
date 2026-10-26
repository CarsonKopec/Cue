"""A stand-in for mpv that speaks its JSON IPC protocol, for tests.

Plays a pretend file of ``FAKE_MPV_DURATION`` seconds at ``FAKE_MPV_SPEED`` x real
time, honours ``--start``, ``seek``, ``observe_property`` and ``quit``, and appends
its arguments and every command it receives to ``FAKE_MPV_LOG`` (JSON lines).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

TICK = 0.02


class FakeMpv:
    def __init__(self, argv: list[str]) -> None:
        self.address = ""
        self.position = 0.0
        for arg in argv:
            if arg.startswith("--input-ipc-server="):
                self.address = arg.split("=", 1)[1]
            elif arg.startswith("--start="):
                self.position = float(arg.split("=", 1)[1])
        self.duration = float(os.environ.get("FAKE_MPV_DURATION", "30"))
        self.speed = float(os.environ.get("FAKE_MPV_SPEED", "1"))
        self.log_path = os.environ.get("FAKE_MPV_LOG")
        self.observed: dict[str, int] = {}
        self.eof = False
        self.writer: asyncio.StreamWriter | None = None
        self.done = asyncio.Event()
        self.log({"args": argv})

    def log(self, entry: dict[str, Any]) -> None:
        if self.log_path:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")

    def send(self, message: dict[str, Any]) -> None:
        if self.writer is not None and not self.writer.is_closing():
            self.writer.write(json.dumps(message).encode() + b"\n")

    def value(self, name: str) -> Any:
        return {"time-pos": self.position, "duration": self.duration, "eof-reached": self.eof}.get(name)

    def notify(self, name: str) -> None:
        if name in self.observed:
            self.send({"event": "property-change", "id": self.observed[name], "name": name, "data": self.value(name)})

    def move_to(self, position: float) -> None:
        self.position = max(0.0, min(position, self.duration))
        self.notify("time-pos")
        if self.position >= self.duration and not self.eof:
            self.eof = True
            self.notify("eof-reached")

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.writer = writer
        while line := await reader.readline():
            request = json.loads(line)
            command = request.get("command", [])
            self.log({"command": command})
            reply: dict[str, Any] = {"error": "success", "data": None, "request_id": request.get("request_id", 0)}
            name = command[0] if command else ""
            if name == "observe_property":
                self.observed[command[2]] = command[1]
                self.send(reply)
                self.notify(command[2])
                continue
            if name == "get_property":
                reply["data"] = self.value(command[1])
            elif name == "seek":
                mode = command[2] if len(command) > 2 else "relative"
                self.move_to(command[1] if mode == "absolute" else self.position + command[1])
            elif name == "quit":
                self.send(reply)
                await writer.drain()
                self.done.set()
                return
            self.send(reply)

    async def play(self) -> None:
        while not self.done.is_set():
            await asyncio.sleep(TICK)
            if self.writer is not None and not self.eof:
                self.move_to(self.position + TICK * self.speed)

    async def serve(self) -> None:
        loop = asyncio.get_running_loop()
        if sys.platform == "win32":

            def factory() -> asyncio.StreamReaderProtocol:
                return asyncio.StreamReaderProtocol(asyncio.StreamReader(), self.handle)

            servers = await loop.start_serving_pipe(factory, self.address)
        else:
            servers = [await asyncio.start_unix_server(self.handle, self.address)]
        try:
            await self.play()
        finally:
            for server in servers:
                server.close()


if __name__ == "__main__":
    asyncio.run(FakeMpv(sys.argv[1:]).serve())
