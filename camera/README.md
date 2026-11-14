# camera/ — drift detection

The webcam script reports when the student zones out and comes back.

    POST $REWIND_API/drift/start  {"start": <epoch>, "cause": "unrelated_screen", "detail": "Online shopping page"}  -> {"drift_id": N}
    POST $REWIND_API/drift/end    {"drift_id": N, "end": <epoch>}

- `cause` and `detail` are optional. `detail` is shown on the timeline bar.
- The backend assigns `drift_id`.
- `409` means no session yet: retry every 2 s.

Run it on the demo laptop so it can use `REWIND_API=http://localhost:8001`.
