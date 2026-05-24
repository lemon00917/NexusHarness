"""
Skill Common Module
====================
Shared utilities for skill management in NexusHarness.

This module provides common functions used by both:
- skill_manager.py: Tool registration and execution
- skill_cli.py: Command-line skill management

Features:
- YAML frontmatter parsing
- Bash code block extraction
- SKILL.md file parsing
- URL normalization and content fetching
- Logging utilities

Usage:
    from microharness.skills.skill_common import (
        parse_frontmatter,
        extract_bash_blocks,
        parse_skill_md,
        make_slug,
        logger,
    )
"""

import logging
import re
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple
from urllib.error import HTTPError

# ── Constants ────────────────────────────────────────────────────────────

FRONTMATTER_SPLIT_LIMIT = 2
MAX_COMMAND_LENGTH = 10000
PATH_PLACEHOLDER = "/path/to/skills/"
SKILL_COMMAND_TIMEOUT = 30
DEFAULT_TIMEOUT = 15  # seconds
MAX_REDIRECTS = 5
MAX_DESCRIPTION_LENGTH = 40
MAX_RETRIES = 3
RETRY_DELAY_BASE = 1.0  # seconds

# Safety level constants
SAFETY_AUTO_APPROVE = "AUTO_APPROVE"
SAFETY_ALWAYS_CONFIRM = "ALWAYS_CONFIRM"
SAFETY_KEYWORD_CHECK = "KEYWORD_CHECK"
VALID_SAFETY_LEVELS = {SAFETY_AUTO_APPROVE, SAFETY_ALWAYS_CONFIRM, SAFETY_KEYWORD_CHECK}

# User agent for HTTP requests
USER_AGENT = "NexusHarness Skill Manager/1.0"

# ── Regex Patterns (precompiled for performance) ──────────────────────

YAML_FRONTMATTER_PATTERN = re.compile(r'^---\s*\n(.*?)\n---', re.DOTALL | re.MULTILINE)
BASH_BLOCK_PATTERN = re.compile(r'```bash\s+(.*?)\s```', re.DOTALL)

# GitHub URL patterns
GITHUB_TREE_PATTERN = re.compile(r'https://github\.com/([^/]+)/([^/]+)/tree/([^/]+)/(.+)')
GITHUB_BLOB_PATTERN = re.compile(r'https://github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.+)')
GITHUB_RAW_PATTERN = re.compile(r'https://raw\.githubusercontent\.com/([^/]+)/([^/]+)/([^/]+)/(.+)')

# ── Data Classes ────────────────────────────────────────────────────────

@dataclass
class BashBlock:
    """Represents a parsed bash code block from SKILL.md."""
    command: str
    params: List[str] = field(default_factory=list)
    raw_content: Optional[str] = None

    def validate(self) -> Tuple[bool, Optional[str]]:
        """Validate the bash block for potential issues."""
        if not self.command:
            return False, "Empty command"

        # Check for dangerous patterns
        dangerous = [
            (r'rm\s+-rf\s+/', "Dangerous: rm -rf /"),
            (r'>\s*/dev/sda', "Dangerous: writing to disk device"),
        ]
        for pattern, warning in dangerous:
            if re.search(pattern, self.command):
                return False, warning

        return True, None


@dataclass
class SkillData:
    """Represents parsed skill data from SKILL.md."""
    name: str
    description: str
    slug: str
    path: Path
    bash_blocks: List[BashBlock] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def safety_level(self) -> str:
        """Extract safety level from metadata."""
        clawdbot = self.metadata.get("clawdbot", {})
        if not isinstance(clawdbot, dict):
            return SAFETY_KEYWORD_CHECK
        safety = clawdbot.get("safety", "")
        return safety if safety in VALID_SAFETY_LEVELS else SAFETY_KEYWORD_CHECK

    @property
    def tool_names(self) -> List[str]:
        """Generate tool names for each bash block."""
        base = self.slug.replace("-", "_")
        if not self.bash_blocks:
            return []
        return [base] + [f"{base}_{i+1}" for i in range(1, len(self.bash_blocks))]

# ── Logging Setup ────────────────────────────────────────────────────────

logger = logging.getLogger(__name__)


def setup_logging(level: int = logging.INFO) -> None:
    """
    Configure logging for skill modules.

    Args:
        level: Logging level (default: INFO)
    """
    logging.basicConfig(
        format='[%(name)s] %(levelname)s: %(message)s',
        level=level
    )
    logging.getLogger('microharness.skills').setLevel(level)


