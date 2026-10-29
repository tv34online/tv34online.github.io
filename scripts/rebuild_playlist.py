#!/usr/bin/env python3

import argparse
import json
import re
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse


DEFAULT_SOURCE_URL = (
    "https://raw.githubusercontent.com/"
    "Dimonovich/TV/Dimonovich/FREE/TV"
)

DEFAULT_BLOCKED = [
    "mhd116.iptv2022.com",
    "arbiter.bolshoe.tv",
    "limehd.ru",
    "limehd",
    "lime",
    "bolshoe.tv",
]

REPAIR_MARKER = "kanal-ne-rabotaet.live"

ALLOWED_PROVIDERS = [
    "WINK",
    "Ott Club + Реклама",
    "RUTUBE (VPN)",
    "Оргтехсервис",
]


def log(message):
    print(message, flush=True)


def read_text(path):
    return Path(path).read_text(encoding="utf-8-sig")


def fetch_url(url, timeout=30):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 IPTV-Playlist-Repair/1.0"
        },
    )

    with urllib.request.urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def load_config(config_path):
    config_file = Path(config_path)

    if not config_file.exists():
        log(f"WARNING: Config not found: {config_path}")
        return {"blocked_url_substrings": DEFAULT_BLOCKED}

    try:
        config = json.loads(read_text(config_file))
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError(
            f"Cannot read config {config_path}: {exc}"
        ) from exc

    if not isinstance(config, dict):
        raise ValueError("providers.json must contain a JSON object")

    blocked = config.get("blocked_url_substrings", DEFAULT_BLOCKED)

    if not isinstance(blocked, list):
        raise ValueError(
            "'blocked_url_substrings' must be a JSON array"
        )

    config["blocked_url_substrings"] = [
        str(item).lower() for item in blocked
    ]

    return config


def is_blocked(url, blocked_substrings):
    lowered = url.lower()
    return any(item in lowered for item in blocked_substrings)


def parse_playlist(text):
    """
    Разбирает M3U, сохраняя строки и метаданные.
    Каждый канал содержит EXTINF и следующие за ним строки до URL.
    """
    lines = text.splitlines()
    channels = []
    preamble = []
    pending = []
    current = None

    for line in lines:
        stripped = line.strip()

        if stripped.startswith("#EXTINF"):
            if current is not None:
                channels.append(current)

            current = {
                "metadata": pending + [line],
                "url": None,
                "extra": [],
            }
            pending = []
            continue

        if current is not None:
            if stripped and not stripped.startswith("#"):
                if current["url"] is None:
                    current["url"] = line
                else:
                    current["extra"].append(line)
            else:
                current["extra"].append(line)
            continue

        if pending or stripped.startswith("#"):
            pending.append(line)
        else:
            preamble.append(line)

    if current is not None:
        channels.append(current)

    # Оставляем комментарии и служебные строки перед первым каналом.
    if channels and channels[0]["metadata"]:
        first_extinf = next(
            (
                i for i, line in enumerate(channels[0]["metadata"])
                if line.strip().startswith("#EXTINF")
            ),
            len(channels[0]["metadata"]),
        )
        preamble.extend(channels[0]["metadata"][:first_extinf])
        channels[0]["metadata"] = channels[0]["metadata"][first_extinf:]

    return preamble, channels


def channel_name(channel):
    for line in channel["metadata"]:
        if line.strip().startswith("#EXTINF"):
            return line.rsplit(",", 1)[-1].strip()
    return "Unknown channel"


def channel_group(channel):
    for line in channel["metadata"]:
        match = re.search(r'group-title="([^"]*)"', line)
        if match:
            return match.group(1)
    return ""


def candidate_provider(channel):
    name = channel_name(channel).lower()
    group = channel_group(channel).lower()
    combined = f"{name} {group}"

    for provider in ALLOWED_PROVIDERS:
        if provider.lower() in combined:
            return provider

    return None


def normalize_key(value):
    value = value.lower().strip()
    value = re.sub(r"\s+", " ", value)
    return value


def is_http_url(url):
    try:
        parsed = urlparse(url.strip())
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    except ValueError:
        return False


def build_candidate_index(channels, blocked):
    index = {}

    for channel in channels:
        url = channel.get("url")

        if not url or not is_http_url(url):
            continue

        if is_blocked(url, blocked):
            continue

        name = normalize_key(channel_name(channel))
        if name and name != "unknown channel":
            index.setdefault(name, []).append(url.strip())

    return index


def check_stream(url, timeout=8):
    """
    Проверяет доступность URL.
    HTTP-ответы 200–399 считаются доступными.
    Некоторые IPTV-серверы блокируют HEAD, поэтому используется GET
    с Range-заголовком.
    """
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 IPTV-Playlist-Repair/1.0",
            "Range": "bytes=0-0",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return 200 <= response.status < 400, f"HTTP {response.status}"

    except urllib.error.HTTPError as exc:
        # Сервер может отвечать 403/405 на короткий запрос,
        # поэтому такой поток не признаётся рабочим.
        return False, f"HTTP {exc.code}"

    except Exception as exc:
        return False, str(exc)[:200]


