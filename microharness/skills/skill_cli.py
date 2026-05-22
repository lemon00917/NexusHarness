"""
Skill CLI — Command-line skill management for NexusHarness.

Usage:
    python harness.py skill list                    # List installed skills
    python harness.py skill install <source>         # Install a skill
    python harness.py skill remove <name>            # Remove a skill
    python harness.py skill show <name>              # Show skill details
    python harness.py skill update <name> [source]  # Update a skill

Sources:
    - Raw SKILL.md URL: https://raw.githubusercontent.com/user/repo/main/skills/weather/SKILL.md
    - GitHub tree URL: https://github.com/user/repo/tree/main/skills/weather
    - Local path: ./my-skill or /path/to/skill
"""

import argparse
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Optional

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False

# ──────────────────────────────────────────────────
# Paths
# ──────────────────────────────────────────────────

def get_project_root() -> Path:
    return Path(__file__).parent.parent


def get_skills_dir() -> Path:
    return get_project_root() / "skills"


# ──────────────────────────────────────────────────
# SKILL.md parsing (copied from skill_manager for CLI-only use)
# ──────────────────────────────────────────────────

def parse_skill_md(path: Path) -> dict:
    """Parse SKILL.md: extract frontmatter, description, and bash blocks."""
    content = path.read_text(encoding="utf-8")

    parts = content.split("---", 2)
    if len(parts) < 3:
        raise ValueError(f"Invalid SKILL.md format in {path}")

    frontmatter = parts[1].strip()
    body = parts[2].strip()

    fm = {}
    if HAS_YAML:
        try:
            fm = yaml.safe_load(frontmatter) or {}
        except Exception:
            pass

    # Extract all bash code blocks
    bash_blocks = []
    for match in re.finditer(r'```bash\s+(.*?)\s```', body, re.DOTALL):
        raw_cmd = match.group(1).strip()
        cmd_lines = [l for l in raw_cmd.splitlines() if not l.strip().startswith("# Output:")]
        cmd = "\n".join(cmd_lines)
        params = list(set(re.findall(r'\{(\w+)\}', cmd)))
        if cmd.strip():
            bash_blocks.append({"command": cmd, "params": params})

    return {
        "name": fm.get("name", path.parent.name),
        "description": fm.get("description", ""),
        "metadata": fm.get("metadata", {}),
        "bash_blocks": bash_blocks,
        "slug": path.parent.name,
        "path": path,
    }


def scan_skills() -> list:
    """Scan skills/ directory and return list of parsed skill data."""
    skills_dir = get_skills_dir()
    if not skills_dir.exists():
        return []

    results = []
    for skill_dir in skills_dir.iterdir():
        if not skill_dir.is_dir() or skill_dir.name.startswith('_'):
            continue
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            continue
        try:
            results.append(parse_skill_md(skill_md))
        except Exception:
            continue
    return results


# ──────────────────────────────────────────────────
# Install
# ──────────────────────────────────────────────────

def make_slug(name: str) -> str:
    """Convert skill name to directory slug."""
    return name.lower().replace(" ", "-").replace("_", "-")


def install_from_url(url: str) -> tuple:
    """
    Download SKILL.md from a URL.
    Returns (slug, content).
    Handles:
      - Raw GitHub URL (raw.githubusercontent.com)
      - Direct .md file URL
      - GitHub tree URL (constructs raw URL)
    """
    url = url.strip()

    # GitHub tree URL → construct raw URL
    gh_tree = re.match(r'https://github\.com/([^/]+)/([^/]+)/tree/([^/]+)/(.+)', url)
    if gh_tree:
        user, repo, branch, path = gh_tree.groups()
        url = f"https://raw.githubusercontent.com/{user}/{repo}/{branch}/{path}/SKILL.md"

    # Try to handle redirects (GitHub "view" URLs redirect to raw or HTML)
    max_redirects = 5
    last_url = url
    for _ in range(max_redirects):
        req = urllib.request.Request(last_url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                content = resp.read().decode("utf-8")
                # Check if we got HTML instead of markdown (GitHub view page)
                if content.strip().startswith('<!DOCTYPE') or content.strip().startswith('<html'):
                    # This is an HTML page, not raw markdown - redirect to raw if possible
                    if 'raw.githubusercontent.com' not in last_url and '/raw/' not in last_url:
                        raise ValueError(
                            f"URL appears to be a GitHub view page, not raw content. "
                            f"Use the raw URL: https://raw.githubusercontent.com/..."
                        )
                break
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308) and 'Location' in e.headers:
                last_url = e.headers['Location']
                continue
            raise

    # Extract skill name from frontmatter for slug
    fm_match = re.search(r'^---\s*\n(.*?)\n---', content, re.DOTALL | re.MULTILINE)
    slug = None
    if fm_match and HAS_YAML:
        try:
            fm = yaml.safe_load(fm_match.group(1))
            if fm and fm.get("name"):
                slug = make_slug(fm["name"])
        except Exception:
            pass

    if not slug:
        # Fallback: slug from URL path
        slug = url.rstrip("/").rsplit("/", 2)[-1] if "/" in url else "unnamed"

    return slug, content


