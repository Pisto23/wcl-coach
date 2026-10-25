"""Offline tests: analyzers on synthetic data + an end-to-end run with a fake API."""
from __future__ import annotations

import re
from collections import Counter

import pytest

from wcl_coach import analysis as A
from wcl_coach import cli
from wcl_coach import raid as R
from wcl_coach.api import WCLError
from wcl_coach.fetch import Fetcher, Fight, Player, PlayerFight, RaidFight, parse_report_ref

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


def test_killing_blow_fallbacks():
    assert A.killing_blow({"killingBlow": {"name": "Smash", "guid": 7}}) == ("Smash", 7)
    recap = {"events": [{"timestamp": 5, "type": "damage", "ability": {"name": "Puddle", "guid": 1}},
                        {"timestamp": 9, "type": "damage", "ability": {"name": "Gash", "guid": 2}},
                        {"timestamp": 12, "type": "heal", "ability": {"name": "Flash Heal", "guid": 3}}],
             "damage": {"abilities": [{"name": "Presence", "guid": 4, "total": 999}]}}
    assert A.killing_blow(recap) == ("Gash", 2)  # latest hit of the death recap
    assert A.killing_blow({"damage": recap["damage"]}) == ("Presence", 4)
    assert A.killing_blow({}) == ("unbekannt", None)


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
        actors = [{"id": 1, "name": "player1", "server": "Antonidas"}, {"id": 2, "name": "Topdog", "server": "X"},
                  {"id": 3, "name": "Topcat", "server": "Y"}]
        if "worldData" in gql:
            return {"worldData": {"encounter": {"name": "Boss", "characterRankings": {"rankings": [
                {"name": "Topdog", "amount": 2_000_000, "report": {"code": "TOPREPORT0000001", "fightID": 7}},
                {"name": "Topcat", "amount": 1_900_000, "report": {"code": "TOPREPORT0000002", "fightID": 7}},
            ]}}}}
        if "playerDetails" in gql:
            return {"reportData": {"report": {
                "playerDetails": {"data": {"playerDetails": {"dps": [
                    {"id": 1, "name": "player1", "type": "Mage", "specs": [{"spec": "Fire", "count": 1}]}]}}},
                "rDps": {"data": [{"fightID": 7, "roles": {"dps": {"characters": [
                    {"name": "player1", "rankPercent": 42, "amount": 1_500_000}]}}}]},
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
                "deaths": {"data": {"entries": [{"id": 1, "name": "player1", "timestamp": START + 200_000,
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
              "-p", "player1", "--md", str(out_md)])
    out = capsys.readouterr().out
    assert "player1 – Fire Mage" in out
    assert "Parse: 42%" in out
    assert "Gestorben bei 3:20" in out
    assert "Fire Puddle" in out
    assert "Combustion" in out
    assert "Aktivität" in out
    md = out_md.read_text()
    assert md.startswith("# player1") and "Topdog" in md
    assert "Enchant fehlt" in out and "Stat-Verteilung" in out


def test_resolve_player_error_lists_names():
    details = {"playerDetails": {"dps": [{"id": 5, "name": "Alice", "type": "Mage", "specs": []}]}}
    with pytest.raises(Exception, match="Alice"):
        Fetcher.resolve_player(details, "Bob", None)


def test_select_fights_ranges_and_boss_names():
    from wcl_coach.pipeline import select_fights as _select_fights
    from wcl_coach.fetch import Fight
    fs = [Fight(id=i, name=n, start=0, end=1, kill=False, encounter_id=1, difficulty=5)
          for i, n in [(5, "Kith'ix"), (23, "Sszorak"), (24, "Sszorak"), (30, "Sszorak")]]
    assert [f.id for f in _select_fights(fs, "23-24", None)] == [23, 24]
    assert [f.id for f in _select_fights(fs, "sszorak", None)] == [23, 24, 30]
    assert [f.id for f in _select_fights(fs, "5,24-30", None)] == [5, 24, 30]


def test_select_fights_kills_or_wipes_of_a_boss():
    from wcl_coach.pipeline import select_fights as sel

    fs = [Fight(id=i, name=n, start=0, end=1, kill=k, encounter_id=1, difficulty=5) for i, n, k in [
        (3, "Twin Fangs", False), (4, "Twin Fangs", True), (5, "Twin Fangs Reborn", False),
        (8, "Sszorak", False), (9, "Sszorak", True)]]

    def ids(s):
        return [f.id for f in sel(fs, s, None)]

    assert ids("twin fangs") == [3, 4]  # the full name only means that boss ...
    assert ids("twin") == [3, 4, 5]  # ... a fragment every boss containing it
    assert ids("Twin Fangs:wipes") == [3] and ids("twin fangs:kills") == [4] and ids("twin:wipe") == [3, 5]
    assert ids("wipes") == [3, 5, 8] and ids("kills") == [4, 9]
    assert ids("3-8:wipes") == [3, 5, 8] and ids("sszorak:kills,twin fangs:wipes") == [3, 9]
    with pytest.raises(WCLError, match="Keine Kämpfe für 'twin fangs reborn:kills'"):
        sel(fs, "twin fangs reborn:kills", None)


# ---------------------------------------------------------------- raid mode


def make_raid(n_tanks=2, n_others=8, kill=True, **kw) -> RaidFight:
    f = Fight(id=7, name="Boss", encounter_id=3009, kill=kill, difficulty=5, start=START, end=START + DUR)
    players = [Player(id=i, name=f"tank{i}", class_name="Warrior", spec="Protection", role="tank")
               for i in range(1, n_tanks + 1)]
    players += [Player(id=i, name=f"player{i}", class_name="Mage", spec="Fire", role="dps")
                for i in range(n_tanks + 1, n_tanks + n_others + 1)]
    rf = RaidFight(fight=f, report_code="AbCdEf1234567890", players=players, **kw)
    for p in players:
        rf.damage_taken.setdefault(p.id, [])
        rf.buffs.setdefault(p.id, [])
        rf.casts.setdefault(p.id, [])
    return rf


def test_raid_avoidable_only_what_most_players_dodge():
    pulse = {"name": "Raid Pulse", "guid": 1, "total": 500_000}
    rf = make_raid()
    for p in rf.players:
        rf.damage_taken[p.id] = [pulse]
    for pid in (1, 2):  # tank-only mechanics
        rf.damage_taken[pid] += [{"name": "Tank Buster", "guid": 5, "total": 900_000}]
    for pid in (3, 4):  # two of eight non-tanks stood in it
        rf.damage_taken[pid] += [{"name": "Fire Puddle", "guid": 2, "total": 200_000, "hitCount": 3}]
    rf.damage_taken[5] += [{"name": "Burning Rush", "guid": 111400, "total": 100_000,  # own spell
                            "sources": [{"name": "player5", "total": 100_000, "type": "Warlock"}]}]
    rf.damage_taken[6] += [{"name": "Melee", "guid": 1, "total": 300_000, "actorType": "NPC"}]  # loose add
    for pid in range(3, 8):  # more than half of the non-tanks -> raid damage
        rf.damage_taken[pid] += [{"name": "Shadow Wave", "guid": 6, "total": 100_000}]

    fails = R.analyze_fight(rf).fails
    assert {(f.player.name, f.subject) for f in fails} == {("player3", "Fire Puddle"), ("player4", "Fire Puddle")}
    assert "3 Treffer" in fails[0].title and "2/8 Spieler" in fails[0].title
    assert R.analyze_fight(rf, R.RaidSettings(ignore=("puddle",))).fails == []


def test_raid_wipe_deaths_and_cutoff():
    deaths = [
        {"id": 4, "name": "player4", "timestamp": START + 60_000, "killingBlow": {"name": "Fire Puddle"}},
        {"id": 5, "name": "player5", "timestamp": START + 90_000, "killingBlow": {"name": "Smash"}},
        {"id": 6, "name": "player6", "timestamp": START + 290_000, "killingBlow": {"name": "Enrage"}},  # tail
        {"id": 99, "name": "Pet", "timestamp": START + 10_000, "killingBlow": {"name": "X"}},
    ]
    rf = make_raid(n_tanks=1, n_others=5, kill=False, deaths=deaths)
    rf.casts[5] = [{"name": "Healthstone", "guid": 6262, "total": 1}]
    rep = R.analyze_fight(rf)
    assert [(d.player.name, d.wipe) for d in rep.deaths] == [("player4", False), ("player5", False), ("player6", True)]
    assert rep.first_death.player.name == "player4"
    titles = {f.player.name: f.title for f in rep.fails if f.category == R.DEATH}
    assert set(titles) == {"player4", "player5"}
    assert "erster Tod" in titles["player4"] and "kein Healthstone" in titles["player4"]
    assert "Healthstone" not in titles["player5"]
    assert R.lost_at(rf.fight, rf.players, deaths, R.RaidSettings()) == START + 290_000
    # on a kill every death counts
    assert R.lost_at(make_raid().fight, rf.players, deaths, R.RaidSettings()) is None


def test_raid_collapse_is_the_wipe_not_individual_fails():
    deaths = [{"id": 3, "timestamp": START + 30_000, "killingBlow": {"name": "Puddle"}}] + [
        {"id": pid, "timestamp": START + 100_000 + pid * 1000, "killingBlow": {"name": "Dark Frenzy"}}
        for pid in (4, 5, 6)  # 3 of 10 dead within 3s while the pull still has 3 minutes left
    ]
    rf = make_raid(kill=False, deaths=deaths)
    rep = R.analyze_fight(rf)
    assert [d.wipe for d in rep.deaths] == [False, True, True, True]
    assert [f.player.name for f in rep.fails if f.category == R.DEATH] == ["player3"]
    assert R.lost_at(rf.fight, rf.players, deaths, R.RaidSettings()) == START + 104_000


def test_raid_consumables_only_flagged_when_raid_has_them():
    rf = make_raid(n_tanks=0, n_others=4)
    for pid in (1, 2, 3):
        rf.buffs[pid] = [{"name": "Flask of Alchemical Chaos", "guid": 1, "totalUptime": DUR}]
        rf.casts[pid] = [{"name": "Tempered Potion", "guid": 2, "total": 1}]
    rf.casts[4] = [{"name": "Algari Healing Potion", "guid": 3, "total": 1}]  # not a combat potion
    fails = R.analyze_fight(rf).fails
    assert {(f.player.name, f.subject) for f in fails} == {("player4", "Flask"), ("player4", "Kampftrank")}
    assert "3/4 im Raid" in fails[0].title  # nobody had food -> pattern didn't match, nothing flagged


def test_raid_kicks_cast_and_channel():
    rf = make_raid(n_tanks=0, n_others=2, ability_names={500: "Void Bolt", 600: "Dark Channel"})
    rf.interrupts = [{"type": "interrupt", "sourceID": 1, "extraAbilityGameID": 500}] * 2 + [
        {"type": "interrupt", "sourceID": 2, "extraAbilityGameID": 600}
    ]
    rf.enemy_casts = (
        [{"type": "begincast", "abilityGameID": 500}] * 3 + [{"type": "cast", "abilityGameID": 500}]
        + [{"type": "cast", "abilityGameID": 600}] * 3  # channel: "cast" at start, one of them kicked
    )
    rep = R.analyze_fight(rf)
    assert rep.kicks == Counter({"player1": 2, "player2": 1})
    assert sorted(rep.kicks_through) == [("Dark Channel", 2, 1), ("Void Bolt", 1, 2)]


def test_raid_stats_per_player_and_merged():
    rf = make_raid(n_tanks=1, n_others=2, ability_names={600: "Shadow Mark", 57723: "Exhaustion", 500: "Void Bolt"})
    rf.damage_done = {2: {"id": 2, "total": 3_000_000}}
    rf.healing = {3: {"id": 3, "total": 600_000, "overheal": 200_000}}
    rf.damage_taken[2] = [
        {"name": "Fire Puddle", "guid": 2, "total": 200_000, "hitCount": 3, "tickCount": 1},
        {"name": "Burning Rush", "guid": 111400, "total": 50_000, "sources": [{"total": 50_000, "type": "Warlock"}]},
    ]
    rf.buffs[2] = [{"name": "Flask", "guid": 9, "totalUptime": 150_000, "totalUses": 1}]
    rf.casts[2] = [{"name": "Fireball", "guid": 133, "total": 40}]
    rf.interrupts = [{"type": "interrupt", "sourceID": 2, "extraAbilityGameID": 500}] * 2
    rf.dispels = [{"name": "Shadow Mark", "guid": 600, "details": [{"id": 3, "total": 2}, {"id": 2, "total": 1}]}]
    rf.debuffs = [
        {"type": "applydebuff", "sourceID": 90, "targetID": 2, "abilityGameID": 600, "timestamp": START + 10_000},
        {"type": "removedebuff", "sourceID": 90, "targetID": 2, "abilityGameID": 600, "timestamp": START + 16_000},
        {"type": "applydebuff", "sourceID": 90, "targetID": 2, "abilityGameID": 600, "timestamp": START + 280_000},
        # was already up at the pull (lust in the pull before)
        {"type": "removedebuff", "sourceID": 3, "targetID": 2, "abilityGameID": 57723, "timestamp": START + 30_000},
    ]
    st = R.analyze_fight(rf).stats["player2"]
    assert st.seconds == 300 and st.dps == 10_000
    assert [(x.name, x.amount, x.count, x.friendly) for x in st.taken] == [
        ("Fire Puddle", 200_000, 4, False), ("Burning Rush", 50_000, 0, True)]  # own damage last
    assert [(x.name, x.count, x.uptime, x.friendly) for x in st.debuffs] == [
        ("Shadow Mark", 2, 26.0, False), ("Exhaustion", 0, 30.0, True)]  # open until the end: 20s
    assert (st.buffs[0].uptime, st.casts[0].count) == (150.0, 40)
    assert [(x.name, x.count) for x in st.kicks] == [("Void Bolt", 2)]
    assert [(x.name, x.count) for x in st.dispels] == [("Shadow Mark", 1)]
    assert st.damage is None  # per ability only on demand
    healer = R.analyze_fight(rf).stats["player3"]
    assert (healer.hps, healer.overheal_total, healer.dispels[0].count) == (2000, 200_000, 2)

    # several pulls add up: rates and uptimes become averages over the combined time
    two = R.merge_stats([st, R.analyze_fight(rf).stats["player2"]])
    assert (two.seconds, two.pulls, two.dps) == (600, 2, 10_000)
    assert two.taken[0].count == 8 and two.debuffs[0].uptime == 52.0 and two.damage is None


def test_ability_rows_pets_and_crits():
    rows = R.ability_rows([
        {"name": "Fireball", "guid": 133, "total": 100, "hitCount": 8, "tickCount": 2, "critHitCount": 3},
        {"name": "Bob", "guid": 691, "total": 300, "sources": [{"name": "Bob", "type": "Pet"}]},
        {"name": "fireball", "guid": 1330, "total": 50, "hitCount": 5},  # variant ID, same spell
        {"name": "Melee", "guid": 1, "total": 0},
    ])
    assert [(r.name, r.spell_id, r.amount, r.count, r.crits) for r in rows] == [
        ("Bob (Pet)", None, 300, 0, 0), ("Fireball", 133, 150, 15, 3)]


def test_load_breakdown_only_missing_pulls():
    client = FakeRaidClient()
    fx = Fetcher(client)
    fight = Fetcher.to_fight(client.query("fights(killType", {"code": "x"})["reportData"]["report"]["fights"][0])
    from wcl_coach.pipeline import load_breakdown, raid_fights

    reports = list(raid_fights(fx, "AbCdEf1234567890", [fight], R.RaidSettings(), lambda _m: None))
    assert load_breakdown(fx, "AbCdEf1234567890", reports, "player1") == 1
    st = reports[0].stats["player1"]
    assert [(x.name, x.amount) for x in st.damage] == [("Fireball", 20e6), ("Pyroblast", 8e6), ("Bob (Pet)", 2e6)]
    assert st.healing[0].overheal == 100_000
    assert client.breakdowns == [(2, [7])]
    assert load_breakdown(fx, "AbCdEf1234567890", reports, "player1") == 0  # already there
    assert load_breakdown(fx, "AbCdEf1234567890", reports, "nobody") == 0
    assert len(client.breakdowns) == 1


def test_raid_death_recap():
    deaths = [{"id": 3, "timestamp": START + 30_000, "killingBlow": {"name": "Smash", "guid": 7}, "events": [
        {"type": "damage", "timestamp": START + 28_500, "amount": 100, "ability": {"name": "Pulse", "guid": 8}},
        {"type": "heal", "timestamp": START + 29_000, "amount": 50, "ability": {"name": "Renew", "guid": 139}},
        {"type": "damage", "timestamp": START + 29_500, "amount": 0, "ability": {"name": "Shield Bash", "guid": 9}},
        {"type": "damage", "timestamp": START + 30_000, "amount": 900, "ability": {"name": "Smash", "guid": 7}},
    ]}]
    d = R.analyze_fight(make_raid(n_tanks=1, n_others=3, deaths=deaths)).deaths[0]
    assert d.recap == [("Smash", 7, 900, 0.0), ("Pulse", 8, 100, 1.5)]


def test_raid_summary_order_and_filter():
    deaths = [{"id": 3, "timestamp": START + 30_000, "killingBlow": {"name": "Smash"}}]
    rf = make_raid(n_tanks=1, n_others=3, deaths=deaths)
    rf.damage_taken[2] = [{"name": "Fire Puddle", "guid": 2, "total": 200_000}]
    reports = [R.analyze_fight(rf), R.analyze_fight(rf)]
    ps = R.summarize(reports)
    assert [p.player.name for p in ps][:2] == ["player3", "player2"]  # first death outweighs a puddle
    assert ps[0].first_deaths == 2 and ps[0].count(R.DEATH) == 2
    assert [p.player.name for p in R.summarize(reports, only={"player2"})] == ["player2"]
    causes = R.top_causes(reports)
    assert {(c, s): sum(w.values()) for c, s, w in causes} == {(R.DEATH, "Smash"): 2, (R.AVOIDABLE, "Fire Puddle"): 2}
    rf.damage_done = {2: {"total": 600_000}}
    p2 = next(p for p in R.summarize([R.analyze_fight(rf)] * 2) if p.player.name == "player2")
    assert (p2.seconds, p2.dps) == (600, 2000)  # 2 × 600k in 2 × 300 s


class FakeRaidClient:
    """A tank, two dps and a healer. player2 stands in fire, dies first and has no flask; the pull wipes."""

    PLAYERS = {"tanks": [(1, "tank1", "Warrior", "Protection")],
               "dps": [(2, "player1", "Mage", "Fire"), (3, "player2", "Rogue", "Outlaw")],
               "healers": [(4, "healer1", "Priest", "Holy")]}

    def __init__(self):
        self.table_vars = []
        self.totals_vars = []
        self.breakdowns = []  # (player id, fight IDs) per breakdown request

    def query(self, gql, variables=None, cache=True):
        v = variables or {}
        fight = {"id": 7, "name": "Boss", "encounterID": 3009, "kill": False, "difficulty": 5,
                 "startTime": START, "endTime": START + DUR, "fightPercentage": 48.5}
        if "fights(killType" in gql:
            return {"reportData": {"report": {"code": v["code"], "title": "Raid", "zone": {"name": "Z"},
                                              "fights": [fight], "masterData": {"actors": []}}}}
        if "abilities" in gql:
            return {"reportData": {"report": {"masterData": {"abilities": [
                {"gameID": 500, "name": "Void Bolt"}, {"gameID": 1766, "name": "Kick"},
                {"gameID": 600, "name": "Shadow Mark"}, {"gameID": 57723, "name": "Exhaustion"}]}}}}
        if "playerDetails" in gql:
            pd = {role: [{"id": i, "name": n, "type": c, "specs": [{"spec": s, "count": 1}]} for i, n, c, s in ps]
                  for role, ps in self.PLAYERS.items()}
            return {"reportData": {"report": {"playerDetails": {"data": {"playerDetails": pd}},
                                              "rDps": {"data": []}, "rHps": {"data": []}}}}
        if "deaths:" in gql:
            return {"reportData": {"report": {"deaths": {"data": {"entries": [
                {"id": 3, "name": "player2", "timestamp": START + 60_000, "killingBlow": {"name": "Fire Puddle"}},
                {"id": 1, "name": "tank1", "timestamp": START + 290_000, "killingBlow": {"name": "Enrage"}},
                {"id": 4, "name": "healer1", "timestamp": START + 295_000, "killingBlow": {"name": "Enrage"}},
            ]}}}}}
        if "dispels:" in gql:  # raid totals
            self.totals_vars.append(v)
            done = [{"id": 1, "name": "tank1", "total": 9_000_000}, {"id": 2, "name": "player1", "total": 30_000_000},
                    {"id": 3, "name": "player2", "total": 6_000_000}, {"id": 4, "name": "healer1", "total": 1_000_000}]
            healing = [{"id": 4, "name": "healer1", "total": 20_000_000, "overheal": 5_000_000}]
            dispels = [{"entries": [{"name": "Shadow Mark", "guid": 600,
                                     "details": [{"name": "healer1", "id": 4, "total": 3}]}]}]
            return {"reportData": {"report": {"done": {"data": {"entries": done}},
                                              "healing": {"data": {"entries": healing}},
                                              "dispels": {"data": {"entries": dispels}}}}}
        if re.search(r"\bd\d+: table", gql):  # one player's damage and healing per ability
            pid = int(re.search(r"sourceID: (\d+)", gql)[1])
            fids = [int(x) for x in re.findall(r"\bd(\d+): table", gql)]
            self.breakdowns.append((pid, fids))
            out = {}
            for fid in fids:
                out[f"d{fid}"] = {"data": {"entries": [
                    {"name": "Fireball", "guid": 133, "total": 20_000_000, "hitCount": 100, "critHitCount": 40},
                    {"name": "Pyroblast", "guid": 11366, "total": 8_000_000, "hitCount": 20, "critHitCount": 15},
                    {"name": "Bob", "guid": 1122, "total": 2_000_000, "sources": [{"name": "Bob", "type": "Pet"}]},
                ]}}
                out[f"h{fid}"] = {"data": {"entries": [
                    {"name": "Leech", "guid": 143924, "total": 500_000, "overheal": 100_000, "hitCount": 50}]}}
            return {"reportData": {"report": out}}
        if "sourceID: " in gql:  # aliased raid tables
            self.table_vars.append(v)
            out = {}
            for alias, sid in re.findall(r"([tbc])(\d+): table", gql):
                sid = int(sid)
                if alias == "t":
                    taken = [{"name": "Raid Pulse", "guid": 1, "total": 400_000}]
                    if sid == 1:
                        taken.append({"name": "Tank Buster", "guid": 5, "total": 900_000})
                    if sid == 3:
                        taken.append({"name": "Fire Puddle", "guid": 2001, "total": 300_000, "hitCount": 4})
                    out[f"t{sid}"] = {"data": {"entries": taken}}
                elif alias == "b":
                    auras = [] if sid == 3 else [{"name": "Flask of Tempered Swiftness", "guid": 9, "totalUptime": DUR}]
                    out[f"b{sid}"] = {"data": {"totalTime": DUR, "auras": auras}}
                else:
                    out[f"c{sid}"] = {"data": {"entries": [{"name": "Tempered Potion", "guid": 2, "total": 1}]}}
            return {"reportData": {"report": out}}
        if "hostilityType" in gql:
            data = []
            if v["dt"] == "Interrupts":
                data = [{"type": "interrupt", "sourceID": 2, "abilityGameID": 1766, "extraAbilityGameID": 500}] * 2
            elif v["dt"] == "Debuffs":  # player2 gets marked twice, everyone is exhausted after the lust
                data = [{"type": "applydebuff", "sourceID": 90, "targetID": 3, "abilityGameID": 600,
                         "timestamp": START + t * 1000} for t in (10, 40)]
                data += [{"type": "removedebuff", "sourceID": 90, "targetID": 3, "abilityGameID": 600,
                          "timestamp": START + t * 1000} for t in (16, 46)]
                data += [{"type": "applydebuff", "sourceID": 4, "targetID": pid, "abilityGameID": 57723,
                          "timestamp": START + 5000} for pid in (1, 2, 3, 4)]
            elif v["host"] == "Enemies":
                data = [{"type": "begincast", "abilityGameID": 500}] * 3 + [{"type": "cast", "abilityGameID": 500},
                                                                            {"type": "cast", "abilityGameID": 77}]
            return {"reportData": {"report": {"events": {"data": data, "nextPageTimestamp": None}}}}
        raise AssertionError(f"unexpected query: {gql[:80]}")


def test_raid_end_to_end(monkeypatch, capsys, tmp_path):
    client = FakeRaidClient()
    monkeypatch.setattr(cli, "_client", lambda args: client)
    out_md = tmp_path / "raid.md"
    cli.main(["raid", "AbCdEf1234567890", "--md", str(out_md)])
    out = capsys.readouterr().out
    overview = out.split("Pro Spieler")[0]
    assert overview.index("player2") < overview.index("player1")  # worst first
    assert "'Fire Puddle': 4 Treffer" in out and "1/3 Spieler" in out
    assert "Raid Pulse" not in out and "Tank Buster" not in out
    assert "Kein Flask (3/4 im Raid hatten einen)" in out
    assert "Tode vor dem Wipe: player2 1:00 'Fire Puddle' (erster)" in out
    assert "Wipe ab 4:50: 2 Tote – 'Enrage' 2× · zuerst tank1" in out
    assert "Kicks: player1 2" in out and "'Void Bolt' 1× (2× gekickt)" in out
    assert "Ohne Fails: healer1, player1, tank1" in out
    # tables stop when the wipe started, so survivors aren't blamed for the collapse
    assert all(tv["et"] == START + 290_000 for tv in client.table_vars) and len(client.table_vars) == 1
    assert [tv["et"] for tv in client.totals_vars] == [START + 290_000]
    assert client.breakdowns == []  # per-ability damage is only loaded on demand (UI)
    md = out_md.read_text()
    assert md.startswith("# Raid-Auswertung: Raid") and "| player2 | 1 | 1 | 1 | 1 | Flask 1× | – |" in md
    assert 'background:#f8a5a5' in md  # colored severity badges


def test_raid_player_filter(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_client", lambda args: FakeRaidClient())
    cli.main(["raid", "AbCdEf1234567890", "-p", "player1,nobody"])
    captured = capsys.readouterr()
    overview = captured.out.split("Pro Spieler")[0]
    assert "player1" in overview and "player2" not in overview
    assert "Nicht im Report gefunden: nobody" in captured.err
