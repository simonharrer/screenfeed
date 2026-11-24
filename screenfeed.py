#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "pyobjc-framework-Cocoa",
#   "pyobjc-framework-Quartz",
#   "pyobjc-framework-AVFoundation",
#   "pillow",
# ]
# ///
"""screenfeed: record screen + voice + mouse cursor, cut it into batches, hand them to Claude.

A floating widget (Rec / Send / Auto) sits on the screen being recorded. Every batch becomes a
folder with timeline.md: what you said (whisper), where the cursor pointed while you said it
(full frame with a red ring + zoomed crop), clicks, and the end state.

Commands:
  screenfeed app              run widget + recorder in the foreground
  screenfeed launch           start the app in the background (no-op if running)
  screenfeed start|stop|toggle  recording on/off (stop = pause; unsent content is kept)
  screenfeed pause|resume     aliases for stop|start
  screenfeed discard          throw away everything recorded but not sent
  screenfeed screens          list displays (* = selected)
  screenfeed screen <n|name>  record that display (switches immediately when recording)
  screenfeed send             cut the current batch and send it
  screenfeed auto <sec|off>   send automatically every <sec> seconds
  screenfeed say <text>       show a message from Claude in the widget
  screenfeed status           JSON status
  screenfeed watch            print one line per ready batch (for Claude's Monitor tool)
  screenfeed quit             stop the app
"""
from __future__ import annotations

import json
import math
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOME = Path(os.environ.get("SCREENFEED_HOME", Path.home() / ".screenfeed"))
PORT = int(os.environ.get("SCREENFEED_PORT", "47811"))
MODEL = os.environ.get(
    "SCREENFEED_WHISPER_MODEL",
    str(Path.home() / ".local/share/whisper-models/ggml-large-v3-turbo.bin"),
)
LANG = os.environ.get("SCREENFEED_LANG", "auto")
AUDIO = os.environ.get("SCREENFEED_AUDIO", "")  # device name, "none", or "" = system default input
FPS = int(os.environ.get("SCREENFEED_FPS", "5"))
KEEP_VIDEO = os.environ.get("SCREENFEED_KEEP_VIDEO") == "1"
MAX_MOMENTS = int(os.environ.get("SCREENFEED_MAX_MOMENTS", "24"))
FFMPEG = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
FFPROBE = shutil.which("ffprobe") or "/opt/homebrew/bin/ffprobe"
WHISPER = shutil.which("whisper-cli") or "/opt/homebrew/bin/whisper-cli"
AUTO_STEPS = [0, 30, 60, 120, 300]
CUT_OVERLAP = 1.2  # seconds the old recorder keeps running after the new one starts (no gap)

READY = HOME / "ready.jsonl"
BATCHES = HOME / "batches"
RAW = HOME / "raw"


def now() -> float:
    return time.time()


def fmt_dur(sec: float) -> str:
    sec = max(0, int(sec))
    return f"{sec // 60}:{sec % 60:02d}"


def emit(event: dict) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    with READY.open("a") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------------------------
# Recording
# --------------------------------------------------------------------------------------------


def default_audio_device() -> str:
    if AUDIO:
        return AUDIO
    try:
        import AVFoundation as AV

        d = AV.AVCaptureDevice.defaultDeviceWithMediaType_(AV.AVMediaTypeAudio)
        return str(d.localizedName()) if d else "none"
    except Exception:
        return "none"


def probe_duration(path: Path) -> float:
    r = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


class Segment:
    """One ffmpeg recording. Wall-clock mapping: actual_start = stop time - recorded duration."""

    def __init__(self, screen: dict, audio: str):
        RAW.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{int(now() * 1000) % 1000:03d}"
        self.path = RAW / f"seg-{stamp}.mkv"
        self.log = RAW / f"seg-{stamp}.log"
        self.screen = screen
        self.audio = audio
        self.proc: subprocess.Popen | None = None
        self.clip_from = now()
        self.clip_to: float | None = None
        self.actual_start: float | None = None
        self.duration = 0.0

    def start(self) -> None:
        cmd = [
            FFMPEG, "-hide_banner", "-loglevel", "warning", "-nostats", "-y",
            "-f", "avfoundation", "-capture_cursor", "1", "-capture_mouse_clicks", "1",
            "-framerate", str(FPS),
            "-i", f"Capture screen {self.screen['index']}:{self.audio}",
            "-vf", "scale='min(3840,iw)':-2",
            "-c:v", "h264_videotoolbox", "-b:v", "5M", "-g", str(FPS * 2),
        ]
        if self.audio != "none":
            cmd += ["-c:a", "aac", "-b:a", "96k", "-ac", "1"]
        cmd.append(str(self.path))
        self.clip_from = now()
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=self.log.open("w"),
        )

    def stop(self) -> float:
        cut = now()
        self.clip_to = cut
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.stdin.write(b"q")
                self.proc.stdin.flush()
            except Exception:
                pass
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                self.proc.wait(timeout=5)
        self.duration = probe_duration(self.path)
        self.actual_start = cut - self.duration
        return cut

    def log_tail(self) -> str:
        try:
            return " | ".join(self.log.read_text(errors="replace").strip().splitlines()[-3:])
        except Exception:
            return ""

    def discard(self) -> None:
        for p in (self.path, self.log):
            p.unlink(missing_ok=True)


