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
  screenfeed screen none      no screen: recording disabled (pauses a running recording)
  screenfeed send             cut the current batch and send it
  screenfeed retry <n>        process a failed batch again from the raw video it kept
  screenfeed deliver [pr | main | branch <name or #PR>]   where Claude puts each batch's changes: a new
                              pull request per batch from main (default), commits on main, or one branch/PR
  screenfeed workdir [path]   show or set the repository whose pull requests and branches the Deliver picker
                              suggests (launch and watch remember the git repository they run in)
  screenfeed note [on|off|toggle]  show or hide the note on the recorded screen (the frame stays)
  screenfeed languages [x,y|auto]  show or set the spoken languages (default en,de); others are re-read
                              in the last allowed one (whisper invents foreign text on silence)
  screenfeed auto <sec|off>   send automatically every <sec> seconds
  screenfeed say <text>       show a message from Claude in the widget
  screenfeed status           JSON status
  screenfeed watch            print one line per ready batch (for Claude's Monitor tool)
  screenfeed quit             stop the app (unsent content is sent as a recovered batch on next start)

No batch is lost: one recorder runs per recording session and writes 2-second chunks; Send only cuts
by time. Chunks stay in ~/.screenfeed/raw until their batch is processed; a failed batch keeps its
raw video for `retry`; content left by a crash or quit becomes a recovered batch on the next start.
A watchdog restarts a recorder that dies or stalls and the gap is noted in the batch.
Every action is logged to ~/.screenfeed/screenfeed.log.
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
LANG = os.environ.get("SCREENFEED_LANG", "auto")  # one fixed language; overrides the default list below
DEFAULT_LANGUAGES = os.environ.get("SCREENFEED_LANGUAGES", "en,de")
AUDIO = os.environ.get("SCREENFEED_AUDIO", "")  # device name, "none", or "" = system default input
FPS = int(os.environ.get("SCREENFEED_FPS", "5"))
KEEP_VIDEO = os.environ.get("SCREENFEED_KEEP_VIDEO") == "1"
MAX_MOMENTS = int(os.environ.get("SCREENFEED_MAX_MOMENTS", "24"))
# The watch ends after this many seconds with no screen picked and nothing unsent (see cmd_watch).
WATCH_IDLE_EXIT = int(os.environ.get("SCREENFEED_WATCH_IDLE_EXIT", "600"))
FFMPEG = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"


def build_info() -> str:
    """When the running code was built: script mtime, plus git commit (+ if uncommitted changes)."""
    src = Path(os.path.realpath(__file__))
    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(src.stat().st_mtime))
    try:
        git = ["git", "-C", str(src.parent)]
        rev = subprocess.run(git + ["rev-parse", "--short", "HEAD"], capture_output=True,
                             text=True, timeout=3).stdout.strip()
        dirty = subprocess.run(git + ["status", "--porcelain", "--", src.name], capture_output=True,
                               text=True, timeout=3).stdout.strip()
        if rev:
            return f"build {stamp} · {rev}{'+' if dirty else ''}"
    except (OSError, subprocess.SubprocessError):
        pass
    return f"build {stamp}"


BUILD = build_info()
FFPROBE = shutil.which("ffprobe") or "/opt/homebrew/bin/ffprobe"
WHISPER = shutil.which("whisper-cli") or "/opt/homebrew/bin/whisper-cli"
AUTO_STEPS = [0, 30, 60, 120, 300]
CHUNK_SECONDS = 2  # the recorder writes chunks this long; a batch cut waits at most this for its last one
STALL_SECONDS = 8.0  # recorder captured no frame for this long while recording: the watchdog replaces it
MAX_RESTARTS = 3  # watchdog restarts in a row before recording stops with an error
END_MARGIN = 0.3  # the END STATE frame is taken this long before the cut

READY = HOME / "ready.jsonl"
BATCHES = HOME / "batches"
RAW = HOME / "raw"
CONSUMED = RAW / "consumed_until"
# Screen capture: "sck" (ScreenCaptureKit through the sfcapture helper, which leaves screenfeed's own widget,
# frame and badges out of the picture) or "avfoundation" (ffmpeg alone; records whatever is on screen).
CAPTURE = os.environ.get("SCREENFEED_CAPTURE", "sck")
SCK_SOURCE = Path(os.path.realpath(__file__)).parent / "sfcapture.swift"
SCK_BINARY = HOME / "bin" / "sfcapture"
SCK = {"ready": False, "failed": None}  # failed: why the helper cannot be used (then: avfoundation)
MAX_CAPTURE_WIDTH = 3840


def now() -> float:
    return time.time()


def fmt_ago(t: float) -> str:
    """How long ago, as the widget shows it: "just now", "1 min ago", "2 h ago". Whole minutes only, so
    that nothing in the widget ticks every second (the recorded-time timer is the only seconds counter)."""
    sec = max(0, int(now() - t))
    if sec < 60:
        return "just now"
    if sec < 3600:
        return f"{sec // 60} min ago"
    return f"{sec // 3600} h ago"


def fmt_dur(sec: float) -> str:
    sec = max(0, int(sec))
    return f"{sec // 60}:{sec % 60:02d}"


def emit(event: dict) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    with READY.open("a") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")
    log("emit", event=json.dumps(event, ensure_ascii=False)[:300])


# --------------------------------------------------------------------------------------------
# Debug log: one line per action in ~/.screenfeed/screenfeed.log (rotated to .log.1 at 5 MB)
# --------------------------------------------------------------------------------------------

LOG = HOME / "screenfeed.log"
LOG_MAX = 5 * 1024 * 1024
_log_lock = threading.Lock()


def log(msg: str, detail: str | None = None, **fields) -> None:
    """Append `time [thread] msg key=value ...`; `detail` (e.g. a traceback) follows, indented."""
    t = now()
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) + f".{int(t * 1000) % 1000:03d}"
    parts = [f"{stamp} [{threading.current_thread().name}] {msg}"]
    for k, v in fields.items():
        v = f"{v:.2f}" if isinstance(v, float) else str(v)
        parts.append(f"{k}={json.dumps(v, ensure_ascii=False) if (not v or ' ' in v) else v}")
    text = " ".join(parts) + "\n"
    if detail:
        text += "".join(f"    {line}\n" for line in detail.rstrip().splitlines())
    with _log_lock:
        try:
            HOME.mkdir(parents=True, exist_ok=True)
            if LOG.exists() and LOG.stat().st_size > LOG_MAX:
                LOG.replace(LOG.with_name(LOG.name + ".1"))
            with LOG.open("a") as f:
                f.write(text)
        except OSError:
            pass


def log_exc(msg: str, exc: BaseException, **fields) -> None:
    import traceback

    log(msg, detail="".join(traceback.format_exception(type(exc), exc, exc.__traceback__)), error=f"{type(exc).__name__}: {exc}", **fields)


def _log_uncaught(args: threading.ExceptHookArgs) -> None:
    """Exceptions that end a thread (e.g. a failed Send) would otherwise only reach app.log."""
    if args.exc_value is not None:
        log_exc("UNCAUGHT exception, thread ended", args.exc_value,
                thread=args.thread.name if args.thread else "?")
    threading.__excepthook__(args)


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


def media_length(path: Path) -> float:
    """Length of a possibly unfinished (killed) Matroska file: its last packet time over all streams."""
    r = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "packet=pts_time", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True)
    ts = [float(x) for x in r.stdout.split() if x.replace(".", "", 1).isdigit()]
    return max(ts, default=0.0)


def probe_duration(path: Path) -> float:
    r = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def read_consumed() -> float:
    """Wall-clock time up to which recorded content has been processed into batches or discarded."""
    try:
        return float(CONSUMED.read_text().strip())
    except (OSError, ValueError):
        return 0.0


def write_consumed(t: float) -> None:
    RAW.mkdir(parents=True, exist_ok=True)
    tmp = CONSUMED.with_name(CONSUMED.name + ".tmp")
    tmp.write_text(f"{t:.3f}")
    tmp.replace(CONSUMED)


def ensure_sck() -> None:
    """Build the sfcapture helper when it is missing or older than its source (about 25 s, once)."""
    if CAPTURE != "sck":
        return
    try:
        if not SCK_SOURCE.exists():
            raise RuntimeError(f"{SCK_SOURCE} missing")
        if not SCK_BINARY.exists() or SCK_BINARY.stat().st_mtime < SCK_SOURCE.stat().st_mtime:
            SCK_BINARY.parent.mkdir(parents=True, exist_ok=True)
            log("sfcapture build", source=str(SCK_SOURCE))
            r = subprocess.run(["xcrun", "swiftc", "-O", "-swift-version", "5", str(SCK_SOURCE), "-o", str(SCK_BINARY)],
                               capture_output=True, text=True, timeout=600)
            if r.returncode != 0:
                raise RuntimeError(f"swiftc failed: {(r.stderr or r.stdout)[-300:]}")
        SCK["ready"] = True
        log("sfcapture ready", binary=str(SCK_BINARY))
    except Exception as e:  # noqa: BLE001
        SCK["failed"] = str(e)
        log_exc("sfcapture unavailable, recording with avfoundation (screenfeed itself shows in the video)", e)


SETTINGS_PANES = {
    "screen": "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",
    "microphone": "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone",
}


def permission_problems() -> list[dict]:
    """What keeps screenfeed from recording properly, for the widget's banner: missing Screen Recording or
    Microphone permission (granted to the app screenfeed was started from, e.g. the terminal), or a
    ScreenCaptureKit helper that cannot capture."""
    problems = []
    try:
        import Quartz

        if not Quartz.CGPreflightScreenCaptureAccess():
            problems.append({"kind": "screen", "text": "Allow Screen Recording for the app screenfeed runs in"})
    except Exception as e:  # noqa: BLE001
        log("permission check failed", what="screen", error=repr(e))
    if default_audio_device() != "none":
        try:
            import AVFoundation as AV

            # 3 = authorized; 0 = not asked yet (the first recording asks), 1/2 = restricted/denied
            if AV.AVCaptureDevice.authorizationStatusForMediaType_(AV.AVMediaTypeAudio) in (1, 2):
                problems.append({"kind": "microphone", "text": "Allow the Microphone for the app screenfeed runs in"})
        except Exception as e:  # noqa: BLE001
            log("permission check failed", what="microphone", error=repr(e))
    if SCK["failed"] and not problems:
        problems.append({"kind": "screen", "text": "Screen capture falls back: screenfeed shows in the video"})
    return problems


def capture_size(screen: dict) -> tuple[int, int]:
    """The display in pixels, at most MAX_CAPTURE_WIDTH wide, even sizes (H.264 needs them)."""
    _, _, w, h = screen["frame"]
    scale = float(screen.get("scale") or 1.0)
    pw, ph = w * scale, h * scale
    if pw > MAX_CAPTURE_WIDTH:
        pw, ph = MAX_CAPTURE_WIDTH, ph * MAX_CAPTURE_WIDTH / pw
    return int(pw) // 2 * 2, int(ph) // 2 * 2


