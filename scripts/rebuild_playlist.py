#!/usr/bin/env python3
"""Проверка и восстановление IPTV-плейлиста.

Читает playlist_source.m3u, проверяет каждый поток, для неработающих /
запрещённых / принудительно заменяемых подбирает проверенную замену из
альтернативного плейлиста и пишет playlist.m3u + playlist-report.json.

Меняются только URL потоков. Каналы никогда не удаляются.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import difflib
import json
import os
import re
import socket
import ssl
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlsplit, urlunsplit

DEFAULT_ALT_URL = "https://raw.githubusercontent.com/Dimonovich/TV/Dimonovich/FREE/TV"
ATTR_RE = re.compile(r'([A-Za-z0-9_.\-]+)="([^"]*)"')
IGNORED_ATTRS = {"tvg-logo", "tvg-id", "tvg-name", "url-tvg", "x-tvg-url"}
NOISE_TOKENS = {"hd", "fhd", "uhd", "sd", "4k", "hq", "full", "online", "international", "tv"}
QUALITY = {"sd": 1, "hd": 2, "fhd": 3, "uhd": 4, "4k": 4}
SENSITIVE_KEY = re.compile(
    r"(token|key|auth|sign|sig|pass|pwd|secret|session|sid|hash|access|jwt|credential|code)", re.I
)
TOKEN_SEGMENT = re.compile(r"^(?=.*\d)(?=.*[A-Za-z])[A-Za-z0-9_\-]{24,}$")


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# Конфигурация
# --------------------------------------------------------------------------
class ConfigError(Exception):
    pass


DEFAULT_LIMITS = {
    "workers": 16,
    "per_host": 4,
    "timeout": 10,
    "request_deadline": 20,
    "max_candidates": 8,
    "max_runtime": 2700,
    "mass_failure_ratio": 0.95,
    "mass_failure_min_channels": 10,
}


def load_config(path: str | Path) -> dict:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise ConfigError(f"Файл конфигурации не найден: {path}") from e
    except json.JSONDecodeError as e:
        raise ConfigError(f"Некорректный JSON в {path}: {e}") from e
    return normalize_config(raw)


def normalize_config(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise ConfigError("Корень конфигурации должен быть объектом")
    for key in ("allowed_providers", "forbidden_url_fragments", "force_replace_url_fragments"):
        if not isinstance(raw.get(key), list):
            raise ConfigError(f"Поле '{key}' обязательно и должно быть списком")
    providers = []
    for p in raw["allowed_providers"]:
        if isinstance(p, str):
            p = {"name": p, "aliases": [p]}
        if not isinstance(p, dict) or not p.get("name"):
            raise ConfigError("Каждый allowed_providers[] должен иметь 'name'")
        providers.append({"name": p["name"], "aliases": list(p.get("aliases") or [p["name"]])})
    cfg = {
        "alternative_playlist_url": raw.get("alternative_playlist_url") or DEFAULT_ALT_URL,
        "allowed_providers": providers,
        "provider_fields": list(
            raw.get("provider_fields")
            or ["group-title", "extgrp", "attr_values", "directives", "comments"]
        ),
        "forbidden_url_fragments": [str(x) for x in raw["forbidden_url_fragments"]],
        "force_replace_url_fragments": [str(x) for x in raw["force_replace_url_fragments"]],
        "no_replace_dead_channels": [str(x) for x in raw.get("no_replace_dead_channels", [])],
        "paid_markers": [str(x) for x in raw.get("paid_markers", [])],
        "http": {
            "headers": dict((raw.get("http") or {}).get("headers") or {}),
            "host_headers": dict((raw.get("http") or {}).get("host_headers") or {}),
        },
        "matching": {
            "fuzzy_enabled": bool((raw.get("matching") or {}).get("fuzzy_enabled", True)),
            "fuzzy_cutoff": float((raw.get("matching") or {}).get("fuzzy_cutoff", 0.93)),
        },
        "limits": {**DEFAULT_LIMITS, **(raw.get("limits") or {})},
    }
    return cfg


# --------------------------------------------------------------------------
# Разбор M3U (без потери метаданных)
# --------------------------------------------------------------------------
@dataclass
class Entry:
    index: int
    prefix: list          # строки между предыдущим URL и этим #EXTINF (комментарии, #EXTGRP и т.п.)
    lines: list           # #EXTINF, дополнительные директивы, URL (последняя строка)
    url_pos: int
    extinf: str
    name: str
    attrs: dict
    extra: list           # директивы между #EXTINF и URL

    @property
    def url(self) -> str:
        return self.lines[self.url_pos].strip()

    @property
    def group(self) -> str:
        return self.attrs.get("group-title", "")


@dataclass
class Playlist:
    header: list
    entries: list
    tail: list
    final_newline: bool
    newline: str = "\n"


def split_extinf(line: str):
    """Возвращает (attrs, name). Запятая-разделитель ищется вне кавычек."""
    body = line.split(":", 1)[1] if ":" in line else ""
    in_q = False
    comma = -1
    for i, ch in enumerate(body):
        if ch == '"':
            in_q = not in_q
        elif ch == "," and not in_q:
            comma = i
            break
    head, name = (body[:comma], body[comma + 1:]) if comma >= 0 else (body, "")
    return dict(ATTR_RE.findall(head)), name.strip()


def parse_m3u(text: str) -> Playlist:
    if text.startswith("\ufeff"):
        text = text[1:]
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.replace("\r\n", "\n").split("\n")
    final_newline = text.endswith("\n")
    if final_newline:
        lines = lines[:-1]
    header: list = []
    entries: list = []
    pending: list = []
    cur = None
    for line in lines:
        s = line.strip()
        if cur is not None:
            cur["lines"].append(line)
            if s and not s.startswith("#"):
                cur["url_pos"] = len(cur["lines"]) - 1
                attrs, name = split_extinf(cur["lines"][0])
                entries.append(
                    Entry(len(entries), cur["prefix"], cur["lines"], cur["url_pos"],
                          cur["lines"][0], name, attrs, [l for l in cur["lines"][1:-1]])
                )
                cur = None
            continue
        if s.upper().startswith("#EXTINF"):
            cur = {"prefix": pending, "lines": [line]}
            pending = []
        elif s and not s.startswith("#"):
            # URL без #EXTINF — сохраняем как запись без имени
            entries.append(Entry(len(entries), pending, [line], 0, "", "", {}, []))
            pending = []
        elif not entries and cur is None and (s.upper().startswith("#EXTM3U") or not header and not pending):
            header.append(line)
        else:
            pending.append(line)
    tail = pending
    if cur is not None:  # #EXTINF без URL в конце файла — сохраняем как есть
        tail = cur["prefix"] + cur["lines"]
    return Playlist(header, entries, tail, final_newline, newline)


def render_m3u(pl: Playlist, new_urls: dict) -> str:
    out = list(pl.header)
    for e in pl.entries:
        out.extend(e.prefix)
        lines = list(e.lines)
        if e.index in new_urls:
            old = lines[e.url_pos]
            indent = old[: len(old) - len(old.lstrip())]
            lines[e.url_pos] = indent + new_urls[e.index]
        out.extend(lines)
    out.extend(pl.tail)
    text = pl.newline.join(out)
    if pl.final_newline:
        text += pl.newline
    return text


# --------------------------------------------------------------------------
# Нормализация, имена, провайдеры, URL
# --------------------------------------------------------------------------
def norm_text(s: str) -> str:
    return re.sub(r"\s+", " ", s.casefold().replace("ё", "е")).strip()


def channel_key(name: str):
    """(ключ без суффиксов качества, уровень качества 0..4)."""
    text = norm_text(name)
    tokens = re.sub(r"[^\w+]+", " ", text).split()
    quality = 0
    for t in tokens:
        quality = max(quality, QUALITY.get(t, 0))
    rest = [t for t in tokens if t not in NOISE_TOKENS]
    return (" ".join(rest) or text), quality


def signature(key: str):
    return tuple(sorted(t for t in key.split() if any(c.isdigit() or c == "+" for c in t)))


def normalize_url(url: str) -> str:
    u = url.strip()
    for _ in range(3):
        n = unquote(u)
        if n == u:
            break
        u = n
    return re.sub(r"\s+", "", u).casefold()


def find_fragment(url: str, fragments) -> str | None:
    n = normalize_url(url)
    for f in fragments:
        if f and f.casefold() in n:
            return f
    return None


def redact_url(url: str) -> str:
    try:
        p = urlsplit(url.strip())
        netloc = p.hostname or ""
        if p.port:
            netloc += f":{p.port}"
        path = "/".join("***" if TOKEN_SEGMENT.match(seg) else seg for seg in p.path.split("/"))
        q = [(k, "***" if SENSITIVE_KEY.search(k) else v) for k, v in parse_qsl(p.query, keep_blank_values=True)]
        return urlunsplit((p.scheme, netloc, path, urlencode(q, safe="*"), ""))
    except Exception:
        return "<unparsable-url>"


def entry_provider(entry: Entry, providers, fields) -> str | None:
    parts = []
    extgrp = [l.split(":", 1)[1] for l in entry.extra + entry.prefix if l.strip().upper().startswith("#EXTGRP:")]
    if "group-title" in fields:
        parts.append(entry.group)
    if "extgrp" in fields:
        parts += extgrp
    if "attr_values" in fields:
        parts += [v for k, v in entry.attrs.items() if k.lower() not in IGNORED_ATTRS]
    if "directives" in fields:
        parts += [l for l in entry.extra if not l.strip().upper().startswith("#EXTGRP:")]
    if "comments" in fields:
        parts += [l for l in entry.prefix if not l.strip().upper().startswith("#EXTGRP:")]
    hay = " | ".join(norm_text(p) for p in parts if p)
    for p in providers:
        for alias in p["aliases"]:
            a = norm_text(alias)
            if a and re.search(rf"(?<!\w){re.escape(a)}(?!\w)", hay):
                return p["name"]
    return None


def looks_paid(entry: Entry, markers) -> bool:
    hay = norm_text(" ".join([entry.extinf] + entry.extra + entry.prefix))
    return any(m.casefold() in hay for m in markers if m)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
@dataclass
class FetchResult:
    status: int = 0
    final_url: str = ""
    content_type: str = ""
    body: bytes = b""
    error: str = ""
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


def classify_exception(exc: BaseException) -> str:
    reason = getattr(exc, "reason", exc)
    if isinstance(reason, socket.gaierror):
        return "dns_error"
    if isinstance(reason, (ssl.SSLError, ssl.CertificateError)):
        return "tls_error"
    if isinstance(reason, (socket.timeout, TimeoutError)):
        return "timeout"
    if isinstance(reason, (ConnectionRefusedError, ConnectionResetError, ConnectionAbortedError)):
        return "connection_error"
    if isinstance(exc, ValueError):
        return "invalid_url"
    return f"network_error:{type(exc).__name__}"


class HttpFetcher:
    """Вызываемый объект fetch(url, max_bytes, byte_range) -> FetchResult."""

    def __init__(self, timeout=10.0, per_host=4, headers=None, host_headers=None, deadline=20.0):
        self.timeout = float(timeout)
        self.per_host = int(per_host)
        self.headers = dict(headers or {})
        self.host_headers = dict(host_headers or {})
        self.deadline = float(deadline)
        self._sems: dict = {}
        self._lock = threading.Lock()

    def _sem(self, host: str):
        with self._lock:
            return self._sems.setdefault(host, threading.BoundedSemaphore(self.per_host))

    def __call__(self, url, max_bytes=262144, byte_range=False, deadline=None) -> FetchResult:
        parts = urlsplit(url)
        if parts.scheme.lower() not in ("http", "https"):
            return FetchResult(error="unsupported_scheme")
        host = parts.hostname or ""
        headers = dict(self.headers)
        for frag, extra in self.host_headers.items():
            if frag in host:
                headers.update(extra)
        if byte_range:
            headers["Range"] = f"bytes=0-{max_bytes - 1}"
        t_end = time.monotonic() + float(deadline or self.deadline)
        with self._sem(host):
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    chunks, total, truncated = [], 0, False
                    while total < max_bytes:
                        if time.monotonic() > t_end:
                            truncated = True
                            break
                        chunk = resp.read(min(16384, max_bytes - total))
                        if not chunk:
                            break
                        chunks.append(chunk)
                        total += len(chunk)
                    body = b"".join(chunks)
                    if truncated and not body:
                        return FetchResult(status=0, error="timeout")
                    return FetchResult(resp.status, resp.geturl(), resp.headers.get("Content-Type", ""),
                                       body, truncated=truncated)
            except urllib.error.HTTPError as e:
                try:
                    e.close()
                except Exception:
                    pass
                return FetchResult(status=e.code, final_url=url, error=f"http_{e.code}")
            except Exception as e:  # noqa: BLE001 — любая сетевая ошибка = «не работает»
                return FetchResult(error=classify_exception(e))


# --------------------------------------------------------------------------
# Проверка потоков
# --------------------------------------------------------------------------
@dataclass
class CheckResult:
    state: str            # ok | fail | forbidden | unknown
    reason: str = ""
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"state": self.state, "reason": self.reason, **({"detail": self.detail} if self.detail else {})}


def _looks_html(body: bytes, ctype: str) -> bool:
    head = body[:512].lstrip().lower()
    return "text/html" in ctype.lower() or head.startswith((b"<!doctype", b"<html", b"<head", b"<body"))


def check_stream(url: str, fetch, forbidden=(), _depth: int = 0) -> CheckResult:
    scheme = urlsplit(url).scheme.lower()
    if scheme not in ("http", "https"):
        return CheckResult("unknown", f"unsupported_scheme:{scheme or 'none'}")
    r = fetch(url, max_bytes=262144)
    if not r.ok:
        reason = r.error or f"http_{r.status}"
        if r.status in (401, 403):
            reason = f"auth_required_or_denied_http_{r.status}"
        return CheckResult("fail", reason)
    if r.final_url and find_fragment(r.final_url, forbidden):
        return CheckResult("forbidden", "redirects_to_forbidden_host")
    body = r.body
    head = body[:1024].lstrip(b"\xef\xbb\xbf \r\n\t")
    if head.startswith(b"#EXTM3U") or "mpegurl" in r.content_type.lower():
        return _check_hls(body.decode("utf-8", "replace"), r.final_url or url, fetch, forbidden, _depth)
    if _looks_html(body, r.content_type):
        return CheckResult("fail", "html_instead_of_stream")
    if len(body) < 188:
        return CheckResult("fail", "too_little_data")
    return CheckResult("ok", "direct_stream", {"bytes": len(body)})


def _check_hls(text: str, base: str, fetch, forbidden, depth: int) -> CheckResult:
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if any(l.startswith("#EXT-X-STREAM-INF") for l in lines):
        if depth >= 2:
            return CheckResult("fail", "hls_nesting_too_deep")
        variants = []
        for i, l in enumerate(lines):
            if l.startswith("#EXT-X-STREAM-INF"):
                for nxt in lines[i + 1:]:
                    if not nxt.startswith("#"):
                        variants.append(urljoin(base, nxt))
                        break
        if not variants:
            return CheckResult("fail", "hls_master_without_variants")
        last = CheckResult("fail", "hls_variant_unavailable")
        for v in variants[:2]:
            if find_fragment(v, forbidden):
                return CheckResult("forbidden", "hls_variant_on_forbidden_host")
            last = check_stream(v, fetch, forbidden, depth + 1)
            if last.state in ("ok", "forbidden"):
                return last
        return last
    segments = [l for l in lines if not l.startswith("#")]
    if not segments:
        return CheckResult("fail", "hls_no_segments")
    urls = [urljoin(base, s) for s in segments]
    for u in urls[-2:]:
        if find_fragment(u, forbidden):
            return CheckResult("forbidden", "hls_segment_on_forbidden_host")
    for u in reversed(urls[-2:]):
        seg = fetch(u, max_bytes=4096, byte_range=True)
        if seg.ok and len(seg.body) >= 16 and not _looks_html(seg.body, seg.content_type):
            return CheckResult("ok", "hls", {"segments": len(urls)})
    return CheckResult("fail", "hls_segment_unavailable")


# --------------------------------------------------------------------------
# Подбор замен
# --------------------------------------------------------------------------
@dataclass
class Candidate:
    entry: Entry
    key: str
    quality: int
    provider: str


class CandidateIndex:
    def __init__(self, playlist: Playlist | None, cfg: dict):
        self.by_key: dict = {}
        self.stats = {"total": 0, "eligible": 0, "rejected_forbidden": 0, "rejected_provider": 0,
                      "rejected_paid": 0, "rejected_scheme": 0}
        self.cfg = cfg
        if not playlist:
            return
        for e in playlist.entries:
            self.stats["total"] += 1
            url = e.url
            if not e.name or urlsplit(url).scheme.lower() not in ("http", "https"):
                self.stats["rejected_scheme"] += 1
                continue
            if find_fragment(url, cfg["forbidden_url_fragments"]) or find_fragment(url, cfg["force_replace_url_fragments"]):
                self.stats["rejected_forbidden"] += 1
                continue
            prov = entry_provider(e, cfg["allowed_providers"], cfg["provider_fields"])
            if not prov:
                self.stats["rejected_provider"] += 1
                continue
            if looks_paid(e, cfg["paid_markers"]):
                self.stats["rejected_paid"] += 1
                continue
            key, q = channel_key(e.name)
            self.by_key.setdefault(key, []).append(Candidate(e, key, q, prov))
            self.stats["eligible"] += 1

    def find(self, entry: Entry, limit: int):
        key, oq = channel_key(entry.name)
        found = [(0, c) for c in self.by_key.get(key, [])]
        if not found and self.cfg["matching"]["fuzzy_enabled"]:
            sig = signature(key)
            for k in difflib.get_close_matches(key, list(self.by_key), n=3, cutoff=self.cfg["matching"]["fuzzy_cutoff"]):
                if signature(k) == sig:
                    found += [(1, c) for c in self.by_key[k]]
        order = [p["name"] for p in self.cfg["allowed_providers"]]
        og = norm_text(entry.group)

        def rank(item):
            kind, c = item
            qd = abs(c.quality - oq) if (oq and c.quality) else 0
            gm = 0 if norm_text(c.entry.group) == og else 1
            return (kind, qd, gm, order.index(c.provider), -c.quality)

        found.sort(key=rank)
        seen, out = set(), []
        for kind, c in found:
            u = c.entry.url
            if u in seen or u == entry.url:
                continue
            seen.add(u)
            out.append((("exact" if kind == 0 else "fuzzy"), c))
            if len(out) >= limit:
                break
        return out


# --------------------------------------------------------------------------
# Основной процесс
# --------------------------------------------------------------------------
def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def rebuild(source_text: str, alt_text: str | None, alt_error: str | None, cfg: dict, fetch,
            workers: int = 16, max_candidates: int = 8, max_runtime: float = 2700) -> tuple:
    """Возвращает (output_text | None, report). Ничего не пишет на диск."""
    started = time.monotonic()
    deadline = started + max_runtime
    report: dict = {
        "generated_at": now_iso(), "status": "ok", "errors": [], "warnings": [],
        "alternative_playlist": {"url": cfg["alternative_playlist_url"], "loaded": alt_text is not None,
                                 "error": alt_error},
        "channels": [],
    }
    pl = parse_m3u(source_text)
    if not pl.entries:
        report.update(status="failed", errors=["В исходном плейлисте не найдено ни одного канала"])
        return None, report
    alt_pl = parse_m3u(alt_text) if alt_text else None
    index = CandidateIndex(alt_pl, cfg)
    report["alternative_playlist"]["candidates"] = index.stats
    forbidden, force = cfg["forbidden_url_fragments"], cfg["force_replace_url_fragments"]
    protected = {channel_key(n)[0] for n in cfg["no_replace_dead_channels"]}

    cache: dict = {}
    cache_lock = threading.Lock()
    used: set = set()
    used_lock = threading.Lock()

    def cached_check(url: str) -> CheckResult:
        with cache_lock:
            hit = cache.get(url)
        if hit is not None:
            return hit
        try:
            res = check_stream(url, fetch, forbidden)
        except Exception as e:  # noqa: BLE001
            res = CheckResult("fail", f"checker_exception:{type(e).__name__}")
        with cache_lock:
            cache[url] = res
        return res

    # --- этап 1: проверка исходных потоков --------------------------------
    info: dict = {}

    def stage1(e: Entry) -> None:
        rec = {"need": None, "check": None}
        info[e.index] = rec
        if time.monotonic() > deadline:
            rec["check"] = CheckResult("unknown", "time_budget_exceeded")
            return
        if find_fragment(e.url, force):
            rec["need"] = "force_replace_domain"
            rec["check"] = CheckResult("fail", "force_replace_domain_skipped_http_check")
        elif find_fragment(e.url, forbidden):
            rec["need"] = "forbidden_source"
            rec["check"] = CheckResult("forbidden", "forbidden_url_fragment")
        else:
            res = cached_check(e.url)
            rec["check"] = res
            if res.state == "fail":
                rec["need"] = "dead"
            elif res.state == "forbidden":
                rec["need"] = "forbidden_source"

    total = len(pl.entries)
    done = 0
    with cf.ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        for _ in ex.map(stage1, pl.entries):
            done += 1
            if done % 25 == 0 or done == total:
                log(f"[1/2] проверено {done}/{total}")

    checks = [i["check"] for i in info.values()]
    real = [c for c in checks if c.state in ("ok", "fail")]
    dead_net = [c for c in real if c.state == "fail"]
    lim = cfg["limits"]
    suspect = (len(real) >= lim["mass_failure_min_channels"]
               and len(dead_net) / len(real) >= lim["mass_failure_ratio"])
    if suspect:
        report.update(status="failed")
        report["errors"].append(
            f"Подозрение на сетевой сбой: не работают {len(dead_net)} из {len(real)} проверенных потоков. "
            "Замены не выполнялись, playlist.m3u не изменён.")

    # --- этап 2: подбор замен ---------------------------------------------
    results: dict = {}
    new_urls: dict = {}
    new_lock = threading.Lock()

    def stage2(e: Entry) -> None:
        rec = info[e.index]
        need = rec["need"]
        if need is None:
            return
        out = {"reason": need, "candidates": []}
        results[e.index] = out
        key = channel_key(e.name)[0] if e.name else ""
        if suspect:
            out.update(status="kept_unreplaced", failure="skipped_network_failure_suspected")
        elif not e.name:
            out.update(status="kept_unreplaced", failure="no_channel_name")
        elif need == "dead" and key in protected:
            out.update(status="protected_kept", failure="protected_channel_not_replaced")
        elif alt_pl is None:
            out.update(status="kept_unreplaced", failure="alternative_playlist_unavailable")
        else:
            cands = index.find(e, max_candidates)
            if not cands:
                out.update(status="kept_unreplaced", failure="no_matching_candidates")
            else:
                out["status"], out["failure"] = "kept_unreplaced", "all_candidates_failed"
                for kind, c in cands:
                    if time.monotonic() > deadline:
                        out["failure"] = "time_budget_exceeded"
                        break
                    url = c.entry.url
                    res = cached_check(url)
                    out["candidates"].append({
                        "url": redact_url(url), "name": c.entry.name, "provider": c.provider, "match": kind,
                        "check": res.to_dict(),
                        "free_status": "assumed_free_by_allowed_provider_not_independently_verified",
                    })
                    if res.state != "ok":
                        continue
                    with used_lock:
                        if url in used:
                            continue
                        used.add(url)
                    with new_lock:
                        new_urls[e.index] = url
                    out.update(status="replaced", failure=None, new_url=redact_url(url), new_provider=c.provider)
                    break

    todo = [e for e in pl.entries if info[e.index]["need"]]
    done = 0
    with cf.ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        for _ in ex.map(stage2, todo):
            done += 1
            if done % 10 == 0 or done == len(todo):
                log(f"[2/2] обработано замен {done}/{len(todo)}")

    # --- отчёт ------------------------------------------------------------
    counts = {"replaced": 0, "kept_unreplaced": 0, "protected_kept": 0}
    for e in pl.entries:
        rec, res = info[e.index], results.get(e.index)
        status = "ok"
        if res:
            status = res["status"]
            counts[status] += 1
        elif rec["check"].state == "unknown":
            status = "unchecked"
        if status != "ok":
            report["channels"].append({
                "index": e.index, "name": e.name, "group": e.group,
                "status": status,
                "need_reason": rec["need"],
                "original_url": redact_url(e.url),
                "source_check": rec["check"].to_dict(),
                **({k: v for k, v in res.items() if k != "status"} if res else {}),
            })
    unchecked = sum(1 for e in pl.entries if info[e.index]["check"].state == "unknown")
    report["summary"] = {
        "total_channels": total,
        "checked_channels": total - unchecked,
        "working_streams": sum(1 for e in pl.entries if info[e.index]["check"].state == "ok"
                               and not info[e.index]["need"]),
        "broken_streams": sum(1 for e in pl.entries if info[e.index]["need"] == "dead"),
        "needing_replacement": len(todo),
        "replacements_succeeded": counts["replaced"],
        "replacements_failed": counts["kept_unreplaced"],
        "replacements_skipped_protected": counts["protected_kept"],
        "unchecked_or_undetermined": unchecked,
        "duration_seconds": round(time.monotonic() - started, 1),
    }
    if alt_error:
        report["errors"].append(f"Не удалось загрузить альтернативный плейлист: {alt_error}")
    if report["status"] != "failed":
        warn = bool(alt_error or counts["kept_unreplaced"] or unchecked)
        report["status"] = "completed_with_warnings" if warn else "ok"
    if report["status"] == "failed":
        return None, report
    return render_m3u(pl, new_urls), report


# --------------------------------------------------------------------------
# Проверка результата
# --------------------------------------------------------------------------
REPORT_KEYS = ("generated_at", "status", "summary", "channels", "errors", "alternative_playlist")


def verify_outputs(source_path, output_path, report_path, cfg) -> list:
    problems = []
    try:
        report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return [f"Отчёт отсутствует или не является валидным JSON: {e}"]
    for k in REPORT_KEYS:
        if k not in report:
            problems.append(f"В отчёте нет поля '{k}'")
    if report.get("status") == "failed":
        problems.append("Отчёт имеет статус failed: " + "; ".join(report.get("errors", [])))
    try:
        src = parse_m3u(Path(source_path).read_text(encoding="utf-8", errors="replace"))
        out_text = Path(output_path).read_text(encoding="utf-8", errors="replace")
        out = parse_m3u(out_text)
    except Exception as e:  # noqa: BLE001
        return problems + [f"Не удалось прочитать M3U: {e}"]
    if not out_text.lstrip("\ufeff").lstrip().startswith("#EXTM3U"):
        problems.append("Результирующий плейлист не начинается с #EXTM3U")
    if len(src.entries) != len(out.entries):
        problems.append(f"Число каналов изменилось: {len(src.entries)} -> {len(out.entries)}")
        return problems
    replaced = {c["index"] for c in report.get("channels", []) if c.get("status") == "replaced"}
    if src.header != out.header or src.tail != out.tail:
        problems.append("Заголовок или хвост плейлиста изменились")
    for a, b in zip(src.entries, out.entries):
        if (a.prefix, a.extinf, a.extra) != (b.prefix, b.extinf, b.extra):
            problems.append(f"Метаданные канала #{a.index} '{a.name}' изменились")
        if a.url != b.url:
            if a.index not in replaced:
                problems.append(f"URL канала #{a.index} '{a.name}' изменён без записи о замене в отчёте")
            if find_fragment(b.url, cfg["forbidden_url_fragments"]) or find_fragment(b.url, cfg["force_replace_url_fragments"]):
                problems.append(f"Новая замена канала #{a.index} '{a.name}' содержит запрещённый фрагмент")
    return problems


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Проверка и восстановление IPTV-плейлиста")
    p.add_argument("--source", default="playlist_source.m3u")
    p.add_argument("--output", default="playlist.m3u")
    p.add_argument("--report", default="playlist-report.json")
    p.add_argument("--config", default="config/providers.json")
    p.add_argument("--alt-url", default=None, help="URL альтернативного плейлиста (по умолчанию из конфига)")
    p.add_argument("--alt-file", default=None, help="Локальный файл вместо --alt-url (для тестов)")
    p.add_argument("--workers", type=int, default=None)
    p.add_argument("--timeout", type=float, default=None)
    p.add_argument("--max-candidates", type=int, default=None)
    p.add_argument("--max-runtime", type=float, default=None, help="Бюджет времени, секунд")
    p.add_argument("--fail-on-alt-error", action="store_true",
                   help="Завершиться ошибкой, если альтернативный плейлист не загрузился")
    p.add_argument("--verify-only", action="store_true",
                   help="Только проверить уже созданные --output и --report")
    return p


def write_atomic(path: str, text: str) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    os.replace(tmp, path)


def write_report(path: str, report: dict) -> None:
    write_atomic(path, json.dumps(report, ensure_ascii=False, indent=2) + "\n")


def load_alt(args, cfg, fetch) -> tuple:
    if args.alt_file:
        try:
            return Path(args.alt_file).read_text(encoding="utf-8", errors="replace"), None
        except OSError as e:
            return None, f"{args.alt_file}: {e}"
    url = args.alt_url or cfg["alternative_playlist_url"]
    last = "unknown"
    for attempt in range(3):
        r = fetch(url, max_bytes=30_000_000, deadline=60)
        if r.ok and r.body:
            if r.truncated:
                last = "truncated_download"
            else:
                text = r.body.decode("utf-8", "replace")
                if "#EXTINF" in text:
                    return text, None
                last = "not_an_m3u_playlist"
        else:
            last = r.error or f"http_{r.status}"
        time.sleep(2 * (attempt + 1))
    return None, f"{url}: {last}"


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        log(f"ОШИБКА конфигурации: {e}")
        write_report(args.report, {"generated_at": now_iso(), "status": "failed", "errors": [str(e)],
                                   "summary": {}, "channels": [], "alternative_playlist": {}})
        return 1
    if args.verify_only:
        problems = verify_outputs(args.source, args.output, args.report, cfg)
        for p in problems:
            log(f"ПРОВЕРКА НЕ ПРОЙДЕНА: {p}")
        if not problems:
            log("Проверка результата пройдена")
        return 1 if problems else 0

    lim = cfg["limits"]
    workers = args.workers or lim["workers"]
    max_cand = args.max_candidates or lim["max_candidates"]
    max_rt = args.max_runtime or lim["max_runtime"]
    fetch = HttpFetcher(timeout=args.timeout or lim["timeout"], per_host=lim["per_host"],
                        headers=cfg["http"]["headers"], host_headers=cfg["http"]["host_headers"],
                        deadline=lim["request_deadline"])
    try:
        try:
            source_text = Path(args.source).read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            raise RuntimeError(f"Не удалось прочитать исходный плейлист {args.source}: {e}") from e
        alt_text, alt_err = load_alt(args, cfg, fetch)
        if alt_err:
            log(f"ПРЕДУПРЕЖДЕНИЕ: {alt_err}")
        output, report = rebuild(source_text, alt_text, alt_err, cfg, fetch, workers, max_cand, max_rt)
    except Exception as e:  # noqa: BLE001
        tb = traceback.format_exc(limit=5)
        log(tb)
        write_report(args.report, {"generated_at": now_iso(), "status": "failed",
                                   "errors": [f"{type(e).__name__}: {e}"], "summary": {}, "channels": [],
                                   "alternative_playlist": {}})
        return 1
    write_report(args.report, report)
    if output is None:
        for e in report["errors"]:
            log(f"ОШИБКА: {e}")
        return 1
    write_atomic(args.output, output)
    s = report["summary"]
    log(f"Готово: каналов {s['total_channels']}, рабочих {s['working_streams']}, нужна замена "
        f"{s['needing_replacement']}, заменено {s['replacements_succeeded']}, не удалось "
        f"{s['replacements_failed']}, не проверено {s['unchecked_or_undetermined']}")
    if args.fail_on_alt_error and alt_err:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())