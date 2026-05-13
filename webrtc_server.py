"""
WebRTC Signaling Server — Python + WebSockets
Handles Render health checks (HEAD/GET /) alongside WebSocket connections.
"""

import asyncio
import json
import logging
import os
import uuid
from collections import defaultdict
from datetime import datetime, timezone

import websockets
from websockets.exceptions import ConnectionClosedOK, ConnectionClosedError
from websockets.legacy.server import HTTPResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

PORT = int(os.environ.get("PORT", 8765))

rooms: dict[str, dict] = defaultdict(dict)
waiting_room: dict[str, list] = defaultdict(list)


async def health_check(path, request_headers):
    """
    Intercept HTTP requests before WebSocket handshake.
    Render sends HEAD / for health checks — respond with 200 OK.
    Any actual WebSocket upgrade (GET with Upgrade: websocket) passes through normally.
    """
    method = request_headers.get("method", "GET")
    upgrade = request_headers.get("Upgrade", "")

    # Respond to health checks (HEAD or GET without WebSocket upgrade)
    if path == "/" and upgrade.lower() != "websocket":
        return HTTPResponse(200, "OK", {"Content-Type": "text/plain"}, b"OK\n")

    # Let WebSocket connections through
    return None


async def broadcast(room_id: str, message: dict, exclude: str = None):
    payload = json.dumps(message)
    for pid, peer in list(rooms[room_id].items()):
        if pid == exclude:
            continue
        try:
            await peer["ws"].send(payload)
        except Exception:
            rooms[room_id].pop(pid, None)


async def send_to(room_id: str, peer_id: str, message: dict):
    peer = rooms[room_id].get(peer_id)
    if peer:
        try:
            await peer["ws"].send(json.dumps(message))
        except Exception:
            rooms[room_id].pop(peer_id, None)


def room_members(room_id: str, exclude: str = None):
    return [
        {"peer_id": p["peer_id"], "name": p["name"], "is_host": p.get("is_host", False)}
        for pid, p in rooms[room_id].items()
        if pid != exclude
    ]


async def handle(ws, path="/"):
    peer_id = str(uuid.uuid4())[:8]
    room_id = None
    peer_name = "Anonymous"
    is_host = False

    log.info(f"New WS connection: {peer_id}")

    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            kind = msg.get("type")

            if kind == "join":
                room_id = msg.get("room_id", "default")
                peer_name = msg.get("name", f"User-{peer_id}")
                existing = rooms[room_id]

                if not existing:
                    is_host = True
                    rooms[room_id][peer_id] = {"ws": ws, "peer_id": peer_id, "name": peer_name, "is_host": True}
                    await ws.send(json.dumps({"type": "joined", "peer_id": peer_id, "room_id": room_id, "is_host": True, "members": []}))
                    log.info(f"{peer_name} created room {room_id} as HOST")
                else:
                    waiting_room[room_id].append({"ws": ws, "peer_id": peer_id, "name": peer_name})
                    await ws.send(json.dumps({"type": "waiting", "peer_id": peer_id, "message": "Waiting for the host to admit you..."}))
                    host_peer = next((p for p in existing.values() if p.get("is_host")), None)
                    if host_peer:
                        await host_peer["ws"].send(json.dumps({"type": "admission_request", "peer_id": peer_id, "name": peer_name}))

            elif kind == "admit":
                target_id = msg.get("peer_id")
                admit = msg.get("admit", True)
                waiting = waiting_room.get(room_id, [])
                guest = next((g for g in waiting if g["peer_id"] == target_id), None)
                if not guest:
                    continue
                waiting_room[room_id] = [g for g in waiting if g["peer_id"] != target_id]
                if admit:
                    rooms[room_id][target_id] = {"ws": guest["ws"], "peer_id": target_id, "name": guest["name"], "is_host": False}
                    await guest["ws"].send(json.dumps({"type": "joined", "peer_id": target_id, "room_id": room_id, "is_host": False, "members": room_members(room_id, exclude=target_id)}))
                    await broadcast(room_id, {"type": "peer_joined", "peer_id": target_id, "name": guest["name"], "timestamp": datetime.now(timezone.utc).isoformat()}, exclude=target_id)
                else:
                    await guest["ws"].send(json.dumps({"type": "denied", "message": "The host did not admit you."}))

            elif kind == "end_meeting":
                if rooms.get(room_id, {}).get(peer_id, {}).get("is_host"):
                    await broadcast(room_id, {"type": "meeting_ended", "message": "The host has ended the meeting."})

            elif kind == "offer":
                await send_to(room_id, msg.get("target"), {"type": "offer", "sdp": msg["sdp"], "from": peer_id, "name": peer_name})

            elif kind == "answer":
                await send_to(room_id, msg.get("target"), {"type": "answer", "sdp": msg["sdp"], "from": peer_id})

            elif kind == "ice-candidate":
                await send_to(room_id, msg.get("target"), {"type": "ice-candidate", "candidate": msg["candidate"], "from": peer_id})

            elif kind == "chat":
                await broadcast(room_id, {"type": "chat", "from": peer_id, "name": peer_name, "message": msg.get("message", ""), "timestamp": datetime.now(timezone.utc).isoformat()})

            elif kind == "reaction":
                await broadcast(room_id, {"type": "reaction", "from": peer_id, "name": peer_name, "emoji": msg.get("emoji", "👍")})

            elif kind == "media-state":
                await broadcast(room_id, {"type": "media-state", "from": peer_id, "audio": msg.get("audio"), "video": msg.get("video"), "screen": msg.get("screen")}, exclude=peer_id)

            elif kind == "raise-hand":
                await broadcast(room_id, {"type": "raise-hand", "from": peer_id, "name": peer_name, "raised": msg.get("raised", True)})

    except (ConnectionClosedOK, ConnectionClosedError):
        pass
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


async def main():
    log.info(f"🚀 Signaling server starting on port {PORT}")
    async with websockets.serve(
        handle,
        "0.0.0.0",
        PORT,
        process_request=health_check,
    ):
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
