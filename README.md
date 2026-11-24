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
| Record / pause button | Start recording, pause (unsent content is kept), resume |
| Timer | Recorded time not sent yet; frozen while paused (red = recording, orange = paused) |
| **Send** | Cut the current batch and hand it to Claude |
| Trash | Discard everything not sent yet |
| Display picker | Which display is recorded, or "No screen" to disable recording; remembered across restarts |
| Auto-send picker | Off, every 30 s, 1, 2 or 5 min (batches without speech are skipped) |
| ✨ line | The latest status message from Claude |
| Sent to Claude | The last 4 batches: time, number, length, and whether they reached Claude |
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
screenfeed discard                 throw away unsent content
screenfeed auto <seconds|off>      periodic sending
screenfeed screens                 list displays (* = selected)
screenfeed screen <n|name|none>    pick the recorded display (none = recording disabled)
screenfeed say "<text>"            show a message in the widget (used by Claude)
screenfeed status                  JSON state, including unsent time and history
screenfeed watch                   one line per ready batch (for Claude Code's Monitor tool)
screenfeed quit
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
| `SCREENFEED_LANG` | `auto` (whisper language detection) |
| `SCREENFEED_AUDIO` | system default input; a device name or `none` |
| `SCREENFEED_WHISPER_MODEL` | `~/.local/share/whisper-models/ggml-large-v3-turbo.bin` |
| `SCREENFEED_FPS` | `5` |
| `SCREENFEED_MAX_MOMENTS` | `24` frames per batch |
| `SCREENFEED_KEEP_VIDEO` | unset; `1` keeps the raw video of each batch |
| `SCREENFEED_HOME` | `~/.screenfeed` |
| `SCREENFEED_PORT` | `47811` |

## Privacy

Everything runs locally; transcription uses a local whisper model. Raw video is deleted after each
batch is processed, but batch folders in `~/.screenfeed/batches` (frames and transcripts) stay until
you delete them, and they contain whatever was on screen. Whatever Claude reads from a batch is sent
to Claude like any other file it reads.

## License

MIT, see [LICENSE](LICENSE).
