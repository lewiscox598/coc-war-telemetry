"""Derived metrics, expressed as SQL views and query functions.

Nothing here is a stored column: every metric is recomputed from the base tables so
that a schema change or a corrected calculation needs no migration, only a rebuild.

The central view is ``attack_values``. Value added is computed from the *prior best*
attack on a base -- MAX(stars) over attacks on that defender with a lower order --
rather than from the API's ``bestOpponentAttack`` field. That field reflects the best
attack received as of the poll, so by the end of a war it includes attacks that
happened after the one being scored: cleanup would be credited zero and the opening
attacker credited for someone else's work. Order is a monotonic per-war sequence,
which makes the prior-best computation exact.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Final

VIEWS: Final = """
DROP VIEW IF EXISTS attack_values;
CREATE VIEW attack_values AS
SELECT
    a.war_id,
    a.order_num,
    a.attacker_tag,
    a.defender_tag,
    a.stars,
    a.destruction_percentage,
    a.duration,
    a.first_seen,
    atk.side              AS attacker_side,
    atk.name              AS attacker_name,
    atk.townhall_level    AS attacker_th,
    dfn.townhall_level    AS defender_th,
    dfn.map_position      AS defender_position,
    COALESCE(prior.best_stars, 0) AS prior_best_stars,
    MAX(0, a.stars - COALESCE(prior.best_stars, 0)) AS new_stars
FROM attacks a
LEFT JOIN war_members atk
       ON atk.war_id = a.war_id AND atk.player_tag = a.attacker_tag
LEFT JOIN war_members dfn
       ON dfn.war_id = a.war_id AND dfn.player_tag = a.defender_tag
LEFT JOIN (
    SELECT p.war_id, p.defender_tag, c.order_num, MAX(p.stars) AS best_stars
    FROM attacks p
    JOIN attacks c
      ON c.war_id = p.war_id
     AND c.defender_tag = p.defender_tag
     AND p.order_num < c.order_num
    GROUP BY p.war_id, p.defender_tag, c.order_num
) prior
       ON prior.war_id = a.war_id
      AND prior.defender_tag = a.defender_tag
      AND prior.order_num = a.order_num;

-- Attacks used against attacks available, per member per war. Built from
-- war_members rather than attacks so that members who attacked zero times still
-- appear: a missed attack is invisible in the attacks table by definition.
DROP VIEW IF EXISTS member_war_usage;
CREATE VIEW member_war_usage AS
SELECT
    m.war_id,
    m.player_tag,
    m.name,
    m.townhall_level,
    m.map_position,
    w.state,
    w.war_type,
    w.start_time,
    w.end_time,
    COALESCE(w.attacks_per_member, 1) AS attacks_available,
    COUNT(a.order_num)                AS attacks_used,
    COALESCE(w.attacks_per_member, 1) - COUNT(a.order_num) AS attacks_missed,
    COALESCE(SUM(a.stars), 0)         AS stars,
    COALESCE(SUM(a.new_stars), 0)     AS new_stars,
    COALESCE(AVG(a.destruction_percentage), 0) AS avg_destruction,
    MIN(a.first_seen)                 AS first_attack_seen
FROM war_members m
JOIN wars w ON w.war_id = m.war_id
LEFT JOIN attack_values a
       ON a.war_id = m.war_id AND a.attacker_tag = m.player_tag
WHERE m.side = 'clan'
GROUP BY m.war_id, m.player_tag;

-- Three-star rate split by matchup. Mirror matches (same TH both sides) are the
-- fair comparison; dipping down or reaching up are different problems.
DROP VIEW IF EXISTS three_star_by_matchup;
CREATE VIEW three_star_by_matchup AS
SELECT
    attacker_tag,
    attacker_name,
    attacker_th,
    defender_th,
    CASE
        WHEN defender_th > attacker_th THEN 'up'
        WHEN defender_th < attacker_th THEN 'down'
        ELSE 'mirror'
    END AS matchup,
    COUNT(*)                                            AS attacks,
    SUM(CASE WHEN stars = 3 THEN 1 ELSE 0 END)          AS three_stars,
    ROUND(AVG(stars), 2)                                AS avg_stars,
    ROUND(AVG(new_stars), 2)                            AS avg_new_stars,
    ROUND(AVG(destruction_percentage), 1)               AS avg_destruction
FROM attack_values
WHERE attacker_side = 'clan'
GROUP BY attacker_tag, attacker_th, defender_th;

