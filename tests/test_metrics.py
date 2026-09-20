"""Metric tests, especially the two calculations most easily got wrong:
value-added stars, and donation deltas across a season reset."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import OUR_CLAN, at

from coc_telemetry.ingest import connect, ingest_war
from coc_telemetry.metrics import (
    apply_views,
    donation_history,
    matchup_breakdown,
    member_performance,
    passivity,
)


@pytest.fixture
def db(tmp_path: Path, fixture_json):
    conn = connect(tmp_path / "telemetry.db")
    ingest_war(conn, fixture_json("war_ended"), at(23), OUR_CLAN)
    apply_views(conn)
    yield conn
    conn.close()


def values(conn) -> dict[int, dict]:
    return {r["order_num"]: dict(r) for r in conn.execute("SELECT * FROM attack_values")}


# --- Value added (schema decision B) -------------------------------------


def test_first_attack_on_a_base_is_worth_its_full_stars(db) -> None:
    row = values(db)[1]
    assert row["stars"] == 3
    assert row["prior_best_stars"] == 0
    assert row["new_stars"] == 3


def test_cleanup_is_credited_only_the_stars_it_adds(db) -> None:
    """Order 3 took a base to 2 stars; order 7 three-stars the same base.

    The cleanup is worth 1 new star, not 3. Using bestOpponentAttack here would
    credit it 0, because by war's end that field already reflects the 3-star.
    """
    rows = values(db)
    assert rows[3]["stars"] == 2 and rows[3]["new_stars"] == 2
    cleanup = rows[7]
    assert cleanup["defender_tag"] == rows[3]["defender_tag"]
    assert cleanup["stars"] == 3
    assert cleanup["prior_best_stars"] == 2
    assert cleanup["new_stars"] == 1


def test_a_worse_repeat_attack_adds_nothing(db) -> None:
    """Order 4 took a base to 1 star, order 10 three-starred it: worth 2, not 3."""
    rows = values(db)
    assert rows[10]["prior_best_stars"] == 1
    assert rows[10]["new_stars"] == 2


def test_new_stars_are_never_negative(db) -> None:
    assert all(r["new_stars"] >= 0 for r in values(db).values())


def test_new_stars_sum_to_the_war_scoreline(db) -> None:
    """Value added across a side must equal that side's reported star total."""
    total = db.execute(
        "SELECT SUM(new_stars) FROM attack_values WHERE attacker_side = 'clan'"
    ).fetchone()[0]
    reported = db.execute("SELECT clan_stars FROM wars").fetchone()[0]
    assert total == reported


# --- Usage ---------------------------------------------------------------


def test_attacks_used_against_available(db) -> None:
    rows = {r["name"]: r for r in member_performance(db)}
    assert rows, "every clan member should appear"
    for row in rows.values():
        assert row["attacks_available"] == 2
        assert row["attacks_used"] + row["attacks_missed"] == 2


def test_members_who_never_attacked_still_appear(db) -> None:
    """A missed attack is invisible in the attacks table, so usage is built from
    the roster instead."""
    idle = [r for r in member_performance(db) if r["attacks_used"] == 0]
    assert idle, "the fixture includes a member who did not attack"
    assert all(r["attacks_missed"] == 2 for r in idle)


def test_matchup_breakdown_classifies_direction(db) -> None:
    attacker = db.execute(
        "SELECT attacker_tag FROM attack_values WHERE attacker_side='clan' LIMIT 1"
    ).fetchone()[0]
    rows = matchup_breakdown(db, attacker)
    assert rows
    assert all(r["matchup"] in {"up", "down", "mirror"} for r in rows)


# --- Donations -----------------------------------------------------------


def seed_donations(conn, series: list[tuple[str, int]]) -> None:
    for date, donations in series:
        conn.execute(
            "INSERT INTO member_snapshots (snapshot_date, player_tag, name, donations) "
            "VALUES (?,?,?,?)",
            (date, "#ABC", "tester", donations),
        )
    conn.commit()


def test_donation_reset_does_not_produce_a_negative_delta(db) -> None:
    """Donations reset to zero each season; a drop is a reset, not a negative."""
    seed_donations(
        db,
        [
            ("2026-09-28", 1200),
            ("2026-09-29", 1450),
            ("2026-10-01", 80),  # season rolled over
            ("2026-10-02", 260),
        ],
    )
    rows = {r["snapshot_date"]: r for r in donation_history(db, "#ABC")}

    assert all(r["donation_delta"] >= 0 for r in rows.values()), "no negative deltas"
    assert rows["2026-09-29"]["donation_delta"] == 250
    assert rows["2026-10-01"]["donation_delta"] == 80, "post-reset value is the delta"
    assert rows["2026-10-01"]["season_reset"] == 1
    assert rows["2026-10-02"]["donation_delta"] == 180
    assert rows["2026-10-02"]["season_reset"] == 0


def test_first_ever_snapshot_has_a_zero_delta(db) -> None:
    seed_donations(db, [("2026-09-01", 500)])
    row = donation_history(db, "#ABC")[0]
    assert row["donation_delta"] == 0
    assert row["season_reset"] == 0


def test_a_flat_donation_count_is_a_zero_delta(db) -> None:
    seed_donations(db, [("2026-09-01", 500), ("2026-09-02", 500)])
    rows = {r["snapshot_date"]: r for r in donation_history(db, "#ABC")}
    assert rows["2026-09-02"]["donation_delta"] == 0
    assert rows["2026-09-02"]["season_reset"] == 0


# --- Passivity -----------------------------------------------------------


def test_passivity_needs_history_before_it_reports(db) -> None:
    """With one war there is nothing to compare, and a false accusation of
    slacking is worse than no signal at all."""
    assert passivity(db) == []
