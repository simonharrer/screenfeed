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
screenfeed deliver [pr | main | branch <name or #PR>]  # where batches go: new PR each (default), main, one branch/PR
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
   or error (`SCREENFEED ERROR ...`).
   When it expires, check `screenfeed status` and re-arm only while a batch can still come: a screen is
   selected (`screen` set), or something is recording, unsent, sending or processing. Otherwise do not re-arm:
   say once in chat that the watch is off until a screen is picked, and re-arm when the user asks.
   `watch` also ends on its own: `SCREENFEED app stopped running` (the user quit the app) and
   `SCREENFEED idle: …` (no screen and nothing unsent for 10 minutes, `SCREENFEED_WATCH_IDLE_EXIT`).
   Do not re-arm after either; one short line in chat is enough.
   `watch` remembers what it printed, so re-arming does not lose or repeat batches.
2. Tell the user they can press Rec, talk while pointing, and press Send (or turn on Auto).
3. On each batch event:
   - `screenfeed say "Batch n: reading"`
   - Read `timeline.md`. Read the `zoom` image of every SAID moment (that is what the user pointed at) and
     the `full` images when you need the context of the page. Read the END STATE frame to know the current state.
   - Decide first what kind of batch it is:
     - **Change request**: concrete remarks about what is on screen ("align this", "show the icon here",
       "render this as Markdown"). Go on with the steps below.
     - **Discussion**: a conversation (several voices, "wie komme ich eigentlich …", ideas, "das muss man mal
       analysieren"), a feature idea that needs design, or remarks spread over several topics. Do not change code.
       Summarize the points in chat (one line each, with what you see in the frames), propose a scope for each
       (quick fix, own PR, analysis first, not now), recommend where to start, and ask. Show
       `screenfeed say "Batch n: question in terminal"`. Act when the user answers; one answer can start several
       PRs (in the `pr` mode, one fork per PR).
     A batch can be both: deliver the concrete remarks, and ask about the rest in the same message.
   - Map what was said + pointed at to concrete changes in the code of the app being shown
     (the `app:` field and the full frame tell you which app/page). Deictic words ("this", "here", "das hier")
     refer to the element under the red ring at that moment.
   - Make the changes. Keep each batch's changes focused on what was said; do not refactor around it.
   - Deliver them as the batch says: the `Deliver:` line in `timeline.md` (and `deliver` in the event) is the
     mode when the batch was sent; the user picks it in the widget's Deliver picker or with
     `screenfeed deliver pr | main | branch <name or #PR>`. In the repository of the app being shown:
     - **New PR per batch** (`pr`, default): **one pull request per batch**. This overrides a repository's
       habit of committing small fixes straight to main.
       1. Before editing: check `git status`. Changes that are not yours from an earlier batch: stop and
          ask. Then `git fetch` and `git switch -c screenfeed/batch-<n>-<short-slug> origin/<default branch>`,
          so every batch starts from the default branch and its PR holds only this batch.
       2. Edit, run the relevant tests (the repository's CLAUDE.md says how), commit with a message that
          says what changed and why, push, and `gh pr create` with a title in the imperative and a body
          with what the user said (quoted, from the transcript), what changed, the tests run, and
          **before/after screenshots** (see below). No co-author lines.
       3. `git switch` back to the default branch, and give the PR link in chat and a short note in the
          widget (`screenfeed say "Batch n: PR #123 ..."`).
       A later batch that corrects an earlier one that is still open goes onto that batch's branch and PR.
     - **Commit to main** (`main`): `git switch <default branch>` and `git pull --ff-only`, edit, run the
       relevant tests, commit, push. No pull request; describe the change and the tests in chat.
     - **Branch / PR** (`branch`, with a target): every batch goes onto that branch. A target like `#123` is
       a pull request: its branch is `gh pr view 123 --json headRefName`. Switch to the branch (fetch it,
       create it from the default branch if it does not exist yet), edit, test, commit, push. If the branch
       has no pull request yet, open one; then add a section per batch to its description (what was said,
       what changed, tests, before/after screenshots). Switch back to the default branch afterwards.
     **Before/after screenshots** (for every PR): before = the batch frame cropped to the changed part of
     the page; after = the same part with the change, from the app running locally on the branch (for
     entropy-data: the `ui-test` skill's start command and `/start`, which signs in a demo organization).
     Crop both to the UI that changed and look at them first: nothing else from the screen (other apps,
     video calls, notifications, personal data, colleagues' names) goes into a PR. Store them on the
     repository's `pr-screenshots` branch (image only, no history with main) and link them in the PR
     description as `https://github.com/<owner>/<repo>/blob/pr-screenshots/pr-<n>/before.png?raw=true`.
     A batch with nothing to change (no speech, only clicking) gets no commit and no PR.
   - `screenfeed say "Batch n: <what changed, max ~60 chars>"` so the user sees it on screen,
     and give a short summary in chat. If a remark is ambiguous, ask in chat and say so in the widget
     ("Batch n: question in terminal").
   - If the app needs a reload/rebuild to show the change, do it (or tell the user) so the next batch shows the new state.
4. Batches arrive while you work; handle them in order. A later batch can correct an earlier one; the newest wins.
5. **New PR per batch: one forked agent per batch.** Only in the `pr` mode (each batch's `Deliver:` line), and
   only when the Agent tool can fork (`subagent_type: "fork"`): batches then share no branch, so they run in
   parallel. On each batch event, after `screenfeed say "Batch n: reading"`, fork one agent for that batch and
   go back to waiting; do not work on the batch yourself. Its prompt names the batch (`timeline.md` path,
   number) and these rules:
   - Its own git worktree from the default branch (`git worktree add -b screenfeed/batch-<n>-<slug>
     ../<repo>-b<n> origin/<default branch>`), never the main checkout, so forks do not switch each other's
     branches. Remove the worktree when the PR is open.
   - Its own port for the local app (e.g. 8140 + n), and stop that app (kill by port) when done. A fresh
     worktree may need what is not in git: copy ignored binaries from the main checkout when they fail
     (entropy-data: `tailwindcss`, whose download can come with a broken signature, so macOS kills it).
   - Everything else as in step 3: read the batch, change, test, commit, push, PR with before/after
     screenshots, `screenfeed say "Batch n: PR #… …"`. A batch that only needs a question: it reports the
     question back instead of guessing.
   - It reports back the PR link, what changed and the tests, or the question; you relay that in chat.
   A batch that corrects an earlier one whose fork is still running: wait for that fork, then give the
   correction to its branch (send it to that agent, or fork a new one on the same branch). In the `main` and
   `branch` modes, batches share one branch: handle them yourself, in order, as in step 3.

## Notes

- No batch is lost by design: one recorder per session writes 2 s chunks and Send only cuts by time;
  a watchdog replaces a dead or stalled recorder (the batch lists the gap); a failed batch keeps its raw
  video (error event says `screenfeed retry <n>`; run it); content left by a crash or quit arrives as a
  recovered batch on the next start (no cursor data, so no pointing frames).
- Auto-sent batches without speech are skipped; a manual Send always produces a batch.
- A batch shows at most 24 moments (`SCREENFEED_MAX_MOMENTS`): one per sentence first, then further moments of
  sentences, then clicks without speech. A double or triple click is one moment.
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
- `screenfeed status` shows `capture`: `sck` leaves screenfeed's widget, frame and badges out of the video;
  `avfoundation` (fallback, see `capture_problem`) records them, so ignore them in the frames then.
- The widget shows a permission banner (and `screenfeed status` lists `permission_problems`) when Screen
  Recording or Microphone permission is missing for the app screenfeed runs in, or the capture falls back.
- `screenfeed status` shows `ui_tick_age`: seconds since the widget last refreshed. Above a few
  seconds the widget is frozen (clicks still reach the app but nothing updates); `watch` then prints
  `SCREENFEED ERROR widget not refreshing`. Check the log for `ui refresh failed`.
- Debugging: every action (widget clicks, CLI/API calls, recorder start/stop with pid, exit code and
  file size, sends, batch processing, uncaught errors with tracebacks) is logged to
  `~/.screenfeed/screenfeed.log` (rotated to `.log.1` at 5 MB). Read it first when a batch does not arrive.
- Data lives in `~/.screenfeed`; raw video is deleted after each batch is processed. Batches (frames and
  transcripts) stay until deleted; they can contain whatever was on screen.