class Recorder:
    """One ffmpeg for one recording session: from Rec until Pause, a screen switch or a recorder failure.

    It writes CHUNK_SECONDS-long chunks and lists each finished one in chunks.csv. Sending a batch only
    cuts by time and never stops or starts the recorder, so a batch cannot be lost to a recorder that fails
    to stop or to start. Chunks stay on disk until every batch that uses them has been processed (or the
    content was discarded), so a crash, a quit or a failed processing step loses nothing either. Only one
    recorder runs at a time: a second capture of the same screen and microphone can hang.
    """

    def __init__(self, screen: dict, audio: str, folder: Path | None = None):
        if folder is None:
            stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{int(now() * 1000) % 1000:03d}"
            folder = RAW / f"rec-{stamp}"
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "meta.json").write_text(json.dumps({"screen": screen, "audio": audio}))
        self.dir = folder
        self.name = folder.name
        self.screen = screen
        self.audio = audio
        self.proc: subprocess.Popen | None = None
        self.helper: subprocess.Popen | None = None  # sfcapture, when capturing with ScreenCaptureKit
        self.capture = "avfoundation"
        self.started_at = now()
        self.stopped_at: float | None = None
        self.origin: float | None = None  # wall-clock time of stream time 0
        self.seen_frames = 0  # watchdog: frames captured when last checked, and when that last grew
        self.grew_at = now()
        self.warned = False

    @classmethod
    def load(cls, folder: Path) -> "Recorder":
        """A recorder folder left by an earlier run (crash or quit), for recovery."""
        meta = json.loads((folder / "meta.json").read_text())
        r = cls(meta["screen"], meta["audio"], folder)
        r.started_at = (folder / "meta.json").stat().st_mtime  # written when the recorder started
        ch = r.chunks(partial=True)
        r.started_at = ch[0]["start"] if ch else folder.stat().st_mtime
        r.stopped_at = ch[-1]["end"] if ch else r.started_at
        return r

    def start(self) -> None:
        if CAPTURE == "sck" and SCK["ready"] and not SCK["failed"] and self.screen.get("id") is not None:
            return self.start_sck()
        cmd = [
            FFMPEG, "-hide_banner", "-loglevel", "warning", "-nostats", "-y",
            # On some macOS versions AVFoundation hands out screen frames with pts 0 (and ignores
            # -framerate); without wall-clock timestamps the video time barely advances and no chunk is
            # ever finished. fps= then brings the frame rate down to FPS.
            "-use_wallclock_as_timestamps", "1",
            "-f", "avfoundation", "-capture_cursor", "1", "-capture_mouse_clicks", "1",
            "-framerate", str(FPS),
            "-i", f"Capture screen {self.screen['index']}:{self.audio}",
            "-vf", f"fps={FPS},scale='min(3840,iw)':-2",
            "-c:v", "h264_videotoolbox", "-b:v", "5M", "-g", str(FPS * CHUNK_SECONDS),
        ]
        if self.audio != "none":
            cmd += ["-c:a", "aac", "-b:a", "96k", "-ac", "1"]
        cmd += [
            "-f", "segment", "-segment_time", str(CHUNK_SECONDS), "-segment_format", "matroska",
            "-reset_timestamps", "1", "-segment_list", str(self.dir / "chunks.csv"),
            "-segment_list_type", "csv", "-segment_list_flags", "+live",
            str(self.dir / "chunk-%05d.mkv"),
            # frame counter once a second: the watchdog's sign of life, whatever the muxer buffers
            "-progress", str(self.dir / "progress.txt"), "-stats_period", "1",
        ]
        self.started_at = self.grew_at = now()
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=(self.dir / "ffmpeg.log").open("w"),
        )
        log("recorder start", rec=self.name, pid=self.proc.pid,
            screen=f"{self.screen['index']}:{self.screen.get('label', '')}", audio=self.audio)

    def start_sck(self) -> None:
        """ScreenCaptureKit: sfcapture writes raw frames into a FIFO that ffmpeg reads, without screenfeed's
        own windows; ffmpeg adds the microphone and cuts the chunks as before."""
        self.capture = "sck"
        width, height = capture_size(self.screen)
        fifo = self.dir / "screen.fifo"
        if not fifo.exists():
            os.mkfifo(fifo)
        cmd = [
            FFMPEG, "-hide_banner", "-loglevel", "warning", "-nostats", "-y",
            "-use_wallclock_as_timestamps", "1",
            "-f", "rawvideo", "-pix_fmt", "nv12", "-s", f"{width}x{height}", "-framerate", str(FPS),
            "-i", str(fifo),
        ]
        if self.audio != "none":
            cmd += ["-use_wallclock_as_timestamps", "1", "-f", "avfoundation", "-i", f":{self.audio}",
                    "-map", "0:v", "-map", "1:a"]
        cmd += ["-vf", f"fps={FPS}", "-c:v", "h264_videotoolbox", "-b:v", "5M", "-g", str(FPS * CHUNK_SECONDS)]
        if self.audio != "none":
            cmd += ["-c:a", "aac", "-b:a", "96k", "-ac", "1"]
        cmd += [
            "-f", "segment", "-segment_time", str(CHUNK_SECONDS), "-segment_format", "matroska",
            "-reset_timestamps", "1", "-segment_list", str(self.dir / "chunks.csv"),
            "-segment_list_type", "csv", "-segment_list_flags", "+live",
            str(self.dir / "chunk-%05d.mkv"),
            "-progress", str(self.dir / "progress.txt"), "-stats_period", "1",
        ]
        self.started_at = self.grew_at = now()
        # ffmpeg first: opening the FIFO for writing waits until a reader has it open.
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                     stderr=(self.dir / "ffmpeg.log").open("w"))
        self.helper = subprocess.Popen(
            [str(SCK_BINARY), "--display", str(self.screen["id"]), "--width", str(width), "--height", str(height),
             "--fps", str(FPS), "--exclude-pid", str(os.getpid()), "--out", str(fifo)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=(self.dir / "sfcapture.log").open("w"))
        log("recorder start", rec=self.name, pid=self.proc.pid, helper=self.helper.pid, capture="sck",
            size=f"{width}x{height}", screen=f"{self.screen['id']}:{self.screen.get('label', '')}", audio=self.audio)

    def alive(self) -> bool:
        if self.helper is not None and self.helper.poll() is not None:
            if self.helper.returncode != 0 and not SCK["failed"]:
                # cannot capture (e.g. no permission): every later recorder falls back to avfoundation
                SCK["failed"] = self.helper_tail() or f"sfcapture exited with {self.helper.returncode}"
                log("sfcapture failed, falling back to avfoundation", rec=self.name, error=SCK["failed"])
            return False
        return bool(self.proc and self.proc.poll() is None)

    def helper_tail(self) -> str:
        try:
            return " | ".join((self.dir / "sfcapture.log").read_text(errors="replace").strip().splitlines()[-2:])
        except OSError:
            return ""

    def chunks(self, partial: bool = False) -> list[dict]:
        """Chunks still on disk, oldest first, as {path, start, end} in wall-clock time. Finished chunks
        only, unless `partial`: then also chunk files ffmpeg never listed (the one being written, or the
        last one of a killed recorder), timed from their packets; those are never thrown away unread.

        A chunk file is last written when the chunk is finished, so its mtime minus its end in stream time is
        the wall-clock time of stream time 0 plus a small write latency; the smallest value seen is the best
        estimate of the origin.
        """
        try:
            rows = (self.dir / "chunks.csv").read_text().splitlines()
        except OSError:
            return []
        out = []
        for row in rows:
            cols = row.rsplit(",", 2)
            if len(cols) != 3:
                continue
            p = Path(cols[0]) if os.path.isabs(cols[0]) else self.dir / cols[0]
            try:
                a, b, m = float(cols[1]), float(cols[2]), p.stat().st_mtime
            except (OSError, ValueError):
                continue  # deleted (consumed) or not a chunk line
            self.origin = m - b if self.origin is None else min(self.origin, m - b)
            out.append((p, a, b))
        if self.origin is not None and self.origin - self.started_at > 2 * CHUNK_SECONDS + 3:
            # packet times far behind the wall clock (pts 0 from the capture): time chunks by when written
            res, at = [], self.started_at
            for p, a, b in out:
                end = max(at + (b - a), p.stat().st_mtime)
                res.append({"path": str(p), "start": at, "end": end})
                at = end
        else:
            res = [{"path": str(p), "start": self.origin + a, "end": self.origin + b} for p, a, b in out]
        if partial:
            listed = {Path(c["path"]).name for c in res}
            at = res[-1]["end"] if res else self.started_at
            for p in sorted(self.dir.glob("chunk-*.mkv")):
                if p.name in listed or p.stat().st_size == 0:
                    continue
                dur = media_length(p)
                if dur <= 0:
                    log("unreadable unlisted chunk kept", rec=self.name, chunk=p.name, bytes=p.stat().st_size)
                    continue
                # its packet times can be wrong (pts 0 from the capture); when it was last written is not
                end = max(at + dur, p.stat().st_mtime)
                res.append({"path": str(p), "start": at, "end": end, "partial": True})
                at = end
        return res

    def frames_captured(self) -> int:
        """Frames ffmpeg has encoded so far (from its -progress output), 0 before the first report."""
        try:
            with (self.dir / "progress.txt").open("rb") as f:
                f.seek(max(0, f.seek(0, 2) - 600))
                tail = f.read().decode(errors="replace")
        except OSError:
            return 0
        # frames encoded plus frames dropped: input still arriving is the sign of life
        last = lambda key: int((re.findall(rf"^{key}=(\d+)", tail, re.M) or ["0"])[-1])  # noqa: E731
        return last("frame") + last("drop_frames")

    def last_chunk_at(self) -> float:
        """When the newest chunk was finished; before the first one, when the recorder started."""
        try:
            return max(self.started_at, (self.dir / "chunks.csv").stat().st_mtime)
        except OSError:
            return self.started_at

    def stop(self) -> None:
        """Stop ffmpeg: q (finishes the current chunk), then SIGTERM, then SIGKILL. Never raises: at worst
        the chunk being written (CHUNK_SECONDS) is lost; everything before it is on disk already."""
        self.stopped_at = self.stopped_at or now()
        h = self.helper
        if h is not None and h.poll() is None:
            # Without new frames ffmpeg finishes the video at once; q below then ends the microphone.
            h.terminate()
            try:
                h.wait(timeout=3)
            except subprocess.TimeoutExpired:
                h.kill()
        p = self.proc
        if not p or p.poll() is not None:
            log("recorder stop: already ended", rec=self.name, exit=p.returncode if p else None)
            return
        log("recorder stop requested", rec=self.name, pid=p.pid, chunks=len(self.chunks()))
        try:
            p.stdin.write(b"q")
            p.stdin.flush()
        except Exception as e:  # noqa: BLE001
            log("recorder stop: sending q failed", rec=self.name, error=repr(e))
        for how, timeout in (("q", 6), ("SIGTERM", 3), ("SIGKILL", 3)):
            if how == "SIGTERM":
                p.terminate()
            elif how == "SIGKILL":
                p.kill()
            try:
                p.wait(timeout=timeout)
                break
            except subprocess.TimeoutExpired:
                log(f"recorder ignored {how} for {timeout}s", rec=self.name, pid=p.pid,
                    ffmpeg=self.log_tail())
        log("recorder stopped", rec=self.name, pid=p.pid, exit=p.returncode, chunks=len(self.chunks()),
            took=now() - self.stopped_at)

    def log_tail(self) -> str:
        try:
            return " | ".join((self.dir / "ffmpeg.log").read_text(errors="replace").strip().splitlines()[-3:])
        except Exception:  # noqa: BLE001
            return ""

    def delete_chunks_until(self, t: float) -> int:
        n = 0
        for c in self.chunks(partial=not self.alive()):
            if c["end"] <= t + 0.01:
                Path(c["path"]).unlink(missing_ok=True)
                n += 1
        return n


class Clip:
    """The chunks of one recorder that a batch covers, joined into one file: what transcription and frame
    rendering read. Wall clock = actual_start + offset in the file."""

    def __init__(self, path: Path, screen: dict, audio: str, actual_start: float, duration: float,
                 clip_from: float, clip_to: float):
        self.path, self.screen, self.audio = path, screen, audio
        self.actual_start, self.duration = actual_start, duration
        self.clip_from, self.clip_to = clip_from, clip_to


class Controller:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.recording = False
        self.rec: Recorder | None = None  # the running recorder
        self.recorders: list[Recorder] = []  # recorders whose chunks are not all consumed yet
        self.rec_since: float | None = None  # start of content not yet sent (the last cut)
        self.restarting = False  # a recorder is being replaced (screen switch or watchdog)
        self.restarts = 0  # watchdog restarts in a row without a new chunk
        self.next_watch = 0.0
        self.auto = 0
        self.next_auto: float | None = None
        self.sending = False  # kept for the widget; sending is instant now
        self.samples: deque = deque()  # (t, x, y, buttons, in_widget)
        self.apps: deque = deque()  # (t, frontmost app name)
        self.screen: dict | None = None  # the selected screen (recorded on next start / now)
        self.screens: list[dict] = []  # all connected screens, refreshed by the UI
        self.flash_until = 0.0  # show "this screen will be recorded" preview until then
        self.widget_frame = (0, 0, 0, 0)
        self.status = "Ready"
        self.claude_msg = ""
        self.claude_msg_at: float | None = None
        self.ui_tick_at: float | None = None  # the widget's last refresh: a frozen widget shows here
        self.permission_problems: list[dict] = []  # checked by the widget every few seconds
        self.last_batch: dict | None = None
        self.history: deque = deque(maxlen=20)  # newest first: sent batches and their fate
        self.busy = 0
        self.batch_no = self._next_batch_no()
        self.jobs: queue.Queue = queue.Queue()
        self._recover()
        threading.Thread(target=self._worker, daemon=True, name="worker").start()

    def _next_batch_no(self) -> int:
        BATCHES.mkdir(parents=True, exist_ok=True)
        nums = [int(m.group(1)) for p in BATCHES.iterdir() if (m := re.match(r"(\d+)-", p.name))]
        return max(nums, default=0) + 1

    def _recover(self) -> None:
        """Recorded content of an earlier run that never made it into a processed batch (crash, quit,
        processing interrupted) becomes a batch now instead of being deleted."""
        if not RAW.exists():
            return
        for p in RAW.glob("seg-*"):  # files of the old one-recorder-per-batch format
            log("old raw file deleted", file=p.name)
            p.unlink(missing_ok=True)
        consumed = read_consumed()
        recs = []
        for folder in sorted(RAW.glob("rec-*")):
            try:
                r = Recorder.load(folder)
            except Exception as e:  # noqa: BLE001
                log_exc("recover: unreadable recorder folder, kept as is", e, rec=folder.name)
                continue
            if any(c["end"] > consumed + 0.5 for c in r.chunks(partial=True)):
                recs.append(r)
            else:
                log("recover: nothing unsent, folder removed", rec=folder.name)
                shutil.rmtree(folder, ignore_errors=True)
        log("recover", consumed=consumed, recorders=",".join(r.name for r in recs) or "none")
        if recs:
            self.recorders = recs
            t0 = max(consumed, min(r.started_at for r in recs))
            t1 = max(r.stopped_at or r.started_at for r in recs)
            self.rec_since = t1
            no = self._queue(t0, t1, manual=True, samples=[], apps=[], recovered=True)
            self.status = f"Batch {no}: recovered from last run, processing…"

    # -- recording control ---------------------------------------------------------------

    def _recorded(self, t0: float, t1: float) -> float:
        """Seconds recorded between t0 and t1, over all recorders."""
        total = 0.0
        for r in self.recorders:
            a, b = max(t0, r.started_at), min(t1, r.stopped_at or now())
            total += max(0.0, b - a)
        return total

    def start(self) -> dict:
        log("action start", recording=self.recording, screen=self.screen and self.screen["label"])
        while self.restarting:
            time.sleep(0.05)
        with self.lock:
            if self.recording:
                return self.state()
            if not self.screen:
                self.status = "No screen selected · pick one to record"
                return {"ok": False, "error": "no screen selected"}
            fresh = not self.has_unsent()
            rec = Recorder(self.screen, default_audio_device())
            rec.start()
            self.rec = rec
            self.recorders.append(rec)
            self.recording = True
            self.restarts = 0
            if fresh or self.rec_since is None:
                self.rec_since = rec.started_at
            if self.auto:
                self.next_auto = now() + self.auto
            self.status = f"Recording {self.screen['label']} · mic: {rec.audio}"
        return self.state()

    def stop(self) -> dict:
        log("action stop (pause)", recording=self.recording)
        while self.restarting:
            time.sleep(0.05)
        with self.lock:
            if not self.recording:
                return self.state()
            rec, self.rec, self.recording, self.next_auto = self.rec, None, False, None
        rec.stop()
        with self.lock:
            self.status = "Paused"
        return self.state()

    def toggle(self) -> dict:
        return self.stop() if self.recording else self.start()

    def unsent_seconds(self) -> float:
        """Recorded (not wall-clock) seconds not yet sent; does not grow while paused."""
        return self._recorded(self.rec_since, now()) if self.rec_since is not None else 0.0

    def has_unsent(self) -> bool:
        return bool(self.recording or self.unsent_seconds() > 0.3)

    def paused_with_unsent(self) -> bool:
        return not self.recording and self.unsent_seconds() > 0.3

    def discard(self) -> dict:
        """Throw away everything not yet sent; a running recording keeps running."""
        log("action discard", recording=self.recording, unsent=self.unsent_seconds())
        with self.lock:
            t = now()
            self.rec_since = t
            self.samples.clear()
            if self.auto and self.recording:
                self.next_auto = now() + self.auto
            # the worker deletes the chunks once the batches queued before are done with them
            self.jobs.put({"discard_until": t})
            self.status = "Unsent content discarded"
        return self.state()

    def update_screens(self, screens: list[dict]) -> None:
        """Called by the UI with the current display list; keeps the selection valid."""
        with self.lock:
            self.screens = screens
            if self.screen:  # selected display unplugged -> no screen (never silently record another)
                self.screen = next((s for s in screens if s["id"] == self.screen["id"]), None)

    def select_screen(self, which: str | int) -> dict:
        """Select by list position (1-based), display id, or (part of) the name; "none" = no screen."""
        w = str(which).strip()
        log("action select screen", which=w, recording=self.recording)
        if w.lower() in ("none", "0", "off", "no", "-"):
            if self.recording:
                self.stop()
            with self.lock:
                self.screen = None
                self.status = "No screen selected · recording disabled"
            try:
                (HOME / "state.json").write_text(json.dumps({"screen_id": None}))
            except OSError:
                pass
            return self.state()
        with self.lock:
            scr = None
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
            switch = self.recording and self.rec and self.rec.screen["id"] != scr["id"]
        if switch:
            self._replace_recorder("screen switch")
        with self.lock:
            self.status = (f"Recording {scr['label']}" if self.recording
                           else f"Will record {scr['label']}")
        return self.state()

    def _replace_recorder(self, reason: str) -> None:
        """Stop the running recorder, then start a new one on the selected screen (screen switch, or the
        watchdog found it dead or stalled). What the old one recorded stays and is sent with the batch."""
        with self.lock:
            old = self.rec
            if not (self.recording and old) or self.restarting:
                return
            self.restarting = True
        log("recorder replace", reason=reason, old=old.name)
        try:
            old.stop()
            with self.lock:
                if not self.recording or self.rec is not old:
                    return
                if not self.screen:
                    self.recording, self.rec, self.next_auto = False, None, None
                    self.status = "Screen gone · recording paused"
                    return
                new = Recorder(self.screen, default_audio_device())
                new.start()
                # recording was meant to go on while the old one was being stopped: count that as its
                # span, so a recorder that had to be killed shows up as a gap, not as a pause
                old.stopped_at = new.started_at
                self.rec = new
                self.recorders.append(new)
        except Exception as e:  # noqa: BLE001
            log_exc("recorder replace failed, recording stopped", e)
            with self.lock:
                self.recording, self.rec, self.next_auto = False, None, None
                self.status = f"Recorder failed to start · see {LOG}"[:140]
            emit({"error": f"recorder failed to start, recording stopped: {e}", "t": now()})
        finally:
            self.restarting = False

    def _watch(self) -> None:
        """Watchdog: a recorder that exited or has written no chunk for STALL_SECONDS is replaced at once.
        After MAX_RESTARTS failures in a row recording stops and Claude gets an error event."""
        with self.lock:
            rec = self.rec
            if not (self.recording and rec) or self.restarting:
                return
        dead = not rec.alive()
        frames = rec.frames_captured()
        if frames > rec.seen_frames:  # still capturing: never kill a recorder that makes progress
            rec.seen_frames, rec.grew_at = frames, now()
        idle = now() - rec.grew_at
        if not dead and idle < STALL_SECONDS:
            if frames and not rec.chunks() and now() - rec.started_at > 4 * CHUNK_SECONDS and not rec.warned:
                rec.warned = True  # captures, but finishes no chunk: batches fall back to the unfinished file
                log("recorder captures but finishes no chunk", rec=rec.name, frames=frames, ffmpeg=rec.log_tail())
            if rec.chunks():
                self.restarts = 0
            return
        self.restarts += 1
        why = (f"recorder exited ({rec.proc.returncode if rec.proc else '-'}): {rec.log_tail()}" if dead
               else f"captured no frame for {idle:.0f}s")
        log("watchdog", problem=why, rec=rec.name, attempt=self.restarts)
        if self.restarts > MAX_RESTARTS:
            with self.lock:
                self.recording, self.rec, self.next_auto = False, None, None
                self.status = f"Recorder keeps failing, recording stopped · see {LOG}"[:140]
            threading.Thread(target=rec.stop, daemon=True, name="stop-failed").start()
            emit({"error": f"recorder failed {MAX_RESTARTS} times in a row, recording stopped: {why}",
                  "t": now()})
            return
        self.status = f"Recorder restarted: {why}"[:140]
        threading.Thread(target=self._replace_recorder, args=(why,), daemon=True, name="watchdog").start()

    def set_auto(self, seconds: int) -> dict:
        log("action auto-send", seconds=seconds)
        with self.lock:
            self.auto = max(0, int(seconds))
            self.next_auto = now() + self.auto if (self.auto and self.recording) else None
        return self.state()

    def cycle_auto(self) -> None:
        i = AUTO_STEPS.index(self.auto) if self.auto in AUTO_STEPS else 0
        self.set_auto(AUTO_STEPS[(i + 1) % len(AUTO_STEPS)])

    def _queue(self, t0: float, t1: float, manual: bool, samples: list, apps: list,
               recovered: bool = False, deliver: dict | None = None) -> int:
        """Queue the batch t0..t1 for the worker (caller holds the lock). Recorders are not touched."""
        no = self.batch_no
        self.batch_no += 1
        entry = {"batch": no, "sent_at": now(), "auto": not manual, "state": "processing",
                 "seconds": round(self._recorded(t0, t1), 1)}
        self.history.appendleft(entry)
        recs = [r for r in self.recorders if r.started_at < t1 and (r.stopped_at or now()) > t0]
        self.jobs.put(dict(no=no, t0=t0, t1=t1, recs=recs, samples=samples, apps=apps, manual=manual,
                           recovered=recovered, entry=entry, deliver=deliver or deliver_mode()))
        log("batch queued", batch=no, t0=t0, t1=t1, recorders=",".join(r.name for r in recs) or "none",
            seconds=entry["seconds"], samples=len(samples), recovered=recovered)
        return no

    def send(self, manual: bool = True) -> dict:
        """Cut a batch at now. Only bookkeeping: the recorder keeps running, the worker collects the
        chunks, so nothing here can lose recorded content."""
        log("action send", manual=manual, recording=self.recording, unsent=self.unsent_seconds())
        with self.lock:
            if not self.has_unsent():
                self.status = "Nothing to send"
                return {"ok": False, "error": "nothing recorded"}
            t1 = now()
            t0 = self.rec_since if self.rec_since is not None else t1
            self.rec_since = t1
            samples = [s for s in self.samples if t0 - 1 <= s[0] <= t1 + 1]
            while self.samples and self.samples[0][0] < t1 - 2:
                self.samples.popleft()
            no = self._queue(t0, t1, manual, samples=samples, apps=list(self.apps), deliver=deliver_mode())
            if self.auto and self.recording:
                self.next_auto = now() + self.auto
            self.status = f"Batch {no}: processing…"
        return {"ok": True, "batch": no}

    def retry(self, no: int) -> dict:
        """Process a failed batch again from the raw video and job it kept."""
        log("action retry", batch=no)
        d = next(iter(sorted(BATCHES.glob(f"{int(no):04d}-*"))), None)
        if not d or not (d / "job.json").exists():
            return {"ok": False, "error": f"batch {no} has nothing to retry (no job.json)"}
        job = json.loads((d / "job.json").read_text())
        missing = [c["path"] for p in job["parts"] for c in p["chunks"] if not Path(c["path"]).exists()]
        if missing:
            return {"ok": False, "error": f"batch {no}: {len(missing)} raw chunks missing"}
        with self.lock:
            entry = {"batch": no, "sent_at": now(), "auto": False, "state": "processing",
                     "seconds": round(job["t1"] - job["t0"], 1)}
            self.history.appendleft(entry)
            job.update(entry=entry, retry=True, dir=str(d))
            self.jobs.put(job)
            self.status = f"Batch {no}: retrying…"
        return {"ok": True, "batch": no}

    # -- sampling (called from the UI thread at ~30 Hz) ------------------------------------

    def sample(self, t, x, y, buttons, in_widget, app) -> None:
        if not self.recording:
            return
        self.samples.append((t, x, y, buttons, in_widget))
        if not self.apps or self.apps[-1][1] != app:
            self.apps.append((t, app))
            while len(self.apps) > 500:
                self.apps.popleft()

    def tick(self) -> None:
        t = now()
        if t >= self.next_watch:
            self.next_watch = t + 1.0
            self._watch()
        if self.auto and self.recording and self.next_auto and t >= self.next_auto:
            self.next_auto = t + self.auto
            threading.Thread(target=self.send, kwargs={"manual": False}, daemon=True, name="auto-send").start()

    # -- processing -----------------------------------------------------------------------

    def _parts(self, job: dict) -> list[dict]:
        """The chunks each recorder has for t0..t1. Waits until a running recorder has finished the chunk
        that covers t1 (at most CHUNK_SECONDS plus encoding), or gives up after STALL_SECONDS and reports
        the rest as a gap."""
        t0, t1 = job["t0"], job["t1"]
        deadline = now() + STALL_SECONDS + CHUNK_SECONDS
        for r in job["recs"]:
            target = min(t1, r.stopped_at or t1)  # a paused recorder finishes its last chunk on stopping
            while r.alive() and now() < deadline:
                ch = r.chunks()
                if ch and ch[-1]["end"] >= target - 0.05:
                    break
                time.sleep(0.2)
        parts = []
        for r in job["recs"]:
            chunks = [c for c in r.chunks(partial=True) if c["end"] > t0 and c["start"] < t1]
            if any(c.get("partial") for c in chunks):
                log("using unfinished chunk", rec=r.name, alive=r.alive())
            expect = [max(t0, r.started_at), min(t1, r.stopped_at or t1)]
            parts.append({"rec": r.name, "screen": r.screen, "audio": r.audio, "chunks": chunks,
                          "expected": expect})
        return parts

    def _consume(self, t: float) -> None:
        """Content up to t is in processed batches (or discarded): delete its chunks and finished recorders."""
        consumed = max(read_consumed(), t)
        write_consumed(consumed)
        with self.lock:
            for r in list(self.recorders):
                n = r.delete_chunks_until(consumed)
                if (r is not self.rec and not r.alive() and (r.stopped_at or 0) <= consumed + 0.01
                        and not any(r.dir.glob("chunk-*.mkv"))):
                    shutil.rmtree(r.dir, ignore_errors=True)
                    self.recorders.remove(r)
                    log("recorder folder removed", rec=r.name)
                elif n:
                    log("chunks deleted", rec=r.name, count=n)

    def _worker(self) -> None:
        while True:
            job = self.jobs.get()
            if "discard_until" in job:
                log("discard applied", until=job["discard_until"])
                self._consume(job["discard_until"])
                continue
            self.busy += 1
            log("batch processing", batch=job["no"])
            d = Path(job["dir"]) if job.get("dir") else (
                BATCHES / f"{job['no']:04d}-{time.strftime('%Y%m%d-%H%M%S', time.localtime(job['t0']))}")
            try:
                d.mkdir(parents=True, exist_ok=True)
                if "parts" not in job:
                    job["parts"] = self._parts(job)
                save_job(job, d)
                result = process_batch(job, d)
                log("batch done", batch=job["no"], skipped=bool(result.get("skipped")),
                    moments=result.get("moments"))
                with self.lock:
                    self.last_batch = result
                    if result.get("skipped"):
                        job["entry"]["state"] = "skipped (nothing said)"
                        self.status = f"Batch {job['no']}: nothing said, skipped"
                    else:
                        shots = f"{result['moments']} screenshot{'' if result['moments'] == 1 else 's'}"
                        job["entry"]["state"] = f"→ Claude · {shots}"
                        self.status = f"Batch {job['no']} → Claude ({shots})"
                if not job.get("retry"):
                    self._consume(job["t1"])
            except Exception as e:  # noqa: BLE001
                log_exc("batch processing failed", e, batch=job["no"])
                kept = keep_raw(job, d)
                hint = f"; raw video kept, retry with: screenfeed retry {job['no']}" if kept else ""
                emit({"batch": job["no"], "error": f"{type(e).__name__}: {e}{hint}", "t": now()})
                job["entry"]["state"] = "failed"
                self.status = f"Batch {job['no']} failed: {e}"[:140]
                if kept and not job.get("retry"):
                    self._consume(job["t1"])
            finally:
                self.busy -= 1

    def state(self) -> dict:
        t = now()
        return {
            "ok": True,
            "recording": self.recording,
            "paused_with_unsent": self.paused_with_unsent(),
            "unsent_seconds": round(self.unsent_seconds(), 1),
            "auto_seconds": self.auto,
            "next_auto_in": round(self.next_auto - t, 1) if self.next_auto else None,
            "sending": self.sending,
            "restarting": self.restarting,
            "processing": self.busy + self.jobs.qsize(),
            "next_batch": self.batch_no,
            "screen": self.screen and self.screen["label"],
            "recording_screen": self.rec.screen["label"] if (self.recording and self.rec) else None,
            "recorder": self.rec.name if self.rec else None,
            "screens": [f"{i}: {s['label']}" for i, s in enumerate(self.screens, 1)],
            "status": self.status,
            "claude_msg": self.claude_msg,
            "claude_msg_ago": fmt_ago(self.claude_msg_at) if self.claude_msg_at else None,
            "last_batch": self.last_batch,
            "build": BUILD,
            "log": str(LOG),
            "ui_tick_age": round(now() - self.ui_tick_at, 1) if self.ui_tick_at else None,
            "widget_frame": [round(v) for v in self.widget_frame],  # x, y (bottom-left), width, height in points
            "languages": languages() or "auto",
            "deliver": deliver_mode(),
            "deliver_label": deliver_label(deliver_mode()),
            "workdir": config_value("workdir"),
            "overlay_note": overlay_note_shown(),
            "capture": (self.rec.capture if self.rec else
                        ("sck" if CAPTURE == "sck" and SCK["ready"] and not SCK["failed"] else "avfoundation")),
            "capture_problem": SCK["failed"],
            "permission_problems": [p["text"] for p in self.permission_problems],
            "history": [dict(h, sent_at=time.strftime("%H:%M:%S", time.localtime(h["sent_at"])),
                             sent_ago=fmt_ago(h["sent_at"]))
                        for h in self.history],
        }

    def shutdown(self) -> None:
        """Stop recording. Unsent content stays on disk and is sent as a recovered batch on next launch."""
        log("shutdown", recording=self.recording, unsent=self.unsent_seconds())
        with self.lock:
            rec, self.rec, self.recording = self.rec, None, False
        if rec:
            rec.stop()


def save_job(job: dict, d: Path) -> None:
    """What a batch is made of, so that it can be processed again (screenfeed retry)."""
    keep = {k: job[k] for k in ("no", "t0", "t1", "samples", "apps", "manual", "recovered", "parts", "deliver") if k in job}
    (d / "job.json").write_text(json.dumps(keep, ensure_ascii=False))


def keep_raw(job: dict, d: Path) -> bool:
    """After a failure: copy the batch's chunks into the batch folder, so they survive the cleanup."""
    try:
        if "parts" not in job:
            return False
        raw = d / "raw"
        raw.mkdir(parents=True, exist_ok=True)
        for part in job["parts"]:
            for c in part["chunks"]:
                src = Path(c["path"])
                dst = raw / f"{part['rec']}-{src.name}"
                if src != dst and src.exists():
                    shutil.copy2(src, dst)
                c["path"] = str(dst)
        save_job(job, d)
        log("raw video kept for retry", batch=job["no"], dir=str(raw))
        return True
    except Exception as e:  # noqa: BLE001
        log_exc("keeping raw video failed", e, batch=job.get("no"))
        return False


def join_chunks(part: dict, t0: float, t1: float, work: Path, i: int) -> Clip | None:
    """One file from a recorder's chunks (stream copy), with the wall-clock time it starts at."""
    chunks = [c for c in part["chunks"] if Path(c["path"]).exists()]
    if not chunks:
        return None
    lst = work / f"chunks-{i}.txt"
    lst.write_text("".join(f"file '{c['path']}'\n" for c in chunks))
    out = work / f"clip-{i}.mkv"
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
                    "-i", str(lst), "-c", "copy", str(out)], check=True)
    start = chunks[0]["start"]
    duration = probe_duration(out)
    log("chunks joined", rec=part["rec"], chunks=len(chunks), duration=duration)
    # the span recorded is what the chunks cover in wall-clock time; an unfinished chunk can have broken
    # packet times, so the file may be shorter (frames and audio offsets are clamped to it)
    return Clip(out, part["screen"], part["audio"], start, duration,
                max(t0, start), min(t1, max(start + duration, chunks[-1]["end"])))


