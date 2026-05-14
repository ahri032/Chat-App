from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from pydantic import BaseModel

from database import get_db, init_db
from models import User, Room, RoomMember, Message, Friendship
from auth import hash_password, verify_password, create_token, decode_token
from connection_manager import manager


# ── 시작/종료 훅 ─────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()  # 서버 시작 시 테이블 생성
    yield


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")


# ── 요청 스키마 ───────────────────────────────────────────────
class RegisterRequest(BaseModel):
    username: str
    password: str

class LoginRequest(BaseModel):
    username: str
    password: str

class CreateRoomRequest(BaseModel):
    name: str
    invite_user_ids: list[int] = []


# ── 인증 헬퍼 ─────────────────────────────────────────────────
async def get_current_user(token: str, db: AsyncSession) -> User:
    payload = decode_token(token)
    user = await db.get(User, int(payload["sub"]))
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user


# ── REST API ──────────────────────────────────────────────────

@app.get("/")
async def root():
    return FileResponse("static/index.html")


@app.post("/register")
async def register(body: RegisterRequest, db: AsyncSession = Depends(get_db)):
    existing = await db.scalar(select(User).where(User.username == body.username))
    if existing:
        raise HTTPException(status_code=400, detail="이미 존재하는 유저명입니다.")
    user = User(username=body.username, hashed_password=hash_password(body.password))
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return {"token": create_token(user.id, user.username)}


@app.post("/login")
async def login(body: LoginRequest, db: AsyncSession = Depends(get_db)):
    user = await db.scalar(select(User).where(User.username == body.username))
    if not user or not verify_password(body.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="아이디 또는 비밀번호가 틀렸습니다.")
    return {"token": create_token(user.id, user.username)}


@app.get("/users/search")
async def search_users(q: str, token: str, db: AsyncSession = Depends(get_db)):
    me = await get_current_user(token, db)
    users = await db.scalars(
        select(User).where(User.username.ilike(f"%{q}%"), User.id != me.id).limit(10)
    )
    return [{"id": u.id, "username": u.username} for u in users]