class Controller:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.recording = False
        self.seg: Segment | None = None
        self.pending: list[Segment] = []
        self.rec_since: float | None = None  # start of content not yet sent
        self.auto = 0
        self.next_auto: float | None = None
        self.sending = False
        self.samples: deque = deque()  # (t, x, y, buttons, in_widget)
        self.apps: deque = deque()  # (t, frontmost app name)
        self.screen: dict | None = None  # the selected screen (recorded on next start / now)
        self.screens: list[dict] = []  # all connected screens, refreshed by the UI
        self.flash_until = 0.0  # show "this screen will be recorded" preview until then
        self.widget_frame = (0, 0, 0, 0)
        self.status = "Ready"
        self.claude_msg = ""
        self.last_batch: dict | None = None
        self.history: deque = deque(maxlen=20)  # newest first: sent batches and their fate
        self.busy = 0
        self.batch_no = self._next_batch_no()
        self.jobs: queue.Queue = queue.Queue()
        threading.Thread(target=self._worker, daemon=True).start()

    def _next_batch_no(self) -> int:
        BATCHES.mkdir(parents=True, exist_ok=True)
        nums = [int(m.group(1)) for p in BATCHES.iterdir() if (m := re.match(r"(\d+)-", p.name))]
        return max(nums, default=0) + 1

    # -- recording control ---------------------------------------------------------------

    def start(self) -> dict:
        with self.lock:
            if self.recording:
                return self.state()
            if not self.screen:
                return {"ok": False, "error": "screen unknown (UI not ready)"}
            seg = Segment(self.screen, default_audio_device())
            seg.start()
            self.seg = seg
            self.recording = True
            if self.rec_since is None:
                self.rec_since = seg.clip_from
            if self.auto:
                self.next_auto = now() + self.auto
            self.status = f"Recording {self.screen['label']} · mic: {seg.audio}"
        return self.state()

    def stop(self) -> dict:
        while self.sending:
            time.sleep(0.05)
        with self.lock:
            if not self.recording:
                return self.state()
            seg, self.seg, self.recording, self.next_auto = self.seg, None, False, None
        seg.stop()
        with self.lock:
            self.pending.append(seg)
            self.status = "Paused"
        return self.state()

    def toggle(self) -> dict:
        return self.stop() if self.recording else self.start()

    def unsent_seconds(self) -> float:
        """Recorded (not wall-clock) seconds not yet sent; does not grow while paused."""
        t = sum((s.clip_to or now()) - s.clip_from for s in self.pending)
        if self.recording and self.seg:
            t += max(0.0, now() - self.seg.clip_from)
        return t

    def has_unsent(self) -> bool:
        return bool(self.recording or self.pending)

    def discard(self) -> dict:
        """Throw away everything not yet sent; keeps recording if it was on."""
        while self.sending:
            time.sleep(0.05)
        with self.lock:
            old, pend, self.pending = None, self.pending, []
            if self.recording and self.seg:
                old = self.seg
                self.seg = Segment(self.screen or old.screen, default_audio_device())
                self.seg.start()
            self.rec_since = self.seg.clip_from if (self.recording and self.seg) else None
            self.samples.clear()
            if self.auto and self.recording:
                self.next_auto = now() + self.auto
            self.status = "Unsent content discarded"
        if old:
            old.stop()
            old.discard()
        for sg in pend:
            sg.discard()
        return self.state()

    def update_screens(self, screens: list[dict]) -> None:
        """Called by the UI with the current display list; keeps the selection valid."""
        with self.lock:
            self.screens = screens
            cur = self.screen and next((s for s in screens if s["id"] == self.screen["id"]), None)
            if cur:
                self.screen = cur
            elif screens:
                self.screen = screens[0]

    def select_screen(self, which: str | int) -> dict:
        """Select by list position (1-based), display id, or (part of) the name."""
        with self.lock:
            scr = None
            w = str(which).strip()
            if w.isdigit() and 1 <= int(w) <= len(self.screens):
                scr = self.screens[int(w) - 1]
            if not scr:
                scr = next((s for s in self.screens if str(s["id"]) == w), None)
            if not scr:
                scr = next((s for s in self.screens if w.lower() in s["label"].lower()), None)
            if not scr:
                return {"ok": False, "error": f"no screen matching {which!r}", "screens": self.screens}
            self.screen = scr
            self.flash_until = now() + 2.0
            try:
                (HOME / "state.json").write_text(json.dumps({"screen_id": scr["id"]}))
            except OSError:
                pass
            switch = self.recording and self.seg and self.seg.screen["id"] != scr["id"]
        if switch:
            self._rotate()
        with self.lock:
            self.status = (f"Recording {scr['label']}" if self.recording
                           else f"Will record {scr['label']}")
        return self.state()

    def _rotate(self) -> None:
        """Move the running recording to the selected screen; what was recorded stays unsent."""
        while self.sending:
            time.sleep(0.05)
        with self.lock:
            if not (self.recording and self.seg):
                return
            self.sending = True
            old = self.seg
            new = Segment(self.screen, default_audio_device())
            new.start()
            self.seg = new
        try:
            time.sleep(CUT_OVERLAP)
            new.clip_from = old.stop()
            with self.lock:
                self.pending.append(old)
        finally:
            self.sending = False

    def set_auto(self, seconds: int) -> dict:
        with self.lock:
            self.auto = max(0, int(seconds))
            self.next_auto = now() + self.auto if (self.auto and self.recording) else None
        return self.state()

    def cycle_auto(self) -> None:
        i = AUTO_STEPS.index(self.auto) if self.auto in AUTO_STEPS else 0
        self.set_auto(AUTO_STEPS[(i + 1) % len(AUTO_STEPS)])

    def send(self, manual: bool = True) -> dict:
        with self.lock:
            if self.sending:
                return {"ok": False, "error": "already sending"}
            if not self.recording and not self.pending:
                self.status = "Nothing to send"
                return {"ok": False, "error": "nothing recorded"}
            self.sending = True
            old = new = None
            if self.recording:
                old = self.seg
                new = Segment(self.screen or old.screen, default_audio_device())
                new.start()
                self.seg = new
            self.status = "Cutting batch…"
        try:
            cut = None
            if old:
                time.sleep(CUT_OVERLAP)  # new recorder is warming up; old one still covers
                cut = old.stop()
                new.clip_from = cut
            with self.lock:
                segs = sorted(self.pending + ([old] if old else []), key=lambda s: s.clip_from)
                self.pending = []
                t0 = self.rec_since or segs[0].clip_from
                t1 = cut or segs[-1].clip_to
                self.rec_since = cut if self.recording else None
                samples = [s for s in self.samples if t0 - 1 <= s[0] <= t1 + 1]
                apps = list(self.apps)
                keep_from = (cut or t1) - 2
                while self.samples and self.samples[0][0] < keep_from:
                    self.samples.popleft()
                no = self.batch_no
                self.batch_no += 1
                entry = {"batch": no, "sent_at": now(), "auto": not manual, "state": "processing",
                         "seconds": round(sum((g.clip_to or now()) - g.clip_from for g in segs), 1)}
                self.history.appendleft(entry)
                if self.auto and self.recording:
                    self.next_auto = now() + self.auto
                self.status = f"Batch {no}: processing…"
            self.jobs.put(dict(no=no, segs=segs, t0=t0, t1=t1, samples=samples, apps=apps,
                               manual=manual, widget=self.widget_frame, entry=entry))
            return {"ok": True, "batch": no}
        finally:
            self.sending = False

    def recorder_died(self) -> None:
        with self.lock:
            seg = self.seg
            if not (self.recording and seg and seg.proc and seg.proc.poll() is not None):
                return
            if self.sending:
                return
            self.recording, self.seg, self.next_auto = False, None, None
            msg = seg.log_tail() or f"ffmpeg exited with {seg.proc.returncode}"
            self.status = f"Recorder failed: {msg[:120]}"
        emit({"error": f"recorder stopped: {msg}", "t": now()})
        seg.discard()

    # -- sampling (called from the UI thread at ~30 Hz) ------------------------------------

    def sample(self, t, x, y, buttons, in_widget, app) -> None:
        if not (self.recording or self.sending):
            return
        self.samples.append((t, x, y, buttons, in_widget))
        if not self.apps or self.apps[-1][1] != app:
            self.apps.append((t, app))
            while len(self.apps) > 500:
                self.apps.popleft()

    def tick(self) -> None:
        self.recorder_died()
        if self.auto and self.recording and self.next_auto and now() >= self.next_auto and not self.sending:
            self.next_auto = now() + self.auto
            threading.Thread(target=self.send, kwargs={"manual": False}, daemon=True).start()

    # -- processing -----------------------------------------------------------------------

    def _worker(self) -> None:
        while True:
            job = self.jobs.get()
            self.busy += 1
            try:
                result = process_batch(job)
                with self.lock:
                    self.last_batch = result
                    if result.get("skipped"):
                        job["entry"]["state"] = "skipped (nothing said)"
                        self.status = f"Batch {job['no']}: nothing said, skipped"
                    else:
                        job["entry"]["state"] = f"→ Claude · {result['moments']} moments"
                        self.status = f"Batch {job['no']} → Claude ({result['moments']} moments)"
            except Exception as e:  # noqa: BLE001
                emit({"batch": job["no"], "error": f"{type(e).__name__}: {e}", "t": now()})
                job["entry"]["state"] = "failed"
                self.status = f"Batch {job['no']} failed: {e}"[:140]
            finally:
                if not KEEP_VIDEO:
                    for s in job["segs"]:
                        s.discard()
                self.busy -= 1

    def state(self) -> dict:
        t = now()
        return {
            "ok": True,
            "recording": self.recording,
            "paused_with_unsent": bool(not self.recording and self.pending),
            "unsent_seconds": round(self.unsent_seconds(), 1),
            "auto_seconds": self.auto,
            "next_auto_in": round(self.next_auto - t, 1) if self.next_auto else None,
            "sending": self.sending,
            "processing": self.busy + self.jobs.qsize(),
            "next_batch": self.batch_no,
            "screen": self.screen and self.screen["label"],
            "recording_screen": self.seg.screen["label"] if (self.recording and self.seg) else None,
            "screens": [f"{i}: {s['label']}" for i, s in enumerate(self.screens, 1)],
            "status": self.status,
            "claude_msg": self.claude_msg,
            "last_batch": self.last_batch,
            "history": [dict(h, sent_at=time.strftime("%H:%M:%S", time.localtime(h["sent_at"])))
                        for h in self.history],
        }

    def shutdown(self) -> None:
        with self.lock:
            seg, self.seg, self.recording = self.seg, None, False
            pend, self.pending = self.pending, []
        if seg:
            seg.stop()
            seg.discard()
        for s in pend:
            s.discard()


