#!/usr/bin/env python3
"""
Create Jira User Story + Subtask issue(s) from local Markdown record(s).

This script reads one or more `.md` file(s) that follow:
  `ga-jira/.cursor/rules/local-jira-records-story-subtask.mdc`

It then creates the User Story and Subtask issue(s) in Jira via Jira REST API v3
and updates the local `.md` file(s) with the created jira_key values.

Required environment variables (same as `ga-jira/mcp.json`):
  - JIRA_BASE_URL   (e.g. "https://gnapartners.atlassian.net")
  - JIRA_EMAIL
  - JIRA_API_TOKEN

Usage:
  python3 create_story_in_jira.py file1.md file2.md [--dry-run]
  python3 create_story_in_jira.py current/Namespacing/Security/*.md [--dry-run]
"""

from __future__ import annotations

import argparse
import base64
import glob
import json
import os
import re
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

try:
    import urllib.request
    import urllib.parse
except Exception:  # pragma: no cover
    urllib = None  # type: ignore


# Jira custom field IDs
CF_TEAM = "customfield_10001"
CF_SPRINT = "customfield_10020"
CF_STORY_POINTS_STORY = "customfield_10037"  # Story story points
CF_STORY_POINTS_SUBTASK_PRIMARY = "customfield_10016"  # Subtask story points
CF_PARENT_LINK = "customfield_10014"  # Feature parent for Stories
CF_PM_OWNER = "customfield_10246"
CF_ACCEPTANCE = "customfield_10256"
CF_TECH_NOTES = "customfield_10356"
CF_QA_TEST = "customfield_10462"

# Issue type IDs
ISSUE_TYPE_STORY = "10007"
ISSUE_TYPE_SUBTASK = "10184"
PROJECT_KEY = "WOR"


@dataclass(frozen=True)
class StoryMeta:
    jira_key: str  # Empty for DRAFT files
    parent_key: str  # Feature key
    status: str
    team_id: str
    sprint_id: str
    pm_owner: str
    assignee: str
    story_points: str
    labels: List[str]


@dataclass(frozen=True)
class SubtaskMeta:
    jira_key: str  # Empty for DRAFT files
    parent_key: str  # Story key (must match story.jira_key after creation)
    status: str
    team_id: str
    sprint_id: str
    assignee: str
    story_points: str


@dataclass(frozen=True)
class ParsedRecord:
    story: StoryMeta
    subtask: SubtaskMeta
    story_summary: str
    subtask_summary: str
    story_adf: Dict[str, Any]
    subtask_adf: Dict[str, Any]


class JiraApiError(RuntimeError):
    pass


def load_dotenv(dotenv_path: str, *, override: bool = False) -> bool:
    """
    Minimal .env loader (no external deps).

    Supports lines like:
      KEY=value
      export KEY=value
      KEY="value with spaces"
      KEY='value'

    Ignores blank lines and comments (# ...).

    Returns True if file was found + parsed, else False.
    """
    path = dotenv_path
    if not path or not os.path.exists(path) or not os.path.isfile(path):
        return False

    with open(path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export ") :].lstrip()
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if not key:
                continue

            # Remove inline comments for unquoted values (KEY=value # comment)
            if value and not (value.startswith('"') or value.startswith("'")) and " #" in value:
                value = value.split(" #", 1)[0].rstrip()

            # Strip surrounding quotes
            if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
                value = value[1:-1]

            if override or key not in os.environ:
                os.environ[key] = value

    return True


def _auto_load_dotenv(explicit_path: Optional[str] = None) -> None:
    """
    Attempt to load env vars from .env if present.

    Search order:
      1) explicit_path (if provided)
      2) <script_dir>/.env
      3) <cwd>/.env
      4) <repo_root>/.env  (repo_root assumed to be script_dir's parent)
    """
    tried: List[str] = []

    def _try(path: str) -> bool:
        if not path:
            return False
        tried.append(path)
        return load_dotenv(path, override=False)

    if explicit_path and _try(explicit_path):
        return

    script_dir = os.path.dirname(os.path.abspath(__file__))
    if _try(os.path.join(script_dir, ".env")):
        return

    if _try(os.path.join(os.getcwd(), ".env")):
        return

    repo_root = os.path.dirname(script_dir)
    _try(os.path.join(repo_root, ".env"))


def _env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise JiraApiError(f"Missing required env var: {name}")
    return val


def _basic_auth_header(email: str, token: str) -> str:
    raw = f"{email}:{token}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _jira_request(
    method: str,
    path: str,
    *,
    query: Optional[Dict[str, str]] = None,
    body: Optional[Dict[str, Any]] = None,
    timeout_sec: int = 30,
) -> Dict[str, Any]:
    if urllib is None:
        raise JiraApiError("urllib is not available in this Python environment")

    base_url = _env("JIRA_BASE_URL").rstrip("/")
    email = _env("JIRA_EMAIL")
    token = _env("JIRA_API_TOKEN")

    url = base_url + path
    if query:
        url += "?" + urllib.parse.urlencode(query)

    data = None
    headers = {
        "Authorization": _basic_auth_header(email, token),
        "Accept": "application/json",
    }
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, method=method.upper(), headers=headers, data=data)
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:  # type: ignore[attr-defined]
        raw = e.read().decode("utf-8") if hasattr(e, "read") else ""
        msg = f"Jira API {method} {path} failed: HTTP {getattr(e, 'code', '?')}"
        if raw:
            msg += f"\n{raw}"
        raise JiraApiError(msg) from e


