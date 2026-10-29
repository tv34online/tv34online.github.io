#!/usr/bin/env python3
"""Check every channel in an M3U playlist and repair unavailable streams."""

import argparse
import concurrent.futures
import difflib
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_SOURCE_URL = (
    "https://raw.githubusercontent.com/Dimonovich/TV/"
    "Dimonovich/FREE/TV"
)
USER_AGENT = "Mozilla/5.0 (compatible; IPTVPlaylistRepair/2.0)"


def now():
    return datetime.now(timezone.utc).isoformat()


def log(message):
    print(message, flush=True)


def read_text(path):
    return Path(path).read_text(encoding="utf-8-sig")


def fetch_text(url, timeout=30):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def load_config(path):
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(
            f"Config not found: {path}. Add config/providers.json."
        )
    config = json.loads(read_text(config_path))
    if not isinstance(config, dict):
        raise ValueError("providers.json must contain a JSON object")
    for key in ("blocked_url_substrings", "allowed_provider_names"):
        if not isinstance(config.get(key), list):
            raise ValueError(f"providers.json: {key} must be a JSON array")
    config.setdefault("repair_marker", "kanal-ne-rabotaet.live")
    config.setdefault("check", {})
    config.setdefault("matching", {})
    config["blocked_url_substrings"] = [
        str(x).lower() for x in config["blocked_url_substrings"]
    ]
    config["allowed_provider_names"] = [
        str(x).strip().lower() for x in config["allowed_provider_names"]
    ]
    return config


def parse_m3u(text):
    """Keep every non-URL line intact; associate each URL with its channel."""
    lines = text.splitlines()
    preamble = []
    records = []
    pending = []
    current = None

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#EXTINF"):
            if current is not None:
                records.append(current)
            current = {
                "prefix": pending + [line],
                "url": None,
                "suffix": []
            }
            pending = []
        elif current is not None:
            if stripped and not stripped.startswith("#") and current["url"] is None:
                current["url"] = line
            else:
                current["suffix"].append(line)
        else:
            pending.append(line)

    if current is not None:
        records.append(current)

    if records:
        preamble = pending if not records[0]["prefix"] else []
        # Preserve all lines before the first EXTINF, including #EXTM3U.
        first_extinf = next(
            (i for i, line in enumerate(records[0]["prefix"])
             if line.strip().startswith("#EXTINF")),
            len(records[0]["prefix"])
        )
        preamble = records[0]["prefix"][:first_extinf]
        records[0]["prefix"] = records[0]["prefix"][first_extinf:]
    else:
        preamble = pending

    return preamble, records


def render_m3u(preamble, records):
    out = list(preamble)
    for record in records:
        out.extend(record["prefix"])
        if record["url"] is not None:
            out.append(record["url"])
        out.extend(record["suffix"])
    return "\n".join(out).rstrip() + "\n"


def extinf_line(record):
    return next(
        (line for line in record["prefix"] if line.strip().startswith("#EXTINF")),
        ""
    )


def channel_name(record):
    line = extinf_line(record)
    if not line:
        return "Unknown channel"
    # Name is the part after the final comma, not commas in attributes.
    return line.rsplit(",", 1)[-1].strip() or "Unknown channel"


def metadata_text(record):
    return "\n".join(record["prefix"] + record["suffix"])


def group_name(record):
    text = metadata_text(record)
    match = re.search(r'group-title="([^"]*)"', text, flags=re.I)
    return match.group(1).strip() if match else ""


def is_blocked(url, blocked):
    lowered = (url or "").lower()
    return any(item and item in lowered for item in blocked)


def valid_http_url(url):
    try:
        parsed = urlparse((url or "").strip())
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    except ValueError:
        return False


def provider_allowed(record, allowed_names):
    """Only accept candidates whose group or metadata explicitly names an allowed provider."""
    searchable = (metadata_text(record) + " " + group_name(record)).lower()
    return any(name in searchable for name in allowed_names)


def normalize_name(name, suffixes):
    value = (name or "").lower()
    value = value.replace("&", " and ")
    value = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", value)
    value = re.sub(r"[^a-zа-яё0-9]+", " ", value, flags=re.I)
    words = [word for word in value.split() if word not in suffixes]
    return " ".join(words).strip()