# --------------------------------------------------------------------------------------------
# Batch processing: transcript + pointing moments + annotated frames + timeline.md
# --------------------------------------------------------------------------------------------

HALLUCINATIONS = {
    "thank you", "thanks for watching", "thank you for watching", "you", "vielen dank",
    "bye", "tschüss", "danke fürs zuschauen", "bis zum nächsten mal",
}


def is_junk(text: str) -> bool:
    t = text.strip()
    if not t:
        return True
    if re.fullmatch(r"[\[\(\*♪].*[\]\)\*♪]", t):
        return True
    low = re.sub(r"[^\w\s]", "", t.lower()).strip()
    if low in HALLUCINATIONS:
        return True
    return any(k in low for k in ("untertitel", "amaraorg", "zdf", "subtitles by"))


def max_volume(wav: Path) -> float:
    r = subprocess.run([FFMPEG, "-hide_banner", "-nostats", "-i", str(wav), "-af", "volumedetect",
                        "-f", "null", "-"], capture_output=True, text=True)
    m = re.search(r"max_volume:\s*(-?[\d.]+) dB", r.stderr)
    return float(m.group(1)) if m else -91.0


def transcribe(seg: Segment, a: float, b: float, workdir: Path, i: int) -> tuple[list, str | None]:
    if seg.audio == "none":
        return [], None
    wav = workdir / f"audio-{i}.wav"
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{a:.3f}",
                    "-t", f"{b - a:.3f}", "-i", str(seg.path), "-vn", "-ar", "16000", "-ac", "1",
                    str(wav)], check=True)
    if max_volume(wav) < -42:
        return [], None
    out = workdir / f"speech-{i}"
    subprocess.run([WHISPER, "-m", MODEL, "-l", LANG, "-oj", "-of", str(out), "-np", "-ml", "90",
                    "-sow", "-sns", "-f", str(wav)], capture_output=True, check=True, timeout=900)
    data = json.loads(out.with_suffix(".json").read_text(errors="replace"))
    base = seg.actual_start + a
    res = []
    for s in data.get("transcription", []):
        text = s.get("text", "").strip()
        if is_junk(text):
            continue
        res.append((base + s["offsets"]["from"] / 1000, base + s["offsets"]["to"] / 1000, text))
    return res, data.get("result", {}).get("language")


