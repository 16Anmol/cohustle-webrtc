"""
WebRTC Signaling Server — aiohttp + WebSockets
Supports: host/participant roles, admit/deny, end-meeting, chat, reactions, screen share.
Fixed for Render: handles HEAD/GET health checks alongside WebSocket connections.
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

# rooms[room_id] = { peer_id: { ws, peer_id, name, is_host } }
rooms: dict = defaultdict(dict)
# waiting_room[room_id] = [ { ws, peer_id, name } ]  — guests waiting for host admission
waiting_room: dict = defaultdict(list)


async def broadcast(room_id: str, message: dict, exclude: str = None):
    payload = json.dumps(message)
    for pid, peer in list(rooms[room_id].items()):
        if pid == exclude:
            continue
        try:
            await peer["ws"].send_str(payload)
        except Exception:
            rooms[room_id].pop(pid, None)


async def send_to(room_id: str, peer_id: str, message: dict):
    peer = rooms[room_id].get(peer_id)
    if peer:
        try:
            await peer["ws"].send_str(json.dumps(message))
        except Exception:
            rooms[room_id].pop(peer_id, None)


def room_members(room_id: str, exclude: str = None):
    return [
        {"peer_id": p["peer_id"], "name": p["name"], "is_host": p.get("is_host", False)}
        for pid, p in rooms[room_id].items()
        if pid != exclude
    ]


# ── Health check endpoint (satisfies Render's HEAD / and GET /) ───────────────
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

    log.info(f"New connection: {peer_id}")

    try:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue

                kind = data.get("type")

                # ── JOIN ────────────────────────────────────────────────────────
                if kind == "join":
                    room_id = data.get("room_id", "default")
                    peer_name = data.get("name", f"User-{peer_id}")

                    existing = rooms[room_id]

                    if not existing:
                        # First person — becomes the HOST automatically
                        is_host = True
                        rooms[room_id][peer_id] = {
                            "ws": ws, "peer_id": peer_id,
                            "name": peer_name, "is_host": True,
                        }
                        await ws.send_str(json.dumps({
                            "type": "joined",
                            "peer_id": peer_id,
                            "room_id": room_id,
                            "is_host": True,
                            "members": [],
                        }))
                        log.info(f"{peer_name} ({peer_id}) created room {room_id} as HOST")
                    else:
                        # Subsequent joiners — go to waiting room, host must admit them
                        waiting_room[room_id].append({
                            "ws": ws, "peer_id": peer_id, "name": peer_name
                        })
                        # Notify guest they're waiting
                        await ws.send_str(json.dumps({
                            "type": "waiting",
                            "peer_id": peer_id,
                            "message": "Waiting for the host to admit you...",
                        }))
                        # Notify host someone is waiting
                        host_peer = next((p for p in existing.values() if p.get("is_host")), None)
                        if host_peer:
                            await host_peer["ws"].send_str(json.dumps({
                                "type": "admission_request",
                                "peer_id": peer_id,
                                "name": peer_name,
                            }))
                        log.info(f"{peer_name} ({peer_id}) waiting for admission to room {room_id}")

                # ── HOST ADMITS or DENIES a waiting participant ─────────────────
                elif kind == "admit":
                    target_id = data.get("peer_id")
                    admit = data.get("admit", True)

                    waiting = waiting_room.get(room_id, [])
                    guest = next((g for g in waiting if g["peer_id"] == target_id), None)
                    if not guest:
                        continue

                    waiting_room[room_id] = [g for g in waiting if g["peer_id"] != target_id]

                    if admit:
                        rooms[room_id][target_id] = {
                            "ws": guest["ws"], "peer_id": target_id,
                            "name": guest["name"], "is_host": False,
                        }
                        await guest["ws"].send_str(json.dumps({
                            "type": "joined",
                            "peer_id": target_id,
                            "room_id": room_id,
                            "is_host": False,
                            "members": room_members(room_id, exclude=target_id),
                        }))
                        await broadcast(room_id, {
                            "type": "peer_joined",
                            "peer_id": target_id,
                            "name": guest["name"],
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        }, exclude=target_id)
                        log.info(f"Host admitted {guest['name']} ({target_id}) to room {room_id}")
                    else:
                        await guest["ws"].send_str(json.dumps({
                            "type": "denied",
                            "message": "The host did not admit you to this meeting.",
                        }))
                        log.info(f"Host denied {guest['name']} ({target_id})")

                # ── END MEETING (host only) ──────────────────────────────────────
                elif kind == "end_meeting":
                    if rooms.get(room_id, {}).get(peer_id, {}).get("is_host"):
                        await broadcast(room_id, {
                            "type": "meeting_ended",
                            "message": "The host has ended the meeting.",
                        })
                        for g in waiting_room.get(room_id, []):
                            try:
                                await g["ws"].send_str(json.dumps({"type": "meeting_ended"}))
                            except Exception:
                                pass
                        log.info(f"Host {peer_name} ended room {room_id}")

                # ── OFFER ───────────────────────────────────────────────────────
                elif kind == "offer":
                    target = data.get("target")
                    await send_to(room_id, target, {
                        "type": "offer", "sdp": data["sdp"],
                        "from": peer_id, "name": peer_name,
                    })

                # ── ANSWER ──────────────────────────────────────────────────────
                elif kind == "answer":
                    target = data.get("target")
                    await send_to(room_id, target, {
                        "type": "answer", "sdp": data["sdp"], "from": peer_id,
                    })

                # ── ICE CANDIDATE ───────────────────────────────────────────────
                elif kind == "ice-candidate":
                    target = data.get("target")
                    await send_to(room_id, target, {
                        "type": "ice-candidate",
                        "candidate": data["candidate"], "from": peer_id,
                    })

                # ── CHAT ────────────────────────────────────────────────────────
                elif kind == "chat":
                    await broadcast(room_id, {
                        "type": "chat", "from": peer_id, "name": peer_name,
                        "message": data.get("message", ""),
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    })

                # ── REACTION ────────────────────────────────────────────────────
                elif kind == "reaction":
                    await broadcast(room_id, {
                        "type": "reaction", "from": peer_id,
                        "name": peer_name, "emoji": data.get("emoji", "👍"),
                    })

                # ── MEDIA STATE ──────────────────────────────────────────────────
                elif kind == "media-state":
                    await broadcast(room_id, {
                        "type": "media-state", "from": peer_id,
                        "audio": data.get("audio"),
                        "video": data.get("video"),
                        "screen": data.get("screen"),
                    }, exclude=peer_id)

                # ── RAISE HAND ──────────────────────────────────────────────────
                elif kind == "raise-hand":
                    await broadcast(room_id, {
                        "type": "raise-hand", "from": peer_id,
                        "name": peer_name, "raised": data.get("raised", True),
                    })

            elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                break

    except Exception as e:
        log.error(f"Error for {peer_id}: {e}")
    finally:
        if room_id:
            rooms[room_id].pop(peer_id, None)
            waiting_room[room_id] = [
                g for g in waiting_room.get(room_id, []) if g["peer_id"] != peer_id
            ]
            await broadcast(room_id, {
                "type": "peer_left",
                "peer_id": peer_id, "name": peer_name, "is_host": is_host,
            })
            log.info(f"{peer_name} ({peer_id}) left room {room_id}")
            if not rooms[room_id]:
                del rooms[room_id]
                waiting_room.pop(room_id, None)

    return ws


async def main():
    app = web.Application()
    app.router.add_get("/", health)              # Render health check GET
  
    app.router.add_get("/ws", ws_handler)        # WebSocket signaling endpoint

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info(f"🚀 Signaling server running on port {PORT}  (health: /  |  ws: /ws)")
    await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main()) 