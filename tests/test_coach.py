"""Offline tests: analyzers on synthetic data + an end-to-end run with a fake API."""
from __future__ import annotations

import pytest

from wcl_coach import analysis as A
from wcl_coach import cli
from wcl_coach.fetch import Fetcher, Fight, Player, PlayerFight, parse_report_ref

START, DUR = 1_000_000, 300_000  # 5-minute fight


def make_pf(name="Me", casts=None, taken=None, buffs=None, deaths=None, events=None, kicks=0, amount=None):
    f = Fight(id=7, name="Boss", encounter_id=3009, kill=True, difficulty=5, start=START, end=START + DUR)
    p = Player(id=1, name=name, class_name="Mage", spec="Fire", role="dps")
    return PlayerFight(
        player=p, fight=f, report_code="AbCdEf1234567890",
        casts=casts or [], damage_taken=taken or [], buffs=buffs or [],
        buffs_total_time=DUR, deaths=deaths or [], cast_events=events or [],
        interrupts=kicks, amount_per_s=amount, damage_done_total=1e6,
    )


def evenly(every_s: float, until_s: float = DUR / 1000, offset_s: float = 0.5) -> list[dict]:
    out, t = [], offset_s
    while t < until_s:
        out.append({"timestamp": START + t * 1000, "type": "cast", "abilityGameID": 1})
        t += every_s
    return out


def top(name, **kw):
    kw.setdefault("events", evenly(1.5))
    kw.setdefault("amount", 1_000_000)
    return make_pf(name=name, **kw)


# ------------------------------------------------------------------ parsing


def test_parse_report_ref():
    assert parse_report_ref("AbCdEf1234567890") == ("AbCdEf1234567890", None, None)
    url = "https://www.warcraftlogs.com/reports/AbCdEf1234567890?fight=12&type=damage-done&source=34"
    assert parse_report_ref(url) == ("AbCdEf1234567890", 12, 34)
    with pytest.raises(ValueError):
        parse_report_ref("nope")


# ---------------------------------------------------------------- activity


def test_activity_detects_gap():
    events = evenly(1.5, until_s=100) + [
        {"timestamp": START + t * 1000, "type": "cast"} for t in range(130, 300, 2)
    ]
    st = A.activity_stats(make_pf(events=events), 2.5)
    assert st["gaps"][0][1] > 29  # ~30s hole at 1:40
    assert 85 < st["active_pct"] < 92


def test_activity_vs_tops_flags_high():
    me = make_pf(events=evenly(3.5))  # lots of downtime
    tops = [top(f"T{i}") for i in range(3)]
    f = A.analyze_activity(me, tops, A.Settings())
    assert f[0].severity == A.HIGH and f[0].category == "Aktivität"


def test_activity_ignores_time_after_death():
    death = [{"timestamp": START + 150_000, "killingBlow": {"name": "Big Hit"}}]
    me = make_pf(events=evenly(1.5, until_s=150), deaths=death)
    assert A.activity_stats(me, 2.5)["active_pct"] > 99


# ------------------------------------------------------------------- casts


def test_casts_missing_and_under():
    core = {"name": "Fireball", "guid": 133, "total": 100}
    cd = {"name": "Combustion", "guid": 190319, "total": 3}
    pyro = {"name": "Pyroblast", "guid": 11366, "total": 40}
    tops = [top(f"T{i}", casts=[core, cd, pyro]) for i in range(3)]
    me = make_pf(casts=[{**core, "total": 98}, {**cd, "total": 1}])
    fs = A.analyze_casts(me, tops, A.Settings())
    titles = " | ".join(f.title for f in fs)
    assert "'Pyroblast' nie benutzt" in titles
    assert "'Combustion': 1×" in titles
    assert "Fireball" not in titles
    assert fs and all(f.severity >= A.LOW for f in fs)


def test_casts_need_min_tops():
    assert A.analyze_casts(make_pf(), [top("T1")], A.Settings()) == []


# ----------------------------------------------------------- damage taken


def test_avoidable_damage():
    raid = {"name": "Raid Pulse", "guid": 1, "total": 500_000}
    puddle = {"name": "Fire Puddle", "guid": 2, "total": 200_000}
    beam = {"name": "Beam", "guid": 3, "total": 50_000}
    tops = [top(f"T{i}", taken=[raid, beam]) for i in range(3)]
    me = make_pf(taken=[raid, puddle, {**beam, "total": 150_000}])
    fs = A.analyze_damage_taken(me, tops, A.Settings())
    by_title = {f.title.split("'")[1]: f for f in fs}
    assert by_title["Fire Puddle"].severity == A.HIGH
    assert by_title["Beam"].severity == A.MEDIUM  # 3x as much
    assert "Raid Pulse" not in by_title


# ------------------------------------------------------------------- buffs


