"""Data access layer: turns GraphQL responses into plain dataclasses.

Everything the analyzers need is collected into a `PlayerFight` so the
analysis code stays pure and can be unit-tested with fixtures.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .api import WCLClient, WCLError

# --------------------------------------------------------------------- models


@dataclass
class Fight:
    id: int
    name: str
    encounter_id: int
    kill: bool
    difficulty: int | None
    start: float
    end: float
    boss_pct: float | None = None

    @property
    def duration_s(self) -> float:
        return max(1.0, (self.end - self.start) / 1000.0)


@dataclass
class Player:
    id: int
    name: str
    server: str | None = None
    class_name: str | None = None  # e.g. "DeathKnight"
    spec: str | None = None  # e.g. "Frost"
    role: str | None = None  # "dps" | "healer" | "tank"


@dataclass
class PlayerFight:
    """All data for one player in one fight."""

    player: Player
    fight: Fight
    report_code: str
    casts: list[dict] = field(default_factory=list)  # table entries: name, guid, total(=count)
    damage_taken: list[dict] = field(default_factory=list)  # name, guid, total(=amount)
    buffs: list[dict] = field(default_factory=list)  # name, guid, totalUptime, totalUses
    buffs_total_time: float = 0.0
    self_buff_ids: set[int] | None = None  # auras the player applied to themselves; None = unknown
    damage_done_total: float = 0.0
    healing_done_total: float = 0.0
    deaths: list[dict] = field(default_factory=list)
    cast_events: list[dict] = field(default_factory=list)
    interrupts: int = 0
    combatant_info: dict | None = None  # stats + gear snapshot at pull
    rank_percent: float | None = None
    amount_per_s: float | None = None

    @property
    def duration_s(self) -> float:
        return self.fight.duration_s


@dataclass
class RaidFight:
    """All players of one fight – raid mode compares them against each other."""

    fight: Fight
    report_code: str
    players: list[Player]
    damage_taken: dict[int, list[dict]] = field(default_factory=dict)  # player id -> table entries
    buffs: dict[int, list[dict]] = field(default_factory=dict)
    casts: dict[int, list[dict]] = field(default_factory=dict)
    deaths: list[dict] = field(default_factory=list)  # every death of the fight
    interrupts: list[dict] = field(default_factory=list)  # friendly interrupt events
    enemy_casts: list[dict] = field(default_factory=list)  # enemy cast/begincast events of kicked spells
    ability_names: dict[int, str] = field(default_factory=dict)
    until: float | None = None  # ms where the tables stop – a lost wipe is cut at the collapse
    damage_done: dict[int, dict] = field(default_factory=dict)  # player id -> DamageDone entry (total, pets)
    healing: dict[int, dict] = field(default_factory=dict)  # player id -> Healing entry (total, overheal)
    dispels: list[dict] = field(default_factory=list)  # dispelled auras, each with who removed how many
    debuffs: list[dict] = field(default_factory=list)  # applydebuff/removedebuff events on friendlies


# ----------------------------------------------------------------- utilities

REPORT_RE = re.compile(r"reports/(?:a:)?([A-Za-z0-9]{16})")


def parse_report_ref(ref: str) -> tuple[str, int | None, int | None]:
    """Accepts a bare code or a full report URL. Returns (code, fight, source)."""
    ref = ref.strip()
    if re.fullmatch(r"[A-Za-z0-9]{16}", ref):
        return ref, None, None
    m = REPORT_RE.search(ref)
    if not m:
        raise ValueError(f"Could not find a report code in '{ref}'")
    qs = parse_qs(urlparse(ref).query)
    fight = qs.get("fight", [None])[0]
    source = qs.get("source", [None])[0]
    fight_id = int(fight) if fight and fight.isdigit() else None
    source_id = int(source) if source and source.isdigit() else None
    return m.group(1), fight_id, source_id


def _table_data(raw: Any) -> dict:
    """WCL table JSON is usually {"data": {...}}; be tolerant of variations."""
    if isinstance(raw, dict) and "data" in raw and isinstance(raw["data"], dict):
        return raw["data"]
    return raw or {}


def _entries(raw: Any) -> list[dict]:
    d = _table_data(raw)
    return list(d.get("entries") or [])


def _nested_entries(raw: Any) -> list[dict]:
    """Dispels/Interrupts tables nest their entries one level deeper: entries[{entries: [...]}]."""
    return [e for grp in _entries(raw) for e in (grp.get("entries", [grp]) if isinstance(grp, dict) else [])]


# ------------------------------------------------------------------- queries

Q_REPORT = """
query($code: String!) {
  reportData {
    report(code: $code) {
      code
      title
      zone { name }
      fights(killType: Encounters) {
        id name encounterID kill difficulty startTime endTime fightPercentage
      }
      masterData { actors(type: "Player") { id name server } }
    }
  }
}"""

Q_DETAILS = """
query($code: String!, $fid: [Int]!) {
  reportData {
    report(code: $code) {
      playerDetails(fightIDs: $fid)
      rDps: rankings(fightIDs: $fid, playerMetric: dps)
      rHps: rankings(fightIDs: $fid, playerMetric: hps)
    }
  }
}"""

Q_PLAYER = """
query($code: String!, $fid: [Int]!, $sid: Int!) {
  reportData {
    report(code: $code) {
      casts:   table(dataType: Casts,       fightIDs: $fid, sourceID: $sid)
      taken:   table(dataType: DamageTaken, fightIDs: $fid, sourceID: $sid)
      buffs:   table(dataType: Buffs,       fightIDs: $fid, sourceID: $sid)
      done:    table(dataType: DamageDone,  fightIDs: $fid, sourceID: $sid)
      healing: table(dataType: Healing,     fightIDs: $fid, sourceID: $sid)
      deaths:  table(dataType: Deaths,      fightIDs: $fid)
    }
  }
}"""

Q_EVENTS = """
query($code: String!, $fid: [Int]!, $sid: Int!, $st: Float!, $et: Float!, $dt: EventDataType!) {
  reportData {
    report(code: $code) {
      events(dataType: $dt, fightIDs: $fid, sourceID: $sid,
             startTime: $st, endTime: $et, limit: 10000) {
        data
        nextPageTimestamp
      }
    }
  }
}"""

Q_EVENTS_ALL = """
query($code: String!, $fid: [Int]!, $host: HostilityType!, $st: Float!, $et: Float!, $dt: EventDataType!) {
  reportData {
    report(code: $code) {
      events(dataType: $dt, fightIDs: $fid, hostilityType: $host,
             startTime: $st, endTime: $et, limit: 10000) {
        data
        nextPageTimestamp
      }
    }
  }
}"""

Q_DEATHS = """
query($code: String!, $fid: [Int]!) {
  reportData {
    report(code: $code) {
      deaths: table(dataType: Deaths, fightIDs: $fid)
    }
  }
}"""

# filled with one aliased table per player and data type, e.g. t12: table(... sourceID: 12)
Q_RAID_TABLES = """
query($code: String!, $fid: [Int]!, $st: Float!, $et: Float!) {
  reportData {
    report(code: $code) {
%s
    }
  }
}"""

# the whole raid in one go: totals per player (the ability lists in here stop after five)
Q_RAID_TOTALS = """
query($code: String!, $fid: [Int]!, $st: Float!, $et: Float!) {
  reportData {
    report(code: $code) {
      done:    table(dataType: DamageDone, fightIDs: $fid, startTime: $st, endTime: $et)
      healing: table(dataType: Healing,    fightIDs: $fid, startTime: $st, endTime: $et)
      dispels: table(dataType: Dispels,    fightIDs: $fid, startTime: $st, endTime: $et)
    }
  }
}"""

# one player's damage and healing per ability, filled with aliased tables per pull, e.g. d27: table(...)
Q_BREAKDOWN = """
query($code: String!) {
  reportData {
    report(code: $code) {
%s
    }
  }
}"""

Q_ABILITIES = """
query($code: String!) {
  reportData {
    report(code: $code) {
      masterData { abilities { gameID name } }
    }
  }
}"""

Q_TOP = """
query($eid: Int!, $cls: String!, $spec: String!, $diff: Int!, $metric: CharacterRankingMetricType!) {
  worldData {
    encounter(id: $eid) {
      name
      characterRankings(className: $cls, specName: $spec, difficulty: $diff, metric: $metric)
    }
  }
}"""

Q_FIGHT_ACTORS = """
query($code: String!, $fid: [Int]!) {
  reportData {
    report(code: $code) {
      fights(fightIDs: $fid) {
        id name encounterID kill difficulty startTime endTime fightPercentage
      }
      masterData { actors(type: "Player") { id name server } }
    }
  }
}"""


# ------------------------------------------------------------------- fetcher


class Fetcher:
    def __init__(self, client: WCLClient, log: Callable[[str], None] = lambda _m: None):
        self.c = client
        self.log = log

    # -- report level --------------------------------------------------------
    def report(self, code: str) -> dict:
        rep = self.c.query(Q_REPORT, {"code": code})["reportData"]["report"]
        if rep is None:
            raise WCLError(f"Report {code} not found or private.")
        return rep

    @staticmethod
    def to_fight(f: dict) -> Fight:
        return Fight(
            id=f["id"],
            name=f["name"],
            encounter_id=f.get("encounterID") or 0,
            kill=bool(f.get("kill")),
            difficulty=f.get("difficulty"),
            start=f["startTime"],
            end=f["endTime"],
            boss_pct=f.get("fightPercentage"),
        )

    def player_details(self, code: str, fight_id: int) -> tuple[dict, dict, dict]:
        r = self.c.query(Q_DETAILS, {"code": code, "fid": [fight_id]})["reportData"]["report"]
        return _table_data(r["playerDetails"]), r.get("rDps") or {}, r.get("rHps") or {}

    @staticmethod
    def players(details: dict) -> list[Player]:
        """Every player of a fight, from playerDetails."""
        pd = details.get("playerDetails", details)
        out: list[Player] = []
        for role_key, role in (("tanks", "tank"), ("healers", "healer"), ("dps", "dps")):
            for p in pd.get(role_key, []) or []:
                specs = p.get("specs") or []
                spec = specs[0].get("spec") if specs and isinstance(specs[0], dict) else None
                out.append(
                    Player(
                        id=p["id"],
                        name=p["name"],
                        server=p.get("server"),
                        class_name=p.get("type"),
                        spec=spec,
                        role=role,
                    )
                )
        return out

    @staticmethod
    def resolve_player(details: dict, name: str | None, source_id: int | None) -> Player:
        candidates = Fetcher.players(details)
        if source_id is not None:
            for c in candidates:
                if c.id == source_id:
                    return c
        if name:
            for c in candidates:
                if c.name.lower() == name.lower():
                    return c
        names = ", ".join(sorted(c.name for c in candidates))
        raise WCLError(f"Player '{name or source_id}' not found in this fight. Players: {names}")

    @staticmethod
    def rank_for(rankings: dict, player: Player) -> tuple[float | None, float | None]:
        """Returns (rankPercent, amount) for the player from report.rankings JSON."""
        for entry in (rankings or {}).get("data", []) or []:
            roles = entry.get("roles", {})
            for role in roles.values():
                for ch in role.get("characters", []) or []:
                    if ch.get("name", "").lower() == player.name.lower():
                        return ch.get("rankPercent"), ch.get("amount")
        return None, None

    # -- player level --------------------------------------------------------
    def _paged_events(self, gql: str, variables: dict, fight: Fight, until: float | None = None) -> list[dict]:
        out: list[dict] = []
        st = fight.start
        while st is not None:
            ev = self.c.query(gql, {**variables, "st": st, "et": until or fight.end})["reportData"]["report"]["events"]
            out.extend(ev.get("data") or [])
            st = ev.get("nextPageTimestamp")
        return out

    def _events(self, code: str, fight: Fight, sid: int, data_type: str) -> list[dict]:
        return self._paged_events(Q_EVENTS, {"code": code, "fid": [fight.id], "sid": sid, "dt": data_type}, fight)

    def _side_events(
        self, code: str, fight: Fight, data_type: str, hostility: str, until: float | None = None
    ) -> list[dict]:
        """Events of every actor on one side ("Friendlies" / "Enemies"), no source filter."""
        return self._paged_events(
            Q_EVENTS_ALL, {"code": code, "fid": [fight.id], "dt": data_type, "host": hostility}, fight, until
        )

    def player_fight(self, code: str, fight: Fight, player: Player) -> PlayerFight:
        r = self.c.query(Q_PLAYER, {"code": code, "fid": [fight.id], "sid": player.id})["reportData"]["report"]
        buffs = _table_data(r["buffs"])
        deaths = [
            d for d in _entries(r["deaths"]) if d.get("id") == player.id or d.get("name") == player.name
        ]
        pf = PlayerFight(
            player=player,
            fight=fight,
            report_code=code,
            casts=_entries(r["casts"]),
            damage_taken=_entries(r["taken"]),
            buffs=list(buffs.get("auras") or []),
            buffs_total_time=float(buffs.get("totalTime") or fight.end - fight.start),
            damage_done_total=sum(e.get("total", 0) for e in _entries(r["done"])),
            healing_done_total=sum(e.get("total", 0) for e in _entries(r["healing"])),
            deaths=deaths,
        )
        pf.cast_events = [
            e for e in self._events(code, fight, player.id, "Casts") if e.get("type") in ("cast", "begincast")
        ]
        pf.interrupts = sum(
            1 for e in self._events(code, fight, player.id, "Interrupts") if e.get("type") == "interrupt"
        )
        ci = [e for e in self._events(code, fight, player.id, "CombatantInfo") if e.get("type") == "combatantinfo"]
        pf.combatant_info = ci[0] if ci else None
        # The Buffs table lists every aura *on* the player, incl. ones from other classes
        # (e.g. a shaman's Earthliving Weapon). Remember which ones the player applied themselves.
        self_ids = {
            e["abilityGameID"]
            for e in self._events(code, fight, player.id, "Buffs")
            if e.get("sourceID") == player.id and e.get("targetID", player.id) == player.id and "abilityGameID" in e
        }
        self_ids |= {
            a["ability"]
            for a in (pf.combatant_info or {}).get("auras") or []
            if a.get("source") == player.id and "ability" in a
        }
        pf.self_buff_ids = self_ids
        return pf

    # -- raid level ----------------------------------------------------------
    def ability_names(self, code: str) -> dict[int, str]:
        r = self.c.query(Q_ABILITIES, {"code": code})["reportData"]["report"]
        abilities = (r.get("masterData") or {}).get("abilities") or []
        return {a["gameID"]: a.get("name") or str(a["gameID"]) for a in abilities if "gameID" in a}

    def deaths(self, code: str, fight: Fight) -> list[dict]:
        return _entries(self.c.query(Q_DEATHS, {"code": code, "fid": [fight.id]})["reportData"]["report"]["deaths"])

    def raid_fight(
        self,
        code: str,
        fight: Fight,
        players: list[Player],
        deaths: list[dict],
        until: float | None = None,
        ability_names: dict[int, str] | None = None,
        chunk: int = 5,
    ) -> RaidFight:
        """Damage taken, buffs, casts, debuffs and totals of every player; everything except
        interrupts stops at `until` (ms) if given."""
        rf = RaidFight(fight=fight, report_code=code, players=players, deaths=deaths,
                       ability_names=ability_names or {}, until=until)
        v = {"code": code, "fid": [fight.id], "st": fight.start, "et": until or fight.end}
        for i in range(0, len(players), chunk):  # a few players per request keeps single queries small
            group = players[i : i + chunk]
            fields = "\n".join(
                f"      {alias}{p.id}: table(dataType: {dt}, fightIDs: $fid, sourceID: {p.id}, "
                "startTime: $st, endTime: $et)"
                for p in group
                for alias, dt in (("t", "DamageTaken"), ("b", "Buffs"), ("c", "Casts"))
            )
            r = self.c.query(Q_RAID_TABLES % fields, v)["reportData"]["report"]
            for p in group:
                rf.damage_taken[p.id] = _entries(r[f"t{p.id}"])
                rf.buffs[p.id] = list(_table_data(r[f"b{p.id}"]).get("auras") or [])
                rf.casts[p.id] = _entries(r[f"c{p.id}"])
        totals = self.c.query(Q_RAID_TOTALS, v)["reportData"]["report"]
        rf.damage_done = {e["id"]: e for e in _entries(totals["done"]) if "id" in e}
        rf.healing = {e["id"]: e for e in _entries(totals["healing"]) if "id" in e}
        rf.dispels = _nested_entries(totals["dispels"])
        rf.debuffs = [
            e
            for e in self._side_events(code, fight, "Debuffs", "Friendlies", until)
            if e.get("type") in ("applydebuff", "removedebuff")
        ]
        rf.interrupts = [
            e for e in self._side_events(code, fight, "Interrupts", "Friendlies") if e.get("type") == "interrupt"
        ]
        if rf.interrupts:  # enemy casts are only needed to see which kickable casts went through
            kicked = {e.get("extraAbilityGameID") for e in rf.interrupts}
            rf.enemy_casts = [
                e
                for e in self._side_events(code, fight, "Casts", "Enemies")
                if e.get("abilityGameID") in kicked and e.get("type") in ("cast", "begincast")
            ]
        return rf

    def breakdown(
        self, code: str, pid: int, windows: list[tuple[int, float, float]], chunk: int = 8
    ) -> dict[int, tuple[list[dict], list[dict]]]:
        """Damage and healing table entries per ability of one player.
        windows: (fight ID, start ms, end ms) per pull -> {fight ID: (damage, healing)}."""
        out: dict[int, tuple[list[dict], list[dict]]] = {}
        for i in range(0, len(windows), chunk):  # several pulls per request
            group = windows[i : i + chunk]
            fields = "\n".join(
                f"      {alias}{fid}: table(dataType: {dt}, fightIDs: [{fid}], sourceID: {pid}, "
                f"startTime: {st:.0f}, endTime: {et:.0f})"
                for fid, st, et in group
                for alias, dt in (("d", "DamageDone"), ("h", "Healing"))
            )
            r = self.c.query(Q_BREAKDOWN % fields, {"code": code})["reportData"]["report"]
            for fid, _, _ in group:
                out[fid] = (_entries(r[f"d{fid}"]), _entries(r[f"h{fid}"]))
        return out

    # -- benchmark -----------------------------------------------------------
    def top_players(self, me: PlayerFight, n: int = 5) -> list[PlayerFight]:
        p, f = me.player, me.fight
        if not (p.class_name and p.spec and f.encounter_id and f.difficulty):
            self.log("Spec/encounter unknown, skipping top-player comparison.")
            return []
        metric = "hps" if p.role == "healer" else "dps"
        data = self.c.query(
            Q_TOP,
            {"eid": f.encounter_id, "cls": p.class_name, "spec": p.spec, "diff": f.difficulty, "metric": metric},
            cache=False,  # rankings change over time
        )
        enc = (data.get("worldData") or {}).get("encounter") or {}
        rankings = (enc.get("characterRankings") or {}).get("rankings") or []
        result: list[PlayerFight] = []
        for rk in rankings:
            if len(result) >= n:
                break
            rep = rk.get("report") or {}
            code, fid = rep.get("code"), rep.get("fightID")
            if not code or fid is None:
                continue
            try:
                r = self.c.query(Q_FIGHT_ACTORS, {"code": code, "fid": [fid]})["reportData"]["report"]
                if not r or not r.get("fights"):
                    continue
                fight = self.to_fight(r["fights"][0])
                actor = next(
                    (a for a in r["masterData"]["actors"] if a["name"].lower() == rk["name"].lower()), None
                )
                if not actor:
                    continue
                tp = Player(
                    id=actor["id"], name=rk["name"], server=actor.get("server"),
                    class_name=p.class_name, spec=p.spec, role=p.role,
                )
                self.log(f"  benchmark: {tp.name} ({rk.get('amount', 0):,.0f} {metric})")
                pf = self.player_fight(code, fight, tp)
                pf.amount_per_s = rk.get("amount")
                pf.rank_percent = 100.0
                result.append(pf)
            except WCLError as e:  # private/deleted reports happen
                self.log(f"  skipped {rk.get('name')}: {e}")
        return result
