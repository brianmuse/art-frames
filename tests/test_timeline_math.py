"""The Schedule timeline's pure helpers in app.js (timelineMath): snap, hhmm, the drop place and the block label.
Run them under Node. The labels must match the server's (web.span_text and web.tight_span)."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from art_frames.web import hhmm, span_text, tight_span

SCRIPT = Path(__file__).resolve().parents[1] / "art_frames" / "static" / "app.js"
NODE = shutil.which("node")


def run(expression):
    source = SCRIPT.read_text()
    block = source[source.index("// BEGIN timeline-math"):source.index("// END timeline-math")]
    program = block + f"console.log(JSON.stringify({expression}));"
    result = subprocess.run([NODE, "-e", program], capture_output=True, text=True, check=True, timeout=30)
    return json.loads(result.stdout)


@pytest.mark.skipif(NODE is None, reason="Node is not installed")
def test_snap_rounds_to_15_minutes():
    assert run("[0, 7, 8, 22, 23, 1433, 1440].map(timelineMath.snap)") == [0, 0, 15, 15, 30, 1440, 1440]


@pytest.mark.skipif(NODE is None, reason="Node is not installed")
def test_hhmm_matches_the_server():
    values = [0, 5, 60, 615, 1439]
    assert run(f"{values}.map(timelineMath.hhmm)") == [hhmm(value) for value in values]


@pytest.mark.skipif(NODE is None, reason="Node is not installed")
def test_a_dropped_block_starts_at_the_pointer_inside_the_day():
    # At 2:00 PM, at 2:07 PM (snaps back), at 2:08 PM (snaps on), before the day, and too late for 2 hours.
    assert run("[840, 847, 848, -30, 1400].map(timelineMath.dropStart)") == [840, 840, 855, 0, 1320]


@pytest.mark.skipif(NODE is None, reason="Node is not installed")
def test_block_labels_match_the_server():
    spans = [(360, 540), (690, 780), (1320, 1440), (0, 1440), (465, 1215)]

    def text(minute):
        return "24:00" if minute == 1440 else hhmm(minute)
    found = run(f"{json.dumps(spans)}.map(([a, b]) => [timelineMath.tightSpan(a, b), timelineMath.spanText(a, b)])")
    assert found == [[tight_span(text(a), text(b)), span_text(text(a), text(b))] for a, b in spans]
    assert found[0][0] == "6–9 AM"
    assert found[1][0] == "11:30 AM–1 PM"


@pytest.mark.skipif(NODE is None, reason="Node is not installed")
def test_a_narrow_block_label_steps_down_to_the_start_hour():
    found = run('["7–9 AM", "11:30 AM–1 PM", "10 PM–12 AM"].map(timelineMath.labelSteps)')
    assert found == [["7–9 AM", "7–9", "7"], ["11:30 AM–1 PM", "11:30–1", "11:30"], ["10 PM–12 AM", "10–12", "10"]]
