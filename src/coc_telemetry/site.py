"""Static site generation.

Everything is rendered at build time. The page makes no API calls and fetches no
data: the token never reaches the client, and the site works with JavaScript
disabled apart from optional table sorting.

Timestamps are stored as UTC and rendered in Europe/London.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import Any, Final
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape

from coc_telemetry.capture import CaptureStore
from coc_telemetry.metrics import (
    apply_views,
    attack_timing,
    current_war,
    current_war_roster,
    donation_history,
    matchup_breakdown,
    member_performance,
    passivity,
    war_history,
)
from coc_telemetry.strategy import (
    RETRIEVED,
    army_options,
    cleanup_board,
    recommend_assignments,
)

LONDON = ZoneInfo("Europe/London")

STATE_LABELS = {
    "preparation": "Preparation",
    "inWar": "Battle day",
    "warEnded": "Ended",
    "notInWar": "Not in war",
}


def _local(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value).astimezone(LONDON)


def format_when(value: str | None, fmt: str = "%a %-d %b, %H:%M") -> str:
    moment = _local(value)
    return moment.strftime(fmt) if moment else "--"


def format_relative(value: str | None, *, now: datetime | None = None) -> str:
    """Human phrasing for how far off a moment is, in Europe/London terms."""
    moment = _local(value)
    if moment is None:
        return ""
    reference = (now or datetime.now(UTC)).astimezone(LONDON)
    delta = moment - reference
    hours = delta.total_seconds() / 3600
    if abs(hours) < 1:
        return f"in {int(abs(hours) * 60)} min" if hours > 0 else "just now"
    if hours > 0:
        return f"in {int(hours)}h" if hours < 24 else f"in {int(hours // 24)}d {int(hours % 24)}h"
    hours = abs(hours)
    return f"{int(hours)}h ago" if hours < 24 else f"{int(hours // 24)}d ago"


def war_timing_label(war: dict[str, Any], *, now: datetime | None = None) -> str:
    """One line telling a clanmate the thing they actually want to know."""
    if war["state"] == "preparation":
        start = format_when(war["start_time"])
        return f"Battle day starts {start} ({format_relative(war['start_time'], now=now)})"
    if war["state"] == "inWar":
        return f"Ends {format_when(war['end_time'])} ({format_relative(war['end_time'], now=now)})"
    return f"Ended {format_when(war['end_time'])}"


def _environment(templates: Path) -> Environment:
    env = Environment(
        loader=FileSystemLoader(templates),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["when"] = format_when
    env.globals["stars"] = star_row
    return env


def _kpi(label: str, value: str, unit: str = "", foot: str = "", spark: str = "") -> dict:
    return {"label": label, "value": value, "unit": unit, "foot": foot, "spark": spark}


def _hero(war: dict[str, Any] | None, roster: list[dict[str, Any]], history: list) -> dict:
    """The one number the page leads with. Exactly one per view.

    What matters changes with the war's phase: before battle day it is how long you
    have; during it, how many attacks the clan still owes; afterwards, the result.
    """
    if war and war["state"] == "preparation":
        return {
            "figure": format_relative(war["start_time"]).removeprefix("in "),
            "label": "until battle day",
            "sub": format_when(war["start_time"]),
            "countdown": war["start_time"],
        }
    if war and war["state"] == "inWar":
        owed = sum(m["attacks_missed"] for m in roster)
        return {
            "figure": str(owed),
            "label": "attacks still owed" if owed else "all attacks used",
            "sub": f"war ends {format_relative(war['end_time'])}",
            "countdown": war["end_time"],
        }
    played = [w for w in history if w.get("state") == "warEnded"]
    if played:
        wins = sum(1 for w in played if w.get("result") == "win")
        return {
            "figure": f"{wins}\u2013{len(played) - wins}",
            "label": "war record",
            "sub": f"{len(played)} wars tracked",
            "countdown": None,
        }
    return {
        "figure": "0",
        "label": "wars completed",
        "sub": "the archive starts from the first poll",
        "countdown": None,
    }


def gather(conn: sqlite3.Connection, player_tag: str) -> dict[str, Any]:
    """Everything both pages need, read once."""
    apply_views(conn)

    # current_war() is live-only by design. But when a war has just finished the
    # page should show the result and the final map, not fall straight back to an
    # empty state -- so fall back to the most recent war of any state.
    war = (
        current_war(conn)
        or conn.execute(
            """
        SELECT * FROM wars
        ORDER BY COALESCE(end_time, start_time, preparation_start) DESC
        LIMIT 1
        """
        ).fetchone()
    )

    roster: list[dict[str, Any]] = []
    if war:
        war = dict(war)
        war["state_label"] = STATE_LABELS.get(war["state"], war["state"])
        war["timing_label"] = war_timing_label(war)
        roster = current_war_roster(conn, war["war_id"])

    history = [dict(w) for w in war_history(conn)]
    for row in history:
        row["ended_label"] = format_when(row["end_time"], "%-d %b")

    timing = [dict(t) for t in attack_timing(conn, player_tag)]
    for row in timing:
        row["first_attack_label"] = format_when(row["first_attack_seen"], "%-d %b %H:%M")

    me_row = conn.execute(
        "SELECT * FROM player_snapshots WHERE player_tag = ? ORDER BY snapshot_date DESC LIMIT 1",
        (player_tag,),
    ).fetchone()

    clan_row = conn.execute(
        "SELECT COUNT(DISTINCT player_tag) AS n FROM member_snapshots "
        "WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM member_snapshots)"
    ).fetchone()

    my_wars = [
        dict(r)
        for r in conn.execute(
            """
            SELECT u.*, w.opponent_name, w.result
            FROM member_war_usage u JOIN wars w ON w.war_id = u.war_id
            WHERE u.player_tag = ? AND u.attacks_used > 0
            ORDER BY w.start_time DESC
            """,
            (player_tag,),
        )
    ]

    members = member_performance(conn)
    donations = donation_history(conn, player_tag)

    totals = conn.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM wars WHERE state = 'warEnded')      AS wars_done,
            (SELECT COUNT(*) FROM attack_values WHERE attacker_side='clan') AS attacks,
            (SELECT ROUND(AVG(stars), 2) FROM attack_values
             WHERE attacker_side='clan') AS avg_stars,
            (SELECT SUM(new_stars) FROM attack_values WHERE attacker_side='clan') AS value_added
        """
    ).fetchone()

    used = sum(m["attacks_used"] or 0 for m in members)
    available = sum(m["attacks_available"] or 0 for m in members)
    usage = round(used / available * 100) if available else 0

    clan_kpis = [
        _kpi("Wars tracked", str(len(history)), foot=f"{totals['wars_done'] or 0} completed"),
        _kpi("Attack usage", str(usage), "%", foot=f"{used} of {available} used"),
        _kpi(
            "Avg stars",
            f"{totals['avg_stars']:.2f}" if totals["avg_stars"] is not None else "\u2013",
            foot=f"over {totals['attacks'] or 0} attacks",
        ),
        _kpi("Value added", str(totals["value_added"] or 0), foot="stars gained over prior best"),
    ]

    my_three = conn.execute(
        """
        SELECT COUNT(*) AS n, SUM(CASE WHEN stars = 3 THEN 1 ELSE 0 END) AS three
        FROM attack_values WHERE attacker_tag = ? AND attacker_side = 'clan'
        """,
        (player_tag,),
    ).fetchone()

    donation_series = [d["donation_delta"] for d in reversed(donations)][-12:]
    me_kpis = [
        _kpi("War stars", str(me_row["war_stars"]) if me_row else "\u2013", foot="lifetime"),
        _kpi(
            "Three-star rate",
            f"{round(my_three['three'] / my_three['n'] * 100)}" if my_three["n"] else "\u2013",
            "%" if my_three["n"] else "",
            foot=f"{my_three['three'] or 0} of {my_three['n'] or 0} attacks",
        ),
        _kpi(
            "Attacks used",
            str(sum(w["attacks_used"] for w in my_wars)),
            foot=f"across {len(my_wars)} wars",
        ),
        _kpi(
            "Donated",
            str(me_row["donations"]) if me_row else "\u2013",
            foot="this season",
            spark=svg_sparkline(donation_series),
        ),
    ]

    # The war map: one row per matchup, carrying both sides' live state.
    duels, my_duel = _duels(conn, war, player_tag)
    feed = _feed(conn, war, player_tag)
    replay = replay_payload(conn, war, player_tag)
    map_rows = _map_rows(conn, war, player_tag)
    phase = PHASES.get(war["state"], "idle") if war else "idle"

    attacks_used = sum(d["attacks_used"] for d in duels)
    attacks_total = sum(d["attacks_available"] for d in duels)

    return {
        "war": war,
        "roster": roster,
        "duels": duels,
        "my_duel": my_duel,
        "feed": feed,
        "replay": replay,
        "map_rows": map_rows,
        "phase": phase,
        "attacks_used": attacks_used,
        "attacks_total": attacks_total,
        "attacks_pct": round(attacks_used / attacks_total * 100) if attacks_total else 0,
        "plan_expected": round(sum(d["expected_stars"] for d in duels), 1),
        "plan_max": len(duels) * 3,
        "army_retrieved": RETRIEVED,
        "hero": _hero(war, roster, history),
        "history": history,
        "members": members,
        "passive": passivity(conn),
        "me": dict(me_row) if me_row else None,
        "me_name": me_row["name"] if me_row else "Me",
        "player_tag": player_tag,
        "my_wars": my_wars,
        "matchups": matchup_breakdown(conn, player_tag),
        "timing": timing,
        "donations": donations,
        "member_count": clan_row["n"] if clan_row else 0,
        "clan_kpis": clan_kpis,
        "me_kpis": me_kpis,
        "war_chart": svg_war_stars(history),
    }