def gaps_of(part: dict, min_len: float = 2.5) -> list[tuple[float, float]]:
    """Stretches the recorder should have covered but has no chunk for (stall, crash, killed recorder)."""
    a, b = part["expected"]
    out, at = [], a
    for c in sorted(part["chunks"], key=lambda c: c["start"]):
        if c["start"] - at >= min_len:
            out.append((at, c["start"]))
        at = max(at, c["end"])
    if b - at >= min_len:
        out.append((at, b))
    return out


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
    # "ご視聴ありがとうございました" = "thanks for watching": whisper's favourite invention on near-silence
    return any(k in low for k in ("untertitel", "amaraorg", "zdf", "subtitles by", "ご視聴ありがとうございました",
                                     "субтитр", "dimatorzok"))  # Russian subtitle credits, likewise invented


def max_volume(wav: Path) -> float:
    r = subprocess.run([FFMPEG, "-hide_banner", "-nostats", "-i", str(wav), "-af", "volumedetect",
                        "-f", "null", "-"], capture_output=True, text=True)
    m = re.search(r"max_volume:\s*(-?[\d.]+) dB", r.stderr)
    return float(m.group(1)) if m else -91.0


CONFIG = HOME / "config.json"
_last_language: str | None = None  # the language of the last transcript, the fallback for the next one


