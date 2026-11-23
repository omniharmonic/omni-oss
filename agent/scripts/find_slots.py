#!/usr/bin/env python3
"""
Find available meeting time slots based on calendar events and preferences.

Usage:
    # Self-contained (fetches calendar directly via gog):
    python3 find_slots.py --account owner@example.com --duration 60 --count 5 --days 14

    # Piped from gog (legacy):
    gog calendar events primary --from ... --to ... --json | python3 find_slots.py --duration 60 --count 3

    --account: Google account to fetch calendar from (if provided, fetches directly)
    --duration: Meeting duration in minutes (default: 60)
    --count: Number of slots to return (default: 5)
    --days: Days ahead to search (default: 14)
    --buffer: Buffer minutes between meetings (default: 0 for online, use 15-30 for IRL)
"""

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Tuple
import argparse
from zoneinfo import ZoneInfo
import os
OWNER_TZ = os.environ.get("OMNI_TIMEZONE", "").strip() or "UTC"  # config: the owner's IANA timezone (identity owner.timezone)


def parse_iso_datetime(dt_str: str) -> datetime:
    """Parse ISO 8601 datetime string."""
    if dt_str.endswith('Z'):
        dt_str = dt_str[:-1] + '+00:00'
    return datetime.fromisoformat(dt_str)