def build_site(
    conn: sqlite3.Connection,
    output: Path,
    *,
    templates: Path,
    clan_name: str,
    player_tag: str,
    store: CaptureStore | None = None,
    clan: dict[str, Any] | None = None,
    demo: bool = False,
) -> list[Path]:
    env = _environment(templates)
    context = gather(conn, player_tag)

    # Clan badges: the API carries both sides' badgeUrls on the war payload.
    # Read from the archive so a site build still needs no token.
    badges: dict[str, str] = {}
    if store is not None:
        latest_war = store.latest_path("currentwar")
        if latest_war is not None:
            payload = store.read(latest_war).data
            for key, side in (("us", "clan"), ("them", "opponent")):
                urls = (payload.get(side) or {}).get("badgeUrls") or {}
                if urls.get("medium"):
                    badges[key] = urls["medium"]
    context["badges"] = badges

    captures = store.paths() if store else []
    first_capture = "--"
    if captures:
        from coc_telemetry.capture import parse_capture_path

        _, earliest = parse_capture_path(captures[0])
        first_capture = earliest.astimezone(LONDON).strftime("%-d %b %Y")

    context.update(
        clan_name=clan_name,
        clan=clan,
        generated_at=datetime.now(UTC).astimezone(LONDON).strftime("%-d %b %Y, %H:%M %Z"),
        capture_count=len(captures),
        demo=demo,
        first_capture=first_capture,
    )

    output.mkdir(parents=True, exist_ok=True)
    (output / "me").mkdir(parents=True, exist_ok=True)
    css_source = templates / "style.css"
    shutil.copyfile(css_source, output / "style.css")

    # Cache-bust the stylesheet on content. Pages serves style.css from a CDN with
    # a long cache life, so without this a returning visitor gets new HTML against
    # their cached old CSS -- which renders as a completely broken page until they
    # hard-refresh. The hash changes only when the CSS actually changes.
    css_version = hashlib.sha256(css_source.read_bytes()).hexdigest()[:10]

    written: list[Path] = []
    for template_name, target, page, root in [
        ("clan.html", output / "index.html", "clan", ""),
        ("me.html", output / "me" / "index.html", "me", "../"),
    ]:
        html = env.get_template(template_name).render(
            **context, page=page, root=root, css_version=css_version
        )
        target.write_text(html, encoding="utf-8")
        written.append(target)
    return written


