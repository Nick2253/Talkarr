"""CLI: ``python -m talkarr.eval {sweep,perturb} [--root /media] [--limit N]``."""

import argparse
import json
import os
import statistics
import sys
import time
from collections import Counter
from types import SimpleNamespace

from ..forced import subtitles as S
from ..forced.audio import fit_timeline, foreign_timeline, regions_from_response
from ..forced.verify import Verdict, verify, verify_audio
from .audioeval import film_regions
from .library import find_items
from .perturb import cases, repair_error
from .refsync import synced_reference

# Films whose forced sub agrees this well with the full sub become perturbation corpus.
CORPUS_MIN_INLIERS = 5
CORPUS_MIN_COVERAGE = 0.8
# A retime repair is a pass if every recoverable cue lands within this many seconds.
REPAIR_TOLERANCE = 0.1


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m talkarr.eval")
    parser.add_argument("command", choices=["sweep", "perturb", "audio"])
    parser.add_argument("--root", default=os.environ.get("MEDIA_PATH", "/media") + "/movies")
    parser.add_argument("--limit", type=int, default=0, help="stop after N films")
    parser.add_argument("--title", action="append", default=[], help="only titles containing")
    parser.add_argument(
        "--out",
        default=os.environ.get("TALKARR_LAB_DIR", ""),
        help="directory for the JSON result (default: $TALKARR_LAB_DIR)",
    )
    parser.add_argument(
        "--sync-reference",
        action="store_true",
        help="align each full subtitle to its video with ffsubsync before judging "
        "(cached under <out>/cache/refsync); only for films the text path can anchor",
    )
    parser.add_argument(
        "--ml-url",
        default=os.environ.get("TALKARR_ML_URL", "https://talkarr-ml.nkcx.net"),
        help="talkarr-ml base URL (audio command)",
    )
    parser.add_argument(
        "--regions",
        help="audio command, offline: read cached analyses from DIR/<title>/"
        "{regions.json,forced.srt} instead of analysing films (for tuning)",
    )
    args = parser.parse_args(argv)

    started = time.monotonic()
    items = [] if args.regions else find_items(args.root)
    if args.title:
        items = [i for i in items if any(t.lower() in i.title.lower() for t in args.title)]
    print(
        f"{len(items)} English forced subtitles under {args.root} "
        f"({sum(1 for i in items if i.reference)} with a full English subtitle)\n"
    )

    cache = os.path.join(args.out or "/tmp/talkarr-eval", "cache", "refsync")
    sweep_rows = run_sweep(
        items,
        limit=args.limit if args.command == "sweep" else 0,
        sync_cache=cache if args.sync_reference else None,
    )
    if args.command == "sweep":
        result = {"command": "sweep", "rows": sweep_rows}
        print_sweep(sweep_rows)
    elif args.command == "perturb":
        result = {"command": "perturb", "rows": run_perturb(items, sweep_rows, args.limit)}
        print_perturb(result["rows"])
    else:
        if args.regions:
            rows, films_info = audio_cases(load_offline(args.regions))
        else:
            audio_cache = os.path.join(args.out or "/tmp/talkarr-eval", "cache", "audio")
            films = corpus(items, sweep_rows) if not args.title else items
            rows, films_info = run_audio(films, audio_cache, args.ml_url, args.limit)
        result = {"command": "audio", "rows": rows, "films": films_info}
        print_audio(films_info)
        print_perturb(rows)

    result["seconds"] = round(time.monotonic() - started, 1)
    print(f"\n({result['seconds']} s)")
    if args.out:
        path = save(result, args.out)
        print(f"results: {path}")
    return 0


# --- sweep -------------------------------------------------------------------------


# Verdicts that depend on the reference's timing, so are re-judged after syncing it.
TIMING_VERDICTS = {Verdict.IN_SYNC, Verdict.RETIMED, Verdict.MISMATCH}


