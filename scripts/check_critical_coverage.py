"""Enforce per-directory coverage thresholds from a coverage.xml report.

Parses the coverage.xml produced by pytest-cov and requires ≥ 90% line coverage
on every directory listed in CRITICAL_DIRS. Treats a directory with zero
measurable lines as passing (the module is just a package marker).

Exit code 0 on success, 1 when any critical directory falls below threshold.
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

CRITICAL_DIRS: tuple[str, ...] = (
    "src/tessera/adapters",
    "src/tessera/auth",
    "src/tessera/migration",
    "src/tessera/retrieval",
    "src/tessera/vault",
)
THRESHOLD: float = 90.0


@dataclass(frozen=True)
class DirCoverage:
    directory: str
    covered: int
    total: int

    @property
    def percent(self) -> float:
        if self.total == 0:
            return 100.0
        return 100.0 * self.covered / self.total


def load_coverage(report_path: Path) -> list[DirCoverage]:
    tree = ET.parse(report_path)
    root = tree.getroot()
    totals: dict[str, list[int]] = {d: [0, 0] for d in CRITICAL_DIRS}
    for cls in root.iter("class"):
        filename = cls.attrib.get("filename", "")
        for directory in CRITICAL_DIRS:
            if filename.startswith(f"{directory}/") or filename == directory:
                lines = cls.find("lines")
                if lines is None:
                    continue
                for line in lines.iter("line"):
                    hits = int(line.attrib.get("hits", "0"))
                    totals[directory][1] += 1
                    if hits > 0:
                        totals[directory][0] += 1
                break
    return [DirCoverage(d, covered=c[0], total=c[1]) for d, c in totals.items()]


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_critical_coverage.py <coverage.xml>", file=sys.stderr)
        return 2
    report = Path(argv[1])
    if not report.is_file():
        print(f"coverage report not found: {report}", file=sys.stderr)
        return 2

    failed = False
    for entry in load_coverage(report):
        status = "ok" if entry.percent >= THRESHOLD else "FAIL"
        print(
            f"{entry.directory}: {entry.percent:6.2f}%  ({entry.covered}/{entry.total})  [{status}]"
        )
        if entry.percent < THRESHOLD:
            failed = True

    if failed:
        print(
            f"\ncritical-directory coverage below {THRESHOLD:.0f}% threshold",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
