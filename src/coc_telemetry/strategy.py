"""War planning: target assignment and army recommendation.

WHAT THIS CANNOT DO, stated plainly because it shapes everything below.

The Clash of Clans API exposes exactly five fields about an opponent base:
``tag``, ``name``, ``townhallLevel``, ``mapPosition`` and ``opponentAttacks``.
There is no layout, no defence levels, no building positions, no base image.
Nothing here can "read the enemy base" -- no tool can, short of a person opening
the game. Nor can a specific base be looked up online: bases are not publicly
indexed by player tag anywhere.

So recommendations are built from what IS evidenced:

1. **Town Hall differential** between attacker and defender -- a baseline
   expectation, and the weakest form of evidence here.
2. **The member's own measured record** at that exact matchup, from our archive.
   This is the strongest evidence and it overrides the baseline once there is
   enough of it.
3. **What the member can actually field**, checked against their real troop,
   spell and hero levels from player_units. An army they have not unlocked or
   have left at level 2 is not a recommendation, it is a wish.

Every recommendation carries its basis and a confidence label, so a reader can
tell a measured claim from a rule of thumb. The curated army table is sourced
and dated; it is general per-Town-Hall strategy, never base-specific.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any, Final

# --- Curated army reference ---------------------------------------------
#
# General per-Town-Hall strategy, retrieved 2026-09-20. This is NOT evidence
# about any particular base -- no such source exists. Requirements are absolute
# unit levels, checked against the member's actual lab and hero progress, which
# is what turns a generic guide into a specific recommendation.

RETRIEVED: Final = "2026-09-20"


@dataclass(frozen=True)
class Army:
    name: str
    town_halls: tuple[int, ...]
    composition: str
    spells: str
    # unit name -> minimum level for the army to be worth running
    requires: dict[str, int] = field(default_factory=dict)
    heroes: dict[str, int] = field(default_factory=dict)
    note: str = ""
    source: str = ""
    source_title: str = ""


ARMIES: Final[tuple[Army, ...]] = (
    Army(
        name="Giants + Archers",
        town_halls=(4, 5, 6),
        composition="Giants leading, Wall Breakers on one wall, Archers behind",
        spells="None available at this level",
        # Level 1 is max or near-max for these at TH4-6, so requiring more
        # would reject every account this bracket is written for.
        requires={"Giant": 1, "Archer": 1, "Wall Breaker": 1},
        note=(
            "There is no three-star meta this low. Send Giants at one side, break a "
            "single wall, and clean with Archers. Against a much higher Town Hall, "
            "take the one star off the Town Hall and do not chase more."
        ),
        source="https://clashofclans.fandom.com/wiki/Attack_Strategies:Best_attack_strategy",
        source_title="Clash of Clans Wiki - Best attack strategy",
    ),
    Army(
        name="Mass Dragons",
        town_halls=(7, 8),
        composition="10 Dragons, 2 Balloons, Wall Breakers to open a compartment",
        spells="3 Rage, 1 Haste (or Heal against heavy splash)",
        requires={"Dragon": 2, "Balloon": 3},
        note=(
            "The most forgiving three-star army at this level: pick the side with "
            "the fewest Air Defences, drop in a wide line, rage over the core."
        ),
        source="https://www.newforestsafari.com/town-hall-8-attack-strategies/",
        source_title="Town Hall 8 attack strategies",
    ),
    Army(
        name="GoWiPe",
        town_halls=(8, 9),
        composition="2 Golems, 2 P.E.K.K.As, 8-18 Wizards, 4-8 Wall Breakers",
        spells="2 Heal, 1 Rage, 1 Poison",
        requires={"Golem": 1, "P.E.K.K.A": 1, "Wizard": 4, "Wall Breaker": 4},
        note=(
            "Golems tank, Wizards clear, P.E.K.K.A finishes. Slower and less "
            "forgiving than Dragons, but it does not care about Air Defences."
        ),
        source="https://www.newforestsafari.com/gowipe-attack-strategy/",
        source_title="GoWiPe attack strategy",
    ),
    Army(
        name="Zap Dragons",
        town_halls=(9, 10, 11, 12),
        composition="Dragons with Balloons behind, 1 Ice Golem to tank",
        spells="Lightning and Earthquake to remove an Air Defence, Rage for the core",
        requires={"Dragon": 5, "Balloon": 4, "Lightning Spell": 6},
        note=(
            "Does not depend on heroes, which makes it the honest pick when Queen "
            "and King are underlevelled. Zap one Air Defence, funnel, go straight in."
        ),
        source="https://blueprintcoc.com/blogs/town-hall-11/best-th11-attack-strategies",
        source_title="Best TH11 attack strategies 2026",
    ),
    Army(
        name="Zap Witches",
        town_halls=(10, 11, 12),
        composition="Golems tanking, Witches behind, Bowlers to clear",
        spells="Lightning and Earthquake on an Inferno, then Heal and Rage",
        requires={"Witch": 4, "Golem": 4, "Bowler": 3},
        note=(
            "Strong against layered ground bases, but Witch level carries the whole "
            "attack -- a low-level Witch dies before her skeletons matter."
        ),
        source="https://blueprintcoc.com/blogs/town-hall-10/best-th10-attack-strategies",
        source_title="Best TH10 attack strategies 2026",
    ),
    Army(
        name="Queen Charge Hybrid",
        town_halls=(11, 12, 13),
        composition="Queen with Healers carving a path, then Hogs and Miners",
        spells="Rage and Freeze for the Queen, Heal for the hybrid",
        requires={"Hog Rider": 5, "Miner": 5, "Healer": 4},
        heroes={"Archer Queen": 35},
        note=(
            "The most versatile attack at these levels, and the most hero-dependent: "
            "the Queen must survive long enough to open a third of the base."
        ),
        source="https://www.clashchamps.com/2026/09/16/top-3-best-th12-attack-strategies-for-2026-clash-of-clans-sir-moose-gaming/",
        source_title="Top 3 best TH12 attack strategies for 2026 - Sir Moose Gaming",
    ),
)


# --- Expected value ------------------------------------------------------
#
# Baseline expected stars by Town Hall differential. Deliberately coarse: it is
# a prior to be overridden by a member's measured record, not a claim to
# precision. Attacking down is easier; every level up costs sharply.

BASELINE: Final[dict[int, float]] = {
    3: 2.90,
    2: 2.85,
    1: 2.70,
    0: 2.20,
    -1: 1.50,
    -2: 0.90,
    -3: 0.45,
}

# A member's own record only overrides the baseline once there is enough of it.
MEASURED_MIN_ATTACKS: Final = 3


def baseline_stars(attacker_th: int, defender_th: int) -> float:
    diff = max(-3, min(3, attacker_th - defender_th))
    return BASELINE[diff]


@dataclass(frozen=True)
class Expectation:
    stars: float
    confidence: str  # 'measured' | 'partial' | 'baseline'
    basis: str


def expectation(
    conn: sqlite3.Connection, player_tag: str, attacker_th: int, defender_th: int
) -> Expectation:
    """Expected new stars for one member against one Town Hall level.

    Blends the member's measured record at this exact matchup with the baseline,
    weighted by how much evidence there is. With no history the baseline stands
    alone and says so.
    """
    base = baseline_stars(attacker_th, defender_th)
    row = conn.execute(
        """
        SELECT COUNT(*) AS n, AVG(new_stars) AS avg_new,
               SUM(CASE WHEN stars = 3 THEN 1 ELSE 0 END) AS threes
        FROM attack_values
        WHERE attacker_tag = ? AND attacker_side = 'clan'
          AND attacker_th = ? AND defender_th = ?
        """,
        (player_tag, attacker_th, defender_th),
    ).fetchone()

    n = row["n"] or 0
    if n == 0:
        # Exact-matchup history stays sparse, because Town Hall levels keep
        # moving. Fall back to the member's record at the same differential --
        # "how do they do one level down" -- before abandoning evidence entirely.
        same = conn.execute(
            """
            SELECT COUNT(*) AS n, AVG(new_stars) AS avg_new
            FROM attack_values
            WHERE attacker_tag = ? AND attacker_side = 'clan'
              AND (attacker_th - defender_th) = ?
            """,
            (player_tag, attacker_th - defender_th),
        ).fetchone()
        if (same["n"] or 0) >= MEASURED_MIN_ATTACKS:
            d = attacker_th - defender_th
            where = "at mirror" if d == 0 else f"{abs(d)} level(s) {'down' if d > 0 else 'up'}"
            return Expectation(
                same["avg_new"],
                "partial",
                f"no record against TH{defender_th} specifically, but {same['n']} "
                f"attacks {where} averaging {same['avg_new']:.1f} value added",
            )
        return Expectation(
            base,
            "baseline",
            f"no record at TH{attacker_th} vs TH{defender_th}; Town Hall differential only",
        )

    measured = row["avg_new"] or 0.0
    if n >= MEASURED_MIN_ATTACKS:
        return Expectation(
            measured,
            "measured",
            f"{row['threes']} three-stars in {n} attacks at this matchup "
            f"(avg {measured:.1f} value added)",
        )

    # Too little to trust outright; shrink toward the baseline.
    weight = n / MEASURED_MIN_ATTACKS
    blended = measured * weight + base * (1 - weight)
    return Expectation(
        blended,
        "partial",
        f"only {n} attack(s) at this matchup so far; blended with baseline",
    )


# --- Assignment ----------------------------------------------------------


def _hungarian(cost: list[list[float]]) -> list[int]:
    """Optimal assignment minimising total cost. Returns row -> column.

    Standard O(n^3) Hungarian method. War rosters top out at 50, so this is
    instant, and unlike a greedy pass it cannot paint itself into a corner by
    spending the best attacker on a target someone else could have handled.
    """
    n, m = len(cost), len(cost[0])
    if n == 0 or m == 0:
        return []
    inf = float("inf")
    u = [0.0] * (n + 1)
    v = [0.0] * (m + 1)
    p = [0] * (m + 1)
    way = [0] * (m + 1)

    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv = [inf] * (m + 1)
        used = [False] * (m + 1)
        while True:
            used[j0] = True
            i0, delta, j1 = p[j0], inf, 0
            for j in range(1, m + 1):
                if used[j]:
                    continue
                cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                if cur < minv[j]:
                    minv[j], way[j] = cur, j0
                if minv[j] < delta:
                    delta, j1 = minv[j], j
            for j in range(m + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while j0:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1

    assignment = [-1] * n
    for j in range(1, m + 1):
        if p[j] > 0:
            assignment[p[j] - 1] = j - 1
    return assignment


def recommend_assignments(conn: sqlite3.Connection, war_id: str) -> list[dict[str, Any]]:
    """Assign each of our members a primary target, maximising expected stars.

    Solved as an assignment problem rather than by pairing mirrors, because the
    best overall plan sometimes means someone attacks off their mirror so that a
    teammate gets a target they can actually three-star.
    """
    ours = [
        dict(r)
        for r in conn.execute(
            "SELECT player_tag, name, map_position, townhall_level FROM war_members "
            "WHERE war_id = ? AND side = 'clan' ORDER BY map_position",
            (war_id,),
        )
    ]
    theirs = [
        dict(r)
        for r in conn.execute(
            "SELECT player_tag, name, map_position, townhall_level FROM war_members "
            "WHERE war_id = ? AND side = 'opponent' ORDER BY map_position",
            (war_id,),
        )
    ]
    if not ours or not theirs:
        return []

    grid: list[list[Expectation]] = [
        [
            expectation(conn, a["player_tag"], a["townhall_level"], d["townhall_level"])
            for d in theirs
        ]
        for a in ours
    ]
    # Maximise stars by minimising their negation.
    cost = [[-e.stars for e in row] for row in grid]

    # Hungarian needs rows <= columns; transpose when we outnumber them.
    if len(ours) <= len(theirs):
        picks = _hungarian(cost)
        pairs = list(enumerate(picks))
    else:
        transposed = [[cost[i][j] for i in range(len(ours))] for j in range(len(theirs))]
        picks = _hungarian(transposed)
        pairs = [(a, d) for d, a in enumerate(picks) if a >= 0]

    out: list[dict[str, Any]] = []
    for ai, di in pairs:
        if di < 0 or ai < 0:
            continue
        attacker, defender = ours[ai], theirs[di]
        exp = grid[ai][di]
        mirror = theirs[ai] if ai < len(theirs) else None
        diff = attacker["townhall_level"] - defender["townhall_level"]
        out.append(
            {
                "attacker_tag": attacker["player_tag"],
                "attacker": attacker["name"],
                "attacker_position": attacker["map_position"],
                "attacker_th": attacker["townhall_level"],
                "defender": defender["name"],
                "defender_position": defender["map_position"],
                "defender_th": defender["townhall_level"],
                "expected_stars": round(exp.stars, 2),
                "confidence": exp.confidence,
                "basis": exp.basis,
                "th_diff": diff,
                "off_mirror": mirror is not None and mirror["player_tag"] != defender["player_tag"],
                "outmatched": diff <= -2,
            }
        )
    return sorted(out, key=lambda r: r["attacker_position"])


# --- Army fit ------------------------------------------------------------


def army_options(conn: sqlite3.Connection, player_tag: str, town_hall: int) -> list[dict[str, Any]]:
    """Which curated armies this member can actually field, and why not otherwise.

    Checked against their real unlocked levels. An army they have not unlocked,
    or have left far behind in the lab, is reported as unavailable with the
    specific unit that blocks it -- which is more useful than silently hiding it.
    """
    levels = {
        r["name"]: r["level"]
        for r in conn.execute(
            """
            SELECT name, level FROM player_units
            WHERE player_tag = ? AND village = 'home'
              AND snapshot_date = (
                  SELECT MAX(snapshot_date) FROM player_units WHERE player_tag = ?
              )
            """,
            (player_tag, player_tag),
        )
    }

    def evaluate(army: Army, at_level: bool) -> dict[str, Any]:
        blockers: list[str] = []
        for unit, needed in {**army.requires, **army.heroes}.items():
            have = levels.get(unit)
            if have is None:
                blockers.append(f"{unit} not unlocked")
            elif have < needed:
                blockers.append(f"{unit} is level {have}, needs {needed}")
        return {
            "name": army.name,
            "composition": army.composition,
            "spells": army.spells,
            "note": army.note,
            "source": army.source,
            "source_title": army.source_title,
            "viable": not blockers,
            "blockers": blockers,
            "hero_dependent": bool(army.heroes),
            "at_level": at_level,
            "designed_for": max(army.town_halls),
        }

    results = [evaluate(a, True) for a in ARMIES if town_hall in a.town_halls]

    # A rushed account can fail every army for its own Town Hall. Rather than
    # show an empty panel, offer the strongest lower-bracket army it can actually
    # field: an under-levelled TH12 running a Town Hall 8 Dragon army is a real
    # plan, and saying so is more useful than saying nothing.
    if not any(r["viable"] for r in results):
        fallbacks = [
            evaluate(a, False)
            for a in ARMIES
            if town_hall not in a.town_halls and max(a.town_halls) < town_hall
        ]
        viable_fallbacks = sorted(
            (f for f in fallbacks if f["viable"]), key=lambda a: -a["designed_for"]
        )
        results.extend(viable_fallbacks[:1])

    # Viable first, at-level before fallback, then by how close the rest are.
    return sorted(results, key=lambda a: (not a["viable"], not a["at_level"], len(a["blockers"])))


# --- Live cleanup --------------------------------------------------------


def cleanup_board(conn: sqlite3.Connection, war_id: str) -> list[dict[str, Any]]:
    """Which enemy bases are still worth hitting, from what has happened so far.

    This is the one genuinely base-specific signal available, and it only exists
    once battle day is underway: how each base has actually held up against our
    attacks. Three stars means done; a base that has absorbed two attacks for one
    star is telling you something no guide can.
    """
    rows = conn.execute(
        """
        SELECT
            d.player_tag, d.name, d.map_position, d.townhall_level,
            COUNT(a.order_num)                       AS attempts,
            COALESCE(MAX(a.stars), 0)                AS best_stars,
            COALESCE(MAX(a.destruction_percentage), 0) AS best_destruction
        FROM war_members d
        LEFT JOIN attacks a
               ON a.war_id = d.war_id AND a.defender_tag = d.player_tag
        WHERE d.war_id = ? AND d.side = 'opponent'
        GROUP BY d.player_tag
        ORDER BY d.map_position
        """,
        (war_id,),
    ).fetchall()

    board = []
    for r in rows:
        best, attempts = r["best_stars"], r["attempts"]
        if best == 3:
            state, advice = "cleared", "Done. Do not spend an attack here."
        elif attempts == 0:
            state, advice = "untouched", "No information yet."
        elif best == 2:
            state, advice = (
                "one star left",
                (f"Held at {r['best_destruction']:.0f}%. One star available to a clean follow-up."),
            )
        elif attempts >= 2:
            state, advice = (
                "resistant",
                (
                    f"{attempts} attacks for {best} star(s). Treat as genuinely hard "
                    "rather than unlucky."
                ),
            )
        else:
            state, advice = "partly open", f"One attempt reached {best} star(s)."
        board.append(
            {
                "name": r["name"],
                "map_position": r["map_position"],
                "townhall_level": r["townhall_level"],
                "attempts": attempts,
                "best_stars": best,
                "best_destruction": round(r["best_destruction"], 1),
                "state": state,
                "advice": advice,
                "stars_available": 3 - best,
            }
        )
    return board