# ── YAML Frontmatter Parsing ─────────────────────────────────────────────

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False
    yaml = None  # type: ignore


def parse_frontmatter(content: str) -> Dict[str, Any]:
    """
    Extract and parse YAML frontmatter from SKILL.md content.

    Args:
        content: Full SKILL.md content

    Returns:
        Parsed frontmatter as dictionary, empty dict if none found or parse fails
    """
    if not content:
        return {}

    match = YAML_FRONTMATTER_PATTERN.search(content)
    if not match:
        return {}

    if not HAS_YAML:
        logger.debug("YAML module not available, skipping frontmatter parsing")
        return {}

    try:
        frontmatter_text = match.group(1)
        result = yaml.safe_load(frontmatter_text)  # type: ignore
        if not isinstance(result, dict):
            logger.warning(f"Frontmatter parsed as {type(result)}, expected dict")
            return {}
        return result
    except Exception as e:
        logger.debug(f"Failed to parse frontmatter: {e}")
        return {}


# ── Slug Generation ──────────────────────────────────────────────────────

def make_slug(name: str) -> str:
    """
    Convert skill name to directory slug.

    Args:
        name: Skill name (e.g., "Weather Skill")

    Returns:
        Slug (e.g., "weather-skill")
    """
    if not name:
        return "unnamed"

    # Convert to lowercase and replace separators
    slug = name.lower().replace(" ", "-").replace("_", "-")

    # Remove any non-alphanumeric characters (except hyphens)
    slug = re.sub(r'[^a-z0-9-]', '', slug)

    # Remove duplicate hyphens
    slug = re.sub(r'-+', '-', slug)

    # Remove leading/trailing hyphens
    return slug.strip('-')


# ── Bash Block Processing ────────────────────────────────────────────────

def extract_bash_blocks(body: str) -> List[BashBlock]:
    """
    Extract executable bash blocks from markdown body.

    Args:
        body: Markdown body text

    Returns:
        List of BashBlock objects
    """
    bash_blocks = []

    for match in BASH_BLOCK_PATTERN.finditer(body):
        raw_cmd = match.group(1).strip()
        block = process_bash_block(raw_cmd)
        if block:
            bash_blocks.append(block)

    return bash_blocks


def process_bash_block(raw_cmd: str) -> Optional[BashBlock]:
    """
    Process a single bash code block.

    Args:
        raw_cmd: Raw bash command text

    Returns:
        BashBlock object or None if invalid/empty
    """
    if not raw_cmd or not raw_cmd.strip():
        return None

    # Filter out comment lines and empty lines
    cmd_lines = []
    for line in raw_cmd.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # Skip output markers and pure comment lines
        if stripped.startswith("# Output:"):
            continue
        cmd_lines.append(line)

    # Skip blocks with no actual commands
    if not cmd_lines:
        return None

    # Check if all lines are comments or paths
    has_command = False
    for line in cmd_lines:
        stripped = line.strip()
        if stripped and not stripped.startswith('#') and not stripped.startswith('/'):
            has_command = True
            break

    if not has_command:
        return None

    # Join and clean command
    cmd = "\n".join(cmd_lines)

    # Remove parameter placeholder syntax like <用户参数>
    cmd = re.sub(r'<\w+>', '', cmd).strip()

    # Check command length
    if len(cmd) > MAX_COMMAND_LENGTH:
        logger.warning(f"Command too long ({len(cmd)} chars), truncating to {MAX_COMMAND_LENGTH}")
        cmd = cmd[:MAX_COMMAND_LENGTH]

    # Extract {param} placeholders
    params = list(set(re.findall(r'\{(\w+)\}', cmd)))

    # Create and validate block
    block = BashBlock(command=cmd, params=params, raw_content=raw_cmd)
    is_valid, error = block.validate()

    if not is_valid:
        logger.warning(f"Invalid bash block: {error}")
        return None

    return block if cmd else None


# ── SKILL.md Parsing ─────────────────────────────────────────────────────

