---
name: screenfeed
description: Iterate on a UI or app from the user's narrated screen recordings. The user talks and points with the mouse; screenfeed cuts this into batches (transcript + frames showing where the cursor pointed) and Claude applies the feedback. Use when the user says "screenfeed", wants to give feedback by showing/talking over their screen, or asks to start/stop/send/auto-send screen feedback.
---

# screenfeed

`screenfeed` (see https://github.com/simonharrer/screenfeed) records the screen the floating widget is on,
the microphone, and the mouse cursor. The widget has a record/pause button, **Send**, **Discard**, a display picker, auto-send (off, 30s, 1m, 2m, 5m) and a quit button.
Each sent batch becomes `~/.screenfeed/batches/<nnnn>-<time>/timeline.md` with:
- `SAID: "..."` lines (whisper transcript, any language, wall-clock aligned)
- for each sentence: where the cursor rested or clicked while it was said, as a `zoom` crop and a `full` frame,
  both with a red ring around the cursor tip
- standalone `CLICK`s (navigation context) and an `END STATE` frame (how the screen looks at the end of the batch)

## Control (Bash)

```
screenfeed launch            # start the widget in the background (idempotent)
screenfeed start | stop      # recording on/off (also the record/pause button); stop = pause,
                             #   unsent content is kept until sent or discarded
screenfeed discard           # throw away everything recorded but not sent (also the Discard button)
screenfeed screens | screen <n|name>   # list / pick the recorded display
screenfeed send              # cut and send the current batch now
screenfeed retry <n>         # process a failed batch again from the raw video it kept
screenfeed languages [en,de|auto]  # spoken languages (default en,de); others are re-read in the last allowed one
screenfeed auto 60 | off     # periodic sending
screenfeed say "text"        # show a short status line from Claude in the widget
screenfeed status            # JSON state
screenfeed quit
```

Never test with `say` or other audio playback; the user may be in a call. Only record when the user asked for it.

## Feedback loop

1. `screenfeed launch`, then arm a Monitor with command `screenfeed watch`, timeout 1800000 ms,
   description "screenfeed batches". It prints one line per ready batch (`SCREENFEED BATCH n ready ... :: "<speech>"`)
   or error (`SCREENFEED ERROR ...`). Re-arm it whenever it expires while the app is running.
   When the user quits the app (Quit button), `watch` prints `SCREENFEED app stopped running` and
   exits, so the watch ends with it; do not re-arm then.
   `watch` remembers what it printed, so re-arming does not lose or repeat batches.
2. Tell the user they can press Rec, talk while pointing, and press Send (or turn on Auto).
3. On each batch event:
   - `screenfeed say "Batch n: reading"`
   - Read `timeline.md`. Read the `zoom` image of every SAID moment (that is what the user pointed at) and
     the `full` images when you need the context of the page. Read the END STATE frame to know the current state.
   - Map what was said + pointed at to concrete changes in the code of the app being shown
     (the `app:` field and the full frame tell you which app/page). Deictic words ("this", "here", "das hier")
     refer to the element under the red ring at that moment.
   - Make the changes. Keep each batch's changes focused on what was said; do not refactor around it.
   - `screenfeed say "Batch n: <what changed, max ~60 chars>"` so the user sees it on screen,
     and give a short summary in chat. If a remark is ambiguous, ask in chat and say so in the widget
     ("Batch n: question in terminal").
   - If the app needs a reload/rebuild to show the change, do it (or tell the user) so the next batch shows the new state.
4. Batches arrive while you work; handle them in order. A later batch can correct an earlier one; the newest wins.

## Notes

- No batch is lost by design: one recorder per session writes 2 s chunks and Send only cuts by time;
  a watchdog replaces a dead or stalled recorder (the batch lists the gap); a failed batch keeps its raw
  video (error event says `screenfeed retry <n>`; run it); content left by a crash or quit arrives as a
  recovered batch on the next start (no cursor data, so no pointing frames).
- Auto-sent batches without speech are skipped; a manual Send always produces a batch.
- The widget shows the unsent recorded time (frozen while paused) and a SENT history of the last
  4 batches (time, number, length, auto/manual, processing / → Claude / skipped / failed).
  `screenfeed status` returns the same as `unsent_seconds`, `paused_with_unsent` and `history`.
- The recorded screen is picked in the widget's "Screen:" dropdown, or with `screenfeed screens` /
  `screenfeed screen <n|name|none>` (default: the last pick, else the display the widget starts on).
  "No screen" disables recording (and pauses a running one). While the app runs and is not
  recording, the selected display shows a green "Ready" frame. Picking a screen flashes
  "This screen will be recorded" on it for 2s; while recording, switching moves the recording at once
  and keeps what was recorded so far in the batch. While recording, that display gets a pulsing red frame and a
  "● REC" badge, and the widget gets a red border; other displays show "Not recorded".
- Config via env vars: `SCREENFEED_LANGUAGES` (default `en,de`; or `screenfeed languages`), `SCREENFEED_LANG` (one fixed language), `SCREENFEED_AUDIO` (mic name or `none`,
  default system input), `SCREENFEED_FPS` (5), `SCREENFEED_KEEP_VIDEO=1`, `SCREENFEED_WHISPER_MODEL`.
- Debugging: every action (widget clicks, CLI/API calls, recorder start/stop with pid, exit code and
  file size, sends, batch processing, uncaught errors with tracebacks) is logged to
  `~/.screenfeed/screenfeed.log` (rotated to `.log.1` at 5 MB). Read it first when a batch does not arrive.
- Data lives in `~/.screenfeed`; raw video is deleted after each batch is processed. Batches (frames and
  transcripts) stay until deleted; they can contain whatever was on screen.
