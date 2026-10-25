"""Compatibility report plugin (tests/compat/REPORT.md), wired in by tests/conftest.py.

Every test under tests/compat/ is one feature: its docstring is the feature name, an xfail marker's
reason is the gap description. Outcomes of the whole run (regression suite + compatibility suite)
are collected and rendered at session end.
"""
import datetime
import os
import platform
import sqlite3

REPORT = os.path.join(os.path.dirname(__file__), "compat", "REPORT.md")

features = {}   # nodeid -> {"suite", "feature", "gap"}
outcomes = {}   # nodeid -> "verified" | "gap" | "regression" | "fixed" | "not run"
versions = {"mysql": None}
suite = {"passed": 0, "failed": 0, "skipped": 0, "xfailed": 0}  # every test in the run

STATUS = {"verified": "✅", "gap": "❌ gap", "regression": "🔴 regression", "fixed": "🟡 fixed",
          "unsupported": "⛔", "quirk": "⚪ quirk", "not run": "⏭"}
LEGEND = ("✅ verified · ❌ gap — known difference, the note names the task that closes it · "
          "⚪ engine quirk — documented, deliberately not aligned · ⛔ unsupported by design, refused with "
          "NotSupportedError · 🟡 fixed — xfail marker to remove · 🔴 regression · ⏭ not run")


def pytest_configure(config):
    config.addinivalue_line("markers", "unsupported(reason): the feature is rejected on purpose "
                            "(NotSupportedError); the test passes when s3rds refuses it cleanly")
    config.addinivalue_line("markers", "quirk: together with xfail -- an engine-level difference that is "
                            "documented and deliberately not aligned (not a gap to fix)")


def pytest_collection_modifyitems(items):
    for item in items:
        nodeid = item.nodeid.replace(os.sep, "/")
        if "tests/compat/" not in nodeid:
            continue
        kind = "mysql" if "test_mysql" in nodeid else "sqlite" if "test_sqlite" in nodeid else None
        if not kind:
            continue
        xfail = item.get_closest_marker("xfail")
        unsupported = item.get_closest_marker("unsupported")
        features[item.nodeid] = {
            "suite": kind,
            "feature": (item.function.__doc__ or item.name).strip().splitlines()[0],
            "gap": xfail.kwargs.get("reason") if xfail else
                   (unsupported.kwargs.get("reason") or (unsupported.args[0] if unsupported.args else "")) if unsupported else None,
            "unsupported": bool(unsupported),
            "quirk": item.get_closest_marker("quirk") is not None,
        }


def pytest_runtest_logreport(report):
    if report.when == "setup" and report.outcome == "skipped":
        suite["skipped"] += 1
    elif report.when == "call":
        if hasattr(report, "wasxfail") and report.outcome == "skipped":
            suite["xfailed"] += 1
        elif report.outcome in ("passed", "failed"):
            suite[report.outcome] += 1
    if report.nodeid not in features:
        return
    if report.when == "setup" and report.outcome == "skipped":
        # xfail(run=False) is reported as skipped at setup: still a known gap, not "not run"
        outcomes[report.nodeid] = "gap" if hasattr(report, "wasxfail") else "not run"
    elif report.when == "call":
        xfail = hasattr(report, "wasxfail")
        if report.outcome == "failed":   # keep the real-vs-s3rds lines so the report shows the actual difference
            text = report.longreprtext
            lines = [ln.strip() for ln in text.splitlines() if ln.strip().startswith(("real:", "s3rds:", "E   "))]
            features[report.nodeid]["fail"] = " ".join(lines)[:800] or text.strip().splitlines()[-1][:800]
        outcomes[report.nodeid] = (
            "quirk" if report.outcome == "skipped" and xfail and features[report.nodeid]["quirk"] else
            "gap" if report.outcome == "skipped" and xfail else        # xfailed
            "fixed" if report.outcome == "passed" and xfail else       # xpassed: remove the marker
            "unsupported" if report.outcome == "passed" and features[report.nodeid]["unsupported"] else
            "verified" if report.outcome == "passed" else "regression")


