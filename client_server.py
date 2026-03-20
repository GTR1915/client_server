import asyncio
import inspect
import os
import shlex
import struct
import sys

import websockets


class PlayerBinaryClient:
    # Must match Network_System.py server constants
    PKT_FORMAT_SET_REQ = 248
    PKT_FORMAT_ANNOUNCE = 249
    PKT_ROOM_INFO = 250
    PKT_PEER_JOIN = 251
    PKT_PEER_LEFT = 252
    PKT_ROOM_FULL = 253
    PKT_RELAY = 254

    def __init__(self, server_url, reconnect_initial_delay=1.0, reconnect_max_delay=10.0):
        self.server_url = server_url
        self.reconnect_initial_delay = reconnect_initial_delay
        self.reconnect_max_delay = reconnect_max_delay

        self.websocket = None
        self.connected_event = asyncio.Event()
        self.stop_event = asyncio.Event()
        self.send_lock = asyncio.Lock()
        self.runner_task = None

        self.player_no = None
        self.alive_count = 0
        self.max_players = 2
        self.room_ready = False

        self.data_queue = asyncio.Queue()  # tuples: (from_player_no, msg_type, payload_bytes)
        self.format_registry = {}  # {msg_type: struct_format}
        self.assigned_functions = {}  # {msg_type: callable}

    @staticmethod
    def _normalize_declared_format(fmt):
        # Format here describes only message_data (NOT packet type byte).
        if not fmt:
            return None
        prefix = fmt[0] if fmt[0] in "@=<>!" else ""
        body = fmt[1:] if prefix else fmt
        if not body:
            return None
        return prefix + body if prefix else body

    @staticmethod
    def pack_user_packet(msg_type, payload=b""):
        if not isinstance(msg_type, int) or not (0 <= msg_type <= 255):
            raise ValueError("msg_type must be int in range 0..255")
        if msg_type >= PlayerBinaryClient.PKT_FORMAT_SET_REQ:
            raise ValueError("msg_type 248..255 is reserved for system/control packets")
        if payload is None:
            payload = b""
        if not isinstance(payload, (bytes, bytearray)):
            raise ValueError("payload must be bytes or bytearray")
        return struct.pack("<B", msg_type) + bytes(payload)

    @staticmethod
    def unpack_user_packet(packet):
        if not isinstance(packet, (bytes, bytearray)) or len(packet) < 1:
            raise ValueError("packet must be non-empty bytes")
        packet = bytes(packet)
        return packet[0], packet[1:]

    async def start(self):
        if self.runner_task and not self.runner_task.done():
            return
        self.stop_event.clear()
        self.runner_task = asyncio.create_task(self._connection_loop())

    async def stop(self):
        self.stop_event.set()
        self.connected_event.clear()
        if self.websocket is not None:
            try:
                await self.websocket.close()
            except Exception:
                pass
        if self.runner_task:
            await self.runner_task

    async def wait_until_connected(self, timeout=None):
        if timeout is None:
            await self.connected_event.wait()
        else:
            await asyncio.wait_for(self.connected_event.wait(), timeout=timeout)

    async def send_data(self, msg_type, payload=b""):
        packet = self.pack_user_packet(msg_type, payload)
        await self.send_raw_packet(packet)

    async def send_raw_packet(self, packet):
        if not isinstance(packet, (bytes, bytearray)) or len(packet) < 1:
            raise ValueError("packet must be non-empty bytes")
        await self.wait_until_connected(timeout=10.0)
        async with self.send_lock:
            if self.websocket is None:
                raise RuntimeError("Not connected")
            await self.websocket.send(bytes(packet))

    async def send_format_definition(self, msg_type, fmt):
        if not isinstance(msg_type, int) or not (0 <= msg_type <= 255):
            raise ValueError("msg_type must be int in range 0..255")
        if not isinstance(fmt, str) or not fmt:
            raise ValueError("fmt must be non-empty string")
        if msg_type >= self.PKT_FORMAT_SET_REQ:
            raise ValueError("msg_type is reserved for system packets")
        fmt = self._normalize_declared_format(fmt)
        if not fmt:
            raise ValueError("Invalid format")

        fmt_bytes = fmt.encode("ascii")
        if len(fmt_bytes) > 255:
            raise ValueError("fmt too long (max 255 bytes)")

        # Validate locally before publishing.
        _ = struct.calcsize(self._normalize_format(fmt))
        pkt = struct.pack("<BBB", self.PKT_FORMAT_SET_REQ, msg_type, len(fmt_bytes)) + fmt_bytes
        await self.send_raw_packet(pkt)

    def _normalize_format(self, fmt):
        return fmt if fmt[0] in "@=<>!" else "<" + fmt

    def _auto_convert_token(self, token):
        try:
            return int(token, 0)
        except ValueError:
            pass
        try:
            return float(token)
        except ValueError:
            pass
        return token

    def pack_by_registered_format(self, msg_type, values):
        if msg_type not in self.format_registry:
            raise ValueError(f"No registered format for message type {msg_type}")
        if msg_type >= self.PKT_FORMAT_SET_REQ:
            raise ValueError("msg_type 248..255 is reserved for system/control packets")
        fmt = self.format_registry[msg_type]
        fmt_norm = self._normalize_format(fmt)
        packed = struct.pack(fmt_norm, *values)
        if len(packed) < 1:
            raise ValueError("Packed payload is empty")
        if packed[0] != msg_type:
            raise ValueError(
                f"Packed first byte ({packed[0]}) does not match msg_type ({msg_type}). "
                "Include leading 'B' field correctly in format/values."
            )
        return packed

    def pack_tokens_by_registered_format(self, msg_type, tokens):
        values = [self._auto_convert_token(token) for token in tokens]
        fmt = self.format_registry.get(msg_type)
        if not fmt:
            raise ValueError(f"No registered format for message type {msg_type}")
        fmt = self._normalize_declared_format(fmt)
        fmt_norm = self._normalize_format(fmt)

        # Packet format is always: msg_type(B) + message_data(fmt).
        try:
            msg_data = struct.pack(fmt_norm, *values)
        except Exception as exc:
            raise ValueError(
                f"Could not pack values for type={msg_type} with fmt={fmt}. "
                f"Provided data={values}. Error={exc}"
            ) from exc

        return self.pack_user_packet(msg_type, msg_data)

    async def next_data(self):
        return await self.data_queue.get()

    def assign_function(self, channel, func):
        """
        Assign a callback for a message type.
        Example:
            client.assign_function(channel=12, func=func1)
        """
        if not isinstance(channel, int) or not (0 <= channel <= 255):
            raise ValueError("channel must be int in range 0..255")
        if channel >= self.PKT_FORMAT_SET_REQ:
            raise ValueError("channel 248..255 is reserved for system/control packets")
        if not callable(func):
            raise ValueError("func must be callable")
        self.assigned_functions[channel] = func

    def remove_assigned_function(self, channel):
        self.assigned_functions.pop(channel, None)

    async def _call_assigned_function(self, msg_type, user_payload, unpacked_values):
        func = self.assigned_functions.get(msg_type)
        if func is None:
            return
        try:
            if unpacked_values is not None:
                result = func(*unpacked_values)
            else:
                result = func(user_payload)
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            print(f"[FUNC ERR] channel={msg_type} error={exc}")

    def room_status(self):
        return {
            "player_no": self.player_no,
            "alive_count": self.alive_count,
            "max_players": self.max_players,
            "room_ready": self.room_ready,
        }

    def _update_room(self, alive_count, max_players=None):
        self.alive_count = int(alive_count)
        if max_players is not None:
            self.max_players = int(max_players)
        self.room_ready = self.alive_count == self.max_players

    def _print_room(self):
        print(
            f"[ROOM] alive={self.alive_count}/{self.max_players} "
            f"ready={self.room_ready} "
            f"you=player_{self.player_no if self.player_no is not None else '?'}"
        )

    async def _handle_binary_message(self, message):
        if len(message) < 1:
            return
        pkt_type = message[0]

        if pkt_type == self.PKT_ROOM_INFO:
            if len(message) < 4:
                return
            _, player_no, alive_count, max_players = struct.unpack("<BBBB", message[:4])
            self.player_no = player_no
            self._update_room(alive_count, max_players)
            print(f"[WELCOME] You are player_{self.player_no}")
            self._print_room()
            return

        if pkt_type == self.PKT_FORMAT_ANNOUNCE:
            if len(message) < 3:
                return
            _, msg_type, fmt_len = struct.unpack("<BBB", message[:3])
            fmt_bytes = message[3:]
            if len(fmt_bytes) != fmt_len:
                return
            try:
                fmt = fmt_bytes.decode("ascii")
                fmt = self._normalize_declared_format(fmt)
                if not fmt:
                    return
                _ = struct.calcsize(self._normalize_format(fmt))
            except Exception:
                return
            self.format_registry[msg_type] = fmt
            print(f"[FORMAT] type={msg_type} fmt={fmt}")
            return

        if pkt_type == self.PKT_PEER_JOIN:
            if len(message) < 3:
                return
            _, peer_player_no, alive_count = struct.unpack("<BBB", message[:3])
            self._update_room(alive_count)
            print(f"[ROOM] player_{peer_player_no} joined")
            self._print_room()
            return

        if pkt_type == self.PKT_PEER_LEFT:
            if len(message) < 3:
                return
            _, peer_player_no, alive_count = struct.unpack("<BBB", message[:3])
            self._update_room(alive_count)
            print(f"[ROOM] player_{peer_player_no} left")
            self._print_room()
            return

        if pkt_type == self.PKT_ROOM_FULL:
            if len(message) >= 2:
                _, max_players = struct.unpack("<BB", message[:2])
                self.max_players = max_players
            print(f"[ERROR] Room full ({self.max_players}/{self.max_players}). Waiting to reconnect...")
            return

        if pkt_type == self.PKT_RELAY:
            if len(message) < 4:
                return
            _, from_player_no, payload_len = struct.unpack("<BBH", message[:4])
            payload = message[4:]
            if len(payload) != payload_len or payload_len < 1:
                return
            msg_type, user_payload = self.unpack_user_packet(payload)
            await self.data_queue.put((from_player_no, msg_type, user_payload))
            print(
                f"[RECV] from=player_{from_player_no} type={msg_type} "
                f"payload_len={len(user_payload)} bytes"
            )
            unpacked_values = None
            if msg_type in self.format_registry:
                fmt = self.format_registry[msg_type]
                fmt_norm = self._normalize_format(fmt)
                try:
                    needed = struct.calcsize(fmt_norm)
                except Exception as exc:
                    print(f"[UNPACK ERR] invalid format for type={msg_type}: {exc}")
                    return
                if len(user_payload) != needed:
                    print(
                        f"[UNPACK WARN] type={msg_type} fmt={fmt} expects {needed} bytes, "
                        f"got {len(user_payload)} bytes"
                    )
                    return
                try:
                    values = struct.unpack(fmt_norm, user_payload)
                except Exception as exc:
                    print(f"[UNPACK ERR] {exc}")
                    return
                unpacked_values = values
                print(f"[UNPACK] type={msg_type} fmt={fmt} data={values}")
            await self._call_assigned_function(msg_type, user_payload, unpacked_values)
            return

        # Unknown server/system packet type.
        print(f"[WARN] Unknown packet type: {pkt_type} size={len(message)}")

    async def _receive_loop(self, websocket):
        async for message in websocket:
            if not isinstance(message, (bytes, bytearray)):
                continue
            await self._handle_binary_message(bytes(message))

    async def _connection_loop(self):
        delay = self.reconnect_initial_delay
        while not self.stop_event.is_set():
            try:
                async with websockets.connect(self.server_url) as websocket:
                    self.websocket = websocket
                    self.connected_event.set()
                    delay = self.reconnect_initial_delay
                    print(f"[INFO] Connected to {self.server_url}")
                    await self._receive_loop(websocket)
            except Exception as exc:
                if self.stop_event.is_set():
                    break
                print(f"[WARN] Connection lost: {exc}")
            finally:
                self.connected_event.clear()
                self.websocket = None

            if self.stop_event.is_set():
                break
            print(f"[INFO] Reconnecting in {delay:.1f}s")
            await asyncio.sleep(delay)
            delay = min(delay * 2.0, self.reconnect_max_delay)


