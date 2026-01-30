#!/usr/bin/env python3
"""
discord_daily_events.py

Cron-friendly Discord webhook notifier for date-based events (holidays, birthdays, etc.).

Exit codes:
  0  Success (including "no events today" or "already sent today")
  2  Usage / argument error
  3  Configuration / data error (bad JSON, invalid event records)
  4  Runtime error (network / Discord rejection / filesystem I/O)

Events JSON format:

Top-level may be either:
  - A JSON list of event objects, or
  - A JSON object with an "events" key containing that list.

Each event object may be one of:

1) Fixed date (optionally recurring):
   {
     "type": "holiday" | "birthday" | "event",
     "name": "Canada Day",
     "date": "2026-07-01",
     "recurring": true,                 # if true, year in "date" is ignored (month/day only)
     "message": "🎉 {name}!",           # optional, supports {name} {age} {age_ordinal} {date} {weekday} {year} {emoji}
     "mention": "<@123456789012345678>" # optional (user/role mention string)
   }

2) Month/day (annual recurring by default):
   {
     "type": "birthday",
     "name": "Ada Lovelace",
     "month": 12,
     "day": 10,
     "year": 1815,                      # optional (birth year for birthdays)
     "message": "🎂 Happy birthday, {name}!",
     "mention": "<@123456789012345678>"
   }

Behavior:
- The script computes "today" in the requested IANA timezone (default: America/Toronto).
- If one or more events match today, it posts to the Discord webhook.
- If --state-file is provided, it will not re-send the *same* message content for the same date
  unless --force is used. This makes it safe for cron retries.

Requirements:
- Python 3.9+ (zoneinfo)
- requests
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except Exception:
    ZoneInfo = None  # type: ignore[assignment]

try:
    import requests
except Exception:
    requests = None  # type: ignore[assignment]


# Discord message content hard limit is 2000 characters.
DISCORD_MAX_CONTENT_LEN = 2000

DEFAULT_TZ = "America/Toronto"

_ALLOWED_WEBHOOK_HOST_SUFFIXES = ("discord.com", "discordapp.com")
_WEBHOOK_PATH_RE = re.compile(r"^/api/webhooks/(\d+)/([\w-]+)$")


def _eprint(msg: str) -> None:
    print(msg, file=sys.stderr)


def die(msg: str, code: int) -> "NoReturn":
    _eprint(f"ERROR: {msg}")
    raise SystemExit(code)


def warn(msg: str, *, verbose: bool) -> None:
    if verbose:
        _eprint(f"WARNING: {msg}")


def info(msg: str, *, verbose: bool) -> None:
    if verbose:
        _eprint(msg)


def _parse_iso_date(s: str, *, context: str) -> _dt.date:
    try:
        return _dt.date.fromisoformat(s)
    except ValueError as e:
        die(f"{context}: invalid ISO date '{s}': {e}", 3)


def _ordinal(n: int) -> str:
    # 1 -> 1st, 2 -> 2nd, 3 -> 3rd, 4 -> 4th, 11 -> 11th, 12 -> 12th, 13 -> 13th
    if 10 <= (n % 100) <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _sha256_hex(data: str) -> str:
    h = hashlib.sha256()
    h.update(data.encode("utf-8"))
    return h.hexdigest()


def _validate_webhook_url(url: str) -> None:
    if not url:
        die("Webhook URL is empty", 2)

    try:
        parsed = urlparse(url)
    except Exception as e:
        die(f"Webhook URL parsing failed: {e}", 2)

    if parsed.scheme.lower() != "https":
        die("Webhook URL must use https", 2)

    host = (parsed.hostname or "").lower()
    if not host or not any(host == suf or host.endswith("." + suf) for suf in _ALLOWED_WEBHOOK_HOST_SUFFIXES):
        die(f"Webhook host must be a Discord domain (got '{parsed.netloc}')", 2)

    path = (parsed.path or "").rstrip("/")
    if not _WEBHOOK_PATH_RE.fullmatch(path):
        die("Invalid Discord webhook URL path format", 2)


@dataclass(frozen=True)
class Event:
    kind: str                 # birthday | holiday | event | other
    name: str
    month: int
    day: int
    specific_date: Optional[_dt.date]  # if provided in JSON and not recurring
    recurring: bool
    year: Optional[int]       # birth year for birthday (or informational)
    message: Optional[str]    # custom message template
    mention: str              # mention string (user/role) or empty
    emoji: Optional[str]      # custom emoji override
    index: int                # index in JSON (for error reporting)


def _as_int(v: Any, *, context: str) -> int:
    if isinstance(v, bool):
        die(f"{context}: expected integer, got boolean", 3)
    if not isinstance(v, int):
        die(f"{context}: expected integer, got {type(v).__name__}", 3)
    return v


def _as_str(v: Any, *, context: str, allow_empty: bool = False) -> str:
    if not isinstance(v, str):
        die(f"{context}: expected string, got {type(v).__name__}", 3)
    s = v.strip()
    if not allow_empty and not s:
        die(f"{context}: string is empty", 3)
    return s


def _as_bool(v: Any, *, context: str) -> bool:
    if not isinstance(v, bool):
        die(f"{context}: expected boolean, got {type(v).__name__}", 3)
    return v


def _normalize_event(obj: dict[str, Any], index: int, *, verbose: bool) -> Optional[Event]:
    ctx = f"events[{index}]"

    kind = str(obj.get("type", "event")).strip().lower()
    if not kind:
        kind = "event"

    name = obj.get("name")
    if name is None:
        die(f"{ctx}: missing required field 'name'", 3)
    name_s = _as_str(name, context=f"{ctx}.name")

    mention = ""
    if "mention" in obj and obj["mention"] is not None:
        mention = _as_str(obj["mention"], context=f"{ctx}.mention", allow_empty=True)

    message = None
    if "message" in obj and obj["message"] is not None:
        message = _as_str(obj["message"], context=f"{ctx}.message", allow_empty=False)

    emoji = None
    if "emoji" in obj and obj["emoji"] is not None:
        emoji = _as_str(obj["emoji"], context=f"{ctx}.emoji", allow_empty=False)

    year: Optional[int] = None
    if "year" in obj and obj["year"] is not None:
        year = _as_int(obj["year"], context=f"{ctx}.year")

    # Determine date form: specific date or month/day.
    specific_date: Optional[_dt.date] = None
    month: Optional[int] = None
    day: Optional[int] = None
    recurring = False

    if "date" in obj and obj["date"] is not None:
        date_s = _as_str(obj["date"], context=f"{ctx}.date")
        specific_date = _parse_iso_date(date_s, context=f"{ctx}.date")
        recurring = bool(obj.get("recurring", False))
        if "recurring" in obj:
            recurring = _as_bool(obj["recurring"], context=f"{ctx}.recurring")
        month = specific_date.month
        day = specific_date.day
        if recurring:
            # Ignore year portion of specific_date for matching purposes.
            specific_date = None
    else:
        if "month" not in obj or "day" not in obj:
            die(f"{ctx}: must contain either 'date' or both 'month' and 'day'", 3)
        month = _as_int(obj["month"], context=f"{ctx}.month")
        day = _as_int(obj["day"], context=f"{ctx}.day")
        recurring = True
        if "recurring" in obj:
            recurring = _as_bool(obj["recurring"], context=f"{ctx}.recurring")

    assert month is not None and day is not None

    if not (1 <= month <= 12):
        die(f"{ctx}: month out of range (1-12): {month}", 3)
    if not (1 <= day <= 31):
        die(f"{ctx}: day out of range (1-31): {day}", 3)

    # Validate month/day combination for *some* year. Use a leap year baseline to allow Feb 29.
    baseline_year = 2024  # leap year for validation
    try:
        _dt.date(baseline_year, month, day)
    except ValueError as e:
        die(f"{ctx}: invalid month/day combination: {e}", 3)

    # For birthdays, year is typically a birth year. Sanity check but do not over-reject.
    if kind == "birthday" and year is not None:
        current_year = _dt.date.today().year
        if year < 1900 or year > current_year:
            warn(f"{ctx}: birthday year '{year}' looks odd; keeping it anyway", verbose=verbose)

    return Event(
        kind=kind,
        name=name_s,
        month=month,
        day=day,
        specific_date=specific_date,
        recurring=recurring,
        year=year,
        message=message,
        mention=mention,
        emoji=emoji,
        index=index,
    )


def _load_events(events_path: Path, *, verbose: bool) -> List[Event]:
    if not events_path.exists():
        die(f"Events file not found: {events_path}", 3)
    if not events_path.is_file():
        die(f"Events path is not a file: {events_path}", 3)

    try:
        raw_text = events_path.read_text(encoding="utf-8")
    except OSError as e:
        die(f"Failed to read events file '{events_path}': {e}", 4)

    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError as e:
        die(f"Events file '{events_path}' is not valid JSON: {e}", 3)

    if isinstance(raw, dict):
        if "events" not in raw:
            die(f"Events JSON object must contain an 'events' list (file: {events_path})", 3)
        raw_events = raw["events"]
    else:
        raw_events = raw

    if not isinstance(raw_events, list):
        die(f"Events JSON must be a list (file: {events_path})", 3)

    events: List[Event] = []
    for i, item in enumerate(raw_events):
        if not isinstance(item, dict):
            warn(f"events[{i}]: expected object, got {type(item).__name__}; skipping", verbose=verbose)
            continue
        ev = _normalize_event(item, i, verbose=verbose)
        if ev is not None:
            events.append(ev)

    return events


def _today_in_tz(tz_name: str, *, override_date: Optional[str]) -> _dt.date:
    if override_date is not None:
        return _parse_iso_date(override_date, context="--date")

    if ZoneInfo is None:
        die("zoneinfo is unavailable; require Python 3.9+ (or install backports.zoneinfo and adapt code)", 2)

    try:
        tz = ZoneInfo(tz_name)
    except Exception as e:
        die(f"Invalid timezone '{tz_name}': {e}", 2)

    now = _dt.datetime.now(tz=tz)
    return now.date()


def _event_matches_today(ev: Event, today: _dt.date) -> bool:
    if ev.specific_date is not None:
        return ev.specific_date == today
    return (ev.month == today.month) and (ev.day == today.day)


def _default_emoji(kind: str) -> str:
    return {
        "birthday": "🎂",
        "holiday": "🎉",
        "event": "📌",
    }.get(kind, "📌")


def _render_event_message(ev: Event, today: _dt.date) -> str:
    emoji = ev.emoji if ev.emoji is not None else _default_emoji(ev.kind)

    age: Optional[int] = None
    if ev.kind == "birthday" and ev.year is not None:
        age = today.year - ev.year
        if age < 0:
            age = None

    weekday = today.strftime("%A")
    date_iso = today.isoformat()

    # If user provided a template, format it.
    if ev.message is not None:
        mapping = {
            "name": ev.name,
            "age": "" if age is None else str(age),
            "age_ordinal": "" if age is None else _ordinal(age),
            "date": date_iso,
            "weekday": weekday,
            "year": "" if ev.year is None else str(ev.year),
            "emoji": emoji,
        }
        try:
            rendered = ev.message.format_map(mapping).strip()
        except KeyError as e:
            die(f"events[{ev.index}].message references unknown placeholder {e!s}", 3)
        if not rendered:
            die(f"events[{ev.index}].message rendered to empty content", 3)
        msg = rendered
    else:
        # Generate a sensible default.
        if ev.kind == "birthday":
            if age is None:
                msg = f"{emoji} Happy birthday, {ev.name}!"
            else:
                msg = f"{emoji} Happy {_ordinal(age)} birthday, {ev.name}!"
        elif ev.kind == "holiday":
            msg = f"{emoji} {ev.name}."
        else:
            msg = f"{emoji} {ev.name}."

    mention = ev.mention.strip()
    if mention:
        msg = f"{mention} {msg}"

    return msg


def _split_discord_content(lines: Sequence[str], max_len: int) -> List[str]:
    if max_len <= 0:
        die("--max-content-len must be > 0", 2)

    chunks: List[str] = []
    current = ""

    for line in lines:
        ln = line.replace("\r\n", "\n").replace("\r", "\n").rstrip()
        if not ln:
            continue

        candidate = ln if not current else current + "\n" + ln

        if len(candidate) <= max_len:
            current = candidate
            continue

        if current:
            chunks.append(current)
            current = ln
            if len(current) <= max_len:
                continue

        while len(current) > max_len:
            chunks.append(current[:max_len])
            current = current[max_len:]

    if current:
        chunks.append(current)

    return chunks


def _read_state(state_path: Path, *, verbose: bool) -> Optional[Tuple[str, str]]:
    try:
        text = state_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError as e:
        warn(f"Failed to read state file '{state_path}': {e} (continuing without state)", verbose=verbose)
        return None

    if not text:
        return None

    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            last_sent = str(obj.get("last_sent", "")).strip()
            sha = str(obj.get("sha256", "")).strip()
            if last_sent and sha:
                return (last_sent, sha)
    except json.JSONDecodeError:
        pass

    warn(f"State file '{state_path}' is not in expected JSON format; ignoring it", verbose=verbose)
    return None


def _write_state(state_path: Path, last_sent: str, sha256_hex: str) -> None:
    state_obj = {"last_sent": last_sent, "sha256": sha256_hex}
    tmp_path = state_path.with_suffix(state_path.suffix + ".tmp")
    try:
        tmp_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path.write_text(json.dumps(state_obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp_path, state_path)
    except OSError as e:
        die(f"Failed to write state file '{state_path}': {e}", 4)
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)  # type: ignore[arg-type]
        except Exception:
            pass


def _http_get_verify(url: str, *, timeout: Tuple[float, float], verbose: bool) -> None:
    assert requests is not None
    headers = {"User-Agent": "discord-daily-events/1.0"}
    try:
        r = requests.get(url, headers=headers, timeout=timeout)
    except requests.RequestException as e:
        die(f"Webhook verification GET failed: {e}", 4)

    if r.status_code != 200:
        die(f"Webhook verification failed: HTTP {r.status_code} - {r.text[:500]}", 4)

    info("Webhook verification GET succeeded.", verbose=verbose)


def _http_post_discord(url: str, content: str, *, timeout: Tuple[float, float], retries: int, verbose: bool) -> None:
    assert requests is not None

    headers = {"User-Agent": "discord-daily-events/1.0"}
    payload = {"content": content}

    last_exc: Optional[str] = None
    for attempt in range(retries + 1):
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=timeout)
        except requests.RequestException as e:
            last_exc = str(e)
            if attempt >= retries:
                die(f"HTTP request failed after {retries + 1} attempts: {last_exc}", 4)
            backoff = min(2 ** attempt, 8)
            info(f"POST failed (attempt {attempt + 1}/{retries + 1}): {e}; retrying in {backoff}s", verbose=verbose)
            time.sleep(backoff)
            continue

        if r.status_code in (200, 204):
            return

        if r.status_code == 429:
            retry_after = 1.0
            try:
                data = r.json()
                ra = data.get("retry_after", None)
                if isinstance(ra, (int, float)) and ra > 0:
                    retry_after = float(ra)
            except Exception:
                pass

            if attempt >= retries:
                die(f"Discord rate-limited (HTTP 429) and retries exhausted; retry_after={retry_after}", 4)

            info(f"Discord rate-limited (HTTP 429); retrying in {retry_after}s", verbose=verbose)
            time.sleep(retry_after)
            continue

        if 500 <= r.status_code < 600 and attempt < retries:
            backoff = min(2 ** attempt, 8)
            info(f"Discord server error HTTP {r.status_code}; retrying in {backoff}s", verbose=verbose)
            time.sleep(backoff)
            continue

        body = (r.text or "").strip()
        if len(body) > 800:
            body = body[:800] + "...(truncated)"
        die(f"Discord rejected request: HTTP {r.status_code} - {body}", 4)

    die("Unreachable: HTTP retry loop fell through", 4)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Cron-friendly Discord webhook notifier for daily events.")
    p.add_argument("--webhook", required=True, help="Discord webhook URL (https://discord.com/api/webhooks/...)")
    p.add_argument("--events-file", required=True, help="Path to JSON file containing events")
    p.add_argument("--tz", default=DEFAULT_TZ, help=f"IANA timezone name (default: {DEFAULT_TZ})")
    p.add_argument("--date", default=None, help="Override today's date for testing (YYYY-MM-DD)")
    p.add_argument("--dry-run", action="store_true", help="Do not post to Discord; print message(s) to stdout")
    p.add_argument("--verbose", action="store_true", help="Enable verbose diagnostics to stderr")
    p.add_argument("--split-messages", action="store_true", help="Send one Discord message per event (default: combined)")
    p.add_argument("--state-file", default=None, help="Path to state JSON file for idempotency (recommended for cron)")
    p.add_argument("--force", action="store_true", help="Ignore state file and send anyway")
    p.add_argument("--verify-webhook", action="store_true", help="Perform a GET to verify webhook URL before posting")
    p.add_argument("--retries", type=int, default=2, help="Number of POST retries on transient failures (default: 2)")
    p.add_argument("--connect-timeout", type=float, default=3.0, help="HTTP connect timeout seconds (default: 3.0)")
    p.add_argument("--read-timeout", type=float, default=10.0, help="HTTP read timeout seconds (default: 10.0)")
    p.add_argument("--max-content-len", type=int, default=DISCORD_MAX_CONTENT_LEN, help="Max Discord content length (default: 2000)")
    args = p.parse_args(argv)

    if sys.version_info < (3, 9):
        die("Python 3.9+ is required (zoneinfo)", 2)
    if requests is None:
        die("Missing dependency 'requests' (pip install requests)", 2)

    args.webhook = args.webhook.strip()
    _validate_webhook_url(args.webhook)

    events_path = Path(args.events_file).expanduser()
    args.events_file = str(events_path)

    if args.state_file is not None:
        args.state_file = str(Path(args.state_file).expanduser())

    if args.retries < 0:
        die("--retries must be >= 0", 2)
    if args.connect_timeout <= 0 or args.read_timeout <= 0:
        die("--connect-timeout and --read-timeout must be > 0", 2)
    if args.max_content_len <= 0:
        die("--max-content-len must be > 0", 2)

    return args


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)

    today = _today_in_tz(args.tz, override_date=args.date)
    events = _load_events(Path(args.events_file), verbose=args.verbose)

    todays_events = [ev for ev in events if _event_matches_today(ev, today)]

    if not todays_events:
        info(f"No matching events for {today.isoformat()} in timezone {args.tz}.", verbose=args.verbose)
        return 0

    lines = [_render_event_message(ev, today) for ev in todays_events]
    combined_for_state = "\n".join(lines)

    if args.split_messages:
        message_units: List[str] = []
        for ln in lines:
            message_units.extend(_split_discord_content([ln], args.max_content_len))
    else:
        message_units = _split_discord_content(lines, args.max_content_len)

    if args.dry_run:
        # Print exactly what would be sent (ignores --state-file).
        for i, msg in enumerate(message_units):
            if i:
                print("\n---\n")
            print(msg)
        return 0

    # Idempotency check (optional)
    state_path: Optional[Path] = Path(args.state_file) if args.state_file else None
    digest = _sha256_hex(combined_for_state)
    if state_path is not None and not args.force:
        st = _read_state(state_path, verbose=args.verbose)
        if st is not None:
            last_sent_date, last_sent_sha = st
            if last_sent_date == today.isoformat() and last_sent_sha == digest:
                info("State indicates today's message already sent; exiting.", verbose=args.verbose)
                return 0

    timeout = (float(args.connect_timeout), float(args.read_timeout))

    if args.verify_webhook:
        _http_get_verify(args.webhook, timeout=timeout, verbose=args.verbose)

    for msg in message_units:
        if not msg.strip():
            continue
        _http_post_discord(args.webhook, msg, timeout=timeout, retries=int(args.retries), verbose=args.verbose)

    if state_path is not None:
        _write_state(state_path, today.isoformat(), digest)

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        die("Interrupted", 4)
