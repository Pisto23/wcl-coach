"""UI: pure helpers plus a simulated user session against the fake raid API (no browser needed)."""
from __future__ import annotations

import asyncio
import os

import pytest

pytest.importorskip("nicegui")

from nicegui.testing import user_simulation  # noqa: E402

from test_coach import FakeRaidClient  # noqa: E402
from wcl_coach import ui as U  # noqa: E402
from wcl_coach.fetch import Fight  # noqa: E402
from wcl_coach.pipeline import select_fights  # noqa: E402
from wcl_coach.raid import PlayerStats, Stat  # noqa: E402


def test_linkify_escapes_text_and_links_abilities():
    spells = {"Ula'tek's Presence": 1285965, "Tempest": 1287083, "Tempest Wave": 1287084}
    out = U.linkify("'Ula'tek's Presence' <b> & 'Tempest Wave', 'Tempest'", spells, "de")
    assert '<a href="https://de.wowhead.com/spell=1285965" target="_blank" rel="noopener">Ula\'tek\'s Presence</a>' in out
    assert "spell=1287084\"" in out and "spell=1287083\"" in out  # the longer name isn't swallowed by the shorter one
    assert "&lt;b&gt; &amp;" in out and "<b>" not in out
    assert U.linkify("'Unknown'", spells) == "'Unknown'"  # no ID -> plain text
    item = U.linkify("Trinket: https://www.wowhead.com/item=270164", {}, "en")
    assert '<a href="https://www.wowhead.com/item=270164"' in item
    # WCL's melee/falling IDs are different spells on Wowhead – no link, or the tooltip would rename them
    assert U.linkify("'Melee'", {"Melee": 1}) == "'Melee'" and U.spell_link("Falling", 3) == "Falling"
    assert "spell=17" in U.spell_link("Power Word: Shield", 17)


def test_stats_html_tables():
    st = PlayerStats(
        seconds=120, damage_total=2_400_000, healing_total=100_000, overheal_total=100_000,
        taken=[Stat("Fire <Puddle>", 2001, amount=300_000, count=4), Stat("Burning Rush", 111400, amount=100_000, friendly=True)],
        buffs=[Stat("Flask", 9, uptime=150, count=1)],  # overlapping variants can exceed the window
        debuffs=[Stat("Shadow Mark", 600, count=2, uptime=12)],
        casts=[Stat("Fireball", 133, count=30)],
        kicks=[Stat("Void Bolt", 500, count=2)],
        damage=[Stat("Fireball", 133, amount=1_800_000, count=60, crits=15), Stat("Bob (Pet)", None, amount=600_000)],
    )
    dmg = U.stats_html("damage", st, "en")
    assert "Gesamt <b>2.4M</b> · <b>20k</b> DPS" in dmg
    assert '<a href="https://www.wowhead.com/spell=133"' in dmg and "<td>Bob (Pet)</td>" in dmg
    assert "<td>1.8M</td><td>" in dmg and "75%" in dmg and "<td>25%</td>" in dmg  # share, crit rate
    taken = U.stats_html("taken", st, "de", avoidable={"fire <puddle>"})
    assert "Fire &lt;Puddle&gt;</a>" in taken and "vermeidbar" in taken and "eigener / Raid" in taken
    assert "width:100%" in U.stats_html("buffs", st)  # capped
    assert "<td>15.0</td>" in U.stats_html("casts", st)  # 30 casts in 2 minutes
    assert "Unterbrochen" in U.stats_html("kicks", st) and "Nichts in dieser Auswahl" in U.stats_html("dispels", st)
    assert "Overheal 50%" in U.total_line("healing", st)


FIGHTS = [Fight(id=i, name=n, encounter_id=1, kill=k, difficulty=5, start=0, end=90_000, boss_pct=None if k else 40)
          for i, n, k in [(1, "Twin Fangs", False), (2, "Twin Fangs", True), (3, "Sszorak", False),
                          (4, "Twin Fangs", False)]]


