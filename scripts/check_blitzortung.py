#!/usr/bin/env python3
"""
Diagnostic tool for [Blitzortung_Service].

Verifies, in order:
  1. Config gating  – is the service enabled? If not, exactly why?
  2. MQTT broker    – is the Blitzortung broker reachable, and are strike
                      messages actually arriving (and inside the configured box)?
  3. Notification routing – which mesh channel and which Discord/Telegram
                      targets would an alert fire at?
  4. Aggregation interval – does the processing loop really run once per window?

Usage:
  python3 scripts/check_blitzortung.py                   # steps 1 + 2 (TCP only) + 3 (dry-run)
  python3 scripts/check_blitzortung.py --listen 30       # step 2: subscribe live for 30 s
  python3 scripts/check_blitzortung.py --simulate        # step 3+4 with synthetic strikes, mesh/external mocked
  python3 scripts/check_blitzortung.py --simulate --live # external posts are REAL (Discord/Telegram get the test alert)

Exit code: 0 = service looks operational, 1 = problem found.
"""

import argparse
import asyncio
import configparser
import json
import logging
import socket
import sys
import time
import uuid
from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock, Mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from modules.service_plugins import blitzortung_service as bz_mod
from modules.service_plugins.blitzortung_service import BlitzortungService

SECTION = "Blitzortung_Service"
BROKER_HOST = "blitzortung.ha.sed.pl"
BROKER_PORT = 1883
TOPIC = "blitzortung/1.1/#"

log = logging.getLogger("check_blitzortung")
problems: list[str] = []


def fail(msg: str) -> None:
    problems.append(msg)
    print(f"  [FAIL] {msg}")


def ok(msg: str) -> None:
    print(f"  [ OK ] {msg}")


def info(msg: str) -> None:
    print(f"  [INFO] {msg}")


# ── step 1: config gating ─────────────────────────────────────────────────────

def load_config(path: Path) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.read(path)
    if not cfg.sections():
        fail(f"config file not found or empty: {path}")
        sys.exit(1)
    return cfg


def build_bot(cfg: configparser.ConfigParser) -> Mock:
    """Mock bot exposing only what BlitzortungService touches."""
    bot = Mock(name="diag-bot")
    bot.logger = log
    bot.config = cfg
    bot.command_manager = Mock()
    bot.command_manager.send_channel_message = AsyncMock()
    return bot


def make_service(cfg: configparser.ConfigParser) -> Optional[BlitzortungService]:
    # Reproduce the plugin-loader gate: section must exist AND enabled=true.
    if not cfg.has_section(SECTION):
        fail(f"config section [{SECTION}] missing — the plugin loader never loads the service")
        return None
    if not cfg.getboolean(SECTION, "enabled", fallback=False):
        fail(f"[{SECTION}] exists but enabled is missing or false — the plugin loader skips the service")
        return None
    ok(f"[{SECTION}] present with enabled=true")

    svc = BlitzortungService(build_bot(cfg))
    if not svc.enabled:
        # __init__ already logged the exact reason as a warning above.
        fail("service instantiated but self.enabled=False (see warning above)")
        return None
    ok(
        f"service enabled: channel={svc.channel} threshold={svc.alert_threshold} "
        f"window={svc.window_seconds:.0f}s "
        f"position=({svc.my_position_lat:.4f}, {svc.my_position_lon:.4f})"
    )
    area = svc.blitz_area
    if not area:
        fail("service enabled but blitz_area is not parsed (config corners missing?)")
        return None
    info(
        f"area: lat {area['min_lat']:.4f}..{area['max_lat']:.4f}  "
        f"lon {area['min_lon']:.4f}..{area['max_lon']:.4f}"
    )
    if not bz_mod.MQTT_AVAILABLE:
        fail("paho-mqtt not installed in this environment (pip install paho-mqtt)")
        return None
    return svc


# ── step 2: broker reachability + live strikes ────────────────────────────────

def check_broker_tcp() -> None:
    try:
        with socket.create_connection((BROKER_HOST, BROKER_PORT), timeout=10):
            ok(f"TCP reachable: {BROKER_HOST}:{BROKER_PORT}")
    except OSError as exc:
        fail(f"cannot open TCP to {BROKER_HOST}:{BROKER_PORT}: {exc}")


