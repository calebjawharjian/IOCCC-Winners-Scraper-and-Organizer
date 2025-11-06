# Organize IOCCC winner sources into a clean, analysis-ready tree.

This tool:

Scans an IOCCC winners repo/clone for all .c files

Organizes them as: out/<year>/<award-or-ranking>/<entry>/...

Excludes non-C artifacts (e.g., README.md, images, build junk)

Writes a per-entry descriptor.json (LLM-friendly) + a top-level manifest.csv

IOCCC entries are intentionally obfuscated. This tool makes the corpus easier to browse, label, and feed to LLMs without all the extra files.
