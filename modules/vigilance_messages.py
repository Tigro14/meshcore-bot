"""Compact radio summaries; official risks, levels and time windows stay exact."""

from collections import defaultdict
from datetime import date, datetime
from typing import Any

from modules.vigilance_periods import PARIS, calendar_days, split_key

AlertEvent = tuple[str, dict[str, Any], str]
_COLORS = {2: 'Jaune', 3: 'Orange', 4: 'Rouge'}


def _day(day: str | None, now: float) -> str:
    if not day:
        return ''
    prefix = 'Demain ' if day == calendar_days(now)[1] else ''
    return prefix + date.fromisoformat(day).strftime('%d/%m')


def _time(at: float, day: str | None, *, end: bool = False) -> str:
    value = datetime.fromtimestamp(at, PARIS)
    if end and day and value.date().isoformat() > day and value.hour == value.minute == 0:
        return '24h'
    return f'{value.hour}h{value.minute:02d}' if value.minute else str(value.hour) + ('h' if end else '')


def _ranges(windows: list[tuple[float, float]], day: str | None) -> tuple[str, ...]:
    merged: list[tuple[float, float]] = []
    for start, end in sorted(windows):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return tuple(_time(start, day) + '-' + _time(end, day, end=True) for start, end in merged)


def _fits(text: str, budget: int) -> bool:
    return len(text.encode('utf-8')) <= budget


def _fragments(prefix: str, codes: list[str], ranges: tuple[str, ...], budget: int) -> list[str]:
    """Split oversized department/time lists without cutting a risk or interval."""
    suffix = ':' + ','.join(ranges) if ranges else ''
    if _fits(prefix + ' ' + '/'.join(codes) + suffix, budget):
        return ['/'.join(codes) + suffix]
    if len(codes) > 1:
        pieces: list[str] = []
        current: list[str] = []
        for code in codes:
            if current and not _fits(prefix + ' ' + '/'.join([*current, code]) + suffix, budget):
                pieces.extend(_fragments(prefix, current, ranges, budget))
                current = []
            current.append(code)
        return [*pieces, *_fragments(prefix, current, ranges, budget)]
    if len(ranges) > 1:
        return [piece for interval in ranges for piece in _fragments(prefix, codes, (interval,), budget)]
    raise ValueError('A vigilance risk does not fit the radio budget')


def compact_alerts(events: list[AlertEvent], names: dict[str, str], now: float, budget: int) -> list[str]:
    """One day/overall level per batch; lower phenomenon levels remain explicit.

    Only events due for transmission belong here. Each returned message repeats
    its date/level header, and fits the sender's actual UTF-8 body budget.
    """
    info = events[0][1]
    color, day = info['color'], info.get('period_date')
    if any(event[1]['color'] != color or event[1].get('period_date') != day for event in events):
        raise ValueError('Different dates or overall levels need separate batches')
    codes = sorted({split_key(key)[0] for key, _, _ in events})
    label = _day(day, now)
    if color == 'Vert':
        header = '✅ Fin de vigilance' + (' ' + label if label else '')
        lines = ['/'.join(codes) + ' : retour au vert.']
    else:
        emoji = {'Jaune': '⚠️', 'Orange': '🟠', 'Rouge': '🔴'}[color]
        reminder = 'Rappel ' if all(reason == 'reminder' for _, _, reason in events) else ''
        header = f'{emoji} {reminder}Vigilance {color.upper()}' + (' ' + label if label else '')
        risks: dict[tuple[str, str], dict[str, list[tuple[float, float]]]] = defaultdict(dict)
        for key, data, _ in events:
            if not isinstance(data.get('phenomena'), list) or not data['phenomena']:
                raise ValueError('No structured phenomena to condense')
            code = split_key(key)[0]
            for item in data['phenomena']:
                pid = str(item['id'])
                windows = [w for w in data.get('windows', []) if str(w['id']) == pid]
                if windows:
                    for window in windows:
                        level = _COLORS[window['color_id']]
                        risks[(pid, level)].setdefault(code, []).append((window['begin'], window['end']))
                else:
                    risks[(pid, item['color'])][code] = []
        room = budget - len(header.encode('utf-8')) - 1
        lines = []
        priority = {'Rouge': 0, 'Orange': 1, 'Jaune': 2}
        for (pid, level), departments in sorted(risks.items(), key=lambda item: (priority[item[0][1]], item[0][0])):
            prefix = names.get(pid, 'Phénomène ' + pid) + (' ' + level.lower() if level != color else '')
            schedules: dict[tuple[str, ...], list[str]] = defaultdict(list)
            for code, windows in departments.items():
                schedules[_ranges(windows, day)].append(code)
            line = prefix
            for ranges, members in sorted(schedules.items(), key=lambda item: sorted(item[1])):
                for fragment in _fragments(prefix, sorted(members), ranges, room):
                    candidate = line + (' ' if line == prefix else '; ') + fragment
                    if not _fits(candidate, room):
                        lines.append(line)
                        line = prefix + ' ' + fragment
                    else:
                        line = candidate
            lines.append(line)
    messages, current = [], header
    for line in lines:
        if not _fits(header + '\n' + line, budget):
            raise ValueError('A vigilance line does not fit the radio budget')
        if not _fits(current + '\n' + line, budget):
            messages.append(current)
            current = header
        current += '\n' + line
    return [*messages, current]