def test_fight_selects_boss_result_pull():
    assert U.boss_options(FIGHTS) == {"all": "Alle Bosse · 4 Pulls",
                                      "twin fangs": "Twin Fangs · 3 Pulls (1 Kill, 2 Wipes)",
                                      "sszorak": "Sszorak · 1 Pull (0 Kills, 1 Wipe)"}
    assert U.result_options(FIGHTS, "twin fangs") == {"both": "Kills + Wipes (3)", "kills": "Kills (1)",
                                                      "wipes": "Wipes (2)"}
    opts = U.pull_options(FIGHTS, "twin fangs", "wipes")
    assert list(opts) == ["all", "1", "4"] and opts["all"] == "Alle passenden Pulls (2)"
    assert opts["4"] == "#4 Twin Fangs (Wipe 40%) · 1:30"
    assert U.pull_options(FIGHTS, "sszorak", "kills") == {"all": "Keine passenden Pulls"}
    assert U.pull_options(FIGHTS, "all", "both", extra="1-3")["1-3"] == "Auswahl aus dem Link: 1-3"


def test_fight_selection_round_trip():
    # every combination of the selects is a selection select_fights picks exactly those pulls with
    for boss in U.boss_options(FIGHTS):
        for result in ("both", "kills", "wipes"):
            want = U.matching(FIGHTS, boss, result)
            sel = U.selection(boss, result, "all")
            if want:
                assert select_fights(FIGHTS, sel, None) == want, sel
            assert U.split_selection(sel, FIGHTS) == (boss, result, "all")
    assert U.selection("twin fangs", "wipes", "all") == "twin fangs:wipes"
    assert U.selection("all", "kills", "all") == "kills" and U.selection("sszorak", "both", "4") == "4"
    assert U.split_selection("4", FIGHTS) == ("twin fangs", "both", "4")  # a pull shows its boss
    assert U.split_selection("Twin Fangs:Kills", FIGHTS) == ("twin fangs", "kills", "all")
    assert U.split_selection("1-3", FIGHTS) == ("all", "both", "1-3")  # kept as is


def test_save_credentials_keeps_other_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("WCL_CLIENT_ID", "")  # restored after the test
    monkeypatch.setenv("WCL_CLIENT_SECRET", "")
    env = tmp_path / "wcl-coach" / ".env"
    env.parent.mkdir()
    env.write_text("WCL_SITE=classic\nWCL_CLIENT_ID=old\n")
    U.save_credentials("id1", "sec1", env)
    assert env.read_text() == "WCL_SITE=classic\nWCL_CLIENT_ID=id1\nWCL_CLIENT_SECRET=sec1\n"
    assert oct(env.stat().st_mode & 0o777) == "0o600"
    assert os.environ["WCL_CLIENT_ID"] == "id1"