def dwell_points(win: list) -> list:
    """Longest periods where the cursor rested (that is where you were pointing)."""
    runs, i = [], 0
    while i < len(win):
        j = i
        while j + 1 < len(win) and math.dist(win[j + 1][1:3], win[i][1:3]) < 8:
            j += 1
        runs.append((win[j][0] - win[i][0], win[i], win[j]))
        i = j + 1
    runs.sort(key=lambda r: -r[0])
    chosen = []
    for dur, a, b in runs:
        if dur < 0.3:
            break
        if all(math.dist(a[1:3], c[1:3]) > 120 for _, c, _ in chosen):
            chosen.append((dur, a, b))
        if len(chosen) == 2:
            break
    return [((a[0] + b[0]) / 2, a[1], a[2], dur) for dur, a, b in sorted(chosen, key=lambda r: r[1][0])]


def cursor_at(samples: list, t: float):
    best = None
    for s in samples:
        if s[0] <= t:
            best = s
        else:
            break
    return best or (samples[0] if samples else None)


def app_at(apps: list, t: float) -> str:
    name = ""
    for at, a in apps:
        if at <= t:
            name = a
    return name or (apps[0][1] if apps else "")


def seg_for(segs: list, t: float) -> Segment:
    for s in segs:
        if s.clip_from - 0.05 <= t <= (s.clip_to or 0) + 0.05:
            return s
    return min(segs, key=lambda s: min(abs(t - s.clip_from), abs(t - (s.clip_to or s.clip_from))))


def render_frame(seg: Segment, t: float, x: float, y: float, base: Path) -> dict:
    from PIL import Image, ImageDraw

    off = min(max(t - seg.actual_start, 0), max(seg.duration - 0.15, 0))
    png = base.with_suffix(".png")
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{off:.3f}",
                    "-i", str(seg.path), "-frames:v", "1", str(png)], check=True)
    img = Image.open(png).convert("RGB")
    png.unlink(missing_ok=True)
    W, H = img.size
    fx, fy, fw, fh = seg.screen["frame"]
    lx, ly = x - fx, (fy + fh) - y  # Cocoa (bottom-left origin) -> screen top-left points
    on_screen = 0 <= lx <= fw and 0 <= ly <= fh
    out = {"pt": (round(lx), round(ly)), "on_screen": on_screen}
    if on_screen:
        px, py = lx * W / fw, ly * H / fh
        d = ImageDraw.Draw(img)
        r, w = W / 70, max(3, int(W / 640))
        d.ellipse([px - r - w, py - r - w, px + r + w, py + r + w], outline=(255, 255, 255), width=w)
        d.ellipse([px - r, py - r, px + r, py + r], outline=(255, 30, 30), width=w)
        cw, ch = int(W * 0.32), int(H * 0.32)
        left = int(min(max(px - cw / 2, 0), W - cw))
        top = int(min(max(py - ch / 2, 0), H - ch))
        zoom = base.parent / (base.name + "_zoom.jpg")
        img.crop((left, top, left + cw, top + ch)).save(zoom, quality=85)
        out["zoom"] = str(zoom)
    full = base.parent / (base.name + "_full.jpg")
    img.resize((1600, int(1600 * H / W))).save(full, quality=80)
    out["full"] = str(full)
    return out