def test_buffs_missing_flask_and_low_uptime():
    flask = {"name": "Flask of Power", "guid": 9, "totalUptime": DUR}
    proc = {"name": "Heating Up", "guid": 8, "totalUptime": DUR * 0.6}
    tops = [top(f"T{i}", buffs=[flask, proc]) for i in range(3)]
    me = make_pf(buffs=[{**proc, "totalUptime": DUR * 0.2}])
    fs = A.analyze_buffs(me, tops, A.Settings())
    cats = {f.category for f in fs}
    assert cats == {"Vorbereitung", "Buff-Uptime"}


def test_buffs_from_other_classes_are_not_blamed_on_player():
    hot = {"name": "Earthliving Weapon", "guid": 7, "totalUptime": DUR * 0.6}
    raid = {"name": "Skyfury", "guid": 6, "totalUptime": DUR}
    own = {"name": "Heating Up", "guid": 8, "totalUptime": DUR * 0.6}
    tops = [top(f"T{i}", buffs=[hot, raid, own]) for i in range(3)]
    for t in tops:
        t.self_buff_ids = {8}
    me = make_pf(buffs=[{**own, "totalUptime": DUR * 0.2}])
    me.self_buff_ids = {8}
    fs = A.analyze_buffs(me, tops, A.Settings())
    titles = " ".join(f.title for f in fs)
    assert "Earthliving" not in titles
    assert {f.category for f in fs} == {"Buff-Uptime", "Raid-Buff"}


# ----------------------------------------------------------- deaths/utility


def test_death_without_healthstone():
    me = make_pf(
        deaths=[{"timestamp": START + 60_000, "killingBlow": {"name": "Smash"}}],
        casts=[{"name": "Healthstone", "guid": 6262, "total": 1}],
        events=[{"timestamp": START + 90_000, "type": "cast", "abilityGameID": 6262}],  # used after death? no
    )
    f = A.analyze_deaths(me, [], A.Settings())[0]
    assert f.severity == A.HIGH and "Smash" in f.title and "Healthstone" in f.tip


def test_no_combat_potion():
    me = make_pf(casts=[{"name": "Algari Healing Potion", "guid": 1, "total": 1}])
    fs = A.analyze_utility(me, [], A.Settings())
    assert any("Kampftrank" in f.title for f in fs)
    me2 = make_pf(casts=[{"name": "Tempered Potion", "guid": 2, "total": 1}])
    assert not any("Kampftrank" in f.title for f in A.analyze_utility(me2, [], A.Settings()))


def test_run_all_sorted():
    me = make_pf(events=evenly(4), deaths=[{"timestamp": START + 30_000, "killingBlow": {"name": "X"}}])
    fs = A.run_all(me, [top(f"T{i}") for i in range(3)])
    sev = [f.severity for f in fs]
    assert sev == sorted(sev, reverse=True)


# -------------------------------------------------------------------- gear


def make_ci(crit=500, haste=600, mastery=700, vers=200, enchant=True, oil=True, gems=2, trinket=111, nested=False):
    gear = [{"id": 1000 + i, "slot": i, "itemLevel": 1 if i == 3 else 640} for i in range(17)]
    for sl in (4, 6, 7, 8, 10, 11, 14, 15):
        gear[sl]["permanentEnchant"] = 7000 + sl if enchant else 0
    gear[15]["temporaryEnchant"] = 6000 if oil else 0
    gear[0]["gems"] = [{"id": 5000 + g} for g in range(gems)]
    gear[12]["id"] = trinket
    stats = {"critMelee": crit, "critSpell": crit - 10, "hasteSpell": haste, "mastery": mastery,
             "versatilityDamageDone": vers, "versatilityDamageReduction": vers / 2, "intellect": 50_000}
    ci = {"type": "combatantinfo", "gear": gear}
    if nested:
        ci["stats"] = {k: {"min": v, "max": v} for k, v in stats.items()}
    else:
        ci.update(stats)
    return ci


def test_secondary_ratings_flat_and_nested():
    flat = A.secondary_ratings(make_ci())
    nested = A.secondary_ratings(make_ci(nested=True))
    assert flat == nested == {"Crit": 500, "Haste": 600, "Mastery": 700, "Vers": 200}
    assert A.secondary_ratings({"gear": []}) == {}
    assert round(sum(A.stat_shares(make_ci()).values())) == 100


def test_gear_findings():
    tops = [top(f"T{i}") for i in range(3)]
    for t in tops:
        t.combatant_info = make_ci(gems=3)
    me = make_pf()
    me.combatant_info = make_ci(crit=1200, mastery=200, enchant=False, oil=False, gems=1, trinket=222)
    fs = A.analyze_gear(me, tops, A.Settings())
    titles = " | ".join(f.title for f in fs)
    assert "Enchant fehlt: Brust, Beine" in titles
    assert any(f.severity == A.HIGH and "Enchant" in f.title for f in fs)
    assert "Waffenöl" in titles
    assert "1 Edelsteine" in titles
    assert "item=111" in titles
    assert "Stat-Verteilung weicht" in titles and "Crit +" in titles
    assert "Ø Itemlevel 640.0" in titles  # shirt (ilvl 1) excluded


def test_gear_clean_when_equal():
    tops = [top(f"T{i}") for i in range(3)]
    for t in tops:
        t.combatant_info = make_ci()
    me = make_pf()
    me.combatant_info = make_ci()
    assert all(f.severity == A.INFO for f in A.analyze_gear(me, tops, A.Settings()))


