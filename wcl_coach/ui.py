"""Local UI (NiceGUI): load a report, pick a character, see what went wrong.

Ability names are Wowhead links – Wowhead's tooltip script shows the original
in-game tooltip on hover, so you can see what a spell actually does.
"""
from __future__ import annotations

import html
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, TypeVar

from nicegui import run, ui

from .analysis import HIGH, INFO, LOW, MEDIUM, SEVERITY_LABEL, Finding, Settings, fmt_t
from .api import CONFIG_ENV, WCLClient, WCLError
from .fetch import Fetcher, Fight, parse_report_ref
from .pipeline import PlayerResult, load_breakdown, player_fights, raid_fights, recurring, select_fights
from .raid import (
    AVOIDABLE,
    DEATH,
    PREP,
    Death,
    Fail,
    FightReport,
    PlayerStats,
    PlayerSummary,
    RaidSettings,
    Stat,
    fmt_amount,
    merge_stats,
    summarize,
    top_causes,
)
from .render import SEVERITY_BG, STATS_BG

T = TypeVar("T")

WOWHEAD = {"de": "https://de.wowhead.com", "en": "https://www.wowhead.com"}
SEVERITIES = {HIGH: "HOCH", MEDIUM: "MITTEL", LOW: "NIEDRIG"}
ROLES = {"tank": "Tank", "healer": "Heiler", "dps": "DPS"}
POINTS_PER_PULL = 75  # raid mode, 20+ players – measured on a real report
BREAKDOWN_POINTS = 2  # damage + healing per ability, one player and pull – measured
# WCL's own small IDs (1 melee, 3 falling, other environment damage) are unrelated test spells on Wowhead
FIRST_SPELL_ID = 10
STAT_TABS = {"damage": "Schaden", "healing": "Heilung", "taken": "Erhaltener Schaden", "buffs": "Buffs",
             "debuffs": "Debuffs", "casts": "Zauber", "kicks": "Kicks", "dispels": "Dispels", "deaths": "Tode"}
# sorts the overview table by the raw number but shows 1.2M / 345k like fmt_amount
AMOUNT_JS = "v => v >= 1e6 ? (v / 1e6).toFixed(1) + 'M' : v >= 1e3 ? Math.round(v / 1e3) + 'k' : String(v)"

HEAD = f"""
<script>const whTooltips = {{colorLinks: true, iconizeLinks: true, renameLinks: true}};</script>
<script src="https://wow.zamimg.com/js/tooltips.js"></script>
<style>
  .sev {{ color: #000; padding: 1px 6px; border-radius: 4px; font-size: .75rem; font-weight: 600;
          white-space: nowrap; }}
  .stats {{ background: {STATS_BG}; color: #000; padding: 0 4px; border-radius: 3px; }}
  .muted {{ opacity: .7; }}
  .stat-wrap {{ overflow-x: auto; }}
  .stat {{ width: 100%; border-collapse: collapse; font-size: .875rem; }}
  .stat th {{ font-weight: 600; opacity: .7; text-align: right; padding: 4px 8px; white-space: nowrap;
              border-bottom: 1px solid rgba(128, 128, 128, .4); }}
  .stat td {{ text-align: right; padding: 3px 8px; white-space: nowrap;
              border-bottom: 1px solid rgba(128, 128, 128, .15); }}
  .stat th:first-child, .stat td:first-child {{ text-align: left; white-space: normal; }}
  .stat .bar {{ display: inline-block; width: 5rem; height: .5rem; margin-right: 6px; vertical-align: middle;
                background: rgba(128, 128, 128, .2); border-radius: 3px; overflow: hidden; }}
  .stat .bar > span {{ display: block; height: 100%; background: #4dabf7; }}
</style>
"""


# ------------------------------------------------------------------ helpers


def _linkable(spell_id: int | None) -> bool:
    return bool(spell_id) and spell_id >= FIRST_SPELL_ID


def spell_link(name: str, spell_id: int | None, lang: str = "de") -> str:
    """Wowhead link (original tooltip on hover), or just the escaped name without a usable spell ID."""
    text = html.escape(name, quote=False)
    if not _linkable(spell_id):
        return text
    return f'<a href="{WOWHEAD.get(lang, WOWHEAD["en"])}/spell={spell_id}" target="_blank" rel="noopener">{text}</a>'


def linkify(text: str, spells: dict[str, int], lang: str = "de") -> str:
    """HTML-escapes `text` and turns quoted ability names ('Tempest') and Wowhead item URLs into
    Wowhead links, which get the original tooltip on hover."""
    base = WOWHEAD.get(lang, WOWHEAD["en"])
    out = html.escape(text, quote=False)
    for name, sid in sorted(spells.items(), key=lambda kv: -len(kv[0])):  # longest first: no partial matches
        if _linkable(sid):
            out = out.replace(f"'{html.escape(name, quote=False)}'", spell_link(name, sid, lang))
    return re.sub(
        r"https://www\.wowhead\.com/item=(\d+)",
        lambda m: f'<a href="{base}/item={m[1]}" target="_blank" rel="noopener">Item {m[1]}</a>',
        out,
    )


def badge(sev: int) -> str:
    return f'<span class="sev" style="background:{SEVERITY_BG[sev]}">{SEVERITY_LABEL[sev]}</span>'