def parse_skill_md(path: Path) -> SkillData:
    """
    Parse SKILL.md file.

    Args:
        path: Path to SKILL.md file

    Returns:
        SkillData object containing skill metadata and bash blocks

    Raises:
        FileNotFoundError: If file doesn't exist
        ValueError: If SKILL.md format is invalid
    """
    if not path.exists():
        raise FileNotFoundError(f"SKILL.md not found: {path}")

    content = path.read_text(encoding="utf-8")

    # Split frontmatter from body
    parts = content.split("---", FRONTMATTER_SPLIT_LIMIT)
    if len(parts) < 3:
        raise ValueError(
            f"Invalid SKILL.md format in {path.name}:\n"
            f"  Expected frontmatter delimited by '---' at the beginning.\n"
            f"  Found {len(parts)} sections, expected at least 3."
        )

    body = parts[2].strip()

    # Parse frontmatter
    fm = parse_frontmatter(content)

    # Extract bash blocks
    bash_blocks = extract_bash_blocks(body)
    slug = path.parent.name

    return SkillData(
        name=fm.get("name", slug),
        description=fm.get("description", ""),
        slug=slug,
        path=path,
        bash_blocks=bash_blocks,
        metadata=fm.get("metadata", {}),
    )


# ── HTML Detection ───────────────────────────────────────────────────────

def is_html_response(content: str) -> bool:
    """
    Check if content is an HTML response (not raw markdown).

    Args:
        content: Response content to check

    Returns:
        True if content appears to be HTML
    """
    if not content:
        return False
    stripped = content.strip()[:1000]  # Check only first 1000 chars
    return stripped.startswith('<!DOCTYPE') or stripped.startswith('<html')


# ── URL Normalization ────────────────────────────────────────────────────

def normalize_github_url(url: str) -> str:
    """
    Normalize GitHub URLs to raw content URLs.

    Handles:
    - GitHub tree URLs -> raw.githubusercontent.com
    - GitHub blob URLs -> raw.githubusercontent.com
    - Already raw URLs -> unchanged

    Args:
        url: GitHub URL

    Returns:
        Normalized raw URL
    """
    if not url:
        return url

    # Check if already a raw URL
    if 'raw.githubusercontent.com' in url:
        return url

    # Convert tree URL to raw
    tree_match = GITHUB_TREE_PATTERN.match(url)
    if tree_match:
        user, repo, branch, path = tree_match.groups()
        return f"https://raw.githubusercontent.com/{user}/{repo}/{branch}/{path}/SKILL.md"

    # Convert blob URL to raw
    blob_match = GITHUB_BLOB_PATTERN.match(url)
    if blob_match:
        user, repo, branch, path = blob_match.groups()
        return f"https://raw.githubusercontent.com/{user}/{repo}/{branch}/{path}"

    return url


