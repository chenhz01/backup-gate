# -*- coding: utf-8 -*-
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 chenhz01 — see LICENSE, LICENSE-SPEC and NOTICE
"""backup-gate — prove a backup directory is intact and fresh.

Pure standard library. Mechanism is deliberately inverted from backup tools:
this does NOT copy data. It (1) snapshots a manifest (SHA-256 + size per
file), (2) re-verifies a backup directory against that manifest, and
(3) checks manifest freshness against a threshold, plus a restore-drill
checklist generator.
Exit codes: 0 = verified clean, 1 = integrity findings (missing/mismatch/
extra), 2 = input error. Freshness issues are warnings, never errors.
"""
import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone

MANIFEST_NAME = "manifest.json"
DEFAULT_FRESHNESS_HOURS = 48

CHECKLIST = """restore-drill checklist (run quarterly):
 1. Copy manifest.json and the backup to an isolated machine.
 2. python gate.py verify <dir> --manifest manifest.json
 3. Pick the 3 largest files; open them with their real applications.
 4. Restore ONE file into production-staging via the documented runbook.
 5. Time the whole drill; if > your RTO, escalate.
 6. Record date, verdict, and drill duration in the compliance log.
"""


def _utcnow_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_of(path, bufsize=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(bufsize)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def build_manifest(root):
    files = []
    root = os.path.abspath(root)
    for dirpath, _dirs, names in os.walk(root):
        for name in names:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace("\\", "/")
            if rel == MANIFEST_NAME:
                continue
            files.append({"path": rel, "size": os.path.getsize(full),
                          "sha256": sha256_of(full)})
    return {"tool": "backup-gate", "generated_at": _utcnow_iso(),
            "root": os.path.basename(root), "files": sorted(files, key=lambda f: f["path"])}


def _parse_iso(ts):
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def verify_backup(root, manifest, freshness_hours=DEFAULT_FRESHNESS_HOURS):
    findings = []
    root = os.path.abspath(root)

    entries = manifest.get("files")
    if not isinstance(entries, list):
        raise ValueError("manifest has no 'files' array")

    declared = {}
    for i, e in enumerate(entries):
        if not isinstance(e, dict) or not isinstance(e.get("path"), str) or not e["path"]:
            raise ValueError("manifest entry #%d needs a non-empty 'path'" % i)
        if e["path"] in declared:
            raise ValueError("manifest lists %r twice" % e["path"])
        declared[e["path"]] = e

    on_disk = set()
    for dirpath, _dirs, names in os.walk(root):
        for name in names:
            rel = os.path.relpath(os.path.join(dirpath, name), root).replace("\\", "/")
            if rel != MANIFEST_NAME:
                on_disk.add(rel)

    missing = sorted(set(declared) - on_disk)
    extra = sorted(on_disk - set(declared))
    for rel in missing:
        findings.append({"severity": "error", "path": rel, "code": "missing",
                         "message": "declared in manifest but absent on disk"})
    for rel in extra:
        findings.append({"severity": "warning", "path": rel, "code": "extra",
                         "message": "on disk but not in manifest"})

    mismatch = 0
    for rel in sorted(set(declared) & on_disk):
        full = os.path.join(root, rel.replace("/", os.sep))
        ent = declared[rel]
        try:
            actual_size = os.path.getsize(full)
        except OSError as exc:
            findings.append({"severity": "error", "path": rel, "code": "unreadable",
                             "message": str(exc)})
            mismatch += 1
            continue
        if isinstance(ent.get("size"), int) and actual_size != ent["size"]:
            findings.append({"severity": "error", "path": rel, "code": "size-mismatch",
                             "message": "size %d != manifest %d" % (actual_size, ent["size"])})
            mismatch += 1
            continue
        try:
            actual_hash = sha256_of(full)
        except OSError as exc:
            findings.append({"severity": "error", "path": rel, "code": "unreadable",
                             "message": str(exc)})
            mismatch += 1
            continue
        if actual_hash != ent.get("sha256"):
            findings.append({"severity": "error", "path": rel, "code": "hash-mismatch",
                             "message": "sha256 differs from manifest"})
            mismatch += 1

    generated = _parse_iso(manifest.get("generated_at"))
    if generated is None:
        findings.append({"severity": "warning", "path": MANIFEST_NAME,
                         "code": "freshness-unknown",
                         "message": "manifest has no parseable generated_at (YYYY-MM-DDTHH:MM:SSZ)"})
    else:
        age_h = (datetime.now(timezone.utc) - generated).total_seconds() / 3600.0
        if age_h > freshness_hours:
            findings.append({"severity": "warning", "path": MANIFEST_NAME,
                             "code": "freshness-stale",
                             "message": "manifest is %.1f h old (threshold %d h); backup may predate recent data"
                                        % (age_h, freshness_hours)})

    errors = sum(1 for f in findings if f["severity"] == "error")
    return {"tool": "backup-gate",
            "summary": {"declared": len(declared), "on_disk": len(on_disk),
                        "missing": len(missing), "extra": len(extra),
                        "mismatch": mismatch, "errors": errors,
                        "warnings": len(findings) - errors,
                        "freshness_hours": freshness_hours},
            "findings": findings,
            "verdict": ("FAIL: %d integrity error(s)" % errors if errors
                        else ("PASS with %d warning(s)" % (len(findings) - errors)
                              if findings else "PASS: backup intact"))}


def render_text(report):
    lines = ["backup-gate report", "=" * 32]
    for f in report["findings"]:
        lines.append("%-6s %-28s %-18s %s"
                     % (f["severity"].upper(), f["path"][:28], f["code"], f["message"]))
    s = report["summary"]
    lines.append("summary: declared=%d on_disk=%d missing=%d extra=%d mismatch=%d "
                 "errors=%d warnings=%d" % (s["declared"], s["on_disk"], s["missing"],
                                            s["extra"], s["mismatch"], s["errors"], s["warnings"]))
    lines.append("verdict: %s" % report["verdict"])
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="backup-gate",
        description="Verify a backup directory against a SHA-256 manifest (verifier, not a backup tool).")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_m = sub.add_parser("manifest", help="snapshot a source tree into a manifest")
    p_m.add_argument("dir")
    p_m.add_argument("--out", default=MANIFEST_NAME)
    p_v = sub.add_parser("verify", help="verify a backup directory")
    p_v.add_argument("dir")
    p_v.add_argument("--manifest", default=MANIFEST_NAME)
    p_v.add_argument("--freshness-hours", type=int, default=DEFAULT_FRESHNESS_HOURS)
    p_v.add_argument("--format", choices=("text", "json"), default="text")
    sub.add_parser("checklist", help="print the restore-drill checklist")
    args = parser.parse_args(argv)

    if args.cmd == "checklist":
        print(CHECKLIST, end="")
        return 0

    if not os.path.isdir(args.dir):
        print("error: %r is not a directory" % args.dir, file=sys.stderr)
        return 2
    if args.cmd == "verify" and args.freshness_hours < 0:
        print("error: --freshness-hours must be >= 0", file=sys.stderr)
        return 2

    if args.cmd == "manifest":
        try:
            manifest = build_manifest(args.dir)
        except OSError as exc:
            print("error: %s" % exc, file=sys.stderr)
            return 2
        try:
            with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(manifest, fh, ensure_ascii=False, indent=2)
        except OSError as exc:
            print("error: cannot write %s: %s" % (args.out, exc), file=sys.stderr)
            return 2
        print("manifest written: %s (%d files)" % (args.out, len(manifest["files"])))
        return 0

    try:
        with open(args.manifest, "r", encoding="utf-8-sig") as fh:
            manifest = json.load(fh)
    except OSError as exc:
        print("error: cannot read manifest %s: %s" % (args.manifest, exc), file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print("error: invalid manifest JSON: %s" % exc, file=sys.stderr)
        return 2
    try:
        report = verify_backup(args.dir, manifest, freshness_hours=args.freshness_hours)
    except ValueError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2

    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_text(report))
    return 1 if report["summary"]["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
