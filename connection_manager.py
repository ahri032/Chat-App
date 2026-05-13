import json
from fastapi import WebSocket


class ConnectionManager:
    """
    채팅방별 WebSocket 연결을 관리한다.
    room_id -> {user_id -> WebSocket} 구조로 메모리에 저장.
    C의 포인터 테이블과 비슷한 개념.
    """

    def __init__(self):
        # { room_id: { user_id: WebSocket } }
        self.rooms: dict[int, dict[int, WebSocket]] = {}

    async def connect(self, room_id: int, user_id: int, username: str, ws: WebSocket):
        await ws.accept()
        if room_id not in self.rooms:
            self.rooms[room_id] = {}
        self.rooms[room_id][user_id] = ws
        await self.broadcast(room_id, {
            "type": "system",
            "content": f"{username}님이 입장했습니다.",
        }, exclude_user=None)

    def disconnect(self, room_id: int, user_id: int):
        if room_id in self.rooms:
            self.rooms[room_id].pop(user_id, None)
            if not self.rooms[room_id]:
                del self.rooms[room_id]

    async def broadcast(self, room_id: int, message: dict, exclude_user: int | None = None):
        if room_id not in self.rooms:
            return
        dead = []
        for uid, ws in self.rooms[room_id].items():
            if uid == exclude_user:
                continue
            try:
                await ws.send_text(json.dumps(message, ensure_ascii=False))
            except Exception:
                dead.append(uid)
        for uid in dead:
            self.rooms[room_id].pop(uid, None)

    async def send_to(self, room_id: int, user_id: int, message: dict):
        ws = self.rooms.get(room_id, {}).get(user_id)
        if ws:
            await ws.send_text(json.dumps(message, ensure_ascii=False))


manager = ConnectionManager()
