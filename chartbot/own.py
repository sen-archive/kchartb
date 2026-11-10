from html import unescape as html_unescape
import base64
import dataclasses
import glob
import json
import logging
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from chartbot import predtable, scoregraph
from chartbot.fetch import SPECS, Chart, Row
from chartbot.history import HRow, SongHistory
from chartbot.render import body_text, post_title, render_html, render_png

log = logging.getLogger("chartbot.own")

KEYS = ("own_top100", "own_rt", "own_users", "own_hot5", "own_melon5", "own_predict", "own_hot100", "own_graph", "own_genie", "own_bugs", "own_flo")

def _rec():
    import recorder
    return recorder

def _arts(x):
    return ", ".join(a["ARTISTNAME"] for a in (x or {}).get("ARTISTLIST", []))

def _int(v):
    try:
        n = int(v)
        return n if n > 0 else None
    except Exception:
        return None

class Own:
    def __init__(self, out, live=None, gaisum=None):
        self.out = Path(out)
        self.dir = self.out / "own"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.live = Path(live) if live else None
        self.gaisum = Path(gaisum) if gaisum else None
        self.rec = _rec()
        self.rec.DATA = self.dir
        self.cache = {}
        self.now = datetime.now()

    def poll_age(self):
        n = self.now
        return 5.0 if (n.minute % 5) * 60 + n.second < 90 or n.minute < 8 else 30.0

    def get(self, name, max_age=None):
        if max_age is None:
            max_age = self.poll_age()
        c = self.cache.get(name)
        if c and time.monotonic() - c[0] < max_age:
            return c[1]
        url, params = self.rec.RAW[name]
        o = self.rec.raw_get(url, params)
        d = self.dir / "raw" / name
        self.rec.save(d / f"{self.now:%Y%m%d-%H%M}.json", o)
        for f in sorted(d.glob("*.json"))[:-40]:
            f.unlink(missing_ok=True)
        self.cache[name] = (time.monotonic(), o)
        return o

    def covers(self, hourly, ids=None):
        self.rec.covers_small(hourly)

    def images(self, items):
        out = {}
        for x in items:
            f = self.dir / "raw" / "covers_s" / f"{x.get('ALBUMID')}.jpg"
            if f.exists():
                out[str(x["ALBUMID"])] = "data:image/jpeg;base64," + base64.b64encode(f.read_bytes()).decode()
        return out

    WEB_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"

    def web_top100(self):
        c = self.__dict__.get("_web")
        if c and time.monotonic() - c[0] < self.poll_age():
            return c[1]
        r = self.fetch_web_top100()
        self._web = (time.monotonic(), r)
        return r

    def fetch_web_top100(self):
        import re
        import urllib.request
        req = urllib.request.Request("https://www.melon.com/chart/index.htm", headers={"User-Agent": self.WEB_UA, "Accept-Language": "ko-KR,ko;q=0.9"})
        h = urllib.request.urlopen(req, timeout=20).read().decode("utf-8")
        hour = re.search(r'<span class="hour">(\d\d:\d\d)</span>', h)
        rows = []
        for tr in re.findall(r'<tr[^>]*data-song-no="\d+".*?</tr>', h, re.S):
            rk = re.search(r'<span class="rank ?">(\d+)</span>', tr)
            nm = re.search(r'class="ellipsis rank01">.*?<a[^>]*>(.*?)</a>', tr, re.S)
            ar = re.search(r'class="ellipsis rank02">\s*(.*?)<span class="checkEllipsis"', tr, re.S)
            al = re.search(r'albumId=(\d+)', tr)
            im = re.search(r'<img[^>]*src="([^"]+)"', tr)
            ch = re.search(r'class="rank_wrap"', tr) and re.search(r'<span title="([^"]*)" class="rank_wrap"', tr)
            if not (rk and nm):
                continue
            rank = int(rk.group(1))
            t = ch.group(1) if ch else ""
            n = re.search(r"(\d+)", t)
            if "상승" in t and n:
                prev = rank + int(n.group(1))
            elif "하락" in t and n:
                prev = rank - int(n.group(1))
            elif "동일" in t:
                prev = rank
            else:
                prev = None
            artists = re.sub(r"<[^>]+>", "", ar.group(1)) if ar else ""
            sid = re.search(r'data-song-no="(\d+)"', tr)
            rows.append({"rank": rank, "cur": prev, "name": html_unescape(nm.group(1).strip()), "artists": html_unescape(artists.strip()),
                         "image": al.group(1) if al else str(rank), "url": im.group(1) if im else "", "sid": sid.group(1) if sid else ""})
        return (hour.group(1) if hour else None), rows

    def web_images(self, rows, referer="https://www.melon.com/"):
        import hashlib
        import os
        import urllib.request
        from concurrent.futures import ThreadPoolExecutor
        cdir = self.out / "covers"
        cdir.mkdir(parents=True, exist_ok=True)

        def one(r):
            f = cdir / (hashlib.sha1(f"{r.get('name') or ''}|{r.get('artists') or ''}|{r['image']}".encode()).hexdigest() + ".bin")
            if f.exists() and f.stat().st_size > 0:
                os.utime(f)
                return r["image"], "data:image/jpeg;base64," + base64.b64encode(f.read_bytes()).decode()
            if not r["url"]:
                return r["image"], None
            try:
                u = r["url"].replace("/resize/120/", "/resize/160/")
                req = urllib.request.Request(u, headers={"User-Agent": self.WEB_UA, "Referer": referer})
                data = urllib.request.urlopen(req, timeout=15).read()
                f.write_bytes(data)
                return r["image"], "data:image/jpeg;base64," + base64.b64encode(data).decode()
            except Exception:
                return r["image"], None
        with ThreadPoolExecutor(16) as ex:
            out = {k: v for k, v in ex.map(one, rows) if v}
        files = sorted(cdir.glob("*.bin"), key=lambda x: x.stat().st_mtime)
        for old in files[:-4000]:
            old.unlink(missing_ok=True)
        return out

    LOGOS = {"melon": "https://cdnimg.melon.co.kr/resource/mobile40/cds/common/image/mobile_apple_180x180.png"}

    def logo(self, plat):
        import urllib.request
        cache = self.__dict__.setdefault("_logos", {})
        if plat not in cache:
            try:
                b = urllib.request.urlopen(urllib.request.Request(self.LOGOS[plat], headers={"User-Agent": self.WEB_UA}), timeout=15).read()
                cache[plat] = predtable.mark("data:image/png;base64," + base64.b64encode(b).decode(), None, 30)
            except Exception:
                log.warning("아이콘을 받지 못함: %s", plat)
                return None
        return cache[plat]

    def top100(self, now, state):
        if now.minute >= 20:
            return None
        stamp = f"{now:%Y%m%d%H}00"
        if state.get("own_top100") == stamp:
            return None
        hour, rows = self.web_top100()
        if hour != f"{now:%H}:00" or len(rows) < 100:
            return None
        title = f"{now:%Y.%m.%d} {now:%H}:00 멜론 TOP100 순위표"
        html = predtable.page(title, rows, self.web_images(rows), logo=self.logo("melon"))
        return self.job("own_top100", stamp, f"[{now:%H}:00] 멜론 TOP100 순위표", "", html, 1.5)

    def hot100(self, now, state):
        if now.minute >= 40:
            return None
        stamp = f"{now:%Y%m%d%H}00"
        if state.get("own_hot100") == stamp:
            return None
        o = self.get("hot100")["response"]
        if o.get("RANKHOUR") != f"{now:%H}:00":
            return None
        items = o.get("SONGLIST") or []
        if len(items) < 100:
            return None
        rows = [{"rank": _int(x["CURRANK"]), "cur": None if x.get("RANKTYPE") == "NEW" else _int(x.get("PASTRANK")), "name": x["SONGNAME"],
                 "artists": _arts(x), "image": str(x["ALBUMID"]), "url": x.get("ALBUMIMGPATH") or x.get("ALBUMIMG") or ""} for x in items[:100]]
        title = f"{now:%Y.%m.%d} {now:%H}:00 멜론 HOT100 순위표"
        html = predtable.page(title, rows, self.web_images(rows), logo=self.logo("melon"), brand="멜론")
        return self.job("own_hot100", stamp, f"[{now:%H}:00] 멜론 HOT100 순위표", "", html, 1.5)

    def hourly(self, now):
        o = self.get("hourly")
        want = f"{now:%H}:00"
        if (o.get("response") or {}).get("RANKHOUR") == want and self.__dict__.get("_hlog") != want:
            self._hlog = want
            log.info("실시간 차트 데이터 확인 %s", f"{now:%H:%M:%S}")
        return o

    def realtime(self, now, state):
        if now.minute >= 40:
            return None
        stamp = f"{now:%Y%m%d%H}00"
        if state.get("own_rt") == stamp:
            return None
        o = self.hourly(now)["response"]
        if o.get("RANKHOUR") != f"{now:%H}:00":
            return None
        items = o.get("CHARTLIST") or []
        if len(items) < 100:
            return None
        rows = [{"rank": _int(x["CURRANK"]), "cur": None if x.get("RANKTYPE") == "NEW" else _int(x.get("PASTRANK")), "name": x["SONGNAME"],
                 "artists": _arts(x), "image": str(x["ALBUMID"]), "url": x.get("ALBUMIMGPATH") or x.get("ALBUMIMG") or ""} for x in items[:100]]
        title = f"{now:%Y.%m.%d} {now:%H}:00 멜론 실시간 차트"
        html = predtable.page(title, rows, self.web_images(rows), logo=self.logo("melon"), brand="멜론")
        return self.job("own_rt", stamp, f"[{now:%H}:00] 멜론 실시간 차트", "", html, 1.5)

    def graph(self, now, state):
        if now.minute >= 40:
            return None
        stamp = f"{now:%Y%m%d%H}00"
        if state.get("own_graph") == stamp:
            return None
        o = self.get("hourtop")["response"]
        if o.get("RANKHOUR") != f"{now:%H}:00":
            return None
        hours = (o.get("XCATE") or "").split(",")
        gl = o.get("GRAPHDATALIST") or []
        if len(gl) < 3 or not hours or hours[-1] != f"{now:%H}":
            return None
        slots = [f"{(now - timedelta(hours=23 - i)):%H}" for i in range(24)]
        series = []
        for g in gl[:3]:
            by = {p.get("TIME") or h: p.get("VAL") for h, p in zip(hours, g.get("GRAPHDATA") or [])}
            vals = [float(by[h]) if by.get(h) not in (None, "") else None for h in slots]
            if vals[-1] is None:
                return None
            series.append({"name": (g.get("GRAPHCHARTINFO") or {}).get("SONGNAME") or str(g.get("SONGID")), "values": vals})
        title = f"{now:%Y.%m.%d} {now:%H}:00 멜론 실시간 그래프"
        html = scoregraph.hour_page(title, [f"{h}시" for h in slots], series)
        return self.job("own_graph", stamp, f"[{now:%H}:00] 멜론 실시간 그래프", "", html, 1.6)

    PLATS = {"own_genie": ("genie", "지니", "지니 실시간 차트"), "own_bugs": ("bugs", "벅스", "벅스 실시간 차트"), "own_flo": ("flo", "플로", "플로 24시간 차트")}
    RANK_GRAPHS = {}
    PLAT_REFERER = {"genie": "https://www.genie.co.kr/", "bugs": "https://music.bugs.co.kr/", "flo": "https://www.music-flo.com/", "melon": "https://www.melon.com/", "melonhot": "https://www.melon.com/"}
    RETRY_GAP = 30

    def snapshot(self, now, state, key, platform, name):
        if now.minute >= 20:
            return None
        if state.get(key) == f"{now:%Y%m%d%H}00":
            return None
        retry = self.__dict__.setdefault("_retry", {})
        if time.monotonic() < retry.get(key, 0):
            return None
        from chartbot.direct import ensure_snapshot
        from chartbot.fetch import Fetcher
        if not hasattr(self, "fetcher"):
            self.fetcher = Fetcher(user_agent=self.WEB_UA, min_gap=3.0)
        try:
            stale = self.__dict__.setdefault("_stale", set())
            st = ensure_snapshot(platform, now, self.fetcher, self.out / "rank", force=key in stale)
        except Exception:
            retry[key] = time.monotonic() + self.RETRY_GAP
            raise
        data = (st.get("latest") or {}).get("rows") or []
        if len(data) < 100:
            retry[key] = time.monotonic() + self.RETRY_GAP
            log.warning("%s 차트 곡 수가 부족해 건너뜀 (%d)", name, len(data))
            return None
        sids = [d["sid"] for d in data]
        last = self.dir / f"last_{key}.json"
        if now.minute < 10 and last.exists() and json.loads(last.read_text(encoding="utf-8")) == sids:
            retry[key] = time.monotonic() + self.RETRY_GAP
            stale.add(key)
            return None
        stale.discard(key)
        last.write_text(json.dumps(sids), encoding="utf-8")
        return st

    def plat(self, now, state, key):
        platform, name, label = self.PLATS[key]
        st = self.snapshot(now, state, key, platform, name)
        if not st:
            return None
        rows = [{"rank": d["rank"], "cur": d["previous"], "name": d["name"], "artists": d["artists"],
                 "image": d["sid"] or str(d["rank"]), "url": self.cover_url(platform, d["image"])} for d in st["latest"]["rows"][:100]]
        title = f"{now:%Y.%m.%d} {now:%H}:00 {label}"
        html = predtable.page(title, rows, self.web_images(rows, self.PLAT_REFERER[platform]), brand=name)
        return self.job(key, f"{now:%Y%m%d%H}00", f"[{now:%H}:00] {label}", "", html, 1.5)

    def rank_graph(self, now, state, key):
        from chartbot.direct import PLATFORM_NAMES, build_chart
        from chartbot.render import embed_images
        platform = self.RANK_GRAPHS[key]
        st = self.snapshot(now, state, key, platform, PLATFORM_NAMES[platform])
        if not st:
            return None
        spec = SPECS[f"{platform}_rank_graph"]
        chart = build_chart(spec, now, st)
        images = embed_images(chart, self.fetcher, self.out / "cache", budget=10, gap=0.5, size=None)
        return self.job(key, f"{now:%Y%m%d%H}00", post_title(chart), self.body(chart), render_html(chart, images), 2.0, chart)

    @staticmethod
    def cover_url(platform, url):
        if platform == "bugs":
            return url.replace("/images/50/", "/images/130/")
        return url

    def predict_extra(self, by, known, url, base, one):
        empty = [k for k in range(1, 101) if k not in by]
        if not empty:
            return {}
        try:
            o = self.get("hot100")["response"]
            cand = []
            for x in o.get("SONGLIST") or []:
                sid = str(x.get("SONGID") or "")
                if not sid or sid in known:
                    continue
                cand.append({"SONGID": sid, "SONGNAME": x["SONGNAME"], "ARTISTS": _arts(x), "ALBUMID": str(x["ALBUMID"]),
                             "WEB": {"rank": None, "name": x["SONGNAME"], "artists": _arts(x), "image": str(x["ALBUMID"]),
                                     "url": x.get("ALBUMIMGPATH") or x.get("ALBUMIMG") or ""}})
            if not cand:
                return {}
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(16) as ex:
                res = list(ex.map(one, cand))
        except Exception:
            log.exception("예측 빈칸 보충 실패")
            return {}
        out = {}
        for x, r in res:
            k = _int((r or {}).get("predicted"))
            if k in empty:
                out.setdefault(k, []).append((x, r))
        log.info("예측 빈칸 %d개 중 %d개를 HOT100 곡으로 채움", len(empty), len(out))
        return out

    def predict(self, now, state):
        if now.minute < 30 or now.minute % 10 >= 6:
            return None
        slot = now.minute // 10 * 10
        stamp = f"{now:%Y%m%d%H}{slot:02d}"
        if state.get("own_predict") == stamp:
            return None
        hour, web = self.web_top100()
        if hour != f"{now:%H}:00" or len(web) < 100 or not all(w["sid"] for w in web):
            return None
        items = [{"SONGID": w["sid"], "SONGNAME": w["name"], "ARTISTS": w["artists"], "ALBUMID": w["image"], "WEB": w} for w in web]
        url, base = self.rec.REPORT
        first = self.rec.report_lite(self.rec.raw_get(url, {**base, "songId": items[0]["SONGID"]}), items[0]["SONGID"])
        if first.get("recent") != f"{now:%H}:{slot:02d}":
            return None
        from concurrent.futures import ThreadPoolExecutor

        def one(x):
            try:
                o = self.rec.raw_get(url, {**base, "songId": str(x["SONGID"])})
                return x, self.rec.report_lite(o, str(x["SONGID"]))
            except Exception as e:
                log.warning("예측 수집 실패 %s %s", x.get("SONGID"), type(e).__name__)
                return x, None

        got = {}
        todo = list(items)
        with ThreadPoolExecutor(16) as ex:
            for attempt in range(4):
                if not todo:
                    break
                if attempt:
                    time.sleep(2)
                for x, r in ex.map(one, todo):
                    if _int((r or {}).get("predicted")):
                        got[x["SONGID"]] = (x, r)
                todo = [x for x in todo if x["SONGID"] not in got]
        by = {}
        for x, r in got.values():
            by.setdefault(_int(r.get("predicted")), []).append((x, r))
        if len(by) < 50:
            log.warning("예측 값이 너무 적어 건너뜀 (%d)", len(by))
            return None
        extra = self.predict_extra(by, {str(w["sid"]) for w in web}, url, base, one)
        for k, c in extra.items():
            by[k] = c
        imgs = list(web) + [x["WEB"] for c in extra.values() for x, _ in c]
        rows = []
        for k in range(1, 101):
            c = by.get(k)
            if not c:
                rows.append({"rank": k, "unknown": True})
                continue
            x, r = sorted(c, key=lambda z: _int(z[1].get("rank")) or 999)[0]
            rows.append({"rank": k, "cur": _int(r.get("rank")) or x["WEB"]["rank"], "name": x["SONGNAME"], "artists": x["ARTISTS"], "image": x["ALBUMID"]})
        title = f"{now:%Y.%m.%d} {now:%H}:{slot:02d} 멜론 TOP100 순위 예측"
        html = predtable.page(title, rows, self.web_images(imgs), logo=self.logo("melon"))
        return self.job("own_predict", stamp, f"[{now:%H}:{slot:02d}] 멜론 TOP100 순위 예측", "", html, 1.5)

    def five_hours(self):
        import derive
        derive.five(self.dir)
        derive.melon5(self.dir)
        hours = {}
        for f in sorted((self.dir / "derived" / "five").glob("*.json")):
            hours[f.stem] = json.loads(f.read_text(encoding="utf-8"))
        m5 = {}
        for f in sorted((self.dir / "derived" / "melon5").glob("*.json")):
            x = json.loads(f.read_text(encoding="utf-8"))
            m5[f.stem] = {sid: {"name": s["name"], "vals": {int(l[3:]): v for l, v in s["vals"].items()}} for sid, s in x["songs"].items()}
        return hours, m5

    def five(self, now, state, kind):
        key = "own_hot5" if kind == "hot" else "own_melon5"
        name = "five" if kind == "hot" else "fivetop"
        m = now.minute // 5 * 5
        if m == 0:
            return None
        stamp = f"{now:%Y%m%d%H}{m:02d}"
        if state.get(key) == stamp:
            return None
        self.get(name)
        hours, m5 = self.five_hours()
        hk = f"{now:%Y%m%d}-{now:%H}00"
        if kind == "hot":
            labs = {int(l[3:]) for s in (hours.get(hk) or {"songs": {}})["songs"].values() for l in s["hot"]}
        else:
            labs = {mm for e in (m5.get(hk) or {}).values() for mm in e["vals"]}
        if m not in labs:
            return None
        ms = list(range(0, m + 1, 5))
        items = []
        if kind == "hot":
            for s in (hours.get(hk) or {"songs": {}})["songs"].values():
                if s["hot"].get(f"{now:%H}:{m:02d}") is None:
                    continue
                items.append((s["name"], [s["hot"].get(f"{now:%H}:{x:02d}") or 0.0 for x in ms]))
            n, spec = 5, "melon_hot100_five"
        else:
            items = [(e["name"], [e["vals"].get(x) or 0.0 for x in ms]) for e in (m5.get(hk) or {}).values() if m in e["vals"]]
            n, spec = 3, "melon_five"
        items.sort(key=lambda z: -z[1][-1])
        rows = []
        for i, (nm, vs) in enumerate(items[:n]):
            vals = [None] * 13
            for j, v in enumerate(vs):
                vals[j] = v
            rows.append(Row(rank=i + 1, previous=None, name=nm, artists="", album="", image="", values=vals))
        chart = Chart(dataclasses.replace(SPECS[spec], source="direct"), now.replace(minute=0, second=0, microsecond=0), rows)
        return self.job(key, stamp, post_title(chart), self.body(chart), render_html(chart), 2.0, chart)

    @staticmethod
    def read_hourtop(files, cnt):
        for f in files:
            try:
                r = json.load(open(f))["response"]
                key = r["RANKDAY"].replace(".", "") + "-" + r["RANKHOUR"][:2] + "00"
                st = Path(f).stem
                if r["RANKHOUR"][:2] == "01" and 2 <= int(st[9:11]) <= 7:
                    if int(st[11:13]) < 6:
                        continue
                    key = st[:11] + "00"
                for g in r["GRAPHDATALIST"]:
                    v = g["CHARTSTATIC"]["SUMMCNT"]
                    if v:
                        cnt[(key, str(g["SONGID"]))] = v
            except Exception:
                continue

    def users_base(self):
        c = self.__dict__.get("_ubase")
        if c:
            return c
        cnt, gai, rt, t100 = {}, {}, {}, {}
        if self.live:
            self.read_hourtop(sorted(glob.glob(str(self.live / "raw/hourtop/*.json"))), cnt)
        if self.gaisum:
            for f in glob.glob(str(self.gaisum / "*.json")):
                x = json.load(open(f))
                for r in x["rows"]:
                    gai[(x["hour"], str(r["sid"]))] = r["count"]
        if self.live:
            for f in glob.glob(str(self.live / "derived/realtime/*.json")):
                x = json.load(open(f))
                for r in x["rows"]:
                    rt[(x["hour"], str(r["id"]))] = r["rank"]
            for f in glob.glob(str(self.live / "top100/*.json")):
                x = json.load(open(f))
                for r in x["rows"]:
                    t100[(x["time"], r["name"])] = r["rank"]
        self._ubase = (cnt, gai, rt, t100)
        return self._ubase

    def users(self, now, state):
        if now.minute >= 20:
            return None
        stamp = f"{now:%Y%m%d%H}00"
        if state.get("own_users") == stamp:
            return None
        ht = self.get("hourtop")["response"]

        frozen = ht["RANKHOUR"][:2] == "01" and 2 <= now.hour <= 7
        if ht["RANKHOUR"][:2] != f"{now:%H}" and not frozen:
            return None
        got = {str(g["SONGID"]): (g.get("CHARTSTATIC") or {}).get("SUMMCNT") for g in ht.get("GRAPHDATALIST") or []}
        if frozen:
            if now.minute < 6 or len([v for v in got.values() if v]) < 3:
                return None
            ph = (now - timedelta(hours=1)).strftime("%Y%m%d-%H")
            pf = sorted(f for d in ([self.live] if self.live else []) + [self.dir]
                        for f in glob.glob(str(d / "raw/hourtop" / f"{ph}*.json")) if int(Path(f).stem[11:13]) >= 6)
            if pf:
                try:
                    old = {str(g["SONGID"]): (g.get("CHARTSTATIC") or {}).get("SUMMCNT") for g in json.load(open(pf[-1]))["response"]["GRAPHDATALIST"]}
                    if old == got:
                        return None
                except Exception:
                    pass
        hourly = self.get("hourly")
        need = [str(x["SONGID"]) for x in hourly["response"]["CHARTLIST"][:3]]
        if not frozen and (len([v for v in got.values() if v]) < 3 or any(not got.get(i) for i in need)):
            return None
        end = now.replace(minute=0, second=0, microsecond=0)
        cb, gai, rb, tb = self.users_base()
        cnt, rt, t100 = dict(cb), dict(rb), dict(tb)
        self.read_hourtop(sorted(glob.glob(str(self.dir / "raw/hourtop/*.json"))), cnt)
        items = hourly["response"]["CHARTLIST"]
        hk = f"{end:%Y%m%d-%H}00"
        for x in items:
            rt[(hk, str(x["SONGID"]))] = _int(x["CURRANK"])
        try:
            hg = self.get("hourgraph")["response"]
            ex = hg["ENTCHARTXCATE"]
            for g in hg["GRAPHDATALIST"]:
                for i, e in enumerate(g.get("ENTGRAPHDATA", [])):
                    t = end.replace(hour=int(ex[i][:2]))
                    if t > end:
                        t -= timedelta(days=1)
                    if e.get("RANK"):
                        t100[(t.strftime("%Y%m%d-%H00"), g["GRAPHCHARTINFO"]["SONGNAME"])] = int(e["RANK"])
        except Exception:
            log.warning("T100 수집 실패")
        top = items[:3]
        self.rec.covers([ht])
        cov = {}
        for g in ht["GRAPHDATALIST"]:
            f = self.dir / "raw" / "covers" / f"{g['GRAPHCHARTINFO']['ALBUMID']}.jpg"
            if f.exists():
                cov[str(g["SONGID"])] = "data:image/jpeg;base64," + base64.b64encode(f.read_bytes()).decode()
        miss = [x for x in top if str(x["SONGID"]) not in cov]
        if miss and now.minute < 8 and not frozen:
            return None
        if miss:
            fb = self.web_images([{"name": x["SONGNAME"], "artists": _arts(x), "image": str(x["SONGID"]), "url": x.get("ALBUMIMGPATH") or x.get("ALBUMIMG") or ""} for x in miss])
            cov.update(fb)
        hist, rows = [], []
        for x in top:
            sid = str(x["SONGID"])
            hr = []
            first = (end - timedelta(hours=24)).strftime("%Y%m%d-%H00")
            older = [(k, v) for src in (cnt, gai) for (k, i), v in src.items() if i == sid and v and k < first]
            prev = max(older)[1] if older else None
            for i in range(24, -1, -1):
                t = end - timedelta(hours=i)
                key = t.strftime("%Y%m%d-%H00")
                c = cnt.get((key, sid)) or gai.get((key, sid))
                hr.append(HRow(time=t, rank=rt.get((key, sid)), t100=t100.get((key, x["SONGNAME"])), count=c, diff=(c - prev) if (c and prev) else None))
                if c:
                    prev = c
            cs = [h.count for h in hr if h.count] + [v for (k, i), v in cnt.items() if i == sid and v] + [v for (k, i), v in gai.items() if i == sid and v]
            seed = 0
            try:
                import os
                pj = os.environ.get("PEAKS_JSON")
                if pj and Path(pj).exists():
                    for r in (json.load(open(pj)).get(sid) or {}).get("records", []):
                        if r.get("type") == "PEAK_COUNT":
                            seed = max(seed, int(r.get("score") or 0))
            except Exception:
                log.warning("역대 최고값 읽기 실패")
            peak = max(cs + [seed]) if (cs or seed) else None
            hist.append(SongHistory(sid=sid, name=x["SONGNAME"], artists=_arts(x), album="", image=cov.get(sid, ""), peak=peak, rows=hr))
            rows.append(Row(rank=_int(x["CURRANK"]), previous=_int(x.get("PASTRANK")), name=x["SONGNAME"], artists=_arts(x), album="", image="", sid=sid))
        chart = Chart(SPECS["melon_24h_count"], end, rows, history=hist)
        return self.job("own_users", stamp, post_title(chart), self.body(chart), render_html(chart, {v: v for v in cov.values()}), 1.6, chart)

    def body(self, chart):
        t = body_text(chart)
        c = chart.spec.credit
        return t[: -len(c)].rstrip() if c and t.endswith(c) else t

    def job(self, key, stamp, title, body, html, scale, chart=None):
        png = self.out / f"{key}_{stamp}.png"
        (self.out / f"{key}_{stamp}.html").write_text(html, encoding="utf-8")
        render_png(html, png, scale)
        return {"key": key, "stamp": stamp, "title": title, "body": body, "png": png}

    def jobs(self, now, state, enabled=None):
        self.now = now
        if "_ubase" not in self.__dict__ and now.minute >= 10:
            self.users_base()
        fns = (("own_predict", lambda: self.predict(now, state)),
               ("own_hot5", lambda: self.five(now, state, "hot")), ("own_melon5", lambda: self.five(now, state, "mel")),
               ("own_graph", lambda: self.graph(now, state)), ("own_top100", lambda: self.top100(now, state)), ("own_rt", lambda: self.realtime(now, state)),
               *[(k, lambda k=k: self.plat(now, state, k)) for k in self.PLATS],
               ("own_users", lambda: self.users(now, state)), ("own_hot100", lambda: self.hot100(now, state)),
               *[(k, lambda k=k: self.rank_graph(now, state, k)) for k in self.RANK_GRAPHS])
        for key, fn in fns:
            if enabled is not None and key not in enabled:
                continue
            t0 = time.monotonic()
            try:
                j = fn()
            except Exception:
                log.exception("own 차트 준비 실패")
                continue
            if time.monotonic() - t0 > 8:
                log.info("%s 준비에 %d초 걸림", key, time.monotonic() - t0)
            if j:
                yield j