def jira_create_issue(fields: Dict[str, Any]) -> Dict[str, Any]:
    """Create a new issue in Jira."""
    return _jira_request("POST", "/rest/api/3/issue", body={"fields": fields})


def jira_update_issue_fields(issue_key: str, fields: Dict[str, Any]) -> None:
    """Update fields on an existing issue."""
    _jira_request("PUT", f"/rest/api/3/issue/{issue_key}", body={"fields": fields})


def jira_find_user_account_id(query: str, *, allow_network: bool = True) -> Optional[str]:
    """
    Resolve an accountId from an email or free-form query using /user/search.

    Returns:
      - accountId string if found
      - None if not found
    """
    q = query.strip()
    if not q:
        return None
    # If caller already passed an accountId-like value, accept it.
    # Atlassian accountIds often look like "712020:..." or long strings.
    if re.match(r"^[A-Za-z0-9:_-]{10,}$", q) and "@" not in q:
        return q

    if not allow_network:
        return None

    users = _jira_request("GET", "/rest/api/3/user/search", query={"query": q, "maxResults": "20"})
    if not isinstance(users, list):
        return None

    # Prefer exact email match if available
    for u in users:
        if isinstance(u, dict) and u.get("emailAddress") and u.get("emailAddress").lower() == q.lower():
            return u.get("accountId")

    # Otherwise pick the first result
    for u in users:
        if isinstance(u, dict) and u.get("accountId"):
            return u.get("accountId")
    return None


def _extract_front_matter_block(md: str) -> str:
    """
    Return the YAML-like front matter block for our combo records.

    Supports both formats:
      - Proper YAML front matter: starts with '---' and ends with the next '---'
      - Legacy: starts at line 1 with 'user_story:' and ends at the first '---'
    """
    if md.lstrip().startswith("---"):
        m = re.search(r"^---\s*\n(.*?)\n---\s*\n", md, flags=re.DOTALL)
        if not m:
            raise ValueError("Could not find terminating '---' for YAML front matter")
        return m.group(1)

    # Legacy: everything from start until first '---' line
    m = re.search(r"^(.*?)(?:\n---\s*\n)", md, flags=re.DOTALL)
    if not m:
        raise ValueError("Could not find YAML front matter terminator ('---')")
    return m.group(1)


def _parse_inline_list(value: str) -> List[str]:
    v = value.strip()
    if v == "[]":
        return []
    # Very small subset: ["a","b"] or [a, b]
    if v.startswith("[") and v.endswith("]"):
        inner = v[1:-1].strip()
        if not inner:
            return []
        parts = [p.strip() for p in inner.split(",")]
        out: List[str] = []
        for p in parts:
            p = p.strip()
            if p.startswith('"') and p.endswith('"'):
                p = p[1:-1]
            if p.startswith("'") and p.endswith("'"):
                p = p[1:-1]
            if p:
                out.append(p)
        return out
    return [v]


