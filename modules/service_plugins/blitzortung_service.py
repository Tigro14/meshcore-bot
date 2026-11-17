#!/usr/bin/env python3
"""
Blitzortung Lightning Detection Service for MeshCore Bot

Connects to the Blitzortung MQTT broker, filters strikes within a configured
bounding box, and sends alerts to a mesh channel when the strike count for a
location bucket exceeds the configured threshold within a rolling time window.

Configuration section: [Blitzortung_Service]
  enabled             = true
  channel             = #bot          # mesh channel for alerts
  alert_threshold     = 10            # min strikes per bucket to trigger
  window_minutes      = 10            # collection window in minutes
  my_position_lat     = 47.5          # bot latitude  (fallback: [Weather_Service])
  my_position_lon     = 15.6          # bot longitude (fallback: [Weather_Service])
  blitz_area_min_lat  = 47.0          # bounding box (required)
  blitz_area_min_lon  = 15.0
  blitz_area_max_lat  = 48.0
  blitz_area_max_lon  = 16.5

Requires paho-mqtt: pip install paho-mqtt
"""

import asyncio
import contextlib
import json
import math
import time
import uuid
from typing import Any, Optional

try:
    import paho.mqtt.client as mqtt

    MQTT_AVAILABLE = True
except ImportError:
    MQTT_AVAILABLE = False
    mqtt = None  # type: ignore[assignment]

from .base_service import BaseServicePlugin

_B32 = "0123456789bcdefghjkmnpqrstuvwxyz"


def _geohash_encode(lat: float, lon: float, precision: int) -> str:
    """Encode a point into a geohash string of the given precision."""
    lat_rng = [-90.0, 90.0]
    lon_rng = [-180.0, 180.0]
    out: list[str] = []
    bits: list[int] = []
    even = True  # longitude bit first, then alternating
    while len(out) < precision:
        if even:
            mid = (lon_rng[0] + lon_rng[1]) / 2.0
            b = 1 if lon >= mid else 0
            lon_rng[1 - b] = mid  # bit 1 narrows the lower bound, bit 0 the upper
        else:
            mid = (lat_rng[0] + lat_rng[1]) / 2.0
            b = 1 if lat >= mid else 0
            lat_rng[1 - b] = mid
        bits.append(b)
        even = not even
        if len(bits) == 5:
            out.append(_B32[int("".join(map(str, bits)), 2)])
            bits = []
    return "".join(out)


