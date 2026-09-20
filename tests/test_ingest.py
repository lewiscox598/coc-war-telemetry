"""Ingest tests driven by fixtures derived from a real #2U9QLCY8Y capture."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from conftest import OUR_CLAN, at, make_result

from coc_telemetry.ingest import (
    connect,
    derive_result,
    derive_war_id,
    ingest_capture,
    ingest_war,
    parse_api_timestamp,
    rebuild,
    to_iso,
)

TABLES = [
    "wars", "war_members", "attacks", "member_snapshots",
    "player_snapshots", "player_units", "capital_raids", "capital_raid_members",
]


@pytest.fixture
def db(tmp_path: Path) -> sqlite3.Connection:
    conn = connect(tmp_path / "telemetry.db")
    yield conn
    conn.close()


def dump(conn: sqlite3.Connection) -> dict[str, list[tuple]]:
    """Every row in every table, ordered, for equality comparison."""
    return {
        table: sorted(tuple(r) for r in conn.execute(f"SELECT * FROM {table}"))
        for table in TABLES
    }


# --- Timestamps ----------------------------------------------------------


def test_parses_supercell_basic_format_timestamps() -> None:
    """The API returns ISO-8601 basic format, which fromisoformat cannot read."""
    parsed = parse_api_timestamp("20260920T144039.000Z")
    assert parsed.isoformat() == "2026-09-20T14:40:39+00:00"


def test_stored_timestamps_sort_lexically() -> None:
    early = to_iso("20260920T144039.000Z")
    late = to_iso("20260921T134039.000Z")
    assert early < late


# --- War identity --------------------------------------------------------


def test_war_id_is_stable_across_state_changes(db, fixture_json) -> None:
    """state changes across a war's life, so it must not contribute to the key."""
    ids = {
        ingest_war(db, fixture_json(name), at(h), OUR_CLAN)
        for name, h in [
            ("war_preparation", 10),
            ("war_in_war_3_attacks", 15),
            ("war_in_war_11_attacks", 20),
            ("war_ended", 23),
        ]
    }
    assert len(ids) == 1, "one war must produce one war_id across every state"
    assert db.execute("SELECT COUNT(*) FROM wars").fetchone()[0] == 1


def test_war_id_differs_for_a_rematch() -> None:
    """Same opponents, later preparation start, therefore a different war."""
    a = derive_war_id("#2U9QLCY8Y", "#2C8QVRLPU", "2026-09-20T14:40:39+00:00")
    b = derive_war_id("#2U9QLCY8Y", "#2C8QVRLPU", "2026-09-27T14:40:39+00:00")
    assert a != b


# --- State handling ------------------------------------------------------


def test_not_in_war_is_skipped(db, fixture_json) -> None:
    """notInWar carries no other fields at all, so there is nothing to record."""
    assert ingest_war(db, fixture_json("war_not_in_war"), at(10), OUR_CLAN) is None
    assert db.execute("SELECT COUNT(*) FROM wars").fetchone()[0] == 0


def test_preparation_records_roster_but_no_attacks(db, fixture_json) -> None:
    """Members have no 'attacks' key at all during preparation."""
    ingest_war(db, fixture_json("war_preparation"), at(10), OUR_CLAN)
    assert db.execute("SELECT COUNT(*) FROM attacks").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM war_members").fetchone()[0] == 10
    assert db.execute("SELECT state FROM wars").fetchone()[0] == "preparation"


def test_war_ended_derives_a_result(db, fixture_json) -> None:
    """currentwar has no result field; it is computed at warEnded."""
    ingest_war(db, fixture_json("war_ended"), at(23), OUR_CLAN)
    row = db.execute("SELECT state, result FROM wars").fetchone()
    assert row["state"] == "warEnded"
    assert row["result"] in {"win", "lose", "tie"}


def test_result_breaks_star_ties_on_destruction() -> None:
    assert derive_result(10, 10, 95.5, 92.0) == "win"
    assert derive_result(10, 10, 90.0, 92.0) == "lose"
    assert derive_result(10, 10, 90.0, 90.0) == "tie"
    assert derive_result(11, 10, 50.0, 99.0) == "win"


# --- Idempotency and gaps: the two properties the whole design rests on ---


def test_repeated_polls_produce_no_duplicate_attacks(db, fixture_json) -> None:
    """Every poll returns the full attack list to date, not a delta."""
    payload = fixture_json("war_in_war_11_attacks")
    for hour in range(10, 20):
        ingest_war(db, payload, at(hour), OUR_CLAN)

    assert db.execute("SELECT COUNT(*) FROM attacks").fetchone()[0] == 11
    assert db.execute("SELECT COUNT(DISTINCT order_num) FROM attacks").fetchone()[0] == 11


def test_a_polling_gap_backfills_every_missed_attack(db, fixture_json) -> None:
    """Missed polls self-heal because each response is cumulative, not incremental."""
    ingest_war(db, fixture_json("war_in_war_3_attacks"), at(14), OUR_CLAN)
    assert db.execute("SELECT COUNT(*) FROM attacks").fetchone()[0] == 3

    # Simulate several hours of failed runs, then one successful poll.
    ingest_war(db, fixture_json("war_in_war_11_attacks"), at(19), OUR_CLAN)

    orders = [r[0] for r in db.execute("SELECT order_num FROM attacks ORDER BY order_num")]
    assert orders == list(range(1, 12)), "every attack in the gap must be backfilled"


