#!/usr/bin/env python3
"""Repair marked/banned IPTV entries using same-name streams from approved providers."""
from __future__ import annotations
import argparse, json, os, re, sys
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(ROOT, "config", "providers.json")
REPAIR_MARKER = "kanal-ne-rabotaet.live"

def norm(s):
    return re.sub(r"[^a-zа-я0-9]+", "", (s or "").lower(), flags=re.I)

def parse_playlist(text):
    header = "#EXTM3U"
    entries, pending = [], []
    for raw in text.replace("\ufeff", "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTM3U"):
            header = line
        elif line.startswith(("http://", "https://")):
            entries.append({"lines": pending[:], "url": line})
            pending = []
        else:
            pending.append(line)
    return header, entries

def serialize_entry(entry):
    return "\n".join(entry["lines"] + [entry["url"]])

def channel_name(entry):
    for line in entry["lines"]:
        if line.startswith("#EXTINF"):
            # Name is the text after the final comma; attributes may contain commas,
            # so rsplit is safer than split.
            return line.rsplit(",", 1)[-1].strip()
    return ""

def provider_for(entry, cfg):
    blob = serialize_entry(entry)
    n = norm(blob)
    # Always deny these markers, even if another provider marker is also present.
    if any(norm(x) in n for x in cfg["blocked_markers"]):
        return None
    matches = [provider for provider, markers in cfg["allowed_providers"].items()
               if any(norm(marker) in n for marker in markers)]
    return matches[0] if len(matches) == 1 else None

def needs_repair(entry, cfg):
    blob = serialize_entry(entry)
    n = norm(blob)
    return (REPAIR_MARKER in n or
            any(norm(x) in n for x in cfg["blocked_markers"]))

def check_stream(url, timeout=8):
    try:
        req = Request(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; TV34PlaylistChecker/1.1)",
            "Range": "bytes=0-1023", "Accept": "*/*"
        })
        with urlopen(req, timeout=timeout) as resp:
            sample = resp.read(1024)
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if getattr(resp, "status", 200) >= 400 or not sample:
                return False, "HTTP error or empty response"
            if "text/html" in ctype and b"#EXTM3U" not in sample:
                return False, "HTML response, not an HLS manifest"
            return True, "HTTP response received (not proof of playback)"
    except HTTPError as e:
        return False, f"HTTP {e.code}"
    except (URLError, TimeoutError, OSError) as e:
        return False, type(e).__name__

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="playlist_source.m3u")
    ap.add_argument("--output", default="playlist.m3u")
    ap.add_argument("--report", default="playlist-report.json")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--timeout", type=int, default=8)
    args = ap.parse_args()

    with open(CONFIG, encoding="utf-8") as f:
        cfg = json.load(f)
    with open(args.input, encoding="utf-8-sig") as f:
        source_text = f.read()
    header, entries = parse_playlist(source_text)

    # Candidate pool: only explicitly identified approved providers; never use blocked sources.
    candidates = []
    for idx, entry in enumerate(entries):
        provider = provider_for(entry, cfg)
        if provider:
            candidates.append((idx, entry, provider))

    # Build replacement index by normalized channel name.
    by_name = {}
    for idx, entry, provider in candidates:
        name = norm(channel_name(entry))
        if name:
            by_name.setdefault(name, []).append((idx, entry, provider))

    output_entries = []
    report = {"input": args.input, "output": args.output, "repaired": [],
              "unrepaired": [], "blocked_sources": [], "checked": bool(args.check)}
    for idx, entry in enumerate(entries):
        if not needs_repair(entry, cfg):
            output_entries.append(entry)
            continue

        name = channel_name(entry)
        name_key = norm(name)
        old_url = entry["url"]
        # Only same-name alternatives from approved providers are considered.
        options = [(ci, ce, cp) for ci, ce, cp in by_name.get(name_key, []) if ci != idx]
        replacement = None
        failures = []
        for ci, cand, provider in options:
            url = cand["url"]
            if REPAIR_MARKER in norm(url):
                continue
            if args.check:
                ok, reason = check_stream(url, args.timeout)
                if not ok:
                    failures.append({"url": url, "reason": reason})
                    continue
            replacement = (url, provider)
            break

        repaired_entry = {"lines": entry["lines"][:], "url": old_url}
        if replacement:
            repaired_entry["url"] = replacement[0]
            report["repaired"].append({"channel": name, "old_url": old_url,
                                       "new_url": replacement[0], "provider": replacement[1]})
        else:
            blob = serialize_entry(entry)
            if any(norm(x) in norm(blob) for x in cfg["blocked_markers"]):
                # Do not publish a known blocked provider URL. Keep the channel metadata
                # and leave an explicit repair marker so it can be revisited later.
                repaired_entry["url"] = "https://" + REPAIR_MARKER + "/"
                report["blocked_sources"].append({"channel": name, "old_url": old_url,
                                                   "action": "blocked URL replaced with repair marker"})
            report["unrepaired"].append({"channel": name, "old_url": old_url,
                                         "reason": "no verified same-name approved alternative",
                                         "candidate_failures": failures})
        output_entries.append(repaired_entry)

    if not output_entries:
        print("ERROR: playlist has no entries; output not written.", file=sys.stderr)
        return 2
    with open(args.output, "w", encoding="utf-8", newline="\n") as f:
        f.write(header + "\n")
        for entry in output_entries:
            for line in entry["lines"]:
                f.write(line + "\n")
            f.write(entry["url"] + "\n")
    report["input_entries"] = len(entries)
    report["output_entries"] = len(output_entries)
    with open(args.report, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"Готово: каналов {len(output_entries)}, заменено {len(report['repaired'])}, без замены {len(report['unrepaired'])}.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())