def fetch_url_content(url: str, timeout: int = DEFAULT_TIMEOUT) -> Tuple[str, str]:
    """
    Fetch content from URL with redirect handling.

    Args:
        url: URL to fetch
        timeout: Request timeout in seconds

    Returns:
        Tuple of (content, final_url) after all redirects

    Raises:
        ValueError: If response is HTML (GitHub view page)
        HTTPError: On HTTP errors
    """
    if not url:
        raise ValueError("URL cannot be empty")

    last_url = url

    for redirect_count in range(MAX_REDIRECTS):
        req = urllib.request.Request(
            last_url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/markdown,text/plain,*/*",
            }
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                content = resp.read().decode("utf-8")
                final_url = resp.geturl()

                # Check if we got HTML instead of markdown
                if is_html_response(content):
                    if 'raw.githubusercontent.com' not in final_url and '/raw/' not in final_url:
                        raise ValueError(
                            f"URL appears to be a GitHub view page, not raw content.\n"
                            f"  Use the raw URL format: https://raw.githubusercontent.com/...\n"
                            f"  Provided URL: {url}"
                        )

                return content, final_url

        except HTTPError as e:
            if e.code in (301, 302, 303, 307, 308) and 'Location' in e.headers:
                last_url = e.headers['Location']
                logger.debug(f"Redirecting to: {last_url}")
                continue
            raise

    raise ValueError(f"Too many redirects ({MAX_REDIRECTS}) fetching {url}")


def fetch_url_content_with_retry(
    url: str,
    timeout: int = DEFAULT_TIMEOUT,
    max_retries: int = MAX_RETRIES,
    retry_delay: float = RETRY_DELAY_BASE
) -> Tuple[str, str]:
    """
    Fetch URL content with automatic retry on transient errors.

    Args:
        url: URL to fetch
        timeout: Request timeout in seconds
        max_retries: Maximum number of retry attempts
        retry_delay: Base delay between retries (exponential backoff)

    Returns:
        Tuple of (content, final_url)

    Raises:
        Exception: After exhausting all retries
    """
    for attempt in range(max_retries):
        try:
            return fetch_url_content(url, timeout)
        except (HTTPError, ConnectionError, TimeoutError) as e:
            # Retry on server errors (5xx) and network issues
            is_retryable = False
            if isinstance(e, HTTPError) and e.code >= 500:
                is_retryable = True
            elif isinstance(e, (ConnectionError, TimeoutError)):
                is_retryable = True

            if is_retryable and attempt < max_retries - 1:
                delay = retry_delay * (2 ** attempt)  # Exponential backoff
                logger.warning(f"Fetch failed (attempt {attempt + 1}/{max_retries}): {e}")
                logger.debug(f"Retrying in {delay:.1f}s...")
                import time
                time.sleep(delay)
                continue
            raise

    raise ValueError(f"Max retries ({max_retries}) exceeded for {url}")


def extract_slug_from_content(content: str) -> Optional[str]:
    """
    Extract skill slug from frontmatter name.

    Args:
        content: SKILL.md content

    Returns:
        Slug string or None if not found
    """
    fm = parse_frontmatter(content)
    if fm and fm.get("name"):
        return make_slug(fm["name"])
    return None


def slug_from_url(url: str) -> str:
    """
    Extract slug from URL path.

    Args:
        url: URL to extract from

    Returns:
        Slug string or "unnamed" if extraction fails
    """
    if not url or "/" not in url:
        return "unnamed"

    # Remove trailing slash and get last path component
    normalized = url.rstrip('/')
    slug = normalized.rsplit('/', 2)[-1]

    # Clean up slug
    return make_slug(slug) if slug else "unnamed"


# ── Install Helpers ─────────────────────────────────────────────────────

def install_from_url(url: str, use_retry: bool = True) -> Tuple[str, str]:
    """
    Download and install SKILL.md from a URL.

    Args:
        url: URL to SKILL.md or GitHub skill page
        use_retry: Whether to use automatic retry on failures

    Returns:
        Tuple of (slug, content)

    Raises:
        ValueError: If URL is invalid or returns HTML
        HTTPError: On HTTP errors
    """
    url = url.strip()
    if not url:
        raise ValueError("URL cannot be empty")

    # Normalize GitHub URLs
    normalized_url = normalize_github_url(url)
    if normalized_url != url:
        logger.debug(f"Normalized URL: {normalized_url}")

    # Fetch content
    fetcher = fetch_url_content_with_retry if use_retry else fetch_url_content
    content, final_url = fetcher(normalized_url)

    # Extract slug
    slug = extract_slug_from_content(content) or slug_from_url(url)
    logger.debug(f"Extracted slug: {slug}")

    return slug, content


def install_from_local(path: Path) -> Tuple[str, str]:
    """
    Install from a local skill directory or SKILL.md file.

    Args:
        path: Path to local file or directory

    Returns:
        Tuple of (slug, content)

    Raises:
        ValueError: If path is invalid or no SKILL.md found
        FileNotFoundError: If path doesn't exist
    """
    if not path:
        raise ValueError("Path cannot be empty")

    resolved_path = Path(path).expanduser().resolve()

    if not resolved_path.exists():
        raise FileNotFoundError(f"Path does not exist: {resolved_path}")

    if resolved_path.is_file():
        content = resolved_path.read_text(encoding="utf-8")
        slug = extract_slug_from_content(content) or resolved_path.parent.name
    elif resolved_path.is_dir():
        skill_md = resolved_path / "SKILL.md"
        if not skill_md.exists():
            raise ValueError(f"No SKILL.md found in {resolved_path}")
        content = skill_md.read_text(encoding="utf-8")
        slug = extract_slug_from_content(content) or resolved_path.name
    else:
        raise ValueError(f"Invalid path (not file or directory): {resolved_path}")

    # Validate content
    if not content or not content.strip():
        raise ValueError("Skill content is empty")

    return slug, content


# ── Module Initialization ───────────────────────────────────────────────

# Validate configuration on import
def _validate_config() -> None:
    """Validate configuration constants."""
    assert DEFAULT_TIMEOUT > 0, "DEFAULT_TIMEOUT must be positive"
    assert MAX_REDIRECTS > 0, "MAX_REDIRECTS must be positive"
    assert MAX_COMMAND_LENGTH > 0, "MAX_COMMAND_LENGTH must be positive"
    assert MAX_RETRIES > 0, "MAX_RETRIES must be positive"
    assert MAX_DESCRIPTION_LENGTH > 0, "MAX_DESCRIPTION_LENGTH must be positive"


_validate_config()