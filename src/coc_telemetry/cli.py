"""Command line entry points. These are what the GitHub Actions workflows call.

Commands are deliberately chatty on stdout so a workflow log explains itself, and
deliberately silent about the token: it is never printed, logged, or interpolated
into a message.
"""

from __future__ import annotations

import argparse
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

from coc_telemetry.capture import CaptureStore
from coc_telemetry.client import ApiResult, CocApiError, CocClient, normalise_tag
from coc_telemetry.ingest import connect, ingest_capture, rebuild
from coc_telemetry.site import build_site

DEFAULT_CONFIG = Path("config.toml")
DEFAULT_RAW = Path("data/raw")
DEFAULT_DB = Path("data/telemetry.db")

# The league group uses this placeholder for rounds that have not been drawn yet.
UNSET_WAR_TAG = "#0"


@dataclass(frozen=True)
class Config:
    clan_tag: str
    player_tag: str
    poll_all_members: bool

    @classmethod
    def load(cls, path: Path = DEFAULT_CONFIG) -> Config:
        if not path.is_file():
            raise SystemExit(f"config not found: {path}")
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        return cls(
            clan_tag=normalise_tag(raw["clan_tag"]),
            player_tag=normalise_tag(raw["player_tag"]),
            poll_all_members=bool(raw.get("poll_all_members", False)),
        )


class Runner:
    """Capture-then-ingest, so the archive is written before anything derived."""

    def __init__(self, config: Config, raw: Path, db: Path) -> None:
        self.config = config
        self.store = CaptureStore(raw)
        self.db_path = db
        self.captured: list[str] = []

    def handle(self, client_result: ApiResult, *, ingest: bool = True) -> None:
        path = self.store.write(client_result)
        status = "OK" if client_result.ok else str(client_result.status_code)
        if path is None:
            print(f"  [{status}] {client_result.endpoint}: unchanged")
            return
        self.captured.append(client_result.endpoint)
        print(f"  [{status}] {client_result.endpoint}: {path}")
        if ingest and client_result.ok:
            capture = self.store.read(path)
            conn = connect(self.db_path)
            try:
                ingest_capture(conn, capture, self.config.clan_tag)
                conn.commit()
            finally:
                conn.close()

    def summary(self) -> str:
        return ", ".join(sorted(set(self.captured))) if self.captured else "no changes"


def cmd_poll(args: argparse.Namespace) -> int:
    """War poller. Runs every 15 minutes.

    Missed runs are harmless: every poll returns the cumulative attack list, so a gap
    self-heals on the next successful poll.
    """
    config = Config.load(args.config)
    runner = Runner(config, args.raw, args.db)
    print(f"polling war for {config.clan_tag}")

    with CocClient() as client:
        war = client.current_war(config.clan_tag)
        runner.handle(war)
        state = war.data.get("state") if war.ok else None
        print(f"  state: {state}")

        # During CWL the regular endpoint reports notInWar, so fall back to the
        # league group and poll each round war tag individually.
        if state == "notInWar":
            group = client.league_group(config.clan_tag)
            if group.ok:
                print("  in CWL, polling round war tags")
                runner.handle(group, ingest=False)
                for war_tag in _round_war_tags(group.data):
                    runner.handle(client.league_war(war_tag))
            else:
                print(f"  not in CWL ({group.data.get('reason')})")

    print(f"captured: {runner.summary()}")
    return 0


def _round_war_tags(group: dict) -> list[str]:
    """Every drawn war tag in the league group, skipping undrawn rounds."""
    tags: list[str] = []
    for rnd in group.get("rounds", []) or []:
        tags.extend(t for t in rnd.get("warTags", []) or [] if t and t != UNSET_WAR_TAG)
    return tags


