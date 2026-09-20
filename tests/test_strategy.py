"""War-planning tests: expectation evidence tiers, assignment, army fit."""

from __future__ import annotations

import pytest
from conftest import OUR_CLAN, at

from coc_telemetry.ingest import connect, ingest_player, ingest_war
from coc_telemetry.metrics import apply_views
from coc_telemetry.strategy import (
    ARMIES,
    army_options,
    baseline_stars,
    cleanup_board,
    expectation,
    recommend_assignments,
)


@pytest.fixture
def db(tmp_path, fixture_json):
    conn = connect(tmp_path / "telemetry.db")
    ingest_war(conn, fixture_json("war_ended"), at(23), OUR_CLAN)
    apply_views(conn)
    yield conn
    conn.close()


@pytest.fixture
def war_id(db):
    return db.execute("SELECT war_id FROM wars").fetchone()[0]


# --- Baseline ------------------------------------------------------------


def test_attacking_down_is_easier_than_mirror_which_beats_attacking_up() -> None:
    assert baseline_stars(12, 10) > baseline_stars(12, 12) > baseline_stars(12, 13)


def test_extreme_mismatches_are_clamped_not_extrapolated() -> None:
    """A TH4 against a TH13 is hopeless, but no more hopeless than -3."""
    assert baseline_stars(4, 13) == baseline_stars(10, 13)
    assert baseline_stars(13, 4) == baseline_stars(13, 10)


# --- Evidence tiers ------------------------------------------------------


def test_no_history_falls_back_to_baseline_and_says_so(db) -> None:
    exp = expectation(db, "#NOBODY", 11, 11)
    assert exp.confidence == "baseline"
    assert exp.stars == baseline_stars(11, 11)
    assert "differential only" in exp.basis


def test_enough_history_overrides_the_baseline(db, fixture_json) -> None:
    """A member's measured record is stronger evidence than a rule of thumb."""
    tag = db.execute(
        "SELECT attacker_tag FROM attack_values WHERE attacker_side='clan' LIMIT 1"
    ).fetchone()[0]
    row = db.execute(
        "SELECT attacker_th, defender_th FROM attack_values WHERE attacker_tag = ? LIMIT 1",
        (tag,),
    ).fetchone()

    # Give this member a long record at that matchup.
    for order in range(100, 110):
        db.execute(
            "INSERT INTO attacks (war_id, order_num, attacker_tag, defender_tag, stars, "
            "destruction_percentage, duration, first_seen) VALUES "
            "((SELECT war_id FROM wars), ?, ?, ?, 3, 100, 100, '2026-09-21T00:00:00+00:00')",
            (order, tag, f"#FAKE{order}"),
        )
        db.execute(
            "INSERT INTO war_members (war_id, player_tag, side, name, townhall_level) "
            "VALUES ((SELECT war_id FROM wars), ?, 'opponent', 'dummy', ?)",
            (f"#FAKE{order}", row["defender_th"]),
        )
    db.commit()

    exp = expectation(db, tag, row["attacker_th"], row["defender_th"])
    assert exp.confidence == "measured"
    assert "three-stars in" in exp.basis


def test_a_single_attack_is_labelled_partial_not_measured(db) -> None:
    tag = db.execute(
        "SELECT attacker_tag FROM attack_values WHERE attacker_side='clan' "
        "GROUP BY attacker_tag HAVING COUNT(*) = 1 LIMIT 1"
    ).fetchone()
    if tag is None:
        pytest.skip("fixture has no single-attack member")
    row = db.execute(
        "SELECT attacker_th, defender_th FROM attack_values WHERE attacker_tag = ?",
        (tag[0],),
    ).fetchone()
    exp = expectation(db, tag[0], row["attacker_th"], row["defender_th"])
    assert exp.confidence == "partial"


# --- Assignment ----------------------------------------------------------


def test_every_member_gets_exactly_one_target(db, war_id) -> None:
    plan = recommend_assignments(db, war_id)
    attackers = [p["attacker_tag"] for p in plan]
    targets = [p["defender_position"] for p in plan]
    assert len(attackers) == len(set(attackers)), "nobody is assigned twice"
    assert len(targets) == len(set(targets)), "no base is double-booked on first attacks"


def test_assignment_beats_naive_mirror_pairing(db, war_id) -> None:
    """The point of solving it is that it finds plans mirror-pairing misses."""
    plan = recommend_assignments(db, war_id)
    chosen = sum(p["expected_stars"] for p in plan)

    ours = db.execute(
        "SELECT townhall_level FROM war_members WHERE war_id=? AND side='clan' "
        "ORDER BY map_position",
        (war_id,),
    ).fetchall()
    theirs = db.execute(
        "SELECT townhall_level FROM war_members WHERE war_id=? AND side='opponent' "
        "ORDER BY map_position",
        (war_id,),
    ).fetchall()
    mirror = sum(
        baseline_stars(a["townhall_level"], d["townhall_level"])
        for a, d in zip(ours, theirs, strict=False)
    )
    assert chosen >= mirror - 1e-9


