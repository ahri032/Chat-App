import json
import uuid
from fastapi import WebSocket


class ConnectionManager:
    def __init__(self):
        # { room_id: { conn_id: {"user_id": int, "username": str, "ws": WebSocket} } }
        self.rooms: dict[int, dict[str, dict]] = {}

    async def connect(self, room_id: int, user_id: int, username: str, ws: WebSocket) -> str:
        await ws.accept()
        conn_id = str(uuid.uuid4())
        if room_id not in self.rooms:
            self.rooms[room_id] = {}
        self.rooms[room_id][conn_id] = {"user_id": user_id, "username": username, "ws": ws}
        await self.broadcast(room_id, {
            "type": "system",
            "content": f"{username}님이 입장했습니다.",
        }, exclude_conn=conn_id)
        return conn_id

    def disconnect(self, room_id: int, conn_id: str):
        if room_id in self.rooms:
            self.rooms[room_id].pop(conn_id, None)
            if not self.rooms[room_id]:
                del self.rooms[room_id]

    async def broadcast(
        self,
        room_id: int,
        message: dict,
        exclude_conn: str | None = None,
        exclude_user: int | None = None,
    ):
        if room_id not in self.rooms:
            return
        dead = []
        for conn_id, conn in self.rooms[room_id].items():
            if conn_id == exclude_conn:
                continue
            if exclude_user is not None and conn["user_id"] == exclude_user:
                continue
            try:
                await conn["ws"].send_text(json.dumps(message, ensure_ascii=False))
            except Exception:
                dead.append(conn_id)
        for conn_id in dead:
            self.rooms[room_id].pop(conn_id, None)


manager = ConnectionManager()
