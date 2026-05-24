"""
Unit tests for skill_common module.
"""

import pytest
from pathlib import Path
from microharness.skills.skill_common import (
    parse_frontmatter,
    extract_bash_blocks,
    process_bash_block,
    make_slug,
    is_html_response,
    normalize_github_url,
    extract_slug_from_content,
    slug_from_url,
    FRONTMATTER_SPLIT_LIMIT,
    MAX_COMMAND_LENGTH,
)


class TestParseFrontmatter:
    """Tests for parse_frontmatter function."""

    def test_valid_frontmatter(self):
        content = """---
name: Test Skill
description: A test skill
---
# Body
"""
        result = parse_frontmatter(content)
        assert result["name"] == "Test Skill"
        assert result["description"] == "A test skill"

    def test_empty_content(self):
        assert parse_frontmatter("") == {}
        assert parse_frontmatter("# No frontmatter") == {}

    def test_no_frontmatter(self):
        content = "# Just body\nNo frontmatter here"
        assert parse_frontmatter(content) == {}


class TestMakeSlug:
    """Tests for make_slug function."""

    def test_basic_conversion(self):
        assert make_slug("Weather Skill") == "weather-skill"
        assert make_slug("Hello World") == "hello-world"

    def test_underscore_replacement(self):
        assert make_slug("weather_skill") == "weather-skill"

    def test_case_normalization(self):
        assert make_slug("Weather Skill") == "weather-skill"
        assert make_slug("WEATHER SKILL") == "weather-skill"


class TestExtractBashBlocks:
    """Tests for extract_bash_blocks function."""

    def test_single_bash_block(self):
        body = """
Some text
```bash
echo hello
```
More text
"""
        blocks = extract_bash_blocks(body)
        assert len(blocks) == 1
        assert blocks[0].command == "echo hello"
        assert blocks[0].params == []

    def test_multiple_bash_blocks(self):
        body = """
```bash
echo one
```
```bash
echo two
```
"""
        blocks = extract_bash_blocks(body)
        assert len(blocks) == 2

    def test_no_bash_blocks(self):
        body = "Just regular text"
        assert extract_bash_blocks(body) == []

    def test_bash_block_with_params(self):
        body = """
```bash
curl http://{url}
```
"""
        blocks = extract_bash_blocks(body)
        assert len(blocks) == 1
        assert "url" in blocks[0].params


class TestProcessBashBlock:
    """Tests for process_bash_block function."""

    def test_basic_command(self):
        result = process_bash_block("echo hello")
        assert result is not None
        assert result.command == "echo hello"

    def test_output_marker_filtered(self):
        raw = """echo hello
# Output: some result"""
        result = process_bash_block(raw)
        assert result is not None
        assert "# Output:" not in result.command

    def test_empty_block(self):
        assert process_bash_block("") is None
        assert process_bash_block("# just a comment") is None

    def test_param_extraction(self):
        raw = "curl http://{url} --data {data}"
        result = process_bash_block(raw)
        assert result is not None
        assert set(result.params) == {"url", "data"}

    def test_chinese_param_filtered(self):
        raw = "echo <用户参数>"
        result = process_bash_block(raw)
        assert result is not None
        assert "<用户参数>" not in result.command


class TestIsHtmlResponse:
    """Tests for is_html_response function."""

    def test_doctype_html(self):
        assert is_html_response("<!DOCTYPE html><html>") == True

    def test_html_tag(self):
        assert is_html_response("<html><body></body></html>") == True

    def test_markdown_not_html(self):
        assert is_html_response("# Hello") == False
        assert is_html_response("```bash\necho hi\n```") == False


class TestNormalizeGithubUrl:
    """Tests for normalize_github_url function."""

    def test_tree_url_conversion(self):
        url = "https://github.com/user/repo/tree/main/skills/weather"
        result = normalize_github_url(url)
        assert "raw.githubusercontent.com" in result
        assert result.endswith("/SKILL.md")

    def test_blob_url_conversion(self):
        url = "https://github.com/user/repo/blob/main/skills/weather/SKILL.md"
        result = normalize_github_url(url)
        assert "raw.githubusercontent.com" in result

    def test_already_raw_url(self):
        url = "https://raw.githubusercontent.com/user/repo/main/skills/weather/SKILL.md"
        assert normalize_github_url(url) == url


class TestExtractSlugFromContent:
    """Tests for extract_slug_from_content function."""

    def test_slug_from_name(self):
        content = """---
name: Weather Skill
---
"""
        assert extract_slug_from_content(content) == "weather-skill"

    def test_no_name_returns_none(self):
        content = """---
description: Just a desc
---
"""
        assert extract_slug_from_content(content) is None


class TestSlugFromUrl:
    """Tests for slug_from_url function."""

    def test_extracts_last_component(self):
        url = "https://example.com/path/to/weather-SKILL.md"
        assert slug_from_url(url) == "weather-skillmd"

    def test_unnamed_fallback(self):
        assert slug_from_url("no-slashes") == "unnamed"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])