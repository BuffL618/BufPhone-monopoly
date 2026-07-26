from __future__ import annotations

import json
import os
import random
import socket
import threading
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse


ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "8000"))

rooms: dict[str, "Room"] = {}
rooms_lock = threading.Lock()

DEFAULT_PLAYER_COLORS = [
    "#8B1E2D",
    "#1D4E89",
    "#9A6A00",
    "#1F6F50",
    "#F4A3A3",
    "#9DC8F6",
    "#F4D76B",
    "#9BD8B2",
]


class Room:
    def __init__(self, room_id: str):
        self.cond = threading.Condition()
        self.state = {
            "id": room_id,
            "started": False,
            "initialCash": 0,
            "passStartAmount": 0,
            "players": [],
            "log": [],
            "rentEvents": [],
            "version": 0,
            "createdAt": datetime.now().isoformat(timespec="seconds"),
        }

    def snapshot(self) -> dict:
        snapshot = json.loads(json.dumps(self.state, ensure_ascii=False))
        for player in snapshot["players"]:
            player["claimed"] = bool(player.pop("clientToken", None))
        for item in snapshot["log"]:
            item.pop("undo", None)
            item["undoable"] = bool(item.get("undoable")) and not bool(item.get("undone"))
        return snapshot

    def mutate(self, fn):
        with self.cond:
            fn(self.state)
            self.state["version"] += 1
            self.cond.notify_all()
            return self.snapshot()


def make_id(prefix: str = "", length: int = 6) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return prefix + "".join(random.choice(alphabet) for _ in range(length))


def get_lan_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"
    finally:
        sock.close()


LAN_IP = get_lan_ip()


def now_label() -> str:
    return datetime.now().strftime("%H:%M")


def clone(value):
    return json.loads(json.dumps(value, ensure_ascii=False))


def append_log(
    state: dict,
    text: str,
    *,
    action_type: str = "",
    actor_player_id: str = "",
    undo: dict | None = None,
    rent_event: dict | None = None,
) -> dict:
    log_id = make_id("l_", 10)
    entry = {
        "id": log_id,
        "time": now_label(),
        "text": text,
        "actionType": action_type,
        "actorPlayerId": actor_player_id,
        "undo": undo,
        "undoable": bool(undo),
        "undone": False,
    }
    state["log"].insert(0, entry)
    del state["log"][80:]
    if rent_event:
        rent_event["logId"] = log_id
        rent_event["undone"] = False
        state.setdefault("rentEvents", []).append(rent_event)
    return entry


def money(value, field: str, allow_zero: bool = True) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} 必须是整数")
    if parsed < 0 or (parsed == 0 and not allow_zero):
        raise ValueError(f"{field} 必须大于 0" if not allow_zero else f"{field} 不能为负数")
    return parsed


def normalize_color(value) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("颜色编号不能为空")
    if raw.startswith("#"):
        raw = raw[1:]
    if len(raw) == 6 and all(char in "0123456789abcdefABCDEF" for char in raw):
        return f"#{raw.upper()}"

    cleaned = raw.lower().replace("rgb", "").replace("rmg", "").replace("(", "").replace(")", "")
    parts = [part.strip() for part in cleaned.replace("，", ",").split(",") if part.strip()]
    if len(parts) == 3:
        try:
            values = [int(part) for part in parts]
        except ValueError:
            values = []
        if len(values) == 3 and all(0 <= part <= 255 for part in values):
            return "#" + "".join(f"{part:02X}" for part in values)

    raise ValueError("颜色编号请填写 #RRGGBB 或 255,0,0")


def find_player(state: dict, player_id: str) -> dict:
    for player in state["players"]:
        if player["id"] == player_id:
            return player
    raise ValueError("找不到玩家")


def find_property(player: dict, property_id: str) -> dict:
    for prop in player["properties"]:
        if prop["id"] == property_id:
            return prop
    raise ValueError("找不到房产")


def find_log(state: dict, log_id: str) -> dict:
    for item in state["log"]:
        if item.get("id") == log_id:
            return item
    raise ValueError("找不到操作记录")