async def interactive_cli(server_url):
    client = PlayerBinaryClient(server_url)
    await client.start()

    print("Player binary client")
    print("Commands:")
    print("  help")
    print("  status")
    print("  set <type_0_to_255> <struct_fmt>")
    print("    example: set 2 BBii")
    print("  formats")
    print("  send <type_0_to_255> <data1> <data2> ...")
    print("    packet format is always: B(msg_type) + message_data")
    print("    example: set 1 Bii ; send 1 9 100 200")
    print("  recv")
    print("  cls")
    print("  quit")

    try:
        while True:
            try:
                raw = await asyncio.to_thread(input, "> ")
            except (EOFError, KeyboardInterrupt):
                break

            line = raw.strip()
            if not line:
                continue

            try:
                parts = shlex.split(line)
            except ValueError as exc:
                print(f"[ERR] {exc}")
                continue

            cmd = parts[0].lower()
            if cmd == "quit":
                break

            try:
                if cmd == "help":
                    print("status")
                    print("set <type_0_to_255> <struct_fmt>")
                    print("formats")
                    print("send <type_0_to_255> <data1> <data2> ...")
                    print("recv")
                    print("cls")
                    print("quit")
                    continue

                if cmd == "status":
                    room = client.room_status()
                    print(
                        f"[ROOM] alive={room['alive_count']}/{room['max_players']} "
                        f"ready={room['room_ready']} "
                        f"you=player_{room['player_no'] if room['player_no'] else '?'}"
                    )
                    continue

                if cmd == "set":
                    if len(parts) != 3:
                        print("[ERR] set <type_0_to_255> <struct_fmt>")
                        continue
                    msg_type = int(parts[1])
                    fmt = parts[2]
                    await client.send_format_definition(msg_type, fmt)
                    print(f"[SENT] format set request: type={msg_type} fmt={fmt}")
                    continue

                if cmd == "formats":
                    if not client.format_registry:
                        print("{}")
                    else:
                        for key in sorted(client.format_registry.keys()):
                            print(f"{key}: {client.format_registry[key]}")
                    continue

                if cmd == "send":
                    if len(parts) < 2:
                        print("[ERR] send <type_0_to_255> <data1> <data2> ...")
                        continue
                    msg_type = int(parts[1])
                    packet = client.pack_tokens_by_registered_format(msg_type, parts[2:])
                    await client.send_raw_packet(packet)
                    print(f"[SENT] packed type={msg_type} total_len={len(packet)} bytes")
                    continue

                if cmd == "recv":
                    print("[INFO] Waiting for one data packet...")
                    from_player_no, msg_type, payload = await client.next_data()
                    msg = f"[DATA] from=player_{from_player_no} type={msg_type} payload_len={len(payload)}"
                    text_repr = None
                    int_repr = None
                    try:
                        text_repr = payload.decode("utf-8")
                    except UnicodeDecodeError:
                        text_repr = None
                    if len(payload) == 4:
                        int_repr = struct.unpack("<i", payload)[0]

                    if text_repr is not None:
                        msg += f" text={text_repr!r}"
                    if int_repr is not None:
                        msg += f" int={int_repr}"
                    if text_repr is None and int_repr is None:
                        msg += f" bytes={payload.hex().upper() if payload else ''}"
                    print(msg)
                    continue

                if cmd == "cls":
                    os.system("cls" if os.name == "nt" else "clear")
                    continue

                print("[ERR] Unknown command. Use help.")
            except Exception as exc:
                print(f"[ERR] {exc}")
    finally:
        await client.stop()


async def main():
    server_url = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("SERVER_URL", "ws://127.0.0.1:10000")
    await interactive_cli(server_url)


if __name__ == "__main__":
    asyncio.run(main())
    print("Hi hello")