def _parse_section_kv(block: str, section_name: str) -> Dict[str, str]:
    """
    Extract indented key/value lines for a named top-level section (user_story / subtask).
    This is NOT a general YAML parser; it's just enough for our repo's fixed template.
    """
    # Find the section header at beginning of a line
    m = re.search(rf"(?m)^{re.escape(section_name)}:\s*\n(.*?)(?=^\S|\Z)", block, flags=re.DOTALL)
    if not m:
        raise ValueError(f"Missing '{section_name}:' section in front matter")
    body = m.group(1)

    out: Dict[str, str] = {}
    for line in body.splitlines():
        # strip comments
        line_no_comment = line.split("#", 1)[0].rstrip()
        if not line_no_comment.strip():
            continue
        # expect "  key: value"
        kv = re.match(r"^\s+([A-Za-z0-9_]+)\s*:\s*(.*)$", line_no_comment)
        if not kv:
            continue
        k = kv.group(1)
        v = kv.group(2).strip()
        # dequote
        if v.startswith('"') and v.endswith('"'):
            v = v[1:-1]
        if v.startswith("'") and v.endswith("'"):
            v = v[1:-1]
        out[k] = v
    return out


def _extract_adf_blocks(md: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    Extract ADF JSON blocks from the markdown for the story and subtask.

    We key by Jira field id (customfield_10256, customfield_10356, customfield_10462).
    """
    story_fields: Dict[str, Any] = {}
    subtask_fields: Dict[str, Any] = {}

    current: Optional[str] = None  # "story" | "subtask"
    lines = md.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if re.match(r"^#\s+User Story\s*$", line):
            current = "story"
        elif re.match(r"^#\s+Subtask\s*$", line):
            current = "subtask"

        m = re.search(r"ADF Version\s+\((customfield_\d+)\)", line)
        if current and m:
            field_id = m.group(1)
            # Find opening ```json fence
            j = i + 1
            while j < len(lines) and lines[j].strip() != "```json":
                j += 1
            if j >= len(lines):
                i += 1
                continue
            # Capture until closing fence
            k = j + 1
            json_lines: List[str] = []
            while k < len(lines) and lines[k].strip() != "```":
                json_lines.append(lines[k])
                k += 1
            if k >= len(lines):
                i += 1
                continue
            raw = "\n".join(json_lines).strip()
            if raw:
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError as e:
                    raise ValueError(f"Invalid JSON in ADF block for {field_id} ({current}): {e}") from e
                if current == "story":
                    story_fields[field_id] = parsed
                else:
                    subtask_fields[field_id] = parsed
            i = k  # jump to closing fence
        i += 1

    return story_fields, subtask_fields


def _extract_summary_from_section(md: str, section_name: str) -> str:
    """Extract summary/title from a section (User Story or Subtask)."""
    # Look for ## Summary or ## Acceptance Criteria (which often contains the title)
    # In User Story section, look for Acceptance Criteria
    # In Subtask section, look for Implementation Steps or Acceptance Criteria
    if section_name == "story":
        pattern = r"^#\s+User Story\s*\n(.*?)(?=^#\s+Subtask|\Z)"
    else:
        pattern = r"^#\s+Subtask\s*\n(.*?)(?=\Z)"
    
    section_match = re.search(pattern, md, re.MULTILINE | re.DOTALL)
    if not section_match:
        return ""
    
    section_content = section_match.group(1)
    
    # Try to find Acceptance Criteria - get the first paragraph after the heading
    ac_match = re.search(r"^##\s+Acceptance Criteria\s*\n(.*?)(?=\n##|\n<details>|\Z)", section_content, re.MULTILINE | re.DOTALL)
    if ac_match:
        ac_content = ac_match.group(1).strip()
        # Get first non-empty line, up to 255 chars
        for line in ac_content.split('\n'):
            line = line.strip()
            if line and not line.startswith('#') and not line.startswith('>') and not line.startswith('<'):
                # Remove markdown formatting
                line = re.sub(r'\*\*([^*]+)\*\*', r'\1', line)  # Remove bold
                line = re.sub(r'\*([^*]+)\*', r'\1', line)  # Remove italic
                line = re.sub(r'`([^`]+)`', r'\1', line)  # Remove code
                if line:
                    return line[:255]  # Jira summary limit
    
    # Fallback: use filename or first non-empty line
    return ""


def _parse_combo_record(md: str) -> ParsedRecord:
    """Parse a User Story + Subtask combo markdown file."""
    fm = _extract_front_matter_block(md)
    us = _parse_section_kv(fm, "user_story")
    st = _parse_section_kv(fm, "subtask")

    labels = _parse_inline_list(us.get("labels", "[]"))

    story = StoryMeta(
        jira_key=us.get("jira_key", "").strip(),
        parent_key=us.get("parent_key", "").strip(),
        status=us.get("status", "").strip(),
        team_id=us.get("team_id", "").strip(),
        sprint_id=us.get("sprint_id", "").strip(),
        pm_owner=us.get("pm_owner", "").strip(),
        assignee=us.get("assignee", "").strip(),
        story_points=us.get("story_points", "").strip(),
        labels=labels,
    )
    subtask = SubtaskMeta(
        jira_key=st.get("jira_key", "").strip(),
        parent_key=st.get("parent_key", "").strip(),
        status=st.get("status", "").strip(),
        team_id=st.get("team_id", "").strip(),
        sprint_id=st.get("sprint_id", "").strip(),
        assignee=st.get("assignee", "").strip(),
        story_points=st.get("story_points", "").strip(),
    )

    if not story.parent_key:
        raise ValueError("user_story.parent_key (Feature key) is required")
    
    # Extract summaries - try to get from Acceptance Criteria or use filename
    story_summary = _extract_summary_from_section(md, "story")
    subtask_summary = _extract_summary_from_section(md, "subtask")
    
    # If no summary found, try to extract from filename
    if not story_summary:
        # This will be set from filename if needed
        story_summary = ""

    story_adf, subtask_adf = _extract_adf_blocks(md)
    return ParsedRecord(
        story=story,
        subtask=subtask,
        story_summary=story_summary,
        subtask_summary=subtask_summary,
        story_adf=story_adf,
        subtask_adf=subtask_adf,
    )


def _maybe_int(v: str) -> Optional[int]:
    s = v.strip()
    if not s:
        return None
    if not re.match(r"^-?\d+$", s):
        return None
    return int(s)


def _maybe_float(v: str) -> Optional[float]:
    """Parse a string as a float, returning None if invalid. Used for story points."""
    s = v.strip()
    if not s:
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _normalize_adf_marks(adf: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalize ADF by removing 'strong' mark when 'code' mark is present.
    Jira doesn't accept combined marks [code, strong] - only [code] is allowed.
    """
    import copy
    normalized = copy.deepcopy(adf)
    
    def normalize_node(node: Any) -> None:
        if isinstance(node, dict):
            if 'marks' in node and isinstance(node['marks'], list):
                # If both code and strong are present, keep only code
                has_code = any(m.get('type') == 'code' for m in node['marks'])
                has_strong = any(m.get('type') == 'strong' for m in node['marks'])
                if has_code and has_strong:
                    node['marks'] = [m for m in node['marks'] if m.get('type') == 'code']
            # Recursively process children
            for key, value in node.items():
                if isinstance(value, (dict, list)):
                    normalize_node(value)
        elif isinstance(node, list):
            for item in node:
                normalize_node(item)
    
    normalize_node(normalized)
    return normalized


def _build_story_create_fields(record: ParsedRecord, *, allow_network: bool) -> Dict[str, Any]:
    """Build fields for creating a Story issue."""
    s = record.story
    fields: Dict[str, Any] = {
        "project": {"key": PROJECT_KEY},
        "issuetype": {"id": ISSUE_TYPE_STORY},
    }
    
    # Summary - use provided or extract from filename
    summary = record.story_summary
    if not summary:
        # Fallback: use a default based on parent
        summary = f"Story for {s.parent_key}"
    if len(summary) > 255:
        summary = summary[:252] + "..."
    fields["summary"] = summary
    
    # Parent (Feature) - use custom field
    if s.parent_key:
        fields[CF_PARENT_LINK] = s.parent_key
    
    # Labels
    if s.labels and len(s.labels) > 0:
        fields["labels"] = s.labels
    
    # Team
    if s.team_id:
        fields[CF_TEAM] = s.team_id
    
    # Sprint
    sprint = _maybe_int(s.sprint_id)
    if sprint is not None and s.sprint_id.strip():
        fields[CF_SPRINT] = sprint
    
    # Story points
    sp = _maybe_float(s.story_points)
    if sp is not None:
        fields[CF_STORY_POINTS_STORY] = sp
    
    # PM Owner
    pm_id = jira_find_user_account_id(s.pm_owner, allow_network=allow_network)
    if pm_id:
        fields[CF_PM_OWNER] = {"accountId": pm_id}
    
    # Assignee
    asg_id = jira_find_user_account_id(s.assignee, allow_network=allow_network)
    if asg_id:
        fields["assignee"] = {"accountId": asg_id}
    
    return fields


def _build_story_adf_update_fields(record: ParsedRecord) -> Dict[str, Any]:
    """Build ADF fields for updating a Story (after creation)."""
    fields: Dict[str, Any] = {}
    
    # ADF fields - normalize and include if present
    if CF_ACCEPTANCE in record.story_adf and record.story_adf[CF_ACCEPTANCE] is not None:
        fields[CF_ACCEPTANCE] = _normalize_adf_marks(record.story_adf[CF_ACCEPTANCE])
    if CF_TECH_NOTES in record.story_adf and record.story_adf[CF_TECH_NOTES] is not None:
        fields[CF_TECH_NOTES] = _normalize_adf_marks(record.story_adf[CF_TECH_NOTES])
    
    return fields


def _build_subtask_create_fields(record: ParsedRecord, story_key: str, *, allow_network: bool) -> Dict[str, Any]:
    """Build fields for creating a Subtask issue."""
    s = record.subtask
    fields: Dict[str, Any] = {
        "project": {"key": PROJECT_KEY},
        "issuetype": {"id": ISSUE_TYPE_SUBTASK},
        "parent": {"key": story_key},
    }
    
    # Summary - use provided or default
    summary = record.subtask_summary
    if not summary:
        summary = f"Subtask for {story_key}"
    if len(summary) > 255:
        summary = summary[:252] + "..."
    fields["summary"] = summary
    
    # Story points
    raw_sp = s.story_points or record.story.story_points
    sp = _maybe_float(raw_sp)
    if sp is not None:
        fields[CF_STORY_POINTS_SUBTASK_PRIMARY] = sp
    
    # Assignee
    asg_id = jira_find_user_account_id(s.assignee, allow_network=allow_network)
    if asg_id:
        fields["assignee"] = {"accountId": asg_id}
    
    return fields


def _build_subtask_adf_update_fields(record: ParsedRecord) -> Dict[str, Any]:
    """Build ADF fields for updating a Subtask (after creation)."""
    fields: Dict[str, Any] = {}
    
    # ADF fields - normalize and include if present
    if CF_ACCEPTANCE in record.subtask_adf and record.subtask_adf[CF_ACCEPTANCE] is not None:
        fields[CF_ACCEPTANCE] = _normalize_adf_marks(record.subtask_adf[CF_ACCEPTANCE])
    if CF_TECH_NOTES in record.subtask_adf and record.subtask_adf[CF_TECH_NOTES] is not None:
        fields[CF_TECH_NOTES] = _normalize_adf_marks(record.subtask_adf[CF_TECH_NOTES])
    if CF_QA_TEST in record.subtask_adf and record.subtask_adf[CF_QA_TEST] is not None:
        fields[CF_QA_TEST] = _normalize_adf_marks(record.subtask_adf[CF_QA_TEST])
    
    return fields


def _update_file_with_jira_keys(file_path: str, story_key: str, subtask_key: Optional[str] = None) -> None:
    """Update the jira_key values in the local markdown file."""
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()
    
    # Update story jira_key
    content = re.sub(
        r'(user_story:\s*\n(?:\s*[^\n]*\n)*?\s*jira_key:\s*)"[^"]*"',
        rf'\1"{story_key}"',
        content,
        flags=re.MULTILINE
    )
    
    # Update subtask jira_key if provided
    if subtask_key:
        content = re.sub(
            r'(subtask:\s*\n(?:\s*[^\n]*\n)*?\s*jira_key:\s*)"[^"]*"',
            rf'\1"{subtask_key}"',
            content,
            flags=re.MULTILINE
        )
        # Also update subtask parent_key to match story_key
        content = re.sub(
            r'(subtask:\s*\n(?:\s*[^\n]*\n)*?\s*parent_key:\s*)"[^"]*"',
            rf'\1"{story_key}"',
            content,
            flags=re.MULTILINE
        )
    
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content)