def languages() -> list[str]:
    """The languages whisper may pick from; empty means any. `screenfeed languages` sets them (config.json,
    read on every batch), else SCREENFEED_LANG or SCREENFEED_LANGUAGES."""
    try:
        value = json.loads(CONFIG.read_text()).get("languages")
    except (OSError, ValueError):
        value = None
    if value is None:
        value = LANG if LANG != "auto" else DEFAULT_LANGUAGES
    value = str(value).strip().lower()
    return [] if value in ("", "auto", "any") else [x.strip() for x in value.split(",") if x.strip()]


DELIVER_MODES = ("pr", "main", "branch")


def deliver_mode() -> dict:
    """Where Claude puts the changes of a batch: {"mode": "pr"} (default: a new branch and pull request from
    the default branch per batch), {"mode": "main"} (commit to the default branch and push) or
    {"mode": "branch", "target": "<branch or #PR>"} (every batch onto that branch and its pull request).
    Set with `screenfeed deliver` or the widget's Deliver picker; stored as "pr", "main" or "branch:<target>"."""
    try:
        value = str(json.loads(CONFIG.read_text()).get("deliver") or "")
    except (OSError, ValueError):
        value = ""
    value = value or os.environ.get("SCREENFEED_DELIVER", "pr")
    if value.startswith("branch:") and value[len("branch:"):].strip():
        return {"mode": "branch", "target": value[len("branch:"):].strip()}
    return {"mode": value if value in ("pr", "main") else "pr"}


