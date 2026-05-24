"""
Skill CLI — Command-line skill management for NexusHarness.
"""

import argparse
import shutil
import sys
from pathlib import Path
from typing import Optional, Dict, Any, Tuple, List
from urllib.error import HTTPError

from microharness.skills.skill_common import (
    parse_skill_md,
    normalize_github_url,
    fetch_url_content_with_retry as fetch_url,
    install_from_local,
    install_from_url,
    MAX_DESCRIPTION_LENGTH,
)

# ── Constants ────────────────────────────────────────────────────────────

EXIT_SUCCESS = 0
EXIT_INPUT_ERROR = 1
EXIT_NETWORK_ERROR = 2
EXIT_IO_ERROR = 3
EXIT_UNKNOWN_ERROR = 99

# ── Paths ─────────────────────────────────────────────────────────────────

def get_project_root() -> Path:
    """
    Get the project root directory.

    skill_cli.py is at: microharness/skills/skill_cli.py
    Returns: parent of microharness directory
    """
    return Path(__file__).parent.parent.parent


def get_skills_dir() -> Path:
    """Get the skills directory path."""
    return get_project_root() / "skills"


# ── Cache Management ─────────────────────────────────────────────────────

class _SkillCache:
    """Thread-safe cache for scanned skills."""

    def __init__(self):
        self._cache: List[Dict[str, Any]] = []
        self._initialized = False

    def get(self) -> List[Dict[str, Any]]:
        """Get cached skills, loading if necessary."""
        if not self._initialized:
            self._refresh()
        return self._cache.copy()

    def _refresh(self) -> None:
        """Rebuild the cache by scanning the skills directory."""
        skills_dir = get_skills_dir()
        self._cache = []

        if not skills_dir.exists():
            self._initialized = True
            return

        for skill_dir in skills_dir.iterdir():
            if not skill_dir.is_dir() or skill_dir.name.startswith('_') or skill_dir.name == '__pycache__':
                continue

            skill_md = skill_dir / "SKILL.md"
            if not skill_md.exists():
                continue

            try:
                self._cache.append(parse_skill_md(skill_md))
            except Exception as e:
                # Log but continue scanning other skills
                print(f"[SKILL] Warning: Failed to parse {skill_dir.name}: {e}", file=sys.stderr)
                continue

        self._initialized = True

    def invalidate(self) -> None:
        """Invalidate the cache, forcing a refresh on next get."""
        self._initialized = False


_skill_cache = _SkillCache()


def scan_skills() -> List[Dict[str, Any]]:
    """Scan skills directory and return list of parsed skill data."""
    return _skill_cache.get()


# ── Install ───────────────────────────────────────────────────────────────

def install(source: str) -> None:
    """
    Install a skill from URL or local path.

    Args:
        source: URL or local path to SKILL.md or skill directory

    Raises:
        ValueError: If source is invalid or empty
        HTTPError: If network request fails
    """
    source = source.strip()
    if not source:
        raise ValueError("Source cannot be empty")

    skills_dir = get_skills_dir()
    skills_dir.mkdir(exist_ok=True)

    print(f"[SKILL] Installing from: {source}")

    # Determine source type and get content
    if source.startswith(("http://", "https://")):
        slug, content = _install_from_url(source)
    else:
        slug, content = _install_from_local_with_feedback(Path(source))

    # Validate content
    if not content or not content.strip():
        raise ValueError("Downloaded content is empty")

    # Write skill file
    target_dir = skills_dir / slug
    target_file = target_dir / "SKILL.md"

    action = "Updating" if target_dir.exists() else "Installing new"
    print(f"[SKILL] {action} skill: {slug}")

    target_dir.mkdir(exist_ok=True)
    target_file.write_text(content, encoding="utf-8")
    print(f"[SKILL] Installed at: {target_dir}")

    # Reload skills
    _reload_skills()
    print(f"[SKILL] ✓ Skill '{slug}' is now loaded and available.")


def _install_from_url(url: str) -> Tuple[str, str]:
    """Download SKILL.md from a URL with progress indication."""
    print(f"[SKILL] Fetching from: {url}")
    normalized_url = normalize_github_url(url)
    if normalized_url != url:
        print(f"[SKILL] Normalized to: {normalized_url}")

    content, final_url = fetch_url(normalized_url)
    print(f"[SKILL] Downloaded {len(content)} bytes")

    # Use install_from_url's slug extraction logic
    slug, _ = install_from_url(url, use_retry=False)
    print(f"[SKILL] Identified as: {slug}")

    return slug, content