def _rename_file_with_jira_key(file_path: str, jira_key: str) -> Optional[str]:
    """
    Rename file from DRAFT format to include Jira key.
    Returns the new file path if renamed, None if no rename needed.
    """
    dir_path = os.path.dirname(file_path)
    old_filename = os.path.basename(file_path)
    
    # Check if file needs renaming (starts with DRAFT)
    if not re.match(r'^DRAFT\s*-\s*(?:Story\s*-\s*|Subtask\s*-\s*)?', old_filename, re.IGNORECASE):
        return None
    
    # Extract title (remove DRAFT - Story - or DRAFT - Subtask - or DRAFT - prefix)
    title = re.sub(r'^DRAFT\s*-\s*(?:Story\s*-\s*|Subtask\s*-\s*)?', '', old_filename, flags=re.IGNORECASE)
    # Remove .md extension
    title = title.replace('.md', '')
    
    # Create new filename: {KEY} - {title}.md
    new_filename = f"{jira_key} - {title}.md"
    new_path = os.path.join(dir_path, new_filename)
    
    # Rename the file
    os.rename(file_path, new_path)
    return new_path


def _extract_summary_from_filename(file_path: str) -> str:
    """Extract a reasonable summary from the filename."""
    basename = os.path.basename(file_path)
    # Remove .md extension
    basename = basename.replace(".md", "")
    # Remove DRAFT prefix if present
    basename = re.sub(r"^DRAFT\s*-\s*", "", basename, flags=re.IGNORECASE)
    # Remove issue type prefix if present
    basename = re.sub(r"^(Feature|Story|Subtask)\s*-\s*", "", basename, flags=re.IGNORECASE)
    # Remove Jira key if present (WOR-123 - ...)
    basename = re.sub(r"^WOR-\d+\s*-\s*", "", basename)
    return basename.strip()