def run_sweep(items, limit=0, sync_cache=None):
    rows = []
    for item in items[: limit or None]:
        row = {"title": item.title, "forced": item.forced, "reference": item.reference}
        try:
            forced = S.load(item.forced)
            reference = S.load(item.reference) if item.reference else None
            report = verify(forced, reference)
            if sync_cache and report.verdict in TIMING_VERDICTS:
                synced, info = synced_reference(item, sync_cache)
                row["reference_sync"] = info
                row["unsynced_verdict"] = str(report.verdict)
                if synced is not None:
                    report = verify(forced, synced)
            row.update(report.as_dict())
        except Exception as e:  # noqa: BLE001 - one bad file must not stop a sweep
            row.update(verdict="error", reason=f"{type(e).__name__}: {e}")
        rows.append(row)
        print(f"  {row['verdict']:12s} {item.title}", flush=True)
    return rows


def print_sweep(rows):
    counts = Counter(r["verdict"] for r in rows)
    print("| verdict | films |\n|---|---|")
    for verdict, n in counts.most_common():
        print(f"| {verdict} | {n} |")
    for verdict in ("retimed", "mismatch", "in_sync", "unverifiable", "error"):
        selected = [r for r in rows if r["verdict"] == verdict]
        if not selected:
            continue
        print(f"\n### {verdict}\n")
        for r in selected:
            flag = " **[review]**" if r.get("review") else ""
            sync = r.get("reference_sync")
            note = ""
            if sync:
                note = (
                    f" (full sub ffsubsync offset {sync['offset']:+.2f}s)"
                    if sync.get("ok") and sync.get("offset") is not None
                    else f" (full sub sync failed: {sync.get('error')})"
                )
            print(f"- {r['title']}: {r['reason']}{flag}{note}")


# --- perturb -----------------------------------------------------------------------


def corpus(items, sweep_rows):
    by_forced = {r["forced"]: r for r in sweep_rows}
    good = []
    for item in items:
        row = by_forced.get(item.forced, {})
        if (
            row.get("verdict") == Verdict.IN_SYNC
            and row.get("inliers", 0) >= CORPUS_MIN_INLIERS
            and row.get("coverage", 0) >= CORPUS_MIN_COVERAGE
        ):
            good.append(item)
    return good


def run_perturb(items, sweep_rows, limit=0):
    films = corpus(items, sweep_rows)[: limit or None]
    print(f"perturbation corpus: {len(films)} films whose forced sub matches their full sub\n")
    loaded = [(f, S.load(f.forced), S.load(f.reference)) for f in films]
    rows = []
    for n, (film, forced, reference) in enumerate(loaded):
        other = loaded[(n + 1) % len(loaded)][1] if len(loaded) > 1 else None
        for case in cases(forced, reference, other_forced=other, seed=n):
            report = verify(case.cues, reference)
            row = {
                "title": film.title,
                "case": case.name,
                "expected": str(case.expected),
                "verdict": str(report.verdict),
                "reason": report.reason,
            }
            ok = report.verdict == case.expected or report.verdict in case.also_ok
            if ok and case.expected == Verdict.RETIMED:
                errors = repair_error(case, report, forced)
                row["max_error"] = round(max(errors), 3) if errors else None
                row["median_error"] = round(statistics.median(errors), 3) if errors else None
                ok = bool(errors) and max(errors) <= REPAIR_TOLERANCE
            row["pass"] = ok
            rows.append(row)
    return rows


def print_perturb(rows):
    by_case = {}
    for r in rows:
        by_case.setdefault(r["case"], []).append(r)
    print("| case | pass |\n|---|---|")
    for name, group in by_case.items():
        passed = sum(r["pass"] for r in group)
        print(f"| {name} | {passed}/{len(group)} |")
    failures = [r for r in rows if not r["pass"]]
    total = len(rows)
    harmful = [r for r in failures if is_harmful(r)]
    print(f"\n**{total - len(failures)}/{total} passed; {len(harmful)} harmful**")
    print(
        "(harmful = a wrong retime, or accepting a subtitle that doesn't belong; other "
        "failures are conservative: left unchanged or reported as unverifiable)"
    )
    if failures:
        print("\n### failures\n")
        for r in failures:
            err = f" (max error {r['max_error']} s)" if r.get("max_error") is not None else ""
            print(
                f"- {r['title']} / {r['case']}: expected {r['expected']}, "
                f"got {r['verdict']}{err}: {r['reason']}"
            )