def test_hopeless_matchups_are_flagged_not_hidden(db, war_id) -> None:
    """Silently assigning someone an unwinnable base is worse than saying so."""
    plan = recommend_assignments(db, war_id)
    for row in plan:
        assert row["outmatched"] == (row["th_diff"] <= -2)


def test_no_plan_without_a_roster(db) -> None:
    assert recommend_assignments(db, "#NOSUCHWAR") == []


# --- Army fit ------------------------------------------------------------


def _give_player(conn, tag: str, units: dict[str, int], th: int) -> None:
    payload = {
        "tag": tag,
        "name": "tester",
        "townHallLevel": th,
        "troops": [
            {"name": n, "level": v, "maxLevel": 20, "village": "home"}
            for n, v in units.items()
            if "Spell" not in n
        ],
        "spells": [
            {"name": n, "level": v, "maxLevel": 10, "village": "home"}
            for n, v in units.items()
            if "Spell" in n
        ],
        "heroes": [
            {
                "name": "Archer Queen",
                "level": units.get("Archer Queen", 1),
                "maxLevel": 110,
                "village": "home",
            }
        ],
    }
    ingest_player(conn, payload, at(3))
    conn.commit()


def test_an_army_is_only_recommended_if_the_member_can_field_it(db) -> None:
    _give_player(db, "#RUSHED", {"Dragon": 2, "Balloon": 1, "Lightning Spell": 3}, 11)
    options = {a["name"]: a for a in army_options(db, "#RUSHED", 11)}
    zap = options["Zap Dragons"]
    assert not zap["viable"]
    assert any("Lightning Spell is level 3" in b for b in zap["blockers"])


def test_blockers_name_the_specific_unit(db) -> None:
    _give_player(db, "#PARTIAL", {"Dragon": 6, "Balloon": 5, "Lightning Spell": 7}, 11)
    options = {a["name"]: a for a in army_options(db, "#PARTIAL", 11)}
    assert options["Zap Dragons"]["viable"]
    assert any("not unlocked" in b for b in options["Zap Witches"]["blockers"])


def test_hero_requirements_are_enforced(db) -> None:
    """Queen Charge needs a Queen that survives; level 9 does not."""
    _give_player(
        db,
        "#LOWQUEEN",
        {"Hog Rider": 8, "Miner": 8, "Healer": 8, "Archer Queen": 9},
        12,
    )
    options = {a["name"]: a for a in army_options(db, "#LOWQUEEN", 12)}
    qc = options["Queen Charge Hybrid"]
    assert not qc["viable"]
    assert any("Archer Queen is level 9" in b for b in qc["blockers"])


def test_a_rushed_account_gets_a_lower_bracket_fallback(db) -> None:
    """Showing nothing is useless; the strongest fieldable army is a real plan."""
    _give_player(
        db,
        "#VERYRUSHED",
        {
            "Dragon": 3,
            "Balloon": 4,
            "Lightning Spell": 2,
            "Giant": 5,
            "Archer": 5,
            "Wall Breaker": 5,
        },
        12,
    )
    options = army_options(db, "#VERYRUSHED", 12)
    viable = [a for a in options if a["viable"]]
    assert viable, "a fallback army should be offered"
    assert not viable[0]["at_level"]
    assert viable[0]["designed_for"] < 12


def test_every_curated_army_cites_a_source() -> None:
    """A recommendation without a source is an opinion."""
    for army in ARMIES:
        assert army.source.startswith("https://"), army.name
        assert army.source_title, army.name
        assert army.note, army.name


# --- Cleanup board -------------------------------------------------------


def test_cleanup_board_covers_every_enemy_base(db, war_id) -> None:
    board = cleanup_board(db, war_id)
    total = db.execute(
        "SELECT COUNT(*) FROM war_members WHERE war_id=? AND side='opponent'", (war_id,)
    ).fetchone()[0]
    assert len(board) == total


def test_a_three_starred_base_is_marked_done(db, war_id) -> None:
    board = {b["map_position"]: b for b in cleanup_board(db, war_id)}
    cleared = [b for b in board.values() if b["best_stars"] == 3]
    assert cleared, "the fixture three-stars at least one base"
    for b in cleared:
        assert b["state"] == "cleared"
        assert b["stars_available"] == 0


def test_stars_available_never_goes_negative(db, war_id) -> None:
    assert all(0 <= b["stars_available"] <= 3 for b in cleanup_board(db, war_id))


def test_untouched_bases_claim_no_knowledge(db, war_id) -> None:
    for b in cleanup_board(db, war_id):
        if b["attempts"] == 0:
            assert b["state"] == "untouched"
            assert "No information" in b["advice"]