def test_first_seen_is_never_overwritten_by_a_later_poll(db, fixture_json) -> None:
    """first_seen is our only proxy for when an attack happened."""
    ingest_war(db, fixture_json("war_in_war_3_attacks"), at(14), OUR_CLAN)
    original = db.execute("SELECT first_seen FROM attacks WHERE order_num = 1").fetchone()[0]

    ingest_war(db, fixture_json("war_in_war_11_attacks"), at(19), OUR_CLAN)
    after = db.execute("SELECT first_seen FROM attacks WHERE order_num = 1").fetchone()[0]

    assert after == original
    late = db.execute("SELECT first_seen FROM attacks WHERE order_num = 11").fetchone()[0]
    assert late > original, "attacks seen only in the later poll carry the later time"


def test_war_first_seen_tracks_earliest_and_last_seen_latest(db, fixture_json) -> None:
    ingest_war(db, fixture_json("war_in_war_11_attacks"), at(19), OUR_CLAN)
    ingest_war(db, fixture_json("war_preparation"), at(10), OUR_CLAN)  # out of order
    row = db.execute("SELECT first_seen, last_seen FROM wars").fetchone()
    assert row["first_seen"] < row["last_seen"]


# --- CWL -----------------------------------------------------------------


def test_cwl_round_is_keyed_on_its_war_tag(db, fixture_json) -> None:
    war_id = ingest_war(
        db, fixture_json("cwl_round"), at(15), OUR_CLAN, war_tag="#2PP0JCCL"
    )
    row = db.execute("SELECT * FROM wars").fetchone()
    assert war_id == "#2PP0JCCL"
    assert row["war_type"] == "cwl"
    assert row["war_tag"] == "#2PP0JCCL"


def test_cwl_wars_have_no_attacks_per_member(db, fixture_json) -> None:
    """Wars from the leagues path omit the field entirely."""
    ingest_war(db, fixture_json("cwl_round"), at(15), OUR_CLAN, war_tag="#2PP0JCCL")
    assert db.execute("SELECT attacks_per_member FROM wars").fetchone()[0] is None


def test_cwl_round_between_other_clans_is_skipped(db, fixture_json) -> None:
    """A league group lists every round war, including ones we are not in."""
    result = ingest_war(
        db, fixture_json("cwl_round"), at(15), "#NOTOURCLAN", war_tag="#2PP0JCCL"
    )
    assert result is None
    assert db.execute("SELECT COUNT(*) FROM wars").fetchone()[0] == 0


def test_our_clan_is_oriented_correctly_when_listed_as_opponent(db, fixture_json) -> None:
    """In a CWL round our clan may appear on either side of the payload."""
    payload = fixture_json("cwl_round")
    payload["clan"], payload["opponent"] = payload["opponent"], payload["clan"]

    ingest_war(db, payload, at(15), OUR_CLAN, war_tag="#2PP0JCCL")
    row = db.execute("SELECT clan_tag, opponent_tag FROM wars").fetchone()
    assert row["clan_tag"] == OUR_CLAN
    assert row["opponent_tag"] != OUR_CLAN


# --- Degradation ---------------------------------------------------------


def test_private_war_log_capture_is_skipped_not_fatal(db, store, fixture_json) -> None:
    store.write(make_result("warlog", fixture_json("warlog_private_403"), at(3), status=403))
    capture = next(store.iter_captures())

    assert capture.is_error
    ingest_capture(db, capture, OUR_CLAN)  # must not raise
    assert db.execute("SELECT COUNT(*) FROM wars").fetchone()[0] == 0


# --- The hard invariant --------------------------------------------------


def test_rebuild_from_raw_reproduces_the_database(tmp_path, store, fixture_json) -> None:
    """Rebuild must reconstruct every row from data/raw alone.

    The derived schema is disposable; the archive is not. This is the test that
    makes that claim true rather than aspirational.
    """
    timeline = [
        ("currentwar", "war_preparation", at(10, 0, day=20)),
        ("currentwar", "war_in_war_3_attacks", at(14, 0)),
        ("currentwar", "war_in_war_11_attacks", at(19, 0)),
        ("currentwar", "war_ended", at(23, 0)),
        ("warlog", "warlog_private_403", at(3, 0)),
    ]
    for endpoint, name, when in timeline:
        status = 403 if "403" in name else 200
        store.write(make_result(endpoint, fixture_json(name), when, status=status))

    # Path A: fold captures forward incrementally, as the poller does.
    incremental_path = tmp_path / "incremental.db"
    conn = connect(incremental_path)
    for capture in store.iter_captures():
        ingest_capture(conn, capture, OUR_CLAN)
    conn.commit()
    expected = dump(conn)
    conn.close()

    # Path B: drop everything and rebuild from raw.
    rebuilt_path = tmp_path / "rebuilt.db"
    counts = rebuild(rebuilt_path, store, OUR_CLAN)
    conn = connect(rebuilt_path)
    actual = dump(conn)
    conn.close()

    assert counts["captures"] == 4
    assert counts["skipped"] == 1, "the 403 war log is archived but not ingested"
    for table in TABLES:
        assert actual[table] == expected[table], f"{table} diverged after rebuild"
    assert expected["attacks"], "the comparison would be vacuous with no attacks"


def test_rebuild_is_idempotent(tmp_path, store, fixture_json) -> None:
    store.write(make_result("currentwar", fixture_json("war_ended"), at(23)))
    db_path = tmp_path / "telemetry.db"

    rebuild(db_path, store, OUR_CLAN)
    conn = connect(db_path)
    first = dump(conn)
    conn.close()

    rebuild(db_path, store, OUR_CLAN)
    conn = connect(db_path)
    second = dump(conn)
    conn.close()

    assert first == second