def deliver_label(d: dict) -> str:
    return {"pr": "New PR per batch", "main": "Commit to main"}.get(d["mode"]) or f"Branch / PR: {d.get('target')}"


def config_value(key: str, default=None):
    try:
        return json.loads(CONFIG.read_text()).get(key, default)
    except (OSError, ValueError):
        return default


def overlay_note_shown() -> bool:
    """Whether the recorded display shows its note ("Ready …", "● REC …", "Not recorded" on the others).
    The coloured frame stays either way. Toggled with the eye in the widget or `screenfeed note on|off`."""
    return bool(config_value("overlay_note", True))


def remember_workdir() -> None:
    """Remember the git repository Claude runs screenfeed from, for the Deliver picker's suggestions."""
    try:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True,
                             timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        top = ""
    if top and top != config_value("workdir"):
        set_config("workdir", top)
        log("workdir", path=top)


SUGGESTIONS: dict = {"repo": None, "at": 0.0, "items": [], "busy": False}


def branch_suggestions(repo: str) -> list[tuple[str, str]]:
    """(value, label) pairs for the Deliver dialog: the repository's open pull requests (newest first),
    then local branches by last commit, without the default branch and branches that have a pull request."""
    def run(cmd: list[str]) -> str:
        try:
            r = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, timeout=8)
            return r.stdout if r.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            return ""

    items, heads = [], set()
    try:
        prs = json.loads(run(["gh", "pr", "list", "--limit", "15", "--json", "number,title,headRefName"]) or "[]")
    except ValueError:
        prs = []
    for pr in prs:
        heads.add(pr["headRefName"])
        items.append((f"#{pr['number']}", f"#{pr['number']}  {pr['title']}  ({pr['headRefName']})"))
    default = run(["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"]).strip().removeprefix("origin/")
    for b in run(["git", "for-each-ref", "--sort=-committerdate", "--count=15", "--format=%(refname:short)",
                  "refs/heads"]).split():
        if b not in heads and b not in (default, "main", "master"):
            items.append((b, b))
    return items


def refresh_suggestions(wait: bool = False) -> None:
    """Fetch the suggestions for the remembered repository in the background (at most once a minute)."""
    repo = config_value("workdir")
    if not repo or SUGGESTIONS["busy"]:
        return
    if SUGGESTIONS["repo"] == repo and now() - SUGGESTIONS["at"] < 60:
        return
    SUGGESTIONS["busy"] = True

    def work():
        try:
            items = branch_suggestions(repo)
            SUGGESTIONS.update(repo=repo, at=now(), items=items)
        finally:
            SUGGESTIONS["busy"] = False

    t = threading.Thread(target=work, daemon=True)
    t.start()
    if wait:
        t.join(10)


def set_config(key: str, value) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    try:
        cfg = json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        cfg = {}
    cfg[key] = value
    CONFIG.write_text(json.dumps(cfg))


def run_whisper(wav: Path, out: Path, lang: str) -> dict:
    subprocess.run([WHISPER, "-m", MODEL, "-l", lang, "-oj", "-of", str(out), "-np", "-ml", "90",
                    "-sow", "-sns", "-f", str(wav)], capture_output=True, check=True, timeout=900)
    return json.loads(out.with_suffix(".json").read_text(errors="replace"))


def transcribe(seg: Clip, a: float, b: float, workdir: Path, i: int) -> tuple[list, str | None]:
    if seg.audio == "none":
        return [], None
    wav = workdir / f"audio-{i}.wav"
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{a:.3f}",
                    "-t", f"{b - a:.3f}", "-i", str(seg.path), "-vn", "-ar", "16000", "-ac", "1",
                    str(wav)], check=True)
    vol = max_volume(wav)
    log("transcribe", seg=seg.path.name, start=a, end=b, max_volume_db=vol)
    if vol < -42:
        log("transcribe skipped: too quiet", seg=seg.path.name)
        return [], None
    global _last_language
    out = workdir / f"speech-{i}"
    allowed = languages()
    data = run_whisper(wav, out, allowed[0] if len(allowed) == 1 else "auto")
    detected = data.get("result", {}).get("language")
    if len(allowed) > 1 and detected not in allowed:
        # whisper heard another language: on near-silence it invents Russian or Japanese subtitle credits
        fallback = _last_language if _last_language in allowed else allowed[0]
        log("transcribe: language not allowed, again", detected=detected, allowed=",".join(allowed), using=fallback)
        data = run_whisper(wav, out, fallback)
        data.setdefault("result", {})["language"] = fallback
    _last_language = data.get("result", {}).get("language") or _last_language
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


def seg_for(segs: list, t: float) -> Clip:
    for s in segs:
        if s.clip_from - 0.05 <= t <= (s.clip_to or 0) + 0.05:
            return s
    return min(segs, key=lambda s: min(abs(t - s.clip_from), abs(t - (s.clip_to or s.clip_from))))


def render_frame(seg: Clip, t: float, x: float, y: float, base: Path) -> dict:
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


def spread(items: list, n: int) -> list:
    """At most n of items, evenly spread over the list (all of them when they fit)."""
    if n <= 0:
        return []
    if len(items) <= n:
        return list(items)
    step = len(items) / n
    return [items[int(i * step)] for i in range(n)]


def select_moments(moments: list[dict], limit: int) -> list[dict]:
    """The moments a batch shows, at most limit, in time order. What was said comes first: one moment for every
    sentence, then the further moments of sentences (a second click, another resting point), and only then clicks
    without speech, which are navigation context. Each group is spread evenly when it does not fit."""
    by_time = sorted(moments, key=lambda m: m["t"])
    first, more, seen = [], [], set()
    for m in by_time:
        if m["speech"] is None:
            continue
        (more if m["speech"] in seen else first).append(m)
        seen.add(m["speech"])
    silent = [m for m in by_time if m["speech"] is None]
    chosen = spread(first, limit)
    chosen += spread(more, limit - len(chosen))
    chosen += spread(silent, limit - len(chosen))
    return sorted(chosen, key=lambda m: m["t"])


def process_batch(job: dict, d: Path) -> dict:
    no, t0, t1 = job["no"], job["t0"], job["t1"]
    samples, apps = job["samples"], job["apps"]
    frames = d / "frames"
    frames.mkdir(parents=True, exist_ok=True)
    work = d / "work"
    work.mkdir(exist_ok=True)

    # 0. the recorded video: one clip per recorder, joined from its chunks; and what is missing
    segs = [c for i, part in enumerate(job["parts"]) if (c := join_chunks(part, t0, t1, work, i))]
    gaps = sorted(g for part in job["parts"] for g in gaps_of(part))
    if gaps:
        log("recording gaps", batch=no, gaps=" ".join(f"{a - t0:.1f}-{b - t0:.1f}s" for a, b in gaps))
    if not segs:
        if not job["manual"]:
            shutil.rmtree(d, ignore_errors=True)
            emit({"batch": no, "skipped": "nothing recorded", "t": now()})
            return {"batch": no, "skipped": True, "moments": 0}
        raise RuntimeError("no recorded video for this batch (recorder produced no chunks)")

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
    log("transcript", batch=no, sentences=len(speech), lang=lang,
        segs=" ".join(f"{s.path.name}:{s.duration:.1f}s" for s in segs))

    if not speech and not job["manual"]:
        shutil.rmtree(d, ignore_errors=True)
        emit({"batch": no, "skipped": "no speech", "t": now()})
        return {"batch": no, "skipped": True, "moments": 0}

    # 2. pointing moments
    def in_range(s):
        return t0 <= s[0] <= t1

    clicks = []
    for i, s in enumerate(samples):
        if not (s[3] and (i == 0 or not samples[i - 1][3]) and not s[4] and in_range(s)):
            continue
        # A double or triple click is one action: one moment, not three copies of the same picture.
        if clicks and s[0] - clicks[-1][0] < 1.0 and math.dist((s[1], s[2]), (clicks[-1][1], clicks[-1][2])) < 30:
            continue
        clicks.append(s)
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
    end = cursor_at(samples, t1 - END_MARGIN)
    end_t = max(t0, t1 - END_MARGIN)

    found = len(moments)
    moments = select_moments(moments, MAX_MOMENTS)
    if found > len(moments):
        log("moments capped", batch=no, found=found, kept=len(moments))
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
        + (", paused in between; timestamps are wall-clock since start)"
           if t1 - t0 > rec_s + sum(b - a for a, b in gaps) + 3 else ")"),
        f"- Screen: {', '.join(dict.fromkeys(s.screen['label'] for s in segs))}, "
        f"spoken language: {lang or 'n/a'}",
        "- Images: `full` = whole screen, `zoom` = crop around the cursor. The red ring marks the "
        "mouse cursor at the moment the sentence was spoken (where the user was pointing). "
        "Coordinates are points from the top-left of the screen. A red frame or REC badge at the "
        "screen edges, if visible, is the recording indicator; ignore it.",
    ]
    if gaps:
        out.append("- Recording gaps (the recorder failed and was restarted; anything said then is missing): "
                   + ", ".join(f"{fmt_dur(a - t0)}–{fmt_dur(b - t0)}" for a, b in gaps))
    deliver = job.get("deliver") or deliver_mode()
    out.append(f"- Deliver: {deliver_label(deliver)} (the mode when this batch was sent; `deliver` in the event)")
    if job.get("recovered"):
        out.append("- Recovered: recorded in an earlier screenfeed run that ended before this was sent "
                   "(crash or quit). Cursor data was not kept, so there are no pointing frames.")
    out += ["", "## Timeline", ""]
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
    if KEEP_VIDEO:
        for c in segs:
            shutil.move(str(c.path), str(d / c.path.name))
    shutil.rmtree(work, ignore_errors=True)
    if not KEEP_VIDEO:
        shutil.rmtree(d / "raw", ignore_errors=True)  # kept after an earlier failure; processed now

    preview = " ".join(s[2] for s in speech)
    event = {"batch": no, "dir": str(d), "timeline": str(timeline), "seconds": int(rec_s),
             "moments": len(rendered), "speech": preview[:200], "gaps": len(gaps), "deliver": deliver, "t": now()}
    emit(event)
    return event


# --------------------------------------------------------------------------------------------
# Widget (AppKit) + local HTTP control API
# --------------------------------------------------------------------------------------------