def cmd_nightly(args: argparse.Namespace) -> int:
    """Clan roster, player records and capital raid seasons. Runs at 03:00 UTC."""
    config = Config.load(args.config)
    runner = Runner(config, args.raw, args.db)
    print(f"nightly snapshot for {config.clan_tag}")

    with CocClient() as client:
        runner.handle(client.clan(config.clan_tag), ingest=False)
        members = client.clan_members(config.clan_tag)
        runner.handle(members)

        # A private war log 403s without blocking anything else.
        log = client.war_log(config.clan_tag)
        runner.handle(log, ingest=False)
        if log.access_denied:
            print("  war log is private, continuing")

        tags = [config.player_tag]
        if config.poll_all_members and members.ok:
            tags = [m["tag"] for m in members.data.get("items", [])]
            if config.player_tag not in tags:
                tags.append(config.player_tag)
        print(f"  polling {len(tags)} player record(s)")
        for tag in tags:
            runner.handle(client.player(tag))

        runner.handle(client.capital_raid_seasons(config.clan_tag))

    print(f"captured: {runner.summary()}")
    return 0


def cmd_rebuild(args: argparse.Namespace) -> int:
    """Drop the database and reconstruct it from data/raw alone."""
    config = Config.load(args.config)
    store = CaptureStore(args.raw)
    print(f"rebuilding {args.db} from {args.raw}")
    counts = rebuild(args.db, store, config.clan_tag)
    print(f"folded {counts['captures']} captures, skipped {counts['skipped']} error bodies")

    conn = connect(args.db)
    try:
        for table in (
            "wars",
            "war_members",
            "attacks",
            "member_snapshots",
            "player_snapshots",
            "player_units",
            "capital_raids",
        ):
            n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f"  {table:20} {n}")
    finally:
        conn.close()
    return 0


def cmd_build_site(args: argparse.Namespace) -> int:
    """Render the static site. Deployed by deploy.yml in the same workflow run.

    Always real data. An earlier version substituted a recorded war while the
    live one sat in preparation, on the reasoning that a war with no attacks
    left the page empty. That was wrong twice over: the page is not empty --
    roster, Town Hall levels, hero levels, assignments and army walkthroughs
    are all live and present before a single attack -- and swapping in a
    recorded scoreline meant the page showed a war that never happened.
    """
    config = Config.load(args.config)
    store = CaptureStore(args.raw)
    conn = connect(args.db)
    try:
        clan_name, clan = _clan_identity(store, config.clan_tag)
        written = build_site(
            conn,
            args.output,
            templates=args.templates,
            clan_name=clan_name,
            player_tag=config.player_tag,
            store=store,
            clan=clan,
        )
    finally:
        conn.close()
    for path in written:
        print(f"  wrote {path}")
    return 0


def _clan_identity(store: CaptureStore, clan_tag: str) -> tuple[str, dict | None]:
    """Clan name and profile from the newest /clans/{tag} capture.

    Read from the archive rather than the API so a site build needs no token and
    can run on any checkout.
    """
    latest = store.latest_path("clan")
    if latest is None:
        return clan_tag, None
    data = store.read(latest).data
    return data.get("name", clan_tag), data


def cmd_check(args: argparse.Namespace) -> int:
    """Verify config, token and connectivity before trusting a scheduled run."""
    config = Config.load(args.config)
    print(f"clan   {config.clan_tag}")
    print(f"player {config.player_tag}")
    with CocClient() as client:
        clan = client.clan(config.clan_tag)
        if not clan.ok:
            print(f"FAIL: {clan.data.get('reason')} - {clan.data.get('message')}")
            return 1
        print(f"OK: {clan.data['name']}, {clan.data['members']} members")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="coc-telemetry", description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--raw", type=Path, default=DEFAULT_RAW)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--output", type=Path, default=Path("site"))
    parser.add_argument("--templates", type=Path, default=Path("templates"))
    sub = parser.add_subparsers(dest="command", required=True)

    subparsers = {}
    for name, fn, help_text in [
        ("poll", cmd_poll, "poll current war, falling back to CWL"),
        ("nightly", cmd_nightly, "snapshot roster, players and capital raids"),
        ("rebuild", cmd_rebuild, "rebuild the database from data/raw"),
        ("build-site", cmd_build_site, "render the static site"),
        ("check", cmd_check, "verify token and connectivity"),
    ]:
        subparsers[name] = sub.add_parser(name, help=help_text)
        subparsers[name].set_defaults(func=fn)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except CocApiError as exc:
        # Message text is safe: the client never puts the token in an exception.
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