def require_actor(state: dict, payload: dict, target_player_id: str) -> None:
    actor_id = payload.get("actorPlayerId")
    actor_token = payload.get("actorToken")
    if actor_id != target_player_id:
        raise ValueError("这台手机只能操作自己的玩家")
    player = find_player(state, target_player_id)
    if not actor_token or actor_token != player.get("clientToken"):
        raise ValueError("请先在本机选择自己的玩家")


def claim_player(room: Room, payload: dict) -> dict:
    player_id = payload.get("playerId")
    token = str(payload.get("token", "")).strip()

    with room.cond:
        state = room.state
        if not state["started"]:
            raise ValueError("请先完成开局设置")
        player = find_player(state, player_id)
        current_token = player.get("clientToken")
        if current_token:
            if token == current_token:
                return {"room": room.snapshot(), "playerId": player["id"], "token": current_token}
            raise ValueError("这个玩家已经被其他手机绑定")

        new_token = make_id("t_", 24)
        player["clientToken"] = new_token
        state["version"] += 1
        room.cond.notify_all()
        return {"room": room.snapshot(), "playerId": player["id"], "token": new_token}


def release_player(room: Room, payload: dict) -> dict:
    player_id = payload.get("playerId")
    token = str(payload.get("token", "")).strip()

    with room.cond:
        state = room.state
        if not state["started"]:
            raise ValueError("请先完成开局设置")
        player = find_player(state, player_id)
        if token != player.get("clientToken"):
            raise ValueError("只能解除本机绑定的玩家")
        player.pop("clientToken", None)
        state["version"] += 1
        room.cond.notify_all()
        return {"room": room.snapshot()}


def mark_rent_event_undone(state: dict, log_id: str) -> None:
    for event in state.setdefault("rentEvents", []):
        if event.get("logId") == log_id:
            event["undone"] = True
            return


def restore_property(player: dict, prop: dict) -> None:
    for index, item in enumerate(player["properties"]):
        if item["id"] == prop["id"]:
            player["properties"][index] = clone(prop)
            return
    player["properties"].append(clone(prop))


def apply_undo(state: dict, entry: dict) -> None:
    undo = entry.get("undo")
    if not undo or entry.get("undone"):
        raise ValueError("这条记录不能撤回")

    undo_type = undo.get("type")

    if undo_type == "renamePlayer":
        player = find_player(state, undo["playerId"])
        player["name"] = undo["name"]
        player["color"] = undo["color"]

    elif undo_type == "addProperty":
        player = find_player(state, undo["playerId"])
        player["cash"] += undo["cost"]
        player["properties"] = [prop for prop in player["properties"] if prop["id"] != undo["propertyId"]]

    elif undo_type == "updateProperty":
        player = find_player(state, undo["playerId"])
        prop = find_property(player, undo["propertyId"])
        prop["name"] = undo["name"]
        prop["toll"] = undo["toll"]

    elif undo_type == "upgradeProperty":
        player = find_player(state, undo["playerId"])
        prop = find_property(player, undo["propertyId"])
        player["cash"] += undo["cost"]
        prop["assetValue"] -= undo["cost"]
        prop["toll"] = undo["toll"]

    elif undo_type == "mortgageProperty":
        player = find_player(state, undo["playerId"])
        prop = find_property(player, undo["propertyId"])
        player["cash"] -= undo["amount"]
        prop.clear()
        prop.update(clone(undo["property"]))

    elif undo_type == "collectRent":
        receiver = find_player(state, undo["receiverId"])
        payer = find_player(state, undo["payerId"])
        receiver["cash"] -= undo["amount"]
        payer["cash"] += undo["amount"]
        mark_rent_event_undone(state, entry["id"])

    elif undo_type == "adjustCash":
        player = find_player(state, undo["playerId"])
        player["cash"] += undo["delta"]

    elif undo_type == "passStart":
        player = find_player(state, undo["playerId"])
        player["cash"] -= undo["amount"]

    elif undo_type == "deleteProperty":
        player = find_player(state, undo["playerId"])
        restore_property(player, undo["property"])

    else:
        raise ValueError("未知撤回类型")

    entry["undone"] = True