def run_app() -> None:
    import AppKit as K
    import objc
    import Quartz
    from PyObjCTools import AppHelper

    HOME.mkdir(parents=True, exist_ok=True)
    threading.excepthook = _log_uncaught
    leftovers = sorted(p.name for p in RAW.glob("*")) if RAW.exists() else []
    log("app starting", pid=os.getpid(), leftover_raw=",".join(leftovers) or "none")
    threading.Thread(target=ensure_sck, daemon=True, name="sfcapture-build").start()
    ctl = Controller()  # recovers what an earlier run left in RAW; never wipe it here

    def screen_info(s) -> dict:
        did = int(s.deviceDescription()["NSScreenNumber"])
        _, ids, n = Quartz.CGGetActiveDisplayList(16, None, None)
        ids = list(ids[:n])
        f = s.frame()
        name = str(s.localizedName())
        return {"id": did, "index": ids.index(did) if did in ids else 0,
                "frame": (f.origin.x, f.origin.y, f.size.width, f.size.height),
                "scale": float(s.backingScaleFactor()),
                "name": name, "label": name, "size": f"{int(f.size.width)}×{int(f.size.height)}"}

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
        """mode: "rec" (being recorded, red pulsing), "ready" / "flash" (green: recorded when you
        press Rec; "flash" = thicker, right after picking it), "paused" (green), "off" (other screen)."""
        recorded = mode != "off"
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
            green = K.NSColor.colorWithSRGBRed_green_blue_alpha_(0.13, 0.80, 0.33, 1.0)
            dark_green = K.NSColor.colorWithSRGBRed_green_blue_alpha_(0.05, 0.50, 0.20, 0.95)
            layer.setBorderWidth_({"rec": 10, "flash": 10}.get(mode, 6))
            layer.setBorderColor_((red if mode == "rec" else green).CGColor())
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
            elif mode == "paused":
                text, bg, size = "⏸  Paused  ·  Resume records this screen", dark_green, 12
            else:
                text, bg, size = ("Ready  ·  press Rec to record this screen", dark_green,
                                  15 if mode == "flash" else 12)
            root.addSubview_(border)
        else:
            text, bg, size = "Not recorded", K.NSColor.colorWithWhite_alpha_(0.25, 0.85), 12
        if not overlay_note_shown():  # the frame alone; "off" screens get no window at all
            w.setContentView_(root)
            w.orderFrontRegardless()
            return w
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
        flashing = now() < ctl.flash_until
        if ctl.recording and ctl.rec:
            target, mode = ctl.rec.screen, "rec"
        elif ctl.screen:  # always show where Rec will record while the app runs
            target = ctl.screen
            mode = "flash" if flashing else ("paused" if ctl.paused_with_unsent() else "ready")
        else:
            target, mode = None, None
        screens = list(K.NSScreen.screens())

        def fr(s):
            f = s.frame()
            return (f.origin.x, f.origin.y, f.size.width, f.size.height)

        note = overlay_note_shown()
        key = (tuple(fr(s) for s in screens), tuple(target["frame"]), mode, note) if target else None
        if key == overlays["key"]:
            return
        for w in overlays["wins"]:
            w.orderOut_(None)
        overlays["wins"], overlays["key"] = [], key
        if key:
            others = note and mode in ("rec", "flash")  # "Not recorded" badges only when it matters
            overlays["wins"] = [make_overlay(s, mode) if fr(s) == tuple(target["frame"])
                                else make_overlay(s, "off") for s in screens
                                if others or fr(s) == tuple(target["frame"])]
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

    def symbol(name: str, size: float = 15, weight: float = 0.0, color=None):
        img = K.NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, None)
        if img is None:
            return None
        cfg = K.NSImageSymbolConfiguration.configurationWithPointSize_weight_(size, weight)
        if color is not None:  # fixed colour, independent of the button's tint
            cfg = cfg.configurationByApplyingConfiguration_(
                K.NSImageSymbolConfiguration.configurationWithPaletteColors_([color]))
        return img.imageWithSymbolConfiguration_(cfg)

    def style_button(b, title, sym, bg, fg, enabled):
        """Colour a self-drawn button; only touches it when its state changes."""
        key = (title, sym, enabled)
        if getattr(b, "sf_key", None) == key:
            return
        b.sf_key = key
        b.sf_enabled = enabled  # not setEnabled_: AppKit would fade the icon a second time
        b.layer().setBackgroundColor_(bg.CGColor())
        para = K.NSMutableParagraphStyle.alloc().init()
        para.setAlignment_(K.NSTextAlignmentCenter)
        b.setAttributedTitle_(K.NSAttributedString.alloc().initWithString_attributes_(
            "  " + title, {K.NSForegroundColorAttributeName: fg,
                           K.NSFontAttributeName: K.NSFont.systemFontOfSize_weight_(13, 0.23),
                           K.NSParagraphStyleAttributeName: para}))
        b.setImage_(symbol(sym, 11, color=fg))

    AUTO_TITLES = {0: "Auto-send off", 30: "Every 30 s", 60: "Every minute", 120: "Every 2 min",
                   300: "Every 5 min"}

    class ScreenfeedUI(K.NSObject):
        def build(self):
            W, H = 340, 314
            self.W, self.H = W, H
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
            # rounded shape via maskImage: clips the blur AND gives the window a rounded shadow
            radius = 16.0

            def draw_mask(rect):
                K.NSColor.blackColor().set()
                K.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(rect, radius, radius).fill()
                return True

            mask = K.NSImage.imageWithSize_flipped_drawingHandler_(
                K.NSMakeSize(radius * 2 + 1, radius * 2 + 1), False, draw_mask)
            mask.setCapInsets_(K.NSEdgeInsetsMake(radius, radius, radius, radius))
            mask.setResizingMode_(K.NSImageResizingModeStretch)
            v.setMaskImage_(mask)
            p.setContentView_(v)
            # thin edge like native HUD windows; turns red while recording
            edge = K.NSView.alloc().initWithFrame_(K.NSMakeRect(0, 0, W, H))
            edge.setWantsLayer_(True)
            edge.layer().setCornerRadius_(radius)
            edge.layer().setBorderWidth_(0.5)
            edge.layer().setBorderColor_(K.NSColor.colorWithWhite_alpha_(1.0, 0.18).CGColor())
            v.addSubview_(edge)
            self.edge = edge
            self.panel, self.view = p, v

            def R(x, top, w, h):  # layout from the top-left, like a design tool
                return K.NSMakeRect(x, H - top - h, w, h)

            def label(rect, size, color, weight=0.0, mono=False):
                t = K.NSTextField.labelWithString_("")
                t.setFrame_(rect)
                t.setFont_(K.NSFont.monospacedDigitSystemFontOfSize_weight_(size, weight) if mono
                           else K.NSFont.systemFontOfSize_weight_(size, weight))
                t.setTextColor_(color)
                t.setLineBreakMode_(K.NSLineBreakByTruncatingTail)
                v.addSubview_(t)
                return t

            def icon_button(rect, sym, size, tint, action, tip):
                b = ScreenfeedButton.alloc().initWithFrame_(rect)
                b.setBordered_(False)
                b.setImage_(symbol(sym, size))
                b.setImagePosition_(K.NSImageOnly)
                b.setContentTintColor_(tint)
                b.setTarget_(self)
                b.setAction_(action)
                b.setToolTip_(tip)
                v.addSubview_(b)
                return b

            def icon(rect, sym, size, tint):
                iv = K.NSImageView.alloc().initWithFrame_(rect)
                iv.setImage_(symbol(sym, size))
                iv.setContentTintColor_(tint)
                v.addSubview_(iv)
                return iv

            def separator(top):
                b = K.NSBox.alloc().initWithFrame_(R(12, top, W - 24, 1))
                b.setBoxType_(K.NSBoxSeparator)
                v.addSubview_(b)

            secondary = K.NSColor.secondaryLabelColor()
            tertiary = K.NSColor.tertiaryLabelColor()

            # header: timer + state, then Record / Send / discard as real buttons
            self.timer = label(R(12, 10, 86, 32), 26, tertiary, weight=-0.2, mono=True)
            self.sub = label(R(100, 20, W - 112, 16), 12, secondary)
            def capsule(rect, action, tip):
                # self-drawn button: keeps its colours although the panel is never the key window
                b = ScreenfeedButton.alloc().initWithFrame_(rect)
                b.setBordered_(False)
                b.setWantsLayer_(True)
                b.layer().setCornerRadius_(8)
                b.setImagePosition_(K.NSImageLeading)
                b.setImageHugsTitle_(True)
                b.setTarget_(self)
                b.setAction_(action)
                b.setToolTip_(tip)
                v.addSubview_(b)
                return b

            self.rec = capsule(R(10, 48, 104, 30), "onRec:", "Start recording")
            self.send = capsule(R(120, 48, 100, 30), "onSend:", "Send what was recorded to Claude")
            self.drop = capsule(R(226, 48, W - 10 - 226, 30), "onDiscard:",
                                "Throw away what was recorded but not sent yet (it never reaches Claude)")

            # screen + auto-send pickers
            icon(R(12, 92, 18, 18), "display", 13, secondary)
            self.pick = K.NSPopUpButton.alloc().initWithFrame_pullsDown_(R(30, 88, 172, 26), False)
            self.pick.setBordered_(False)
            self.pick.setFont_(K.NSFont.systemFontOfSize_(12))
            self.pick.setTarget_(self)
            self.pick.setAction_("onScreen:")
            self.pick.setToolTip_("Which display is recorded")
            v.addSubview_(self.pick)
            self.pick_labels = []
            icon(R(208, 92, 18, 18), "timer", 13, secondary)
            self.autopick = K.NSPopUpButton.alloc().initWithFrame_pullsDown_(R(226, 88, W - 12 - 226, 26), False)
            self.autopick.setBordered_(False)
            self.autopick.setFont_(K.NSFont.systemFontOfSize_(12))
            self.autopick.setTarget_(self)
            self.autopick.setAction_("onAuto:")
            self.autopick.setToolTip_("Send automatically (batches without speech are skipped)")
            v.addSubview_(self.autopick)
            self.auto_steps = []

            # where Claude puts the changes of a batch
            icon(R(12, 120, 18, 18), "arrow.triangle.branch", 13, secondary)
            self.deliverpick = K.NSPopUpButton.alloc().initWithFrame_pullsDown_(R(30, 116, W - 70, 26), False)
            self.deliverpick.setBordered_(False)
            self.deliverpick.setFont_(K.NSFont.systemFontOfSize_(12))
            self.deliverpick.setTarget_(self)
            self.deliverpick.setAction_("onDeliver:")
            self.deliverpick.setToolTip_("Where Claude puts the changes of each batch")
            v.addSubview_(self.deliverpick)
            self.deliver_items = []
            # show / hide the note on the recorded screen ("Ready …", "● REC …"); the frame stays
            self.notebtn = icon_button(R(W - 12 - 24, 119, 24, 20), "eye", 13, secondary, "onNote:",
                                       "Show or hide the note on the recorded screen (the frame stays)")
            self.note_shown = None

            separator(150)
            self.stat = label(R(12, 158, W - 24, 15), 11, secondary)
            self.claude_icon = icon(R(11, 176, 16, 15), "sparkles", 11, K.NSColor.systemOrangeColor())
            self.msg = label(R(30, 176, W - 42, 15), 11, K.NSColor.labelColor())

            # permission banner: over the status and Claude lines while something is missing
            self.banner = K.NSView.alloc().initWithFrame_(R(10, 154, W - 20, 40))
            self.banner.setWantsLayer_(True)
            self.banner.layer().setCornerRadius_(8)
            self.banner.layer().setBackgroundColor_(K.NSColor.colorWithRed_green_blue_alpha_(0.45, 0.33, 0.05, 0.95).CGColor())
            self.banner.setHidden_(True)
            bw = W - 20
            warn = K.NSImageView.alloc().initWithFrame_(K.NSMakeRect(8, 12, 16, 16))
            warn.setImage_(symbol("exclamationmark.triangle.fill", 12))
            warn.setContentTintColor_(K.NSColor.systemYellowColor())
            self.banner.addSubview_(warn)
            self.banner_text = K.NSTextField.labelWithString_("")
            self.banner_text.setFrame_(K.NSMakeRect(30, 4, bw - 30 - 84, 32))
            self.banner_text.setFont_(K.NSFont.systemFontOfSize_weight_(11, 0.2))
            self.banner_text.setTextColor_(K.NSColor.whiteColor())
            self.banner_text.setMaximumNumberOfLines_(2)
            self.banner_text.setLineBreakMode_(K.NSLineBreakByWordWrapping)
            self.banner.addSubview_(self.banner_text)
            self.banner_btn = K.NSButton.alloc().initWithFrame_(K.NSMakeRect(bw - 80, 8, 72, 24))
            self.banner_btn.setTitle_("Settings")
            self.banner_btn.setBezelStyle_(K.NSBezelStyleRounded)
            self.banner_btn.setControlSize_(K.NSControlSizeSmall)
            self.banner_btn.setTarget_(self)
            self.banner_btn.setAction_("onPermissions:")
            self.banner.addSubview_(self.banner_btn)
            v.addSubview_(self.banner)
            self.permissions_checked = 0.0

            separator(200)
            hdr = label(R(12, 208, 200, 13), 10, tertiary, weight=0.3)
            hdr.setStringValue_("SENT TO CLAUDE")
            self.hist_icons, self.hist = [], []
            for i in range(4):
                top = 225 + 17 * i
                self.hist_icons.append(icon(R(11, top, 15, 15), "circle", 11, tertiary))
                self.hist.append(label(R(30, top, W - 42, 15), 11, secondary, mono=True))

            # footer: build + quit
            self.build_lbl = label(R(12, H - 22, W - 60, 13), 9.5, tertiary, mono=True)
            self.build_lbl.setStringValue_(BUILD)
            self.build_lbl.setToolTip_("When the running version was built (file time · git commit,"
                                       " + = uncommitted changes)")
            self.quit = icon_button(R(W - 12 - 20, H - 25, 20, 18), "power", 12, tertiary, "onQuit:",
                                    "Quit screenfeed")
            self.quit_armed = 0.0
            self.n = 0
            self.acting = None  # (button, title) while a clicked action runs: shown at once
            self.hist_cache: dict = {}  # what each history row shows, to redraw only on change
            self.tick_error_logged = 0.0
            self.fast_until = 0.0  # refresh at full rate until then (right after a click)
            self.slow_logged = 0.0
            p.orderFrontRegardless()
            p.invalidateShadow()
            return self

        # -- actions ---------------------------------------------------------------------------
        def onRec_(self, sender):
            log("ui click: record/pause", enabled=getattr(sender, "sf_enabled", True))
            if not getattr(sender, "sf_enabled", True):
                return
            self.act(self.rec, "Pausing…" if ctl.recording else "Starting…", ctl.toggle)

        def onSend_(self, sender):
            log("ui click: send", enabled=getattr(sender, "sf_enabled", True))
            if not getattr(sender, "sf_enabled", True):
                return
            self.act(self.send, "Sending…", lambda: ctl.send(manual=True))

        def onAuto_(self, sender):
            i = int(sender.indexOfSelectedItem())
            if 0 <= i < len(self.auto_steps):
                ctl.set_auto(self.auto_steps[i])

        def onScreen_(self, sender):
            pos = int(sender.indexOfSelectedItem())  # 0 = "No screen"
            threading.Thread(target=ctl.select_screen, args=(pos or "none",), daemon=True).start()

        def onQuit_(self, sender):
            log("ui click: quit", unsent=ctl.has_unsent())
            # with unsent content, ask for a second click within 3 s
            if ctl.has_unsent() and now() > self.quit_armed:
                self.quit_armed = now() + 3
                self.quit.setContentTintColor_(K.NSColor.systemRedColor())
                ctl.status = "Click ⏻ again to quit · unsent recording is sent on next start"
                return
            AppHelper.callAfter(shutdown)

        def onDeliver_(self, sender):
            i = int(sender.indexOfSelectedItem())
            if not (0 <= i < len(self.deliver_items)):
                return
            choice = self.deliver_items[i]
            if choice == "ask":
                target = self.askBranch()
                if not target:
                    self.deliver_key = None  # back to the current mode in the picker
                    return
                choice = f"branch:{target}"
            set_config("deliver", choice)
            log("ui deliver", mode=choice)
            self.deliver_key = None

        def onNote_(self, sender):
            shown = not overlay_note_shown()
            set_config("overlay_note", shown)
            log("ui note", shown=shown)

        @objc.python_method
        def syncNote(self):
            shown = overlay_note_shown()
            if shown == self.note_shown:
                return
            self.note_shown = shown
            self.notebtn.setImage_(symbol("eye" if shown else "eye.slash", 13))
            self.notebtn.setToolTip_("Hide the note on the recorded screen (the frame stays)" if shown
                                     else "Show the note on the recorded screen")

        @objc.python_method
        def askBranch(self) -> str | None:
            """A small dialog for the branch name or pull request number (#123), with the open pull
            requests and recent branches of the repository Claude runs in as suggestions."""
            refresh_suggestions()
            deadline = now() + 8
            while SUGGESTIONS["busy"] and now() < deadline:  # first fetch: wait briefly, it is a modal dialog anyway
                time.sleep(0.1)
            repo = config_value("workdir")
            items = SUGGESTIONS["items"] if SUGGESTIONS["repo"] == repo else []
            K.NSApp.activateIgnoringOtherApps_(True)
            alert = K.NSAlert.alloc().init()
            alert.setMessageText_("Deliver to a branch or pull request")
            alert.setInformativeText_(
                "Every batch goes onto this branch, in the repository of the app you show. "
                "A pull request number like #123 means its branch."
                + (f"\n\nSuggestions from {os.path.basename(repo)}: open pull requests, then recent branches."
                   if items else ""))
            field = K.NSComboBox.alloc().initWithFrame_(K.NSMakeRect(0, 0, 420, 26))
            field.setPlaceholderString_("feature/my-branch or #123")
            field.setCompletes_(True)
            field.setNumberOfVisibleItems_(12)
            labels = [label for _, label in items]
            if labels:
                field.addItemsWithObjectValues_(labels)
            current = deliver_mode()
            if current.get("target"):
                field.setStringValue_(next((l for v, l in items if v == current["target"]), current["target"]))
            alert.setAccessoryView_(field)
            alert.addButtonWithTitle_("Use")
            alert.addButtonWithTitle_("Cancel")
            alert.window().setInitialFirstResponder_(field)
            if alert.runModal() != K.NSAlertFirstButtonReturn:
                return None
            text = str(field.stringValue()).strip()
            value = next((v for v, l in items if l == text), None)  # a picked suggestion
            if value is None:
                m = re.match(r"#(\d+)\b", text)  # "#123 …" typed or edited by hand
                value = f"#{m.group(1)}" if m else text
            return value or None

        @objc.python_method
        def syncDeliver(self):
            d = deliver_mode()
            key = json.dumps(d, sort_keys=True)
            if getattr(self, "deliver_key", None) == key:
                return
            self.deliver_key = key
            items = [("pr", "New PR per batch (from main)"), ("main", "Commit to main")]
            if d["mode"] == "branch":
                items.append((f"branch:{d['target']}", f"Branch / PR: {d['target']}"))
            items.append(("ask", "Branch / PR…"))
            self.deliver_items = [k for k, _ in items]
            self.deliverpick.removeAllItems()
            self.deliverpick.addItemsWithTitles_([t for _, t in items])
            current = "branch:" + d["target"] if d["mode"] == "branch" else d["mode"]
            self.deliverpick.selectItemAtIndex_(self.deliver_items.index(current))

        def onPermissions_(self, sender):
            problems = ctl.permission_problems
            kind = problems[0]["kind"] if problems else "screen"
            log("ui click: permission settings", kind=kind)
            subprocess.Popen(["open", SETTINGS_PANES.get(kind, SETTINGS_PANES["screen"])])

        def onDiscard_(self, sender):
            log("ui click: discard", enabled=getattr(sender, "sf_enabled", True))
            if not getattr(sender, "sf_enabled", True):
                return
            self.act(self.drop, "Discarding…", ctl.discard)

        @objc.python_method  # a plain Python helper, not an Objective-C selector
        def act(self, button, title, fn):
            """Run a clicked action off the main thread, but answer the click at once: the button shows
            `title` until the action is done, and the widget refreshes at full rate meanwhile."""
            self.acting = (button, title)
            self.fast_until = now() + 1.5
            style_button(button, title, "hourglass", K.NSColor.colorWithWhite_alpha_(0.36, 0.95),
                         K.NSColor.whiteColor(), False)
            clicked = now()

            def run():
                try:
                    fn()
                finally:
                    log("ui action done", action=title, took=now() - clicked)
                    self.acting = None
                    self.fast_until = now() + 0.5

            threading.Thread(target=run, daemon=True, name=f"ui:{title}").start()

        # -- refresh ---------------------------------------------------------------------------

        def syncPicker(self):
            labels = ["No screen"] + [s["label"] for s in ctl.screens]
            if labels != self.pick_labels:
                self.pick.removeAllItems()
                self.pick.addItemsWithTitles_(labels)
                self.pick_labels = labels
            i = labels.index(ctl.screen["label"]) if ctl.screen and ctl.screen["label"] in labels else 0
            if self.pick.indexOfSelectedItem() != i:
                self.pick.selectItemAtIndex_(i)
            steps = AUTO_STEPS + ([ctl.auto] if ctl.auto not in AUTO_STEPS else [])
            if steps != self.auto_steps:
                self.autopick.removeAllItems()
                self.autopick.addItemsWithTitles_(
                    [AUTO_TITLES.get(s, f"Every {fmt_dur(s)}") for s in steps])
                self.auto_steps = steps
            j = steps.index(ctl.auto)
            if self.autopick.indexOfSelectedItem() != j:
                self.autopick.selectItemAtIndex_(j)

        def tick_(self, timer):
            """The widget's heartbeat (30 Hz). An exception escaping an NSTimer callback stops the timer for
            good, without a word: the widget would freeze while clicks still start and stop recording."""
            ctl.ui_tick_at = now()
            try:
                self.refresh()
            except Exception as e:  # noqa: BLE001
                if now() - self.tick_error_logged > 10:
                    self.tick_error_logged = now()
                    log_exc("ui refresh failed (widget keeps running)", e)

        @objc.python_method
        def refresh(self):
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
            if self.n % 6 and t > self.fast_until:  # 5 Hz, but 30 Hz right after a click
                return
            if t - self.permissions_checked > 5:
                self.permissions_checked = t
                problems = permission_problems()
                if [p["text"] for p in problems] != [p["text"] for p in ctl.permission_problems]:
                    log("permissions", problems=" | ".join(p["text"] for p in problems) or "none")
                ctl.permission_problems = problems
                self.banner.setHidden_(not problems)
                if problems:
                    self.banner_text.setStringValue_(" · ".join(p["text"] for p in problems))
            rec = ctl.recording
            paused = ctl.paused_with_unsent()
            sync_overlays(self.panel)
            self.syncPicker()
            self.syncDeliver()
            self.syncNote()
            if self.n % 900 == 0:  # every 30 s; fetches at most once a minute, in the background
                refresh_suggestions()
            if getattr(self, "edge_rec", None) != rec:
                self.edge_rec = rec
                self.edge.layer().setBorderWidth_(2 if rec else 0.5)
                self.edge.layer().setBorderColor_(
                    (K.NSColor.systemRedColor() if rec else K.NSColor.colorWithWhite_alpha_(1.0, 0.18)).CGColor())
            if self.quit_armed and t > self.quit_armed:
                self.quit_armed = 0.0
                self.quit.setContentTintColor_(K.NSColor.tertiaryLabelColor())

            # record / pause / resume, send, discard: colours carry the state
            red = K.NSColor.systemRedColor()
            blue = K.NSColor.systemBlueColor()
            white = K.NSColor.whiteColor()
            # solid backgrounds: contrast stays the same whatever is behind the translucent panel
            dim_bg = K.NSColor.colorWithWhite_alpha_(0.22, 0.92)
            dim_fg = K.NSColor.colorWithWhite_alpha_(0.70, 1.0)
            neutral_bg = K.NSColor.colorWithWhite_alpha_(0.36, 0.95)
            can_rec = bool(rec or ctl.screen)
            if rec:
                style_button(self.rec, "Pause", "pause.fill", neutral_bg, white, True)
                self.rec.setToolTip_("Pause (unsent recording is kept)")
            else:
                style_button(self.rec, "Resume" if paused else "Record", "circle.fill",
                                   red if can_rec else dim_bg, white if can_rec else dim_fg, can_rec)
                self.rec.setToolTip_("Resume recording" if paused else "Start recording")
            can_send = ctl.has_unsent() and not ctl.sending
            style_button(self.send, "Sending…" if ctl.sending else "Send", "paperplane.fill",
                               blue if can_send else dim_bg, white if can_send else dim_fg, can_send)
            style_button(self.drop, "Discard", "trash", neutral_bg if can_send else dim_bg,
                               white if can_send else dim_fg, can_send)
            acting = self.acting
            if acting:  # a clicked action still runs: keep showing that it is under way
                style_button(acting[0], acting[1], "hourglass", neutral_bg, white, False)

            # timer = recorded time not sent yet (frozen while paused)
            self.timer.setStringValue_(fmt_dur(ctl.unsent_seconds()))
            self.timer.setTextColor_(K.NSColor.systemRedColor() if rec else
                                     K.NSColor.systemOrangeColor() if paused else
                                     K.NSColor.tertiaryLabelColor())
            if ctl.sending:
                sub = "Sending…"
            elif rec:
                sub = "Recording" + (f" · sends in {fmt_dur(ctl.next_auto - t)}"
                                     if ctl.auto and ctl.next_auto else "")
            elif paused:
                sub = "Paused · not sent yet"
            elif not ctl.screen:
                sub = "Pick a screen to record"
            else:
                sub = "Ready to record"
            self.sub.setStringValue_(sub)

            busy = ctl.busy + ctl.jobs.qsize()
            quiet = ctl.status in ("Ready",) or ctl.status.startswith(("Will record", "Recording "))
            self.stat.setStringValue_(("" if quiet else ctl.status)
                                      + (f" ({busy} processing)" if busy > 1 else ""))
            self.msg.setStringValue_(f"{fmt_ago(ctl.claude_msg_at)} · {ctl.claude_msg}"
                                     if ctl.claude_msg and ctl.claude_msg_at else
                                     ctl.claude_msg or "Waiting for your first batch")
            self.msg.setTextColor_(K.NSColor.labelColor() if ctl.claude_msg else K.NSColor.tertiaryLabelColor())
            self.msg.setToolTip_(ctl.claude_msg)

            hist = list(ctl.history)[:4]
            for i, (ic, lab) in enumerate(zip(self.hist_icons, self.hist)):
                if i >= len(hist):
                    ic.setHidden_(True)
                    self.hist_cache.pop((i, "text"), None)
                    lab.setStringValue_("Nothing sent yet" if i == 0 else "")
                    lab.setFrameOrigin_(K.NSMakePoint(12 if i == 0 else 30, lab.frame().origin.y))
                    continue
                h = hist[i]
                st = h["state"]
                if st.startswith("→"):
                    sym, tint, what = "checkmark.circle.fill", K.NSColor.systemGreenColor(), st[2:].replace("Claude · ", "")
                elif st.startswith("processing"):
                    sym, tint, what = "clock", K.NSColor.secondaryLabelColor(), "processing…"
                elif st.startswith("skipped"):
                    sym, tint, what = "minus.circle", K.NSColor.tertiaryLabelColor(), "skipped, nothing said"
                else:
                    sym, tint, what = "exclamationmark.triangle.fill", K.NSColor.systemYellowColor(), "failed"
                ic.setHidden_(False)
                lab.setFrameOrigin_(K.NSMakePoint(30, lab.frame().origin.y))
                # caches live on self: AppKit's own views do not take Python attributes
                if self.hist_cache.get((i, "sym")) != sym:  # building symbol images on every refresh is slow
                    self.hist_cache[(i, "sym")] = sym
                    ic.setImage_(symbol(sym, 11))
                    ic.setContentTintColor_(tint)
                # columns on tab stops: when (right-aligned), batch number, length, outcome
                if not hasattr(self, "hist_style"):
                    self.hist_style = K.NSMutableParagraphStyle.alloc().init()
                    self.hist_style.setTabStops_([
                        K.NSTextTab.alloc().initWithTextAlignment_location_options_(align, x, {})
                        for align, x in ((K.NSTextAlignmentRight, 60), (K.NSTextAlignmentRight, 90),
                                         (K.NSTextAlignmentRight, 124), (K.NSTextAlignmentLeft, 134))])
                    self.hist_style.setLineBreakMode_(K.NSLineBreakByTruncatingTail)
                text = (f"\t{fmt_ago(h['sent_at'])}\t#{h['batch']}\t{fmt_dur(h['seconds'])}\t{what}"
                        + (" · auto" if h["auto"] else ""))
                if self.hist_cache.get((i, "text")) != text:
                    self.hist_cache[(i, "text")] = text
                    lab.setAttributedStringValue_(K.NSAttributedString.alloc().initWithString_attributes_(text, {
                        K.NSFontAttributeName: lab.font(), K.NSForegroundColorAttributeName: lab.textColor(),
                        K.NSParagraphStyleAttributeName: self.hist_style}))
            took = now() - t
            if took > 0.05 and t - self.slow_logged > 10:  # a slow refresh delays the next click
                self.slow_logged = t
                log("slow ui refresh", ms=round(took * 1000))


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
                    {"n": i, "label": x["label"], "size": x["size"], "id": x["id"], "selected": x["id"] == sel}
                    for i, x in enumerate(ctl.screens, 1)]})
            self.reply({"ok": False, "error": "not found"}, 404)

        def do_POST(self):
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n).decode("utf-8", "replace") if n else ""
            log("api", path=self.path, body=body[:200])
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
                if u.path == "/retry":
                    return self.reply(ctl.retry(int(q.get("batch", ["0"])[0])))
                if u.path == "/auto":
                    return self.reply(ctl.set_auto(int(q.get("seconds", ["0"])[0])))
                if u.path == "/say":
                    ctl.claude_msg = body.strip()
                    ctl.claude_msg_at = now()
                    return self.reply({"ok": True})
                if u.path == "/quit":
                    self.reply({"ok": True})
                    AppHelper.callAfter(shutdown)
                    return
            except Exception as e:  # noqa: BLE001
                log_exc("api request failed", e, path=self.path)
                return self.reply({"ok": False, "error": str(e)}, 500)
            self.reply({"ok": False, "error": "not found"}, 404)

    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        print(f"screenfeed already running on port {PORT}", file=sys.stderr)
        log("app start aborted: port in use", port=PORT)
        sys.exit(1)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    app = K.NSApplication.sharedApplication()
    app.setActivationPolicy_(K.NSApplicationActivationPolicyAccessory)
    ui = ScreenfeedUI.alloc().init().build()
    ctl.update_screens(all_screens())
    start_id = screen_info(ui.panel.screen() or K.NSScreen.mainScreen())["id"]
    try:  # remember the last picked screen across restarts (if still connected)
        st = json.loads((HOME / "state.json").read_text())
        saved = st.get("screen_id")
        if "screen_id" in st and saved is None:
            start_id = None
        elif any(x["id"] == saved for x in ctl.screens):
            start_id = saved
    except (OSError, ValueError):
        pass
    ctl.screen = next((x for x in ctl.screens if x["id"] == start_id), None)
    timer = K.NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(1 / 30, ui, "tick:", None, True)
    K.NSRunLoop.currentRunLoop().addTimer_forMode_(timer, K.NSRunLoopCommonModes)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: shutdown())
    print(f"screenfeed running · http://127.0.0.1:{PORT} · data in {HOME} · log {LOG}", flush=True)
    log("app started", pid=os.getpid(), build=BUILD, screen=ctl.screen and ctl.screen["label"],
        audio=default_audio_device())
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
            break
        time.sleep(0.2)
    else:
        sys.exit(f"screenfeed did not come up, see {HOME / 'app.log'}")
    for _ in range(25):  # the widget refreshes 30 times a second once it is up
        age = ui_tick_age()
        if age is not None and age < 2:
            print(f"screenfeed running (log: {HOME / 'app.log'}, actions: {LOG})")
            return
        time.sleep(0.2)
    sys.exit(f"screenfeed is up but its widget does not refresh, see {LOG} and {HOME / 'app.log'}")