def install_from_local(path: Path) -> tuple:
    """Install from a local skill directory or SKILL.md file."""
    path = Path(path).expanduser().resolve()

    if path.is_file():
        # Direct SKILL.md file
        content = path.read_text(encoding="utf-8")
        slug = _slug_from_content(content) or path.parent.name
    elif path.is_dir():
        skill_md = path / "SKILL.md"
        if not skill_md.exists():
            raise ValueError(f"No SKILL.md found in {path}")
        content = skill_md.read_text(encoding="utf-8")
        slug = _slug_from_content(content) or path.name
    else:
        raise ValueError(f"Invalid path: {path}")

    return slug, content


def _slug_from_content(content: str) -> Optional[str]:
    """Extract slug from SKILL.md frontmatter name."""
    fm_match = re.search(r'^---\s*\n(.*?)\n---', content, re.DOTALL | re.MULTILINE)
    if fm_match and HAS_YAML:
        try:
            fm = yaml.safe_load(fm_match.group(1))
            if fm and fm.get("name"):
                return make_slug(fm["name"])
        except Exception:
            pass
    return None


def install(source: str) -> None:
    """Install a skill from URL or local path."""
    source = source.strip()
    skills_dir = get_skills_dir()
    skills_dir.mkdir(exist_ok=True)

    if source.startswith("http://") or source.startswith("https://"):
        slug, content = install_from_url(source)
    else:
        slug, content = install_from_local(source)

    target_dir = skills_dir / slug
    target_file = target_dir / "SKILL.md"

    if target_dir.exists():
        print(f"[SKILL] Updating existing skill: {slug}")
    else:
        print(f"[SKILL] Installing new skill: {slug}")

    target_dir.mkdir(exist_ok=True)
    target_file.write_text(content, encoding="utf-8")
    print(f"[SKILL] Installed at: {target_dir}")

    # Reload skills so the new one is available immediately
    from .skill_manager import load_skills, clear_skills
    clear_skills()
    load_skills()
    print(f"[SKILL] Skill '{slug}' is now loaded and available.")


# ──────────────────────────────────────────────────
# List
# ──────────────────────────────────────────────────

def list_skills() -> None:
    """List all installed skills."""
    skills = scan_skills()

    if not skills:
        print("No skills installed. Run 'python harness.py skill install <source>' to add one.")
        return

    print(f"{'Name':<22} {'Slug':<20} {'Description'}")
    print("-" * 70)
    for s in skills:
        name = s["name"]
        slug = s["slug"]
        desc = (s["description"] or "")[:40]
        meta = s.get("metadata", {})
        safety = ""
        if isinstance(meta, dict):
            clawdbot = meta.get("clawdbot", {}) or {}
            safety = clawdbot.get("safety", "")
        print(f"{name:<22} {slug:<20} {desc}  [{safety}]")

    print(f"\n({len(skills)} skill(s) installed)")


# ──────────────────────────────────────────────────
# Show
# ──────────────────────────────────────────────────

