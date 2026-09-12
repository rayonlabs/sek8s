#!/usr/bin/env python3
"""
Fragment-based changelog promotion.

Developers drop categorized .md fragments into changelogs/<component>/unreleased/.
This script aggregates them into CHANGELOG.md entries.

Usage:
    # Promote all components that have pending fragments (idempotent)
    python promote_changelogs.py --promote

    # Validate changelog state for bumped versions
    python promote_changelogs.py --check

    # Strict mode: fail if ANY fragments remain in any unreleased/ dir
    python promote_changelogs.py --check --strict

    # Verify a branch-named fragment exists for each changed component
    # (pipe changed files from git diff --name-only)
    git diff --name-only base..head | python promote_changelogs.py --check-branch feature/nvidia-590-drivers
"""
import argparse
from collections import OrderedDict
from datetime import date
from pathlib import Path
import re
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent

VERSION_CHANGELOG_MAP: dict[str, str] = {
    "ansible/guest/VERSION": "changelogs/vm",
    "src/sek8s/VERSION": "changelogs/sek8s",
    "src/attestation-proxy/VERSION": "changelogs/attestation-proxy",
    "src/chutes-cvm/VERSION": "changelogs/chutes-cvm",
    "changelogs/ops/VERSION": "changelogs/ops",
}

CATEGORY_ORDER = ["Added", "Changed", "Fixed", "Removed"]

# Maps file path prefixes to the changelog component they affect.
# Order matters: first match wins, so more specific prefixes go first.
PATH_CHANGELOG_MAP: list[tuple[str, str]] = [
    ("src/sek8s/", "changelogs/sek8s"),
    ("src/sek8s-common/", "changelogs/sek8s"),
    ("src/attestation-proxy/", "changelogs/attestation-proxy"),
    ("src/chutes-cvm/", "changelogs/chutes-cvm"),
    ("ansible/guest/", "changelogs/vm"),
    ("nvevidence/", "changelogs/vm"),
    # Ops changelog — versioned via changelogs/ops/VERSION (CalVer YYYY.MM.PATCH).
    ("ansible/host/", "changelogs/ops"),
    ("host-tools/", "changelogs/ops"),
    (".github/workflows/", "changelogs/ops"),
]

HEADING_RE = re.compile(r"^## \[(.+?)\]")
CATEGORY_RE = re.compile(r"^### (.+)")


def read_version(version_file: Path) -> str:
    text = version_file.read_text().strip()
    return text.splitlines()[0].strip() if text else ""


def changelog_has_version(changelog: Path, version: str) -> bool:
    if not changelog.is_file():
        return False
    for line in changelog.read_text().splitlines():
        m = HEADING_RE.match(line)
        if m and m.group(1) == version:
            return True
    return False


def collect_fragments(unreleased_dir: Path) -> list[Path]:
    if not unreleased_dir.is_dir():
        return []
    return sorted(
        p for p in unreleased_dir.iterdir()
        if p.suffix == ".md" and p.name != ".gitkeep"
    )


def parse_fragment(path: Path) -> OrderedDict[str, list[str]]:
    """Parse a fragment file into {category: [bullet lines]}."""
    categories: OrderedDict[str, list[str]] = OrderedDict()
    current_cat = None
    for line in path.read_text().splitlines():
        cat_match = CATEGORY_RE.match(line)
        if cat_match:
            current_cat = cat_match.group(1)
            if current_cat not in categories:
                categories[current_cat] = []
        elif current_cat is not None and line.strip():
            categories[current_cat].append(line)
    return categories


def merge_categories(
    existing: OrderedDict[str, list[str]],
    new: OrderedDict[str, list[str]],
) -> OrderedDict[str, list[str]]:
    """Merge new category bullets into existing, preserving standard order."""
    combined: dict[str, list[str]] = {}
    for cat, bullets in existing.items():
        combined.setdefault(cat, []).extend(bullets)
    for cat, bullets in new.items():
        combined.setdefault(cat, []).extend(bullets)

    ordered: OrderedDict[str, list[str]] = OrderedDict()
    for cat in CATEGORY_ORDER:
        if cat in combined:
            ordered[cat] = combined.pop(cat)
    for cat in sorted(combined.keys()):
        ordered[cat] = combined[cat]
    return ordered


def aggregate_fragments(fragments: list[Path]) -> OrderedDict[str, list[str]]:
    """Merge all fragments, grouping bullets by category in standard order."""
    merged: dict[str, list[str]] = {}
    for frag in fragments:
        parsed = parse_fragment(frag)
        for cat, bullets in parsed.items():
            merged.setdefault(cat, []).extend(bullets)

    ordered: OrderedDict[str, list[str]] = OrderedDict()
    for cat in CATEGORY_ORDER:
        if cat in merged:
            ordered[cat] = merged.pop(cat)
    for cat in sorted(merged.keys()):
        ordered[cat] = merged[cat]
    return ordered