def _install_from_local_with_feedback(path: Path) -> Tuple[str, str]:
    """Install from local path with progress indication."""
    print(f"[SKILL] Reading from: {path}")
    slug, content = install_from_local(path)
    print(f"[SKILL] Identified as: {slug}")
    return slug, content


def _reload_skills() -> None:
    """Reload skills in the skill manager and invalidate cache."""
    _skill_cache.invalidate()

    try:
        from .skill_manager import load_skills
        load_skills(force=True)
    except ImportError:
        pass


# ── Index Building ───────────────────────────────────────────────────────

def build_skill_index() -> Dict[str, Path]:
    """Build a mapping from skill identifiers to skill directory path."""
    skills_dir = get_skills_dir()
    index = {}

    if not skills_dir.exists():
        return index

    for skill_dir in skills_dir.iterdir():
        if not skill_dir.is_dir() or skill_dir.name.startswith('_'):
            continue

        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            continue

        try:
            data = parse_skill_md(skill_md)
            # Add all possible lookup variants
            variants = {
                data["slug"], data["slug"].lower(),
                data["name"], data["name"].lower(),
                data["name"].lower().replace(" ", "-"),
                data["name"].lower().replace(" ", "_"),
                skill_dir.name, skill_dir.name.lower(),
            }
            for variant in variants:
                index[variant] = skill_dir
        except Exception:
            continue

    return index


def find_skill_by_name(name: str) -> Optional[Path]:
    """
    Find a skill directory by name or slug.

    Args:
        name: Skill name or slug

    Returns:
        Path to skill directory or None if not found
    """
    if not name:
        return None

    index = build_skill_index()
    return index.get(name) or index.get(name.lower())


# ── List Command ─────────────────────────────────────────────────────────

def list_skills() -> None:
    """List all installed skills with their details."""
    skills = scan_skills()

    if not skills:
        print("No skills installed. Run 'python harness.py skill install <source>' to add one.")
        return

    # Print header
    print(f"{'Name':<22} {'Slug':<20} {'Description'}")
    print("-" * 70)

    # Print each skill
    for skill in skills:
        name = skill["name"]
        slug = skill["slug"]
        desc = (skill["description"] or "")[:MAX_DESCRIPTION_LENGTH]

        # Get safety level from metadata
        safety = _extract_safety_level(skill.get("metadata", {}))
        safety_display = f"[{safety}]" if safety else ""

        print(f"{name:<22} {slug:<20} {desc}  {safety_display}")

    print(f"\n({len(skills)} skill(s) installed)")


def _extract_safety_level(metadata: Any) -> str:
    """Extract safety level from skill metadata."""
    if not isinstance(metadata, dict):
        return ""

    clawdbot = metadata.get("clawdbot", {})
    if not isinstance(clawdbot, dict):
        return ""

    return clawdbot.get("safety", "")


# ── Show Command ─────────────────────────────────────────────────────────

def show_skill(name: str) -> None:
    """
    Show detailed information about a skill.

    Args:
        name: Skill name or slug
    """
    if not name:
        print("[SKILL] Error: Skill name is required", file=sys.stderr)
        return

    skill_dir = find_skill_by_name(name)

    if not skill_dir or not skill_dir.exists():
        print(f"[SKILL] Not found: {name}")
        available = [s['slug'] for s in scan_skills()]
        if available:
            print(f"  Available skills: {', '.join(available)}")
        return

    # Parse and display skill details
    skill_md = skill_dir / "SKILL.md"

    try:
        data = parse_skill_md(skill_md)
    except Exception as e:
        print(f"[SKILL] Error parsing skill: {e}", file=sys.stderr)
        return

    # Display basic info
    print(f"Name:        {data['name']}")
    print(f"Slug:        {data['slug']}")
    print(f"Description: {data['description'] or '(none)'}")
    print(f"Path:        {skill_md}")

    # Display metadata
    _display_skill_metadata(data.get("metadata", {}))

    # Display bash commands
    bash_blocks = data.get("bash_blocks", [])
    print(f"\nBash commands: {len(bash_blocks)}")
    for i, block in enumerate(bash_blocks):
        cmd = block['command']
        preview = cmd[:80] + ('...' if len(cmd) > 80 else '')
        print(f"\n  [{i+1}] Command: {preview}")
        if block.get('params'):
            print(f"      Params: {', '.join(block['params'])}")

    # Check if currently loaded
    _show_loaded_status(data['slug'])


def _display_skill_metadata(metadata: Any) -> None:
    """Display skill metadata if present."""
    if not isinstance(metadata, dict):
        return

    clawdbot = metadata.get("clawdbot", {})
    if not isinstance(clawdbot, dict) or not clawdbot:
        return

    print(f"Safety:      {clawdbot.get('safety', 'KEYWORD_CHECK')}")
    requires = clawdbot.get("requires")
    if requires:
        print(f"Requires:    {requires}")