def render_playlist(preamble, channels):
    output = list(preamble)

    for channel in channels:
        output.extend(channel["metadata"])

        if channel["url"] is not None:
            output.append(channel["url"])

        output.extend(channel["extra"])

    return "\n".join(output).rstrip() + "\n"


def main():
    parser = argparse.ArgumentParser(
        description="Repair IPTV playlist streams"
    )

    parser.add_argument("--input", required=True)
    parser.add_argument("--source-url", default=DEFAULT_SOURCE_URL)
    parser.add_argument("--output", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--config", default="config/providers.json")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--timeout", type=int, default=8)

    args = parser.parse_args()

    report = {
        "status": "running",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "input": args.input,
        "source_url": args.source_url,
        "total_channels": 0,
        "marked_channels": 0,
        "repaired_channels": 0,
        "unrepaired_channels": 0,
        "blocked_streams_replaced": 0,
        "errors": [],
        "channels": [],
    }

    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        log(f"Loading input playlist: {args.input}")
        input_text = read_text(args.input)

        preamble, channels = parse_playlist(input_text)

        if not channels:
            raise ValueError(
                "No #EXTINF channels found in the input playlist"
            )

        report["total_channels"] = len(channels)
        log(f"Channels found: {len(channels)}")

        config = load_config(args.config)
        blocked = config["blocked_url_substrings"]

        log(f"Downloading candidate playlist: {args.source_url}")
        source_text = fetch_url(args.source_url)

        _, source_channels = parse_playlist(source_text)

        if not source_channels:
            raise ValueError(
                "The candidate playlist contains no #EXTINF channels"
            )

        log(f"Candidate channels found: {len(source_channels)}")

        candidate_index = build_candidate_index(
            source_channels,
            blocked,
        )

        for number, channel in enumerate(channels, start=1):
            name = channel_name(channel)
            old_url = (channel.get("url") or "").strip()

            marked = REPAIR_MARKER in old_url.lower()
            blocked_url = bool(old_url) and is_blocked(old_url, blocked)

            if marked:
                report["marked_channels"] += 1

            if not marked and not blocked_url:
                continue

            reason = (
                "marked_for_repair"
                if marked
                else "blocked_source"
            )

            entry = {
                "channel": name,
                "old_url": old_url,
                "status": "unrepaired",
                "reason": reason,
                "new_url": None,
            }

            log(
                f"[{number}/{len(channels)}] Checking: {name} "
                f"({reason})"
            )

            key = normalize_key(name)
            candidates = candidate_index.get(key, [])

            # Сначала проверяем кандидатов с тем же названием.
            replacement = None
            last_error = "No matching candidate stream found"

            for candidate in candidates:
                if candidate == old_url:
                    continue

                if is_blocked(candidate, blocked):
                    continue

                if args.check:
                    working, message = check_stream(
                        candidate,
                        timeout=args.timeout,
                    )
                    if not working:
                        last_error = message
                        continue

                replacement = candidate
                break

            if replacement:
                channel["url"] = replacement
                entry["status"] = "repaired"
                entry["new_url"] = replacement

                report["repaired_channels"] += 1

                if blocked_url:
                    report["blocked_streams_replaced"] += 1

                log(f"  Replaced stream for: {name}")

            else:
                report["unrepaired_channels"] += 1
                entry["error"] = last_error
                log(f"  No verified replacement for: {name}")

            report["channels"].append(entry)

        # Дополнительная проверка: не оставляем заблокированные URL
        # без отметки в отчёте и не удаляем соответствующие каналы.
        for channel in channels:
            url = (channel.get("url") or "").strip()
            if url and is_blocked(url, blocked):
                log(
                    "WARNING: blocked URL remains in playlist: "
                    f"{channel_name(channel)}"
                )

        output_text = render_playlist(preamble, channels)

        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(output_text, encoding="utf-8")

        report["status"] = "success"
        report["finished_at"] = datetime.now(timezone.utc).isoformat()

        log(f"Saved playlist: {args.output}")
        log(f"Repaired: {report['repaired_channels']}")
        log(f"Unrepaired: {report['unrepaired_channels']}")

    except Exception as exc:
        report["status"] = "error"
        report["errors"].append(str(exc))
        report["finished_at"] = datetime.now(timezone.utc).isoformat()

        log(f"ERROR: {exc}")
        print(
            json.dumps(report, ensure_ascii=False, indent=2),
            file=sys.stderr,
        )

    finally:
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        log(f"Saved report: {args.report}")

    if report["status"] != "success":
        sys.exit(1)


if __name__ == "__main__":
    main()