def show_skill(name: str) -> None:
    """Show detailed info about a skill."""
    skills_dir = get_skills_dir()
    if not skills_dir.exists():
        print(f"[SKILL] Not found: {name}")
        return

    # Try exact directory match first, then by slug
    skill_dir = None
    for d in skills_dir.iterdir():
        if d.is_dir() and (d.name == name or d.name == name.lower().replace(" ", "-")):
            skill_dir = d
            break
        # Also check if name matches slugified version of subdirectory names
        slugified = d.name.lower().replace("-", "_").replace(" ", "_")
        if slugified == name.lower().replace("-", "_").replace(" ", "_"):
            skill_dir = d
            break
        # Check if the skill's own name (from frontmatter) matches
        if d.is_dir():
            skill_md = d / "SKILL.md"
            if skill_md.exists():
                try:
                    data = parse_skill_md(skill_md)
                    if data["name"].lower().replace(" ", "-") == name.lower().replace(" ", "-"):
                        skill_dir = d
                        break
                except Exception:
                    pass

    skill_md = skill_dir / "SKILL.md" if skill_dir and skill_dir.exists() else None
    if not skill_md or not skill_md.exists():
        print(f"[SKILL] Not found: {name}")
        print(f"  Available skills: {[s['slug'] for s in scan_skills()]}")
        return

    data = parse_skill_md(skill_md)

    print(f"Name:        {data['name']}")
    print(f"Slug:        {data['slug']}")
    print(f"Description: {data['description'] or '(none)'}")
    print(f"Path:        {skill_md}")

    meta = data.get("metadata", {})
    if isinstance(meta, dict):
        clawdbot = meta.get("clawdbot", {}) or {}
        if clawdbot:
            print(f"Safety:      {clawdbot.get('safety', 'KEYWORD_CHECK')}")
            if clawdbot.get("requires"):
                print(f"Requires:    {clawdbot['requires']}")

    print(f"\nBash commands: {len(data['bash_blocks'])}")
    for i, block in enumerate(data["bash_blocks"]):
        print(f"\n  [{i+1}] Command: {block['command'][:80]}{'...' if len(block['command']) > 80 else ''}")
        if block["params"]:
            print(f"      Params: {block['params']}")

    # Check if currently loaded
    from .skill_manager import get_skills
    loaded = [t.name for t in get_skills()]
    if data["slug"] in [make_slug(s["name"]) for s in scan_skills()]:
        tool_names = [n for n in loaded if n.startswith(data["slug"].replace("-", "_"))]
        if tool_names:
            print(f"\n  Loaded as tools: {', '.join(tool_names)}")


# ──────────────────────────────────────────────────
# Remove
# ──────────────────────────────────────────────────

def remove_skill(name: str) -> None:
    """Remove an installed skill."""
    skills_dir = get_skills_dir()
    skill_dir = skills_dir / name

    if not skill_dir.exists():
        print(f"[SKILL] Not found: {name}")
        return

    shutil.rmtree(skill_dir)
    print(f"[SKILL] Removed: {name}")

    # Reload so the removed skill is deregistered
    from .skill_manager import load_skills, clear_skills
    clear_skills()
    load_skills()


# ──────────────────────────────────────────────────
# Update
# ──────────────────────────────────────────────────

def update_skill(name: str, source: Optional[str] = None) -> None:
    """Update a skill. If source not provided, re-fetch from original location (NYI)."""
    if not source:
        print("[SKILL] Update requires a source URL. Example:")
        print(f"  python harness.py skill update {name} https://raw.githubusercontent.com/.../SKILL.md")
        return

    # Re-run install logic to overwrite
    install(source)


# ──────────────────────────────────────────────────
# Main CLI entry point
# ──────────────────────────────────────────────────

def main() -> None:
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')

    parser = argparse.ArgumentParser(
        prog="python harness.py skill",
        description="NexusHarness Skill CLI — manage agent skills",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # list
    sub.add_parser("list", help="List installed skills")

    # install
    install_parser = sub.add_parser("install", help="Install a skill from URL or local path")
    install_parser.add_argument("source", help="URL or local path to SKILL.md or skill directory")

    # remove
    remove_parser = sub.add_parser("remove", help="Remove an installed skill")
    remove_parser.add_argument("name", help="Skill name/slug to remove")

    # show
    show_parser = sub.add_parser("show", help="Show skill details")
    show_parser.add_argument("name", help="Skill name/slug")

    # update
    update_parser = sub.add_parser("update", help="Update a skill")
    update_parser.add_argument("name", help="Skill name/slug to update")
    update_parser.add_argument("source", nargs="?", help="New source URL (required)")

    args = parser.parse_args(sys.argv[2:] if len(sys.argv) > 2 else [])

    try:
        if args.cmd == "list":
            list_skills()
        elif args.cmd == "install":
            install(args.source)
        elif args.cmd == "remove":
            remove_skill(args.name)
        elif args.cmd == "show":
            show_skill(args.name)
        elif args.cmd == "update":
            update_skill(args.name, args.source)
    except Exception as e:
        print(f"[SKILL] Error: {e}")
        raise


if __name__ == "__main__":
    main()