def process_batch(job: dict) -> dict:
    no, segs, t0, t1 = job["no"], job["segs"], job["t0"], job["t1"]
    samples, apps = job["samples"], job["apps"]
    d = BATCHES / f"{no:04d}-{time.strftime('%Y%m%d-%H%M%S', time.localtime(t0))}"
    frames = d / "frames"
    frames.mkdir(parents=True, exist_ok=True)
    work = d / "work"
    work.mkdir(exist_ok=True)

    # 1. transcript (wall-clock timestamps)
    speech, lang = [], None
    for i, s in enumerate(segs):
        if s.duration < 0.3 or s.actual_start is None:
            continue
        a = max(0.0, s.clip_from - s.actual_start)
        b = min(s.duration, (s.clip_to or t1) - s.actual_start)
        if b - a < 0.3:
            continue
        res, l = transcribe(s, a, b, work, i)
        speech += res
        lang = lang or l
    speech.sort()

    if not speech and not job["manual"]:
        shutil.rmtree(d, ignore_errors=True)
        emit({"batch": no, "skipped": "no speech", "t": now()})
        return {"batch": no, "skipped": True, "moments": 0}

    # 2. pointing moments
    def in_range(s):
        return t0 <= s[0] <= t1

    clicks = [s for i, s in enumerate(samples)
              if s[3] and (i == 0 or not samples[i - 1][3]) and not s[4] and in_range(s)]
    moments = []  # dict(t, x, y, kind, speech, dwell)
    used_clicks = set()
    for si, (ts, te, _) in enumerate(speech):
        cs = [c for c in clicks if ts - 0.5 <= c[0] <= te + 0.5]
        if cs:
            for c in cs[:2]:
                used_clicks.add(c[0])
                moments.append(dict(t=c[0], x=c[1], y=c[2], kind="click", speech=si, dwell=0))
            continue
        win = [s for s in samples if ts <= s[0] <= te and not s[4]]
        dw = dwell_points(win)
        if not dw:
            c = cursor_at(samples, (ts + te) / 2)
            if c:
                dw = [((ts + te) / 2, c[1], c[2], 0)]
        for t, x, y, dur in dw:
            moments.append(dict(t=t, x=x, y=y, kind="point", speech=si, dwell=dur))
    for c in clicks:
        if c[0] not in used_clicks:
            moments.append(dict(t=c[0], x=c[1], y=c[2], kind="click", speech=None, dwell=0))
    end = cursor_at(samples, t1 - CUT_OVERLAP - 0.2)
    end_t = max(t0, t1 - CUT_OVERLAP - 0.2)

    spoken = [m for m in moments if m["speech"] is not None]
    other = sorted([m for m in moments if m["speech"] is None], key=lambda m: m["t"])
    if len(spoken) > MAX_MOMENTS:
        step = len(spoken) / MAX_MOMENTS
        spoken = [spoken[int(i * step)] for i in range(MAX_MOMENTS)]
    moments = sorted(spoken + other[: max(0, MAX_MOMENTS - len(spoken))], key=lambda m: m["t"])
    if end:
        moments.append(dict(t=end_t, x=end[1], y=end[2], kind="end", speech=None, dwell=0))

    # 3. frames (reuse a frame when two moments are basically the same picture)
    rendered: list[dict] = []
    for k, m in enumerate(moments, 1):
        twin = next((r for r in rendered if abs(r["t"] - m["t"]) < 0.8
                     and math.dist((r["x"], r["y"]), (m["x"], m["y"])) < 30), None)
        if twin and m["kind"] != "end":
            m["img"] = twin["img"]
        else:
            m["img"] = render_frame(seg_for(segs, m["t"]), m["t"], m["x"], m["y"], frames / f"m{k:02d}")
        m["app"] = app_at(apps, m["t"])
        rendered.append(m)

    # 4. timeline.md
    rec_s = sum((g.clip_to or t1) - g.clip_from for g in segs)
    rel = lambda t: fmt_dur(t - t0)  # noqa: E731

    def img_lines(m):
        im = m["img"]
        where = (f"cursor at ({im['pt'][0]}, {im['pt'][1]}) pt" if im["on_screen"]
                 else "cursor on another screen")
        lines = [f"  - {m['kind']} @ {rel(m['t'])} · {where}"
                 + (f" · rested {m['dwell']:.1f}s" if m["dwell"] else "")
                 + (f" · app: {m['app']}" if m["app"] else "")]
        if im.get("zoom"):
            lines.append(f"    zoom: {im['zoom']}")
        lines.append(f"    full: {im['full']}")
        return lines

    out = [
        f"# Screenfeed batch {no}",
        "",
        f"- Recorded {time.strftime('%H:%M:%S', time.localtime(t0))} to "
        f"{time.strftime('%H:%M:%S', time.localtime(t1))} ({int(rec_s)} s recorded"
        + (", paused in between; timestamps are wall-clock since start)" if t1 - t0 > rec_s + 3 else ")"),
        f"- Screen: {', '.join(dict.fromkeys(s.screen['label'] for s in segs))}, "
        f"spoken language: {lang or 'n/a'}",
        "- Images: `full` = whole screen, `zoom` = crop around the cursor. The red ring marks the "
        "mouse cursor at the moment the sentence was spoken (where the user was pointing). "
        "Coordinates are points from the top-left of the screen. A red frame or REC badge at the "
        "screen edges, if visible, is the recording indicator; ignore it.",
        "",
        "## Timeline",
        "",
    ]
    by_speech: dict[int, list] = {}
    for m in rendered:
        if m["speech"] is not None:
            by_speech.setdefault(m["speech"], []).append(m)
    entries = [(ts, "say", si) for si, (ts, _, _) in enumerate(speech)]
    entries += [(m["t"], "other", m) for m in rendered if m["speech"] is None]
    for t, kind, ref in sorted(entries, key=lambda e: e[0]):
        if kind == "say":
            out.append(f"- [{rel(t)}] SAID: \"{speech[ref][2]}\"")
            for m in by_speech.get(ref, []):
                out += img_lines(m)
        else:
            label = "END STATE (what the screen looks like now)" if ref["kind"] == "end" else "CLICK"
            out.append(f"- [{rel(t)}] {label}")
            out += img_lines(ref)
    if not speech:
        out += ["", "_No speech in this batch (sent manually)._"]
    out += ["", "## Full transcript", "", " ".join(s[2] for s in speech) or "_(none)_", ""]
    timeline = d / "timeline.md"
    timeline.write_text("\n".join(out))
    shutil.rmtree(work, ignore_errors=True)

    preview = " ".join(s[2] for s in speech)
    event = {"batch": no, "dir": str(d), "timeline": str(timeline), "seconds": int(rec_s),
             "moments": len(rendered), "speech": preview[:200], "t": now()}
    emit(event)
    return event


# --------------------------------------------------------------------------------------------
# Widget (AppKit) + local HTTP control API
# --------------------------------------------------------------------------------------------