def result_label(f: Fight) -> str:
    return "Kill" if f.kill else f"Wipe {f.boss_pct:.0f}%" if f.boss_pct is not None else "Wipe"


def pull_label(f: Fight) -> str:
    return f"#{f.id} {f.name} ({result_label(f)})"


# ---- fight selection: boss, kills/wipes/both, one pull or all of them


def _count(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def matching(fights: list[Fight], boss: str, result: str) -> list[Fight]:
    """Pulls of one boss ('all': every boss) – result 'both', 'kills' or 'wipes'."""
    return [f for f in fights
            if boss in ("all", f.name.lower()) and (result == "both" or f.kill == (result == "kills"))]


def boss_options(fights: list[Fight]) -> dict[str, str]:
    opts = {"all": f"Alle Bosse · {_count(len(fights), 'Pull', 'Pulls')}"}
    for boss in dict.fromkeys(f.name for f in fights):  # in pull order
        fs = matching(fights, boss.lower(), "both")
        kills = sum(f.kill for f in fs)
        opts[boss.lower()] = (f"{boss} · {_count(len(fs), 'Pull', 'Pulls')} "
                              f"({_count(kills, 'Kill', 'Kills')}, {_count(len(fs) - kills, 'Wipe', 'Wipes')})")
    return opts


def result_options(fights: list[Fight], boss: str) -> dict[str, str]:
    fs = matching(fights, boss, "both")
    kills = sum(f.kill for f in fs)
    return {"both": f"Kills + Wipes ({len(fs)})", "kills": f"Kills ({kills})", "wipes": f"Wipes ({len(fs) - kills})"}


def pull_options(fights: list[Fight], boss: str, result: str, extra: str = "all") -> dict[str, str]:
    """'all' = every matching pull, or one of them. `extra`: a selection from a link the selects can't show."""
    fs = matching(fights, boss, result)
    opts = {"all": f"Alle passenden Pulls ({len(fs)})" if fs else "Keine passenden Pulls"}
    opts |= {str(f.id): f"{pull_label(f)} · {fmt_t(f.duration_s)}" for f in fs}
    if extra not in opts:
        opts[extra] = f"Auswahl aus dem Link: {extra}"
    return opts


def selection(boss: str, result: str, pull: str) -> str:
    """The three selects as one selection for `select_fights` – the same text works as `-f` and `?fight=`."""
    if pull != "all":
        return pull
    if boss == "all":
        return "all" if result == "both" else result
    return boss if result == "both" else f"{boss}:{result}"


def split_selection(sel: str, fights: list[Fight]) -> tuple[str, str, str]:
    """(boss, result, pull) for a selection from a link; what the selects can't show is kept as the pull."""
    sel = sel.strip().lower()
    if sel in ("all", "kills", "wipes"):
        return "all", "both" if sel == "all" else sel, "all"
    boss, result = sel.rsplit(":", 1) if ":" in sel else (sel, "both")
    if result in ("both", "kills", "wipes") and any(f.name.lower() == boss for f in fights):
        return boss, result, "all"
    for f in fights:
        if sel == str(f.id):
            return f.name.lower(), "both", sel
    return "all", "both", sel


def player_label(ps: PlayerSummary) -> str:
    p = ps.player
    parts = [f"{p.name} – {p.spec or '?'} {p.class_name or '?'}"]
    if n := ps.count(DEATH):
        parts.append(f"{n} {'Tod' if n == 1 else 'Tode'}")
    if n := ps.count(AVOIDABLE):
        parts.append(f"{n}× vermeidbarer Schaden")
    return " · ".join(parts)


def group_fails(fails: list[Fail]) -> list[list[Fail]]:
    """The same problem across pulls (killing blow, ability, consumable) as one entry, worst first."""
    groups: dict[tuple[str, str], list[Fail]] = defaultdict(list)
    for f in fails:
        groups[(f.category, f.subject)].append(f)
    return sorted(groups.values(), key=lambda fs: (-max(f.severity for f in fs), -len(fs)))


def group_title(fs: list[Fail]) -> str:
    f = fs[0]
    if f.category == DEATH:
        return f"Tod durch '{f.subject}'"
    if f.category == AVOIDABLE:
        return f"Vermeidbarer Schaden durch '{f.subject}'"
    return f.title.split(" (")[0]  # "Kein Flask (15/20 im Raid hatten einen)" -> "Kein Flask"


def pull_lines(r: FightReport, lang: str) -> list[str]:
    """Deaths, wipe and kicks of one pull as HTML lines."""
    lines = []
    early = [d for d in r.deaths if not d.wipe]
    wipe = [d for d in r.deaths if d.wipe]
    if early:
        lines.append(
            f"<b>Tode{' vor dem Wipe' if wipe else ''}:</b> "
            + ", ".join(
                linkify(f"{d.player.name} {fmt_t(d.t)} '{d.killing_blow}'", r.spells, lang)
                + (" (erster)" if i == 0 else "")
                for i, d in enumerate(early)
            )
        )
    if wipe:
        causes = Counter(d.killing_blow for d in wipe).most_common(3)
        lines.append(
            f"<b>Wipe ab {fmt_t(wipe[0].t)}:</b> {len(wipe)} Tote – "
            + linkify(", ".join(f"'{kb}' {n}×" for kb, n in causes), r.spells, lang)
            + f" · zuerst {html.escape(wipe[0].player.name)}"
        )
    if not r.deaths:
        lines.append("Keine Tode")
    if r.kicks:
        lines.append("<b>Kicks:</b> " + html.escape(", ".join(f"{n} {k}" for n, k in r.kicks.most_common())))
    through = [x for x in r.kicks_through if x[1]]
    if through:
        lines.append(
            "<b>Durchgekommen:</b> "
            + linkify(", ".join(f"'{a}' {t}× ({k}× gekickt)" for a, t, k in through), r.spells, lang)
        )
    return lines


def bar(share: float) -> str:
    pct = max(0.0, min(1.0, share)) * 100
    return f'<span class="bar"><span style="width:{pct:.0f}%"></span></span>{pct:.0f}%'


def _share(part: float, whole: float) -> str:
    return f"{part / whole * 100:.0f}%" if whole else "–"


def stat_table(head: list[str], rows: list[list[str]]) -> str:
    """Cells are HTML already (escaped text, links). First column left, numbers right."""
    if not rows:
        return "<div class='text-sm muted'>Nichts in dieser Auswahl.</div>"
    th = "".join(f"<th>{h}</th>" for h in head)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in rows)
    return f"<div class='stat-wrap'><table class='stat'><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></div>"


