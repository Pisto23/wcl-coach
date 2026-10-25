"""Analysis runs shared by the CLI and the UI: pick fights, fetch, analyze – no output."""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Callable, Iterator

from .analysis import INFO, Finding, Settings, run_all
from .api import WCLError
from .fetch import Fetcher, Fight, PlayerFight
from .raid import FightReport, RaidSettings, ability_rows, analyze_fight, lost_at

Log = Callable[[str], None]
PlayerResult = tuple[PlayerFight, list[PlayerFight], list[Finding]]


RESULT_WORDS = {"kills": True, "kill": True, "wipes": False, "wipe": False}  # suffix ':kills' -> kill == True


def _pulls(fights: list[Fight], target: str) -> list[Fight]:
    """Fights of one selection part: everything, an ID, an ID range or a boss name (exact, else fragment)."""
    if target in ("", "all"):
        return fights
    if m := re.fullmatch(r"(\d+)\s*-\s*(\d+)", target):
        return [f for f in fights if int(m[1]) <= f.id <= int(m[2])]
    if target.isdigit():
        return [f for f in fights if f.id == int(target)]
    exact = [f for f in fights if f.name.lower() == target]
    return exact or [f for f in fights if target in f.name.lower()]


def select_fights(fights: list[Fight], sel: str | None, url_fight: int | None) -> list[Fight]:
    if not fights:
        raise WCLError("Report enthält keine Boss-Kämpfe.")
    sel = sel or (str(url_fight) if url_fight else "last-kill")
    kills = [f for f in fights if f.kill]
    if sel == "last-kill":
        return [kills[-1] if kills else fights[-1]]
    if sel == "last":
        return [fights[-1]]
    if sel == "kills":
        return kills or [fights[-1]]
    if sel == "all":
        return fights
    # comma-separated parts, each optionally limited to kills or wipes: "sszorak:wipes", "23-41:kills", "wipes"
    picked: set[int] = set()
    for part in (x.strip().lower() for x in sel.split(",")):
        target, colon, word = part.rpartition(":")
        if colon and word.strip() in RESULT_WORDS:
            want: bool | None = RESULT_WORDS[word.strip()]
            target = target.strip()
        elif part in RESULT_WORDS:
            want, target = RESULT_WORDS[part], "all"
        else:
            want, target = None, part
        if target or want is not None:
            picked.update(f.id for f in _pulls(fights, target) if want is None or f.kill == want)
    chosen = [f for f in fights if f.id in picked]
    if not chosen:
        raise WCLError(f"Keine Kämpfe für '{sel}' gefunden. 'wcl-coach fights <report>' zeigt alle.")
    return chosen


def player_fights(
    fx: Fetcher,
    code: str,
    fights: list[Fight],
    player_name: str | None,
    source_id: int | None,
    top: int,
    settings: Settings,
    log: Log,
) -> Iterator[PlayerResult]:
    """One player vs. top parses, fight by fight. Fights the player wasn't in are skipped."""
    for fight in fights:
        log(f"→ {fight.name} (#{fight.id})")
        details, r_dps, r_hps = fx.player_details(code, fight.id)
        try:
            player = fx.resolve_player(details, player_name, source_id)
        except WCLError as e:
            log(f"  übersprungen: {e}")
            continue
        me = fx.player_fight(code, fight, player)
        me.rank_percent, me.amount_per_s = fx.rank_for(r_hps if player.role == "healer" else r_dps, player)
        tops = fx.top_players(me, n=top) if top > 0 else []
        yield me, tops, run_all(me, tops, settings)


def raid_fights(fx: Fetcher, code: str, fights: list[Fight], settings: RaidSettings, log: Log) -> Iterator[FightReport]:
    """Every player of every fight, measured against the rest of the raid."""
    names = fx.ability_names(code)
    for fight in fights:
        log(f"→ {fight.name} (#{fight.id})")
        details, _, _ = fx.player_details(code, fight.id)
        players = Fetcher.players(details)
        if not players:
            log("  übersprungen: keine Spieler gefunden")
            continue
        deaths = fx.deaths(code, fight)
        until = lost_at(fight, players, deaths, settings)
        rf = fx.raid_fight(code, fight, players, deaths, until=until, ability_names=names)
        yield analyze_fight(rf, settings)


def load_breakdown(fx: Fetcher, code: str, reports: list[FightReport], name: str) -> int:
    """Fills one player's damage and healing per ability into the pulls that lack it. Costs API points
    per player and pull, so the UI only does it for the character on screen. Returns the pulls loaded."""
    todo: dict[int, list[FightReport]] = defaultdict(list)
    for r in reports:
        st = r.stats.get(name)
        p = next((p for p in r.players if p.name == name), None)
        if st and p and (st.damage is None or st.healing is None):
            todo[p.id].append(r)
    for pid, rs in todo.items():
        tables = fx.breakdown(code, pid, [(r.fight.id, r.fight.start, r.until) for r in rs])
        for r in rs:
            done, healing = tables[r.fight.id]
            r.stats[name].damage, r.stats[name].healing = ability_rows(done), ability_rows(healing)
    return sum(len(rs) for rs in todo.values())


def recurring(results: list[PlayerResult]) -> list[list[Finding]]:
    """Findings that repeat across pulls, most frequent first – those are the real habits."""
    groups: dict[str, list[Finding]] = defaultdict(list)
    for _, _, findings in results:
        for f in findings:
            if f.severity == INFO:
                continue
            key = f"{f.category}|" + re.sub(r"[\d.,:]+", "#", f.title)
            groups[key].append(f)
    return sorted(
        (fs for fs in groups.values() if len(fs) >= 2),
        key=lambda fs: (-len(fs), -max(f.severity for f in fs)),
    )