def listen_broker(svc: BlitzortungService, seconds: int, topics: list[str]) -> None:
    if not bz_mod.MQTT_AVAILABLE:
        fail("cannot listen: paho-mqtt not installed")
        return
    import paho.mqtt.client as mqtt

    stats = {"total": 0, "parsed": 0, "in_area": 0, "connack": None, "suback": None}

    try:  # paho >= 2.x and 1.6 callback signatures
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                             client_id=f"meshcore_diag_{uuid.uuid4().hex[:8]}")
    except AttributeError:
        client = mqtt.Client(client_id=f"meshcore_diag_{uuid.uuid4().hex[:8]}")

    def on_message(*a):  # version-agnostic: message is always the last arg
        msg = a[-1]
        stats["total"] += 1
        try:
            data = json.loads(msg.payload.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        lat, lon = data.get("lat"), data.get("lon")
        if lat is None or lon is None:
            return
        stats["parsed"] += 1
        a_ = svc.blitz_area
        if not a_:
            return
        if a_["min_lat"] <= lat <= a_["max_lat"] and a_["min_lon"] <= lon <= a_["max_lon"]:
            stats["in_area"] += 1

    def on_connect(*a):
        # paho 2.x: (client, userdata, flags, reason_code, properties)
        # paho 1.6: (client, userdata, flags, rc)
        stats["connack"] = next(x for x in a if isinstance(x, int) or hasattr(x, "value"))

    def on_subscribe(*a):
        # paho 2.x: (client, userdata, mid, reason_codes, properties)
        # paho 1.6: (client, userdata, mid, granted_qos)
        last = a[-1]
        stats["suback"] = last if isinstance(last, (list, tuple)) else a[3]

    client.on_message = on_message
    client.on_connect = on_connect
    client.on_subscribe = on_subscribe
    try:
        client.connect(BROKER_HOST, BROKER_PORT, 60)
        for t in topics:
            client.subscribe(t)
        client.loop_start()
        info(f"subscribed to {topics} for {seconds}s …")
        time.sleep(seconds)
        client.loop_stop()
        client.disconnect()
    except Exception as exc:
        fail(f"MQTT subscribe failed: {exc}")
        return

    # The service code never checks SUBACK; a broker can accept CONNECT yet
    # refuse SUBSCRIBE (granted QoS 0x80 / "Not authorized") and stay silent.
    suback = stats["suback"]
    if suback is not None:
        codes = suback if isinstance(suback, (list, tuple)) else [suback]
        values = [int(getattr(c, "value", c)) for c in codes]
        if any(v >= 0x80 for v in values):
            fail(f"broker REJECTED the subscription to {topics} "
                 f"(suback={[hex(v) for v in values]}) — "
                 "the service would log 'Connected, subscribed' but receive nothing")
            return

    info(f"received={stats['total']} parsed-strikes={stats['parsed']} in-configured-area={stats['in_area']}")
    if stats["total"] == 0:
        fail("subscription accepted but broker delivered no messages — feed or topic problem")
    elif stats["parsed"] == 0:
        fail("messages arrived but none parsed as strikes (unexpected payload format)")
    elif stats["in_area"] == 0:
        info("no strikes in your area during the window (may simply be quiet weather)")
    else:
        ok("live strikes arriving inside the configured bounding box")


# ── step 3: notification routing with synthetic strikes ───────────────────────

async def simulate_alert(svc: BlitzortungService, live: bool) -> None:
    """Inject threshold synthetic strikes ~40 km due east, run one real cycle."""
    if svc.my_position_lat is None or svc.my_position_lon is None:
        fail("cannot simulate: my_position_lat/lon not configured")
        return
    lat = svc.my_position_lat
    lon = svc.my_position_lon + 0.36  # ~40 km east at mid-latitudes

    ext = svc._get_external_notify_settings()
    print(
        f"  mesh:    channel={svc.channel} silence_mesh_output={svc.silence_mesh_output} "
        f"flood_scope={svc.get_mesh_flood_scope()}"
    )
    print(f"  discord: {len(ext.discord_urls)} webhook url(s)")
    print(
        f"  telegram: {len(ext.telegram_chat_ids)} chat id(s), "
        f"token={'resolved' if ext.telegram_token else 'NOT RESOLVED'}"
    )
    if ext.telegram_chat_ids and not ext.telegram_token:
        fail("telegram_chat_ids set but no bot token "
             "(telegram_bot_token / TELEGRAM_BOT_TOKEN / [TelegramBridge] api_token)")
    if not ext.discord_urls and not ext.telegram_chat_ids and svc.silence_mesh_output:
        fail("silence_mesh_output=true and no external targets configured — alerts go nowhere!")

    # Feed strikes through the real strike-handling path.
    for _ in range(svc.alert_threshold):
        await svc._handle_lightning_strike({"lat": lat, "lon": lon, "time": time.time()})
    info(f"injected {svc.alert_threshold} synthetic strikes at ({lat}, {lon})")

    if not live:
        svc._geocode_location = AsyncMock(return_value="SimTown")  # type: ignore[method-assign]
        ext_send = AsyncMock()
        svc.send_external_notifications = ext_send  # type: ignore[method-assign]
    else:
        # skip the Nominatim lookup so the live run stays fast
        svc._geocode_location = AsyncMock(return_value=None)  # type: ignore[method-assign]
        ext_send = None

    await svc._process_lightning_buffer()

    mesh_calls = svc.bot.command_manager.send_channel_message.await_count
    if svc.silence_mesh_output:
        ok("silence_mesh_output=true: no mesh message expected")
    elif mesh_calls == 1:
        call = svc.bot.command_manager.send_channel_message.call_args
        ok(f"mesh alert sent to channel {call.args[0]!r}: {call.args[1]}")
    else:
        fail(f"expected exactly 1 mesh channel message, got {mesh_calls}")

    if ext_send is not None:
        ext_calls = ext_send.await_count
        expected = 1
        if ext_calls == expected:
            ok(f"send_external_notifications() called {ext_calls}x "
               f"(would hit {len(ext.discord_urls)} discord, {len(ext.telegram_chat_ids)} telegram)")
        else:
            fail(f"expected {expected} external notification call, got {ext_calls}")
    else:
        # live mode: verify Discord/Telegram actually accepted the message
        if ext.discord_urls or (ext.telegram_chat_ids and ext.telegram_token):
            await svc.send_external_notifications("🌩️ Blitzortung diagnostic test alert")
            ok("LIVE: external notification posted (check your Discord/Telegram channel)")
        else:
            info("LIVE mode but no external targets configured — nothing posted")

    if svc.blitz_buffer != [] or svc.seen_blitz_keys != set():
        fail("buffer/seen-keys not reset after processing cycle")
    else:
        ok("buffer reset after cycle (no duplicate alerts)")


# ── step 4: aggregation interval cadence ──────────────────────────────────────

async def interval_test(svc: BlitzortungService) -> None:
    """Shorten the window to 2s and measure the real loop cadence."""
    configured_window = svc.window_seconds
    svc.window_seconds = 2.0
    svc._running = True
    stamps: list[float] = []

    async def spy():
        stamps.append(time.monotonic())

    svc._process_lightning_buffer = spy  # type: ignore[method-assign]
    task = asyncio.create_task(svc._poll_lightning_loop())
    await asyncio.sleep(svc.window_seconds * 3.5)
    svc._running = False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    if len(stamps) < 3:
        fail(f"aggregation loop fired only {len(stamps)}x in {svc.window_seconds * 3.5:.0f}s")
        return
    gaps = [round(b - a, 2) for a, b in zip(stamps, stamps[1:], strict=False)]
    ok(f"loop cadence with window=2s: {len(stamps)} cycles, gaps={gaps}s "
       f"(configured window is {configured_window:.0f}s via config)")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                    formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=str(ROOT / "config.ini"))
    parser.add_argument("--listen", type=int, metavar="SECONDS",
                        help="subscribe to the live Blitzortung feed for N seconds")
    parser.add_argument("--topic", action="append",
                        help="MQTT topic(s) for --listen; repeatable. Default: the "
                             "service's own per-geohash subscriptions")
    parser.add_argument("--simulate", action="store_true",
                        help="inject synthetic strikes and verify alert routing")
    parser.add_argument("--live", action="store_true",
                        help="with --simulate: really post to Discord/Telegram targets")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    cfg = load_config(Path(args.config))
    print(f"\n== 1. Config gating ({args.config}) ==")
    svc = make_service(cfg)
    if svc is None:
        print(f"\nRESULT: {len(problems)} problem(s) — service not operational.")
        return 1

    print("\n== 2. MQTT broker ==")
    check_broker_tcp()
    if args.listen:
        topics = args.topic or (svc._mqtt_topics() or [TOPIC])
        listen_broker(svc, args.listen, topics)

    if args.simulate:
        print("\n== 3. Notification routing (simulated strikes) ==")
        await simulate_alert(svc, live=args.live)
        print("\n== 4. Aggregation interval ==")
        await interval_test(svc)
    else:
        print("\n== 3/4. Skipped (add --simulate to test routing & interval; add --live to really post) ==")

    if problems:
        print(f"\nRESULT: {len(problems)} problem(s) found.")
        return 1
    print("\nRESULT: all checks passed — service looks operational.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
