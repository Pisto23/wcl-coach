"""Command line interface.

Examples:
  wcl-coach analyze https://www.warcraftlogs.com/reports/AbCdEf1234567890?fight=12 -p <charname>
  wcl-coach analyze AbCdEf1234567890 -p <charname> --fight kills --md report.md
  wcl-coach raid AbCdEf1234567890 --md raid.md
  wcl-coach fights AbCdEf1234567890
  wcl-coach ui
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from collections import Counter
from pathlib import Path

from .analysis import SEVERITY_LABEL, Settings
from .api import CONFIG_ENV, SITES, WCLClient, WCLError
from .fetch import Fetcher, parse_report_ref
from .pipeline import player_fights, raid_fights, recurring, select_fights
from .raid import RaidSettings
from .render import md_severity, raid_summary, to_markdown, to_terminal

REPORTS_DIR = Path("reports")
FIGHT_HELP = ("Fight-ID(s) '12' / '3,7,12' / '23-41', Bossname 'sszorak', nur Kills/Wipes 'sszorak:wipes', "
              "'last', 'last-kill', 'kills', 'wipes', 'all'")


def _load_dotenv() -> None:
    for p in (Path.cwd() / ".env", CONFIG_ENV):
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                m = re.match(r"\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.*)\s*$", line)
                if m and m.group(1) not in os.environ:
                    os.environ[m.group(1)] = m.group(2).strip().strip("'\"")


def _client(args) -> WCLClient:
    cid, secret = os.environ.get("WCL_CLIENT_ID"), os.environ.get("WCL_CLIENT_SECRET")
    if not cid or not secret:
        sys.exit(
            "WCL_CLIENT_ID / WCL_CLIENT_SECRET fehlen.\n"
            "Client anlegen: https://www.warcraftlogs.com/api/clients/ und als Env-Variablen "
            "oder in einer .env-Datei hinterlegen."
        )
    return WCLClient(cid, secret, site=args.site, use_cache=not args.no_cache)


def _log(msg: str) -> None:
    print(msg, file=sys.stderr)


def _write_md(path: str, md: str) -> None:
    out = Path(path)
    if out.parent == Path("."):  # bare file name -> reports/ folder
        out = REPORTS_DIR / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    _log(f"Markdown gespeichert: {out}")


# ---------------------------------------------------------------- commands


def cmd_fights(args) -> None:
    code, _, _ = parse_report_ref(args.report)
    rep = Fetcher(_client(args)).report(code)
    print(f"{rep['title']} ({(rep.get('zone') or {}).get('name', '?')})")
    for f in map(Fetcher.to_fight, rep["fights"]):
        res = "KILL" if f.kill else f"wipe {f.boss_pct:.0f}%" if f.boss_pct is not None else "wipe"
        print(f"  #{f.id:<4} {f.name:<35} diff={f.difficulty}  {int(f.duration_s // 60)}:{int(f.duration_s % 60):02d}  {res}")


def cmd_quota(args) -> None:
    rl = _client(args).rate_limit()
    print(f"{rl['pointsSpentThisHour']:.0f} / {rl['limitPerHour']} Punkte, Reset in {rl['pointsResetIn']}s")


def cmd_analyze(args) -> None:
    code, url_fight, url_source = parse_report_ref(args.report)
    player_name = args.player or os.environ.get("WCL_COACH_PLAYER")
    if not player_name and url_source is None:
        sys.exit("Bitte Spieler angeben: -p <Name> (oder ?source=<id> in der URL, oder WCL_COACH_PLAYER).")

    fx = Fetcher(_client(args), log=_log)
    rep = fx.report(code)
    fights = select_fights([Fetcher.to_fight(f) for f in rep["fights"]], args.fight, url_fight)
    settings = Settings(gap_threshold_s=args.gap)

    all_results = []
    for result in player_fights(fx, code, fights, player_name, url_source, args.top, settings, _log):
        all_results.append(result)
        print(to_terminal(*result))
        print("\n" + "─" * 72 + "\n")

    if not all_results:
        sys.exit("Keine auswertbaren Kämpfe gefunden.")

    if len(all_results) > 1:
        print(recurring_summary(all_results))

    if args.md:
        md = "\n\n---\n\n".join(to_markdown(*r) for r in all_results)
        if len(all_results) > 1:
            md = recurring_summary(all_results, markdown=True) + "\n\n---\n\n" + md
        _write_md(args.md, md)


def cmd_raid(args) -> None:
    code, url_fight, _ = parse_report_ref(args.report)
    only = {n.strip().lower() for n in args.player.split(",") if n.strip()} if args.player else None
    settings = RaidSettings(ignore=tuple(x.strip() for x in (args.ignore or "").split(",") if x.strip()))

    fx = Fetcher(_client(args), log=_log)
    rep = fx.report(code)
    sel = args.fight or (None if url_fight else "all")
    fights = select_fights([Fetcher.to_fight(f) for f in rep["fights"]], sel, url_fight)
    reports = list(raid_fights(fx, code, fights, settings, _log))

    if not reports:
        sys.exit("Keine auswertbaren Kämpfe gefunden.")
    if only:
        unknown = only - {p.name.lower() for r in reports for p in r.players}
        if unknown:
            _log(f"Nicht im Report gefunden: {', '.join(sorted(unknown))}")

    print(raid_summary(rep["title"], reports, only))
    if args.md:
        _write_md(args.md, raid_summary(rep["title"], reports, only, markdown=True))


def cmd_ui(args) -> None:
    try:
        from .ui import run_ui
    except ImportError:
        sys.exit("Die UI braucht NiceGUI: pip install -e '.[ui]'")
    run_ui(site=args.site, use_cache=not args.no_cache, host=args.host, port=args.port, show=not args.no_browser)


def recurring_summary(results, markdown: bool = False) -> str:
    """Findings that repeat across pulls – those are your real habits."""
    n = len(results)
    head = f"Wiederkehrende Muster über {n} Kämpfe"
    lines = [f"## {head}" if markdown else head, ""]
    groups = recurring(results)
    if not groups:
        lines.append("Keine Muster, die sich in mehreren Kämpfen wiederholen.")
    for fs in groups[:12]:
        sev = Counter(f.severity for f in fs).most_common(1)[0][0]
        label = md_severity(sev) if markdown else f"[{SEVERITY_LABEL[sev]}]"
        lines.append(f"{'- ' if markdown else '  '}{len(fs)}/{n}× {label} {fs[-1].category}: {fs[-1].title}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    _load_dotenv()
    ap = argparse.ArgumentParser(prog="wcl-coach", description="Warcraft-Logs-Auswertung mit Verbesserungstipps")
    ap.add_argument("--site", choices=sorted(SITES), default=os.environ.get("WCL_SITE", "retail"))
    ap.add_argument("--no-cache", action="store_true", help="API-Cache ignorieren")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("analyze", help="Spieler in einem oder mehreren Kämpfen auswerten")
    a.add_argument("report", help="Report-URL oder 16-stelliger Code")
    a.add_argument("-p", "--player", help="Charaktername")
    a.add_argument("-f", "--fight", help=FIGHT_HELP + " (Standard: last-kill)")
    a.add_argument("--top", type=int, default=5, help="Anzahl Top-Parses zum Vergleich (0 = aus)")
    a.add_argument("--gap", type=float, default=2.5, help="Cast-Pause ab der Downtime zählt (s)")
    a.add_argument("--md", help="Ergebnis zusätzlich als Markdown speichern (reiner Dateiname -> reports/)")
    a.set_defaults(func=cmd_analyze)

    r = sub.add_parser("raid", help="Alle Spieler auswerten: wer ist woran gestorben, wer hat Mechaniken gefailt")
    r.add_argument("report", help="Report-URL oder 16-stelliger Code")
    r.add_argument("-p", "--player", help="Nur diese Spieler anzeigen, kommagetrennt (Vergleich bleibt der ganze Raid)")
    r.add_argument("-f", "--fight", help=FIGHT_HELP + " (Standard: all)")
    r.add_argument("--ignore", help="Fähigkeiten, die nie als vermeidbar zählen (kommagetrennt, Teil des Namens reicht)")
    r.add_argument("--md", help="Ergebnis zusätzlich als Markdown speichern (reiner Dateiname -> reports/)")
    r.set_defaults(func=cmd_raid)

    f = sub.add_parser("fights", help="Boss-Kämpfe eines Reports auflisten")
    f.add_argument("report")
    f.set_defaults(func=cmd_fights)

    q = sub.add_parser("quota", help="Verbleibendes API-Punktebudget anzeigen")
    q.set_defaults(func=cmd_quota)

    u = sub.add_parser("ui", help="Oberfläche im Browser starten")
    u.add_argument("--host", default="127.0.0.1", help="'0.0.0.0' macht die UI im Netzwerk erreichbar (Standard: nur lokal)")
    u.add_argument("--port", type=int, default=8080)
    u.add_argument("--no-browser", action="store_true", help="Nur den Server starten, keinen Browser öffnen")
    u.set_defaults(func=cmd_ui)

    args = ap.parse_args(argv)
    try:
        args.func(args)
    except WCLError as e:
        sys.exit(f"Fehler: {e}")
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
