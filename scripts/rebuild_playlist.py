#!/usr/bin/env python3
"""Пересборка IPTV-плейлиста: проверяет потоки, нерабочие заменяет запасными.

Исходник (playlist_source.m3u) никогда не меняется — каждый раз он
проверяется заново, поэтому родной поток возвращается, как только оживёт.
Результат пишется в playlist.m3u.
"""
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
S = requests.Session()
S.headers["User-Agent"] = UA

DEFAULTS = {
    "source": "playlist_source.m3u",
    "output": "playlist.m3u",
    "fallbacks_file": "fallbacks.json",
    "protect_first_group": True,
    "protected_groups": [],
    "protected_names": [],
    "extra_playlists": [],
    "extra_skip_groups_contains": ["VPN", "INFO"],
    "use_iptv_org": True,
    "iptv_org_url": "https://iptv-org.github.io/iptv/languages/rus.m3u",
    "use_rutube_search": True,
    "workers": 20,
    "timeout": 10,
    "drop_dead": False,
    "max_dead_ratio": 0.6,
}
NOISE = {"hd", "fhd", "uhd", "sd", "hq", "4k"}


# ---------- разбор m3u ----------
def tokens(name):
    return [t for t in re.findall(r"\w+", name.lower()) if t not in NOISE]


def parse(text):
    head, entries, pre = [], [], []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#EXTM3U"):
            head.append(line)
        elif line.startswith("#"):
            pre.append(line)
        else:
            entries.append({"meta": pre, "url": line})
            pre = []
    return head, entries


def info(entry):
    name, group = "", ""
    for line in entry["meta"]:
        if line.startswith("#EXTINF"):
            if "," in line:
                name = line.rsplit(",", 1)[-1].strip()
            m = re.search(r'group-title="([^"]*)"', line)
            if m:
                group = m.group(1)
        elif line.startswith("#EXTGRP:") and not group:
            group = line.split(":", 1)[1].strip()
    return name, group


def is_protected(name, group, cfg, first_group):
    g = group.strip().lower()
    if cfg["protect_first_group"] and first_group and g == first_group:
        return True
    if g and g in [x.lower() for x in cfg["protected_groups"]]:
        return True
    nt = tokens(name)
    for p in cfg["protected_names"]:
        pt = tokens(p)
        if pt and nt[:len(pt)] == pt:
            return True
    return False


# ---------- проверка потока ----------
def _alive(url, timeout, depth=0):
    r = S.get(url, timeout=timeout, stream=True, allow_redirects=True)
    try:
        if r.status_code != 200:
            return False
        chunk = next(r.iter_content(65536), b"")
        if not chunk:
            return False
        if chunk.lstrip()[:7] == b"#EXTM3U":
            if depth > 3:
                return False
            lines = [l.strip() for l in chunk.decode("utf-8", "ignore").splitlines()]
            uris = [l for l in lines if l and not l.startswith("#")]
            if not uris:
                return False
            if any(l.startswith("#EXT-X-STREAM-INF") for l in lines):
                return _alive(urljoin(r.url, uris[0]), timeout, depth + 1)
            seg = S.get(urljoin(r.url, uris[-1]), timeout=timeout, stream=True,
                        headers={"Range": "bytes=0-4095"})
            try:
                return seg.status_code in (200, 206) and bool(next(seg.iter_content(1024), b""))
            finally:
                seg.close()
        ctype = r.headers.get("content-type", "").lower()
        return not ctype.startswith("text/html")
    finally:
        r.close()


def alive(url, timeout):
    for attempt in range(2):
        try:
            if _alive(url, timeout):
                return True
        except Exception:
            pass
        if attempt == 0:
            time.sleep(2)
    return False


# ---------- источники замен ----------
def rutube_stream(video_id):
    r = S.get(f"https://rutube.ru/api/play/options/{video_id}/",
              params={"no_404": "true", "referer": "https://rutube.ru"}, timeout=15)
    vb = r.json().get("video_balancer") or {}
    return vb.get("m3u8") or vb.get("default")


def rutube_search(name):
    r = S.get("https://rutube.ru/api/search/video/",
              params={"query": f"{name} прямой эфир"}, timeout=15)
    want = set(tokens(name))
    n = 0
    for v in r.json().get("results", []):
        if v.get("is_livestream") and want <= set(tokens(v.get("title", ""))):
            yield v["id"]
            n += 1
            if n >= 5:
                return


OPT_PREFIXES = ("#EXTVLCOPT", "#EXTHTTP")


def load_one(url, label, skip_groups):
    _, items = parse(S.get(url, timeout=30).text)
    for it in items:
        name, group = info(it)
        if any(x.lower() in group.lower() for x in skip_groups):
            continue
        key = " ".join(tokens(name))
        if key:
            opts = [l for l in it["meta"] if l.startswith(OPT_PREFIXES)]
            yield key, label, it["url"], opts


