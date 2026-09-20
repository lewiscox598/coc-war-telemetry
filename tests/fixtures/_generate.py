"""Regenerate the war-state fixtures.

war_preparation.json is a verbatim capture of #2U9QLCY8Y taken on 2026-09-20 while a
regular war sat in preparation. Every other war fixture is derived from it by adding
a plausible battle-day attack sequence, so they keep the real payload's exact shape
-- including the sparse member objects and the ISO-8601 basic timestamps -- rather
than encoding assumptions about what the API returns.

Replace these with genuine captures once real battle-day and CWL data exist.

Run with: uv run python tests/fixtures/_generate.py
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

FIX = Path(__file__).parent
PREP = json.loads((FIX / "war_preparation.json").read_text(encoding="utf-8"))

CLAN = [m["tag"] for m in PREP["clan"]["members"]]
OPP = [m["tag"] for m in PREP["opponent"]["members"]]


def attack(order: int, atk: str, dfn: str, stars: int, dest: int, dur: int) -> dict:
    return {
        "order": order,
        "attackerTag": atk,
        "defenderTag": dfn,
        "stars": stars,
        "destructionPercentage": dest,
        "duration": dur,
    }


# Order is a global monotonic per-war counter spanning both sides, which is what
# makes (war_id, order) a safe idempotency key. The sequence deliberately includes:
#   - a cleanup (order 7) on a base already at 2 stars, worth 1 new star not 3
#   - a member who uses only one attack (CLAN[3])
#   - a member who never attacks at all (CLAN[4]), invisible in the attacks table
SEQUENCE = [
    attack(1, CLAN[0], OPP[0], 3, 100, 112),
    attack(2, OPP[0], CLAN[1], 2, 78, 165),
    attack(3, CLAN[1], OPP[1], 2, 85, 180),
    attack(4, CLAN[2], OPP[2], 1, 52, 190),
    attack(5, OPP[1], CLAN[0], 3, 100, 121),
    attack(6, CLAN[3], OPP[3], 2, 71, 175),
    attack(7, CLAN[0], OPP[1], 3, 100, 95),
    attack(8, OPP[2], CLAN[2], 1, 44, 200),
    attack(9, CLAN[1], OPP[4], 0, 22, 205),
    attack(10, CLAN[2], OPP[2], 3, 100, 130),
    attack(11, OPP[3], CLAN[3], 2, 66, 188),
]


def build(state: str, attacks: list[dict], *, cwl: bool = False) -> dict:
    war = copy.deepcopy(PREP)
    war["state"] = state

    by_attacker: dict[str, list[dict]] = {}
    for a in attacks:
        by_attacker.setdefault(a["attackerTag"], []).append(a)

    for side in ("clan", "opponent"):
        for m in war[side]["members"]:
            if mine := by_attacker.get(m["tag"]):
                m["attacks"] = mine
            received = [a for a in attacks if a["defenderTag"] == m["tag"]]
            m["opponentAttacks"] = len(received)
            if received:
                m["bestOpponentAttack"] = max(
                    received, key=lambda a: (a["stars"], a["destructionPercentage"])
                )

    # Side totals are the sum of each enemy base's best result, as the API reports.
    for side, enemy in (("clan", "opponent"), ("opponent", "clan")):
        stars = dest = 0.0
        for m in war[enemy]["members"]:
            best = max(
                (a for a in attacks if a["defenderTag"] == m["tag"]),
                key=lambda a: (a["stars"], a["destructionPercentage"]),
                default=None,
            )
            if best:
                stars += best["stars"]
                dest += best["destructionPercentage"]
        war[side]["stars"] = int(stars)
        war[side]["destructionPercentage"] = round(dest / len(war[enemy]["members"]), 2)

    if cwl:
        war["warTag"] = "#2PP0JCCL"
        war.pop("attacksPerMember", None)  # wars from the leagues path omit this
    return war


def main() -> None:
    written = {
        "war_not_in_war.json": {"state": "notInWar"},
        "warlog_private_403.json": {"reason": "accessDenied", "message": "war log is private"},
        "war_in_war_3_attacks.json": build("inWar", SEQUENCE[:3]),
        "war_in_war_11_attacks.json": build("inWar", SEQUENCE),
        "war_ended.json": build("warEnded", SEQUENCE),
        "cwl_round.json": build("inWar", SEQUENCE[:6], cwl=True),
    }
    for name, payload in written.items():
        (FIX / name).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(f"  wrote {name}")


if __name__ == "__main__":
    main()
