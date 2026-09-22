"""bakeoff/looptest/tokens.py: per-response rows, dedupe, subagent discovery, sensitivity."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "looptest"))
import tokens


def _entry(ts, request, model, *, inp=0, cw=0, cr=0, out=0, sidechain=False):
    return json.dumps(
        {
            "timestamp": ts,
            "requestId": request,
            "isSidechain": sidechain,
            "message": {
                "model": model,
                "usage": {
                    "input_tokens": inp,
                    "cache_creation_input_tokens": cw,
                    "cache_read_input_tokens": cr,
                    "output_tokens": out,
                },
            },
        }
    )


def _logs(tmp_path: Path) -> Path:
    main = tmp_path / "sess.jsonl"
    main.write_text(
        "\n".join(
            [
                "not json",
                _entry(
                    "2026-09-20T10:00:00Z", "r1", "claude-fable-5-1", inp=10, cw=100, cr=1000, out=5
                ),
                _entry(
                    "2026-09-20T10:00:00Z", "r1", "claude-fable-5-1", inp=10, cw=100, cr=1000, out=5
                ),
                _entry(
                    "2026-09-20T10:10:00Z", "r2", "claude-fable-5-1", inp=1, cw=9, cr=2000, out=10
                ),
                json.dumps({"timestamp": "2026-09-20T10:11:00Z", "message": {"role": "user"}}),
            ]
        ),
        encoding="utf-8",
    )
    sub = tmp_path / "sess" / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-abc.jsonl").write_text(
        _entry("2026-09-20T10:05:00Z", "s1", "claude-sonnet-5", inp=50, cw=50, cr=500, out=100),
        encoding="utf-8",
    )
    return main


def test_rows_are_deduped_ordered_and_attributed(tmp_path: Path) -> None:
    rows = tokens.read_rows([_logs(tmp_path)])
    assert [(r.who, r.model) for r in rows] == [
        ("lead", "claude-fable-5-1"),
        ("subagent", "claude-sonnet-5"),
        ("lead", "claude-fable-5-1"),
    ]
    assert rows[0].fresh == 115 and rows[0].context == 1110


def test_summary_and_sensitivity(tmp_path: Path) -> None:
    summary = tokens.summarize(tokens.read_rows([_logs(tmp_path)]))
    assert summary["lead_model"] == "claude-fable-5-1"
    assert summary["lead"]["responses"] == 2 and summary["lead"]["fresh_tokens"] == 135
    assert summary["subagents"]["fresh_tokens"] == 200
    assert summary["lead_peak_context"] == 2010
    assert summary["wall_minutes"] == 10.0
    table = {
        (t["lead_x"], t["cache_read_x"]): t["units"] for t in summary["weighted_units_sensitivity"]
    }
    # lead: 135 fresh + 3000 cache read; subagent (weight 1): 200 fresh + 500 cache read
    assert table[(3.0, 0.1)] == (3 * (135 + 300) + (200 + 50))
    assert table[(5.0, 0.25)] == (5 * (135 + 750) + (200 + 125))


def test_time_window_and_series(tmp_path: Path) -> None:
    main = _logs(tmp_path)
    rows = tokens.read_rows([main], start=tokens._ts("2026-09-20T10:04:00Z"))
    assert len(rows) == 2
    out = tmp_path / "series.csv"
    tokens.write_series(tokens.read_rows([main]), out)
    lines = out.read_text().splitlines()
    assert len(lines) == 4
    assert lines[-1].split(",")[-4:] == ["135", "3000", "200", "500"]
