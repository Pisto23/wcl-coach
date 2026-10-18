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
    def resolve_player(details: dict, name: str | None, source_id: int | None) -> Player:
        pd = details.get("playerDetails", details)
        candidates: list[Player] = []
        for role_key, role in (("tanks", "tank"), ("healers", "healer"), ("dps", "dps")):
            for p in pd.get(role_key, []) or []:
                specs = p.get("specs") or []
                spec = specs[0].get("spec") if specs and isinstance(specs[0], dict) else None
                candidates.append(
                    Player(
                        id=p["id"],
                        name=p["name"],
                        server=p.get("server"),
                        class_name=p.get("type"),
                        spec=spec,
                        role=role,
                    )
                )
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
    def _events(self, code: str, fight: Fight, sid: int, data_type: str) -> list[dict]:
        out: list[dict] = []
        st = fight.start
        while st is not None:
            ev = self.c.query(
                Q_EVENTS,
                {"code": code, "fid": [fight.id], "sid": sid, "st": st, "et": fight.end, "dt": data_type},
            )["reportData"]["report"]["events"]
            out.extend(ev.get("data") or [])
            st = ev.get("nextPageTimestamp")
        return out

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
        return pf

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