def load_sources(cfg):
    """Все запасные плейлисты -> {нормализованное имя: [(источник, url, opts)]}."""
    sources = []
    for u in cfg["extra_playlists"]:
        parts = u.split("/")
        label = "плейлист " + "/".join(parts[3:5]) if len(parts) > 4 else "доп. плейлист"
        sources.append((u, label, cfg["extra_skip_groups_contains"]))
    if cfg["use_iptv_org"]:
        sources.append((cfg["iptv_org_url"], "iptv-org", []))
    idx = {}
    for url, label, skip in sources:
        try:
            n = 0
            for key, lab, u, opts in load_one(url, label, skip):
                idx.setdefault(key, []).append((lab, u, opts))
                n += 1
            print(f"Загружен {label}: {n} потоков")
        except Exception as e:
            print(f"! не удалось загрузить {url}: {e}")
    return idx


def resolve(cand):
    """'rutube:<url|id>' -> m3u8, остальное — как есть."""
    if cand.startswith("rutube:"):
        m = re.search(r"[0-9a-f]{32}", cand)
        return rutube_stream(m.group(0)) if m else None
    return cand


def find_replacement(name, fallbacks, sources, cfg):
    key = " ".join(tokens(name))
    tried = set()

    def cands():
        for c in fallbacks.get(key, []):
            yield "fallbacks.json", c, []
        for lab, u, opts in sources.get(key, []):
            yield lab, u, opts
        if cfg["use_rutube_search"]:
            try:
                for vid in rutube_search(name):
                    yield "Rutube (поиск)", f"rutube:{vid}", []
            except Exception as e:
                print(f"  ! поиск Rutube для «{name}»: {e}")

    for label, c, opts in cands():
        try:
            url = resolve(c)
        except Exception:
            continue
        if not url or url in tried:
            continue
        tried.add(url)
        if alive(url, cfg["timeout"]):
            return label, url, opts
    return None


# ---------- main ----------
def main():
    cfg = dict(DEFAULTS)
    cfg_path = os.environ.get("IPTV_CONFIG", "iptv_config.json")
    if os.path.exists(cfg_path):
        cfg.update(json.load(open(cfg_path, encoding="utf-8")))

    head, entries = parse(open(cfg["source"], encoding="utf-8").read())

    fallbacks = {}
    if os.path.exists(cfg["fallbacks_file"]):
        raw = json.load(open(cfg["fallbacks_file"], encoding="utf-8"))
        fallbacks = {" ".join(tokens(k)): v for k, v in raw.items()}

    first_group = next((info(e)[1].strip().lower() for e in entries if info(e)[1].strip()), "")
    for e in entries:
        e["name"], e["group"] = info(e)
        e["protected"] = is_protected(e["name"], e["group"], cfg, first_group)

    todo = [e for e in entries if not e["protected"]]
    print(f"Каналов: {len(entries)}, защищённых: {len(entries) - len(todo)}, проверяю: {len(todo)}")

    with ThreadPoolExecutor(cfg["workers"]) as ex:
        for e, ok in zip(todo, ex.map(lambda e: alive(e["url"], cfg["timeout"]), todo)):
            e["alive"] = ok
    dead = [e for e in todo if not e["alive"]]

    report = []
    if todo and len(dead) / len(todo) > cfg["max_dead_ratio"]:
        msg = (f"⚠️ «Мёртвыми» оказалось {len(dead)} из {len(todo)} — похоже на блокировку "
               f"раннера GitHub или сбой сети. Замену не делаю, плейлист остаётся как есть.")
        print(msg)
        report.append(msg)
        dead = []

    sources = load_sources(cfg) if dead else {}
    replaced = failed = 0
    for e in dead:
        res = find_replacement(e["name"], fallbacks, sources, cfg)
        if res:
            label, url, opts = res
            e["url"] = url
            e["meta"] = [l for l in e["meta"] if not l.startswith(OPT_PREFIXES)] + opts
            replaced += 1
            report.append(f"✅ {e['name']} → {label}")
        else:
            failed += 1
            e["drop"] = cfg["drop_dead"]
            report.append(f"❌ {e['name']}: замены нет" + (" (удалён)" if cfg["drop_dead"] else " (оставлен как был)"))

    out = (head or ["#EXTM3U"])[:1]
    for e in entries:
        if e.get("drop"):
            continue
        out += e["meta"] + [e["url"]]
    open(cfg["output"], "w", encoding="utf-8", newline="\n").write("\n".join(out) + "\n")

    summary = (f"## Пересборка плейлиста\n\nВсего: {len(entries)} · защищено: {len(entries) - len(todo)} · "
               f"нерабочих: {len(dead)} · заменено: {replaced} · без замены: {failed}\n\n" + "\n".join(f"- {r}" for r in report))
    print(summary)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(summary + "\n")


if __name__ == "__main__":
    sys.exit(main())