def _counts(kind):
    c = {}
    for nid, f in features.items():
        if f["suite"] == kind:
            o = outcomes.get(nid, "not run")
            c[o] = c.get(o, 0) + 1
    total = sum(c.values())
    supported = c.get("verified", 0) + c.get("quirk", 0) + c.get("fixed", 0)
    pct = f"**Support {100 * supported // total}%** ({supported} / {total} features) · " if total else ""
    return pct + " · ".join(f"{n} {k}" for k, n in sorted(c.items()))


def render():
    import s3rds
    try:
        import sqlglot
        sg = sqlglot.__version__
    except ImportError:
        sg = "not installed"
    lines = [
        "# s3rds compatibility report · [tasks](TASKS.md)", "",
        f"Generated {datetime.date.today()} by `pytest tests` · s3rds {s3rds.__version__} · "
        f"sqlglot {sg} · Python {platform.python_version()}", "",
        "Each row is one feature exercised with the same SQL on the reference database and on s3rds; "
        "results, Python types, rowcount, lastrowid and error codes must match. The feature list only grows "
        "(a removed case would show in git), so the support percentage can only move up.", "",
        f"Whole test run: **{suite['passed']} passed**, {suite['failed']} failed, "
        f"{suite['xfailed']} expected failures (known gaps), {suite['skipped']} skipped "
        "— regression suite (`tests/`) and compatibility suite (`tests/compat/`) together.", "",
    ]
    for kind, title, ref in [("mysql", "s3rds.pymysql vs MySQL", versions["mysql"] or "MySQL (not run)"),
                             ("sqlite", "s3rds.sqlite vs sqlite3", f"SQLite {sqlite3.sqlite_version} (standard library)")]:
        rows = [(nid, f) for nid, f in features.items() if f["suite"] == kind]
        lines += [f"## {title} — reference: {ref}", "", _counts(kind), "", LEGEND, ""]
        # docstring "Category | Feature | short note": the category becomes a bold separator row
        order = {"verified": 0, "fixed": 1, "regression": 2, "gap": 3, "quirk": 4, "unsupported": 5, "not run": 6}
        groups = {}
        for nid, f in rows:
            parts = [p.strip() for p in f["feature"].split(" | ")]
            cat, feat, note = (parts + ["", ""])[:3] if len(parts) >= 2 else ("General", parts[0], "")
            if f.get("fail"):
                note = f["fail"].replace("|", "\\|")
            groups.setdefault(cat, []).append((nid, feat, note))
        lines += ["| Feature | Status | Note |", "|---|---|---|"]
        for cat in sorted(groups, key=lambda c: (CATEGORY_ORDER.index(c) if c in CATEGORY_ORDER else 99, c)):
            lines.append(f"| **{cat}** | | |")
            for nid, feat, note in sorted(groups[cat], key=lambda x: (order[outcomes.get(x[0], "not run")], x[1])):
                lines.append(f"| {feat} | {STATUS[outcomes.get(nid, 'not run')]} | {note} |")
        lines.append("")
    return "\n".join(lines)


CATEGORY_ORDER = ["DDL", "DML", "Queries", "Functions", "Constraints & errors", "Metadata", "Types", "Unsupported", "General"]


def _ran():
    return any(outcomes.get(n) not in (None, "not run") for n in features)


def pytest_sessionfinish(session):
    if _ran():
        with open(REPORT, "w", encoding="utf-8") as fh:
            fh.write(render())


def pytest_terminal_summary(terminalreporter):
    if not _ran():
        return
    terminalreporter.section("s3rds compatibility")
    for kind in ("mysql", "sqlite"):
        terminalreporter.write_line(f"{kind:7s} {_counts(kind)}")
    terminalreporter.write_line(f"report: {os.path.relpath(REPORT)}")