def run_app() -> None:
    import AppKit as K
    import Quartz
    from PyObjCTools import AppHelper

    HOME.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(RAW, ignore_errors=True)
    ctl = Controller()

    def screen_info(s) -> dict:
        did = int(s.deviceDescription()["NSScreenNumber"])
        _, ids, n = Quartz.CGGetActiveDisplayList(16, None, None)
        ids = list(ids[:n])
        f = s.frame()
        name = str(s.localizedName())
        return {"id": did, "index": ids.index(did) if did in ids else 0,
                "frame": (f.origin.x, f.origin.y, f.size.width, f.size.height),
                "name": name, "label": f"{name} ({int(f.size.width)}×{int(f.size.height)})"}

    def all_screens() -> list[dict]:
        infos = [screen_info(s) for s in K.NSScreen.screens()]
        for i in infos:  # identical monitors: make labels unique
            same = [j for j in infos if j["label"] == i["label"]]
            if len(same) > 1:
                i["label"] += f" #{same.index(i) + 1}"
        return infos

    # -- recording indicator: red pulsing frame + REC badge on the recorded display, -------
    # -- "Not recorded" badge on every other display. Click-through, kept out of the capture.
    overlays: dict = {"key": None, "wins": []}

    def make_overlay(s, mode: str):
        """mode: "rec" (being recorded), "preview" (selected, not yet recording), "off"."""
        recorded = mode in ("rec", "preview")
        f, vf = s.frame(), s.visibleFrame()
        w = K.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            f, K.NSWindowStyleMaskBorderless, K.NSBackingStoreBuffered, False)
        w.setFrame_display_(f, False)
        w.setReleasedWhenClosed_(False)
        w.setOpaque_(False)
        w.setBackgroundColor_(K.NSColor.clearColor())
        w.setHasShadow_(False)
        w.setIgnoresMouseEvents_(True)
        w.setLevel_(K.NSStatusWindowLevel)
        w.setSharingType_(K.NSWindowSharingNone)
        w.setCollectionBehavior_(K.NSWindowCollectionBehaviorCanJoinAllSpaces
                                 | K.NSWindowCollectionBehaviorStationary
                                 | K.NSWindowCollectionBehaviorFullScreenAuxiliary
                                 | K.NSWindowCollectionBehaviorIgnoresCycle)
        fw, fh = f.size.width, f.size.height
        root = K.NSView.alloc().initWithFrame_(K.NSMakeRect(0, 0, fw, fh))
        red = K.NSColor.colorWithSRGBRed_green_blue_alpha_(1.0, 0.13, 0.13, 1.0)
        if recorded:
            border = K.NSView.alloc().initWithFrame_(K.NSMakeRect(0, 0, fw, fh))
            border.setWantsLayer_(True)
            layer = border.layer()
            layer.setBorderWidth_(10)
            layer.setBorderColor_(red.CGColor())
            pulse = Quartz.CABasicAnimation.animationWithKeyPath_("opacity")
            pulse.setFromValue_(1.0)
            pulse.setToValue_(0.35)
            pulse.setDuration_(0.8)
            pulse.setAutoreverses_(True)
            pulse.setRepeatCount_(1e9)
            if mode == "rec":
                layer.addAnimation_forKey_(pulse, "pulse")
                text, bg, size = ("●  REC    This screen is being recorded  ·  screen + microphone + cursor",
                                  red, 15)
            else:
                text, bg, size = ("This screen will be recorded",
                                  K.NSColor.colorWithSRGBRed_green_blue_alpha_(0.55, 0.05, 0.05, 0.95), 15)
            root.addSubview_(border)
        else:
            text, bg, size = "Not recorded", K.NSColor.colorWithWhite_alpha_(0.25, 0.85), 12
        lbl = K.NSTextField.labelWithString_(text)
        lbl.setFont_(K.NSFont.boldSystemFontOfSize_(size))
        lbl.setTextColor_(K.NSColor.whiteColor())
        lbl.sizeToFit()
        lw, lh = lbl.frame().size.width, lbl.frame().size.height
        cw, ch = lw + 32, lh + 14
        top = vf.origin.y + vf.size.height - f.origin.y  # below the menu bar, window coordinates
        badge = K.NSView.alloc().initWithFrame_(K.NSMakeRect((fw - cw) / 2, top - ch - 14, cw, ch))
        badge.setWantsLayer_(True)
        badge.layer().setBackgroundColor_(bg.CGColor())
        badge.layer().setCornerRadius_(ch / 2)
        lbl.setFrameOrigin_(K.NSMakePoint(16, (ch - lh) / 2))
        badge.addSubview_(lbl)
        root.addSubview_(badge)
        w.setContentView_(root)
        w.orderFrontRegardless()
        return w

    def sync_overlays(widget_panel):
        if ctl.recording and ctl.seg:
            target, mode = ctl.seg.screen, "rec"
        elif ctl.screen and now() < ctl.flash_until:
            target, mode = ctl.screen, "preview"
        else:
            target, mode = None, None
        screens = list(K.NSScreen.screens())

        def fr(s):
            f = s.frame()
            return (f.origin.x, f.origin.y, f.size.width, f.size.height)

        key = (tuple(fr(s) for s in screens), tuple(target["frame"]), mode) if target else None
        if key == overlays["key"]:
            return
        for w in overlays["wins"]:
            w.orderOut_(None)
        overlays["wins"], overlays["key"] = [], key
        if key:
            overlays["wins"] = [make_overlay(s, mode if fr(s) == tuple(target["frame"]) else "off")
                                for s in screens]
            widget_panel.orderFrontRegardless()

    class ScreenfeedPanel(K.NSPanel):
        def canBecomeKeyWindow(self):
            return True

    class ScreenfeedDragView(K.NSVisualEffectView):
        def mouseDown_(self, ev):
            self.window().performWindowDragWithEvent_(ev)

    class ScreenfeedButton(K.NSButton):
        def acceptsFirstMouse_(self, ev):
            return True

    class ScreenfeedUI(K.NSObject):
        def build(self):
            W, H = 392, 214
            vf = K.NSScreen.mainScreen().visibleFrame()
            rect = K.NSMakeRect(vf.origin.x + vf.size.width - W - 24, vf.origin.y + 24, W, H)
            p = ScreenfeedPanel.alloc().initWithContentRect_styleMask_backing_defer_(
                rect, K.NSWindowStyleMaskBorderless | K.NSWindowStyleMaskNonactivatingPanel,
                K.NSBackingStoreBuffered, False)
            p.setLevel_(K.NSStatusWindowLevel)
            p.setCollectionBehavior_(K.NSWindowCollectionBehaviorCanJoinAllSpaces
                                     | K.NSWindowCollectionBehaviorStationary
                                     | K.NSWindowCollectionBehaviorFullScreenAuxiliary)
            p.setHidesOnDeactivate_(False)
            p.setBecomesKeyOnlyIfNeeded_(True)
            p.setOpaque_(False)
            p.setBackgroundColor_(K.NSColor.clearColor())
            p.setHasShadow_(True)
            p.setAppearance_(K.NSAppearance.appearanceNamed_(K.NSAppearanceNameDarkAqua))
            v = ScreenfeedDragView.alloc().initWithFrame_(K.NSMakeRect(0, 0, W, H))
            v.setMaterial_(K.NSVisualEffectMaterialHUDWindow)
            v.setBlendingMode_(K.NSVisualEffectBlendingModeBehindWindow)
            v.setState_(K.NSVisualEffectStateActive)
            v.setWantsLayer_(True)
            v.layer().setCornerRadius_(12)
            v.layer().setMasksToBounds_(True)
            p.setContentView_(v)

            def button(x, w, title, action):
                b = ScreenfeedButton.alloc().initWithFrame_(K.NSMakeRect(x, H - 34, w, 28))
                b.setBezelStyle_(K.NSBezelStyleRounded)
                b.setTitle_(title)
                b.setTarget_(self)
                b.setAction_(action)
                v.addSubview_(b)
                return b

            def label(x, y, w, size, color, mono=False, bold=False, h=16):
                t = K.NSTextField.labelWithString_("")
                t.setFrame_(K.NSMakeRect(x, y, w, h))
                weight = 0.4 if bold else 0
                t.setFont_(K.NSFont.monospacedDigitSystemFontOfSize_weight_(size, weight) if mono
                           else K.NSFont.systemFontOfSize_weight_(size, weight))
                t.setTextColor_(color)
                t.setLineBreakMode_(K.NSLineBreakByTruncatingTail)
                v.addSubview_(t)
                return t

            self.rec = button(8, 92, "● Rec", "onRec:")
            self.send = button(102, 80, "Send ⏎", "onSend:")
            self.drop = button(184, 36, "✕", "onDiscard:")
            self.drop.setToolTip_("Discard everything not sent yet")
            self.auto = button(222, 162, "Auto: off", "onAuto:")
            # what is recorded but not sent yet (recorded time only; frozen while paused)
            self.unsent = label(12, 156, W - 24, 13, K.NSColor.whiteColor(), mono=True, bold=True, h=19)
            rl = label(12, 130, 60, 12, K.NSColor.secondaryLabelColor())
            rl.setStringValue_("Screen:")
            self.pick = K.NSPopUpButton.alloc().initWithFrame_pullsDown_(
                K.NSMakeRect(64, 124, W - 72, 26), False)
            self.pick.setTarget_(self)
            self.pick.setAction_("onScreen:")
            self.pick.setToolTip_("Which display is recorded (picking one highlights it)")
            v.addSubview_(self.pick)
            self.pick_labels = []
            self.stat = label(12, 104, W - 24, 11, K.NSColor.secondaryLabelColor())
            self.msg = label(12, 88, W - 24, 11, K.NSColor.systemTealColor())
            hh = label(12, 68, W - 24, 10, K.NSColor.tertiaryLabelColor(), bold=True, h=14)
            hh.setStringValue_("SENT")
            self.hist = [label(12, 54 - 14 * i, W - 24, 10.5, K.NSColor.secondaryLabelColor(),
                               mono=True, h=14) for i in range(4)]
            self.panel = p
            self.view = v
            self.n = 0
            p.orderFrontRegardless()
            return self

        def onRec_(self, sender):
            threading.Thread(target=ctl.toggle, daemon=True).start()

        def onSend_(self, sender):
            threading.Thread(target=ctl.send, kwargs={"manual": True}, daemon=True).start()

        def onAuto_(self, sender):
            ctl.cycle_auto()

        def onDiscard_(self, sender):
            threading.Thread(target=ctl.discard, daemon=True).start()

        def onScreen_(self, sender):
            pos = int(sender.indexOfSelectedItem()) + 1
            threading.Thread(target=ctl.select_screen, args=(pos,), daemon=True).start()

        def syncPicker(self):
            labels = [s["label"] for s in ctl.screens]
            if labels != self.pick_labels:
                self.pick.removeAllItems()
                self.pick.addItemsWithTitles_(labels)
                self.pick_labels = labels
            if ctl.screen and ctl.screen["label"] in labels:
                i = labels.index(ctl.screen["label"])
                if self.pick.indexOfSelectedItem() != i:
                    self.pick.selectItemAtIndex_(i)

        def tick_(self, timer):
            t = now()
            f = self.panel.frame()
            ctl.widget_frame = (f.origin.x, f.origin.y, f.size.width, f.size.height)
            if self.n % 30 == 0:
                ctl.update_screens(all_screens())
            loc = K.NSEvent.mouseLocation()
            inw = K.NSPointInRect(loc, f)
            app = K.NSWorkspace.sharedWorkspace().frontmostApplication()
            ctl.sample(t, loc.x, loc.y, int(K.NSEvent.pressedMouseButtons()), bool(inw),
                       str(app.localizedName()) if app else "")
            ctl.tick()
            self.n += 1
            if self.n % 6:
                return
            rec = ctl.recording
            sync_overlays(self.panel)
            self.syncPicker()
            self.view.layer().setBorderWidth_(3 if rec else 0)
            self.view.layer().setBorderColor_(K.NSColor.systemRedColor().CGColor())
            paused = not rec and bool(ctl.pending)
            self.rec.setTitle_("⏸ Pause" if rec else ("● Resume" if paused else "● Rec"))
            self.rec.setBezelColor_(K.NSColor.systemRedColor() if rec else None)
            u = fmt_dur(ctl.unsent_seconds())
            if rec:
                self.unsent.setStringValue_(f"● REC   {u} unsent")
                self.unsent.setTextColor_(K.NSColor.systemRedColor())
            elif paused:
                self.unsent.setStringValue_(f"⏸ PAUSED   {u} unsent · Send, Resume or ✕")
                self.unsent.setTextColor_(K.NSColor.systemOrangeColor())
            else:
                self.unsent.setStringValue_("Nothing unsent")
                self.unsent.setTextColor_(K.NSColor.tertiaryLabelColor())
            self.send.setTitle_("Sending…" if ctl.sending else "Send ⏎")
            self.send.setEnabled_(ctl.has_unsent() and not ctl.sending)
            self.drop.setEnabled_(ctl.has_unsent() and not ctl.sending)
            hist = list(ctl.history)[:4]
            for i, lab in enumerate(self.hist):
                if i < len(hist):
                    h = hist[i]
                    lab.setStringValue_(
                        f"{time.strftime('%H:%M', time.localtime(h['sent_at']))}  #{h['batch']:<3} "
                        f"{fmt_dur(h['seconds']):>5}  {'auto' if h['auto'] else 'sent'}  {h['state']}")
                else:
                    lab.setStringValue_("nothing sent yet" if i == 0 and not hist else "")
            if ctl.auto:
                eta = f" · {fmt_dur(ctl.next_auto - t)}" if ctl.next_auto else " (paused)"
                self.auto.setTitle_(f"Auto: every {fmt_dur(ctl.auto)}{eta}")
            else:
                self.auto.setTitle_("Auto: off")
            busy = ctl.busy + ctl.jobs.qsize()
            self.stat.setStringValue_(ctl.status + (f"  [{busy} processing]" if busy > 1 else ""))
            self.msg.setStringValue_(f"Claude: {ctl.claude_msg}" if ctl.claude_msg else "")
            self.msg.setToolTip_(ctl.claude_msg)

    # -- HTTP API ---------------------------------------------------------------------------
    def shutdown():
        ctl.shutdown()
        K.NSApp.terminate_(None)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def reply(self, obj, code=200):
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urllib.parse.urlparse(self.path).path
            if path == "/status":
                return self.reply(ctl.state())
            if path == "/screens":
                sel = ctl.screen and ctl.screen["id"]
                return self.reply({"ok": True, "screens": [
                    {"n": i, "label": x["label"], "id": x["id"], "selected": x["id"] == sel}
                    for i, x in enumerate(ctl.screens, 1)]})
            self.reply({"ok": False, "error": "not found"}, 404)

        def do_POST(self):
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n).decode("utf-8", "replace") if n else ""
            try:
                if u.path == "/screen":
                    return self.reply(ctl.select_screen(q.get("which", [""])[0]))
                if u.path == "/start":
                    return self.reply(ctl.start())
                if u.path == "/discard":
                    return self.reply(ctl.discard())
                if u.path == "/stop":
                    return self.reply(ctl.stop())
                if u.path == "/toggle":
                    return self.reply(ctl.toggle())
                if u.path == "/send":
                    return self.reply(ctl.send(manual=True))
                if u.path == "/auto":
                    return self.reply(ctl.set_auto(int(q.get("seconds", ["0"])[0])))
                if u.path == "/say":
                    ctl.claude_msg = body.strip()
                    return self.reply({"ok": True})
                if u.path == "/quit":
                    self.reply({"ok": True})
                    AppHelper.callAfter(shutdown)
                    return
            except Exception as e:  # noqa: BLE001
                return self.reply({"ok": False, "error": str(e)}, 500)
            self.reply({"ok": False, "error": "not found"}, 404)

    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        print(f"screenfeed already running on port {PORT}", file=sys.stderr)
        sys.exit(1)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    app = K.NSApplication.sharedApplication()
    app.setActivationPolicy_(K.NSApplicationActivationPolicyAccessory)
    ui = ScreenfeedUI.alloc().init().build()
    ctl.update_screens(all_screens())
    start_id = screen_info(ui.panel.screen() or K.NSScreen.mainScreen())["id"]
    try:  # remember the last picked screen across restarts (if still connected)
        saved = json.loads((HOME / "state.json").read_text()).get("screen_id")
        if any(x["id"] == saved for x in ctl.screens):
            start_id = saved
    except (OSError, ValueError):
        pass
    ctl.screen = next((x for x in ctl.screens if x["id"] == start_id), ctl.screen)
    timer = K.NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(1 / 30, ui, "tick:", None, True)
    K.NSRunLoop.currentRunLoop().addTimer_forMode_(timer, K.NSRunLoopCommonModes)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: shutdown())
    print(f"screenfeed running · http://127.0.0.1:{PORT} · data in {HOME}", flush=True)
    app.run()


