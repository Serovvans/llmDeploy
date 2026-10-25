"""Shares of the control set and the «before → after» comparison of two runs.

Input — JSON files written by ``portal eval-code --out`` (docs/portal-api.md §13.4); only
``tasks[].id``, ``tasks[].group`` and ``tasks[].result`` are read. Usage::

    python compare.py before.json after.json

Exit code 1 if some group got worse: its share of «not built» grew, or it stayed the same
and the share of «built at once» fell.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

RESULTS = ("first_try", "after_fix", "failed")
TOTAL = "всего"


@dataclass(frozen=True)
class Shares:
    """Task counts of one group by result."""

    first_try: int
    after_fix: int
    failed: int

    @property
    def total(self) -> int:
        """Number of tasks in the group."""
        return self.first_try + self.after_fix + self.failed

    def share(self, result: str) -> float:
        """Share of tasks with ``result``; 0 for an empty group."""
        return getattr(self, result) / self.total if self.total else 0.0


def shares(tasks: Iterable[Mapping[str, str]]) -> dict[str, Shares]:
    """Count results by group, plus the ``всего`` row over all tasks."""
    counts: dict[str, dict[str, int]] = {}
    for task in tasks:
        result = task["result"]
        if result not in RESULTS:
            raise ValueError(f"задача {task['id']}: неизвестный итог {result!r}")
        for group in (task["group"], TOTAL):
            row = counts.setdefault(group, dict.fromkeys(RESULTS, 0))
            row[result] += 1
    return {group: Shares(**row) for group, row in counts.items()}


def worse(before: Shares, after: Shares) -> bool:
    """Whether ``after`` is worse than ``before`` for the same group."""
    if after.share("failed") != before.share("failed"):
        return after.share("failed") > before.share("failed")
    return after.share("first_try") < before.share("first_try")


def compare(
    before: Sequence[Mapping[str, str]], after: Sequence[Mapping[str, str]]
) -> tuple[list[str], bool]:
    """Report lines «before → after» by group and changed tasks; ``True`` if a group got worse."""
    old, new = shares(before), shares(after)
    lines: list[str] = []
    regressed = False
    for group in sorted(old.keys() | new.keys(), key=lambda g: (g == TOTAL, g)):
        a, b = old.get(group, Shares(0, 0, 0)), new.get(group, Shares(0, 0, 0))
        mark = ""
        if group in old and group in new and worse(a, b):
            mark, regressed = "  ХУЖЕ", True
        cells = "  ".join(f"{r} {a.share(r):.0%} → {b.share(r):.0%}" for r in RESULTS)
        lines.append(f"{group:<12} ({a.total} → {b.total})  {cells}{mark}")
    rank = {r: i for i, r in enumerate(RESULTS)}
    was = {t["id"]: t["result"] for t in before}
    for task in after:
        prev = was.get(task["id"])
        if prev is not None and prev != task["result"]:
            sign = "хуже" if rank[task["result"]] > rank[prev] else "лучше"
            lines.append(f"  {task['id']}: {prev} → {task['result']} ({sign})")
    return lines, regressed


def _load(path: Path) -> list[dict[str, str]]:
    return list(json.loads(path.read_text(encoding="utf-8"))["tasks"])


def main(argv: Sequence[str]) -> int:
    """Print the comparison of two runs; exit code 1 on a group regression."""
    if len(argv) != 2:
        sys.stderr.write("usage: python compare.py before.json after.json\n")
        return 2
    lines, regressed = compare(_load(Path(argv[0])), _load(Path(argv[1])))
    sys.stdout.write("\n".join(lines) + "\n")
    return 1 if regressed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