def build_version_section(version: str, categories: OrderedDict[str, list[str]]) -> str:
    lines = [f"## [{version}] - {date.today().isoformat()}", ""]
    for cat, bullets in categories.items():
        lines.append(f"### {cat}")
        lines.extend(bullets)
        lines.append("")
    return "\n".join(lines)


def find_insert_position(content: str) -> int:
    """Find the byte offset after the preamble where versioned entries start."""
    lines = content.splitlines(keepends=True)
    offset = 0
    for line in lines:
        if HEADING_RE.match(line):
            return offset
        offset += len(line)
    return offset


def parse_changelog_section(
    changelog: Path, version: str,
) -> tuple[OrderedDict[str, list[str]], int, int]:
    """Extract an existing version section from a CHANGELOG.

    Returns (categories, start_offset, end_offset) where offsets are byte
    positions in the file content spanning from the ``## [version]`` line
    through the end of the section (up to the next ``## [...]`` heading or EOF).
    """
    content = changelog.read_text()
    lines = content.splitlines(keepends=True)
    categories: OrderedDict[str, list[str]] = OrderedDict()
    current_cat = None
    in_section = False
    start = 0
    offset = 0

    for line in lines:
        heading = HEADING_RE.match(line)
        if heading:
            if in_section:
                return categories, start, offset
            if heading.group(1) == version:
                in_section = True
                start = offset
        elif in_section:
            cat_match = CATEGORY_RE.match(line)
            if cat_match:
                current_cat = cat_match.group(1)
                if current_cat not in categories:
                    categories[current_cat] = []
            elif current_cat is not None and line.strip():
                categories[current_cat].append(line.rstrip("\n"))
        offset += len(line)

    if in_section:
        return categories, start, offset
    return OrderedDict(), 0, 0


def insert_new_section(changelog: Path, version_section: str) -> None:
    content = changelog.read_text() if changelog.is_file() else ""
    pos = find_insert_position(content)
    new_content = content[:pos] + version_section + "\n" + content[pos:]
    changelog.write_text(new_content)


def replace_section(changelog: Path, version: str, new_section_text: str) -> None:
    content = changelog.read_text()
    _, start, end = parse_changelog_section(changelog, version)
    new_content = content[:start] + new_section_text + "\n" + content[end:]
    changelog.write_text(new_content)


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------


def check_mode(strict: bool) -> int:
    """Validate changelog state.

    Default: for each component where VERSION differs from the latest changelog
    heading, verify fragments exist (pre-promotion) OR the heading matches
    (post-promotion).

    --strict: fail if ANY .md fragments remain in ANY unreleased/ directory.
    """
    if strict:
        return _check_strict()
    return _check_normal()


def _check_strict() -> int:
    errors = 0
    changelogs_dir = REPO_ROOT / "changelogs"
    if not changelogs_dir.is_dir():
        print("OK: No changelogs/ directory.")
        return 0
    for comp_dir in sorted(changelogs_dir.iterdir()):
        if not comp_dir.is_dir():
            continue
        fragments = collect_fragments(comp_dir / "unreleased")
        if fragments:
            names = ", ".join(f.name for f in fragments)
            print(f"ERROR: {comp_dir.relative_to(REPO_ROOT)}/unreleased/ "
                  f"still has fragments: {names}. "
                  "Run: make promote-changelogs")
            errors += 1
    if not errors:
        print("OK: No changelog fragments remain.")
    return 1 if errors else 0


def latest_changelog_version(changelog: Path) -> str | None:
    """Return the version from the first ``## [x.y.z]`` heading, or None."""
    if not changelog.is_file():
        return None
    for line in changelog.read_text().splitlines():
        m = HEADING_RE.match(line)
        if m:
            return m.group(1)
    return None


def _check_normal() -> int:
    """For each component where VERSION differs from the latest changelog
    heading, verify that fragments exist (pre-promotion state)."""
    errors = 0
    for vf_rel, comp_dir_rel in VERSION_CHANGELOG_MAP.items():
        vf = REPO_ROOT / vf_rel
        comp_dir = REPO_ROOT / comp_dir_rel
        if not vf.is_file():
            continue

        version = read_version(vf)
        if not version:
            continue

        changelog = comp_dir / "CHANGELOG.md"
        latest = latest_changelog_version(changelog)
        fragments = collect_fragments(comp_dir / "unreleased")

        if version == latest:
            if fragments:
                names = ", ".join(f.name for f in fragments)
                print(f"WARN: {changelog.relative_to(REPO_ROOT)} already has ## [{version}] "
                      f"but fragments remain ({names}). Run: make promote-changelogs")
            else:
                print(f"OK: {changelog.relative_to(REPO_ROOT)} has ## [{version}].")
        elif changelog_has_version(changelog, version):
            print(f"OK: {changelog.relative_to(REPO_ROOT)} has ## [{version}].")
        elif fragments:
            names = ", ".join(f.name for f in fragments)
            print(f"OK: {comp_dir.relative_to(REPO_ROOT)}/unreleased/ has "
                  f"fragments pending promotion: {names}")
        else:
            print(f"ERROR: {vf_rel} is at {version} but "
                  f"{changelog.relative_to(REPO_ROOT)} has no ## [{version}] heading "
                  f"and no fragments in unreleased/. Add a changelog fragment.")
            errors += 1
    return 1 if errors else 0