def ui_tick_age() -> float | None:
    """Seconds since the widget last refreshed, None if unknown."""
    try:
        return api("GET", "/status", timeout=3).get("ui_tick_age")
    except Exception:  # noqa: BLE001
        return None


def watch_idle() -> bool:
    """True when no batch can come: no screen picked, not recording, nothing unsent, sending or processing."""
    try:
        st = api("GET", "/status", timeout=3)
    except Exception:  # noqa: BLE001
        return False
    return (not st.get("screen") and not st.get("recording") and not st.get("unsent_seconds")
            and not st.get("sending") and not st.get("processing"))


def cmd_watch() -> None:
    """One stdout line per ready batch or error. Remembers what it already printed."""
    cursor = HOME / "watch.cursor"
    HOME.mkdir(parents=True, exist_ok=True)
    READY.touch()
    seen = int(cursor.read_text() or 0) if cursor.exists() else len(READY.read_text().splitlines())
    alive, last_check, frozen, idle_since = running(), 0.0, False, None
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
            # The Mac slept if this loop itself stood still: the widget did not refresh then either.
            slept = time.time() - last_check > 15 and last_check > 0
            last_check = time.time()
            ok = running()
            if alive and not ok:
                print("SCREENFEED app stopped running (watch exits)", flush=True)
                return
            alive = ok
            # Nothing can arrive while no screen is picked and nothing is recorded, unsent or in the works: after a
            # while the watch ends instead of being re-armed for hours. Picking a screen is when to re-arm it.
            if ok and watch_idle():
                idle_since = idle_since or time.time()
                if time.time() - idle_since > WATCH_IDLE_EXIT:
                    print(f"SCREENFEED idle: no screen selected and nothing unsent for {WATCH_IDLE_EXIT // 60} min "
                          "(watch exits; re-arm when a screen is picked)", flush=True)
                    return
            else:
                idle_since = None
            age = ui_tick_age() if ok else None
            if slept:
                age = None
            if age is not None and age > 10 and not frozen:
                frozen = True  # the widget stopped refreshing: clicks may go unseen
                print(f"SCREENFEED ERROR widget not refreshing for {age:.0f}s (see {LOG})", flush=True)
            elif age is not None and age < 2:
                frozen = False
        time.sleep(0.5)


