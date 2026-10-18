"""Command line interface.

Examples:
  wcl-coach analyze https://www.warcraftlogs.com/reports/AbCdEf1234567890?fight=12 -p <charname>
  wcl-coach analyze AbCdEf1234567890 -p <charname> --fight kills --md report.md
  wcl-coach fights AbCdEf1234567890
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

from .analysis import INFO, SEVERITY_LABEL, Settings, run_all
from .api import SITES, WCLClient, WCLError
from .fetch import Fetcher, Fight, parse_report_ref
from .render import md_severity, to_markdown, to_terminal

REPORTS_DIR = Path("reports")


def _load_dotenv() -> None:
    for p in (Path.cwd() / ".env", Path.home() / ".config" / "wcl-coach" / ".env"):
        if p.exists():
            for line in p.read_text().splitlines():
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


def _select_fights(fights: list[Fight], sel: str | None, url_fight: int | None) -> list[Fight]:
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
    ids: set[int] = set()
    names: list[str] = []
    for part in (x.strip() for x in sel.split(",")):
        if m := re.fullmatch(r"(\d+)\s*-\s*(\d+)", part):
            ids.update(range(int(m[1]), int(m[2]) + 1))
        elif part.isdigit():
            ids.add(int(part))
        elif part:
            names.append(part.lower())
    chosen = [f for f in fights if f.id in ids or any(n in f.name.lower() for n in names)]
    if not chosen:
        raise WCLError(f"Fight(s) '{sel}' nicht gefunden. 'wcl-coach fights <report>' zeigt alle.")
    return chosen


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
    fights = _select_fights([Fetcher.to_fight(f) for f in rep["fights"]], args.fight, url_fight)
    settings = Settings(gap_threshold_s=args.gap)

    all_results = []
    for fight in fights:
        _log(f"→ {fight.name} (#{fight.id})")
        details, r_dps, r_hps = fx.player_details(code, fight.id)
        try:
            player = fx.resolve_player(details, player_name, url_source)
        except WCLError as e:
            _log(f"  übersprungen: {e}")
            continue
        me = fx.player_fight(code, fight, player)
        rank, amount = fx.rank_for(r_hps if player.role == "healer" else r_dps, player)
        me.rank_percent, me.amount_per_s = rank, amount
        tops = fx.top_players(me, n=args.top) if args.top > 0 else []
        findings = run_all(me, tops, settings)
        all_results.append((me, tops, findings))

        print(to_terminal(me, tops, findings))
        print("\n" + "─" * 72 + "\n")

    if not all_results:
        sys.exit("Keine auswertbaren Kämpfe gefunden.")

    if len(all_results) > 1:
        print(recurring_summary(all_results))

    if args.md:
        md = "\n\n---\n\n".join(to_markdown(*r) for r in all_results)
        if len(all_results) > 1:
            md = recurring_summary(all_results, markdown=True) + "\n\n---\n\n" + md
        out = Path(args.md)
        if out.parent == Path("."):  # bare file name -> reports/ folder
            out = REPORTS_DIR / out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(md, encoding="utf-8")
        _log(f"Markdown gespeichert: {out}")


def recurring_summary(results, markdown: bool = False) -> str:
    """Groups findings that repeat across pulls – those are your real habits."""
    groups: dict[str, list] = defaultdict(list)
    for _, _, findings in results:
        for f in findings:
            if f.severity == INFO:
                continue
            key = f"{f.category}|" + re.sub(r"[\d.,:]+", "#", f.title)
            groups[key].append(f)
    n = len(results)
    recurring = sorted(
        ((k, fs) for k, fs in groups.items() if len(fs) >= 2),
        key=lambda kv: (-len(kv[1]), -max(f.severity for f in kv[1])),
    )
    head = f"Wiederkehrende Muster über {n} Kämpfe"
    lines = [f"## {head}" if markdown else head, ""]
    if not recurring:
        lines.append("Keine Muster, die sich in mehreren Kämpfen wiederholen.")
    for k, fs in recurring[:12]:
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
    a.add_argument(
        "-f", "--fight",
        help="Fight-ID(s) '12' / '3,7,12' / '23-41', Bossname 'sszorak', "
        "'last', 'last-kill' (Standard), 'kills', 'all'",
    )
    a.add_argument("--top", type=int, default=5, help="Anzahl Top-Parses zum Vergleich (0 = aus)")
    a.add_argument("--gap", type=float, default=2.5, help="Cast-Pause ab der Downtime zählt (s)")
    a.add_argument("--md", help="Ergebnis zusätzlich als Markdown speichern (reiner Dateiname -> reports/)")
    a.set_defaults(func=cmd_analyze)

    f = sub.add_parser("fights", help="Boss-Kämpfe eines Reports auflisten")
    f.add_argument("report")
    f.set_defaults(func=cmd_fights)

    q = sub.add_parser("quota", help="Verbleibendes API-Punktebudget anzeigen")
    q.set_defaults(func=cmd_quota)

    args = ap.parse_args(argv)
    try:
        args.func(args)
    except WCLError as e:
        sys.exit(f"Fehler: {e}")
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