def name_similarity(a, b):
    a = re.sub(r"\s+", " ", a).strip()
    b = re.sub(r"\s+", " ", b).strip()
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    # Compare full strings and token sets to tolerate HD/TV suffix differences.
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    at, bt = set(a.split()), set(b.split())
    overlap = len(at & bt) / max(1, len(at | bt))
    return max(ratio, overlap)


def check_stream(url, timeout):
    """Check HTTP reachability and basic response; not a guarantee of playable video."""
    if not valid_http_url(url):
        return False, "invalid_url"
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Range": "bytes=0-1023",
            "Accept": "*/*"
        }
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            if not 200 <= status < 400:
                return False, f"http_{status}"
            body = response.read(1024)
            # Empty body is suspicious, but don't reject a valid 204 response solely
            # for being empty; typical IPTV media endpoints should return content.
            if status != 204 and not body:
                return False, "empty_response"
            return True, f"http_{status}"
    except urllib.error.HTTPError as exc:
        return False, f"http_{exc.code}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {str(exc)[:160]}"


def check_many(urls, timeout, workers):
    results = {}
    unique = list(dict.fromkeys(url for url in urls if url))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(check_stream, url, timeout): url
            for url in unique
        }
        for future in concurrent.futures.as_completed(futures):
            url = futures[future]
            try:
                results[url] = future.result()
            except Exception as exc:
                results[url] = (False, f"check_error: {exc}")
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--source-url", default=DEFAULT_SOURCE_URL)
    parser.add_argument("--output", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--config", default="config/providers.json")
    parser.add_argument("--check", action="store_true",
                        help="Check current and replacement streams (recommended)")
    args = parser.parse_args()

    report = {
        "status": "running",
        "generated_at": now(),
        "input": args.input,
        "source_url": args.source_url,
        "total_channels": 0,
        "checked_channels": 0,
        "working_channels": 0,
        "marked_channels": 0,
        "blocked_channels": 0,
        "unavailable_channels": 0,
        "repaired_channels": 0,
        "unrepaired_channels": 0,
        "errors": [],
        "channels": []
    }
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        config = load_config(args.config)
        check_cfg = config["check"]
        match_cfg = config["matching"]
        timeout = int(check_cfg.get("timeout_seconds", 8))
        workers = max(1, int(check_cfg.get("workers", 12)))
        max_candidates = max(1, int(check_cfg.get("max_candidates_per_channel", 8)))
        marker = str(config["repair_marker"]).lower()
        blocked = config["blocked_url_substrings"]
        allowed = config["allowed_provider_names"]
        suffixes = {
            str(x).lower() for x in match_cfg.get("strip_quality_suffixes", [])
        }
        threshold = float(match_cfg.get("minimum_name_similarity", 0.78))

        log(f"Loading playlist: {args.input}")
        source_text = read_text(args.input)
        preamble, records = parse_m3u(source_text)
        if not records:
            raise ValueError("No #EXTINF channels found in input playlist")
        report["total_channels"] = len(records)
        log(f"Input channels: {len(records)}")

        log(f"Downloading candidate playlist: {args.source_url}")
        candidate_text = fetch_text(args.source_url)
        _, candidates = parse_m3u(candidate_text)
        if not candidates:
            raise ValueError("Candidate playlist contains no #EXTINF channels")
        log(f"Candidate channels downloaded: {len(candidates)}")

        # Build candidate list from explicitly allowed provider metadata only.
        candidate_rows = []
        for candidate in candidates:
            url = (candidate.get("url") or "").strip()
            if not url or not valid_http_url(url) or is_blocked(url, blocked):
                continue
            if not provider_allowed(candidate, allowed):
                continue
            candidate_rows.append({
                "record": candidate,
                "url": url,
                "name": channel_name(candidate),
                "normalized": normalize_name(channel_name(candidate), suffixes)
            })
        log(f"Candidates from allowed providers: {len(candidate_rows)}")

        current_urls = [(r.get("url") or "").strip() for r in records]
        current_urls = [u for u in current_urls if u and valid_http_url(u)]
        stream_results = check_many(current_urls, timeout, workers) if args.check else {}

        replacement_urls = [row["url"] for row in candidate_rows]
        candidate_results = check_many(replacement_urls, timeout, workers) if args.check else {}

        for idx, record in enumerate(records, start=1):
            name = channel_name(record)
            old_url = (record.get("url") or "").strip()
            meta = metadata_text(record).lower()
            marked = marker in old_url.lower() or marker in meta
            blocked_old = bool(old_url) and is_blocked(old_url, blocked)

            if marked:
                report["marked_channels"] += 1
            if blocked_old:
                report["blocked_channels"] += 1

            if not old_url:
                current_ok, current_reason = False, "missing_stream_url"
            elif blocked_old:
                current_ok, current_reason = False, "blocked_source"
            elif marked:
                current_ok, current_reason = False, "user_confirmed_broken"
            elif args.check:
                current_ok, current_reason = stream_results.get(
                    old_url, (False, "not_checked")
                )
            else:
                current_ok, current_reason = True, "not_checked_check_flag_disabled"

            # Every channel is checked when --check is enabled. Marker/blocked URLs
            # always trigger repair even if an HTTP endpoint might respond.
            needs_repair = marked or blocked_old or (args.check and not current_ok)

            if args.check:
                report["checked_channels"] += 1
                if current_ok and not needs_repair:
                    report["working_channels"] += 1

            item = {
                "channel": name,
                "old_url": old_url,
                "status": "unchanged",
                "reason": current_reason,
                "new_url": None
            }

            if not needs_repair:
                report["channels"].append(item)
                continue

            report["unavailable_channels"] += 1
            normalized = normalize_name(name, suffixes)
            ranked = []
            for row in candidate_rows:
                score = name_similarity(
                    normalized,
                    row["normalized"]
                )
                if score >= threshold:
                    ranked.append((score, row))
            ranked.sort(key=lambda pair: pair[0], reverse=True)

            replacement = None
            replacement_reason = "no_matching_allowed_provider_candidate"
            for score, row in ranked[:max_candidates]:
                candidate_url = row["url"]
                if candidate_url == old_url or is_blocked(candidate_url, blocked):
                    continue
                if args.check:
                    is_ok, check_reason = candidate_results.get(
                        candidate_url, (False, "not_checked")
                    )
                    if not is_ok:
                        replacement_reason = f"candidate_unavailable: {check_reason}"
                        continue
                replacement = row
                break

            if replacement:
                record["url"] = replacement["url"]
                item.update({
                    "status": "repaired",
                    "reason": "user_confirmed_broken" if marked
                              else "blocked_source" if blocked_old
                              else "stream_unavailable",
                    "new_url": replacement["url"],
                    "matched_candidate": replacement["name"]
                })
                report["repaired_channels"] += 1
                log(f"[{idx}/{len(records)}] REPAIRED: {name}")
            else:
                item.update({
                    "status": "unrepaired",
                    "reason": "user_confirmed_broken" if marked
                              else "blocked_source" if blocked_old
                              else "stream_unavailable",
                    "error": replacement_reason
                })
                report["unrepaired_channels"] += 1
                log(f"[{idx}/{len(records)}] NO REPLACEMENT: {name}")

            report["channels"].append(item)

        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(render_m3u(preamble, records), encoding="utf-8")

        report["status"] = "success"
        report["finished_at"] = now()
        log(
            "Finished: checked={checked}, repaired={repaired}, "
            "unrepaired={unrepaired}".format(
                checked=report["checked_channels"],
                repaired=report["repaired_channels"],
                unrepaired=report["unrepaired_channels"]
            )
        )
    except Exception as exc:
        report["status"] = "error"
        report["errors"].append(f"{type(exc).__name__}: {exc}")
        report["finished_at"] = now()
        log(f"ERROR: {type(exc).__name__}: {exc}")
    finally:
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8"
        )
        log(f"Report saved: {args.report}")

    if report["status"] != "success":
        sys.exit(1)


if __name__ == "__main__":
    main()