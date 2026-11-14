#!/usr/bin/env python3
"""Summarizes drift_log.jsonl: how many drifts there were and how long each lasted.

    python3 measure.py               # the last run
    python3 measure.py --runs 3      # the last 3 runs
    python3 measure.py --all         # everything in the log
    python3 measure.py --staged 10   # also print a catch / false-alarm sheet for 10 staged drifts
    python3 measure.py --marked --since 14:34   # combine every run with m marks started after 14:34

If you pressed `m` in drift.py when each staged drift began, the marks are matched to drifts
automatically: catches, misses, false alarms and how fast each was detected.

Reads only drift times and labels; no frames are ever stored.
"""
import argparse
import json
import os
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def load_runs(path):
    runs, cur = [], None
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("event") == "run" or cur is None:   # logs from before run markers form one run
            cur = {"t": e.get("t"), "dry": e.get("dry"), "drifts": {}, "order": [], "marks": []}
            runs.append(cur)
            if e.get("event") == "run":
                continue
        did = e.get("drift_id")
        if e.get("event") == "mark":
            cur["marks"].append(e["t"])
        elif e.get("event") == "start":
            key = did if did is not None else f"local{len(cur['order'])}"   # older dry-run logs had no id
            cur["drifts"][key] = {"start": e["start"], "end": None,
                                  "cause": e.get("cause", ""), "detail": e.get("detail", ""),
                                  "detected_at": e.get("detected_at")}
            cur["order"].append(key)
        elif e.get("event") == "end":
            d = cur["drifts"].get(did)
            if d is None:   # no matching id: pair with the oldest drift still open
                d = next((cur["drifts"][k] for k in cur["order"] if cur["drifts"][k]["end"] is None), None)
            if d is not None:
                d["end"] = e["end"]
    return [r for r in runs if r["order"] or r["marks"]]


def match_marks(marks, drifts, before, after):
    """Each mark claims the first unclaimed automatic drift starting within [mark - before, mark + after]."""
    claimed, rows = set(), []
    for m in marks:
        hit = next((i for i, d in enumerate(drifts) if i not in claimed and d["cause"] != "manual"
                    and m - before <= d["start"] <= m + after), None)
        if hit is not None:
            claimed.add(hit)
        rows.append((m, hit))
    false_alarms = [i for i, d in enumerate(drifts) if d["cause"] != "manual" and i not in claimed]
    return rows, false_alarms


def clock(t):
    return time.strftime("%H:%M:%S", time.localtime(t))


def main():
    ap = argparse.ArgumentParser(description="Count drifts in drift_log.jsonl")
    ap.add_argument("--log", default=os.path.join(HERE, "drift_log.jsonl"))
    ap.add_argument("--runs", type=int, default=1, help="how many recent runs to include (default 1)")
    ap.add_argument("--all", action="store_true", help="include every run in the log")
    ap.add_argument("--marked", action="store_true", help="only runs where you pressed m (combine all of them)")
    ap.add_argument("--since", help="only runs started at or after this time today, HH:MM")
    ap.add_argument("--staged", type=int, default=None, help="number of drifts you staged, for the score sheet")
    ap.add_argument("--window", type=float, default=15.0,
                    help="a drift starting up to this many seconds after an m mark counts as its catch (default 15)")
    args = ap.parse_args()

    if not os.path.exists(args.log):
        raise SystemExit(f"No log at {args.log}. Run drift.py first.")
    runs = load_runs(args.log)
    if not runs:
        raise SystemExit("The log has no drifts yet.")
    if args.since:
        hh, mm = (int(x) for x in args.since.split(":"))
        cut = time.mktime(time.localtime()[:3] + (hh, mm, 0, 0, 0, -1))
        runs = [r for r in runs if r["t"] and r["t"] >= cut]
    if args.marked:
        runs = [r for r in runs if r["marks"]]
    if not runs:
        raise SystemExit("No runs match those filters.")
    runs = runs if (args.all or args.marked or args.since) else runs[-args.runs:]

    drifts = [d for r in runs for d in (r["drifts"][i] for i in r["order"])]
    print(f"{len(drifts)} drift(s) in {len(runs)} run(s)\n")
    print(f"{'#':>3}  {'start':8}  {'length':>7}  {'cause':16}  detail")
    durations, by_cause = [], {}
    for n, d in enumerate(drifts, 1):
        if d["end"] is None:
            length = "open"
        else:
            secs = d["end"] - d["start"]
            durations.append(secs)
            length = f"{secs:6.1f}s"
        by_cause[d["cause"]] = by_cause.get(d["cause"], 0) + 1
        print(f"{n:>3}  {clock(d['start'])}  {length:>7}  {d['cause'][:16]:16}  {d['detail'][:50]}")

    if durations:
        print(f"\nlength: total {sum(durations):.1f}s, mean {sum(durations) / len(durations):.1f}s, "
              f"min {min(durations):.1f}s, max {max(durations):.1f}s")
    print("by cause: " + ", ".join(f"{c or '?'} {k}" for c, k in sorted(by_cause.items())))
    open_n = sum(1 for d in drifts if d["end"] is None)
    if open_n:
        print(f"{open_n} drift(s) never got an end (script killed hard, or the end call failed)")

    marks = [m for r in runs for m in r["marks"]]
    if marks:
        rows, false_alarms = match_marks(marks, drifts, 3.0, args.window)
        caught = [(m, i) for m, i in rows if i is not None]
        print(f"\nStaged drifts (m marks): {len(marks)}")
        print(f"{'mark':>4}  {'marked':8}  result")
        delays = []
        for k, (m, i) in enumerate(rows, 1):
            if i is None:
                print(f"{k:>4}  {clock(m)}  MISSED (no drift within {args.window:.0f}s)")
                continue
            d = drifts[i]
            delay = d["detected_at"] - m if d.get("detected_at") else None
            if delay is not None:
                delays.append(delay)
            print(f"{k:>4}  {clock(m)}  caught by drift #{i + 1} ({d['cause']})"
                  + (f", detected {delay:.1f}s after the mark" if delay is not None else ""))
        print(f"\nRESULT: caught {len(caught)}/{len(marks)}, missed {len(marks) - len(caught)}, "
              f"false alarms {len(false_alarms)}" + (f" (drift #{', #'.join(str(i + 1) for i in false_alarms)})" if false_alarms else ""))
        if delays:
            print(f"detection delay after the mark: mean {sum(delays) / len(delays):.1f}s, max {max(delays):.1f}s")
        # time actually observed: each run from its start to its last event (gaps between runs excluded)
        secs = 0.0
        for r in runs:
            ds = [r["drifts"][k] for k in r["order"]]
            last = max([d["end"] or d["start"] for d in ds] + r["marks"] + [r["t"] or 0])
            secs += last - (r["t"] or last)
        print(f"over about {secs / 60:.1f} min of watching (at least; time after the last event isn't logged)")
    elif args.staged is not None:
        manual = by_cause.get("manual", 0)
        auto = len(drifts) - manual
        print(f"\nScore sheet: {args.staged} staged drifts, {auto} automatic detections"
              + (f" (+{manual} manual, not counted)" if manual else ""))
        print("Mark each automatic drift above as a catch (matches a staged drift) or a false alarm, then:")
        print("  catches = ____ / %d    false alarms = ____    missed = %d - catches" % (args.staged, args.staged))
        if auto < args.staged:
            print(f"  at least {args.staged - auto} staged drift(s) were missed")


if __name__ == "__main__":
    main()