def test_ui_raid_flow(monkeypatch):
    monkeypatch.setenv("WCL_CLIENT_ID", "x")
    monkeypatch.setenv("WCL_CLIENT_SECRET", "y")

    async def scenario() -> None:
        async with user_simulation(lambda: U.page(FakeRaidClient)) as user:
            await user.open("/")
            await user.should_see("Report-URL einfügen")
            user.find(marker="report").type("AbCdEf1234567890")
            user.find(marker="load").click()
            await user.should_see("Auswahl: 1 Pull")

            # boss + kills/wipes: the only pull is a wipe
            assert user.find(marker="boss").elements.pop().options == {
                "all": "Alle Bosse · 1 Pull", "boss": "Boss · 1 Pull (0 Kills, 1 Wipe)"}
            user.find(marker="boss").elements.pop().value = "boss"
            user.find(marker="result").elements.pop().value = "kills"
            await user.should_see("Keine passenden Pulls für diese Auswahl")
            assert not user.find(marker="analyze").elements.pop().enabled
            user.find(marker="result").elements.pop().value = "wipes"
            await user.should_see("Auswahl: 1 Pull")
            assert user.find(marker="pull").elements.pop().options == {
                "all": "Alle passenden Pulls (1)", "7": "#7 Boss (Wipe 48%) · 5:00"}
            user.find(marker="analyze").click()
            await user.should_see("Pro Pull")

            table = user.find(marker="overview").elements.pop()
            assert [r["name"] for r in table.rows][0] == "player2"  # worst first
            await user.should_see("Wipe ab 4:50")

            # clicking a row picks the character: their fails, abilities as Wowhead links
            user.find(marker="overview").trigger("rowClick", [{}, {"name": "player2"}, 0])
            await user.should_see("Wichtigste Punkte")
            assert user.find(marker="char").elements.pop().value == "player2"
            await user.should_see('href="https://de.wowhead.com/spell=2001"')  # Fire Puddle, from the damage table
            await user.should_see("Kein Flask")
            await user.should_see("Detail-Analyse gegen Top-Spieler")

            # stats: damage per ability is loaded for the character on screen, the rest is already there
            await user.should_see("Statistik")
            await user.should_see('href="https://de.wowhead.com/spell=11366"')  # Pyroblast, from the breakdown
            await user.should_see("Bob (Pet)")
            await user.should_see("Debuffs (2)")  # Shadow Mark from the boss, Exhaustion from the raid
            await user.should_see("Tode (1)")
            await user.should_see("durch Fire Puddle · erster Tod")  # no spell ID in the deaths table -> plain
            assert table.rows[0]["dps"] == round(6e6 / 290)  # the wipe counts until it was lost at 4:50

            # priority filter: without HOCH only the missing flask (MITTEL) is left
            user.find(marker="prio").elements.pop().value = [U.MEDIUM, U.LOW]
            await user.should_not_see("Tod durch")
            await user.should_see("Kein Flask")

            # back to the raid overview
            user.find("Raid-Übersicht").click()
            await user.should_see("Klick auf einen Spieler")

    asyncio.run(scenario())


def test_ui_asks_for_credentials(monkeypatch):
    monkeypatch.delenv("WCL_CLIENT_ID", raising=False)
    monkeypatch.delenv("WCL_CLIENT_SECRET", raising=False)

    async def scenario() -> None:
        async with user_simulation(lambda: U.page(FakeRaidClient)) as user:
            await user.open("/")
            await user.should_see("WCL-API-Zugang")
            await user.should_see("api/clients")

    asyncio.run(scenario())


def test_ui_never_sends_the_secret_to_the_browser(monkeypatch):
    monkeypatch.setenv("WCL_CLIENT_ID", "my-client")
    monkeypatch.setenv("WCL_CLIENT_SECRET", "topsecret123")

    async def scenario() -> None:
        async with user_simulation(lambda: U.page(FakeRaidClient)) as user:
            await user.open("/")
            await user.should_see("my-client")  # the dialog is built, just closed
            await user.should_not_see("topsecret123")

    asyncio.run(scenario())


def test_ui_url_fight_parameter_sets_the_selects(monkeypatch):
    monkeypatch.setenv("WCL_CLIENT_ID", "x")
    monkeypatch.setenv("WCL_CLIENT_SECRET", "y")

    async def scenario() -> None:
        async with user_simulation(lambda: U.page(FakeRaidClient)) as user:
            await user.open("/?report=AbCdEf1234567890&fight=boss:wipes")
            await user.should_see("Klick auf einen Spieler")
            assert user.find(marker="boss").elements.pop().value == "boss"
            assert user.find(marker="result").elements.pop().value == "wipes"
            assert user.find(marker="pull").elements.pop().value == "all"

    asyncio.run(scenario())


def test_ui_url_parameters_open_a_result(monkeypatch):
    monkeypatch.setenv("WCL_CLIENT_ID", "x")
    monkeypatch.setenv("WCL_CLIENT_SECRET", "y")

    async def scenario() -> None:
        async with user_simulation(lambda: U.page(FakeRaidClient)) as user:
            await user.open("/?report=AbCdEf1234567890&char=player1")
            await user.should_see("Wichtigste Punkte")
            await user.should_see("Keine Fails in dieser Auswahl")
            assert user.find(marker="char").elements.pop().value == "player1"

    asyncio.run(scenario())
