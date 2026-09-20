"""Static site generation.

Everything is rendered at build time. The page makes no API calls and fetches no
data: the token never reaches the client, and the site works with JavaScript
disabled apart from optional table sorting.

Timestamps are stored as UTC and rendered in Europe/London.
"""

from __future__ import annotations

import shutil
import sqlite3
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import Any
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

    war = current_war(conn)
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

    # War plan: only meaningful while a war exists. Each attacker carries the
    # evidence behind its target and the armies they can actually field.
    plan: list[dict[str, Any]] = []
    cleanup: list[dict[str, Any]] = []
    if war:
        for row in recommend_assignments(conn, war["war_id"]):
            row = dict(row)
            row["armies"] = army_options(conn, row["attacker_tag"], row["attacker_th"])
            plan.append(row)
        if war["state"] in ("inWar", "warEnded"):
            cleanup = cleanup_board(conn, war["war_id"])

    return {
        "war": war,
        "roster": roster,
        "plan": plan,
        "cleanup": cleanup,
        "plan_expected": round(sum(p["expected_stars"] for p in plan), 1),
        "plan_max": len(plan) * 3,
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
) -> list[Path]:
    env = _environment(templates)
    context = gather(conn, player_tag)

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
        first_capture=first_capture,
    )

    output.mkdir(parents=True, exist_ok=True)
    (output / "me").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(templates / "style.css", output / "style.css")

    written: list[Path] = []
    for template_name, target, page, root in [
        ("clan.html", output / "index.html", "clan", ""),
        ("me.html", output / "me" / "index.html", "me", "../"),
    ]:
        html = env.get_template(template_name).render(**context, page=page, root=root)
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

SURFACE = "#1c1c1b"
SERIES_1 = "#3987e5"
SERIES_2 = "#d95926"
INK_3 = "#78786f"
GRID = "#2e2e2b"


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
