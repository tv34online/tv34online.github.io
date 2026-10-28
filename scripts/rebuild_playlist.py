#!/usr/bin/env python3
"""Проверяет исходный IPTV-плейлист и заменяет нерабочие каналы
только потоками из разрешённых групп group-title.
Исходный playlist_source.m3u никогда не изменяется.
"""
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA})
OPT_PREFIXES = ("#EXTVLCOPT", "#EXTHTTP")
# Любой URL с этим фрагментом запрещён, включая указанный iptv2022-адрес.
FORBIDDEN_URL_SUBSTRINGS = ("iptv2022",)

def forbidden_stream(url):
    """True, если адрес потока содержит запрещённый фрагмент."""
    value = (url or "").strip().casefold()
    return any(fragment.casefold() in value for fragment in FORBIDDEN_URL_SUBSTRINGS)

NOISE = {"hd", "fhd", "uhd", "sd", "hq", "4k"}
DEFAULTS = {
    "source": "playlist_source.m3u",
    "output": "playlist.m3u",
    "fallbacks_file": "fallbacks.json",
    "allowed_source_groups": [
        "Ott Club+Реклама",
        "Rutube (VPN)",
        "Оргтехсервис (VPN🇷🇺)",
        "Webhost (VPN🇷🇺)",
        "Wink (VPN🇷🇺)"
    ],
    "protect_first_group": True,
    "protected_groups": [],
    "protected_names": [
        "Первый", "Россия 1", "Россия 24", "Россия К", "Матч", "НТВ",
        "Пятый", "ОТР", "ТВ Центр", "ТВЦ", "РЕН ТВ", "Звезда", "Мир",
        "Карусель", "СПАС"
    ],
    "extra_playlists": [
        "https://raw.githubusercontent.com/Dimonovich/TV/Dimonovich/FREE/TV"
    ],
    "workers": 20,
    "timeout": 10,
    "max_dead_ratio": 0.6
}


def tokens(name):
    return [t for t in re.findall(r"\w+", name.casefold()) if t not in NOISE]


def norm_group(group):
    return re.sub(r"\s+", " ", (group or "").strip()).casefold()


def parse(text):
    headers, entries, meta = [], [], []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTM3U"):
            headers.append(line)
        elif line.startswith("#"):
            meta.append(line)
        else:
            entries.append({"meta": meta, "url": line})
            meta = []
    return headers, entries


def info(entry):
    name, group = "", ""
    for line in entry.get("meta", []):
        if line.startswith("#EXTINF"):
            if "," in line:
                name = line.rsplit(",", 1)[-1].strip()
            m = re.search(r'group-title="([^"]*)"', line, re.I)
            if m:
                group = m.group(1).strip()
        elif line.startswith("#EXTGRP:") and not group:
            group = line.split(":", 1)[1].strip()
    return name, group


def is_protected(name, group, cfg, first_group):
    g = norm_group(group)
    if cfg.get("protect_first_group", True) and first_group and g == first_group:
        return True
    if g and g in {norm_group(x) for x in cfg.get("protected_groups", [])}:
        return True
    nt = tokens(name)
    for protected in cfg.get("protected_names", []):
        pt = tokens(protected)
        if pt and nt[:len(pt)] == pt:
            return True
    return False


def _alive(url, timeout, depth=0):
    response = SESSION.get(url, timeout=timeout, stream=True, allow_redirects=True)
    try:
        if response.status_code != 200:
            return False
        chunk = next(response.iter_content(65536), b"")
        if not chunk:
            return False
        if chunk.lstrip().startswith(b"#EXTM3U"):
            if depth >= 3:
                return False
            lines = [x.strip() for x in chunk.decode("utf-8", "ignore").splitlines()]
            uris = [x for x in lines if x and not x.startswith("#")]
            if not uris:
                return False
            if any(x.startswith("#EXT-X-STREAM-INF") for x in lines):
                return _alive(urljoin(response.url, uris[0]), timeout, depth + 1)
            segment = SESSION.get(urljoin(response.url, uris[-1]), timeout=timeout,
                                  stream=True, headers={"Range": "bytes=0-4095"})
            try:
                return segment.status_code in (200, 206) and bool(next(segment.iter_content(1024), b""))
            finally:
                segment.close()
        return not response.headers.get("content-type", "").lower().startswith("text/html")
    finally:
        response.close()


def alive(url, timeout):
    for attempt in range(2):
        try:
            if _alive(url, timeout):
                return True
        except Exception:
            pass
        if attempt == 0:
            time.sleep(1)
    return False


def load_one(url, allowed_groups, timeout):
    response = SESSION.get(url, timeout=max(30, timeout))
    response.raise_for_status()
    _, entries = parse(response.text)
    allowed = {norm_group(g) for g in allowed_groups}
    for entry in entries:
        name, group = info(entry)
        # Никогда не используем запрещённые потоки даже как источник замены.
        if forbidden_stream(entry["url"]):
            continue
        # Строгое совпадение group-title; прочие группы не могут стать заменами.
        if norm_group(group) not in allowed:
            continue
        key = " ".join(tokens(name))
        if key:
            opts = [line for line in entry["meta"] if line.startswith(OPT_PREFIXES)]
            yield key, name, group, entry["url"], opts


