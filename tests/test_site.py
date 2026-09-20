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


# --- War Room primitives -------------------------------------------------


def test_star_row_fills_only_what_was_earned() -> None:
    from coc_telemetry.site import star_row

    svg = star_row(2)
    assert svg.count('class="on"') == 2
    assert svg.count('class="off"') == 1
    assert 'aria-label="2 of 3 stars"' in svg


def test_star_row_clamps_nonsense_values() -> None:
    from coc_telemetry.site import star_row

    assert star_row(None).count('class="on"') == 0
    assert star_row(-4).count('class="on"') == 0
    assert star_row(99).count('class="on"') == 3


def test_star_row_is_self_contained_svg() -> None:
    from coc_telemetry.site import star_row

    svg = star_row(1)
    assert svg.startswith("<svg") and svg.endswith("</svg>")
    assert svg.count("<g") == svg.count("</g>")


def test_phase_is_derived_from_war_state(tmp_path, fixture_json) -> None:
    """Preparation, battle day and post-war are different products, so the page
    needs to know which one it is rendering."""
    from coc_telemetry.site import gather

    for name, expected in [
        ("war_preparation", "prep"),
        ("war_in_war_11_attacks", "battle"),
        ("war_ended", "ended"),
    ]:
        conn = connect(tmp_path / f"{name}.db")
        ingest_war(conn, fixture_json(name), at(12), OUR_CLAN)
        assert gather(conn, "#202VL9GR")["phase"] == expected
        conn.close()


def test_phase_is_idle_with_no_war(tmp_path) -> None:
    from coc_telemetry.site import gather

    conn = connect(tmp_path / "empty.db")
    ctx = gather(conn, "#202VL9GR")
    assert ctx["phase"] == "idle"
    assert ctx["duels"] == []
    assert ctx["my_duel"] is None
    conn.close()


def test_duels_merge_roster_plan_and_defence(tmp_path, fixture_json) -> None:
    """The map replaced three separate sections; each row must carry all three."""
    from coc_telemetry.site import gather

    conn = connect(tmp_path / "t.db")
    ingest_war(conn, fixture_json("war_ended"), at(23), OUR_CLAN)
    duels = gather(conn, "#202VL9GR")["duels"]
    conn.close()

    assert duels
    for d in duels:
        for key in (
            "attacker",
            "defender",
            "attacks_used",
            "attacks_available",
            "target_stars",
            "target_state",
            "expected_stars",
            "armies",
        ):
            assert key in d, f"{key} missing from duel row"
        assert 0 <= d["target_stars"] <= 3
        assert d["attacks_used"] <= d["attacks_available"]


def test_my_duel_is_picked_out_for_the_orders_card(tmp_path, fixture_json) -> None:
    from coc_telemetry.site import gather

    conn = connect(tmp_path / "t.db")
    ingest_war(conn, fixture_json("war_ended"), at(23), OUR_CLAN)
    tag = conn.execute(
        "SELECT player_tag FROM war_members WHERE war_id=(SELECT war_id FROM wars) "
        "AND side='clan' LIMIT 1"
    ).fetchone()[0]
    ctx = gather(conn, tag)
    conn.close()

    assert ctx["my_duel"] is not None
    assert ctx["my_duel"]["attacker_tag"] == tag
    assert ctx["my_duel"]["is_me"] is True


def test_both_pages_lead_with_orders_during_a_war(tmp_path, fixture_json) -> None:
    conn = connect(tmp_path / "t.db")
    ingest_war(conn, fixture_json("war_preparation"), at(10), OUR_CLAN)
    tag = conn.execute("SELECT player_tag FROM war_members WHERE side='clan' LIMIT 1").fetchone()[0]
    out = tmp_path / "site"
    build_site(conn, out, templates=TEMPLATES, clan_name="Sunbury Massive", player_tag=tag)
    conn.close()

    for page in ["index.html", "me/index.html"]:
        assert "Your orders" in (out / page).read_text(encoding="utf-8")


def test_map_shows_who_was_actually_attacked_not_who_was_planned(tmp_path, fixture_json) -> None:
    """An assignment is a plan. Once someone attacks, showing their stars beside a
    base they never touched is simply wrong."""
    from coc_telemetry.site import gather

    conn = connect(tmp_path / "t.db")
    ingest_war(conn, fixture_json("war_ended"), at(23), OUR_CLAN)
    duels = gather(conn, "#202VL9GR")["duels"]

    for d in duels:
        if "first_stars" not in d:
            continue  # member did not attack; the planned target still stands
        real = conn.execute(
            """
            SELECT d.name FROM attacks a
            JOIN war_members d ON d.war_id = a.war_id AND d.player_tag = a.defender_tag
                              AND d.side = 'opponent'
            WHERE a.war_id = a.war_id AND a.attacker_tag = ?
            ORDER BY a.order_num LIMIT 1
            """,
            (d["attacker_tag"],),
        ).fetchone()
        assert d["defender"] == real["name"], (
            f"{d['attacker']} is shown attacking {d['defender']} but really hit {real['name']}"
        )
        # The stars on the row must belong to the attack the row depicts.
        assert d["first_stars"] <= d["target_stars"], (
            "an attack cannot score more than the base's best result"
        )
    conn.close()


def test_off_plan_attacks_are_labelled(tmp_path, fixture_json) -> None:
    from coc_telemetry.site import gather

    conn = connect(tmp_path / "t.db")
    ingest_war(conn, fixture_json("war_ended"), at(23), OUR_CLAN)
    duels = gather(conn, "#202VL9GR")["duels"]
    conn.close()

    off = [d for d in duels if d.get("went_off_plan")]
    assert off, "the fixture has members who attacked off their assignment"
    for d in off:
        assert d["planned_defender"] != d["defender"]
