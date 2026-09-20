"""Derived SQLite store, built by folding raw captures forward.

Hard invariant: ``rebuild`` must reconstruct this database from ``data/raw`` alone.
Nothing here may depend on the wall clock or on ingest order beyond the chronology
encoded in capture filenames.

Two API quirks drive most of the parsing code, both observed live rather than
assumed:

* Supercell returns timestamps in ISO-8601 *basic* format (``20260920T144039.000Z``),
  which ``datetime.fromisoformat`` cannot parse.
* War member objects are sparse. During preparation a member has no ``attacks`` and
  no ``bestOpponentAttack`` key at all; they appear as the war progresses.
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from coc_telemetry.capture import Capture, CaptureStore

API_TIMESTAMP_FORMAT: Final = "%Y%m%dT%H%M%S.%fZ"

SCHEMA: Final = """
CREATE TABLE IF NOT EXISTS wars (
    war_id              TEXT PRIMARY KEY,
    war_type            TEXT NOT NULL CHECK (war_type IN ('regular', 'cwl')),
    war_tag             TEXT,
    clan_tag            TEXT NOT NULL,
    opponent_tag        TEXT NOT NULL,
    opponent_name       TEXT,
    team_size           INTEGER,
    attacks_per_member  INTEGER,
    battle_modifier     TEXT,
    preparation_start   TEXT,
    start_time          TEXT,
    end_time            TEXT,
    state               TEXT NOT NULL,
    result              TEXT,
    clan_stars          INTEGER,
    opponent_stars      INTEGER,
    clan_destruction    REAL,
    opponent_destruction REAL,
    first_seen          TEXT NOT NULL,
    last_seen           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS war_members (
    war_id                      TEXT NOT NULL,
    player_tag                  TEXT NOT NULL,
    side                        TEXT NOT NULL CHECK (side IN ('clan', 'opponent')),
    name                        TEXT,
    map_position                INTEGER,
    townhall_level              INTEGER,
    opponent_attacks            INTEGER,
    best_opponent_attack_order  INTEGER,
    PRIMARY KEY (war_id, player_tag, side)
);

CREATE TABLE IF NOT EXISTS attacks (
    war_id                  TEXT NOT NULL,
    order_num               INTEGER NOT NULL,
    attacker_tag            TEXT NOT NULL,
    defender_tag            TEXT NOT NULL,
    stars                   INTEGER NOT NULL,
    destruction_percentage  REAL NOT NULL,
    duration                INTEGER,
    first_seen              TEXT NOT NULL,
    PRIMARY KEY (war_id, order_num)
);

CREATE TABLE IF NOT EXISTS member_snapshots (
    snapshot_date       TEXT NOT NULL,
    player_tag          TEXT NOT NULL,
    name                TEXT,
    role                TEXT,
    town_hall_level     INTEGER,
    exp_level           INTEGER,
    trophies            INTEGER,
    donations           INTEGER,
    donations_received  INTEGER,
    war_stars           INTEGER,
    clan_rank           INTEGER,
    PRIMARY KEY (snapshot_date, player_tag)
);

CREATE TABLE IF NOT EXISTS player_snapshots (
    snapshot_date           TEXT NOT NULL,
    player_tag              TEXT NOT NULL,
    name                    TEXT,
    town_hall_level         INTEGER,
    town_hall_weapon_level  INTEGER,
    exp_level               INTEGER,
    trophies                INTEGER,
    best_trophies           INTEGER,
    war_stars               INTEGER,
    attack_wins             INTEGER,
    defense_wins            INTEGER,
    donations               INTEGER,
    donations_received      INTEGER,
    clan_tag                TEXT,
    clan_name               TEXT,
    role                    TEXT,
    war_preference          TEXT,
    PRIMARY KEY (snapshot_date, player_tag)
);

CREATE TABLE IF NOT EXISTS player_units (
    snapshot_date   TEXT NOT NULL,
    player_tag      TEXT NOT NULL,
    category        TEXT NOT NULL,
    name            TEXT NOT NULL,
    village         TEXT NOT NULL,
    level           INTEGER,
    max_level       INTEGER,
    PRIMARY KEY (snapshot_date, player_tag, category, name, village)
);

CREATE TABLE IF NOT EXISTS capital_raids (
    clan_tag            TEXT NOT NULL,
    start_time          TEXT NOT NULL,
    end_time            TEXT,
    capital_total_loot  INTEGER,
    raids_completed     INTEGER,
    total_attacks       INTEGER,
    districts_destroyed INTEGER,
    offensive_reward    INTEGER,
    defensive_reward    INTEGER,
    PRIMARY KEY (clan_tag, start_time)
);

CREATE TABLE IF NOT EXISTS capital_raid_members (
    clan_tag                 TEXT NOT NULL,
    start_time               TEXT NOT NULL,
    player_tag               TEXT NOT NULL,
    name                     TEXT,
    attacks                  INTEGER,
    attack_limit             INTEGER,
    bonus_attack_limit       INTEGER,
    capital_resources_looted INTEGER,
    PRIMARY KEY (clan_tag, start_time, player_tag)
);

CREATE INDEX IF NOT EXISTS idx_attacks_attacker ON attacks (attacker_tag);
CREATE INDEX IF NOT EXISTS idx_attacks_defender ON attacks (defender_tag);
CREATE INDEX IF NOT EXISTS idx_war_members_player ON war_members (player_tag);
CREATE INDEX IF NOT EXISTS idx_wars_state ON wars (state);
CREATE INDEX IF NOT EXISTS idx_member_snapshots_player ON member_snapshots (player_tag);
"""


# --- Timestamps ----------------------------------------------------------


def parse_api_timestamp(value: str) -> datetime:
    """Parse Supercell's ISO-8601 basic format into an aware UTC datetime."""
    return datetime.strptime(value, API_TIMESTAMP_FORMAT).replace(tzinfo=UTC)


def to_iso(value: str | datetime | None) -> str | None:
    """Normalise to extended ISO-8601 UTC, which sorts correctly as SQLite text."""
    if value is None:
        return None
    moment = parse_api_timestamp(value) if isinstance(value, str) else value
    return moment.astimezone(UTC).isoformat()


def snapshot_date(moment: datetime) -> str:
    return moment.astimezone(UTC).date().isoformat()


# --- Identity ------------------------------------------------------------


def derive_war_id(clan_tag: str, opponent_tag: str, preparation_start: str) -> str:
    """Stable id for a regular war.

    Keyed on the pairing plus preparation start, never on state: state changes across
    the war's life and would otherwise fragment one war across several rows.
    """
    material = f"{clan_tag}|{opponent_tag}|{preparation_start}".encode()
    return hashlib.sha256(material).hexdigest()[:16]


def derive_result(
    clan_stars: int, opponent_stars: int, clan_destruction: float, opponent_destruction: float
) -> str:
    """Win/lose/tie from the final scoreline.

    The currentwar endpoint carries no ``result`` field -- only war log entries do,
    and the log may be private -- so this is computed at warEnded.
    """
    if clan_stars != opponent_stars:
        return "win" if clan_stars > opponent_stars else "lose"
    if clan_destruction != opponent_destruction:
        return "win" if clan_destruction > opponent_destruction else "lose"
    return "tie"


# --- Connection ----------------------------------------------------------


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


# --- War ingestion -------------------------------------------------------


def _orient(data: dict[str, Any], our_clan_tag: str) -> tuple[dict, dict] | None:
    """Return (us, them). In a CWL round our clan may be either side."""
    clan, opponent = data.get("clan", {}), data.get("opponent", {})
    if clan.get("tag") == our_clan_tag:
        return clan, opponent
    if opponent.get("tag") == our_clan_tag:
        return opponent, clan
    return None


def ingest_war(
    conn: sqlite3.Connection,
    data: dict[str, Any],
    captured_at: datetime,
    our_clan_tag: str,
    *,
    war_tag: str | None = None,
) -> str | None:
    """Fold one war payload into the database. Returns the war_id, or None if skipped.

    Returns None for notInWar (which carries no other fields at all) and for CWL
    rounds that do not involve our clan.
    """
    state = data.get("state")
    if state in (None, "notInWar"):
        return None

    sides = _orient(data, our_clan_tag)
    if sides is None:
        return None  # a CWL round between two other clans
    us, them = sides

    preparation_start = to_iso(data.get("preparationStartTime"))
    if war_tag is not None:
        war_id, war_type = war_tag, "cwl"
    else:
        war_id = derive_war_id(us.get("tag", ""), them.get("tag", ""), preparation_start or "")
        war_type = "regular"

    seen = to_iso(captured_at)
    clan_stars, opp_stars = us.get("stars", 0), them.get("stars", 0)
    clan_dest = us.get("destructionPercentage", 0.0)
    opp_dest = them.get("destructionPercentage", 0.0)
    result = (
        derive_result(clan_stars, opp_stars, clan_dest, opp_dest) if state == "warEnded" else None
    )

    conn.execute(
        """
        INSERT INTO wars (
            war_id, war_type, war_tag, clan_tag, opponent_tag, opponent_name,
            team_size, attacks_per_member, battle_modifier, preparation_start,
            start_time, end_time, state, result, clan_stars, opponent_stars,
            clan_destruction, opponent_destruction, first_seen, last_seen
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT (war_id) DO UPDATE SET
            state = excluded.state,
            result = COALESCE(excluded.result, wars.result),
            team_size = excluded.team_size,
            clan_stars = excluded.clan_stars,
            opponent_stars = excluded.opponent_stars,
            clan_destruction = excluded.clan_destruction,
            opponent_destruction = excluded.opponent_destruction,
            end_time = COALESCE(excluded.end_time, wars.end_time),
            -- first_seen is the earliest observation, which matters because captures
            -- may be folded in any order during a rebuild.
            first_seen = MIN(wars.first_seen, excluded.first_seen),
            last_seen = MAX(wars.last_seen, excluded.last_seen)
        """,
        (
            war_id,
            war_type,
            war_tag,
            us.get("tag"),
            them.get("tag"),
            them.get("name"),
            data.get("teamSize"),
            data.get("attacksPerMember"),
            data.get("battleModifier"),
            preparation_start,
            to_iso(data.get("startTime")),
            to_iso(data.get("endTime")),
            state,
            result,
            clan_stars,
            opp_stars,
            clan_dest,
            opp_dest,
            seen,
            seen,
        ),
    )

    for side, roster in (("clan", us), ("opponent", them)):
        for member in roster.get("members", []) or []:
            _ingest_war_member(conn, war_id, side, member)
            # 'attacks' is absent entirely during preparation, not merely empty.
            for attack in member.get("attacks", []) or []:
                _ingest_attack(conn, war_id, attack, seen)

    return war_id


def _ingest_war_member(
    conn: sqlite3.Connection, war_id: str, side: str, member: dict[str, Any]
) -> None:
    best = member.get("bestOpponentAttack") or {}
    conn.execute(
        """
        INSERT INTO war_members (
            war_id, player_tag, side, name, map_position, townhall_level,
            opponent_attacks, best_opponent_attack_order
        ) VALUES (?,?,?,?,?,?,?,?)
        ON CONFLICT (war_id, player_tag, side) DO UPDATE SET
            name = excluded.name,
            map_position = excluded.map_position,
            townhall_level = excluded.townhall_level,
            opponent_attacks = excluded.opponent_attacks,
            best_opponent_attack_order = excluded.best_opponent_attack_order
        """,
        (
            war_id,
            member.get("tag"),
            side,
            member.get("name"),
            member.get("mapPosition"),
            member.get("townhallLevel"),
            member.get("opponentAttacks"),
            best.get("order"),
        ),
    )


def _ingest_attack(
    conn: sqlite3.Connection, war_id: str, attack: dict[str, Any], seen: str | None
) -> None:
    """Insert an attack, never overwriting one already recorded.

    Every poll returns the full attack list to date rather than a delta, so the same
    attack arrives repeatedly. (war_id, order) is a safe idempotency key because
    order is a monotonic per-war sequence. DO NOTHING preserves the original
    first_seen, which is our only proxy for when the attack actually happened.
    """
    conn.execute(
        """
        INSERT INTO attacks (
            war_id, order_num, attacker_tag, defender_tag, stars,
            destruction_percentage, duration, first_seen
        ) VALUES (?,?,?,?,?,?,?,?)
        ON CONFLICT (war_id, order_num) DO NOTHING
        """,
        (
            war_id,
            attack.get("order"),
            attack.get("attackerTag"),
            attack.get("defenderTag"),
            attack.get("stars"),
            attack.get("destructionPercentage"),
            attack.get("duration"),
            seen,
        ),
    )


# --- Snapshot ingestion --------------------------------------------------


def ingest_clan_members(
    conn: sqlite3.Connection, data: dict[str, Any], captured_at: datetime
) -> int:
    """Fold a clan members list. warStars is absent from ClanMember, so it stays NULL
    here and is filled by ingest_player for members we also poll individually."""
    date = snapshot_date(captured_at)
    rows = data.get("items", []) or []
    for member in rows:
        conn.execute(
            """
            INSERT INTO member_snapshots (
                snapshot_date, player_tag, name, role, town_hall_level, exp_level,
                trophies, donations, donations_received, war_stars, clan_rank
            ) VALUES (?,?,?,?,?,?,?,?,?,NULL,?)
            ON CONFLICT (snapshot_date, player_tag) DO UPDATE SET
                name = excluded.name, role = excluded.role,
                town_hall_level = excluded.town_hall_level,
                exp_level = excluded.exp_level, trophies = excluded.trophies,
                donations = excluded.donations,
                donations_received = excluded.donations_received,
                clan_rank = excluded.clan_rank
            """,
            (
                date,
                member.get("tag"),
                member.get("name"),
                member.get("role"),
                member.get("townHallLevel"),
                member.get("expLevel"),
                member.get("trophies"),
                member.get("donations"),
                member.get("donationsReceived"),
                member.get("clanRank"),
            ),
        )
    return len(rows)


def ingest_player(conn: sqlite3.Connection, data: dict[str, Any], captured_at: datetime) -> None:
    date = snapshot_date(captured_at)
    tag = data.get("tag")
    clan = data.get("clan") or {}

    conn.execute(
        """
        INSERT INTO player_snapshots (
            snapshot_date, player_tag, name, town_hall_level, town_hall_weapon_level,
            exp_level, trophies, best_trophies, war_stars, attack_wins, defense_wins,
            donations, donations_received, clan_tag, clan_name, role, war_preference
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT (snapshot_date, player_tag) DO UPDATE SET
            name = excluded.name, town_hall_level = excluded.town_hall_level,
            town_hall_weapon_level = excluded.town_hall_weapon_level,
            exp_level = excluded.exp_level, trophies = excluded.trophies,
            best_trophies = excluded.best_trophies, war_stars = excluded.war_stars,
            attack_wins = excluded.attack_wins, defense_wins = excluded.defense_wins,
            donations = excluded.donations,
            donations_received = excluded.donations_received,
            clan_tag = excluded.clan_tag, clan_name = excluded.clan_name,
            role = excluded.role, war_preference = excluded.war_preference
        """,
        (
            date,
            tag,
            data.get("name"),
            data.get("townHallLevel"),
            data.get("townHallWeaponLevel"),
            data.get("expLevel"),
            data.get("trophies"),
            data.get("bestTrophies"),
            data.get("warStars"),
            data.get("attackWins"),
            data.get("defenseWins"),
            data.get("donations"),
            data.get("donationsReceived"),
            clan.get("tag"),
            clan.get("name"),
            data.get("role"),
            data.get("warPreference"),
        ),
    )

    # Backfill war_stars onto the same day's clan member row, which ClanMember omits.
    conn.execute(
        "UPDATE member_snapshots SET war_stars = ? WHERE snapshot_date = ? AND player_tag = ?",
        (data.get("warStars"), date, tag),
    )

    for category, key in (
        ("hero", "heroes"),
        ("troop", "troops"),
        ("spell", "spells"),
        ("hero_equipment", "heroEquipment"),
    ):
        for unit in data.get(key, []) or []:
            conn.execute(
                """
                INSERT INTO player_units (
                    snapshot_date, player_tag, category, name, village, level, max_level
                ) VALUES (?,?,?,?,?,?,?)
                ON CONFLICT (snapshot_date, player_tag, category, name, village)
                DO UPDATE SET level = excluded.level, max_level = excluded.max_level
                """,
                (
                    date,
                    tag,
                    category,
                    unit.get("name"),
                    unit.get("village", "home"),
                    unit.get("level"),
                    unit.get("maxLevel"),
                ),
            )


def ingest_capital_raids(conn: sqlite3.Connection, data: dict[str, Any], clan_tag: str) -> int:
    seasons = data.get("items", []) or []
    for season in seasons:
        start = to_iso(season.get("startTime"))
        conn.execute(
            """
            INSERT INTO capital_raids (
                clan_tag, start_time, end_time, capital_total_loot, raids_completed,
                total_attacks, districts_destroyed, offensive_reward, defensive_reward
            ) VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT (clan_tag, start_time) DO UPDATE SET
                end_time = excluded.end_time,
                capital_total_loot = excluded.capital_total_loot,
                raids_completed = excluded.raids_completed,
                total_attacks = excluded.total_attacks,
                districts_destroyed = excluded.districts_destroyed,
                offensive_reward = excluded.offensive_reward,
                defensive_reward = excluded.defensive_reward
            """,
            (
                clan_tag,
                start,
                to_iso(season.get("endTime")),
                season.get("capitalTotalLoot"),
                season.get("raidsCompleted"),
                season.get("totalAttacks"),
                season.get("enemyDistrictsDestroyed"),
                season.get("offensiveReward"),
                season.get("defensiveReward"),
            ),
        )
        for member in season.get("members", []) or []:
            conn.execute(
                """
                INSERT INTO capital_raid_members (
                    clan_tag, start_time, player_tag, name, attacks, attack_limit,
                    bonus_attack_limit, capital_resources_looted
                ) VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT (clan_tag, start_time, player_tag) DO UPDATE SET
                    name = excluded.name, attacks = excluded.attacks,
                    attack_limit = excluded.attack_limit,
                    bonus_attack_limit = excluded.bonus_attack_limit,
                    capital_resources_looted = excluded.capital_resources_looted
                """,
                (
                    clan_tag,
                    start,
                    member.get("tag"),
                    member.get("name"),
                    member.get("attacks"),
                    member.get("attackLimit"),
                    member.get("bonusAttackLimit"),
                    member.get("capitalResourcesLooted"),
                ),
            )
    return len(seasons)


# --- Folding -------------------------------------------------------------


def ingest_capture(conn: sqlite3.Connection, capture: Capture, our_clan_tag: str) -> None:
    """Dispatch one capture to the right handler. Error bodies are skipped."""
    if capture.is_error:
        return

    endpoint, data, when = capture.endpoint, capture.data, capture.captured_at

    if endpoint == "currentwar":
        ingest_war(conn, data, when, our_clan_tag)
    elif endpoint.startswith("cwlwar_"):
        ingest_war(conn, data, when, our_clan_tag, war_tag=f"#{endpoint.removeprefix('cwlwar_')}")
    elif endpoint == "members":
        ingest_clan_members(conn, data, when)
    elif endpoint == "player" or endpoint.startswith("player_"):
        ingest_player(conn, data, when)
    elif endpoint == "capitalraidseasons":
        ingest_capital_raids(conn, data, our_clan_tag)
    # clan, warlog and leaguegroup are captured for the archive but have no
    # derived tables of their own yet.


def rebuild(db_path: Path, store: CaptureStore, our_clan_tag: str) -> dict[str, int]:
    """Drop the database and reconstruct it from data/raw alone.

    This is the hard invariant: the derived schema is disposable, the archive is not.
    Captures are folded in the chronological order encoded in their filenames, so the
    result is independent of when the rebuild runs.
    """
    if db_path.exists():
        db_path.unlink()

    conn = connect(db_path)
    counts = {"captures": 0, "skipped": 0}
    try:
        for capture in store.iter_captures():
            if capture.is_error:
                counts["skipped"] += 1
                continue
            ingest_capture(conn, capture, our_clan_tag)
            counts["captures"] += 1
        conn.commit()
    finally:
        conn.close()
    return counts