-- Donation deltas, reset-aware. Seasons reset donations to zero, so a drop is a
-- reset rather than a negative contribution: the post-reset value is the delta.
DROP VIEW IF EXISTS donation_deltas;
CREATE VIEW donation_deltas AS
SELECT
    s.snapshot_date,
    s.player_tag,
    s.name,
    s.donations,
    s.donations_received,
    prev.donations AS previous_donations,
    CASE
        WHEN prev.donations IS NULL      THEN 0
        WHEN s.donations < prev.donations THEN s.donations
        ELSE s.donations - prev.donations
    END AS donation_delta,
    CASE
        WHEN prev.donations IS NOT NULL AND s.donations < prev.donations
        THEN 1 ELSE 0
    END AS season_reset
FROM member_snapshots s
LEFT JOIN member_snapshots prev
       ON prev.player_tag = s.player_tag
      AND prev.snapshot_date = (
          SELECT MAX(p2.snapshot_date) FROM member_snapshots p2
          WHERE p2.player_tag = s.player_tag AND p2.snapshot_date < s.snapshot_date
      );
"""


def apply_views(conn: sqlite3.Connection) -> None:
    conn.executescript(VIEWS)
    conn.commit()


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(sql, params)]


def war_history(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    return _rows(
        conn,
        """
        SELECT war_id, war_type, opponent_name, opponent_tag, team_size, state, result,
               clan_stars, opponent_stars, clan_destruction, opponent_destruction,
               start_time, end_time
        FROM wars
        ORDER BY COALESCE(start_time, preparation_start) DESC
        LIMIT ?
        """,
        (limit,),
    )


def current_war(conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT * FROM wars
        WHERE state IN ('preparation', 'inWar')
        ORDER BY COALESCE(start_time, preparation_start) DESC
        LIMIT 1
        """
    ).fetchone()
    return dict(row) if row else None


def current_war_roster(conn: sqlite3.Connection, war_id: str) -> list[dict[str, Any]]:
    return _rows(
        conn,
        """
        SELECT player_tag, name, townhall_level, map_position,
               attacks_used, attacks_available, attacks_missed, stars, new_stars,
               ROUND(avg_destruction, 1) AS avg_destruction
        FROM member_war_usage
        WHERE war_id = ?
        ORDER BY map_position
        """,
        (war_id,),
    )