def branch_to_fragment_name(branch: str) -> str:
    """Derive the expected fragment filename from a branch name.

    The leading ``type/`` segment (``feat/``, ``fix/``, ``chore/``,
    ``refactor/``, ...) is a branch-naming convention only and is deliberately
    NOT part of the fragment name. Drop the first path segment when present and
    flatten any remaining slashes so the result is always a flat filename.
    """
    name = branch.split("/", 1)[1] if "/" in branch else branch
    return f"{name.replace('/', '-')}.md"


def affected_components(changed_files: list[str]) -> set[str]:
    """Map changed file paths to the changelog component directories they affect."""
    components: set[str] = set()
    for filepath in changed_files:
        for prefix, comp_dir in PATH_CHANGELOG_MAP:
            if filepath.startswith(prefix):
                components.add(comp_dir)
                break
    return components


def check_branch_mode(branch: str) -> int:
    """Verify that a branch-named fragment exists in every component affected
    by the changed files.  Changed files are read from stdin (one per line),
    as produced by ``git diff --name-only``."""
    fragment_name = branch_to_fragment_name(branch)

    changed = [line.strip() for line in sys.stdin if line.strip()]
    if not changed:
        print("OK: No changed files provided; nothing to check.")
        return 0

    required = affected_components(changed)
    if not required:
        print("OK: No changelog-tracked paths changed.")
        return 0

    errors = 0
    for comp_dir_rel in sorted(required):
        frag = REPO_ROOT / comp_dir_rel / "unreleased" / fragment_name
        if frag.is_file():
            print(f"OK: Found {frag.relative_to(REPO_ROOT)}")
        else:
            print(f"ERROR: Missing {comp_dir_rel}/unreleased/{fragment_name} — "
                  f"add a changelog fragment for your changes.")
            errors += 1

    return 1 if errors else 0


def promote_mode() -> int:
    """Idempotent promotion: aggregate fragments into CHANGELOG.md.

    - Heading absent + fragments: create new versioned section
    - Heading present + fragments: merge fragments into existing section
    - No fragments: skip
    """
    promoted = []
    for vf_rel, comp_dir_rel in VERSION_CHANGELOG_MAP.items():
        vf = REPO_ROOT / vf_rel
        comp_dir = REPO_ROOT / comp_dir_rel
        if not vf.is_file():
            continue

        version = read_version(vf)
        if not version:
            print(f"SKIP: {vf_rel} is empty")
            continue

        unreleased_dir = comp_dir / "unreleased"
        fragments = collect_fragments(unreleased_dir)
        if not fragments:
            continue

        changelog = comp_dir / "CHANGELOG.md"
        new_categories = aggregate_fragments(fragments)

        if changelog_has_version(changelog, version):
            existing_cats, _, _ = parse_changelog_section(changelog, version)
            merged = merge_categories(existing_cats, new_categories)
            section = build_version_section(version, merged)
            replace_section(changelog, version, section)
            action = "merged into"
        else:
            section = build_version_section(version, new_categories)
            insert_new_section(changelog, section)
            action = "created"

        for frag in fragments:
            frag.unlink()

        promoted.append(
            f"{comp_dir.relative_to(REPO_ROOT)}: [{version}] "
            f"{action} from {len(fragments)} fragment(s)"
        )

    if promoted:
        for msg in promoted:
            print(f"Promoted {msg}")
    else:
        print("Nothing to promote.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--check", action="store_true",
        help="Validate changelog state. Add --strict to block any remaining fragments.",
    )
    group.add_argument(
        "--check-branch", metavar="BRANCH",
        help="Verify a branch-named fragment exists for each affected component. "
             "Reads changed file paths from stdin.",
    )
    group.add_argument(
        "--promote", action="store_true",
        help="Idempotent: aggregate fragments into CHANGELOG.md and delete them.",
    )
    parser.add_argument(
        "--strict", action="store_true",
        help="With --check: fail if ANY fragments exist in any unreleased/ dir.",
    )
    args = parser.parse_args()

    if args.check:
        return check_mode(strict=args.strict)
    elif args.check_branch:
        return check_branch_mode(args.check_branch)
    else:
        return promote_mode()


if __name__ == "__main__":
    sys.exit(main())