def fetch_calendar_events(account: str, days_ahead: int) -> List[Dict]:
    """Fetch calendar events directly via gog CLI."""
    mst = ZoneInfo(OWNER_TZ)
    now = datetime.now(mst)
    end = now + timedelta(days=days_ahead)

    cmd = [
        "gog", "calendar", "events", "primary",
        "--from", now.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "--to", end.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "--max", "200",
        "--json",
        "--account", account,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        print(f"Error fetching calendar: {result.stderr}", file=sys.stderr)
        sys.exit(1)

    try:
        data = json.loads(result.stdout)
        # gog wraps events in {"events": [...]}
        if isinstance(data, dict) and 'events' in data:
            return data['events']
        if isinstance(data, list):
            return data
        print(f"Unexpected calendar JSON structure: {list(data.keys()) if isinstance(data, dict) else type(data)}", file=sys.stderr)
        return []
    except json.JSONDecodeError as e:
        print(f"Error parsing calendar JSON: {e}", file=sys.stderr)
        print(f"Raw output: {result.stdout[:500]}", file=sys.stderr)
        sys.exit(1)


def get_availability_windows() -> List[Tuple[int, int]]:
    """Return daily availability windows as (start_hour, end_hour) tuples."""
    return [(9, 17)]  # 9 AM - 5 PM MST


def is_weekend(dt: datetime) -> bool:
    return dt.weekday() >= 5


def generate_candidate_slots(
    start_date: datetime,
    end_date: datetime,
    duration_minutes: int,
    slot_increment: int = 30
) -> List[Tuple[datetime, datetime]]:
    """Generate all possible time slots within availability windows."""
    slots = []
    tz = start_date.tzinfo if start_date.tzinfo else ZoneInfo(OWNER_TZ)
    current_date = start_date.replace(hour=0, minute=0, second=0, microsecond=0)

    while current_date < end_date:
        if is_weekend(current_date):
            current_date += timedelta(days=1)
            continue

        for start_hour, end_hour in get_availability_windows():
            current_time = current_date.replace(hour=start_hour, minute=0)
            window_end = current_date.replace(hour=end_hour, minute=0)

            while current_time + timedelta(minutes=duration_minutes) <= window_end:
                slot_end = current_time + timedelta(minutes=duration_minutes)

                if slot_end <= start_date:
                    current_time += timedelta(minutes=slot_increment)
                    continue

                slots.append((current_time, slot_end))
                current_time += timedelta(minutes=slot_increment)

        current_date += timedelta(days=1)

    return slots


def slots_overlap(
    slot_start: datetime,
    slot_end: datetime,
    event_start: datetime,
    event_end: datetime,
    buffer_minutes: int = 0
) -> bool:
    """Check if a candidate slot overlaps with a calendar event (with optional buffer)."""
    event_start_buffered = event_start - timedelta(minutes=buffer_minutes)
    event_end_buffered = event_end + timedelta(minutes=buffer_minutes)
    return not (slot_end <= event_start_buffered or slot_start >= event_end_buffered)


def find_available_slots(
    calendar_events: List[Dict],
    duration_minutes: int,
    count: int,
    days_ahead: int,
    buffer_minutes: int = 0,
    spread: bool = False
) -> List[Dict]:
    """Find available meeting slots by filtering calendar conflicts.

    If spread=True, distributes slots across different days and times of day
    (morning/afternoon) to give varied options for scheduling proposals.
    """
    mst = ZoneInfo(OWNER_TZ)
    now = datetime.now(mst)
    end_date = now + timedelta(days=days_ahead)

    candidate_slots = generate_candidate_slots(now, end_date, duration_minutes)

    busy_periods = []
    for event in calendar_events:
        if not event.get('start', {}).get('dateTime') or not event.get('end', {}).get('dateTime'):
            continue
        start = parse_iso_datetime(event['start']['dateTime'])
        end = parse_iso_datetime(event['end']['dateTime'])
        busy_periods.append((start, end))

    all_free = []
    for slot_start, slot_end in candidate_slots:
        conflicts = any(
            slots_overlap(slot_start, slot_end, busy_start, busy_end, buffer_minutes)
            for busy_start, busy_end in busy_periods
        )

        if not conflicts:
            all_free.append({
                'start': slot_start.isoformat(),
                'end': slot_end.isoformat(),
                'start_formatted': slot_start.strftime('%A, %B %d at %I:%M %p MST'),
                'duration_minutes': duration_minutes,
                'day_of_week': slot_start.strftime('%A'),
                'date': slot_start.strftime('%Y-%m-%d'),
                'time_of_day': 'morning' if slot_start.hour < 12 else 'afternoon',
                '_dt': slot_start,
            })

    if not spread:
        # Return first N available (consecutive)
        result = all_free[:count]
        for s in result:
            del s['_dt']
        return result

    # Spread mode: pick slots across different days, alternating morning/afternoon
    selected = []
    used_day_periods = set()  # track (date, time_of_day) pairs used

    # First pass: one slot per day-period combo
    for slot in all_free:
        key = (slot['date'], slot['time_of_day'])
        if key not in used_day_periods:
            used_day_periods.add(key)
            selected.append(slot)
            if len(selected) >= count:
                break

    # Second pass: if not enough, fill from remaining
    if len(selected) < count:
        used_dates = set()
        for slot in all_free:
            if slot not in selected:
                day = slot['date']
                if day not in used_dates or len(selected) < count:
                    used_dates.add(day)
                    selected.append(slot)
                    if len(selected) >= count:
                        break

    for s in selected:
        del s['_dt']
    return selected[:count]


def main():
    parser = argparse.ArgumentParser(description='Find available meeting time slots')
    parser.add_argument('--account', type=str, default=None,
                      help='Google account to fetch calendar from (fetches directly via gog)')
    parser.add_argument('--duration', type=int, default=60,
                      help='Meeting duration in minutes (default: 60)')
    parser.add_argument('--count', type=int, default=5,
                      help='Number of slots to return (default: 5)')
    parser.add_argument('--days', type=int, default=14,
                      help='Days ahead to search (default: 14)')
    parser.add_argument('--buffer', type=int, default=0,
                      help='Buffer minutes between meetings (default: 0 for online, use 15-30 for IRL)')
    parser.add_argument('--stdin', action='store_true',
                      help='Read calendar JSON from stdin instead of fetching via gog')
    parser.add_argument('--spread', action='store_true',
                      help='Spread slots across different days/times (good for proposals)')

    args = parser.parse_args()

    if args.stdin:
        try:
            calendar_events = json.load(sys.stdin)
            # Handle gog's {"events": [...]} wrapper
            if isinstance(calendar_events, dict) and 'events' in calendar_events:
                calendar_events = calendar_events['events']
        except json.JSONDecodeError as e:
            print(json.dumps({'error': f'Invalid JSON input: {e}'}), file=sys.stderr)
            sys.exit(1)
    elif args.account:
        calendar_events = fetch_calendar_events(args.account, args.days)
    else:
        print("Error: Provide --account or --stdin", file=sys.stderr)
        sys.exit(1)

    slots = find_available_slots(
        calendar_events,
        args.duration,
        args.count,
        args.days,
        args.buffer,
        args.spread
    )

    print(json.dumps({
        'available_slots': slots,
        'count': len(slots),
        'search_parameters': {
            'duration_minutes': args.duration,
            'requested_count': args.count,
            'days_ahead': args.days,
            'buffer_minutes': args.buffer
        }
    }, indent=2))


if __name__ == '__main__':
    main()