def member_performance(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """One row per member, aggregated across every war they appear in."""
    return _rows(
        conn,
        """
        SELECT
            u.player_tag,
            u.name,
            MAX(u.townhall_level) AS townhall_level,
            COUNT(DISTINCT u.war_id) AS wars,
            SUM(u.attacks_used) AS attacks_used,
            SUM(u.attacks_available) AS attacks_available,
            SUM(u.attacks_missed) AS attacks_missed,
            SUM(u.stars) AS stars,
            SUM(u.new_stars) AS new_stars,
            ROUND(AVG(NULLIF(u.avg_destruction, 0)), 1) AS avg_destruction,
            ROUND(
                CAST(SUM(u.attacks_used) AS REAL)
                / NULLIF(SUM(u.attacks_available), 0) * 100, 0
            ) AS usage_pct,
            (
                SELECT ROUND(
                    CAST(SUM(CASE WHEN av.stars = 3 THEN 1 ELSE 0 END) AS REAL)
                    / NULLIF(COUNT(*), 0) * 100, 0)
                FROM attack_values av
                WHERE av.attacker_tag = u.player_tag AND av.attacker_side = 'clan'
            ) AS three_star_pct,
            (
                SELECT ROUND(AVG(av.stars), 2) FROM attack_values av
                WHERE av.attacker_tag = u.player_tag AND av.attacker_side = 'clan'
            ) AS avg_stars
        FROM member_war_usage u
        GROUP BY u.player_tag
        ORDER BY usage_pct DESC, new_stars DESC
        """,
    )


def matchup_breakdown(conn: sqlite3.Connection, player_tag: str) -> list[dict[str, Any]]:
    return _rows(
        conn,
        """
        SELECT attacker_th, defender_th, matchup, attacks, three_stars,
               avg_stars, avg_new_stars, avg_destruction,
               ROUND(CAST(three_stars AS REAL) / NULLIF(attacks, 0) * 100, 0) AS three_star_pct
        FROM three_star_by_matchup
        WHERE attacker_tag = ?
        ORDER BY defender_th DESC
        """,
        (player_tag,),
    )


def attack_timing(conn: sqlite3.Connection, player_tag: str | None = None) -> list[dict[str, Any]]:
    """How long after battle day opens a member attacks.

    first_seen is our capture time, not the true attack time, so this is accurate to
    roughly the poll interval. That is enough to separate "attacks immediately" from
    "attacks in the last hour", which is the behaviour worth knowing.
    """
    sql = """
        SELECT
            u.player_tag, u.name, u.war_id, w.opponent_name, u.start_time,
            u.first_attack_seen,
            ROUND(
                (julianday(u.first_attack_seen) - julianday(u.start_time)) * 24, 1
            ) AS hours_after_start,
            ROUND(
                (julianday(w.end_time) - julianday(u.first_attack_seen)) * 24, 1
            ) AS hours_before_end
        FROM member_war_usage u
        JOIN wars w ON w.war_id = u.war_id
        WHERE u.first_attack_seen IS NOT NULL AND u.start_time IS NOT NULL
    """
    params: tuple = ()
    if player_tag:
        sql += " AND u.player_tag = ?"
        params = (player_tag,)
    return _rows(conn, sql + " ORDER BY u.start_time DESC", params)


def donation_history(
    conn: sqlite3.Connection, player_tag: str | None = None
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM donation_deltas"
    params: tuple = ()
    if player_tag:
        sql += " WHERE player_tag = ?"
        params = (player_tag,)
    return _rows(conn, sql + " ORDER BY snapshot_date DESC", params)


def passivity(conn: sqlite3.Connection, window: int = 4) -> list[dict[str, Any]]:
    """Members whose attack usage *and* donations have both declined.

    Both signals must move together. Attack usage alone catches people who were
    simply away; donations alone catch people who are short on resources. The pair
    is what distinguishes drifting away from a bad fortnight.
    """
    wars = [
        r["war_id"]
        for r in _rows(
            conn,
            """
            SELECT war_id FROM wars
            WHERE state = 'warEnded'
            ORDER BY COALESCE(end_time, start_time) DESC
            LIMIT ?
            """,
            (window * 2,),
        )
    ]
    if len(wars) < 2:
        return []

    recent, earlier = wars[: len(wars) // 2], wars[len(wars) // 2 :]

    def usage(war_ids: list[str]) -> dict[str, float]:
        placeholders = ",".join("?" * len(war_ids))
        return {
            r["player_tag"]: r["pct"]
            for r in _rows(
                conn,
                f"""
                SELECT player_tag,
                       CAST(SUM(attacks_used) AS REAL)
                       / NULLIF(SUM(attacks_available), 0) * 100 AS pct
                FROM member_war_usage WHERE war_id IN ({placeholders})
                GROUP BY player_tag
                """,
                tuple(war_ids),
            )
        }

    recent_usage, earlier_usage = usage(recent), usage(earlier)

    donations = {
        r["player_tag"]: (r["recent"], r["earlier"])
        for r in _rows(
            conn,
            """
            SELECT player_tag,
                   AVG(CASE WHEN snapshot_date >= date('now', '-14 days')
                            THEN donation_delta END) AS recent,
                   AVG(CASE WHEN snapshot_date <  date('now', '-14 days')
                            AND snapshot_date >= date('now', '-28 days')
                            THEN donation_delta END) AS earlier
            FROM donation_deltas GROUP BY player_tag
            """,
        )
    }

    flagged = []
    for tag, recent_pct in recent_usage.items():
        earlier_pct = earlier_usage.get(tag)
        if earlier_pct is None or recent_pct >= earlier_pct:
            continue
        recent_don, earlier_don = donations.get(tag, (None, None))
        if recent_don is None or earlier_don is None or recent_don >= earlier_don:
            continue
        name = conn.execute(
            "SELECT name FROM war_members WHERE player_tag = ? ORDER BY rowid DESC LIMIT 1",
            (tag,),
        ).fetchone()
        flagged.append(
            {
                "player_tag": tag,
                "name": name["name"] if name else tag,
                "usage_before": round(earlier_pct, 0),
                "usage_now": round(recent_pct, 0),
                "donations_before": round(earlier_don, 0),
                "donations_now": round(recent_don, 0),
            }
        )
    return sorted(flagged, key=lambda f: f["usage_now"] - f["usage_before"])
