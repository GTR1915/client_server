import asyncio
import os
import shlex
import struct
from client_server import PlayerBinaryClient


def torch_on():
    os.system("termux-torch on")


def torch_off():
    os.system("termux-torch off")


def vibrate(duration_ms):
    os.system(f"termux-vibrate -d {duration_ms}")


device_id = int(input("Enter device ID (0-7): "))
torch_is_on = False
MESSAGE_TYPE = 12


def sync_torch_from_mask(mask_value):
    global torch_is_on

    if not isinstance(mask_value, int):
        raise ValueError("mask_value must be an int")
    if not (0 <= mask_value <= 255):
        raise ValueError("mask_value must be in range 0..255")
    if not (0 <= device_id <= 7):
        raise ValueError("device_id must be in range 0..7 for an 8-bit mask")

    should_turn_on = bool(mask_value & (1 << device_id))

    if should_turn_on and not torch_is_on:
        torch_on()
        torch_is_on = True
        print(f"Torch turned on for mask {mask_value:08b}")
    elif not should_turn_on and torch_is_on:
        torch_off()
        torch_is_on = False
        print(f"Torch turned off for mask {mask_value:08b}")
    else:
        print(f"No torch change for mask {mask_value:08b}")


def print_help():
    print("Commands:")
    print("  help")
    print("  status")
    print("  formats")
    print("  recv")
    print("  sendmask <0_to_255>")
    print("  torch on")
    print("  torch off")
    print("  vibrate <duration_ms>")
    print("  cls")
    print("  quit")


async def debug_cli(client):
    print_help()

    while True:
        try:
            raw = await asyncio.to_thread(input, "main_user> ")
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

        try:
            if cmd == "help":
                print_help()
                continue

            if cmd == "status":
                print(client.room_status())
                print(f"device_id={device_id} torch_is_on={torch_is_on}")
                continue

            if cmd == "formats":
                if not client.format_registry:
                    print("{}")
                else:
                    for key in sorted(client.format_registry):
                        print(f"{key}: {client.format_registry[key]}")
                continue

            if cmd == "recv":
                print("[INFO] Waiting for one packet...")
                from_player_no, msg_type, payload = await client.next_data()
                print(
                    f"[DATA] from=player_{from_player_no} "
                    f"type={msg_type} payload={payload.hex().upper()}"
                )
                continue

            if cmd == "sendmask":
                if len(parts) != 2:
                    print("[ERR] sendmask <0_to_255>")
                    continue
                mask_value = int(parts[1], 0)
                if not (0 <= mask_value <= 255):
                    print("[ERR] mask must be in range 0..255")
                    continue
                await client.send_data(MESSAGE_TYPE, struct.pack("<B", mask_value))
                print(f"[SENT] mask={mask_value} bits={mask_value:08b}")
                continue

            if cmd == "torch":
                if len(parts) != 2 or parts[1].lower() not in ("on", "off"):
                    print("[ERR] torch on|off")
                    continue
                if parts[1].lower() == "on":
                    torch_on()
                else:
                    torch_off()
                continue

            if cmd == "vibrate":
                if len(parts) != 2:
                    print("[ERR] vibrate <duration_ms>")
                    continue
                vibrate(int(parts[1], 0))
                continue

            if cmd == "cls":
                os.system("cls" if os.name == "nt" else "clear")
                continue

            if cmd in ("quit", "exit"):
                break

            print("[ERR] Unknown command. Use help.")
        except Exception as exc:
            print(f"[ERR] {exc}")


async def app():
    client = PlayerBinaryClient("ws://127.0.0.1:10000")
    await client.start()
    await client.wait_until_connected()

    await client.send_format_definition(MESSAGE_TYPE, "B")
    client.assign_function(MESSAGE_TYPE, sync_torch_from_mask)

    try:
        await debug_cli(client)
    finally:
        await client.stop()


asyncio.run(app())
