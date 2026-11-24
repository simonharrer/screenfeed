# screenfeed

Show Claude what you mean: talk while pointing at your screen with the mouse, and screenfeed turns it
into batches Claude Code can act on.

A small floating widget records the screen you pick, your microphone, and the mouse cursor. Every
batch becomes a folder with a `timeline.md`:

- **what you said**, transcribed locally with whisper.cpp (any language)
- **where you pointed while you said it**: for each sentence, the moment the cursor rested or clicked,
  as a zoomed crop and a full-screen frame with a red ring around the cursor
- **clicks** in between (navigation context) and an **end state** frame

Claude Code watches for new batches, reads them, and changes the code of whatever you were showing.

## Requirements

macOS (Apple Silicon tested), plus:

```sh
brew install ffmpeg whisper-cpp uv
mkdir -p ~/.local/share/whisper-models
curl -L -o ~/.local/share/whisper-models/ggml-large-v3-turbo.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin
```

The terminal you start screenfeed from needs **Screen Recording** and **Microphone** permission
(System Settings, Privacy & Security). Python dependencies (PyObjC, Pillow) are installed by `uv`
on first run.

## Install

```sh
git clone https://github.com/simonharrer/screenfeed.git
ln -s "$PWD/screenfeed/screenfeed.py" ~/.local/bin/screenfeed   # any directory on your PATH
screenfeed launch
```

## The widget

| Control | What it does |
|---|---|
| **Record / Pause / Resume** | Start recording, pause (unsent content is kept), resume |
| Timer | Recorded time not sent yet; frozen while paused (red = recording, orange = paused) |
| **Send** | Cut the current batch and hand it to Claude |
| **Discard** | Throw away what was recorded but not sent yet; it never reaches Claude |
| Display picker | Which display is recorded, or "No screen" to disable recording; remembered across restarts |
| Auto-send picker | Off, every 30 s, 1, 2 or 5 min (batches without speech are skipped) |
| ✨ line | The latest status message from Claude, with how long ago it came ("1 min ago") |
| Sent to Claude | The last 4 batches: how long ago ("just now", "3 min ago", in whole minutes), number, length, and whether they reached Claude |
| Power button | Quit (asks for a second click if there is unsent content) |

While the app runs, the selected display shows a green frame with a **Ready** badge, so you always
know which screen Rec will record. While recording, it turns into a pulsing red frame with a
**● REC** badge, and other displays show **Not recorded**. The frames are click-through and kept
out of the recording. The widget also shows the build time and git commit of the running version.

## CLI

```
screenfeed launch                  start the widget in the background
screenfeed start | stop            record / pause   (aliases: resume | pause)
screenfeed send                    send the current batch
screenfeed retry <n>               process a failed batch again from the raw video it kept
screenfeed languages [en,de|auto]  show or set the spoken languages (default en,de)
screenfeed discard                 throw away unsent content
screenfeed auto <seconds|off>      periodic sending
screenfeed screens                 list displays (* = selected)
screenfeed screen <n|name|none>    pick the recorded display (none = recording disabled)
screenfeed say "<text>"            show a message in the widget (used by Claude)
screenfeed status                  JSON state, including unsent time and history
screenfeed watch                   one line per ready batch (for Claude Code's Monitor tool)
screenfeed quit                    unsent content is sent as a recovered batch on next start
```

The widget is controlled through a local HTTP API on `127.0.0.1:47811` (no authentication, local
processes only).

## Use with Claude Code

Copy the skill and ask Claude to "start screenfeed" in the project you want to improve:

```sh
mkdir -p ~/.claude/skills/screenfeed
cp claude-skill/SKILL.md ~/.claude/skills/screenfeed/
```

Claude launches the widget, follows `screenfeed watch`, reads each `timeline.md` and its images,
makes the changes, and reports back in the widget with `screenfeed say`.

## Configuration

| Variable | Default |
|---|---|
| `SCREENFEED_LANG` | one fixed language, e.g. `de` (overrides `SCREENFEED_LANGUAGES`) |
| `SCREENFEED_AUDIO` | system default input; a device name or `none` |
| `SCREENFEED_WHISPER_MODEL` | `~/.local/share/whisper-models/ggml-large-v3-turbo.bin` |
| `SCREENFEED_FPS` | `5` |
| `SCREENFEED_MAX_MOMENTS` | `24` frames per batch |
| `SCREENFEED_KEEP_VIDEO` | unset; `1` keeps the raw video of each batch |
| `SCREENFEED_LANGUAGES` | `en,de`: spoken languages; anything else whisper detects is transcribed again in the last allowed one. Change at runtime with `screenfeed languages en,de` (or `auto`) |
| `SCREENFEED_HOME` | `~/.screenfeed` |
| `SCREENFEED_PORT` | `47811` |

## No lost batches

The recorder is built so that a batch cannot get lost:

- **One recorder per recording session.** It runs from Rec until Pause (or a screen switch) and writes
  2-second chunks to `~/.screenfeed/raw`. **Send only cuts by time**: it never stops or starts the
  recorder, so a recorder that hangs on stop or fails to start cannot take a batch with it. Only one
  recorder runs at a time.
- **Watchdog.** A recorder that exits or captures no frame for 8 s (ffmpeg's own frame counter) is stopped (q, then SIGTERM, then
  SIGKILL) and replaced. The batch notes the gap ("Recording gaps … anything said then is missing").
  After 3 failures in a row recording stops and Claude gets an error event.
- **Chunks stay until their batch is processed.** If processing fails, the batch folder keeps its raw
  video and `job.json`, and Claude gets an error with the hint `screenfeed retry <n>`.
- **Crash and quit recovery.** Chunks that never made it into a processed batch are sent as a
  recovered batch on the next start (without cursor data, so without pointing frames).

A recorder that captures but cannot finish chunks is left running; batches then read its unfinished
chunk file. Screen frames get wall-clock timestamps, because AVFoundation on recent macOS hands out
frames with timestamp 0.

The only content that can be lost is the chunk being written when a recorder has to be killed
(at most 2 s), and whatever is said while a recorder is hung, which the batch reports as a gap.

## Troubleshooting

Every action is logged to `~/.screenfeed/screenfeed.log` (rotated to `.log.1` at 5 MB): widget
clicks, CLI calls, each recorder (ffmpeg) start and stop with its pid, exit code and file size, sends,
batch processing, and uncaught errors with tracebacks. When a batch does not arrive, look there first.
`~/.screenfeed/app.log` has the app's raw stdout and stderr.

## Privacy

Everything runs locally; transcription uses a local whisper model. Raw video is deleted after each
batch is processed, but batch folders in `~/.screenfeed/batches` (frames and transcripts) stay until
you delete them, and they contain whatever was on screen. Whatever Claude reads from a batch is sent
to Claude like any other file it reads.

## License

MIT, see [LICENSE](LICENSE).