def load_sources(cfg):
    index = {}
    allowed = cfg["allowed_source_groups"]
    for url in cfg.get("extra_playlists", []):
        try:
            count = 0
            for key, name, group, stream_url, opts in load_one(url, allowed, cfg["timeout"]):
                index.setdefault(key, []).append((group, stream_url, opts, name))
                count += 1
            print(f"Загружено разрешённых потоков из {url}: {count}")
        except Exception as exc:
            print(f"! Не удалось загрузить {url}: {exc}")
    return index


def load_fallbacks(path, allowed_groups):
    """Формат: {"Имя канала": [{"group":"точная группа", "url":"https://..."}]}.
    Старый формат со списком URL намеренно игнорируется: у него нет группы-источника.
    """
    if not os.path.exists(path):
        return {}
    try:
        raw = json.load(open(path, encoding="utf-8"))
    except Exception as exc:
        print(f"! Не удалось прочитать {path}: {exc}")
        return {}
    allowed = {norm_group(g) for g in allowed_groups}
    result = {}
    for channel, items in raw.items():
        if not isinstance(items, list):
            continue
        valid = []
        for item in items:
            if not isinstance(item, dict):
                continue
            group, url = item.get("group", ""), item.get("url", "")
            if (norm_group(group) in allowed and isinstance(url, str)
                    and url.startswith(("http://", "https://")) and not forbidden_stream(url)):
                valid.append((group, url, []))
        result[" ".join(tokens(channel))] = valid
    return result


def find_replacement(name, fallbacks, sources, cfg):
    key = " ".join(tokens(name))
    tried = set()
    candidates = []
    for group, url, opts in fallbacks.get(key, []):
        candidates.append((group, url, opts))
    for group, url, opts, _source_name in sources.get(key, []):
        candidates.append((group, url, opts))
    for group, url, opts in candidates:
        if norm_group(group) not in {norm_group(x) for x in cfg["allowed_source_groups"]}:
            continue
        if not url or forbidden_stream(url) or url in tried:
            continue
        tried.add(url)
        if alive(url, cfg["timeout"]):
            return group, url, opts
    return None


def main():
    cfg = dict(DEFAULTS)
    config_path = os.environ.get("IPTV_CONFIG", "iptv_config.json")
    if os.path.exists(config_path):
        with open(config_path, encoding="utf-8") as f:
            cfg.update(json.load(f))

    with open(cfg["source"], encoding="utf-8-sig") as f:
        headers, entries = parse(f.read())
    removed_forbidden = sum(1 for e in entries if forbidden_stream(e["url"]))
    entries = [e for e in entries if not forbidden_stream(e["url"])]
    if removed_forbidden:
        print(f"Удалено потоков с запрещённым адресом iptv2022: {removed_forbidden}")
    if not entries:
        print(f"Ошибка: после удаления запрещённых потоков в {cfg['source']} не осталось каналов.")
        return 2

    fallbacks = load_fallbacks(cfg["fallbacks_file"], cfg["allowed_source_groups"])
    first_group = next((norm_group(info(e)[1]) for e in entries if info(e)[1].strip()), "")
    for entry in entries:
        entry["name"], entry["group"] = info(entry)
        entry["protected"] = is_protected(entry["name"], entry["group"], cfg, first_group)

    todo = [e for e in entries if not e["protected"]]
    print(f"Каналов: {len(entries)}; защищено: {len(entries)-len(todo)}; проверяю: {len(todo)}")
    with ThreadPoolExecutor(max_workers=int(cfg["workers"])) as pool:
        for entry, is_ok in zip(todo, pool.map(lambda e: alive(e["url"], int(cfg["timeout"])), todo)):
            entry["alive"] = is_ok

    dead = [e for e in todo if not e["alive"]]
    report = []
    if todo and len(dead) / len(todo) > float(cfg["max_dead_ratio"]):
        message = (f"⚠️ Нерабочих каналов {len(dead)} из {len(todo)}; вероятен сбой сети. "
                   "Исходный результат не перезаписываю.")
        print(message)
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
                f.write("## Пересборка плейлиста\n\n" + message + "\n")
        return 0

    sources = load_sources(cfg) if dead else {}
    replaced = failed = 0
    for entry in dead:
        replacement = find_replacement(entry["name"], fallbacks, sources, cfg)
        if replacement:
            group, url, opts = replacement
            entry["url"] = url
            entry["meta"] = [x for x in entry["meta"] if not x.startswith(OPT_PREFIXES)] + opts
            replaced += 1
            report.append(f"✅ {entry['name']} → {group}")
        else:
            failed += 1
            report.append(f"❌ {entry['name']}: разрешённой замены нет, оставлен исходный URL")

    output = (headers or ["#EXTM3U"])[:1]
    for entry in entries:
        # Финальная страховка: запрещённый URL не попадёт в итоговый файл.
        if forbidden_stream(entry["url"]):
            continue
        output.extend(entry["meta"])
        output.append(entry["url"])
    with open(cfg["output"], "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(output) + "\n")

    summary = (f"## Пересборка плейлиста\n\nВсего: {len(entries)} · защищено: {len(entries)-len(todo)} · "
               f"нерабочих: {len(dead)} · заменено: {replaced} · без замены: {failed}\n\n" +
               "\n".join(f"- {line}" for line in report))
    print(summary)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(summary + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())