# --- Inline SVG, generated at build time ---------------------------------
#
# Mark specs follow the house rules: 2px lines with round joins, markers at r>=4
# carrying a 2px ring in the surface colour so they stay legible where they
# overlap, hairline recessive gridlines, and selective direct labels rather than a
# number on every point. Colours are the validated categorical slots for the panel
# surface; text never wears a data colour.

# Documented dark-mode steps, validated against the stone chart surface:
# lightness band, chroma, CVD dE 26.8, normal-vision dE 31.8, contrast all PASS.
# An earlier pair was picked by eye to match the theme and failed the
# lightness band -- which is precisely why the validator gets run.
SURFACE = "#1a202b"
SERIES_1 = "#3987e5"
SERIES_2 = "#d95926"
INK_3 = "#8b95a6"
GRID = "#2c3545"


def svg_sparkline(
    values: list[float], *, width: int = 132, height: int = 34, color: str = SERIES_1
) -> str:
    """A trend line for a stat tile. Returns '' when there is nothing to plot.

    An honest sparkline needs at least two points; one point is a dot pretending
    to be a trend.
    """
    if len(values) < 2:
        return ""
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1
    step = width / (len(values) - 1)
    pad = 4
    usable = height - pad * 2

    points = [(i * step, pad + usable - ((v - lo) / span) * usable) for i, v in enumerate(values)]
    path = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}" for i, (x, y) in enumerate(points))
    last_x, last_y = points[-1]
    return (
        f'<svg class="k-spark" viewBox="0 0 {width} {height}" width="{width}" '
        f'height="{height}" fill="none" aria-hidden="true">'
        f'<path d="{path}" stroke="{color}" stroke-width="2" '
        f'stroke-linecap="round" stroke-linejoin="round"/>'
        f'<circle cx="{last_x:.1f}" cy="{last_y:.1f}" r="4" fill="{color}" '
        f'stroke="{SURFACE}" stroke-width="2"/>'
        "</svg>"
    )