# --- audio -------------------------------------------------------------------------


def run_audio(films, cache_dir, ml_url, limit=0):
    """Perturbation suite judged by the audio path alone (no full subtitle used)."""
    films = films[: limit or None]
    print(f"audio corpus: {len(films)} films\n")
    sources = []
    for film in films:
        try:
            regions, info = film_regions(film, cache_dir, ml_url)
        except Exception as e:  # noqa: BLE001 - one film must not stop the run
            regions, info = None, {"ok": False, "error": f"{type(e).__name__}: {e}"}
        sources.append((film.title, film.forced, regions, info))
    return audio_cases(sources)


def load_offline(directory):
    """Cached analyses exported from the lab: ``DIR/<title>/{regions.json,forced.srt}``."""
    sources = []
    for title in sorted(os.listdir(directory)):
        with open(os.path.join(directory, title, "regions.json")) as f:
            data = json.load(f)
        info = {"ok": True, "stream": data.get("stream", {}), "regions": len(data["regions"])}
        sources.append(
            (title, os.path.join(directory, title, "forced.srt"), regions_from_response(data), info)
        )
    return sources


def audio_cases(sources):
    loaded = []
    films_info = []
    for title, forced_path, regions, info in sources:
        info["title"] = title
        if regions is not None:
            timeline = foreign_timeline(regions)
            info["foreign_spans"] = len(timeline.foreign)
            info["foreign_seconds"] = round(timeline.foreign_seconds, 1)
            loaded.append((SimpleNamespace(title=title), S.load(forced_path), timeline))
        films_info.append(info)
        print(f"  analysed {title}: {info}", flush=True)

    rows = []
    for n, (film, forced, timeline) in enumerate(loaded):
        other = loaded[(n + 1) % len(loaded)][1] if len(loaded) > 1 else None
        for case in cases(forced, forced, other_forced=other, seed=n):
            if case.name == "full subtitle labelled forced":
                continue  # no full subtitle on this path
            fit = fit_timeline(case.cues, timeline.foreign)
            report = verify_audio(case.cues, fit, timeline.foreign)
            row = {
                "title": film.title,
                "case": case.name,
                "expected": str(case.expected),
                "verdict": str(report.verdict),
                "reason": report.reason,
            }
            ok = report.verdict == case.expected or report.verdict in case.also_ok
            if ok and case.expected == Verdict.RETIMED:
                errors = repair_error(case, report, forced)
                row["max_error"] = round(max(errors), 3) if errors else None
                row["median_error"] = round(statistics.median(errors), 3) if errors else None
                # Audio anchors are VAD edges, not subtitle times: judge the median.
                ok = bool(errors) and statistics.median(errors) <= AUDIO_REPAIR_TOLERANCE
            row["pass"] = ok
            rows.append(row)
    return rows, films_info


AUDIO_REPAIR_TOLERANCE = 0.5


def print_audio(films_info):
    print("| film | stream | foreign spans | foreign s |\n|---|---|---|---|")
    for f in films_info:
        if not f.get("ok"):
            print(f"| {f['title']} | error: {f.get('error')} | | |")
            continue
        st = f["stream"]
        print(
            f"| {f['title']} | #{st['index']} {st['layout']}{' FC' if st['center'] else ''} "
            f"| {f.get('foreign_spans')} | {f.get('foreign_seconds')} |"
        )
    print()


def is_harmful(row):
    """A failure that would damage or wrongly bless a subtitle, as opposed to a
    conservative one (no change, or "can't tell")."""
    if row["verdict"] == Verdict.RETIMED:
        return True  # retimed when it shouldn't be, or to the wrong timing
    return row["verdict"] == Verdict.IN_SYNC and row["expected"] == Verdict.MISMATCH


def save(result, out_dir):
    runs = os.path.join(out_dir, "runs")
    os.makedirs(runs, exist_ok=True)
    path = os.path.join(runs, f"{time.strftime('%Y%m%d-%H%M%S')}-{result['command']}.json")
    with open(path, "w") as f:
        json.dump(result, f, indent=1)
    return path


if __name__ == "__main__":
    sys.exit(main())