def total_line(tab: str, st: PlayerStats) -> str:
    if tab == "damage":
        return f"Gesamt <b>{fmt_amount(st.damage_total)}</b> · <b>{fmt_amount(st.dps)}</b> DPS"
    oh = _share(st.overheal_total, st.healing_total + st.overheal_total)
    return f"Gesamt <b>{fmt_amount(st.healing_total)}</b> · <b>{fmt_amount(st.hps)}</b> HPS · Overheal {oh}"


def stats_html(tab: str, st: PlayerStats, lang: str = "de", avoidable: frozenset[str] | set[str] = frozenset()) -> str:
    """One tab of the stats card as HTML. `avoidable`: lower-case names flagged as avoidable damage."""
    def link(x: Stat) -> str:
        return spell_link(x.name, x.spell_id, lang)

    if tab in ("damage", "healing"):
        rows = (st.damage if tab == "damage" else st.healing) or []
        total = sum(x.amount for x in rows) or 1.0
        last = (["Treffer", "Krit"], lambda x: [str(x.count), _share(x.crits, x.count)]) if tab == "damage" else (
            ["Overheal"], lambda x: [_share(x.overheal, x.amount + x.overheal)])
        return f"<div class='text-sm mb-1'>{total_line(tab, st)}</div>" + stat_table(
            ["Fähigkeit", STAT_TABS[tab], "Anteil", "DPS" if tab == "damage" else "HPS", *last[0]],
            [[link(x), fmt_amount(x.amount), bar(x.amount / total), fmt_amount(x.amount / st.seconds), *last[1](x)]
             for x in rows],
        )
    if tab == "taken":
        total = sum(x.amount for x in st.taken)

        def note(x: Stat) -> str:
            if x.name.lower() in avoidable:
                return f'<span class="sev" style="background:{SEVERITY_BG[HIGH]}">vermeidbar</span>'
            return "<span class='muted'>eigener / Raid</span>" if x.friendly else ""

        return f"<div class='text-sm mb-1'>Gesamt <b>{fmt_amount(total)}</b></div>" + stat_table(
            ["Fähigkeit", "Schaden", "Anteil", "Treffer", ""],
            [[link(x), fmt_amount(x.amount), bar(x.amount / (total or 1)), str(x.count), note(x)] for x in st.taken],
        )
    if tab == "buffs":
        return stat_table(["Buff", "Uptime", "Anwendungen"],
                          [[link(x), bar(x.uptime / st.seconds), str(x.count)] for x in st.buffs])
    if tab == "debuffs":
        return stat_table(
            ["Debuff", "Anwendungen", "Uptime", "Quelle"],
            [[link(x), str(x.count), bar(x.uptime / st.seconds),
              "<span class='muted'>eigener / Raid</span>" if x.friendly else "Gegner"] for x in st.debuffs],
        )
    if tab == "casts":
        return stat_table(["Fähigkeit", "Casts", "pro Minute"],
                          [[link(x), str(x.count), f"{x.count / st.seconds * 60:.1f}"] for x in st.casts])
    rows = st.kicks if tab == "kicks" else st.dispels
    return stat_table(["Unterbrochen" if tab == "kicks" else "Entfernt", "Anzahl"],
                      [[link(x), str(x.count)] for x in rows])


def deaths_html(deaths: list[tuple[FightReport, Death]], lang: str = "de") -> str:
    """Deaths with killing blow and the last hits before them."""
    if not deaths:
        return "<div class='text-sm muted'>Keine Tode in dieser Auswahl 👍</div>"
    out = []
    for r, d in deaths:
        tag = " · erster Tod" if d is r.first_death else " · <span class='muted'>Pull war schon verloren</span>" if d.wipe else ""
        recap = ", ".join(
            f"{spell_link(name, sid, lang)} {fmt_amount(amount)}" + (f" <span class='muted'>(−{dt:.1f} s)</span>" if dt >= 0.05 else "")
            for name, sid, amount, dt in d.recap
        )
        out.append(
            f"<div class='mb-2'><b>{html.escape(pull_label(r.fight))}</b> · {fmt_t(d.t)} · durch "
            f"{spell_link(d.killing_blow, d.spell_id, lang)}{tag}"
            + (f"<div class='text-sm pl-4'><span class='muted'>Letzte Treffer:</span> {recap}</div>" if recap else "")
            + "</div>"
        )
    return "".join(out)