# --------------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------------


def api(method: str, path: str, data: str | None = None, timeout: float = 20) -> dict:
    body = data.encode() if data is not None else (b"" if method == "POST" else None)
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", data=body, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def running() -> bool:
    try:
        api("GET", "/status", timeout=1)
        return True
    except Exception:
        return False


def cmd_launch() -> None:
    if running():
        print("screenfeed already running")
        return
    HOME.mkdir(parents=True, exist_ok=True)
    log = (HOME / "app.log").open("a")
    subprocess.Popen([sys.executable, os.path.abspath(__file__), "app"], stdout=log, stderr=log,
                     stdin=subprocess.DEVNULL, start_new_session=True)
    for _ in range(100):
        if running():
            print(f"screenfeed running (log: {HOME / 'app.log'})")
            return
        time.sleep(0.2)
    sys.exit(f"screenfeed did not come up, see {HOME / 'app.log'}")


def cmd_watch() -> None:
    """One stdout line per ready batch or error. Remembers what it already printed."""
    cursor = HOME / "watch.cursor"
    HOME.mkdir(parents=True, exist_ok=True)
    READY.touch()
    seen = int(cursor.read_text() or 0) if cursor.exists() else len(READY.read_text().splitlines())
    alive, last_check = running(), 0.0
    print(f"SCREENFEED watching (app {'running' if alive else 'NOT running'})", flush=True)
    while True:
        lines = READY.read_text().splitlines()
        for line in lines[seen:]:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("error"):
                print(f"SCREENFEED ERROR batch {e.get('batch', '-')}: {e['error']}", flush=True)
            elif e.get("timeline"):
                print(f"SCREENFEED BATCH {e['batch']} ready ({e['seconds']}s, {e['moments']} moments): "
                      f"{e['timeline']} :: \"{e['speech'][:160]}\"", flush=True)
        if len(lines) != seen:
            seen = len(lines)
            cursor.write_text(str(seen))
        if time.time() - last_check > 5:
            last_check = time.time()
            ok = running()
            if alive and not ok:
                print("SCREENFEED ERROR app stopped running", flush=True)
            alive = ok
        time.sleep(0.5)


