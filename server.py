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
            item.pop("restore", None)
            item["undoable"] = bool(item.get("undoable")) and not bool(item.get("undone"))
            item["restorable"] = bool(item.get("restorable")) and bool(item.get("undone"))
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


def redeem_cost(value: int) -> int:
    return (int(value) * 110 + 99) // 100


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
            ensure_property_shape(prop)
            return prop
    raise ValueError("找不到房产")


def ensure_property_shape(prop: dict) -> None:
    prop.setdefault("landValue", prop.get("assetValue", 0))
    prop.setdefault("buildings", [])
    prop.setdefault("mortgaged", False)
    if prop.get("mortgaged"):
        prop.setdefault("mortgageValue", prop.get("assetValue", 0))


def green_house_count(prop: dict) -> int:
    ensure_property_shape(prop)
    return sum(1 for building in prop["buildings"] if building.get("type") == "house")


def hotel_building(prop: dict) -> dict | None:
    ensure_property_shape(prop)
    for building in prop["buildings"]:
        if building.get("type") == "hotel":
            return building
    return None


def has_buildings(prop: dict) -> bool:
    ensure_property_shape(prop)
    return bool(prop["buildings"])


def building_count_for_bonus(prop: dict) -> int:
    ensure_property_shape(prop)
    if hotel_building(prop):
        return 5
    return green_house_count(prop)


def restore_property_in_place(prop: dict, old_prop: dict) -> None:
    prop.clear()
    prop.update(clone(old_prop))
    ensure_property_shape(prop)


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


def mark_rent_event_restored(state: dict, log_id: str) -> None:
    for event in state.setdefault("rentEvents", []):
        if event.get("logId") == log_id:
            event["undone"] = False
            return


def restore_property(player: dict, prop: dict) -> None:
    for index, item in enumerate(player["properties"]):
        if item["id"] == prop["id"]:
            player["properties"][index] = clone(prop)
            return
    player["properties"].append(clone(prop))


def capture_restore(state: dict, entry: dict, undo: dict) -> dict:
    undo_type = undo.get("type")

    if undo_type == "renamePlayer":
        player = find_player(state, undo["playerId"])
        return {
            "type": "renamePlayer",
            "playerId": player["id"],
            "name": player["name"],
            "color": player.get("color", DEFAULT_PLAYER_COLORS[0]),
        }

    if undo_type == "addProperty":
        player = find_player(state, undo["playerId"])
        prop = find_property(player, undo["propertyId"])
        return {"type": "addProperty", "playerId": player["id"], "property": clone(prop), "cost": undo["cost"]}

    if undo_type == "updateProperty":
        player = find_player(state, undo["playerId"])
        prop = find_property(player, undo["propertyId"])
        return {
            "type": "updateProperty",
            "playerId": player["id"],
            "propertyId": prop["id"],
            "name": prop["name"],
            "toll": prop["toll"],
            "colorSetAmount": prop.get("colorSetAmount", 0),
        }

    if undo_type in ("upgradeProperty", "mortgageProperty", "redeemProperty", "sellBuilding"):
        player = find_player(state, undo["playerId"])
        prop = find_property(player, undo["propertyId"])
        return {
            "type": undo_type,
            "playerId": player["id"],
            "propertyId": prop["id"],
            "property": clone(prop),
            "cost": undo.get("cost"),
            "amount": undo.get("amount"),
            "income": undo.get("income"),
        }

    if undo_type == "collectRent":
        return {
            "type": "collectRent",
            "receiverId": undo["receiverId"],
            "payerId": undo["payerId"],
            "amount": undo["amount"],
        }

    if undo_type == "adjustCash":
        return {"type": "adjustCash", "playerId": undo["playerId"], "delta": undo["delta"]}

    if undo_type == "passStart":
        return {"type": "passStart", "playerId": undo["playerId"], "amount": undo["amount"]}

    if undo_type == "deleteProperty":
        return {
            "type": "deleteProperty",
            "playerId": undo["playerId"],
            "propertyId": undo["property"]["id"],
        }

    raise ValueError("未知撤回类型")


def apply_undo(state: dict, entry: dict) -> None:
    undo = entry.get("undo")
    if not undo or entry.get("undone"):
        raise ValueError("这条记录不能撤回")

    undo_type = undo.get("type")
    entry["restore"] = capture_restore(state, entry, undo)
    entry["restorable"] = True

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
        prop["colorSetAmount"] = undo.get("colorSetAmount", prop.get("colorSetAmount", 0))

    elif undo_type == "upgradeProperty":
        player = find_player(state, undo["playerId"])
        prop = find_property(player, undo["propertyId"])
        player["cash"] += undo["cost"]
        restore_property_in_place(prop, undo["property"])

    elif undo_type == "mortgageProperty":
        player = find_player(state, undo["playerId"])
        prop = find_property(player, undo["propertyId"])
        player["cash"] -= undo["amount"]
        restore_property_in_place(prop, undo["property"])

    elif undo_type == "redeemProperty":
        player = find_player(state, undo["playerId"])
        prop = find_property(player, undo["propertyId"])
        player["cash"] += undo["cost"]
        restore_property_in_place(prop, undo["property"])

    elif undo_type == "sellBuilding":
        player = find_player(state, undo["playerId"])
        prop = find_property(player, undo["propertyId"])
        player["cash"] -= undo["income"]
        restore_property_in_place(prop, undo["property"])

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