def main(argv: list[str]) -> None:
    cmd = argv[1] if len(argv) > 1 else "help"
    try:
        if cmd == "app":
            return run_app()
        if cmd in ("launch", "watch"):  # run by Claude from the repository it works in
            remember_workdir()
        if cmd == "launch":
            return cmd_launch()
        if cmd == "watch":
            return cmd_watch()
        cmd = {"pause": "stop", "resume": "start"}.get(cmd, cmd)
        if cmd in ("start", "stop", "toggle", "send", "discard"):
            r = api("POST", f"/{cmd}")
        elif cmd in ("languages", "language", "lang"):
            if len(argv) > 2:
                value = argv[2].strip().lower()
                if value not in ("auto", "any") and not re.fullmatch(r"[a-z]{2,3}(,[a-z]{2,3})*", value.replace(" ", "")):
                    sys.exit("usage: screenfeed languages <codes, e.g. en,de | auto>")
                set_config("languages", value.replace(" ", ""))
            print(",".join(languages()) or "auto (any language)")
            return
        elif cmd == "deliver":
            if len(argv) > 2:
                mode = argv[2]
                if mode == "branch" and len(argv) > 3 and argv[3].strip():
                    set_config("deliver", f"branch:{argv[3].strip()}")
                elif mode in ("pr", "main"):
                    set_config("deliver", mode)
                else:
                    sys.exit("usage: screenfeed deliver pr | main | branch <branch or #PR>")
            print(deliver_label(deliver_mode()))
            return
        elif cmd == "note":
            arg = argv[2] if len(argv) > 2 else ""
            if arg in ("on", "off", "toggle"):
                set_config("overlay_note", (not overlay_note_shown()) if arg == "toggle" else arg == "on")
            elif arg:
                sys.exit("usage: screenfeed note [on | off | toggle]")
            print("note shown" if overlay_note_shown() else "note hidden (the frame stays)")
            return
        elif cmd == "workdir":
            if len(argv) > 2:
                path = os.path.abspath(os.path.expanduser(argv[2]))
                if not os.path.isdir(path):
                    sys.exit(f"not a directory: {path}")
                set_config("workdir", path)
            print(config_value("workdir") or "not set (screenfeed launch / watch remember the git repository they run in)")
            return
        elif cmd == "retry":
            if len(argv) < 3 or not argv[2].isdigit():
                sys.exit("usage: screenfeed retry <batch number>")
            r = api("POST", f"/retry?batch={int(argv[2])}")
        elif cmd == "auto":
            arg = argv[2] if len(argv) > 2 else "off"
            r = api("POST", f"/auto?seconds={0 if arg == 'off' else int(arg)}")
        elif cmd == "say":
            r = api("POST", "/say", " ".join(argv[2:]))
        elif cmd == "screens":
            r = api("GET", "/screens")
            print(f"{'*' if not any(x['selected'] for x in r['screens']) else ' '} 0: none (recording off)")
            for x in r["screens"]:
                print(f"{'*' if x['selected'] else ' '} {x['n']}: {x['label']} ({x.get('size', '')})")
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
