import asyncio
import json
import os
import shlex
import struct
import time

import websockets


def now_ts():
    return time.time()


class GeneralWebSocketServer:
    # Control packets (binary protocol)
    PKT_FORMAT_SET_REQ = 248   # client->server: <BBB + fmt_ascii> = type, msg_type, fmt_len, fmt
    PKT_FORMAT_ANNOUNCE = 249  # server->all:    <BBB + fmt_ascii> = type, msg_type, fmt_len, fmt

    # Server -> client room/system packets
    PKT_ROOM_INFO = 250   # <BBBB> = type, your_player_no, alive_count, max_players
    PKT_PEER_JOIN = 251   # <BBB>  = type, peer_player_no, alive_count
    PKT_PEER_LEFT = 252   # <BBB>  = type, peer_player_no, alive_count
    PKT_ROOM_FULL = 253   # <BB>   = type, max_players
    PKT_RELAY = 254       # <BBH + payload> = type, from_player_no, payload_len, raw_user_payload

    def __init__(self, host="0.0.0.0", port=10000, enable_cli=True, max_players=2):
        self.host = host
        self.port = port
        self.enable_cli = enable_cli
        self.max_players = max_players
        self.stop_event = asyncio.Event()

        self.client_id_counter = 1
        self.clients = {}  # {websocket: {"id": int, "player_no": int}}
        self.player_slots = {slot: None for slot in range(1, self.max_players + 1)}
        self.message_formats = {}  # {msg_type: struct_format}
        self.system_types = {
            self.PKT_FORMAT_SET_REQ,
            self.PKT_FORMAT_ANNOUNCE,
            self.PKT_ROOM_INFO,
            self.PKT_PEER_JOIN,
            self.PKT_PEER_LEFT,
            self.PKT_ROOM_FULL,
            self.PKT_RELAY,
        }

    @staticmethod
    def _normalize_declared_format(fmt):
        """
        Format here describes only message_data (NOT packet type byte).
        Endianness prefix is preserved if provided.
        """
        if not fmt:
            return None
        prefix = fmt[0] if fmt[0] in "@=<>!" else ""
        body = fmt[1:] if prefix else fmt
        if not body:
            return None
        return prefix + body if prefix else body

    @staticmethod
    def _format_for_calc(fmt):
        # Default to little-endian when no explicit prefix is supplied.
        return fmt if fmt[0] in "@=<>!" else "<" + fmt

    def _next_client_id(self):
        value = self.client_id_counter
        self.client_id_counter += 1
        return value

    def _allocate_player_slot(self):
        for slot, ws in self.player_slots.items():
            if ws is None:
                return slot
        return None

    def _room_state(self):
        alive = len(self.clients)
        return {
            "alive_count": alive,
            "max_players": self.max_players,
            "room_ready": alive == self.max_players,
        }

    def _get_client_view(self):
        rows = []
        for data in self.clients.values():
            rows.append({"client_id": data["id"], "player_no": data["player_no"]})
        return sorted(rows, key=lambda item: item["client_id"])

    def _runtime_snapshot(self):
        return {
            **self._room_state(),
            "clients": self._get_client_view(),
            "formats": {str(k): v for k, v in sorted(self.message_formats.items())},
            "slots": {
                str(slot): (self.clients[ws]["id"] if ws in self.clients else None)
                for slot, ws in self.player_slots.items()
            },
            "server_time": now_ts(),
        }

    async def _send_bytes(self, ws, payload):
        await ws.send(payload)

    async def _broadcast_bytes(self, payload, exclude=None):
        dead = []
        for ws in list(self.clients.keys()):
            if exclude is not None and ws == exclude:
                continue
            try:
                await self._send_bytes(ws, payload)
            except websockets.exceptions.ConnectionClosed:
                dead.append(ws)
        for ws in dead:
            if ws in self.clients:
                await self._cleanup_client(ws)

    async def _broadcast_room_info(self):
        room = self._room_state()
        for ws, data in list(self.clients.items()):
            pkt = struct.pack(
                "<BBBB",
                self.PKT_ROOM_INFO,
                data["player_no"],
                room["alive_count"],
                room["max_players"],
            )
            try:
                await self._send_bytes(ws, pkt)
            except websockets.exceptions.ConnectionClosed:
                if ws in self.clients:
                    await self._cleanup_client(ws)

    async def _announce_format(self, msg_type, fmt, target_ws=None):
        fmt_bytes = fmt.encode("ascii")
        pkt = struct.pack("<BBB", self.PKT_FORMAT_ANNOUNCE, msg_type, len(fmt_bytes)) + fmt_bytes
        if target_ws is not None:
            await self._send_bytes(target_ws, pkt)
            return
        await self._broadcast_bytes(pkt)

    async def _send_all_formats_to(self, ws):
        for msg_type, fmt in sorted(self.message_formats.items()):
            try:
                await self._announce_format(msg_type, fmt, target_ws=ws)
            except websockets.exceptions.ConnectionClosed:
                break

    async def _cleanup_client(self, websocket):
        if websocket not in self.clients:
            return

        data = self.clients[websocket]
        client_id = data["id"]
        player_no = data["player_no"]
        del self.clients[websocket]

        if self.player_slots.get(player_no) == websocket:
            self.player_slots[player_no] = None

        print(f"[-] Client {client_id} disconnected (player {player_no})", flush=True)

        left_pkt = struct.pack("<BBB", self.PKT_PEER_LEFT, player_no, len(self.clients))
        await self._broadcast_bytes(left_pkt)
        await self._broadcast_room_info()

    async def _relay_user_payload(self, websocket, payload):
        # User payload format is raw bytes where payload[0] is user-defined data type (0..255).
        if len(payload) < 1:
            return

        msg_type = payload[0]
        if msg_type in self.system_types:
            # Reserved control/system packet types are not relayed as user payload.
            return

        # If format is registered for this message type, validate packet before relaying.
        fmt = self.message_formats.get(msg_type)
        if fmt:
            try:
                fmt_norm = self._format_for_calc(fmt)
                msg_size = struct.calcsize(fmt_norm)
                if len(payload) != 1 + msg_size:
                    return
                struct.unpack(fmt_norm, payload[1:])
            except Exception:
                return

        from_player = self.clients[websocket]["player_no"]
        relay_header = struct.pack("<BBH", self.PKT_RELAY, from_player, len(payload))
        relay_packet = relay_header + payload
        await self._broadcast_bytes(relay_packet, exclude=websocket)

    async def _handle_format_set_request(self, websocket, payload):
        # <BBB + fmt_ascii> => pkt_type(248), msg_type, fmt_len, fmt_bytes
        if len(payload) < 3:
            return
        _, msg_type, fmt_len = struct.unpack("<BBB", payload[:3])
        fmt_bytes = payload[3:]
        if len(fmt_bytes) != fmt_len:
            return
        try:
            fmt = fmt_bytes.decode("ascii")
        except UnicodeDecodeError:
            return
        fmt = self._normalize_declared_format(fmt)
        if not fmt:
            return
        if msg_type in self.system_types:
            return
        try:
            # Validate format string early to prevent bad format broadcast.
            struct.calcsize(self._format_for_calc(fmt))
        except Exception:
            return

        self.message_formats[msg_type] = fmt
        print(f"[FORMAT] msg_type={msg_type} fmt={fmt}", flush=True)
        await self._announce_format(msg_type, fmt)

    async def handler(self, websocket):
        if len(self.clients) >= self.max_players:
            room_full = struct.pack("<BB", self.PKT_ROOM_FULL, self.max_players)
            await self._send_bytes(websocket, room_full)
            await websocket.close(code=4001, reason="room_full")
            return

        player_no = self._allocate_player_slot()
        if player_no is None:
            room_full = struct.pack("<BB", self.PKT_ROOM_FULL, self.max_players)
            await self._send_bytes(websocket, room_full)
            await websocket.close(code=4002, reason="slot_unavailable")
            return

        client_id = self._next_client_id()
        self.clients[websocket] = {"id": client_id, "player_no": player_no}
        self.player_slots[player_no] = websocket
        print(f"[+] Client {client_id} connected as player {player_no}", flush=True)

        room = self._room_state()
        welcome = struct.pack("<BBBB", self.PKT_ROOM_INFO, player_no, room["alive_count"], room["max_players"])
        await self._send_bytes(websocket, welcome)
        await self._send_all_formats_to(websocket)

        peer_join = struct.pack("<BBB", self.PKT_PEER_JOIN, player_no, len(self.clients))
        await self._broadcast_bytes(peer_join, exclude=websocket)
        await self._broadcast_room_info()

        try:
            async for message in websocket:
                if not isinstance(message, (bytes, bytearray)):
                    # Binary-only wire protocol
                    continue
                raw = bytes(message)
                if len(raw) < 1:
                    continue
                if raw[0] == self.PKT_FORMAT_SET_REQ:
                    await self._handle_format_set_request(websocket, raw)
                    continue
                await self._relay_user_payload(websocket, raw)
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            await self._cleanup_client(websocket)

    def _print_cli_help(self):
        print("Server CLI commands:", flush=True)
        print("  help      Show this help", flush=True)
        print("  alive     Show alive/max and room-ready", flush=True)
        print("  clients   Show connected clients", flush=True)
        print("  formats   Show message format registry", flush=True)
        print("  slots     Show player slot assignment", flush=True)
        print("  snapshot  Show full runtime snapshot", flush=True)
        print("  cls       Clear terminal screen", flush=True)
        print("  quit      Stop server", flush=True)

    async def _handle_cli_command(self, line):
        try:
            parts = shlex.split(line)
        except ValueError as exc:
            print(f"[CLI ERR] {exc}", flush=True)
            return

        if not parts:
            return

        cmd = parts[0].lower()
        if cmd == "help":
            self._print_cli_help()
            return
        if cmd == "alive":
            room = self._room_state()
            print(
                f"[CLI] alive={room['alive_count']}/{room['max_players']} ready={room['room_ready']}",
                flush=True,
            )
            return
        if cmd == "clients":
            print(json.dumps(self._get_client_view(), indent=2), flush=True)
            return
        if cmd == "formats":
            print(json.dumps({str(k): v for k, v in sorted(self.message_formats.items())}, indent=2), flush=True)
            return
        if cmd == "slots":
            slot_view = {
                str(slot): (self.clients[ws]["id"] if ws in self.clients else None)
                for slot, ws in self.player_slots.items()
            }
            print(json.dumps(slot_view, indent=2), flush=True)
            return
        if cmd == "snapshot":
            print(json.dumps(self._runtime_snapshot(), indent=2), flush=True)
            return
        if cmd == "cls":
            os.system("cls" if os.name == "nt" else "clear")
            return
        if cmd in ("quit", "exit"):
            print("[CLI] Stopping server...", flush=True)
            self.stop_event.set()
            return

        print(f"[CLI ERR] Unknown command: {cmd}. Use 'help'.", flush=True)

    async def cli_loop(self):
        self._print_cli_help()
        while not self.stop_event.is_set():
            try:
                line = await asyncio.to_thread(input, "server> ")
            except EOFError:
                print("[CLI] stdin closed. CLI disabled; server continues running.", flush=True)
                return
            except KeyboardInterrupt:
                self.stop_event.set()
                return
            await self._handle_cli_command(line.strip())

    async def run(self):
        async with websockets.serve(self.handler, self.host, self.port):
            print(f"[OK] Binary 2-player server on {self.host}:{self.port}", flush=True)
            tasks = []
            if self.enable_cli:
                tasks.append(asyncio.create_task(self.cli_loop()))

            await self.stop_event.wait()

            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


async def main():
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "10000"))
    enable_cli = os.environ.get("ENABLE_SERVER_CLI", "1").strip() not in ("0", "false", "False")
    server = GeneralWebSocketServer(host=host, port=port, enable_cli=enable_cli, max_players=3)
    await server.run()


if __name__ == "__main__":
    asyncio.run(main())