@app.post("/rooms")
async def create_room(
    body: CreateRoomRequest,
    token: str,
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(token, db)
    room = Room(name=body.name)
    db.add(room)
    await db.flush()
    db.add(RoomMember(room_id=room.id, user_id=user.id))
    for uid in body.invite_user_ids:
        if uid != user.id:
            db.add(RoomMember(room_id=room.id, user_id=uid))
    await db.commit()
    return {"room_id": room.id, "name": room.name}


@app.get("/rooms")
async def list_rooms(token: str, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(token, db)
    memberships = await db.scalars(
        select(RoomMember).where(RoomMember.user_id == user.id).options(selectinload(RoomMember.room))
    )
    return [{"id": m.room.id, "name": m.room.name} for m in memberships]


@app.get("/rooms/all")
async def list_all_rooms(token: str, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(token, db)
    all_rooms = await db.scalars(select(Room))
    my_room_ids = {m.room_id for m in await db.scalars(
        select(RoomMember).where(RoomMember.user_id == user.id)
    )}
    return [{"id": r.id, "name": r.name, "joined": r.id in my_room_ids} for r in all_rooms]


@app.post("/rooms/{room_id}/join")
async def join_room(room_id: int, token: str, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(token, db)
    existing = await db.scalar(
        select(RoomMember).where(RoomMember.room_id == room_id, RoomMember.user_id == user.id)
    )
    if not existing:
        db.add(RoomMember(room_id=room_id, user_id=user.id))
        await db.commit()
    return {"ok": True}


@app.delete("/rooms/{room_id}/leave")
async def leave_room(room_id: int, token: str, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(token, db)
    member = await db.scalar(
        select(RoomMember).where(RoomMember.room_id == room_id, RoomMember.user_id == user.id)
    )
    if member:
        await db.delete(member)
        await db.commit()
    return {"ok": True}


@app.get("/rooms/{room_id}/members")
async def get_room_members(room_id: int, token: str, db: AsyncSession = Depends(get_db)):
    await get_current_user(token, db)
    members = await db.scalars(
        select(RoomMember).where(RoomMember.room_id == room_id).options(selectinload(RoomMember.user))
    )
    return [{"user_id": m.user_id, "username": m.user.username} for m in members]


@app.get("/rooms/{room_id}/messages")
async def get_messages(room_id: int, token: str, db: AsyncSession = Depends(get_db)):
    await get_current_user(token, db)
    msgs = await db.scalars(
        select(Message)
        .where(Message.room_id == room_id)
        .options(selectinload(Message.sender))
        .order_by(Message.created_at)
        .limit(50)
    )
    return [
        {
            "id": m.id,
            "sender": m.sender.username,
            "content": m.content,
            "created_at": m.created_at.isoformat(),
        }
        for m in msgs
    ]


# ── 방 초대 ───────────────────────────────────────────────────

@app.post("/rooms/{room_id}/invite/{user_id}")
async def invite_to_room(room_id: int, user_id: int, token: str, db: AsyncSession = Depends(get_db)):
    await get_current_user(token, db)
    existing = await db.scalar(
        select(RoomMember).where(RoomMember.room_id == room_id, RoomMember.user_id == user_id)
    )
    if not existing:
        db.add(RoomMember(room_id=room_id, user_id=user_id))
        await db.commit()
    return {"ok": True}


# ── 친구 ──────────────────────────────────────────────────────

@app.post("/friends/request/{to_user_id}")
async def send_friend_request(to_user_id: int, token: str, db: AsyncSession = Depends(get_db)):
    me = await get_current_user(token, db)
    if me.id == to_user_id:
        raise HTTPException(status_code=400, detail="자기 자신에게 요청할 수 없습니다.")
    existing = await db.scalar(
        select(Friendship).where(
            ((Friendship.from_user_id == me.id) & (Friendship.to_user_id == to_user_id)) |
            ((Friendship.from_user_id == to_user_id) & (Friendship.to_user_id == me.id))
        )
    )
    if existing:
        raise HTTPException(status_code=400, detail="이미 요청이 존재합니다.")
    db.add(Friendship(from_user_id=me.id, to_user_id=to_user_id, status="pending"))
    await db.commit()
    return {"ok": True}


@app.get("/friends/requests")
async def get_friend_requests(token: str, db: AsyncSession = Depends(get_db)):
    me = await get_current_user(token, db)
    reqs = await db.scalars(
        select(Friendship)
        .where(Friendship.to_user_id == me.id, Friendship.status == "pending")
        .options(selectinload(Friendship.from_user))
    )
    return [{"id": r.id, "from_user_id": r.from_user_id, "username": r.from_user.username} for r in reqs]


@app.post("/friends/accept/{friendship_id}")
async def accept_friend(friendship_id: int, token: str, db: AsyncSession = Depends(get_db)):
    me = await get_current_user(token, db)
    req = await db.get(Friendship, friendship_id)
    if not req or req.to_user_id != me.id:
        raise HTTPException(status_code=404, detail="요청을 찾을 수 없습니다.")
    req.status = "accepted"
    await db.commit()
    return {"ok": True}


@app.post("/friends/reject/{friendship_id}")
async def reject_friend(friendship_id: int, token: str, db: AsyncSession = Depends(get_db)):
    me = await get_current_user(token, db)
    req = await db.get(Friendship, friendship_id)
    if not req or req.to_user_id != me.id:
        raise HTTPException(status_code=404, detail="요청을 찾을 수 없습니다.")
    req.status = "rejected"
    await db.commit()
    return {"ok": True}


@app.get("/friends")
async def get_friends(token: str, db: AsyncSession = Depends(get_db)):
    me = await get_current_user(token, db)
    rows = await db.scalars(
        select(Friendship)
        .where(
            ((Friendship.from_user_id == me.id) | (Friendship.to_user_id == me.id)),
            Friendship.status == "accepted"
        )
        .options(selectinload(Friendship.from_user), selectinload(Friendship.to_user))
    )
    friends = []
    for r in rows:
        friend = r.to_user if r.from_user_id == me.id else r.from_user
        friends.append({"id": friend.id, "username": friend.username})
    return friends


# ── WebSocket ─────────────────────────────────────────────────

@app.websocket("/ws/{room_id}")
async def websocket_endpoint(
    ws: WebSocket,
    room_id: int,
    token: str,
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(token, db)

    await manager.connect(room_id, user.id, user.username, ws)
    try:
        while True:
            # 클라이언트가 보낸 텍스트를 기다린다 (블로킹)
            text = await ws.receive_text()

            # DB에 저장
            msg = Message(room_id=room_id, sender_id=user.id, content=text)
            db.add(msg)
            await db.commit()
            await db.refresh(msg)

            # 보낸 사람 제외하고 나머지에게 브로드캐스트
            await manager.broadcast(room_id, {
                "type": "message",
                "sender": user.username,
                "content": text,
                "created_at": msg.created_at.isoformat(),
            }, exclude_user=user.id)
    except WebSocketDisconnect:
        manager.disconnect(room_id, user.id)
        await manager.broadcast(room_id, {
            "type": "system",
            "content": f"{user.username}님이 퇴장했습니다.",
        })
