"""Calendar dates and official phenomenon time windows, independent of radio delivery."""

import math
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

PARIS = ZoneInfo("Europe/Paris")


def calendar_days(now: float) -> list[str]:
    today = datetime.fromtimestamp(now, PARIS).date()
    return [(today + timedelta(days=offset)).isoformat() for offset in (0, 1)]


def period_key(code: str, day: str) -> str:
    return f"{code}@{day}"


def split_key(key: str) -> tuple[str, str | None]:
    code, separator, day = key.partition("@")
    return code, day if separator else None


def day_label(day: str, now: float) -> str:
    parsed = date.fromisoformat(day)
    today, tomorrow = calendar_days(now)
    prefix = "Aujourd'hui" if day == today else "Demain" if day == tomorrow else ""
    return (prefix + " " if prefix else "") + parsed.strftime("(%d/%m)")


def department_label(keys: list[str], now: float) -> str:
    groups: dict[str | None, list[str]] = {}
    for key in keys:
        code, day = split_key(key)
        groups.setdefault(day, []).append(code)
    return " ; ".join(
        "/".join(sorted(codes)) + (" " + day_label(day, now) if day else "") for day, codes in groups.items()
    )


def flatten(infos: dict[str, dict[str, Any]], include_tomorrow: bool) -> dict[str, dict[str, Any]]:
    if not include_tomorrow:
        return infos
    return {
        period_key(code, period["period_date"]): period
        for code, info in infos.items()
        for period in info.get("periods", [])
    }


def _epoch(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("Invalid time window")
    if isinstance(value, (int, float)) and math.isfinite(value):
        return float(value)
    if isinstance(value, str):
        if value.isdigit():
            return float(value)
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            return parsed.timestamp()
    raise ValueError("Invalid time window")


def attach_period(info: dict[str, Any], day: str, timelaps: list[dict[str, Any]]) -> dict[str, Any]:
    """Require a dated source; never label an old J1 as the new calendar tomorrow."""
    parsed = date.fromisoformat(day)
    begin = datetime.combine(parsed, time.min, PARIS).timestamp()
    end = datetime.combine(parsed + timedelta(days=1), time.min, PARIS).timestamp()
    supplied_end = info.get("valid_until")
    if supplied_end is None or datetime.fromtimestamp(_epoch(supplied_end) - 1, PARIS).date() != parsed:
        raise ValueError("Données de vigilance indisponibles : journée du bulletin incorrecte")
    windows = []
    for phenomenon in timelaps:
        for step in phenomenon.get("timelaps_items") or []:
            color = str(step.get("color_id", ""))
            if color not in {"1", "2", "3", "4"}:
                raise ValueError("Invalid time-window color")
            if color == "1" or step.get("begin_time") is None or step.get("end_time") is None:
                continue
            start, finish = _epoch(step["begin_time"]), _epoch(step["end_time"])
            if finish <= start:
                raise ValueError("Invalid time-window order")
            start, finish = max(start, begin), min(finish, end)
            if start < finish:
                windows.append(
                    {"id": str(phenomenon["phenomenon_id"]), "color_id": int(color), "begin": start, "end": finish}
                )
    ordered = sorted(windows, key=lambda w: (w["id"], w["begin"], w["end"], w["color_id"]))
    merged: list[dict[str, Any]] = []
    for window in ordered:
        if (
            merged
            and merged[-1]["id"] == window["id"]
            and merged[-1]["color_id"] == window["color_id"]
            and merged[-1]["end"] >= window["begin"]
        ):
            merged[-1]["end"] = max(merged[-1]["end"], window["end"])
        else:
            merged.append(window)
    return {**info, "period_date": day, "period_begin": begin, "windows": merged}


def risk_lines(info: dict[str, Any], names: dict[str, str]) -> list[str]:
    lines = []
    for item in info.get("phenomena", []):
        label = names.get(item["id"], "Phénomène " + item["id"])
        windows = [window for window in info.get("windows", []) if window["id"] == item["id"]]
        if not windows:
            lines.append(f"{label} : {item['color'].lower()}")
            continue
        merged: list[dict[str, Any]] = []
        for window in windows:
            if merged and merged[-1]["color_id"] == window["color_id"] and merged[-1]["end"] >= window["begin"]:
                merged[-1]["end"] = max(merged[-1]["end"], window["end"])
            else:
                merged.append(dict(window))
        for window in merged:
            color = {2: "jaune", 3: "orange", 4: "rouge"}[window["color_id"]]
            start = datetime.fromtimestamp(window["begin"], PARIS).strftime("%H:%M")
            end = datetime.fromtimestamp(window["end"], PARIS).strftime("%H:%M")
            if datetime.fromtimestamp(window["end"], PARIS).date().isoformat() != info["period_date"]:
                end = "24:00"
            lines.append(f"{label} : {color} {start}-{end}")
    return lines