def apply_action(room: Room, payload: dict) -> dict:
    action_type = payload.get("type")

    def change(state: dict) -> None:
        if action_type == "setup":
            if state["started"]:
                raise ValueError("本局已经开始")
            initial_cash = money(payload.get("initialCash"), "初始资金")
            pass_start_amount = money(payload.get("passStartAmount", 0), "经过起点金额")
            names = [str(name).strip() for name in payload.get("players", []) if str(name).strip()]
            if not 1 <= len(names) <= 8:
                raise ValueError("玩家人数必须是 1 到 8 人")
            state["initialCash"] = initial_cash
            state["passStartAmount"] = pass_start_amount
            state["players"] = []
            for index, name in enumerate(names):
                state["players"].append(
                    {
                        "id": make_id("p_", 8),
                        "name": name,
                        "color": DEFAULT_PLAYER_COLORS[index],
                        "cash": initial_cash,
                        "properties": [],
                    }
                )
            state["started"] = True
            append_log(state, f"开局：{len(names)} 名玩家，初始资金 {initial_cash}，经过起点 {pass_start_amount}")
            return

        if not state["started"]:
            raise ValueError("请先完成开局设置")

        if action_type == "renamePlayer":
            player = find_player(state, payload.get("playerId"))
            require_actor(state, payload, player["id"])
            name = str(payload.get("name", "")).strip()
            if not name:
                raise ValueError("玩家名称不能为空")
            old_name = player["name"]
            old_color = player.get("color", DEFAULT_PLAYER_COLORS[0])
            color = normalize_color(payload.get("color", old_color))
            player["name"] = name
            player["color"] = color
            append_log(
                state,
                f"{old_name} 修改资料为 {name}",
                action_type=action_type,
                actor_player_id=player["id"],
                undo={"type": "renamePlayer", "playerId": player["id"], "name": old_name, "color": old_color},
            )

        elif action_type == "addProperty":
            player = find_player(state, payload.get("playerId"))
            require_actor(state, payload, player["id"])
            name = str(payload.get("name", "")).strip()
            if not name:
                raise ValueError("房产名称不能为空")
            cost = money(payload.get("cost"), "购买金额")
            toll = money(payload.get("toll"), "过路费")
            player["cash"] -= cost
            prop = {
                "id": make_id("h_", 8),
                "name": name,
                "toll": toll,
                "assetValue": cost,
                "mortgaged": False,
                "createdAt": datetime.now().isoformat(timespec="seconds"),
            }
            player["properties"].append(prop)
            append_log(
                state,
                f"{player['name']} 购买 {name}，花费 {cost}，过路费 {toll}",
                action_type=action_type,
                actor_player_id=player["id"],
                undo={"type": "addProperty", "playerId": player["id"], "propertyId": prop["id"], "cost": cost},
            )

        elif action_type == "updateProperty":
            player = find_player(state, payload.get("playerId"))
            require_actor(state, payload, player["id"])
            prop = find_property(player, payload.get("propertyId"))
            name = str(payload.get("name", "")).strip()
            if not name:
                raise ValueError("房产名称不能为空")
            toll = money(payload.get("toll"), "过路费")
            old_name = prop["name"]
            old_toll = prop["toll"]
            prop["name"] = name
            prop["toll"] = toll
            append_log(
                state,
                f"{player['name']} 修改 {name}，过路费 {toll}",
                action_type=action_type,
                actor_player_id=player["id"],
                undo={
                    "type": "updateProperty",
                    "playerId": player["id"],
                    "propertyId": prop["id"],
                    "name": old_name,
                    "toll": old_toll,
                },
            )

        elif action_type == "upgradeProperty":
            player = find_player(state, payload.get("playerId"))
            require_actor(state, payload, player["id"])
            prop = find_property(player, payload.get("propertyId"))
            cost = money(payload.get("cost"), "升级金额", allow_zero=False)
            toll = payload.get("toll")
            old_toll = prop["toll"]
            if toll not in (None, ""):
                prop["toll"] = money(toll, "新过路费")
            player["cash"] -= cost
            prop["assetValue"] += cost
            append_log(
                state,
                f"{player['name']} 升级 {prop['name']}，花费 {cost}",
                action_type=action_type,
                actor_player_id=player["id"],
                undo={
                    "type": "upgradeProperty",
                    "playerId": player["id"],
                    "propertyId": prop["id"],
                    "cost": cost,
                    "toll": old_toll,
                },
            )

        elif action_type == "mortgageProperty":
            player = find_player(state, payload.get("playerId"))
            require_actor(state, payload, player["id"])
            prop = find_property(player, payload.get("propertyId"))
            amount = money(payload.get("amount"), "抵押价值")
            old_prop = clone(prop)
            player["cash"] += amount
            prop["assetValue"] = amount
            prop["mortgaged"] = True
            prop["mortgageValue"] = amount
            append_log(
                state,
                f"{player['name']} 抵押 {prop['name']}，获得 {amount}",
                action_type=action_type,
                actor_player_id=player["id"],
                undo={
                    "type": "mortgageProperty",
                    "playerId": player["id"],
                    "propertyId": prop["id"],
                    "amount": amount,
                    "property": old_prop,
                },
            )

        elif action_type == "collectRent":
            receiver = find_player(state, payload.get("receiverId"))
            require_actor(state, payload, receiver["id"])
            payer = find_player(state, payload.get("payerId"))
            if receiver["id"] == payer["id"]:
                raise ValueError("收款人和付款人不能相同")
            prop = find_property(receiver, payload.get("propertyId"))
            amount = money(payload.get("amount", prop["toll"]), "收款金额")
            payer["cash"] -= amount
            receiver["cash"] += amount
            append_log(
                state,
                f"{receiver['name']} 向 {payer['name']} 收取 {prop['name']} 过路费 {amount}",
                action_type=action_type,
                actor_player_id=receiver["id"],
                undo={
                    "type": "collectRent",
                    "receiverId": receiver["id"],
                    "payerId": payer["id"],
                    "amount": amount,
                },
                rent_event={
                    "propertyId": prop["id"],
                    "propertyName": prop["name"],
                    "ownerId": receiver["id"],
                    "ownerName": receiver["name"],
                    "payerId": payer["id"],
                    "payerName": payer["name"],
                    "amount": amount,
                    "time": now_label(),
                },
            )

        elif action_type == "adjustCash":
            player = find_player(state, payload.get("playerId"))
            require_actor(state, payload, player["id"])
            amount = money(payload.get("amount"), "金额", allow_zero=False)
            direction = payload.get("direction")
            note = str(payload.get("note", "")).strip() or "现金调整"
            if direction == "out":
                player["cash"] -= amount
                delta = amount
                text = f"{player['name']} 支出 {amount}：{note}"
            elif direction == "in":
                player["cash"] += amount
                delta = -amount
                text = f"{player['name']} 收入 {amount}：{note}"
            else:
                raise ValueError("请选择收入或支出")
            append_log(
                state,
                text,
                action_type=action_type,
                actor_player_id=player["id"],
                undo={"type": "adjustCash", "playerId": player["id"], "delta": delta},
            )

        elif action_type == "passStart":
            player = find_player(state, payload.get("playerId"))
            require_actor(state, payload, player["id"])
            amount = money(state.get("passStartAmount", 0), "经过起点金额")
            player["cash"] += amount
            append_log(
                state,
                f"{player['name']} 经过起点，收入 {amount}",
                action_type=action_type,
                actor_player_id=player["id"],
                undo={"type": "passStart", "playerId": player["id"], "amount": amount},
            )

        elif action_type == "deleteProperty":
            player = find_player(state, payload.get("playerId"))
            require_actor(state, payload, player["id"])
            prop = find_property(player, payload.get("propertyId"))
            old_prop = clone(prop)
            player["properties"] = [item for item in player["properties"] if item["id"] != prop["id"]]
            append_log(
                state,
                f"{player['name']} 删除房产 {prop['name']}",
                action_type=action_type,
                actor_player_id=player["id"],
                undo={"type": "deleteProperty", "playerId": player["id"], "property": old_prop},
            )

        elif action_type == "undoLog":
            actor_player = find_player(state, payload.get("actorPlayerId"))
            require_actor(state, payload, actor_player["id"])
            entry = find_log(state, payload.get("logId"))
            if entry.get("actorPlayerId") != actor_player["id"]:
                raise ValueError("只能撤回自己的操作")
            original_text = entry["text"]
            apply_undo(state, entry)
            append_log(
                state,
                f"{actor_player['name']} 撤回：{original_text}",
                action_type=action_type,
                actor_player_id=actor_player["id"],
            )

        else:
            raise ValueError("未知操作")

    return room.mutate(change)


