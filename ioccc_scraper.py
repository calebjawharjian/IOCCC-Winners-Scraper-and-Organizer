#!/usr/bin/env python3
"""
IOCCC Winners Scraper / Organizer
---------------------------------

What it does
============
- Clones (or reuses) a local clone of the IOCCC winners GitHub repository.
- Finds ALL .c source files across winners.
- Organizes outputs by: year / award-or-ranking / entry-name
- Copies only .c files (ignores README.md and other non-C artifacts).
- Writes a small LLM-friendly descriptor.json for each entry with metadata:
  { year, entry, award, authors, original_path, source_files, summary }
- Generates a top-level manifest.csv for quick auditing.

Assumptions
===========
- The IOCCC winners repo typically has a directory layout like:
    winners/<YEAR>/<ENTRY>/... (varies a bit across years)
- We try to infer year and entry from path segments.
- "Award" / "Ranking" is heuristic: we try to extract it from common files
  (README*, index.html, remarks, .txt, Makefile) or from C header comments.
  If we can't find it, we set it to "unknown".

Usage
=====
    python ioccc_scraper.py --repo-url https://github.com/ioccc-src/winner \
                            --workdir ./work \
                            --outdir ./iocc_out

If you already have a local clone:
    python ioccc_scraper.py --local-clone /path/to/local/clone --outdir ./iocc_out

Options
=======
- --repo-url       : Git URL to clone (default guesses a common IOCCC winners repo)
- --branch         : Git branch to checkout (defaults to repo default)
- --workdir        : Where to place (or find) the clone
- --local-clone    : Use an existing local clone instead of cloning
- --outdir         : Where organized outputs will be written
- --force          : Overwrite outdir if it exists
"""

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple


DEFAULT_REPO_URL = "https://github.com/ioccc-src/winner.git"


def run(cmd: List[str], cwd: Optional[Path] = None) -> Tuple[int, str, str]:
    proc = subprocess.Popen(cmd, cwd=str(cwd) if cwd else None,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    out, err = proc.communicate()
    return proc.returncode, out, err


def ensure_clone(repo_url: str, workdir: Path, local_clone: Optional[Path], branch: Optional[str]) -> Path:
    if local_clone:
        repo_path = local_clone.resolve()
        if not repo_path.exists():
            raise FileNotFoundError(f"--local-clone does not exist: {repo_path}")
        return repo_path

    workdir.mkdir(parents=True, exist_ok=True)
    repo_name = Path(repo_url.rstrip("/").split("/")[-1])
    if not str(repo_name).endswith(".git"):
        repo_name = Path(str(repo_name) + ".git")
    clone_dir = workdir / repo_name.stem

    if clone_dir.exists():
        # Already cloned, pull latest
        code, out, err = run(["git", "fetch", "--all"], cwd=clone_dir)
        if code != 0:
            print("Warning: git fetch failed, continuing with existing clone.")
        if branch:
            code, out, err = run(["git", "checkout", branch], cwd=clone_dir)
            if code != 0:
                print(f"Warning: git checkout {branch} failed: {err.strip()}")
        code, out, err = run(["git", "pull", "--ff-only"], cwd=clone_dir)
        if code != 0:
            print("Warning: git pull failed, continuing with existing clone.")
    else:
        code, out, err = run(["git", "clone", repo_url, str(clone_dir)])
        if code != 0:
            raise RuntimeError(f"git clone failed: {err.strip()}")
        if branch:
            code, out, err = run(["git", "checkout", branch], cwd=clone_dir)
            if code != 0:
                print(f"Warning: git checkout {branch} failed: {err.strip()}")

    return clone_dir


AWARD_HINTS = [
    r"(?i)\baward\b\s*:\s*(.+)",
    r"(?i)\bcategory\b\s*:\s*(.+)",
    r"(?i)\branking\b\s*:\s*(.+)",
    r"(?i)\bprize\b\s*:\s*(.+)",
    r"(?i)\b(honou?rable mention|honorable mention)\b",
    r"(?i)\b(best|most|worst)\b[^\n]+",  # e.g., "Most over-engineered"
]

AUTHOR_HINTS = [
    r"(?i)\bauthor[s]?\b\s*:\s*(.+)",
    r"(?i)\bby\s+([^\n]+)",
]

SUMMARY_HINTS = [
    r"(?i)\bsummary\b\s*:\s*(.+)",
    r"(?i)\bdescription\b\s*:\s*(.+)",
    r"(?i)\bwhat it does\b\s*:\s*(.+)",
]


TEXTY_FILES = [
    "README", "README.txt", "README.md", "readme", "readme.txt", "readme.md",
    "index.html", "index.htm", "remarks", "remarks.txt", "overview", "overview.txt",
    "ABOUT", "about.txt", "Makefile", "makefile", "info", "info.txt",
]


def read_text_safely(p: Path, max_bytes: int = 512_000) -> str:
    try:
        data = p.read_bytes()[:max_bytes]
        return data.decode("utf-8", errors="ignore")
    except Exception:
        return ""


def search_patterns(text: str, patterns: List[str]) -> Optional[str]:
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            if m.groups():
                return m.group(1).strip()
            return m.group(0).strip()
    return None


def extract_from_c_header(c_path: Path) -> Dict[str, Optional[str]]:
    """
    Look at the top of the C file for a block comment with descriptors.
    """
    head = read_text_safely(c_path, max_bytes=64_000)
    # extract first comment block
    m = re.search(r"/\*.*?\*/", head, flags=re.S)
    text = m.group(0) if m else head[:2000]

    award = search_patterns(text, AWARD_HINTS)
    authors = search_patterns(text, AUTHOR_HINTS)
    summary = search_patterns(text, SUMMARY_HINTS)

    # As a fallback, take the first 1-2 lines that look descriptive
    if not summary:
        lines = [ln.strip("/* #\t -") for ln in text.splitlines() if ln.strip()]
        if lines:
            summary = lines[0]
            if len(lines) > 1 and len((summary + " " + lines[1])) < 200:
                summary = (summary + " " + lines[1]).strip()

    return {"award": award, "authors": authors, "summary": summary}


def extract_from_side_files(entry_dir: Path) -> Dict[str, Optional[str]]:
    merged = {"award": None, "authors": None, "summary": None}
    for fname in TEXTY_FILES:
        p = entry_dir / fname
        if not p.exists():
            continue
        text = read_text_safely(p)
        if not merged["award"]:
            merged["award"] = search_patterns(text, AWARD_HINTS)
        if not merged["authors"]:
            merged["authors"] = search_patterns(text, AUTHOR_HINTS)
        if not merged["summary"]:
            merged["summary"] = search_patterns(text, SUMMARY_HINTS)
        if all(merged.values()):
            break

    return merged


def short_slug(s: Optional[str]) -> str:
    if not s:
        return "unknown"
    s = s.strip()
    s = re.sub(r"[\s/]+", "_", s)
    s = re.sub(r"[^a-zA-Z0-9_.-]+", "", s)
    return s[:64] if len(s) > 64 else s


def guess_year_and_entry(c_path: Path, repo_root: Path) -> Tuple[str, str]:
    """
    Heuristic: look for path segments that look like /<year>/<entry>/...
    If not found, fallback to 'unknown'.
    """
    rel = c_path.relative_to(repo_root)
    parts = rel.parts
    year = "unknown"
    entry = "unknown"
    # Look for a 4-digit year in parts
    for i, seg in enumerate(parts):
        if re.fullmatch(r"\d{4}", seg):
            year = seg
            if i + 1 < len(parts):
                entry = parts[i + 1]
            break
    # sanitize entry
    entry = short_slug(entry)
    return year, entry


def find_c_files(repo_root: Path) -> List[Path]:
    return [p for p in repo_root.rglob("*.c") if p.is_file()]


def write_descriptor(out_dir: Path, data: Dict) -> None:
    (out_dir / "descriptor.json").write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )


def ensure_clean_outdir(outdir: Path, force: bool):
    if outdir.exists():
        if not force:
            raise FileExistsError(f"Output directory exists: {outdir}. Use --force to overwrite.")
        shutil.rmtree(outdir)
    outdir.mkdir(parents=True, exist_ok=True)


def main():
    ap = argparse.ArgumentParser(description="IOCCC winners scraper and organizer")
    ap.add_argument("--repo-url", default=DEFAULT_REPO_URL, help="Git repo URL to clone")
    ap.add_argument("--branch", default=None, help="Git branch to checkout")
    ap.add_argument("--workdir", default="./work", help="Where to create/find the clone")
    ap.add_argument("--local-clone", default=None, help="Existing clone path (skips cloning)")
    ap.add_argument("--outdir", default="./iocc_out", help="Output directory")
    ap.add_argument("--force", action="store_true", help="Overwrite output directory if exists")
    args = ap.parse_args()

    workdir = Path(args.workdir).resolve()
    outdir = Path(args.outdir).resolve()
    local_clone = Path(args.local_clone).resolve() if args.local_clone else None

    repo_root = ensure_clone(args.repo_url, workdir, local_clone, args.branch)
    ensure_clean_outdir(outdir, args.force)

    c_files = find_c_files(repo_root)

    manifest_rows = []
    for c_path in c_files:
        year, entry = guess_year_and_entry(c_path, repo_root)

        # Probe for side metadata from entry dir, if it exists:
        entry_dir = c_path.parent
        side = extract_from_side_files(entry_dir)

        # Probe C header comment:
        head = extract_from_c_header(c_path)

        # Merge heuristic (prefer side file award if present):
        award = side["award"] or head["award"] or "unknown"
        authors = side["authors"] or head["authors"] or "unknown"
        summary = side["summary"] or head["summary"] or f"IOCCC entry {entry} ({year})"

        award_slug = short_slug(award)

        # Build output path: /year/award/entry/
        target_dir = outdir / year / award_slug / entry
        target_dir.mkdir(parents=True, exist_ok=True)

        # Copy ONLY .c files from the entry directory (and subdirs) for this entry
        # We detect the entry root as the parent directory that contains the current .c
        # but we limit copies to files under that parent that are .c
        # (prevents bringing along README and other artifacts).
        for p in entry_dir.rglob("*.c"):
            rel = p.relative_to(entry_dir)
            dest = target_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dest)

        # Build descriptor
        descriptor = {
            "year": year,
            "entry": entry,
            "award": award,
            "authors": authors,
            "original_path": str(c_path.parent.resolve()),
            "source_files": sorted([str(Path("."+os.sep) / f.relative_to(target_dir)) for f in target_dir.rglob("*.c")]),
            "summary": summary,
            "LLM_context": (
                "This directory contains only the C source files of an IOCCC winning entry. "
                "The code is intentionally obfuscated or unusually constructed. "
                "When analyzing, first read descriptor.json for year, award, and authors. "
                "Focus on top-of-file comments, macros, and unusual control flow to infer purpose. "
                "Avoid relying on removed README/Makefiles from the original repo."
            ),
        }
        write_descriptor(target_dir, descriptor)

        manifest_rows.append([year, award, entry, authors, str(target_dir)])

    # Write a manifest CSV at top
    manifest_path = outdir / "manifest.csv"
    with manifest_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["year", "award", "entry", "authors", "output_dir"])
        w.writerows(manifest_rows)

    print(f"Done. C files organized under: {outdir}")
    print(f"Manifest: {manifest_path}")


if __name__ == "__main__":
    main()
