import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import rebuild_playlist as rp  # noqa: E402

CFG = rp.load_config(ROOT / "config" / "providers.json")
TS = b"\x47" + b"\x00" * 400
HLS = "#EXTM3U\n#EXT-X-VERSION:3\n#EXTINF:6,\nseg1.ts\n#EXTINF:6,\nseg2.ts\n"


class Fake:
    """Фиктивный fetch: никаких реальных сетевых запросов."""

    def __init__(self):
        self.map = {}
        self.calls = []

    def hls(self, url, ok=True):
        self.map[url] = rp.FetchResult(200, url, "application/vnd.apple.mpegurl", HLS.encode())
        base = url.rsplit("/", 1)[0]
        for s in ("seg1.ts", "seg2.ts"):
            self.map[f"{base}/{s}"] = (rp.FetchResult(200, f"{base}/{s}", "video/mp2t", TS) if ok
                                       else rp.FetchResult(404, error="http_404"))

    def put(self, url, result):
        self.map[url] = result

    def __call__(self, url, max_bytes=262144, byte_range=False, deadline=None):
        self.calls.append(url)
        return self.map.get(url, rp.FetchResult(error="dns_error"))


def src(*blocks):
    return "#EXTM3U url-tvg=\"http://epg.example/x.xml\"\n" + "\n".join(blocks) + "\n"


def blk(name, url, group="News", extra=""):
    return (f'#EXTINF:-1 tvg-id="{name}" tvg-name="{name}" tvg-logo="http://l/{name}.png" '
            f'group-title="{group}",{name}\n{extra}{url}')


def alt(*blocks):
    return "#EXTM3U\n" + "\n".join(blocks) + "\n"


def run(source, altp, fake, alt_error=None):
    out, rep = rp.rebuild(source, altp, alt_error, CFG, fake, workers=4)
    return out, rep