def _geohash_cells(
    min_lat: float, min_lon: float, max_lat: float, max_lon: float, precision: int
) -> list[str]:
    """Enumerate every geohash cell of *precision* that intersects a bbox.

    Uses cell-index arithmetic (not point sampling) so no intersecting cell is
    skipped. The Blitzortung MQTT relay rejects wildcard subscriptions that are
    too shallow (e.g. "blitzortung/1.1/#" answers SUBACK 0x80), so the service
    must subscribe per-cell prefixes instead.
    """
    lon_bits = (5 * precision + 1) // 2  # alternating, longitude first
    lat_bits = 5 * precision - lon_bits
    lon_size = 360.0 / 2**lon_bits
    lat_size = 180.0 / 2**lat_bits
    lon_i0 = max(0, int((min_lon + 180.0) // lon_size))
    lon_i1 = min(2**lon_bits - 1, int((max_lon + 180.0) // lon_size))
    lat_i0 = max(0, int((min_lat + 90.0) // lat_size))
    lat_i1 = min(2**lat_bits - 1, int((max_lat + 90.0) // lat_size))

    cells: list[str] = []
    for li in range(lat_i0, lat_i1 + 1):
        for lo in range(lon_i0, lon_i1 + 1):
            # Re-interleave the cell indices into geohash bits, longitude first.
            bits: list[int] = []
            for bit in range(5 * precision):
                if bit % 2 == 0:
                    n = bit // 2
                    bits.append((lo >> (lon_bits - 1 - n)) & 1 if n < lon_bits else 0)
                else:
                    n = bit // 2
                    bits.append((li >> (lat_bits - 1 - n)) & 1 if n < lat_bits else 0)
            cells.append(
                "".join(
                    _B32[int("".join(map(str, bits[i * 5 : i * 5 + 5])), 2)]
                    for i in range(precision)
                )
            )
    return cells


def _mqtt_topics_for_area(area: dict[str, float]) -> list[str]:
    """Per-geohash wildcard subscriptions covering a bounding box.

    The relay (blitzortung.ha.sed.pl) answers SUBACK 0x80 to "blitzortung/1.1/#",
    so we subscribe one wildcard per geohash cell instead. Cells of precision 3
    (~1.4° x 1.4°) keep traffic low for a typical bbox; larger boxes fall back
    to precision 2. Out-of-box strikes are dropped by the on_message filter.
    """
    precision = 3
    cells = _geohash_cells(
        area["min_lat"], area["min_lon"], area["max_lat"], area["max_lon"], precision
    )
    if len(cells) > 16:
        precision = 2
        cells = _geohash_cells(
            area["min_lat"], area["min_lon"], area["max_lat"], area["max_lon"], precision
        )
    return [f"blitzortung/1.1/{'/'.join(cell)}/#" for cell in cells[:24]]


class BlitzortungService(BaseServicePlugin):
    """Real-time lightning detection via Blitzortung MQTT.

    Subscribes to the Blitzortung public MQTT broker, groups incoming strikes
    by (heading, 10 km distance) bucket, and fires an alert on the configured
    channel whenever a bucket accumulates at least *alert_threshold* strikes
    within *window_minutes* minutes.
    """

    config_section = "Blitzortung_Service"
    description = "Real-time lightning detection via Blitzortung MQTT"

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)

        # --- runtime state ---------------------------------------------------
        # Initialized unconditionally so stop() never hits AttributeError when
        # an early-return below disables the service.
        self.blitz_buffer: list[dict[str, Any]] = []
        self.seen_blitz_keys: set[str] = set()
        self.mqtt_client: Optional[Any] = None
        self._mqtt_task: Optional[asyncio.Task] = None
        self._lightning_task: Optional[asyncio.Task] = None

        section = "Blitzortung_Service"

        # --- alert destination ------------------------------------------------
        self.channel: str = self.bot.config.get(section, "channel", fallback="general")

        # --- thresholds -------------------------------------------------------
        # Try Blitzortung_Service first, then fall back to Weather_Service (legacy)
        self.alert_threshold: int = self.bot.config.getint(
            section, "alert_threshold", fallback=None
        )
        if self.alert_threshold is None:
            self.alert_threshold = self.bot.config.getint(
                "Weather_Service", "blitz_alert_threshold", fallback=10
            )

        # Try Blitzortung_Service window_minutes first
        window_minutes: Optional[int] = None
        if self.bot.config.has_option(section, "window_minutes"):
            window_minutes = self.bot.config.getint(section, "window_minutes")
        # Fall back to Weather_Service blitz_collection_interval (in milliseconds)
        elif self.bot.config.has_option("Weather_Service", "blitz_collection_interval"):
            blitz_collection_interval_ms = self.bot.config.getint(
                "Weather_Service", "blitz_collection_interval", fallback=600000
            )
            window_minutes = int(blitz_collection_interval_ms / 60000.0)
        else:
            window_minutes = 10
        self.window_seconds: float = window_minutes * 60.0

        # --- mesh silence (send only to external webhooks/Telegram) ----------
        self.silence_mesh_output: bool = self.bot.config.getboolean(
            section, "silence_mesh_output", fallback=False
        )

        # --- bot position (try Blitzortung_Service → Weather_Service fallback) ---
        self.my_position_lat: Optional[float] = self.bot.config.getfloat(
            section, "my_position_lat", fallback=None
        )
        self.my_position_lon: Optional[float] = self.bot.config.getfloat(
            section, "my_position_lon", fallback=None
        )
        if self.my_position_lat is None and self.bot.config.has_section("Weather_Service"):
            self.my_position_lat = self.bot.config.getfloat(
                "Weather_Service", "my_position_lat", fallback=None
            )
        if self.my_position_lon is None and self.bot.config.has_section("Weather_Service"):
            self.my_position_lon = self.bot.config.getfloat(
                "Weather_Service", "my_position_lon", fallback=None
            )

        if self.my_position_lat is None or self.my_position_lon is None:
            self.logger.warning(
                "Blitzortung service disabled: my_position_lat/lon missing "
                "(set in [Blitzortung_Service] or [Weather_Service])"
            )
            self.enabled = False
            return

        # --- bounding box (try Blitzortung_Service → Weather_Service fallback) ---
        self.blitz_area: Optional[dict[str, float]] = None
        # Try Blitzortung_Service first
        if self.bot.config.has_option(section, "blitz_area_min_lat"):
            self.blitz_area = {
                "min_lat": self.bot.config.getfloat(section, "blitz_area_min_lat"),
                "min_lon": self.bot.config.getfloat(section, "blitz_area_min_lon"),
                "max_lat": self.bot.config.getfloat(section, "blitz_area_max_lat"),
                "max_lon": self.bot.config.getfloat(section, "blitz_area_max_lon"),
            }
        # Fall back to Weather_Service
        elif self.bot.config.has_option("Weather_Service", "blitz_area_min_lat"):
            self.blitz_area = {
                "min_lat": self.bot.config.getfloat("Weather_Service", "blitz_area_min_lat"),
                "min_lon": self.bot.config.getfloat("Weather_Service", "blitz_area_min_lon"),
                "max_lat": self.bot.config.getfloat("Weather_Service", "blitz_area_max_lat"),
                "max_lon": self.bot.config.getfloat("Weather_Service", "blitz_area_max_lon"),
            }

        if not self.blitz_area:
            self.logger.warning(
                "Blitzortung service disabled: blitz_area_min/max_lat/lon not configured "
                "(set in [Blitzortung_Service] or [Weather_Service])"
            )
            self.enabled = False
            return

        if not MQTT_AVAILABLE:
            self.logger.warning(
                "Blitzortung service disabled: paho-mqtt not installed "
                "(pip install paho-mqtt)"
            )
            self.enabled = False
            return

        self.logger.info(
            "Blitzortung service initialized: channel=%s threshold=%d window=%ds "
            "position=(%.4f, %.4f)",
            self.channel,
            self.alert_threshold,
            int(self.window_seconds),
            self.my_position_lat,
            self.my_position_lon,
        )

    # ── lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        if not self.enabled:
            self.logger.info("Blitzortung service is disabled, not starting")
            return

        self._running = True
        self._lightning_task = asyncio.create_task(self._poll_lightning_loop())
        self._mqtt_task = asyncio.create_task(self._connect_blitzortung_mqtt())
        self.logger.info("Blitzortung service started")

    async def stop(self) -> None:
        self._running = False
        self.logger.info("Stopping Blitzortung service")

        if self._lightning_task:
            self._lightning_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._lightning_task

        if self._mqtt_task:
            self._mqtt_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._mqtt_task

        if self.mqtt_client:
            try:
                self.mqtt_client.loop_stop()
                self.mqtt_client.disconnect()
            except Exception:
                pass

        self.logger.info("Blitzortung service stopped")

    # ── MQTT connection ───────────────────────────────────────────────────────

    def _mqtt_topics(self) -> list[str]:
        """Wildcard subscriptions covering the configured bbox (see _mqtt_topics_for_area)."""
        if not self.blitz_area:
            return []
        return _mqtt_topics_for_area(self.blitz_area)

    async def _connect_blitzortung_mqtt(self) -> None:
        """Connect to Blitzortung MQTT broker and subscribe to strike feed."""
        if not self.blitz_area or not MQTT_AVAILABLE:
            return

        broker_host = "blitzortung.ha.sed.pl"
        broker_port = 1883
        topics = self._mqtt_topics()

        self.logger.info(
            "Connecting to Blitzortung MQTT broker: %s:%d (%d topics)",
            broker_host,
            broker_port,
            len(topics),
        )

        while self._running:
            try:
                client_id = f"meshcore_blitzortung_{uuid.uuid4().hex[:8]}"
                client = mqtt.Client(client_id=client_id)
                self.mqtt_client = client

                def on_message(client: Any, userdata: Any, msg: Any) -> None:
                    try:
                        blitz_data = json.loads(msg.payload.decode("utf-8"))
                        lat = blitz_data.get("lat")
                        lon = blitz_data.get("lon")
                        if lat is None or lon is None:
                            return
                        area = self.blitz_area
                        if area is None:
                            return
                        if (
                            area["min_lat"] <= lat <= area["max_lat"]
                            and area["min_lon"] <= lon <= area["max_lon"]
                        ):
                            asyncio.create_task(self._handle_lightning_strike(blitz_data))
                    except json.JSONDecodeError:
                        self.logger.debug("Invalid JSON in Blitzortung MQTT message")
                    except Exception as exc:
                        self.logger.debug(
                            "Error processing Blitzortung MQTT message: %s", exc
                        )

                client.on_message = on_message

                def on_subscribe(client: Any, userdata: Any, mid: Any, granted_qos: Any) -> None:
                    # A broker can accept CONNECT but refuse SUBSCRIBE (granted QoS
                    # >= 0x80); without this check the service looks connected but
                    # silently receives nothing.
                    try:
                        denied = [q for q in granted_qos if int(q) >= 0x80]
                    except (TypeError, ValueError):
                        denied = []
                    if denied:
                        self.logger.warning(
                            "Blitzortung MQTT subscription %s refused by broker "
                            "(granted_qos=%s)", mid, granted_qos
                        )

                client.on_subscribe = on_subscribe

                loop = asyncio.get_event_loop()
                try:
                    await loop.run_in_executor(
                        None, client.connect, broker_host, broker_port, 60
                    )
                except Exception as connect_error:
                    self.logger.debug("connect() failed: %s", connect_error)
                    raise

                client.subscribe([(t, 0) for t in topics])
                client.loop_start()
                self.logger.info(
                    "Connected to Blitzortung MQTT, subscribed to %s",
                    ", ".join(topics),
                )

                while self._running:
                    await asyncio.sleep(1)
                    if not client.is_connected():
                        self.logger.warning(
                            "Blitzortung MQTT disconnected, reconnecting…"
                        )
                        break

                client.loop_stop()
                client.disconnect()

            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.logger.error("Error in Blitzortung MQTT connection: %s", exc)
                if self._running:
                    self.logger.info("Reconnecting to Blitzortung MQTT in 30 seconds…")
                    await asyncio.sleep(30)

    # ── strike handling ───────────────────────────────────────────────────────

    async def _handle_lightning_strike(self, blitz_data: dict[str, Any]) -> None:
        """Buffer a single strike after computing its heading/distance bucket."""
        lat = blitz_data.get("lat")
        lon = blitz_data.get("lon")
        if lat is None or lon is None:
            return

        heading, distance = self._calculate_heading_and_distance(
            self.my_position_lat, self.my_position_lon, lat, lon  # type: ignore[arg-type]
        )
        key = f"{heading}|{int(distance / 10)}"

        self.blitz_buffer.append(
            {
                "key": key,
                "heading": heading,
                "distance": distance,
                "lat": lat,
                "lon": lon,
                "timestamp": blitz_data.get("time", time.time()),
            }
        )

    # ── aggregation loop ──────────────────────────────────────────────────────

    async def _poll_lightning_loop(self) -> None:
        """Sleep for *window_seconds* then process the strike buffer."""
        self.logger.info(
            "Starting lightning aggregation (window: %ds)", int(self.window_seconds)
        )
        while self._running:
            try:
                await asyncio.sleep(self.window_seconds)
                await self._process_lightning_buffer()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.logger.error("Error in lightning aggregation loop: %s", exc)
                await asyncio.sleep(60)

    async def _process_lightning_buffer(self) -> None:
        """Send alerts for any bucket that reached the threshold, then reset."""
        if not self.blitz_buffer:
            return

        counter: dict[str, int] = {}
        for strike in self.blitz_buffer:
            key = strike["key"]
            counter[key] = counter.get(key, 0) + 1

        for key, count in counter.items():
            if count >= self.alert_threshold and key not in self.seen_blitz_keys:
                bucket_strikes = [b for b in self.blitz_buffer if b["key"] == key]
                if not bucket_strikes:
                    continue

                data = min(bucket_strikes, key=lambda b: b["distance"])
                compass_name = self._heading_to_compass(data["heading"])
                location_name = await self._geocode_location(data["lat"], data["lon"])

                if location_name:
                    message = (
                        f"🌩️ {location_name} ({int(data['distance'])}km {compass_name})"
                    )
                else:
                    message = (
                        f"🌩️ Lightning activity ({int(data['distance'])}km {compass_name})"
                    )

                if not self.silence_mesh_output:
                    await self.bot.command_manager.send_channel_message(
                        self.channel,
                        message,
                        scope=self.get_mesh_flood_scope(),
                    )
                await self.send_external_notifications(message)
                self.logger.info("Lightning alert sent: %s", message)

                self.seen_blitz_keys.add(key)
                await asyncio.sleep(2)

        # Reset for next window
        self.blitz_buffer = []
        self.seen_blitz_keys = set()

    # ── geometry helpers ──────────────────────────────────────────────────────

    def _calculate_heading_and_distance(
        self, lat1: float, lon1: float, lat2: float, lon2: float
    ) -> tuple[int, float]:
        """Return (heading_degrees, distance_km) from (lat1,lon1) to (lat2,lon2)."""
        lat1_r = math.radians(lat1)
        lat2_r = math.radians(lat2)
        dlon_r = math.radians(lon2 - lon1)

        a = math.sin((lat2_r - lat1_r) / 2) ** 2 + math.cos(lat1_r) * math.cos(
            lat2_r
        ) * math.sin(dlon_r / 2) ** 2
        distance_km = 6371 * 2 * math.asin(math.sqrt(a))

        y = math.sin(dlon_r) * math.cos(lat2_r)
        x = math.cos(lat1_r) * math.sin(lat2_r) - math.sin(lat1_r) * math.cos(
            lat2_r
        ) * math.cos(dlon_r)
        heading_deg = (math.degrees(math.atan2(y, x)) + 360) % 360

        return (int(heading_deg), distance_km)

    def _heading_to_compass(self, heading: int) -> str:
        """Convert a heading in degrees to a 16-point compass abbreviation."""
        points = [
            "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
        ]
        return points[int((heading + 11.25) / 22.5) % 16]

    async def _geocode_location(self, lat: float, lon: float) -> Optional[str]:
        """Reverse-geocode a coordinate to a city/town name (best-effort)."""
        try:
            from ..utils import rate_limited_nominatim_reverse_sync

            location = await asyncio.to_thread(
                rate_limited_nominatim_reverse_sync,
                self.bot,
                f"{lat}, {lon}",
                timeout=5,
            )
            if location:
                if isinstance(location, dict):
                    return (
                        location.get("city")
                        or location.get("town")
                        or location.get("village")
                        or None
                    )
                return str(location)
        except Exception:
            pass
        return None
