from __future__ import annotations

import argparse
import random
import time
import json
import logging
import os
import sys
import time
import tomllib
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from chartbot.dcinside import DcUploader, HumanCheckRequired, SpamBlocked, SubmitUncertain, UploadFailed
from chartbot.fetch import SPECS, Fetcher, RateLimited, get_chart, resolve
from chartbot.direct import ensure_snapshot
from chartbot.samples import SampleFetcher, seed_sample_history
from chartbot.render import body_text, embed_images, png_scale, post_title, render_html, render_png

ROOT = Path(__file__).parent
KST = ZoneInfo("Asia/Seoul")
log = logging.getLogger("chartbot")

def load_config(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        sys.exit(f"설정 파일이 없습니다: {p}  (config.example.toml 을 복사해서 만드세요)")
    cfg = tomllib.loads(p.read_text(encoding="utf-8"))

    if os.environ.get("DC_GALLERY_ID"):
        cfg.setdefault("dc", {})["gallery_id"] = os.environ["DC_GALLERY_ID"]
    if "DC_HEADTEXT" in os.environ:
        cfg.setdefault("dc", {})["headtext"] = os.environ["DC_HEADTEXT"].strip()
    if os.environ.get("DC_KIND"):
        cfg.setdefault("dc", {})["kind"] = os.environ["DC_KIND"].strip()
    if os.environ.get("DC_MINOR"):
        cfg.setdefault("dc", {})["minor"] = os.environ["DC_MINOR"].lower() == "true"
    if os.environ.get("CHARTS", "").strip():
        cfg["bot"]["charts"] = [c.strip() for c in os.environ["CHARTS"].split(",") if c.strip()]
    if os.environ.get("CHART_FORCE", "").lower() == "true":
        cfg["bot"]["force"] = True
    if os.environ.get("MAX_POSTS", "").strip().isdigit():
        cfg["bot"]["max_posts"] = int(os.environ["MAX_POSTS"])
    if os.environ.get("DRY_RUN"):
        cfg["bot"]["dry_run"] = os.environ["DRY_RUN"].lower() != "false"
    return cfg

def make_uploader(cfg, headed):
    dc = cfg["dc"]
    state_file = None
    if os.environ.get("DC_STORAGE_STATE") or os.environ.get("DC_ID"):

        out_dir = Path(cfg["bot"].get("out_dir", ROOT / "out"))
        out_dir.mkdir(parents=True, exist_ok=True)
        state_file = str(out_dir / ".dc_session.json")

        import hashlib

        who = hashlib.sha256((os.environ.get("DC_ID") or "state").strip().encode()).hexdigest()[:16]
        marker = out_dir / ".dc_session.who"
        if marker.exists() and marker.read_text().strip() != who:
            Path(state_file).unlink(missing_ok=True)
            log.info("로그인 계정이 바뀌어 저장된 로그인 상태를 지웠습니다")
        marker.write_text(who)
        if os.environ.get("DC_STORAGE_STATE") and not Path(state_file).exists():
            fd = os.open(state_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(os.environ["DC_STORAGE_STATE"])
    return DcUploader(storage_state=state_file, login_id=os.environ.get("DC_ID"), login_pw=os.environ.get("DC_PW"),
        anon_nick=os.environ.get("DC_ANON_NICK"), anon_pw=os.environ.get("DC_ANON_PW"),
        gallery_id=dc["gallery_id"], headtext=dc.get("headtext", "차트봇"), profile_dir=dc.get("profile_dir", "~/.chartbot/profile"),
        selectors=dc.get("selectors"), minor=dc.get("minor", True), headed=headed,
        kind=dc.get("kind"),
    )

import threading
_POST_LOCK = threading.Lock()
_LAST_POST = [0.0]

def prepare(key, cfg, fetcher, out, state):
    spec = resolve(key)
    b = cfg["bot"]

    now = datetime.now(KST).replace(tzinfo=None)

    if spec.cadence == "hourly":

        if not b.get("force") and now.minute >= int(b.get("hourly_window_minutes", 15)):
            log.info("%s 정시 창(%d분) 밖이라 건너뜀 (%s)", spec.key, int(b.get("hourly_window_minutes", 15)), f"{now:%H:%M}")
            return None

        if spec.source != "direct" and now.minute < int(b.get("hourly_after_minute", 5)):
            return None
        if state.get(spec.key) == f"{now:%Y%m%d%H}00":
            return None

    chart = get_chart(spec.key, now, fetcher, cache_dir=out / "cache")
    stamp = f"{chart.stamp:%Y%m%d%H%M}"
    if state.get(spec.key) == stamp:
        log.info("%s %s 이미 처리함, 건너뜀", spec.key, stamp)
        return None

    size = 400 if spec.layout == "table_users" else (None if spec.source == "direct" else 80)
    budget, gap = int(b.get("image_budget", 30)), float(b.get("image_gap", 1.0))
    images = embed_images(chart, fetcher, out / "cache", budget=budget, gap=gap, size=size)
    missing = [r for r in chart.rows if r.image and r.image not in images]
    if missing:
        images = embed_images(chart, fetcher, out / "cache", budget=budget, gap=gap, size=size)
        missing = [r for r in chart.rows if r.image and r.image not in images]
        if missing:
            log.warning("%s 앨범 이미지 %d곡 못 받음: %s", spec.key, len(missing), ", ".join(f"{r.rank}위" for r in missing[:10]))
    import chartbot.render as _r
    if _r.RANK_STYLE == "list2" and spec.layout == "table":
        ok = _r.upgrade_covers(images, chart.rows)
        log.info("%s 1~3위 큰 커버 %d/3", spec.key, ok)
    html = render_html(chart, images)
    png = out / f"{spec.key}_{stamp}.png"
    (out / f"{spec.key}_{stamp}.html").write_text(html, encoding="utf-8")
    render_png(html, png, png_scale(chart))
    title, body = post_title(chart), body_text(chart)
    if b.get("dry_run", True):
        log.info("[dry-run] 업로드 생략: %s -> %s", title, png)
        return None
    return {"key": spec.key, "stamp": stamp, "title": title, "body": body, "png": png}

def head_for(key, cfg):
    name = "genie" if "genie" in key else "bugs" if "bugs" in key else "flo" if "flo" in key else "melon"
    return (cfg.get("dc", {}).get("heads") or {}).get(name)

def publish(job, cfg, uploader, state, out):
    b = cfg["bot"]
    h = head_for(job["key"], cfg)
    if h:
        uploader.headtext = h["text"]
        uploader.head_no = str(h.get("no", ""))
    _g = [float(x) for x in str(b.get("post_gap_seconds", "20-30")).split("-")]
    gap = random.uniform(_g[0], _g[-1])
    with _POST_LOCK:
        wait = _LAST_POST[0] + gap - time.monotonic()
        if _LAST_POST[0] and wait > 0:
            log.info("이전 글과 간격을 두기 위해 %.0f초 대기", wait)
            time.sleep(wait)
        _LAST_POST[0] = time.monotonic()
    uploader.post(job["title"], job["body"], job["png"], out)
    log.info("업로드 완료: %s (%s)", job["title"], f"{datetime.now(KST):%H:%M:%S}")
    state[job["key"]] = job["stamp"]
    for f in sorted(out.glob(f"{job['key']}_2*"))[:-6]:
        f.unlink(missing_ok=True)

def _kst():
    t = datetime.now(KST).replace(tzinfo=None)
    fake = os.environ.get("NOW_OVERRIDE", "").strip()
    if fake and os.environ.get("DRY_RUN", "").lower() != "false":
        hh, mm = fake.split(":")
        t = t.replace(hour=int(hh), minute=int(mm))
    return t

def prewarm(cfg, fetcher, out, deadline):
    b = cfg["bot"]
    for key in b["charts"]:
        left = (deadline - _kst()).total_seconds()
        if left <= 0:
            break
        try:
            spec = resolve(key)
            chart = get_chart(key, _kst(), fetcher, cache_dir=out / "cache")
            gap = float(b.get("image_gap", 1.0))
            budget = int(min(int(b.get("image_budget", 30)), max(0, left / max(gap, 0.5))))
            embed_images(chart, fetcher, out / "cache", budget=budget, gap=gap,
                         size=400 if spec.layout == "table_users" else (None if spec.source == "direct" else 80))
        except Exception:
            log.exception("%s 미리 준비 실패 (정각에 다시 시도)", key)

def wait_until(target):
    while True:
        left = (target - _kst()).total_seconds()
        if left <= 0:
            return
        time.sleep(min(left, 5) if left > 5 else max(left, 0.02))

def precise_wait(cfg, fetcher, out):
    b = cfg["bot"]
    top = (b.get("charts") or [None])[0]
    now = _kst()
    lead = int(b.get("precise_lead_minutes", 12))
    if not top or b.get("force") or now.minute < 60 - lead:
        return
    target = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    log.info("정각(%s)에 맞춰 올리려고 준비합니다", f"{target:%H:%M}")
    prewarm(cfg, fetcher, out, target - timedelta(seconds=45))
    wait_until(target)

    spec = resolve(top)
    if spec.source == "direct":
        from chartbot.direct import PARSERS, urls
        prev = None
        try:
            prev = json.loads((out / "rank" / f"{spec.platform}.json").read_text(encoding="utf-8"))["latest"]["rows"]
        except Exception:
            pass
        sig = lambda rows: [(r["sid"], r["rank"]) for r in rows]
        t0 = time.monotonic()
        while prev and time.monotonic() - t0 < 90:
            try:
                fetcher._last = 0.0
                cur = PARSERS[spec.platform](fetcher.get(urls(spec.platform, _kst())[0]))
            except Exception:
                break
            if sig(cur) != sig(prev):
                break
            log.info("%s 아직 갱신 전 (직전 순위와 동일) — 5초 뒤 다시 확인", top)
            time.sleep(5)
        fetcher._last = 0.0

def collect_direct(cfg, fetcher, out):
    now = datetime.now(KST).replace(tzinfo=None)
    for platform in dict.fromkeys(resolve(k).platform for k in cfg["bot"]["charts"] if resolve(k).source == "direct"):
        try:
            ensure_snapshot(platform, now, fetcher, out / "rank")
        except RateLimited:
            raise
        except Exception:
            log.exception("%s 순위 수집 실패", platform)

def run_cycle(cfg, fetcher, uploader, state, out):
    import queue
    import threading
    b = cfg["bot"]
    limit = int(b.get("max_posts", 1))
    q, stop = queue.Queue(), threading.Event()

    def worker():
        try:
            for n, key in enumerate(b["charts"]):
                if stop.is_set():
                    break
                try:
                    job = prepare(key, cfg, fetcher, out, state)
                    if job:
                        q.put(job)
                except RateLimited as e:
                    log.error("%s — 이번 주기의 나머지 요청을 중단합니다. 이 환경(IP)에서 가이섬 접근이 막혀 있을 수 있습니다.", e)
                    break
                except Exception:
                    log.exception("%s 준비 실패 (다음 차트로 계속)", key)
                if n == 0:
                    try:
                        collect_direct(cfg, fetcher, out)
                    except RateLimited as e:
                        log.error("%s — 이번 주기를 중단합니다.", e)
                        break
        finally:
            q.put(None)

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    posted = 0
    while True:
        job = q.get()
        if job is None:
            break
        if stop.is_set():
            continue
        if posted >= limit:
            log.info("이번 실행의 게시 한도(%d개)에 도달, 나머지는 다음 실행에서 처리", limit)
            stop.set()
            continue
        try:
            publish(job, cfg, uploader, state, out)
            posted += 1
            (out / "state.json").write_text(json.dumps(state), encoding="utf-8")
        except UploadFailed:
            log.exception("%s 업로드 실패 — 이번 주기의 나머지 업로드를 중단합니다.", job["key"])
            stop.set()
        except Exception:
            log.exception("%s 처리 실패 (다음 차트로 계속)", job["key"])
    t.join(timeout=5)
    (out / "state.json").write_text(json.dumps(state), encoding="utf-8")

def own_loop(cfg, uploader, out, state, minutes):
    import queue
    import threading
    from chartbot.own import Own
    b = cfg["bot"]
    own = Own(out, os.environ.get("LIVE_DATA"), os.environ.get("GAISUM_USERS"))
    prio = {"own_melon5": -2, "own_hot5": -1, "own_users": 0, "own_predict": 0, "own_graph": 0, "own_top100": 1, "own_rt": 2, "own_hot100": 3, "own_genie": 6, "own_bugs": 7, "own_flo": 8}
    pq = queue.PriorityQueue()
    stop = threading.Event()
    fails = [0]
    seq = [0]
    lk = threading.Lock()
    failed = set()
    retries = int(b.get("upload_retries", 3))
    gate = threading.Lock()
    last_start = [0.0]
    pace = {"own_genie": 60.0, "own_bugs": 60.0, "own_flo": 60.0}
    pause = [0.0]
    chain = {"own_top100": ("own_graph", 25.0, 60.0), "own_rt": ("own_top100", 25.0, 100.0), "own_hot100": ("own_rt", 25.0, 140.0), "own_bugs": ("own_genie", 60.0, 540.0), "own_flo": ("own_bugs", 60.0, 540.0)}
    started = {}
    deferred = []
    dlk = threading.Lock()

    def reserved(job):
        if job["key"] in ("own_users", "own_hot5", "own_melon5"):
            return False
        n_ = _kst()
        t_ = n_.minute * 60 + n_.second
        if job["key"] in ("own_genie", "own_bugs", "own_flo") and t_ < 250 and job["stamp"][:10] == f"{n_:%Y%m%d%H}":
            return True
        if not 240 <= t_ < 360:
            return False
        st = started.get("own_users")
        return not (st and st[0] == f"{n_:%Y%m%d%H}00")

    def ready(job):
        pk, d, fb = chain[job["key"]]
        hs = datetime.strptime(job["stamp"][:10], "%Y%m%d%H").replace(tzinfo=KST).timestamp()
        if time.time() - hs > fb:
            return True
        st = started.get(pk)
        return bool(st and st[0] == job["stamp"] and time.time() >= st[1] + d)
    gap_lo, gap_hi = (lambda v: (v[0], v[-1]))([float(x) for x in str(b.get("upload_gap", "25-35")).split("-")])

    queued = {}
    gf = out / "dc_gap.json"
    adapt = {"g": gap_lo, "t": 0.0}
    try:
        adapt["g"] = max(gap_lo, float(json.loads(gf.read_text(encoding="utf-8"))["gap"]))
    except Exception:
        pass

    def save_gap():
        try:
            gf.write_text(json.dumps({"gap": adapt["g"]}), encoding="utf-8")
        except Exception:
            pass

    def save():
        with lk:
            (out / "state.json").write_text(json.dumps(dict(state)), encoding="utf-8")

    def worker(up):
        while not stop.is_set():
            with dlk:
                due = [x for x in deferred if x[0] <= time.time()]
                deferred[:] = [x for x in deferred if x[0] > time.time()]
            for x in due:
                pq.put(x[1])
            try:
                item = pq.get(timeout=1)
            except queue.Empty:
                continue
            job = item[2]
            if time.time() < pause[0] or reserved(job) or (job["key"] in chain and not ready(job)):
                with dlk:
                    deferred.append((time.time() + 1, item))
                pq.task_done()
                continue
            ok = False
            for attempt in range(retries + 1):
                try:
                    with gate:
                        k = job["key"]
                        gap = pace[k] if k in pace else random.uniform(adapt["g"], adapt["g"] * 1.15)
                        wait = last_start[0] + gap - time.time()
                        if wait > 0:
                            log.info("%s 업로드 간격 %d초 대기", job["key"], wait)
                            time.sleep(wait)
                        last_start[0] = time.time()
                        started[k] = (job["stamp"], last_start[0])
                    publish(job, cfg, up, state, out)
                    fails[0] = 0
                    ok = True
                    if adapt["g"] > gap_lo and time.time() - adapt["t"] > 3600:
                        adapt["g"] = max(gap_lo, adapt["g"] - 5)
                        adapt["t"] = time.time()
                        save_gap()
                        log.info("업로드 간격을 %d초로 줄입니다", adapt["g"])
                    break
                except SpamBlocked as e:
                    wait_s = 360.0
                    if e.until:
                        y, mo, d, h, mi = e.until
                        wait_s = max(60.0, datetime(y, mo, d, h, mi, tzinfo=KST).timestamp() + 40 - time.time())
                    pause[0] = max(pause[0], time.time() + wait_s)
                    adapt["g"] = min(adapt["g"] + 15, 90.0)
                    adapt["t"] = time.time()
                    save_gap()
                    log.error("업로드 간격을 %d초로 늘립니다", adapt["g"])
                    log.error("%s 디시 도배 차단: %d초 쉬고 다시 올립니다", job["key"], wait_s)
                    with dlk:
                        deferred.append((pause[0], item))
                    ok = None
                    break
                except (HumanCheckRequired, SubmitUncertain) as e:

                    log.exception("%s 업로드 실패, 재시도 안 함: %s", job["key"], type(e).__name__)
                    break
                except Exception:
                    log.exception("%s 업로드 실패 (시도 %d/%d)", job["key"], attempt + 1, retries + 1)
                    if attempt >= retries or stop.is_set():
                        break
                    wait = min(10 * (attempt + 1), 30)
                    log.info("%s %d초 뒤 다시 시도합니다", job["key"], wait)
                    time.sleep(wait)
            if ok is None:
                save()
                pq.task_done()
                continue
            if not ok:
                failed.add((job["key"], job["stamp"]))
                fails[0] += 1
                log.error("%s 최종 업로드 실패 (%d회 연속)", job["key"], fails[0])
                if fails[0] >= 3:
                    log.error("연속 실패로 중단합니다 (제한/차단 가능성)")
                    stop.set()
            save()
            pq.task_done()

    import copy
    n_workers = int(b.get("upload_workers", 2))
    threads = [threading.Thread(target=worker, args=(uploader if i == 0 else copy.copy(uploader),), daemon=True) for i in range(n_workers)]
    end = time.time() + minutes * 60
    e = datetime.fromtimestamp(end, KST)
    if e.minute >= 56 or e.minute < 14:
        cut = (e if e.minute >= 56 else e - timedelta(hours=1)).replace(minute=55, second=30, microsecond=0)
        if cut.timestamp() > time.time() + 1800:
            e, end = cut, cut.timestamp()
    log.info("종료 예정 %s", f"{e:%H:%M:%S}")
    if not os.environ.get("CP_KEY_3"):
        log.warning("CP_KEY_3 Secret이 비어 있어 실시간 차트 요청에 CP_KEY를 대신 씁니다")
    log.info("own 시작: %d분, dry_run=%s, 업로드 스레드 %d", minutes, b.get("dry_run", True), len(threads))
    if not b.get("dry_run", True):
        for t in threads:
            t.start()
    while time.time() < end and not stop.is_set():
        now = _kst()
        for job in own.jobs(now, {**state, **queued}, __import__("os").environ.get("OWN_ONLY", "").split(",") if __import__("os").environ.get("OWN_ONLY") else b.get("own_charts")):
            if b.get("dry_run", True):
                state[job["key"]] = job["stamp"]
                log.info("[dry-run] 업로드 생략: %s -> %s", job["title"], job["png"])
                continue
            queued[job["key"]] = job["stamp"]
            seq[0] += 1
            pq.put((prio.get(job["key"], 9), seq[0], job))
            log.info("업로드 대기열: %s (%s)", job["title"], f"{_kst():%H:%M:%S}")
        save()
        time.sleep(3)
    drain = time.time() + float(b.get("drain_minutes", 7)) * 60
    while not stop.is_set() and (pq.unfinished_tasks or deferred) and time.time() < drain:
        time.sleep(2)
    if pq.unfinished_tasks or deferred:
        log.warning("종료 시점에 못 올린 업로드는 다음 실행이 이어서 올립니다 (%d건 남음)", pq.unfinished_tasks + len(deferred))
    stop.set()
    for t in threads:
        if t.is_alive():
            t.join(timeout=60)
    save()

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["render", "login", "export-session", "once", "loop", "own"])
    ap.add_argument("--minutes", type=int, default=110)
    ap.add_argument("--config", default=str(ROOT / "config.toml"))
    ap.add_argument("--headed", action="store_true", help="업로드 시 브라우저 창을 보이게")
    ap.add_argument("--only", help="render 에서 특정 차트 키만")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if a.cmd == "render":
        a_only = a.only
        out = ROOT / "out"
        out.mkdir(exist_ok=True)
        sf = SampleFetcher(ROOT / "samples")
        for key, spec in SPECS.items():
            if a_only and resolve(a_only).key != key:
                continue

            if spec.source == "direct":
                sample_time = datetime(2026, 11, 5, 14, 35)
                sdir = out / "rank_sample"
                seed_sample_history(spec.platform, sample_time, sf, sdir / "rank")
                c = get_chart(key, sample_time, sf, cache_dir=sdir / "cache")
            else:
                sample_time = datetime.fromisoformat(json.loads((ROOT / "samples" / f"{spec.platform}-{spec.path}.json").read_text(encoding="utf-8"))["time"])
                c = get_chart(key, sample_time, sf, cache_dir=None)
            h = render_html(c)
            (out / f"{key}.html").write_text(h, encoding="utf-8")
            render_png(h, out / f"{key}.png", png_scale(c))
            print("생성:", out / f"{key}.png")
        return

    cfg = load_config(a.config)
    if a.cmd != "own":
        for k in cfg["bot"]["charts"]:
            resolve(k)
    out = Path(cfg["bot"].get("out_dir", ROOT / "out"))
    out.mkdir(parents=True, exist_ok=True)
    uploader = make_uploader(cfg, a.headed)

    if a.cmd == "login":
        uploader.login()
        return
    if a.cmd == "export-session":
        target = ROOT / "dc_session.json"
        uploader.export_session(str(target))
        print(f"저장했습니다: {target}\n이 파일의 내용 전체를 GitHub Secrets 의 DC_STORAGE_STATE 에 넣고, 파일은 지우세요 (비밀번호와 같은 값입니다).")
        return

    b = cfg["bot"]
    import chartbot.render as _render
    _render.RANK_STYLE = os.environ.get("RANK_STYLE") or b.get("rank_style", "classic")
    fetcher = Fetcher(user_agent=b.get("user_agent", "chartbot/1.0"), min_gap=b.get("min_request_gap", 3.0))
    sf = out / "state.json"
    state = json.loads(sf.read_text()) if sf.exists() else {}

    if a.cmd == "own":
        own_loop(cfg, uploader, out, state, a.minutes)
        return

    if a.cmd == "once":
        if b.get("precise_top", False):
            precise_wait(cfg, fetcher, out)
        run_cycle(cfg, fetcher, uploader, state, out)
        return

    interval = int(b.get("interval_minutes", 5)) * 60
    delay = int(b.get("delay_seconds", 20))
    log.info("loop 시작: %d분 주기, 정각+%d초에 실행, dry_run=%s, 차트=%s", interval // 60, delay, b.get("dry_run", True), b["charts"])
    while True:
        now = time.time()
        nxt = (now // interval + 1) * interval + delay
        time.sleep(max(0, nxt - now))
        run_cycle(cfg, fetcher, uploader, state, out)

if __name__ == "__main__":
    main()
