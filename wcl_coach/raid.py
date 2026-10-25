"""Raid mode: every player of a pull, measured against the rest of the raid.

Damage done doesn't decide what counts as a fail – the question is who died, who
stood in what and who came unprepared. The raid is its own benchmark: an ability
most of the raid took no damage from was very likely avoidable. No top logs
needed, which also keeps the API cost per pull low. Damage, healing, auras and
kicks per player are collected as plain stats next to the fails.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from typing import Iterable

from .analysis import (
    COMBAT_POT_RE,
    HEAL_CONSUMABLE_RE,
    HIGH,
    LOW,
    MEDIUM,
    NON_COMBAT_POT_RE,
    _norm_name,
    fmt_t,
    killing_blow,
)
from .fetch import Fight, Player, RaidFight

DEATH, AVOIDABLE, PREP = "Tod", "Vermeidbarer Schaden", "Vorbereitung"

FLASK_RE = re.compile(r"flask|phial|fläschchen", re.I)
FOOD_RE = re.compile(r"well fed|satt", re.I)
ENEMY_SOURCES = {"Boss", "NPC"}  # anything else is a player class or pet – own/friendly damage
# adds hitting a dps is a tanking/aggro question, not positioning
NEVER_AVOIDABLE = {"melee"}


@dataclass
class RaidSettings:
    avoidable_max_share: float = 0.5  # hit at most this share of non-tanks -> likely avoidable
    min_taken_share: float = 0.02  # ignore abilities below this share of a player's damage taken
    consumable_min_share: float = 0.5  # flag a missing consumable only if this share of the raid had it
    wipe_tail_s: float = 15.0  # deaths this close to the end of a wipe belong to the wipe
    collapse_window_s: float = 10.0  # on a wipe, this share of the raid dying within this window ...
    collapse_share: float = 0.2  # ... means the pull collapsed – from there on it's the wipe
    ignore: tuple[str, ...] = ()  # ability name fragments never counted as avoidable


@dataclass
class Fail:
    severity: int
    category: str
    player: Player
    fight: Fight
    title: str
    subject: str = ""  # killing blow / ability / consumable – groups fails across pulls
    impact: float = 0.0
    spells: dict[str, int] = field(default_factory=dict)  # ability name in the title -> spell ID (tooltips)


@dataclass
class Death:
    player: Player
    t: float  # seconds into the fight
    killing_blow: str
    wipe: bool  # the pull was already lost – not counted as a fail
    spell_id: int | None = None
    recap: list[tuple[str, int | None, float, float]] = field(default_factory=list)  # see _recap


@dataclass
class Stat:
    """One row of the stats view: an ability, aura or interrupted spell. All numbers add up across pulls."""

    name: str
    spell_id: int | None = None
    amount: float = 0.0  # damage, healing or damage taken
    count: int = 0  # hits, casts, applications, interrupts or dispels
    crits: int = 0
    overheal: float = 0.0
    uptime: float = 0.0  # seconds the aura was up
    friendly: bool = False  # damage taken / debuff from the player themselves or the raid, not an enemy


STAT_LISTS = ("taken", "buffs", "debuffs", "casts", "kicks", "dispels")


@dataclass
class PlayerStats:
    """What one player did in a pull – shown next to the fails, not judged."""

    seconds: float  # time the numbers cover: a lost wipe stops at the collapse
    damage_total: float = 0.0
    healing_total: float = 0.0
    overheal_total: float = 0.0
    taken: list[Stat] = field(default_factory=list)
    buffs: list[Stat] = field(default_factory=list)
    debuffs: list[Stat] = field(default_factory=list)
    casts: list[Stat] = field(default_factory=list)
    kicks: list[Stat] = field(default_factory=list)  # interrupted enemy spells
    dispels: list[Stat] = field(default_factory=list)  # removed auras
    # per ability – costs API points per player, so it's loaded on demand (pipeline.load_breakdown)
    damage: list[Stat] | None = None
    healing: list[Stat] | None = None
    pulls: int = 1

    @property
    def dps(self) -> float:
        return self.damage_total / self.seconds

    @property
    def hps(self) -> float:
        return self.healing_total / self.seconds


@dataclass
class FightReport:
    fight: Fight
    players: list[Player]
    deaths: list[Death]
    fails: list[Fail]
    kicks: Counter  # player name -> interrupts
    kicks_through: list[tuple[str, int, int]]  # (ability, went through, interrupted)
    spells: dict[str, int] = field(default_factory=dict)  # killing blows and kicked spells -> spell ID
    until: float = 0.0  # ms where the stats stop: the end, or when a wipe was lost
    stats: dict[str, PlayerStats] = field(default_factory=dict)  # player name -> stats

    @property
    def first_death(self) -> Death | None:
        return self.deaths[0] if self.deaths and not self.deaths[0].wipe else None


def fmt_amount(x: float) -> str:
    if x >= 1e6:
        return f"{x / 1e6:.1f}M"
    if x >= 1e3:
        return f"{x / 1e3:.0f}k"
    return f"{x:.0f}"


# ================================================================ deaths


def _recap(death: dict, n: int = 5) -> list[tuple[str, int | None, float, float]]:
    """The last hits before a death, latest first: (ability, spell ID, damage, seconds before the death)."""
    t0 = death.get("timestamp", 0)
    hits = [
        e for e in death.get("events") or []
        if e.get("type") == "damage" and e.get("amount") and (e.get("ability") or {}).get("name")
    ]
    hits.sort(key=lambda e: -e.get("timestamp", 0))
    return [
        (e["ability"]["name"], e["ability"].get("guid"), float(e.get("amount", 0)), (t0 - e.get("timestamp", t0)) / 1000)
        for e in hits[:n]
    ]


def classify_deaths(fight: Fight, players: list[Player], deaths: list[dict], s: RaidSettings) -> list[Death]:
    by_id = {p.id: p for p in players}
    by_name = {p.name.lower(): p for p in players}
    rows: list[tuple[Player, float, tuple[str, int | None], dict]] = []
    for d in sorted(deaths, key=lambda x: x.get("timestamp", 0)):
        p = by_id.get(d.get("id")) or by_name.get((d.get("name") or "").lower())
        if not p:
            continue  # pets, NPCs
        t = (d.get("timestamp", fight.start) - fight.start) / 1000.0
        rows.append((p, t, killing_blow(d), d))

    lost = float("inf")
    if not fight.kill:
        ts = [t for _, t, _, _ in rows]
        burst = max(3, s.collapse_share * len(players))
        for i, t in enumerate(ts):
            # the pull is lost once the raid collapses, half of it is dead or the pull is about to end
            if (
                sum(1 for u in ts[i:] if u - t <= s.collapse_window_s) >= burst
                or i >= len(players) / 2
                or fight.duration_s - t <= s.wipe_tail_s
            ):
                lost = t
                break
    return [Death(p, t, kb, t >= lost, kb_id, _recap(d)) for p, t, (kb, kb_id), d in rows]


def lost_at(fight: Fight, players: list[Player], deaths: list[dict], s: RaidSettings) -> float | None:
    """Timestamp (ms) from which a wipe was lost. Damage after that would only blame the survivors."""
    for d in classify_deaths(fight, players, deaths, s):
        if d.wipe:
            return fight.start + d.t * 1000
    return None


def _death_fails(rf: RaidFight, deaths: list[Death]) -> list[Fail]:
    out: list[Fail] = []
    for i, d in enumerate(deaths):
        if d.wipe:
            continue
        pct = d.t / rf.fight.duration_s * 100
        notes = ["erster Tod"] if i == 0 else []
        if not any(HEAL_CONSUMABLE_RE.search(c.get("name", "")) for c in rf.casts.get(d.player.id, [])):
            notes.append("kein Healthstone/Heiltrank benutzt")
        out.append(
            Fail(
                HIGH if pct < 90 else MEDIUM,
                DEATH,
                d.player,
                rf.fight,
                f"bei {fmt_t(d.t)} ({pct:.0f}%) durch '{d.killing_blow}'" + (f" – {', '.join(notes)}" if notes else ""),
                subject=d.killing_blow,
                impact=rf.fight.duration_s - d.t,
                spells={d.killing_blow: d.spell_id} if d.spell_id else {},
            )
        )
    return out


# ========================================================= avoidable damage


def _enemy_amount(e: dict) -> float:
    """Damage of a table entry that came from enemies. Own spells (Burning Rush, Refraction, ...)
    and friendly fire are class mechanics, not fails."""
    if e.get("sources"):
        return sum(float(s.get("total", 0)) for s in e["sources"] if s.get("type") in ENEMY_SOURCES)
    if "actorType" in e:
        return float(e.get("total", 0)) if e["actorType"] in ENEMY_SOURCES else 0.0
    return float(e.get("total", 0))  # no source info – count it


def _taken(entries: list[dict]) -> dict[str, list]:
    """name-key -> [display name, enemy damage, hits, spell ID]. Keyed by name so variant spell IDs merge."""
    idx: dict[str, list] = {}
    for e in entries:
        amount = _enemy_amount(e)
        if amount <= 0:
            continue
        name = e.get("name") or str(e.get("guid"))
        row = idx.setdefault(_norm_name(name), [name, 0.0, 0, e.get("guid")])
        row[1] += amount
        row[2] += int(e.get("hitCount", 0) or 0) + int(e.get("tickCount", 0) or 0)
    return idx


def _avoidable(rf: RaidFight, s: RaidSettings) -> list[Fail]:
    # Tanks take boss melee, busters and frontals by design – they are neither judged nor the baseline.
    nontanks = [p for p in rf.players if p.role != "tank"]
    taken = {p.id: _taken(rf.damage_taken.get(p.id, [])) for p in nontanks}
    totals = {p.id: sum(float(e.get("total", 0)) for e in rf.damage_taken.get(p.id, [])) or 1.0 for p in nontanks}
    out: list[Fail] = []
    for k in {k for t in taken.values() for k in t} - NEVER_AVOIDABLE:
        hit = [p for p in nontanks if k in taken[p.id]]
        if len(hit) > s.avoidable_max_share * len(nontanks):
            continue  # raid-wide damage
        name = taken[hit[0].id][k][0]
        if any(frag.lower() in name.lower() for frag in s.ignore):
            continue
        for p in hit:
            _, amount, hits, guid = taken[p.id][k]
            share = amount / totals[p.id]
            if share < s.min_taken_share:
                continue
            out.append(
                Fail(
                    HIGH if share >= 0.10 else MEDIUM if share >= 0.04 else LOW,
                    AVOIDABLE,
                    p,
                    rf.fight,
                    f"'{name}': "
                    + (f"{hits} Treffer, " if hits else "")
                    + f"{fmt_amount(amount)} Schaden ({share * 100:.0f}% des erhaltenen Schadens) – "
                    f"{len(hit)}/{len(nontanks)} Spieler (ohne Tanks) getroffen",
                    subject=name,
                    impact=share * 100,
                    spells={name: guid} if guid else {},
                )
            )
    return out


# ============================================================ consumables


def _has_buff(rf: RaidFight, pid: int, pattern: re.Pattern) -> bool:
    return any(pattern.search(b.get("name", "")) for b in rf.buffs.get(pid, []))


def _used_combat_pot(rf: RaidFight, pid: int) -> bool:
    names = (c.get("name", "") for c in rf.casts.get(pid, []))
    return any(COMBAT_POT_RE.search(n) and not NON_COMBAT_POT_RE.search(n) for n in names)


CONSUMABLES = (
    ("Flask", "Kein Flask", MEDIUM, lambda rf, pid: _has_buff(rf, pid, FLASK_RE)),
    ("Food", "Kein Food-Buff", MEDIUM, lambda rf, pid: _has_buff(rf, pid, FOOD_RE)),
    ("Kampftrank", "Kein Kampftrank", LOW, _used_combat_pot),
)


def _consumables(rf: RaidFight, s: RaidSettings) -> list[Fail]:
    out: list[Fail] = []
    n = len(rf.players)
    for kind, title, sev, has in CONSUMABLES:
        if kind == "Kampftrank" and rf.fight.duration_s < 60:
            continue
        have = {p.id for p in rf.players if has(rf, p.id)}
        # detection is name-based: if most of the raid "lacks" it, the pattern simply didn't match
        if len(have) < s.consumable_min_share * n:
            continue
        for p in rf.players:
            if p.id not in have:
                out.append(Fail(sev, PREP, p, rf.fight, f"{title} ({len(have)}/{n} im Raid hatten einen)",
                                subject=kind))
    return out


# ============================================================= interrupts


def _interrupts(rf: RaidFight) -> tuple[Counter, list[tuple[str, int, int]], dict[str, int]]:
    names = {p.id: p.name for p in rf.players}
    kicks: Counter = Counter()
    kicked: Counter = Counter()
    for e in rf.interrupts:
        if e.get("sourceID") in names:
            kicks[names[e["sourceID"]]] += 1
            kicked[e.get("extraAbilityGameID")] += 1
    begins: Counter = Counter()
    casts: Counter = Counter()
    for e in rf.enemy_casts:
        (begins if e.get("type") == "begincast" else casts)[e.get("abilityGameID")] += 1
    through = []
    spells: dict[str, int] = {}
    for gid, n in kicked.items():
        # Cast-time spells: begincast = attempt, cast = went through.
        # Channels have no begincast and log "cast" at the start, so kicked channels must be subtracted.
        attempts = max(begins[gid], casts[gid])
        name = rf.ability_names.get(gid, str(gid))
        through.append((name, min(casts[gid], max(0, attempts - n)), n))
        if gid:
            spells[name] = gid
    through.sort(key=lambda x: (-x[1], -x[2]))
    return kicks, through, spells


# ================================================================== stats


def _hits(e: dict) -> int:
    return int(e.get("hitCount", 0) or 0) + int(e.get("tickCount", 0) or 0)


def _rows(stats: Iterable[Stat]) -> list[Stat]:
    """Merges rows of the same name (variant spell IDs, several pulls); enemy rows and big numbers first."""
    merged: dict[str, Stat] = {}
    for st in stats:
        m = merged.get(k := _norm_name(st.name))
        if m is None:
            merged[k] = replace(st)
            continue
        m.amount += st.amount
        m.count += st.count
        m.crits += st.crits
        m.overheal += st.overheal
        m.uptime += st.uptime
        m.spell_id = m.spell_id or st.spell_id
        m.friendly = m.friendly and st.friendly
    return sorted(merged.values(), key=lambda x: (x.friendly, -x.amount, -x.uptime, -x.count, x.name.lower()))


def ability_rows(entries: list[dict]) -> list[Stat]:
    """Damage or healing table of one player. Pets keep their own name instead of the summon spell."""
    rows = []
    for e in entries:
        if not e.get("total"):
            continue  # e.g. a "Melee" row with nothing in it
        pet = any(src.get("type") == "Pet" for src in e.get("sources") or [])
        name = e.get("name") or str(e.get("guid"))
        rows.append(Stat(
            f"{name} (Pet)" if pet else name,
            None if pet else e.get("guid"),
            float(e.get("total", 0)),
            _hits(e),
            int(e.get("critHitCount", 0) or 0) + int(e.get("critTickCount", 0) or 0),
            float(e.get("overheal", 0) or 0),
        ))
    return _rows(rows)


def _debuffs(rf: RaidFight, end: float) -> dict[int, list[Stat]]:
    """Debuffs per player from apply/remove events: applications and uptime."""
    friends = {p.id for p in rf.players}
    rows: dict[tuple[int, int], Stat] = {}
    since: dict[tuple[int, int], float] = {}
    for e in sorted(rf.debuffs, key=lambda x: x.get("timestamp", 0)):
        key = (e.get("targetID"), e.get("abilityGameID"))
        st = rows.get(key)
        if st is None:
            gid = key[1]
            st = rows[key] = Stat(rf.ability_names.get(gid, str(gid)), gid, friendly=e.get("sourceID") in friends)
        if e.get("type") == "applydebuff":
            st.count += 1
            since.setdefault(key, e.get("timestamp", rf.fight.start))
        else:  # removed – without an apply it was already up at the pull (e.g. Exhaustion from a lust before)
            st.uptime += (e.get("timestamp", end) - since.pop(key, rf.fight.start)) / 1000
    for key, t in since.items():
        rows[key].uptime += (end - t) / 1000
    out: dict[int, list[Stat]] = defaultdict(list)
    for (pid, _), st in rows.items():
        out[pid].append(st)
    return out


def player_stats(rf: RaidFight) -> dict[str, PlayerStats]:
    end = rf.until or rf.fight.end
    debuffs = _debuffs(rf, end)
    out: dict[str, PlayerStats] = {}
    for p in rf.players:
        done, heal = rf.damage_done.get(p.id, {}), rf.healing.get(p.id, {})
        out[p.name] = PlayerStats(
            seconds=max(1.0, (end - rf.fight.start) / 1000),
            damage_total=float(done.get("total", 0)),
            healing_total=float(heal.get("total", 0)),
            overheal_total=float(heal.get("overheal", 0) or 0),
            taken=_rows(
                Stat(e.get("name") or str(e.get("guid")), e.get("guid"), float(e.get("total", 0)), _hits(e),
                     friendly=_enemy_amount(e) <= 0)
                for e in rf.damage_taken.get(p.id, [])
            ),
            buffs=_rows(
                Stat(b.get("name") or str(b.get("guid")), b.get("guid"), count=int(b.get("totalUses", 0) or 0),
                     uptime=float(b.get("totalUptime", 0)) / 1000)
                for b in rf.buffs.get(p.id, [])
            ),
            debuffs=_rows(debuffs.get(p.id, [])),
            casts=_rows(
                Stat(c.get("name") or str(c.get("guid")), c.get("guid"), count=int(c.get("total", 0)))
                for c in rf.casts.get(p.id, [])
            ),
            kicks=_rows(
                Stat(rf.ability_names.get(g, str(g)), g, count=1)
                for e in rf.interrupts
                if e.get("sourceID") == p.id
                for g in [e.get("extraAbilityGameID")]
            ),
            dispels=_rows(
                Stat(a.get("name") or str(a.get("guid")), a.get("guid"), count=int(d.get("total", 0)))
                for a in rf.dispels
                for d in a.get("details") or []
                if d.get("id") == p.id
            ),
        )
    return out


def merge_stats(items: list[PlayerStats]) -> PlayerStats:
    """Several pulls as one: everything adds up, so rates and uptimes become averages over the combined time."""
    def merged(attr: str) -> list[Stat] | None:
        parts = [getattr(x, attr) for x in items]
        return None if any(p is None for p in parts) else _rows(st for p in parts for st in p)

    return PlayerStats(
        seconds=sum(x.seconds for x in items),
        damage_total=sum(x.damage_total for x in items),
        healing_total=sum(x.healing_total for x in items),
        overheal_total=sum(x.overheal_total for x in items),
        damage=merged("damage"),
        healing=merged("healing"),
        pulls=sum(x.pulls for x in items),
        **{attr: merged(attr) for attr in STAT_LISTS},
    )


# ================================================================ entry


def analyze_fight(rf: RaidFight, s: RaidSettings | None = None) -> FightReport:
    s = s or RaidSettings()
    deaths = classify_deaths(rf.fight, rf.players, rf.deaths, s)
    fails = _death_fails(rf, deaths) + _avoidable(rf, s) + _consumables(rf, s)
    fails.sort(key=lambda f: (-f.severity, -f.impact))
    kicks, through, kicked_ids = _interrupts(rf)
    spells = {d.killing_blow: d.spell_id for d in deaths if d.spell_id} | kicked_ids
    return FightReport(rf.fight, rf.players, deaths, fails, kicks, through, spells,
                       until=rf.until or rf.fight.end, stats=player_stats(rf))


# ============================================================ aggregation


@dataclass
class PlayerSummary:
    player: Player
    pulls: int = 0
    first_deaths: int = 0
    kicks: int = 0
    dispels: int = 0
    damage: float = 0.0
    healing: float = 0.0
    seconds: float = 0.0
    fails: list[Fail] = field(default_factory=list)  # in fight order

    def count(self, category: str) -> int:
        return sum(1 for f in self.fails if f.category == category)

    @property
    def dps(self) -> float:
        return self.damage / self.seconds if self.seconds else 0.0

    @property
    def hps(self) -> float:
        return self.healing / self.seconds if self.seconds else 0.0

    @property
    def score(self) -> float:
        """Only for ordering: deaths and mechanics weigh more than missing consumables."""
        return 2 * self.first_deaths + sum(0.5 if f.category == PREP else f.severity for f in self.fails)


def summarize(reports: list[FightReport], only: set[str] | None = None) -> list[PlayerSummary]:
    """Per player across all pulls, worst first. `only`: lower-case names to keep."""
    by_name: dict[str, PlayerSummary] = {}
    for r in reports:
        for p in r.players:
            if only and p.name.lower() not in only:
                continue
            ps = by_name.setdefault(p.name, PlayerSummary(p))
            ps.pulls += 1
            ps.kicks += r.kicks.get(p.name, 0)
            if st := r.stats.get(p.name):
                ps.damage += st.damage_total
                ps.healing += st.healing_total
                ps.seconds += st.seconds
                ps.dispels += sum(d.count for d in st.dispels)
        if r.first_death and r.first_death.player.name in by_name:
            by_name[r.first_death.player.name].first_deaths += 1
        for f in r.fails:
            if f.player.name in by_name:
                by_name[f.player.name].fails.append(f)
    return sorted(by_name.values(), key=lambda ps: (-ps.score, ps.player.name.lower()))


def top_causes(
    reports: list[FightReport], only: set[str] | None = None, n: int = 10, severities: set[int] | None = None
) -> list[tuple[str, str, Counter]]:
    """(category, killing blow / ability, player -> count), most frequent first."""
    groups: dict[tuple[str, str], Counter] = defaultdict(Counter)
    for r in reports:
        for f in r.fails:
            if (
                f.category in (DEATH, AVOIDABLE)
                and (not only or f.player.name.lower() in only)
                and (severities is None or f.severity in severities)
            ):
                groups[(f.category, f.subject)][f.player.name] += 1
    ranked = sorted(groups.items(), key=lambda kv: -sum(kv[1].values()))
    return [(cat, subject, who) for (cat, subject), who in ranked[:n]]
