"""
WebRTC Signaling Server — aiohttp + WebSockets
Uses aiohttp to handle both HTTP health checks and WebSocket connections
on the same port, compatible with Render's HEAD health check.
"""

import asyncio
import json
import logging
import os
import uuid
from collections import defaultdict
from datetime import datetime, timezone

from aiohttp import web
import aiohttp

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

PORT = int(os.environ.get("PORT", 8765))

rooms: dict = defaultdict(dict)
waiting_room: dict = defaultdict(list)


async def broadcast(room_id, message, exclude=None):
    payload = json.dumps(message)
    for pid, peer in list(rooms[room_id].items()):
        if pid == exclude:
            continue
        try:
            await peer["ws"].send_str(payload)
        except Exception:
            rooms[room_id].pop(pid, None)


async def send_to(room_id, peer_id, message):
    peer = rooms[room_id].get(peer_id)
    if peer:
        try:
            await peer["ws"].send_str(json.dumps(message))
        except Exception:
            rooms[room_id].pop(peer_id, None)


def room_members(room_id, exclude=None):
    return [
        {"peer_id": p["peer_id"], "name": p["name"], "is_host": p.get("is_host", False)}
        for pid, p in rooms[room_id].items()
        if pid != exclude
    ]


# ── Health check endpoint (handles GET and HEAD) ──────────────────────────────
async def health(request):
    return web.Response(text="OK", status=200)


# ── WebSocket handler ─────────────────────────────────────────────────────────
async def ws_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    peer_id = str(uuid.uuid4())[:8]
    room_id = None
    peer_name = "Anonymous"
    is_host = False

    log.info(f"New WS connection: {peer_id}")

    try:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue

                kind = data.get("type")

                if kind == "join":
                    room_id = data.get("room_id", "default")
                    peer_name = data.get("name", f"User-{peer_id}")
                    existing = rooms[room_id]

                    if not existing:
                        is_host = True
                        rooms[room_id][peer_id] = {"ws": ws, "peer_id": peer_id, "name": peer_name, "is_host": True}
                        await ws.send_str(json.dumps({"type": "joined", "peer_id": peer_id, "room_id": room_id, "is_host": True, "members": []}))
                        log.info(f"{peer_name} created room {room_id} as HOST")
                    else:
                        waiting_room[room_id].append({"ws": ws, "peer_id": peer_id, "name": peer_name})
                        await ws.send_str(json.dumps({"type": "waiting", "peer_id": peer_id, "message": "Waiting for the host to admit you..."}))
                        host_peer = next((p for p in existing.values() if p.get("is_host")), None)
                        if host_peer:
                            await host_peer["ws"].send_str(json.dumps({"type": "admission_request", "peer_id": peer_id, "name": peer_name}))

                elif kind == "admit":
                    target_id = data.get("peer_id")
                    admit = data.get("admit", True)
                    waiting = waiting_room.get(room_id, [])
                    guest = next((g for g in waiting if g["peer_id"] == target_id), None)
                    if not guest:
                        continue
                    waiting_room[room_id] = [g for g in waiting if g["peer_id"] != target_id]
                    if admit:
                        rooms[room_id][target_id] = {"ws": guest["ws"], "peer_id": target_id, "name": guest["name"], "is_host": False}
                        await guest["ws"].send_str(json.dumps({"type": "joined", "peer_id": target_id, "room_id": room_id, "is_host": False, "members": room_members(room_id, exclude=target_id)}))
                        await broadcast(room_id, {"type": "peer_joined", "peer_id": target_id, "name": guest["name"], "timestamp": datetime.now(timezone.utc).isoformat()}, exclude=target_id)
                    else:
                        await guest["ws"].send_str(json.dumps({"type": "denied", "message": "The host did not admit you."}))

                elif kind == "end_meeting":
                    if rooms.get(room_id, {}).get(peer_id, {}).get("is_host"):
                        await broadcast(room_id, {"type": "meeting_ended", "message": "The host has ended the meeting."})

                elif kind == "offer":
                    await send_to(room_id, data.get("target"), {"type": "offer", "sdp": data["sdp"], "from": peer_id, "name": peer_name})

                elif kind == "answer":
                    await send_to(room_id, data.get("target"), {"type": "answer", "sdp": data["sdp"], "from": peer_id})

                elif kind == "ice-candidate":
                    await send_to(room_id, data.get("target"), {"type": "ice-candidate", "candidate": data["candidate"], "from": peer_id})

                elif kind == "chat":
                    await broadcast(room_id, {"type": "chat", "from": peer_id, "name": peer_name, "message": data.get("message", ""), "timestamp": datetime.now(timezone.utc).isoformat()})

                elif kind == "reaction":
                    await broadcast(room_id, {"type": "reaction", "from": peer_id, "name": peer_name, "emoji": data.get("emoji", "👍")})

                elif kind == "media-state":
                    await broadcast(room_id, {"type": "media-state", "from": peer_id, "audio": data.get("audio"), "video": data.get("video"), "screen": data.get("screen")}, exclude=peer_id)

                elif kind == "raise-hand":
                    await broadcast(room_id, {"type": "raise-hand", "from": peer_id, "name": peer_name, "raised": data.get("raised", True)})

            elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                break

    except Exception as e:
        log.error(f"Error for {peer_id}: {e}")
    finally:
        if room_id:
            rooms[room_id].pop(peer_id, None)
            waiting_room[room_id] = [g for g in waiting_room.get(room_id, []) if g["peer_id"] != peer_id]
            await broadcast(room_id, {"type": "peer_left", "peer_id": peer_id, "name": peer_name, "is_host": is_host})
            if not rooms[room_id]:
                del rooms[room_id]
                waiting_room.pop(room_id, None)
        log.info(f"{peer_name} ({peer_id}) disconnected")

    return ws


async def main():
    app = web.Application()
    app.router.add_get("/", health)       # health check GET
    app.router.add_route("HEAD", "/", health)  # health check HEAD
    app.router.add_get("/ws", ws_handler) # WebSocket endpoint

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info(f"🚀 Server running on port {PORT}  (HTTP health: /  |  WebSocket: /ws)")
    await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())