class Tests(unittest.TestCase):
    def test_01_working_stream_unchanged(self):
        f = Fake(); f.hls("http://good.example/a/index.m3u8")
        s = src(blk("Alpha", "http://good.example/a/index.m3u8"))
        out, rep = run(s, alt(), f)
        self.assertEqual(out, s)
        self.assertEqual(rep["summary"]["working_streams"], 1)
        self.assertEqual(rep["status"], "ok")

    def test_02_kanal_ne_rabotaet_forced_even_with_200(self):
        f = Fake()
        bad = "http://kanal-ne-rabotaet.live/a/index.m3u8"
        f.hls(bad)  # сервер отвечает 200 и сегменты тоже
        f.hls("http://ok.example/a/index.m3u8")
        s = src(blk("Alpha", bad))
        a = alt(blk("Alpha", "http://ok.example/a/index.m3u8", group="WINK"))
        out, rep = run(s, a, f)
        self.assertIn("http://ok.example/a/index.m3u8", out)
        self.assertNotIn("kanal-ne-rabotaet", out)
        self.assertEqual(rep["channels"][0]["need_reason"], "force_replace_domain")
        self.assertNotIn(bad, f.calls)  # HTTP-проверка не нужна и не делается

    def test_03_forbidden_source_replaced(self):
        f = Fake(); f.hls("http://ok.example/a/index.m3u8")
        s = src(blk("Alpha", "http://x.limehd.ru/a.m3u8"))
        a = alt(blk("Alpha", "http://ok.example/a/index.m3u8", group="Оргтехсервис"))
        out, rep = run(s, a, f)
        self.assertIn("ok.example", out)
        self.assertEqual(rep["channels"][0]["need_reason"], "forbidden_source")

    def test_04_forbidden_never_chosen_as_candidate(self):
        f = Fake()
        for u in ("http://mhd116.iptv2022.com/a/i.m3u8", "http://a.bolshoe.tv/a/i.m3u8",
                  "http://Lime-cdn.example/a/i.m3u8", "http://x.example/%6Cime/a/i.m3u8"):
            f.hls(u)
        s = src(blk("Alpha", "http://dead.example/a.m3u8"))
        a = alt(*[blk("Alpha", u, group="WINK") for u in (
            "http://mhd116.iptv2022.com/a/i.m3u8", "http://a.bolshoe.tv/a/i.m3u8",
            "http://Lime-cdn.example/a/i.m3u8", "http://x.example/%6Cime/a/i.m3u8")])
        out, rep = run(s, a, f)
        self.assertEqual(out, s)
        self.assertEqual(rep["channels"][0]["status"], "kept_unreplaced")
        self.assertEqual(rep["alternative_playlist"]["candidates"]["rejected_forbidden"], 4)

    def test_05_dead_gets_allowed_replacement(self):
        f = Fake(); f.hls("http://ok.example/b/i.m3u8")
        s = src(blk("Beta", "http://dead.example/b.m3u8"))
        a = alt(blk("Beta", "http://ok.example/b/i.m3u8", group="Ott Club + Реклама"))
        out, rep = run(s, a, f)
        self.assertIn("http://ok.example/b/i.m3u8", out)
        self.assertEqual(rep["summary"]["replacements_succeeded"], 1)
        self.assertEqual(rep["channels"][0]["new_provider"], "Ott Club + Реклама")

    def test_06_no_replacement_channel_kept(self):
        f = Fake()
        s = src(blk("Alpha", "http://dead.example/a.m3u8"), blk("Gamma", "http://dead2.example/g.m3u8"))
        out, rep = run(s, alt(blk("Zzz", "http://z.example/z.m3u8", group="WINK")), f)
        self.assertEqual(out, s)
        self.assertEqual(rep["summary"]["replacements_failed"], 2)
        self.assertEqual(rep["channels"][0]["failure"], "no_matching_candidates")

    def test_07_extinf_metadata_preserved(self):
        f = Fake(); f.hls("http://ok.example/a/i.m3u8")
        s = src(blk("Alpha", "http://dead.example/a.m3u8", group='Кино, "новости"'))
        a = alt(blk("Alpha", "http://ok.example/a/i.m3u8", group="WINK"))
        out, _ = run(s, a, f)
        self.assertEqual(out.replace("http://ok.example/a/i.m3u8", "http://dead.example/a.m3u8"), s)

    def test_08_complex_entries_not_corrupted(self):
        f = Fake(); f.hls("http://ok.example/a/i.m3u8"); f.hls("http://good.example/c/i.m3u8")
        s = ("#EXTM3U\n# комментарий\n#EXTGRP:Новости\n"
             '#EXTINF:-1 tvg-id="a" group-title="News" catchup="x,y",Alpha, HD\n'
             "#EXTVLCOPT:http-user-agent=Foo/1.0\n#KODIPROP:inputstream=x\nhttp://dead.example/a.m3u8\n\n"
             '#EXTINF:-1,Без атрибутов\r\nhttp://good.example/c/i.m3u8\n').replace("\r", "")
        a = alt(blk("Alpha, HD", "http://ok.example/a/i.m3u8", group="WINK"))
        out, rep = run(s, a, f)
        self.assertEqual(out, s.replace("http://dead.example/a.m3u8", "http://ok.example/a/i.m3u8"))
        self.assertEqual(rep["summary"]["total_channels"], 2)
        pl = rp.parse_m3u(s)
        self.assertEqual(pl.entries[0].name, "Alpha, HD")
        self.assertEqual(pl.entries[0].extra, ["#EXTVLCOPT:http-user-agent=Foo/1.0", "#KODIPROP:inputstream=x"])

    def test_09_alt_playlist_unavailable(self):
        f = Fake()
        s = src(blk("Alpha", "http://dead.example/a.m3u8"))
        out, rep = run(s, None, f, alt_error="http_503")
        self.assertEqual(out, s)
        self.assertEqual(rep["status"], "completed_with_warnings")
        self.assertEqual(rep["channels"][0]["failure"], "alternative_playlist_unavailable")
        self.assertTrue(any("альтернативный" in e for e in rep["errors"]))
        json.dumps(rep)

    def test_10_report_is_valid_json_via_cli(self):
        f = Fake()
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "s.m3u").write_text(src(blk("Alpha", "http://dead.example/a.m3u8")), encoding="utf-8")
            (d / "a.m3u").write_text(alt(), encoding="utf-8")
            # реальный CLI-запуск без сети: проверка URL падает с dns_error -> защита от массового сбоя не
            # срабатывает (каналов < 10), результат — исходный плейлист и валидный отчёт
            rc = rp.main(["--source", str(d / "s.m3u"), "--output", str(d / "o.m3u"), "--report",
                          str(d / "r.json"), "--config", str(ROOT / "config/providers.json"),
                          "--alt-file", str(d / "a.m3u"), "--workers", "2", "--timeout", "1"])
            self.assertEqual(rc, 0)
            rep = json.loads((d / "r.json").read_text(encoding="utf-8"))
            for k in rp.REPORT_KEYS:
                self.assertIn(k, rep)
            self.assertEqual(rp.main(["--verify-only", "--source", str(d / "s.m3u"), "--output",
                                      str(d / "o.m3u"), "--report", str(d / "r.json"),
                                      "--config", str(ROOT / "config/providers.json")]), 0)

    def test_11_similar_names_not_mixed(self):
        f = Fake(); f.hls("http://ok.example/r24/i.m3u8"); f.hls("http://ok.example/r1/i.m3u8")
        s = src(blk("Россия 1", "http://dead.example/1.m3u8"), blk("Первый канал +2", "http://dead.example/2.m3u8"))
        a = alt(blk("Россия 24", "http://ok.example/r24/i.m3u8", group="WINK"),
                blk("Первый канал", "http://ok.example/r1/i.m3u8", group="WINK"))
        out, rep = run(s, a, f)
        self.assertEqual(out, s)
        self.assertEqual(rep["summary"]["replacements_succeeded"], 0)
        # а «HD»-вариант того же канала — это тот же канал
        self.assertEqual(rp.channel_key("Alpha HD")[0], rp.channel_key("Alpha")[0])
        self.assertNotEqual(rp.channel_key("Матч ТВ")[0], rp.channel_key("Матч Футбол 1")[0])

    def test_12_providers_from_real_metadata_fields(self):
        mk = lambda block: rp.parse_m3u("#EXTM3U\n" + block + "\n").entries[0]
        prov, fields = CFG["allowed_providers"], CFG["provider_fields"]
        self.assertEqual(rp.entry_provider(mk('#EXTINF:-1 group-title="WINK",A\nhttp://x/a'), prov, fields), "WINK")
        self.assertEqual(rp.entry_provider(mk('#EXTGRP:Оргтехсервис\n#EXTINF:-1,A\nhttp://x/a'), prov, fields), "Оргтехсервис")
        self.assertEqual(rp.entry_provider(mk('#EXTINF:-1 group-title="Ott Club + Реклама",A\nhttp://x/a'), prov, fields), "Ott Club + Реклама")
        self.assertEqual(rp.entry_provider(mk('#EXTINF:-1 group-title="RUTUBE (VPN)",A\nhttp://x/a'), prov, fields), "RUTUBE (VPN)")
        # имя канала и логотип провайдером не считаются; «winks» — не WINK
        self.assertIsNone(rp.entry_provider(mk('#EXTINF:-1 tvg-logo="http://wink.ru/l.png" group-title="Winks",Wink Кино\nhttp://x/a'), prov, fields))
        self.assertIsNone(rp.entry_provider(mk('#EXTINF:-1 group-title="Other",A\nhttp://wink.ru/a'), prov, fields))

    def test_13_zero_replacements_ok(self):
        f = Fake(); f.hls("http://good.example/a/i.m3u8")
        s = src(blk("Alpha", "http://good.example/a/i.m3u8"))
        out, rep = run(s, alt(), f)
        self.assertEqual(rep["summary"]["needing_replacement"], 0)
        self.assertEqual(rep["summary"]["replacements_succeeded"], 0)
        self.assertEqual(out, s)

    def test_14_workflow_cli_args_supported(self):
        text = (ROOT / ".github/workflows/rebuild-playlist.yml").read_text(encoding="utf-8")
        self.assertRegex(text, r"(?m)^on:\s*\n\s+workflow_dispatch:")
        joined = re.sub(r"\\\n\s*", " ", text)
        known = {a for act in rp.build_parser()._actions for a in act.option_strings}
        found = 0
        for line in joined.splitlines():
            if "scripts/rebuild_playlist.py" in line:
                found += 1
                for flag in re.findall(r"(?<!\S)(--[a-z][a-z-]*)", line):
                    self.assertIn(flag, known, f"workflow использует неизвестный аргумент {flag}")
        self.assertGreaterEqual(found, 2)  # запуск + проверка

    # --- дополнительные -----------------------------------------------------
    def test_15_hls_segments_dead_means_dead(self):
        f = Fake(); f.hls("http://bad.example/a/i.m3u8", ok=False)
        r = rp.check_stream("http://bad.example/a/i.m3u8", f)
        self.assertEqual((r.state, r.reason), ("fail", "hls_segment_unavailable"))

    def test_16_html_page_is_not_a_stream(self):
        f = Fake(); f.put("http://x.example/s", rp.FetchResult(200, "http://x.example/s", "text/html", b"<html>" + b"a" * 400))
        self.assertEqual(rp.check_stream("http://x.example/s", f).reason, "html_instead_of_stream")

    def test_17_redirect_to_forbidden_host_rejected(self):
        f = Fake(); f.put("http://x.example/s", rp.FetchResult(200, "http://cdn.limehd.ru/s", "video/mp2t", TS))
        self.assertEqual(rp.check_stream("http://x.example/s", f, CFG["forbidden_url_fragments"]).state, "forbidden")

    def test_18_mass_failure_protects_playlist(self):
        f = Fake()  # всё недоступно, как при отсутствии сети у раннера
        s = src(*[blk(f"Ch{i}", f"http://d{i}.example/a.m3u8") for i in range(12)])
        out, rep = run(s, alt(blk("Ch1", "http://ok.example/x.m3u8", group="WINK")), f)
        self.assertIsNone(out)
        self.assertEqual(rep["status"], "failed")

    def test_19_secrets_redacted_in_report(self):
        f = Fake()
        s = src(blk("Alpha", "http://user:pw@dead.example/a/ABCDEFGHIJKLMNOPQRSTUVWX1234/i.m3u8?token=SECRET&x=1"))
        _, rep = run(s, alt(), f)
        dumped = json.dumps(rep)
        for secret in ("SECRET", "user:pw", "ABCDEFGHIJKLMNOPQRSTUVWX1234"):
            self.assertNotIn(secret, dumped)

    def test_20_protected_dead_channel_not_replaced_but_forbidden_is(self):
        f = Fake(); f.hls("http://ok.example/n/i.m3u8")
        s = src(blk("НТВ", "http://dead.example/n.m3u8"))
        a = alt(blk("НТВ", "http://ok.example/n/i.m3u8", group="WINK"))
        out, rep = run(s, a, f)
        self.assertEqual(out, s)
        self.assertEqual(rep["channels"][0]["status"], "protected_kept")
        s2 = src(blk("НТВ", "http://x.limehd.ru/n.m3u8"))
        out2, _ = run(s2, a, f)
        self.assertIn("ok.example", out2)

    def test_21_verify_detects_tampering(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            s = src(blk("Alpha", "http://a.example/a"))
            (d / "s").write_text(s, encoding="utf-8")
            (d / "o").write_text(s.replace("http://a.example/a", "http://x.limehd.ru/a"), encoding="utf-8")
            (d / "r").write_text(json.dumps({"generated_at": "x", "status": "ok", "summary": {}, "channels": [],
                                             "errors": [], "alternative_playlist": {}}), encoding="utf-8")
            probs = rp.verify_outputs(d / "s", d / "o", d / "r", CFG)
            self.assertTrue(any("без записи о замене" in p for p in probs))


if __name__ == "__main__":
    unittest.main(verbosity=2)