class Handler(BaseHTTPRequestHandler):
    server_version = "MonopolyScorekeeper/1.0"

    def log_message(self, fmt: str, *args) -> None:
        print(f"{self.address_string()} - {fmt % args}")

    def send_json(self, status: int, data: dict) -> None:
        encoded = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8")
        if not raw:
            return {}
        return json.loads(raw)

    def get_room(self, room_id: str) -> Room | None:
        with rooms_lock:
            return rooms.get(room_id)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)

        if path == "/api/config":
            self.send_json(
                HTTPStatus.OK,
                {
                    "lanOrigin": f"http://{LAN_IP}:{PORT}",
                    "localOrigin": f"http://127.0.0.1:{PORT}",
                },
            )
            return

        if path.startswith("/api/rooms/") and path.endswith("/events"):
            room_id = path.split("/")[3]
            room = self.get_room(room_id)
            if not room:
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "房间不存在"})
                return
            self.handle_events(room)
            return

        if path.startswith("/api/rooms/"):
            room_id = path.split("/")[3]
            room = self.get_room(room_id)
            if not room:
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "房间不存在"})
                return
            self.send_json(HTTPStatus.OK, {"room": room.snapshot()})
            return

        if path == "/" or path.startswith("/room/"):
            self.serve_file(STATIC_DIR / "index.html")
            return

        file_path = (STATIC_DIR / path.lstrip("/")).resolve()
        if not str(file_path).startswith(str(STATIC_DIR.resolve())):
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        self.serve_file(file_path)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)

        if path == "/api/rooms":
            room_id = make_id()
            room = Room(room_id)
            with rooms_lock:
                rooms[room_id] = room
            self.send_json(HTTPStatus.CREATED, {"room": room.snapshot()})
            return

        if path.startswith("/api/rooms/") and path.endswith("/claim"):
            room_id = path.split("/")[3]
            room = self.get_room(room_id)
            if not room:
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "房间不存在"})
                return
            try:
                data = claim_player(room, self.read_json())
            except (ValueError, json.JSONDecodeError) as exc:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            self.send_json(HTTPStatus.OK, data)
            return

        if path.startswith("/api/rooms/") and path.endswith("/release"):
            room_id = path.split("/")[3]
            room = self.get_room(room_id)
            if not room:
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "房间不存在"})
                return
            try:
                data = release_player(room, self.read_json())
            except (ValueError, json.JSONDecodeError) as exc:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            self.send_json(HTTPStatus.OK, data)
            return

        if path.startswith("/api/rooms/") and path.endswith("/actions"):
            room_id = path.split("/")[3]
            room = self.get_room(room_id)
            if not room:
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "房间不存在"})
                return
            try:
                state = apply_action(room, self.read_json())
            except (ValueError, json.JSONDecodeError) as exc:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            self.send_json(HTTPStatus.OK, {"room": state})
            return

        self.send_error(HTTPStatus.NOT_FOUND)

    def serve_file(self, file_path: Path) -> None:
        if not file_path.exists() or not file_path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content = file_path.read_bytes()
        mime = "text/plain; charset=utf-8"
        if file_path.suffix == ".html":
            mime = "text/html; charset=utf-8"
        elif file_path.suffix == ".css":
            mime = "text/css; charset=utf-8"
        elif file_path.suffix == ".js":
            mime = "application/javascript; charset=utf-8"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def handle_events(self, room: Room) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        last_version = -1
        try:
            while True:
                with room.cond:
                    if room.state["version"] == last_version:
                        room.cond.wait(timeout=15)
                    snapshot = room.snapshot()
                    changed = snapshot["version"] != last_version
                    last_version = snapshot["version"]
                if changed:
                    data = json.dumps({"room": snapshot}, ensure_ascii=False)
                    self.wfile.write(f"event: state\ndata: {data}\n\n".encode("utf-8"))
                else:
                    self.wfile.write(b": keep-alive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            return


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print("手机大富翁记分器已启动")
    print(f"本机访问：http://127.0.0.1:{PORT}")
    print(f"局域网访问：http://{LAN_IP}:{PORT}")
    print("停止服务：按 Ctrl+C")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