def main(argv: list[str]) -> None:
    cmd = argv[1] if len(argv) > 1 else "help"
    try:
        if cmd == "app":
            return run_app()
        if cmd == "launch":
            return cmd_launch()
        if cmd == "watch":
            return cmd_watch()
        cmd = {"pause": "stop", "resume": "start"}.get(cmd, cmd)
        if cmd in ("start", "stop", "toggle", "send", "discard"):
            r = api("POST", f"/{cmd}")
        elif cmd == "auto":
            arg = argv[2] if len(argv) > 2 else "off"
            r = api("POST", f"/auto?seconds={0 if arg == 'off' else int(arg)}")
        elif cmd == "say":
            r = api("POST", "/say", " ".join(argv[2:]))
        elif cmd == "screens":
            r = api("GET", "/screens")
            for x in r["screens"]:
                print(f"{'*' if x['selected'] else ' '} {x['n']}: {x['label']}")
            return
        elif cmd == "screen":
            which = " ".join(argv[2:])
            r = api("POST", "/screen?which=" + urllib.parse.quote(which))
        elif cmd == "status":
            r = api("GET", "/status")
        elif cmd == "quit":
            r = api("POST", "/quit")
        else:
            print(__doc__)
            return
    except urllib.error.URLError:
        sys.exit("screenfeed is not running (start it with: screenfeed launch)")
    print(json.dumps(r, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main(sys.argv)