def _process_single_file(md_path: str, args) -> bool:
    """Process a single Story+Subtask .md file and create in Jira. Returns True on success, False on failure."""
    try:
        with open(md_path, "r", encoding="utf-8") as f:
            md = f.read()
    except FileNotFoundError:
        print(f"✗ File not found: {md_path}", file=sys.stderr)
        return False

    try:
        record = _parse_combo_record(md)
    except Exception as e:
        print(f"✗ Failed to parse {md_path}: {e}", file=sys.stderr)
        return False

    # Skip if story jira_key is already set (not a DRAFT)
    if record.story.jira_key:
        print(f"\n{os.path.basename(md_path)}:")
        print(f"  Story: {record.story.jira_key} (already exists, skipping)")
        if record.subtask.jira_key:
            print(f"  Subtask: {record.subtask.jira_key} (already exists, skipping)")
        return True

    # If we're going to talk to Jira, ensure auth exists.
    can_call_jira = True
    try:
        _env("JIRA_BASE_URL")
        _env("JIRA_EMAIL")
        _env("JIRA_API_TOKEN")
    except JiraApiError:
        can_call_jira = False
        if not args.dry_run:
            print(f"✗ Missing required Jira env vars for {md_path}", file=sys.stderr)
            return False

    # Extract summary from filename if not found in content
    if not record.story_summary:
        record = ParsedRecord(
            story=record.story,
            subtask=record.subtask,
            story_summary=_extract_summary_from_filename(md_path),
            subtask_summary=record.subtask_summary or f"Subtask for {record.story.parent_key}",
            story_adf=record.story_adf,
            subtask_adf=record.subtask_adf,
        )

    print(f"\n{os.path.basename(md_path)}:")
    print(f"  Creating Story...")

    story_create_fields = _build_story_create_fields(record, allow_network=can_call_jira and not args.dry_run)
    story_adf_fields = _build_story_adf_update_fields(record)

    if args.dry_run:
        print("  [dry-run] Would create Story with fields:")
        print(json.dumps(story_create_fields, indent=4, sort_keys=True))
        if story_adf_fields:
            print("  [dry-run] Would update Story ADF fields:")
            print(json.dumps(story_adf_fields, indent=4, sort_keys=True))
    else:
        try:
            # Create the Story
            result = jira_create_issue(story_create_fields)
            story_key = result.get("key", "")
            if not story_key:
                print(f"  ✗ Failed to create Story: no key returned", file=sys.stderr)
                return False
            
            print(f"  ✓ Created Story: {story_key}")
            
            # Update Story ADF fields if present
            if story_adf_fields:
                try:
                    jira_update_issue_fields(story_key, story_adf_fields)
                    print(f"  ✓ Updated Story ADF fields: {story_key}")
                except JiraApiError as e:
                    print(f"  ⚠ Warning: Failed to update Story ADF fields: {e}", file=sys.stderr)
            
            # Create Subtask if needed
            subtask_key = None
            if not record.subtask.jira_key:
                print(f"  Creating Subtask...")
                subtask_create_fields = _build_subtask_create_fields(
                    record, story_key, allow_network=can_call_jira and not args.dry_run
                )
                subtask_adf_fields = _build_subtask_adf_update_fields(record)
                
                try:
                    subtask_result = jira_create_issue(subtask_create_fields)
                    subtask_key = subtask_result.get("key", "")
                    if not subtask_key:
                        print(f"  ⚠ Warning: Failed to create Subtask: no key returned", file=sys.stderr)
                    else:
                        print(f"  ✓ Created Subtask: {subtask_key}")
                        
                        # Update Subtask ADF fields if present
                        if subtask_adf_fields:
                            try:
                                jira_update_issue_fields(subtask_key, subtask_adf_fields)
                                print(f"  ✓ Updated Subtask ADF fields: {subtask_key}")
                            except JiraApiError as e:
                                print(f"  ⚠ Warning: Failed to update Subtask ADF fields: {e}", file=sys.stderr)
                except JiraApiError as e:
                    print(f"  ⚠ Warning: Failed to create Subtask: {e}", file=sys.stderr)
                    # Continue anyway - the Story was created
            
            # Update local file with jira_keys
            _update_file_with_jira_keys(md_path, story_key, subtask_key)
            print(f"  ✓ Updated local file with jira_keys")
            
            # Rename file if it's a DRAFT
            new_path = _rename_file_with_jira_key(md_path, story_key)
            if new_path:
                print(f"  ✓ Renamed file: {os.path.basename(md_path)} -> {os.path.basename(new_path)}")
            
        except JiraApiError as e:
            print(f"  ✗ Failed to create Story: {e}", file=sys.stderr)
            return False

    return True


