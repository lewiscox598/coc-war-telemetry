"""Site rendering tests: chart honesty, time formatting, and no key leakage."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import OUR_CLAN, at

from coc_telemetry.ingest import connect, ingest_war
from coc_telemetry.site import (
    build_site,
    format_relative,
    format_when,
    svg_sparkline,
    svg_war_stars,
    war_timing_label,
)

TEMPLATES = Path(__file__).parent.parent / "templates"


# --- Charts --------------------------------------------------------------


def test_sparkline_needs_at_least_two_points() -> None:
    """One point is a dot pretending to be a trend."""
    assert svg_sparkline([]) == ""
    assert svg_sparkline([5]) == ""
    assert svg_sparkline([5, 9]).startswith("<svg")


def test_sparkline_survives_a_flat_series() -> None:
    """A zero range must not divide by zero."""
    assert "<svg" in svg_sparkline([3, 3, 3])


def test_war_chart_needs_two_completed_wars() -> None:
    assert svg_war_stars([]) == ""
    assert svg_war_stars([{"state": "warEnded", "clan_stars": 5, "opponent_stars": 3}]) == ""


def test_war_chart_ignores_wars_still_in_progress() -> None:
    wars = [
        {"state": "inWar", "clan_stars": 4, "opponent_stars": 2, "opponent_name": "A"},
        {"state": "warEnded", "clan_stars": 9, "opponent_stars": 6, "opponent_name": "B"},
    ]
    assert svg_war_stars(wars) == ""


def test_war_chart_renders_two_labelled_series() -> None:
    wars = [
        {"state": "warEnded", "clan_stars": 11, "opponent_stars": 4, "opponent_name": "A"},
        {"state": "warEnded", "clan_stars": 6, "opponent_stars": 12, "opponent_name": "B"},
    ]
    svg = svg_war_stars(wars)
    assert svg.startswith("<svg") and svg.endswith("</svg>")
    assert svg.count("<path") == 2
    assert "#3987e5" in svg and "#d95926" in svg
    assert "<title>" in svg, "hover layer should be present"


def test_end_labels_are_dropped_when_they_would_collide() -> None:
    """Both sides finishing on the same score would stack the labels on top of
    each other; nudging them apart detaches them from their lines."""
    # History arrives newest-first and the chart plots oldest-first, so the
    # rightmost point -- the one carrying the end labels -- is element zero.
    tied = [
        {"state": "warEnded", "clan_stars": 9, "opponent_stars": 9, "opponent_name": "B"},
        {"state": "warEnded", "clan_stars": 8, "opponent_stars": 3, "opponent_name": "A"},
    ]
    separated = [
        {"state": "warEnded", "clan_stars": 14, "opponent_stars": 2, "opponent_name": "B"},
        {"state": "warEnded", "clan_stars": 8, "opponent_stars": 3, "opponent_name": "A"},
    ]
    assert svg_war_stars(tied).count('font-weight="600"') == 0
    assert svg_war_stars(separated).count('font-weight="600"') == 2


def test_chart_escapes_opponent_names() -> None:
    wars = [
        {"state": "warEnded", "clan_stars": 8, "opponent_stars": 3, "opponent_name": "A"},
        {"state": "warEnded", "clan_stars": 9, "opponent_stars": 2, "opponent_name": "<script>"},
    ]
    svg = svg_war_stars(wars)
    assert "<script>" not in svg
    assert "&lt;script&gt;" in svg


# --- Time ----------------------------------------------------------------


def test_times_render_in_europe_london_not_utc() -> None:
    """Stored UTC, displayed London. In September that is BST, one hour ahead."""
    assert format_when("2026-09-21T13:40:39+00:00", "%H:%M") == "14:40"


def test_relative_time_reads_naturally() -> None:
    now = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    assert format_relative("2026-09-20T15:00:00+00:00", now=now) == "in 3h"
    assert format_relative("2026-09-22T12:00:00+00:00", now=now) == "in 2d 0h"
    assert format_relative("2026-09-20T09:00:00+00:00", now=now) == "3h ago"


def test_war_timing_label_leads_with_what_matters_per_phase() -> None:
    prep = {"state": "preparation", "start_time": "2026-09-21T13:40:39+00:00", "end_time": None}
    assert "Battle day starts" in war_timing_label(prep)
    live = {"state": "inWar", "start_time": None, "end_time": "2026-09-22T13:40:39+00:00"}
    assert war_timing_label(live).startswith("Ends")


# --- Build ---------------------------------------------------------------


@pytest.fixture
def built(tmp_path, fixture_json):
    conn = connect(tmp_path / "telemetry.db")
    ingest_war(conn, fixture_json("war_ended"), at(23), OUR_CLAN)
    out = tmp_path / "site"
    build_site(conn, out, templates=TEMPLATES, clan_name="Sunbury Massive", player_tag="#202VL9GR")
    conn.close()
    return out


def test_builds_both_pages(built) -> None:
    assert (built / "index.html").is_file()
    assert (built / "me" / "index.html").is_file()
    assert (built / "style.css").is_file()


def test_pages_carry_the_required_attribution(built) -> None:
    for page in ["index.html", "me/index.html"]:
        html = (built / page).read_text(encoding="utf-8")
        assert "not endorsed by Supercell" in html


def test_no_token_or_api_host_reaches_the_client(built) -> None:
    """The key must never be in the output, and the page must not call the API."""
    for page in ["index.html", "me/index.html"]:
        html = (built / page).read_text(encoding="utf-8")
        assert "COC_API_TOKEN" not in html
        assert "eyJ" not in html
        assert "royaleapi" not in html
        assert "clashofclans.com" not in html
        assert "fetch(" not in html
        assert "XMLHttpRequest" not in html


def test_builds_cleanly_with_an_empty_database(tmp_path) -> None:
    """The site must render before any war exists, not just after."""
    conn = connect(tmp_path / "empty.db")
    out = tmp_path / "site"
    build_site(conn, out, templates=TEMPLATES, clan_name="Sunbury Massive", player_tag="#202VL9GR")
    conn.close()
    html = (out / "index.html").read_text(encoding="utf-8")
    assert "No war in progress" in html
    assert "not endorsed by Supercell" in html
