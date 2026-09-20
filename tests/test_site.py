"""Site rendering tests: chart honesty, time formatting, and no key leakage."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import OUR_CLAN, at

from coc_telemetry.ingest import connect, ingest_war
from coc_telemetry.metrics import apply_views
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
    # Assert the module's own validated steps, and that they are distinct.
    # Hardcoding hexes here broke this test on three legitimate theme changes.
    from coc_telemetry.site import SERIES_1, SERIES_2

    assert SERIES_1 != SERIES_2
    assert SERIES_1 in svg and SERIES_2 in svg
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

    # Assert on the element, not its wording: the label changes with war phase
    # and copy changes should not break a structural guarantee.
    for page in ["index.html", "me/index.html"]:
        html = (out / page).read_text(encoding="utf-8")
        assert 'class="orders"' in html, f"{page} does not lead with the orders block"
        body = html.split("<body", 1)[1]
        assert body.index('class="orders"') < body.index('class="tabs"'), (
            f"{page} shows orders after the tabs"
        )


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


def test_stylesheet_is_cache_busted_on_content(tmp_path, fixture_json) -> None:
    """Pages serves CSS from a long-lived CDN cache. Without a content-keyed
    version, a returning visitor gets new HTML against their old cached CSS and
    the page renders completely broken until they hard-refresh."""
    import re

    counter = iter(range(100))

    def build_with(css: str) -> str:
        # A fresh directory per build: identical CSS must still build twice.
        templates = tmp_path / f"build{next(counter)}"
        templates.mkdir()
        for name in ("base.html", "clan.html", "me.html", "_orders.html"):
            (templates / name).write_text(
                (TEMPLATES / name).read_text(encoding="utf-8"), encoding="utf-8"
            )
        (templates / "style.css").write_text(css, encoding="utf-8")

        conn = connect(templates / "db.sqlite")
        ingest_war(conn, fixture_json("war_ended"), at(23), OUR_CLAN)
        out = templates / "site"
        build_site(conn, out, templates=templates, clan_name="X", player_tag="#202VL9GR")
        conn.close()
        html = (out / "index.html").read_text(encoding="utf-8")
        match = re.search(r"style\.css\?v=([a-f0-9]+)", html)
        assert match, "stylesheet link carries no version"
        return match.group(1)

    first = build_with("body { color: red }")
    again = build_with("body { color: red }")
    changed = build_with("body { color: blue }")

    assert first == again, "identical CSS must keep the same version"
    assert first != changed, "changed CSS must produce a new version"


# --- Replay --------------------------------------------------------------


def test_replay_is_null_without_a_war(tmp_path) -> None:
    from coc_telemetry.site import replay_payload

    conn = connect(tmp_path / "empty.db")
    assert replay_payload(conn, None, "#202VL9GR") == "null"
    conn.close()


def test_replay_carries_every_attack_and_the_full_roster(tmp_path, fixture_json) -> None:
    import json as _json

    from coc_telemetry.site import replay_payload

    conn = connect(tmp_path / "t.db")
    ingest_war(conn, fixture_json("war_ended"), at(23), OUR_CLAN)
    apply_views(conn)
    war = dict(conn.execute("SELECT * FROM wars").fetchone())
    data = _json.loads(replay_payload(conn, war, "#202VL9GR"))

    attacks = conn.execute("SELECT COUNT(*) FROM attacks").fetchone()[0]
    members = conn.execute("SELECT COUNT(*) FROM war_members").fetchone()[0]
    conn.close()

    assert len(data["attacks"]) == attacks
    assert len(data["roster"]) == members
    orders = [a["n"] for a in data["attacks"]]
    assert orders == sorted(orders), "the replay must be in attack order"


def test_replaying_the_payload_reproduces_the_stored_scoreline(tmp_path, fixture_json) -> None:
    """The browser recomputes the score from the event stream. If that drifts from
    what the API reported, the replay is lying, so this mirrors the client's
    scoring rule and checks it against the stored result."""
    import json as _json

    from coc_telemetry.site import replay_payload

    conn = connect(tmp_path / "t.db")
    ingest_war(conn, fixture_json("war_ended"), at(23), OUR_CLAN)
    apply_views(conn)
    war = dict(conn.execute("SELECT * FROM wars").fetchone())
    data = _json.loads(replay_payload(conn, war, "#202VL9GR"))
    conn.close()

    side = {m["tag"]: m["side"] for m in data["roster"]}
    best: dict[str, int] = {}
    for a in data["attacks"]:  # same rule as the page
        best[a["on"]] = max(best.get(a["on"], 0), a["s"])

    ours = sum(s for tag, s in best.items() if side[tag] == "opponent")
    theirs = sum(s for tag, s in best.items() if side[tag] == "clan")

    assert ours == war["clan_stars"], "replayed clan stars diverge from the API's"
    assert theirs == war["opponent_stars"]


def test_replay_score_never_decreases_as_the_war_advances(tmp_path, fixture_json) -> None:
    import json as _json

    from coc_telemetry.site import replay_payload

    conn = connect(tmp_path / "t.db")
    ingest_war(conn, fixture_json("war_ended"), at(23), OUR_CLAN)
    apply_views(conn)
    war = dict(conn.execute("SELECT * FROM wars").fetchone())
    data = _json.loads(replay_payload(conn, war, "#202VL9GR"))
    conn.close()

    side = {m["tag"]: m["side"] for m in data["roster"]}
    previous = (0, 0)
    for t in range(len(data["attacks"]) + 1):
        best: dict[str, int] = {}
        for a in data["attacks"][:t]:
            best[a["on"]] = max(best.get(a["on"], 0), a["s"])
        now = (
            sum(s for g, s in best.items() if side[g] == "opponent"),
            sum(s for g, s in best.items() if side[g] == "clan"),
        )
        assert now[0] >= previous[0] and now[1] >= previous[1], f"score fell at t={t}"
        previous = now


# --- War map -------------------------------------------------------------


def test_map_pairs_by_position_with_no_duplicate_bases(tmp_path, fixture_json) -> None:
    """Pairing by assignment rendered one base twice when two members were sent
    to it. A map shows each base exactly once."""
    from coc_telemetry.site import gather

    conn = connect(tmp_path / "t.db")
    ingest_war(conn, fixture_json("war_ended"), at(23), OUR_CLAN)
    rows = gather(conn, "#202VL9GR")["map_rows"]
    conn.close()

    tags = [m["player_tag"] for r in rows for m in (r["us"], r["them"]) if m]
    assert len(tags) == len(set(tags)), "a base appears more than once on the map"
    for r in rows:
        if r["us"] and r["them"]:
            assert r["us"]["map_position"] == r["them"]["map_position"]


def test_the_page_only_ever_shows_real_war_data(tmp_path, fixture_json) -> None:
    """No substituted or recorded scoreline may reach the page. An earlier build
    swapped in a sample war while the live one sat in preparation, which showed a
    war that never happened."""
    conn = connect(tmp_path / "t.db")
    ingest_war(conn, fixture_json("war_preparation"), at(10), OUR_CLAN)
    out = tmp_path / "site"
    build_site(conn, out, templates=TEMPLATES, clan_name="X", player_tag="#202VL9GR")
    conn.close()

    html = (out / "index.html").read_text(encoding="utf-8")
    assert "sample-note" not in html
    assert "sample war" not in html.lower()
    # A war in preparation genuinely has no attacks, so there is nothing to replay
    # -- but the page must say so rather than silently dropping the section.
    assert 'id="scrub"' not in html
    assert "replay locked" in html
    assert "Unlocks with the first attack" in html


def test_preparation_page_is_not_hollow(tmp_path, fixture_json) -> None:
    """Before a single attack the page still has to be worth opening: the roster,
    the matchups and the army walkthroughs are all live from the start."""
    conn = connect(tmp_path / "t.db")
    ingest_war(conn, fixture_json("war_preparation"), at(10), OUR_CLAN)
    out = tmp_path / "site"
    build_site(conn, out, templates=TEMPLATES, clan_name="X", player_tag="#202VL9GR")
    conn.close()

    html = (out / "index.html").read_text(encoding="utf-8")
    assert html.count('<button type="button" class="tile') == 10, "full map"
    assert 'ol class="steps"' in html, "army walkthroughs"
    assert 'id="base-detail"' in html, "inspector"
    assert "data-countdown" in html, "countdown to battle day"


def test_every_army_carries_a_step_by_step_walkthrough() -> None:
    """Composition alone does not tell anyone how to run the attack."""
    from coc_telemetry.strategy import ARMIES

    for army in ARMIES:
        assert len(army.steps) >= 5, f"{army.name} has too few steps"
        for step in army.steps:
            assert step[0].isupper(), f"{army.name}: step does not read as an instruction"
            assert step.endswith("."), f"{army.name}: step is not a sentence"
        joined = " ".join(army.steps).lower()
        assert "deploy" in joined or "drop" in joined or "send" in joined, (
            f"{army.name}: walkthrough never says what to put down"
        )


def test_walkthrough_reaches_both_the_page_and_the_inspector(tmp_path, fixture_json) -> None:
    import json as _json

    from coc_telemetry.site import replay_payload

    conn = connect(tmp_path / "t.db")
    ingest_war(conn, fixture_json("war_ended"), at(23), OUR_CLAN)
    out = tmp_path / "site"
    build_site(conn, out, templates=TEMPLATES, clan_name="X", player_tag="#202VL9GR")
    apply_views(conn)
    war = dict(conn.execute("SELECT * FROM wars").fetchone())
    payload = _json.loads(replay_payload(conn, war, "#202VL9GR"))
    conn.close()

    assert 'ol class="steps"' in (out / "index.html").read_text(encoding="utf-8")
    armies = [m["army"] for m in payload["roster"] if m.get("army")]
    assert armies and all(a["steps"] for a in armies)


def test_clan_badges_are_read_from_the_archive(tmp_path, fixture_json) -> None:
    """Badges come off the archived war payload, so a site build still needs no
    token and the deploy job never sees the key."""
    from conftest import make_result

    from coc_telemetry.capture import CaptureStore

    store = CaptureStore(tmp_path / "raw")
    payload = fixture_json("war_ended")
    payload["clan"]["badgeUrls"] = {"medium": "https://example.invalid/us.png"}
    payload["opponent"]["badgeUrls"] = {"medium": "https://example.invalid/them.png"}
    store.write(make_result("currentwar", payload, at(23)))

    conn = connect(tmp_path / "t.db")
    ingest_war(conn, payload, at(23), OUR_CLAN)
    out = tmp_path / "site"
    build_site(conn, out, templates=TEMPLATES, clan_name="X", player_tag="#202VL9GR", store=store)
    conn.close()

    html = (out / "index.html").read_text(encoding="utf-8")
    assert "https://example.invalid/us.png" in html
    assert "https://example.invalid/them.png" in html


def test_bases_are_buttons_so_the_map_is_keyboard_reachable(tmp_path, fixture_json) -> None:
    conn = connect(tmp_path / "t.db")
    ingest_war(conn, fixture_json("war_ended"), at(23), OUR_CLAN)
    out = tmp_path / "site"
    build_site(conn, out, templates=TEMPLATES, clan_name="X", player_tag="#202VL9GR")
    conn.close()

    html = (out / "index.html").read_text(encoding="utf-8")
    assert html.count('<button type="button" class="tile') == 10
    assert 'aria-expanded="false"' in html
    assert 'id="base-detail"' in html


# --- Inspector payload ---------------------------------------------------


def _payload(conn, tag="#202VL9GR"):
    import json as _json

    from coc_telemetry.site import replay_payload

    apply_views(conn)
    war = dict(conn.execute("SELECT * FROM wars").fetchone())
    return _json.loads(replay_payload(conn, war, tag))


def test_our_members_carry_context_the_enemy_does_not(tmp_path, fixture_json) -> None:
    """Tapping one of ours and tapping an enemy ask different questions, so the
    payload carries different things for each side."""
    conn = connect(tmp_path / "t.db")
    ingest_war(conn, fixture_json("war_ended"), at(23), OUR_CLAN)
    data = _payload(conn)
    conn.close()

    ours = [m for m in data["roster"] if m["side"] == "clan"]
    theirs = [m for m in data["roster"] if m["side"] == "opponent"]
    assert ours and theirs

    for m in ours:
        assert "heroes" in m and "record" in m, "our members need their own context"
    for m in theirs:
        assert "heroes" not in m, "enemy hero levels are not available from the API"
        assert "from" in m, "an enemy base should name who is assigned to it"


def test_hero_levels_reach_the_inspector(tmp_path, fixture_json) -> None:
    """Hero levels are the evidence behind 'this army is not fieldable', so they
    have to be visible on the member panel."""
    from coc_telemetry.capture import CaptureStore
    from coc_telemetry.ingest import ingest_capture

    conn = connect(tmp_path / "t.db")
    ingest_war(conn, fixture_json("war_ended"), at(23), OUR_CLAN)
    store = CaptureStore(Path("data/raw"))
    for capture in store.iter_captures():
        if capture.endpoint.startswith("player_"):
            ingest_capture(conn, capture, OUR_CLAN)
    conn.commit()
    data = _payload(conn)
    conn.close()

    with_heroes = [m for m in data["roster"] if m.get("heroes")]
    assert with_heroes, "no member carried hero levels"
    assert any("AQ" in m["heroes"] or "BK" in m["heroes"] for m in with_heroes)


def test_payload_embeds_no_attack_derived_figures(tmp_path, fixture_json) -> None:
    """Anything that moves with the scrub must be recomputed in the page, never
    baked in -- otherwise the panel claims things that had not happened yet."""
    conn = connect(tmp_path / "t.db")
    ingest_war(conn, fixture_json("war_ended"), at(23), OUR_CLAN)
    data = _payload(conn)
    conn.close()

    for m in data["roster"]:
        for banned in ("stars", "attacksUsed", "starsConceded", "verdict", "best"):
            assert banned not in m, f"{banned} is scrub-dependent and must not be embedded"