def main():
    parser = argparse.ArgumentParser(
        description="Create Jira User Story + Subtask issue(s) from local .md record(s)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Create a single Story+Subtask
  python3 create_story_in_jira.py path/to/story.md

  # Create multiple Stories
  python3 create_story_in_jira.py file1.md file2.md

  # Create all DRAFT Stories in a directory
  python3 create_story_in_jira.py current/Namespacing/Security/*.md
        """
    )
    parser.add_argument(
        "md_files",
        nargs="+",
        help="Path(s) to the local Story/Subtask .md file(s) (supports glob patterns)",
    )
    parser.add_argument(
        "--env-file",
        help="Optional path to a .env file to load (defaults to searching for .env automatically)",
        default=None,
    )
    parser.add_argument("--dry-run", action="store_true", help="Do not create in Jira; print intended creation")
    args = parser.parse_args()

    # Load .env if present before we look for Jira env vars.
    _auto_load_dotenv(args.env_file)

    # Expand any glob patterns in the file list
    expanded_files = []
    for pattern in args.md_files:
        if '*' in pattern or '?' in pattern:
            expanded_files.extend(glob.glob(pattern))
        else:
            expanded_files.append(pattern)

    if not expanded_files:
        print("No files found to process.", file=sys.stderr)
        return 1

    print(f"Processing {len(expanded_files)} file(s)...\n")

    success_count = 0
    failed_count = 0

    for md_path in expanded_files:
        if _process_single_file(md_path, args):
            success_count += 1
        else:
            failed_count += 1

    print(f"\n{'='*60}")
    print(f"Summary: {success_count} succeeded, {failed_count} failed")
    print(f"{'='*60}")

    return 0 if failed_count == 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())