def test_gear_without_combatant_info():
    assert A.analyze_gear(make_pf(), [], A.Settings()) == []


# ------------------------------------------------------ end-to-end (fake API)


class FakeClient:
    """Answers the GraphQL queries the Fetcher sends, using the documented JSON shapes."""

    def __init__(self):
        self.calls = []

    def query(self, gql, variables=None, cache=True):
        v = variables or {}
        self.calls.append(gql.split("(")[0].strip())
        fight = {"id": 7, "name": "Boss", "encounterID": 3009, "kill": True, "difficulty": 5,
                 "startTime": START, "endTime": START + DUR, "fightPercentage": 0}
        actors = [{"id": 1, "name": "Peter", "server": "Antonidas"}, {"id": 2, "name": "Topdog", "server": "X"},
                  {"id": 3, "name": "Topcat", "server": "Y"}]
        if "worldData" in gql:
            return {"worldData": {"encounter": {"name": "Boss", "characterRankings": {"rankings": [
                {"name": "Topdog", "amount": 2_000_000, "report": {"code": "TOPREPORT0000001", "fightID": 7}},
                {"name": "Topcat", "amount": 1_900_000, "report": {"code": "TOPREPORT0000002", "fightID": 7}},
            ]}}}}
        if "playerDetails" in gql:
            return {"reportData": {"report": {
                "playerDetails": {"data": {"playerDetails": {"dps": [
                    {"id": 1, "name": "Peter", "type": "Mage", "specs": [{"spec": "Fire", "count": 1}]}]}}},
                "rDps": {"data": [{"fightID": 7, "roles": {"dps": {"characters": [
                    {"name": "Peter", "rankPercent": 42, "amount": 1_500_000}]}}}]},
                "rHps": {"data": []},
            }}}
        if "events(" in gql:
            sid = v["sid"]
            if v["dt"] == "CombatantInfo":
                ci = make_ci(crit=900 if sid == 1 else 500, mastery=300 if sid == 1 else 700,
                             enchant=sid != 1)
                return {"reportData": {"report": {"events": {"data": [ci], "nextPageTimestamp": None}}}}
            if v["dt"] == "Interrupts":
                return {"reportData": {"report": {"events": {"data": [], "nextPageTimestamp": None}}}}
            step = 4.0 if sid == 1 else 1.5
            evs = evenly(step)
            half = len(evs) // 2
            if v["st"] == START:  # exercise pagination
                return {"reportData": {"report": {"events": {
                    "data": evs[:half], "nextPageTimestamp": evs[half]["timestamp"]}}}}
            return {"reportData": {"report": {"events": {"data": evs[half:], "nextPageTimestamp": None}}}}
        if "casts:" in gql:
            mine = v["sid"] == 1
            return {"reportData": {"report": {
                "casts": {"data": {"entries": [{"name": "Fireball", "guid": 133, "total": 60 if mine else 150},
                                               {"name": "Combustion", "guid": 190319, "total": 1 if mine else 3}]}},
                "taken": {"data": {"entries": [{"name": "Fire Puddle", "guid": 2, "total": 300_000}] if mine else []}},
                "buffs": {"data": {"totalTime": DUR, "auras": []}},
                "done": {"data": {"entries": [{"total": 4.5e8}]}},
                "healing": {"data": {"entries": []}},
                "deaths": {"data": {"entries": [{"id": 1, "name": "Peter", "timestamp": START + 200_000,
                                                 "killingBlow": {"name": "Smash"}}] if mine else []}},
            }}}
        if "fights(fightIDs" in gql:
            return {"reportData": {"report": {"fights": [fight], "masterData": {"actors": actors}}}}
        if "fights(killType" in gql:
            return {"reportData": {"report": {"code": v["code"], "title": "Raid", "zone": {"name": "Z"},
                                              "fights": [fight], "masterData": {"actors": actors}}}}
        raise AssertionError(f"unexpected query: {gql[:80]}")


def test_end_to_end(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(cli, "_client", lambda args: FakeClient())
    out_md = tmp_path / "r.md"
    cli.main(["analyze", "https://www.warcraftlogs.com/reports/AbCdEf1234567890?fight=7",
              "-p", "Peter", "--md", str(out_md)])
    out = capsys.readouterr().out
    assert "Peter – Fire Mage" in out
    assert "Parse: 42%" in out
    assert "Gestorben bei 3:20" in out
    assert "Fire Puddle" in out
    assert "Combustion" in out
    assert "Aktivität" in out
    md = out_md.read_text()
    assert md.startswith("# Peter") and "Topdog" in md
    assert "Enchant fehlt" in out and "Stat-Verteilung" in out


def test_resolve_player_error_lists_names():
    details = {"playerDetails": {"dps": [{"id": 5, "name": "Alice", "type": "Mage", "specs": []}]}}
    with pytest.raises(Exception, match="Alice"):
        Fetcher.resolve_player(details, "Bob", None)