def svg_war_stars(wars: list[dict[str, Any]], *, width: int = 520, height: int = 200) -> str:
    """Stars per war, us against them.

    Two series, so a legend is always present -- identity is never colour alone.
    Only the final point of each series is directly labelled: a number on every
    point is chaos and goes unread.

    The viewBox is deliberately close to 2.6:1 rather than wider. The SVG scales to
    its container, so a wide viewBox shrinks the axis text to ~6px on a phone; these
    proportions keep it legible at 350px while the CSS max-height stops it dominating
    a desktop.
    """
    played = [w for w in reversed(wars) if w.get("state") == "warEnded"]
    if len(played) < 2:
        return ""

    left, right, top, bottom = 44, 38, 18, 26
    plot_w = width - left - right
    plot_h = height - top - bottom

    ours = [w["clan_stars"] or 0 for w in played]
    theirs = [w["opponent_stars"] or 0 for w in played]
    hi = max([*ours, *theirs, 1])
    # Round the axis up to a clean number rather than the raw maximum.
    hi = int((hi + 4) // 5 * 5)
    step = plot_w / max(len(played) - 1, 1)

    def pts(series: list[int]) -> list[tuple[float, float]]:
        return [(left + i * step, top + plot_h - (v / hi) * plot_h) for i, v in enumerate(series)]

    parts = [
        f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="Stars per war, our clan against the opponent">'
    ]

    for tick in range(0, hi + 1, max(hi // 3, 1)):
        y = top + plot_h - (tick / hi) * plot_h
        parts.append(
            f'<line x1="{left}" y1="{y:.1f}" x2="{width - right}" y2="{y:.1f}" '
            f'stroke="{GRID}" stroke-width="1"/>'
            f'<text x="{left - 8}" y="{y + 4:.1f}" fill="{INK_3}" font-size="15" '
            f'text-anchor="end">{tick}</text>'
        )

    # Direct end-labels only work when the series separate at the right edge. When
    # both sides finish on the same score they would sit on top of each other, and
    # nudging them apart detaches them from their lines -- so drop them and let the
    # legend, the axis and the war history table below carry the values.
    label_ends = abs(pts(ours)[-1][1] - pts(theirs)[-1][1]) >= 18

    for series, color, name in ((theirs, SERIES_2, "Them"), (ours, SERIES_1, "Us")):
        p = pts(series)
        d = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}" for i, (x, y) in enumerate(p))
        parts.append(
            f'<path d="{d}" fill="none" stroke="{color}" stroke-width="2" '
            f'stroke-linecap="round" stroke-linejoin="round"/>'
        )
        for (x, y), war, value in zip(p, played, series, strict=True):
            # Native tooltip: a hover layer with no scripting and no data fetching.
            tip = f"{name} {value} stars vs {war.get('opponent_name', 'opponent')}"
            parts.append(
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" fill="{color}" '
                f'stroke="{SURFACE}" stroke-width="2"><title>{escape(tip)}</title></circle>'
            )
        if label_ends:
            ex, ey = p[-1]
            parts.append(
                f'<text x="{ex + 11:.1f}" y="{ey + 5:.1f}" fill="{INK_3}" font-size="15" '
                f'font-weight="600">{series[-1]}</text>'
            )

    parts.append("</svg>")
    return "".join(parts)


# --- War Room primitives -------------------------------------------------

PHASES: Final = {
    "preparation": "prep",
    "inWar": "battle",
    "warEnded": "ended",
}


def star_row(earned: int | None, total: int = 3, *, size: int = 15) -> str:
    """Stars as glyphs, which is the native unit of this game.

    A row of filled and hollow stars reads instantly at any size; the integer 2
    does not. Drawn as SVG rather than text glyphs so it renders identically
    across platforms instead of inheriting whatever star the font happens to ship.
    """
    earned = 0 if earned is None else max(0, min(total, int(earned)))
    gap = size * 0.18
    width = total * size + (total - 1) * gap
    path = "M12 2.6l2.9 5.9 6.5.95-4.7 4.6 1.1 6.5L12 17.5 6.2 20.5l1.1-6.5-4.7-4.6 6.5-.95z"
    parts = [
        f'<svg class="stars" viewBox="0 0 {width:.1f} {size}" width="{width:.1f}" '
        f'height="{size}" role="img" aria-label="{earned} of {total} stars">'
    ]
    for i in range(total):
        x = i * (size + gap)
        scale = size / 24
        cls = "on" if i < earned else "off"
        parts.append(
            f'<g transform="translate({x:.1f},0) scale({scale:.3f})">'
            f'<path class="{cls}" d="{path}"/></g>'
        )
    parts.append("</svg>")
    return "".join(parts)


def _duels(
    conn: sqlite3.Connection, war: dict[str, Any] | None, player_tag: str
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """One row per matchup: our attacker, their base, and the live state of both.

    The war map replaces the old roster list, war-plan cards and cleanup board.
    They were three views of the same thing -- who is hitting whom, and how it is
    going -- so they collapse into one structure the page can render as facing
    columns, which is how players already picture a war.
    """
    if not war:
        return [], None

    war_id = war["war_id"]
    usage = {
        r["player_tag"]: dict(r)
        for r in conn.execute("SELECT * FROM member_war_usage WHERE war_id = ?", (war_id,))
    }
    defence = {b["name"]: b for b in cleanup_board(conn, war_id)}

    # Who each member ACTUALLY hit first. An assignment is a plan; once someone
    # attacks, showing their stars beside a base they never touched is simply
    # wrong, so the map switches to the real pairing as soon as one exists.
    actual = {
        r["attacker_tag"]: dict(r)
        for r in conn.execute(
            """
            SELECT a.attacker_tag, a.stars, a.new_stars,
                   d.player_tag AS defender_tag,
                   d.name AS defender, d.map_position AS defender_position,
                   d.townhall_level AS defender_th
            FROM attack_values a
            JOIN war_members d
              ON d.war_id = a.war_id AND d.player_tag = a.defender_tag
             AND d.side = 'opponent'
            WHERE a.war_id = ? AND a.attacker_side = 'clan'
              AND a.order_num = (
                  SELECT MIN(order_num) FROM attacks
                  WHERE war_id = a.war_id AND attacker_tag = a.attacker_tag
              )
            """,
            (war_id,),
        )
    }

    rows: list[dict[str, Any]] = []
    for plan in recommend_assignments(conn, war_id):
        used = usage.get(plan["attacker_tag"], {})
        hit = actual.get(plan["attacker_tag"])
        if hit:
            plan = {
                **plan,
                "defender_tag": hit["defender_tag"],
                "defender": hit["defender"],
                "defender_position": hit["defender_position"],
                "defender_th": hit["defender_th"],
                "th_diff": plan["attacker_th"] - hit["defender_th"],
                "went_off_plan": hit["defender"] != plan["defender"],
                "planned_defender": plan["defender"],
                # Stars from THIS attack, not the member's war total -- the row
                # shows one pairing, so it must show that pairing's result.
                "first_stars": hit["stars"],
                "first_new_stars": hit["new_stars"],
            }
        target = defence.get(plan["defender"], {})
        rows.append(
            {
                **plan,
                "attacks_used": used.get("attacks_used", 0),
                "attacks_available": used.get("attacks_available", 2),
                "attacks_left": used.get("attacks_missed", 2),
                "stars_scored": used.get("stars", 0),
                "target_stars": target.get("best_stars", 0),
                "target_state": target.get("state", "untouched"),
                "attempts": target.get("attempts", 0),
                "target_advice": target.get("advice", ""),
                "armies": army_options(conn, plan["attacker_tag"], plan["attacker_th"]),
                "outmatched": plan["th_diff"] <= -2,
                "is_me": plan["attacker_tag"] == player_tag,
            }
        )

    mine = next((r for r in rows if r["is_me"]), None)
    return rows, mine


def _feed(
    conn: sqlite3.Connection, war: dict[str, Any] | None, player_tag: str
) -> list[dict[str, Any]]:
    """Every attack in the war, in order, both sides.

    A war is an ordered event stream -- order_num is a monotonic per-war sequence
    and is already the backbone of the ingest design. Rendering it as a feed with
    a central spine, rather than a static snapshot, is the form that matches the
    data and needs no legend to read.
    """
    if not war:
        return []
    rows = conn.execute(
        """
        SELECT
            a.order_num, a.stars, a.new_stars, a.destruction_percentage,
            a.attacker_side, a.attacker_tag,
            atk.name AS attacker, atk.map_position AS attacker_position,
            atk.townhall_level AS attacker_th,
            dfn.name AS defender, dfn.map_position AS defender_position,
            dfn.townhall_level AS defender_th
        FROM attack_values a
        LEFT JOIN war_members atk
               ON atk.war_id = a.war_id AND atk.player_tag = a.attacker_tag
              AND atk.side = a.attacker_side
        LEFT JOIN war_members dfn
               ON dfn.war_id = a.war_id AND dfn.player_tag = a.defender_tag
              AND dfn.side = CASE WHEN a.attacker_side = 'clan' THEN 'opponent' ELSE 'clan' END
        WHERE a.war_id = ?
        ORDER BY a.order_num
        """,
        (war["war_id"],),
    )
    return [
        {**dict(r), "is_me": r["attacker_tag"] == player_tag, "ours": r["attacker_side"] == "clan"}
        for r in rows
    ]


def replay_payload(conn: sqlite3.Connection, war: dict[str, Any] | None, player_tag: str) -> str:
    """The war as a replayable event stream, embedded in the page as JSON.

    This is the one thing the archive can do that the game cannot: Supercell's API
    returns only current state, so once a war ends its blow-by-blow is gone. We
    kept every poll, and order_num is a monotonic per-war sequence, so the war can
    be wound back and played forward.

    Embedded at build time, not fetched. No request leaves the browser and the
    token is nowhere near it.
    """
    if not war:
        return "null"

    # Static per-entity context for the inspector. Attack-derived figures are
    # NOT baked in here -- those are recomputed in the page so the panel stays
    # truthful while the war is scrubbed. Only things that do not move with the
    # scrub (hero levels, lifetime record, the assignment) are embedded.
    heroes: dict[str, dict[str, int]] = {}
    for r in conn.execute(
        """
        SELECT player_tag, name, level FROM player_units
        WHERE category = 'hero' AND village = 'home'
          AND snapshot_date = (SELECT MAX(snapshot_date) FROM player_units)
        """
    ):
        short = {
            "Barbarian King": "BK",
            "Archer Queen": "AQ",
            "Grand Warden": "GW",
            "Royal Champion": "RC",
            "Minion Prince": "MP",
        }.get(r["name"])
        if short:
            heroes.setdefault(r["player_tag"], {})[short] = r["level"]

    record = {
        r["player_tag"]: {
            "wars": r["wars"],
            "usage": int(r["usage_pct"] or 0),
            "three": int(r["three_star_pct"]) if r["three_star_pct"] is not None else None,
        }
        for r in member_performance(conn)
    }
    roles = {
        r["player_tag"]: r["role"]
        for r in conn.execute(
            "SELECT player_tag, role FROM member_snapshots WHERE snapshot_date = "
            "(SELECT MAX(snapshot_date) FROM member_snapshots)"
        )
    }

    assigned_to: dict[str, str] = {}
    assigned_from: dict[str, dict[str, Any]] = {}
    for plan in recommend_assignments(conn, war["war_id"]):
        target = plan.get("planned_defender_tag") or plan["defender_tag"]
        assigned_to[plan["attacker_tag"]] = plan["defender"]
        assigned_from[target] = {
            "name": plan["attacker"],
            "th": plan["attacker_th"],
            "over": -plan["th_diff"] if plan["th_diff"] <= -2 else 0,
        }

    roster = []
    for r in conn.execute(
        "SELECT player_tag, name, map_position, townhall_level, side "
        "FROM war_members WHERE war_id = ? ORDER BY side, map_position",
        (war["war_id"],),
    ):
        tag = r["player_tag"]
        entry: dict[str, Any] = {
            "tag": tag,
            "name": r["name"],
            "pos": r["map_position"],
            "th": r["townhall_level"],
            "side": r["side"],
            "me": tag == player_tag,
        }
        if r["side"] == "clan":
            entry["heroes"] = heroes.get(tag, {})
            entry["role"] = roles.get(tag)
            entry["record"] = record.get(tag)
            entry["orders"] = assigned_to.get(tag)
            best = next(iter(army_options(conn, tag, r["townhall_level"])), None)
            if best:
                entry["army"] = {
                    "name": best["name"],
                    "ok": best["viable"],
                    "atLevel": best["at_level"],
                    "blockers": best["blockers"][:3],
                    "troops": best["composition"],
                }
        else:
            entry["from"] = assigned_from.get(tag)
        roster.append(entry)
    attacks = [
        {
            "n": r["order_num"],
            "by": r["attacker_tag"],
            "on": r["defender_tag"],
            "s": r["stars"],
            "d": round(r["destruction_percentage"] or 0),
            "ours": r["attacker_side"] == "clan",
        }
        for r in conn.execute(
            "SELECT order_num, attacker_tag, defender_tag, stars, "
            "destruction_percentage, attacker_side FROM attack_values "
            "WHERE war_id = ? ORDER BY order_num",
            (war["war_id"],),
        )
    ]
    return json.dumps(
        {
            "roster": roster,
            "attacks": attacks,
            "teamSize": war["team_size"],
            "perMember": war["attacks_per_member"] or 1,
            "opponent": war["opponent_name"],
        },
        separators=(",", ":"),
    )


def _map_rows(
    conn: sqlite3.Connection, war: dict[str, Any] | None, player_tag: str
) -> list[dict[str, Any]]:
    """The war map, paired by map position: our #1 faces their #1.

    Pairing by *assignment* looked right until two members were assigned the same
    base and it rendered that base twice. A map shows each base exactly once;
    who attacks whom is a separate question, answered in the orders and details.
    """
    if not war:
        return []
    sides: dict[str, dict[int, dict[str, Any]]] = {"clan": {}, "opponent": {}}
    for r in conn.execute(
        "SELECT player_tag, name, map_position, townhall_level, side "
        "FROM war_members WHERE war_id = ?",
        (war["war_id"],),
    ):
        sides[r["side"]][r["map_position"]] = dict(r)

    positions = sorted(set(sides["clan"]) | set(sides["opponent"]))
    rows = []
    for pos in positions:
        us, them = sides["clan"].get(pos), sides["opponent"].get(pos)
        diff = us["townhall_level"] - them["townhall_level"] if us and them else 0
        rows.append(
            {
                "position": pos,
                "us": us,
                "them": them,
                "is_me": bool(us and us["player_tag"] == player_tag),
                "over": -diff if diff <= -2 else 0,
            }
        )
    return rows
