"""Aggregate tests/production/reports/*.json into STRESS_TEST_REPORT.md."""

from __future__ import annotations

import json
import platform
import sys
from pathlib import Path

REPORTS = Path(__file__).parent / "reports"


def fmt_usage(u: dict) -> str:
    keys = [k for k in u if not isinstance(u[k], (list, dict))]
    return "<br>".join(f"{k}: {u[k]}" for k in keys[:30])


def main(out: Path, env_note: str) -> None:
    recs = []
    for p in sorted(REPORTS.glob("*.json")):
        if p.name.startswith("_"):
            continue
        recs.append(json.loads(p.read_text(encoding="utf-8")))
    recs.sort(key=lambda r: (int(r["number"]) if str(r["number"]).isdigit() else 99, r["id"]))
    lines = ["# Звіт production stress / failure testing", "",
             f"Середовище: {env_note}", "",
             f"Python {platform.python_version()}, {platform.platform()}", "",
             "| № | Тест | Статус | Тривалість |", "|---|---|---|---|"]
    for r in recs:
        lines.append(f"| {r['number']} | {r['title'] or r['id']} | **{r['status']}** | {r.get('duration_s', '')} с |")
    passed = sum(r["status"] == "PASS" for r in recs)
    lines += ["", f"Підсумок: {passed}/{len(recs)} PASS.", ""]
    for r in recs:
        lines += [f"## {r['number']}. {r['title'] or r['id']}", "",
                  f"* **TEST:** `{r['id']}`",
                  f"* **INPUT:** {r['input']}",
                  f"* **EXPECTED:** {r['expected']}",
                  f"* **ACTUAL:** {r['actual']}",
                  f"* **STATUS:** **{r['status']}**",
                  f"* **RESOURCE USAGE:** {fmt_usage(r.get('resource_usage', {})) or '—'}",
                  f"* **FAILURE MODE:** {r['failure_mode'] or '—'}",
                  f"* **FIX:** {r['fix'] or '—'}",
                  f"* **Залишкові процеси після тесту:** {r.get('orphans_after') or 'немає'}"]
        for n in r.get("notes", []):
            lines.append(f"* {n}")
        if r.get("failure_text"):
            lines += ["", "```", r["failure_text"][-1500:], "```"]
        lines.append("")
    out.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main(Path(sys.argv[1]), sys.argv[2] if len(sys.argv) > 2 else "")