def save_credentials(client_id: str, secret: str, path: Path = CONFIG_ENV) -> None:
    """Writes the API client into the config .env, keeping other settings there."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    keep = [ln for ln in lines if not re.match(r"\s*WCL_CLIENT_(ID|SECRET)\s*=", ln)]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(keep + [f"WCL_CLIENT_ID={client_id}", f"WCL_CLIENT_SECRET={secret}"]) + "\n",
                    encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    os.environ["WCL_CLIENT_ID"], os.environ["WCL_CLIENT_SECRET"] = client_id, secret


def client_factory(site: str, use_cache: bool) -> Callable[[], WCLClient]:
    def make() -> WCLClient:  # reads the environment on every call: credentials may be set in the UI
        cid, secret = os.environ.get("WCL_CLIENT_ID"), os.environ.get("WCL_CLIENT_SECRET")
        if not cid or not secret:
            raise WCLError("WCL-API-Zugang fehlt – oben rechts unter ⚙ eintragen.")
        return WCLClient(cid, secret, site=site, use_cache=use_cache)

    return make


# --------------------------------------------------------------------- page


@dataclass
class State:
    code: str = ""
    title: str = ""
    fights: list[Fight] = field(default_factory=list)
    fight_sel: str = "all"  # selection the current reports were made with
    reports: list[FightReport] = field(default_factory=list)
    players: list[PlayerSummary] = field(default_factory=list)
    spells: dict[str, int] = field(default_factory=dict)  # every ability ID seen in the raid analysis
    selected: str | None = None
    severities: set[int] = field(default_factory=lambda: set(SEVERITIES))
    lang: str = "de"
    busy: bool = False
    status: str = ""
    details: dict[tuple[str, str], list[PlayerResult]] = field(default_factory=dict)  # (player, fights)
    stat_pull: str = "all"  # fight ID, or all pulls of the character together
    stat_tab: str = "damage"
    loading: set[str] = field(default_factory=set)  # characters whose damage/healing breakdown is loading


def page(make_client: Callable[[], WCLClient]) -> None:
    s = State()
    ui.add_head_html(HEAD)

    def tooltips() -> None:
        # Wowhead scans the page once on load – new links need a refresh for icons and localized names
        ui.run_javascript("setTimeout(() => window.$WowheadPower && $WowheadPower.refreshLinks(), 150)")

    def rerender() -> None:
        content.refresh()
        tooltips()

    def log(msg: str) -> None:  # called from worker threads, shown via binding
        s.status = msg.strip()

    async def work(text: str, fn: Callable[[], T]) -> T | None:
        """Runs blocking API work off the event loop; errors become a notification."""
        s.busy, s.status = True, text
        try:
            return await run.io_bound(fn)
        except Exception as e:  # noqa: BLE001 – anything from the API should end up in front of the user
            ui.notify(str(e) or type(e).__name__, type="negative", multi_line=True, close_button=True)
            return None
        finally:
            s.busy = False

    async def update_quota() -> None:
        try:
            rl = await run.io_bound(lambda: make_client().rate_limit())
            quota.text = f"API {rl['pointsSpentThisHour']:.0f} / {rl['limitPerHour']}"
        except Exception:  # noqa: BLE001 – the budget display is optional
            pass

    # -- actions -------------------------------------------------------------
    async def load() -> None:
        try:
            code, url_fight, _ = parse_report_ref(report_in.value or "")
        except ValueError:
            ui.notify("Keine Report-URL bzw. kein 16-stelliger Report-Code erkannt.", type="warning")
            return
        rep = await work("Lade Report …", lambda: Fetcher(make_client()).report(code))
        if rep is None:
            return
        s.code, s.title = code, rep.get("title") or code
        s.fights = [Fetcher.to_fight(f) for f in rep["fights"]]
        if not s.fights:
            ui.notify("Der Report enthält keine Boss-Kämpfe.", type="warning")
            return
        set_selection(str(url_fight) if url_fight else "all")
        fight_row.set_visibility(True)

    def current() -> tuple[str, str, str]:
        return boss_sel.value or "all", result_sel.value or "both", pull_sel.value or "all"

    def current_fights() -> list[Fight]:
        """The pulls the selects stand for; raises WCLError for a link selection that matches nothing."""
        boss, result, pull = current()
        if pull == "all" and not matching(s.fights, boss, result):
            return []  # select_fights("kills") would fall back to the last pull
        return select_fights(s.fights, selection(boss, result, pull), None)

    def set_selection(sel: str) -> None:
        boss, result, pull = split_selection(sel, s.fights)
        boss_sel.set_options(boss_options(s.fights), value=boss)
        result_sel.set_options(result_options(s.fights, boss), value=result)
        pull_sel.set_options(pull_options(s.fights, boss, result, extra=pull), value=pull)
        show_estimate()

    def on_boss() -> None:
        result_sel.set_options(result_options(s.fights, current()[0]), value=current()[1])
        on_result()

    def on_result() -> None:
        boss, result, _ = current()
        pull_sel.set_options(pull_options(s.fights, boss, result), value="all")
        show_estimate()

    def show_estimate() -> None:
        try:
            n = len(current_fights())
            text = (
                f"Auswahl: {n} {'Pull' if n == 1 else 'Pulls'} · Raid-Auswertung ≈ {n * POINTS_PER_PULL} API-Punkte, "
                "bereits geladene Pulls sind gratis" if n else "Keine passenden Pulls für diese Auswahl"
            )
        except WCLError as e:
            n, text = 0, str(e)
        info.text = f"{s.title} · {text}"
        analyze_btn.set_enabled(n > 0)

    async def analyze() -> None:
        sel = selection(*current())
        try:
            fights = current_fights()
        except WCLError as e:
            ui.notify(str(e), type="warning")
            return
        if not fights:
            ui.notify("Keine passenden Pulls für diese Auswahl.", type="warning")
            return

        def job() -> list[FightReport]:
            return list(raid_fights(Fetcher(make_client(), log=log), s.code, fights, RaidSettings(), log))

        reports = await work(f"Werte {len(fights)} Pulls aus …", job)
        if reports is None:
            return
        if not reports:
            ui.notify("Keine auswertbaren Kämpfe gefunden.", type="warning")
            return
        s.reports, s.fight_sel, s.players = reports, sel, summarize(reports)
        s.spells = {}
        for r in reports:
            s.spells |= r.spells
            for f in r.fails:
                s.spells |= f.spells
        s.details.clear()
        if s.selected not in {ps.player.name for ps in s.players}:
            s.selected = None
        char_sel.set_options({ps.player.name: player_label(ps) for ps in s.players}, value=s.selected)
        picks.set_visibility(True)
        rerender()
        await update_quota()

    async def run_details(name: str) -> None:
        fights = select_fights(s.fights, s.fight_sel, None)

        def job() -> list[PlayerResult]:
            fx = Fetcher(make_client(), log=log)
            return list(player_fights(fx, s.code, fights, name, None, 5, Settings(), log))

        results = await work(f"Detail-Analyse für {name} über {len(fights)} Pulls …", job)
        if results is None:
            return
        s.details[(name, s.fight_sel)] = results
        rerender()
        await update_quota()

    async def fetch_breakdown(name: str) -> None:
        """Damage and healing per ability for the character on screen – costs points per player."""
        reports = s.reports
        missing = [r for r in reports if (st := r.stats.get(name)) and (st.damage is None or st.healing is None)]
        if not missing or name in s.loading:
            return
        s.loading.add(name)
        rerender()
        try:
            n = await work(f"Lade Schaden und Heilung pro Fähigkeit für {name} ({len(missing)} Pulls) …",
                           lambda: load_breakdown(Fetcher(make_client(), log=log), s.code, reports, name))
        finally:
            s.loading.discard(name)
        if s.selected == name:
            rerender()
        if n:
            await update_quota()

    def select_player(name: str | None) -> None:
        char_sel.value = name  # triggers on_char

    async def on_char(e: Any) -> None:
        s.selected = e.value or None
        rerender()
        if s.selected:
            await fetch_breakdown(s.selected)

    def on_stat_pull(e: Any) -> None:
        s.stat_pull = e.value or "all"
        rerender()

    def on_stat_tab(e: Any) -> None:
        s.stat_tab = e.value
        tooltips()  # the panel's links are new on the page

    def on_prio(e: Any) -> None:
        s.severities = set(e.value or [])
        rerender()

    def on_lang(e: Any) -> None:
        s.lang = e.value
        rerender()

    def save_settings() -> None:
        cid = (cid_in.value or "").strip()
        secret = (secret_in.value or "").strip() or os.environ.get("WCL_CLIENT_SECRET", "")  # empty = keep
        if not cid or not secret:
            ui.notify("Bitte Client ID und Secret eintragen.", type="warning")
            return
        save_credentials(cid, secret)
        settings.close()
        ui.notify(f"Gespeichert in {CONFIG_ENV}", type="positive")

    # -- views ---------------------------------------------------------------
    def fail_html(f: Fail) -> str:
        return f"<span class='muted'>{html.escape(pull_label(f.fight))}:</span> {linkify(f.title, f.spells, s.lang)}"

    def finding_block(f: Finding) -> None:
        with ui.column().classes("gap-0 w-full mb-2"):
            ui.html(f"{badge(f.severity)} <b>{html.escape(f.category)}</b>: {linkify(f.title, f.spells, s.lang)}",
                    sanitize=False)
            if f.detail:
                ui.html(linkify(f.detail, f.spells, s.lang), sanitize=False).classes("text-sm pl-6 muted")
            if f.tip:
                ui.label(f"→ {f.tip}").classes("text-sm pl-6")

    def raid_view() -> None:
        kills = sum(1 for r in s.reports if r.fight.kill)
        with ui.card().classes("w-full"):
            ui.label(s.title).classes("text-lg font-bold")
            ui.label(
                f"{len(s.reports)} Pulls ({kills} Kills, {len(s.reports) - kills} Wipes) · {len(s.players)} Spieler"
                " · Klick auf einen Spieler zeigt die Details"
            ).classes("text-sm muted")
            rows = [
                {
                    "name": ps.player.name,
                    "spec": f"{ps.player.spec or '?'} {ps.player.class_name or '?'}",
                    "pulls": ps.pulls,
                    "deaths": ps.count(DEATH),
                    "first": ps.first_deaths,
                    "avoid": ps.count(AVOIDABLE),
                    "prep": ps.count(PREP),
                    "kicks": ps.kicks,
                    "dispels": ps.dispels,
                    "dps": round(ps.dps),
                    "hps": round(ps.hps),
                }
                for ps in s.players
            ]
            columns = [
                {"name": k, "label": label, "field": k, "sortable": True, "align": "left" if k in ("name", "spec") else "right"}
                | ({":format": AMOUNT_JS} if k in ("dps", "hps") else {})
                for k, label in (("name", "Spieler"), ("spec", "Spec"), ("pulls", "Pulls"), ("dps", "Ø DPS"),
                                 ("hps", "Ø HPS"), ("deaths", "Tode"), ("first", "davon erster"),
                                 ("avoid", "Vermeidbarer Schaden"), ("prep", "Vorbereitung"), ("kicks", "Kicks"),
                                 ("dispels", "Dispels"))
            ]
            table = ui.table(rows=rows, columns=columns, row_key="name").props("dense flat").classes("w-full")
            table.classes("cursor-pointer").mark("overview")
            table.on("rowClick", lambda e: select_player(e.args[1]["name"]))

        causes = [c for c in top_causes(s.reports, severities=s.severities) if sum(c[2].values()) >= 2]
        if causes:
            with ui.card().classes("w-full"):
                ui.label("Häufigste Ursachen").classes("text-lg font-bold")
                for cat, subject, who in causes:
                    what = linkify("'" + subject + "'", s.spells, s.lang)
                    kind = "Tode" if cat == DEATH else "× vermeidbarer Schaden"
                    people = html.escape(", ".join(f"{n} {k}×" for n, k in who.most_common()))
                    ui.html(f"<b>{what}</b> – {sum(who.values())} {kind}: <span class='muted'>{people}</span>",
                            sanitize=False)

        with ui.card().classes("w-full"):
            ui.label("Pro Pull").classes("text-lg font-bold")
            for r in s.reports:
                with ui.expansion(f"{pull_label(r.fight)} · {fmt_t(r.fight.duration_s)}",
                                  on_value_change=tooltips).classes("w-full"):
                    for line in pull_lines(r, s.lang):
                        ui.html(line, sanitize=False).classes("text-sm")

    def player_view(ps: PlayerSummary) -> None:
        p = ps.player
        with ui.card().classes("w-full"):
            with ui.row().classes("w-full items-center"):
                with ui.column().classes("gap-0"):
                    ui.label(p.name).classes("text-xl font-bold")
                    ui.label(f"{p.spec or '?'} {p.class_name or '?'} · {ROLES.get(p.role or '', p.role or '?')}"
                             ).classes("muted")
                ui.space()
                ui.button("Raid-Übersicht", icon="groups", on_click=lambda: select_player(None)).props("flat")
            with ui.row().classes("gap-2"):
                for icon, text in (
                    ("replay", f"{ps.pulls} Pulls"),
                    ("bolt", f"Ø {fmt_amount(ps.dps)} DPS"),
                    *([("healing", f"Ø {fmt_amount(ps.hps)} HPS")] if p.role == "healer" else []),
                    ("dangerous", f"{ps.count(DEATH)} Tode"),
                    ("looks_one", f"{ps.first_deaths}× erster Tod"),
                    ("local_fire_department", f"{ps.count(AVOIDABLE)}× vermeidbarer Schaden"),
                    ("science", f"{ps.count(PREP)}× Vorbereitung"),
                    ("front_hand", f"{ps.kicks} Kicks"),
                    *([("cleaning_services", f"{ps.dispels} Dispels")] if ps.dispels else []),
                ):
                    ui.chip(text, icon=icon).props("outline")

        fails = [f for f in ps.fails if f.severity in s.severities]
        with ui.card().classes("w-full").mark("important"):
            ui.label("Wichtigste Punkte").classes("text-lg font-bold")
            if not fails:
                ui.label("Keine Fails in dieser Auswahl 👍").classes("muted")
            for fs in group_fails(fails):
                spells = {k: v for f in fs for k, v in f.spells.items()}
                pulls = ", ".join(f"#{f.fight.id}" for f in fs)
                with ui.column().classes("gap-0 w-full mb-2"):
                    ui.html(f"{badge(max(f.severity for f in fs))} <b>{linkify(group_title(fs), spells, s.lang)}</b>"
                            f" · {len(fs)}× <span class='muted'>({pulls})</span>", sanitize=False)
                    for f in fs:
                        ui.html(fail_html(f), sanitize=False).classes("text-sm pl-6")

        stats_card(ps)
        details_card(p.name)

    def stats_card(ps: PlayerSummary) -> None:
        name = ps.player.name
        mine = [r for r in s.reports if name in r.stats]
        if not mine:
            return
        opts = {"all": f"Alle {len(mine)} Pulls zusammen"} if len(mine) > 1 else {}
        opts |= {str(r.fight.id): f"{pull_label(r.fight)} · {fmt_t(r.fight.duration_s)}" for r in mine}
        pull = s.stat_pull if s.stat_pull in opts else next(iter(opts))
        chosen = [r for r in mine if pull in ("all", str(r.fight.id))]
        st = merge_stats([r.stats[name] for r in chosen])
        ids = {r.fight.id for r in chosen}
        avoidable = {f.subject.lower() for f in ps.fails if f.category == AVOIDABLE and f.fight.id in ids}
        deaths = [(r, d) for r in chosen for d in r.deaths if d.player.name == name]
        counts = {"buffs": len(st.buffs), "debuffs": len(st.debuffs), "kicks": sum(x.count for x in st.kicks),
                  "dispels": sum(x.count for x in st.dispels), "deaths": len(deaths)}

        with ui.card().classes("w-full").mark("stats"):
            with ui.row().classes("w-full items-center no-wrap"):
                with ui.column().classes("gap-0"):
                    ui.label("Statistik").classes("text-lg font-bold")
                    note = f"{fmt_t(st.seconds)} Kampfzeit"
                    cut = [r for r in chosen if r.until < r.fight.end]
                    if len(cut) > 3:
                        note += f" · {len(cut)} Wipes zählen nur bis zum Zusammenbruch"
                    elif cut:
                        note += " · Wipes zählen nur bis zum Zusammenbruch (" + ", ".join(
                            f"#{r.fight.id} ab {fmt_t((r.until - r.fight.start) / 1000)}" for r in cut) + ")"
                    ui.label(note).classes("text-sm muted")
                ui.space()
                ui.select(opts, value=pull, label="Pull", on_change=on_stat_pull).classes("min-w-[18rem]").mark("stat-pull")
            with ui.tabs(value=s.stat_tab, on_change=on_stat_tab).props("dense align=left outside-arrows mobile-arrows"
                                                                         ).classes("w-full") as tabs:
                for key, label in STAT_TABS.items():
                    ui.tab(key, f"{label} ({counts[key]})" if key in counts else label)
            with ui.tab_panels(tabs, value=s.stat_tab, animated=False).classes("w-full"):
                for key in STAT_TABS:
                    with ui.tab_panel(key).classes("px-0 py-2"):
                        if key == "deaths":
                            ui.html(deaths_html(deaths, s.lang), sanitize=False).classes("w-full")
                        elif key in ("damage", "healing") and (st.damage if key == "damage" else st.healing) is None:
                            ui.html(total_line(key, st), sanitize=False).classes("text-sm")
                            with ui.row().classes("items-center gap-2"):
                                if name in s.loading:
                                    ui.spinner()
                                    ui.label("Lade die Aufschlüsselung pro Fähigkeit …").classes("text-sm muted")
                                else:  # loading failed – try again
                                    ui.button("Pro Fähigkeit laden", icon="download",
                                              on_click=lambda: fetch_breakdown(name)).props("flat")
                                    ui.label(f"≈ {BREAKDOWN_POINTS * len(chosen)} API-Punkte").classes("text-sm muted")
                        else:
                            ui.html(stats_html(key, st, s.lang, avoidable), sanitize=False).classes("w-full")

    def details_card(name: str) -> None:
        results = s.details.get((name, s.fight_sel))
        with ui.card().classes("w-full"):
            with ui.row().classes("w-full items-center no-wrap"):
                with ui.column().classes("gap-0"):
                    ui.label("Detail-Analyse gegen Top-Spieler").classes("text-lg font-bold")
                    ui.label(
                        "Rotation, Cooldowns, Buff-Uptime, Gear und Stats im Vergleich mit den Top-5-Logs derselben "
                        "Spec. Braucht beim ersten Mal pro Boss deutlich mehr API-Punkte als die Raid-Auswertung."
                    ).classes("text-sm muted")
                if results is None:
                    ui.space()
                    ui.button("Starten", icon="play_arrow", on_click=lambda: run_details(name)).mark("details")
            if results is None:
                return
            if not results:
                ui.label("Keine auswertbaren Kämpfe für diesen Charakter.").classes("muted")
                return

            infos = [f for f in results[-1][2] if f.severity == INFO]
            if infos:
                ui.label("Überblick (letzter Pull)").classes("font-bold mt-2")
                for f in infos:
                    title = linkify(f.title, f.spells, s.lang)
                    if f.category == "Stats":
                        title = f"<span class='stats'>{title}</span>"
                    ui.html(f"<b>{html.escape(f.category)}:</b> {title}", sanitize=False).classes("text-sm")

            if len(results) > 1:
                groups = []
                for fs in recurring(results):
                    sev = Counter(f.severity for f in fs).most_common(1)[0][0]
                    if sev in s.severities:
                        groups.append((sev, fs))
                if groups:
                    ui.label(f"Wiederkehrend über {len(results)} Pulls").classes("font-bold mt-2")
                    for sev, fs in groups[:12]:
                        f = fs[-1]
                        ui.html(f"{badge(sev)} {len(fs)}/{len(results)}× <b>{html.escape(f.category)}</b>: "
                                f"{linkify(f.title, f.spells, s.lang)}", sanitize=False)

            ui.label("Pro Pull").classes("font-bold mt-2")
            for me, tops, findings in results:
                items = [f for f in findings if f.severity in s.severities]
                with ui.expansion(f"{pull_label(me.fight)} · {len(items)} Punkte", value=len(results) == 1,
                                  on_value_change=tooltips).classes("w-full"):
                    if not tops:
                        ui.label("Kein Top-Vergleich möglich – nur absolute Checks.").classes("text-sm muted")
                    for f in items:
                        finding_block(f)

    @ui.refreshable
    def content() -> None:
        if not s.reports:
            with ui.card().classes("w-full items-center py-10"):
                ui.icon("insights", size="3rem").classes("muted")
                ui.label("Report-URL einfügen → Laden → Pulls wählen → Auswerten").classes("muted")
            return
        ps = next((x for x in s.players if x.player.name == s.selected), None)
        if ps:
            player_view(ps)
        else:
            raid_view()

    # -- layout --------------------------------------------------------------
    with ui.dialog() as settings, ui.card().classes("w-[32rem] max-w-full"):
        ui.label("WCL-API-Zugang").classes("text-lg font-bold")
        ui.markdown(
            "Client anlegen unter <https://www.warcraftlogs.com/api/clients/> (Name egal, Redirect-URL z.B. "
            f"`http://localhost`), dann ID und Secret hier eintragen. Gespeichert wird in `{CONFIG_ENV}`."
        )
        cid_in = ui.input("Client ID", value=os.environ.get("WCL_CLIENT_ID", "")).classes("w-full")
        # the stored secret never goes to the browser
        secret_in = ui.input("Client Secret", password=True, password_toggle_button=True,
                             placeholder="gespeichert – leer lassen zum Behalten"
                             if os.environ.get("WCL_CLIENT_SECRET") else None).classes("w-full")
        with ui.row().classes("w-full justify-end"):
            ui.button("Abbrechen", on_click=settings.close).props("flat")
            ui.button("Speichern", on_click=save_settings)

    with ui.header().classes("items-center gap-3 px-4"):
        ui.label("wcl-coach").classes("text-lg font-bold")
        ui.label("Wer ist woran gestorben, wer hat was gefailt").classes("text-sm opacity-80 max-sm:hidden")
        ui.space()
        quota = ui.label("").classes("text-sm opacity-80")
        ui.toggle({"de": "DE", "en": "EN"}, value=s.lang, on_change=on_lang).props(
            "dense flat toggle-color=white text-color=white"
        ).tooltip("Sprache der Wowhead-Tooltips")
        ui.button(icon="settings", on_click=settings.open).props("flat round color=white").tooltip("WCL-API-Zugang")

    with ui.footer().classes("bg-transparent text-current py-1 px-4") as footer:
        ui.spinner(size="1.2em")
        ui.label().bind_text_from(s, "status").classes("text-sm")
    footer.bind_visibility_from(s, "busy")

    with ui.column().classes("w-full max-w-6xl mx-auto p-4 gap-4"):
        with ui.card().classes("w-full"):
            with ui.row().classes("w-full items-center gap-2 no-wrap"):
                report_in = ui.input("Report-URL oder Code",
                                     placeholder="https://www.warcraftlogs.com/reports/…").classes("grow").mark("report")
                report_in.on("keydown.enter", load)
                ui.button("Laden", icon="download", on_click=load).mark("load")
            with ui.row().classes("w-full items-center gap-2") as fight_row:
                boss_sel = ui.select({}, label="Boss", on_change=on_boss).classes("grow min-w-[16rem]").mark("boss")
                result_sel = ui.toggle({}, on_change=on_result).props("no-caps").mark("result")
                pull_sel = ui.select({}, label="Pull", on_change=show_estimate).classes("grow min-w-[16rem]").mark("pull")
                analyze_btn = ui.button("Auswerten", icon="analytics", on_click=analyze).mark("analyze")
            fight_row.set_visibility(False)
            info = ui.label("").classes("text-sm muted")

        with ui.row().classes("w-full items-center gap-2 no-wrap") as picks:
            char_sel = ui.select({}, label="Charakter", with_input=True, clearable=True,
                                 on_change=on_char).classes("grow").mark("char")
            ui.select(SEVERITIES, value=list(SEVERITIES), multiple=True, label="Priorität",
                      on_change=on_prio).props("use-chips").classes("min-w-[20rem]").mark("prio")
        picks.set_visibility(False)

        content()

    if not (os.environ.get("WCL_CLIENT_ID") and os.environ.get("WCL_CLIENT_SECRET")):
        settings.open()
    else:
        ui.timer(0.1, update_quota, once=True)

    # ?report=…&fight=…&char=… opens a result directly (bookmarks, links in the raid Discord)
    request = getattr(ui.context.client, "request", None)
    params = dict(request.query_params) if request is not None else {}
    if params.get("report"):
        report_in.value = params["report"]

        async def autostart() -> None:
            await load()
            if not s.fights:
                return
            if sel := params.get("fight"):
                set_selection(sel)
            await analyze()
            if params.get("char") in {ps.player.name for ps in s.players}:
                select_player(params["char"])

        ui.timer(0.1, autostart, once=True)


def run_ui(
    site: str = "retail",
    use_cache: bool = True,
    host: str = "127.0.0.1",  # only this machine – the page spends your API budget
    port: int = 8080,
    show: bool = True,
) -> None:
    make_client = client_factory(site, use_cache)
    ui.run(lambda: page(make_client), title="wcl-coach", host=host, port=port, reload=False, dark=None,
           language="de", favicon="⚔️", show=show)