def apply_restore(state: dict, entry: dict) -> None:
    restore = entry.get("restore")
    if not restore or not entry.get("undone"):
        raise ValueError("这条记录不能恢复")

    restore_type = restore.get("type")

    if restore_type == "renamePlayer":
        player = find_player(state, restore["playerId"])
        player["name"] = restore["name"]
        player["color"] = restore["color"]

    elif restore_type == "addProperty":
        player = find_player(state, restore["playerId"])
        player["cash"] -= restore["cost"]
        restore_property(player, restore["property"])

    elif restore_type == "updateProperty":
        player = find_player(state, restore["playerId"])
        prop = find_property(player, restore["propertyId"])
        prop["name"] = restore["name"]
        prop["toll"] = restore["toll"]
        prop["colorSetAmount"] = restore.get("colorSetAmount", prop.get("colorSetAmount", 0))

    elif restore_type == "upgradeProperty":
        player = find_player(state, restore["playerId"])
        prop = find_property(player, restore["propertyId"])
        player["cash"] -= restore["cost"]
        restore_property_in_place(prop, restore["property"])

    elif restore_type == "mortgageProperty":
        player = find_player(state, restore["playerId"])
        prop = find_property(player, restore["propertyId"])
        player["cash"] += restore["amount"]
        restore_property_in_place(prop, restore["property"])

    elif restore_type == "redeemProperty":
        player = find_player(state, restore["playerId"])
        prop = find_property(player, restore["propertyId"])
        player["cash"] -= restore["cost"]
        restore_property_in_place(prop, restore["property"])

    elif restore_type == "sellBuilding":
        player = find_player(state, restore["playerId"])
        prop = find_property(player, restore["propertyId"])
        player["cash"] += restore["income"]
        restore_property_in_place(prop, restore["property"])

    elif restore_type == "collectRent":
        receiver = find_player(state, restore["receiverId"])
        payer = find_player(state, restore["payerId"])
        receiver["cash"] += restore["amount"]
        payer["cash"] -= restore["amount"]
        mark_rent_event_restored(state, entry["id"])

    elif restore_type == "adjustCash":
        player = find_player(state, restore["playerId"])
        player["cash"] -= restore["delta"]

    elif restore_type == "passStart":
        player = find_player(state, restore["playerId"])
        player["cash"] += restore["amount"]

    elif restore_type == "deleteProperty":
        player = find_player(state, restore["playerId"])
        player["properties"] = [prop for prop in player["properties"] if prop["id"] != restore["propertyId"]]

    else:
        raise ValueError("未知恢复类型")

    entry["undone"] = False
    entry["restorable"] = False
    entry.pop("restore", None)


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
                raise ValueError("土地名称不能为空")
            cost = money(payload.get("cost"), "购买金额")
            toll = money(payload.get("toll"), "过路费")
            player["cash"] -= cost
            prop = {
                "id": make_id("h_", 8),
                "name": name,
                "toll": toll,
                "colorSetAmount": 0,
                "landValue": cost,
                "assetValue": cost,
                "buildings": [],
                "mortgaged": False,
                "createdAt": datetime.now().isoformat(timespec="seconds"),
            }
            player["properties"].append(prop)
            append_log(
                state,
                f"{player['name']} 买地 {name}，花费 {cost}，过路费 {toll}",
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
                raise ValueError("土地名称不能为空")
            toll = money(payload.get("toll"), "过路费")
            old_name = prop["name"]
            old_toll = prop["toll"]
            old_color_set_amount = prop.get("colorSetAmount", 0)
            prop["name"] = name
            prop["toll"] = toll
            prop["colorSetAmount"] = money(payload.get("colorSetAmount", old_color_set_amount), "同色集齐金额")
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
                    "colorSetAmount": old_color_set_amount,
                },
            )

        elif action_type == "upgradeProperty":
            player = find_player(state, payload.get("playerId"))
            require_actor(state, payload, player["id"])
            prop = find_property(player, payload.get("propertyId"))
            if prop.get("mortgaged"):
                raise ValueError("土地已抵押，不能建房子")
            if hotel_building(prop):
                raise ValueError("已有红色房子，不能继续建房子")
            cost = money(payload.get("cost"), "建房子金额", allow_zero=False)
            toll = payload.get("toll")
            old_prop = clone(prop)
            if toll not in (None, ""):
                prop["toll"] = money(toll, "建房子后过路费")
            player["cash"] -= cost
            prop["assetValue"] += cost
            houses = [building for building in prop["buildings"] if building.get("type") == "house"]
            if len(houses) >= 4:
                prop["buildings"] = [
                    {
                        "id": make_id("b_", 8),
                        "type": "hotel",
                        "cost": cost,
                        "houseCosts": [house.get("cost", 0) for house in houses[:4]],
                    }
                ]
                build_text = "红色房子"
            else:
                prop["buildings"].append({"id": make_id("b_", 8), "type": "house", "cost": cost})
                build_text = "绿色房子"
            append_log(
                state,
                f"{player['name']} 给 {prop['name']} 建{build_text}，花费 {cost}",
                action_type=action_type,
                actor_player_id=player["id"],
                undo={
                    "type": "upgradeProperty",
                    "playerId": player["id"],
                    "propertyId": prop["id"],
                    "cost": cost,
                    "property": old_prop,
                },
            )

        elif action_type == "mortgageProperty":
            player = find_player(state, payload.get("playerId"))
            require_actor(state, payload, player["id"])
            prop = find_property(player, payload.get("propertyId"))
            if prop.get("mortgaged"):
                raise ValueError("土地已经抵押")
            if has_buildings(prop):
                raise ValueError("土地上有房子，请先卖房")
            amount = money(payload.get("amount"), "抵押价值")
            old_prop = clone(prop)
            player["cash"] += amount
            prop["assetValue"] = amount
            prop["mortgaged"] = True
            prop["mortgageValue"] = amount
            prop["preMortgageAssetValue"] = old_prop.get("assetValue", amount)
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

        elif action_type == "redeemProperty":
            player = find_player(state, payload.get("playerId"))
            require_actor(state, payload, player["id"])
            prop = find_property(player, payload.get("propertyId"))
            if not prop.get("mortgaged"):
                raise ValueError("土地未抵押")
            mortgage_value = money(prop.get("mortgageValue", prop.get("assetValue", 0)), "抵押价值")
            cost = redeem_cost(mortgage_value)
            old_prop = clone(prop)
            player["cash"] -= cost
            prop["mortgaged"] = False
            prop["assetValue"] = prop.get("preMortgageAssetValue", prop.get("landValue", mortgage_value))
            prop.pop("preMortgageAssetValue", None)
            append_log(
                state,
                f"{player['name']} 赎回 {prop['name']}，花费 {cost}",
                action_type=action_type,
                actor_player_id=player["id"],
                undo={
                    "type": "redeemProperty",
                    "playerId": player["id"],
                    "propertyId": prop["id"],
                    "cost": cost,
                    "property": old_prop,
                },
            )

        elif action_type == "sellBuilding":
            player = find_player(state, payload.get("playerId"))
            require_actor(state, payload, player["id"])
            prop = find_property(player, payload.get("propertyId"))
            if not has_buildings(prop):
                raise ValueError("该土地没有可卖的房子")
            old_prop = clone(prop)
            hotel = hotel_building(prop)
            if hotel:
                sold_cost = money(hotel.get("cost", 0), "红色房子金额")
                income = sold_cost // 2
                prop["assetValue"] -= sold_cost
                prop["buildings"] = [
                    {"id": make_id("b_", 8), "type": "house", "cost": cost}
                    for cost in hotel.get("houseCosts", [])[:4]
                ]
                text = f"{player['name']} 卖出 {prop['name']} 的红色房子，获得 {income}"
            else:
                building = prop["buildings"].pop()
                sold_cost = money(building.get("cost", 0), "绿色房子金额")
                income = sold_cost // 2
                prop["assetValue"] -= sold_cost
                text = f"{player['name']} 卖出 {prop['name']} 的绿色房子，获得 {income}"
            toll = payload.get("toll")
            if toll not in (None, ""):
                prop["toll"] = money(toll, "卖房后过路费")
            prop["assetValue"] = max(prop.get("landValue", 0), prop["assetValue"])
            player["cash"] += income
            append_log(
                state,
                text,
                action_type=action_type,
                actor_player_id=player["id"],
                undo={
                    "type": "sellBuilding",
                    "playerId": player["id"],
                    "propertyId": prop["id"],
                    "income": income,
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
            if prop.get("mortgaged"):
                raise ValueError("土地已抵押，不能收过路费")
            amount = money(payload.get("amount", prop["toll"]), "收款金额")
            color_set_amount = money(prop.get("colorSetAmount", 0), "同色集齐金额")
            color_set_extra = color_set_amount * building_count_for_bonus(prop)
            amount += color_set_extra
            payer["cash"] -= amount
            receiver["cash"] += amount
            extra_text = f"，同色集齐加收 {color_set_extra}" if color_set_extra else ""
            append_log(
                state,
                f"{receiver['name']} 向 {payer['name']} 收取 {prop['name']} 过路费 {amount}{extra_text}",
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

        elif action_type == "restoreLog":
            actor_player = find_player(state, payload.get("actorPlayerId"))
            require_actor(state, payload, actor_player["id"])
            entry = find_log(state, payload.get("logId"))
            if entry.get("actorPlayerId") != actor_player["id"]:
                raise ValueError("只能恢复自己的操作")
            original_text = entry["text"]
            apply_restore(state, entry)
            append_log(
                state,
                f"{actor_player['name']} 恢复：{original_text}",
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