def _show_loaded_status(slug: str) -> None:
    """Show if skill is currently loaded as tools."""
    try:
        from .skill_manager import get_skills
        loaded = [t.name for t in get_skills()]
        tool_prefix = slug.replace("-", "_")
        tool_names = [n for n in loaded if n.startswith(tool_prefix)]
        if tool_names:
            print(f"\n  Loaded as tools: {', '.join(tool_names)}")
    except ImportError:
        pass


# ── Remove Command ───────────────────────────────────────────────────────

def remove_skill(name: str) -> None:
    """
    Remove an installed skill.

    Args:
        name: Skill name or slug to remove
    """
    if not name:
        print("[SKILL] Error: Skill name is required", file=sys.stderr)
        return

    skill_dir = find_skill_by_name(name)

    if not skill_dir or not skill_dir.exists():
        print(f"[SKILL] Not found: {name}")
        return

    # Confirm removal
    confirm = input(f"Remove skill '{skill_dir.name}'? [y/N] ").strip().lower()
    if confirm not in ('y', 'yes'):
        print("[SKILL] Cancelled")
        return

    # Remove directory
    shutil.rmtree(skill_dir)
    print(f"[SKILL] Removed: {skill_dir.name}")

    # Reload to deregister
    _reload_skills()


# ── Update Command ───────────────────────────────────────────────────────

def update_skill(name: str, source: Optional[str] = None) -> None:
    """
    Update a skill with new source.

    Args:
        name: Skill name or slug to update
        source: New source URL (required)
    """
    if not source:
        print("[SKILL] Update requires a source URL. Example:", file=sys.stderr)
        print(f"  python harness.py skill update {name} https://raw.githubusercontent.com/.../SKILL.md", file=sys.stderr)
        return

    # Check if skill exists
    skill_dir = find_skill_by_name(name)
    if not skill_dir:
        print(f"[SKILL] Skill not found: {name}", file=sys.stderr)
        return

    print(f"[SKILL] Updating '{name}' from: {source}")

    # Reuse install logic to overwrite
    install(source)


# ── Main CLI Entry Point ─────────────────────────────────────────────────

def main() -> None:
    """Main entry point for skill CLI."""
    # Configure stdout for UTF-8
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')

    parser = argparse.ArgumentParser(
        prog="python harness.py skill",
        description="NexusHarness Skill CLI — manage agent skills",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python harness.py skill list
  python harness.py skill install ./my-skill
  python harness.py skill install https://raw.githubusercontent.com/user/repo/main/skills/weather/SKILL.md
  python harness.py skill remove weather-skill
  python harness.py skill show weather-skill
        """
    )

    sub = parser.add_subparsers(dest="cmd", required=True, help="Command to execute")

    # List command
    sub.add_parser("list", help="List installed skills")

    # Install command
    install_parser = sub.add_parser("install", help="Install a skill")
    install_parser.add_argument("source", help="URL or local path to SKILL.md or skill directory")

    # Remove command
    remove_parser = sub.add_parser("remove", help="Remove an installed skill")
    remove_parser.add_argument("name", help="Skill name/slug to remove")

    # Show command
    show_parser = sub.add_parser("show", help="Show skill details")
    show_parser.add_argument("name", help="Skill name/slug")

    # Update command
    update_parser = sub.add_parser("update", help="Update a skill")
    update_parser.add_argument("name", help="Skill name/slug to update")
    update_parser.add_argument("source", nargs="?", help="New source URL (required)")

    # Parse arguments (skip script name and 'skill' subcommand)
    args = parser.parse_args(sys.argv[2:] if len(sys.argv) > 2 else [])

    # Execute command with error handling
    exit_code = EXIT_SUCCESS

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
    except ValueError as e:
        print(f"[SKILL] Input error: {e}", file=sys.stderr)
        exit_code = EXIT_INPUT_ERROR
    except HTTPError as e:
        print(f"[SKILL] Network error: {e}", file=sys.stderr)
        exit_code = EXIT_NETWORK_ERROR
    except (IOError, OSError) as e:
        print(f"[SKILL] File system error: {e}", file=sys.stderr)
        exit_code = EXIT_IO_ERROR
    except KeyboardInterrupt:
        print("\n[SKILL] Cancelled by user", file=sys.stderr)
        exit_code = EXIT_INPUT_ERROR
    except Exception as e:
        print(f"[SKILL] Unexpected error: {e}", file=sys.stderr)
        exit_code = EXIT_UNKNOWN_ERROR
        raise  # For debugging

    sys.exit(exit_code)


if __name__ == "__main__":
    main()