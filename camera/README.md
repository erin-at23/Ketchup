# Rewind P1: drift detection (outward camera)

## Setup (5 minutes)

    pip install -r requirements.txt
    cp .env.example .env        # fill in REWIND_API, GEMINI_API_KEY, GEMINI_MODEL, REWIND_TOPIC
    python3 drift.py --list-cameras   # find the glasses camera's index, put it in CAMERA_INDEX
    python3 drift.py

The first run downloads MediaPipe's small object detection model (a few MB) next to the script.

## Using it

1. Look at the slides and press `c`. Then look at the speaker and press `c` again. Up to `max_targets` (6)
   targets: before the demo, press `c` on several different slides, since a new slide can lose the match.
2. Watch the overlay: the top bar shows FOCUSED, DRIFTING or RETURNING and why.
3. `d` starts or stops a manual drift (demo backup). `q` quits and ends any open drift.

| Key | Does |
| --- | --- |
| c | add a target reference |
| x | clear references |
| d | manual drift on/off |
| m | mark the start of a staged drift (for measure.py; local log only) |
| [ / ] | lower / raise the match threshold |
| r | reload config.json |
| q | quit |

## How it decides

1. **On target?** ORB features in each frame are matched against your references. If the RANSAC inlier count
   is at or above `inlier_threshold`, you are focused. Watch the "match" number on screen while looking at
   the slides versus away, and set the threshold between the two.
2. **Not on target:** MediaPipe looks for a phone, laptop or notes.
   - A phone, laptop or page: the AI check (Gemini, or OpenAI with `JUDGE_PROVIDER=openai`) sees one frame
     (cached 6 s) and says related or unrelated to `REWIND_TOPIC` and the recent transcript. Unrelated with
     confidence >= 0.7 for 2 s starts a drift. A "book" goes to the AI too, because a laptop screen seen up
     close is often detected as one; notes on the subject come back related. Detections are held for
     `object_hold_seconds` (1.5 s) because they flicker. The cause is `phone` only if most detections were phones.
   - A drift on a screen is backdated to the first frame the AI rated unrelated, so related code shown
     on the same laptop before a tab switch is not counted. If the AI says `none` (no screen or paper
     actually in view), the detection was a false alarm and it counts as looking away.
   - A "tv" (usually the projector) also goes to the AI, so a slide change that loses the step 1 match
     still reads as focused ("related: lecture slide"). Without the AI check a tv is ignored.
   - An AI call that runs past the timeout + 1 s is abandoned and a new one is sent; its late answer is dropped.
   - Without the AI check: notes (a book or paper) count as focused.
   - Without Gemini: a phone in view for 3 s starts a drift; a laptop counts as focused.
   - Nothing on target and no screen (window, floor) for 5 s starts a drift.
3. Focused for 2 s closes the drift; drifts less than 3 s apart are merged. Start times are backdated.

All timings are in `config.json`; press `r` to reload after editing.

## Events sent to P3

    POST /drift/start {"start": <epoch>, "cause": "away" | "phone" | "unrelated_screen" | "manual", "detail": "shopping site"}
    POST /drift/end   {"drift_id": <id from the start response>, "end": <epoch>}

`cause` and `detail` are extra, optional fields. A 409 is retried every 2 s. Every event is also appended
to `drift_log.jsonl`, which is handy for counting catches and false alarms.

## Testing without the team

    python3 fake_backend.py --started &        # stand-in for P3 (omit --started to test the 409 retry)
    REWIND_API=http://localhost:8787 python3 drift.py
    python3 drift.py --video recording.mp4 --calibrate-at 1   # replay a recording from the real mount
    python3 make_test_video.py && python3 drift.py --video test.mp4 --headless --dry --calibrate-at 1

To record test footage from the glasses camera, any recording app works; or capture with OpenCV.

A replay runs at the video's real speed when the AI check is on or a window is shown, so AI verdicts
arrive at realistic moments. `--fast` replays as fast as possible (fine with the AI check off).

## Measuring (catches and false alarms)

    python3 measure.py               # drifts in the last run, with start time, length, cause, detail
    python3 measure.py --runs 3      # the last 3 runs
    python3 measure.py --staged 10   # adds a score sheet for 10 staged drifts

Working alone: press `m` in the video window the moment you start each staged drift. measure.py then
matches each mark to the first drift starting within 15 s (`--window`) and prints catches, misses,
false alarms (drifts with no mark) and the detection delay. Manual `d` drifts are not counted.
Measured in several short runs? `python3 measure.py --marked --since 14:34` combines every run with
marks started after that time. Set `REWIND_LOG=/some/other.jsonl` for test runs you don't want counted.

Each run of drift.py starts a new section in `drift_log.jsonl`, so you never need to delete the log.

## Tuning checklist

- Calibrate in the real demo spot, with the projector on.
- Look at the slides, then away: note both match numbers and set `inlier_threshold` between them.
- If slide changes knock you off target, calibrate with more of the room around the screen in view.
- If your phone isn't detected, hold it a bit higher and closer; lower `detector_score` to 0.3.
