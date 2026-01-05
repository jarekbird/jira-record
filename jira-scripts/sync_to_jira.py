#!/usr/bin/env python3
"""
Update Jira for local "User Story + Subtask" Markdown record(s).

This script reads one or more `.md` file(s) that follow:
  `ga-jira/.cursor/rules/local-jira-records-story-subtask.mdc`

It then updates BOTH Jira issues via Jira REST API v3:
  - User Story (Story)
  - Subtask (Sub-task)

Required environment variables (same as `ga-jira/mcp.json`):
  - JIRA_BASE_URL   (e.g. "https://gnapartners.atlassian.net")
  - JIRA_EMAIL
  - JIRA_API_TOKEN

Usage:
  python3 update_user_story.py file1.md file2.md [--dry-run] [--no-transition]
  python3 update_user_story.py current/Namespacing/Security/*.md [--dry-run] [--no-transition]
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


# Jira custom field IDs we care about (per rules / prior usage in this repo)
CF_TEAM = "customfield_10001"
CF_SPRINT = "customfield_10020"
# Story points fields
#
# This Jira exposes multiple story points fields:
#   - customfield_10016: "Story point estimate" (JSW story points, jsw-story-points)
#   - customfield_10037: "Story Points" (the field that appears in the Jira UI for Stories)
#
# The Jira UI displays customfield_10037 as "Story Points" for User Stories.
# We use customfield_10037 for Stories and customfield_10016 for Subtasks.
CF_STORY_POINTS_STORY = "customfield_10037"  # The field shown in Jira UI for Stories
CF_STORY_POINTS_SUBTASK_PRIMARY = "customfield_10016"  # JSW story points for Subtasks
CF_PARENT_LINK = "customfield_10014"  # Feature parent for Stories in this Jira
CF_PM_OWNER = "customfield_10246"
CF_ACCEPTANCE = "customfield_10256"
CF_TECH_NOTES = "customfield_10356"
CF_QA_TEST = "customfield_10462"


@dataclass(frozen=True)
class StoryMeta:
    jira_key: str
    parent_key: str
    status: str
    team_id: str
    sprint_id: str
    pm_owner: str
    assignee: str
    story_points: str
    labels: List[str]


@dataclass(frozen=True)
class SubtaskMeta:
    jira_key: str
    parent_key: str
    status: str
    team_id: str
    sprint_id: str
    assignee: str
    story_points: str


@dataclass(frozen=True)
class ParsedRecord:
    story: StoryMeta
    subtask: SubtaskMeta
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


def jira_get_issue(issue_key: str, *, fields: Optional[List[str]] = None) -> Dict[str, Any]:
    q: Dict[str, str] = {}
    if fields:
        q["fields"] = ",".join(fields)
    return _jira_request("GET", f"/rest/api/3/issue/{issue_key}", query=q)


def jira_update_issue_fields(issue_key: str, fields: Dict[str, Any]) -> None:
    _jira_request("PUT", f"/rest/api/3/issue/{issue_key}", body={"fields": fields})


def jira_get_transitions(issue_key: str) -> List[Dict[str, Any]]:
    data = _jira_request("GET", f"/rest/api/3/issue/{issue_key}/transitions")
    return data.get("transitions", []) or []


def jira_transition(issue_key: str, transition_id: str) -> None:
    _jira_request(
        "POST",
        f"/rest/api/3/issue/{issue_key}/transitions",
        body={"transition": {"id": transition_id}},
    )


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


def _parse_combo_record(md: str) -> ParsedRecord:
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

    if not story.jira_key:
        raise ValueError("user_story.jira_key is required")
    if not story.parent_key:
        raise ValueError("user_story.parent_key (Feature key) is required")
    # Subtask is optional - if jira_key is empty, we'll skip subtask updates
    if subtask.jira_key and subtask.parent_key != story.jira_key:
        raise ValueError(f"subtask.parent_key must match user_story.jira_key ({story.jira_key})")

    story_adf, subtask_adf = _extract_adf_blocks(md)
    return ParsedRecord(story=story, subtask=subtask, story_adf=story_adf, subtask_adf=subtask_adf)


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


def _maybe_int(v: str) -> Optional[int]:
    s = v.strip()
    if not s:
        return None
    if not re.match(r"^-?\d+$", s):
        return None
    return int(s)


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


def _maybe_float(v: str) -> Optional[float]:
    """Parse a string as a float, returning None if invalid. Used for story points."""
    s = v.strip()
    if not s:
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _build_story_update(record: ParsedRecord, *, allow_network: bool) -> Dict[str, Any]:
    s = record.story
    fields: Dict[str, Any] = {}

    # Parent (Feature link) - this Jira uses a custom field for Story->Feature parent.
    # NOTE: Skipping parent link updates as they may cause INVALID_INPUT errors
    # The parent relationship is typically set at creation and shouldn't be changed
    # if s.parent_key:
    #     fields[CF_PARENT_LINK] = s.parent_key

    # Labels - skip empty lists as they may cause issues
    if s.labels and len(s.labels) > 0:
        fields["labels"] = s.labels

    # Team
    # Format: Team field expects just the UUID string, not wrapped in an object
    if s.team_id:
        fields[CF_TEAM] = s.team_id

    # Sprint - skip if sprint_id is empty (allows clearing sprint)
    # Note: We don't set sprint if it's empty to avoid validation errors
    sprint = _maybe_int(s.sprint_id)
    if sprint is not None and s.sprint_id.strip():
        # Try to set sprint, but don't fail if it's a closed sprint
        # Jira will reject closed sprints with "Issue can be assigned only active or future sprints"
        fields[CF_SPRINT] = sprint

    # Story points (Story) - can be float (e.g., 0.5, 1.5, 2.0)
    sp = _maybe_float(s.story_points)
    if sp is not None:
        fields[CF_STORY_POINTS_STORY] = sp

    # PM Owner (user picker custom field)
    pm_id = jira_find_user_account_id(s.pm_owner, allow_network=allow_network)
    if pm_id:
        fields[CF_PM_OWNER] = {"accountId": pm_id}
    elif s.pm_owner and not allow_network:
        # Only set placeholder in dry-run mode; skip field if network is allowed but lookup failed
        fields[CF_PM_OWNER] = {"accountId": f"<unresolved:{s.pm_owner}>"}

    # Assignee
    asg_id = jira_find_user_account_id(s.assignee, allow_network=allow_network)
    if asg_id:
        fields["assignee"] = {"accountId": asg_id}
    elif s.assignee and not allow_network:
        # Only set placeholder in dry-run mode; skip field if network is allowed but lookup failed
        fields["assignee"] = {"accountId": f"<unresolved:{s.assignee}>"}

    # NOTE: Reporter field cannot be updated via API (read-only/restricted field)
    # Reporter is typically set at issue creation and can only be changed by admins

    # ADF fields - only include if present and not None
    # Normalize ADF to remove combined marks (code+strong -> code only) as Jira doesn't accept them
    if CF_ACCEPTANCE in record.story_adf and record.story_adf[CF_ACCEPTANCE] is not None:
        fields[CF_ACCEPTANCE] = _normalize_adf_marks(record.story_adf[CF_ACCEPTANCE])
    if CF_TECH_NOTES in record.story_adf and record.story_adf[CF_TECH_NOTES] is not None:
        fields[CF_TECH_NOTES] = _normalize_adf_marks(record.story_adf[CF_TECH_NOTES])

    return fields


def _build_subtask_update(record: ParsedRecord, *, allow_network: bool) -> Dict[str, Any]:
    s = record.subtask
    fields: Dict[str, Any] = {}

    # Parent (Story)
    # NOTE: Jira does not always allow changing subtask parent; we'll attempt if editmeta allows,
    # but even if it fails, we still update other fields.
    fields["parent"] = {"key": s.parent_key}

    # Team
    # NOTE: Subtasks inherit team from their parent, so we don't set it here
    # if s.team_id:
    #     fields[CF_TEAM] = s.team_id

    # Sprint
    # NOTE: Subtasks inherit sprint from their parent, so we don't set it here
    # sprint = _maybe_int(s.sprint_id)
    # if sprint is not None:
    #     fields[CF_SPRINT] = sprint

    # Story points (Subtask) - can be float (e.g., 0.5, 1.5, 2.0)
    # Prefer subtask.story_points if present, else fall back to user_story.story_points.
    raw_sp = record.subtask.story_points or record.story.story_points
    sp = _maybe_float(raw_sp)
    if sp is not None:
        fields[CF_STORY_POINTS_SUBTASK_PRIMARY] = sp

    # Assignee
    asg_id = jira_find_user_account_id(s.assignee, allow_network=allow_network)
    if asg_id:
        fields["assignee"] = {"accountId": asg_id}
    elif s.assignee and not allow_network:
        # Only set placeholder in dry-run mode; skip field if network is allowed but lookup failed
        fields["assignee"] = {"accountId": f"<unresolved:{s.assignee}>"}

    # ADF fields - only include if present and not None
    # Normalize ADF to remove combined marks (code+strong -> code only) as Jira doesn't accept them
    if CF_ACCEPTANCE in record.subtask_adf and record.subtask_adf[CF_ACCEPTANCE] is not None:
        fields[CF_ACCEPTANCE] = _normalize_adf_marks(record.subtask_adf[CF_ACCEPTANCE])
    if CF_TECH_NOTES in record.subtask_adf and record.subtask_adf[CF_TECH_NOTES] is not None:
        fields[CF_TECH_NOTES] = _normalize_adf_marks(record.subtask_adf[CF_TECH_NOTES])
    if CF_QA_TEST in record.subtask_adf and record.subtask_adf[CF_QA_TEST] is not None:
        fields[CF_QA_TEST] = _normalize_adf_marks(record.subtask_adf[CF_QA_TEST])

    return fields


def _maybe_transition(issue_key: str, desired_status: str, *, dry_run: bool) -> None:
    desired = (desired_status or "").strip()
    if not desired:
        return
    issue = jira_get_issue(issue_key, fields=["status"])
    current = (((issue.get("fields") or {}).get("status") or {}).get("name")) or ""
    if current.strip() == desired:
        return

    transitions = jira_get_transitions(issue_key)
    # Find transition whose "to" status matches desired.
    transition_id = None
    for t in transitions:
        to = (t.get("to") or {}).get("name")
        if to == desired:
            transition_id = t.get("id")
            break

    if not transition_id:
        # Fallback: some workflows name transition itself as the status
        for t in transitions:
            if t.get("name") == desired:
                transition_id = t.get("id")
                break

    if not transition_id:
        raise JiraApiError(
            f"No available transition for {issue_key}: '{current}' -> '{desired}'. "
            f"Available: {[t.get('to', {}).get('name') for t in transitions]}"
        )

    if dry_run:
        print(f"[dry-run] Would transition {issue_key}: {current} -> {desired} (transition {transition_id})")
        return

    jira_transition(issue_key, transition_id)
    print(f"Transitioned {issue_key}: {current} -> {desired}")

def _process_single_file(md_path: str, args) -> bool:
    """Process a single .md file and update Jira. Returns True on success, False on failure."""
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

    story_key = record.story.jira_key
    subtask_key = record.subtask.jira_key

    story_fields = _build_story_update(record, allow_network=can_call_jira and not args.dry_run)
    subtask_fields = _build_subtask_update(record, allow_network=can_call_jira and not args.dry_run)

    print(f"\n{os.path.basename(md_path)}:")
    print(f"  Story:   {story_key}")
    if subtask_key:
        print(f"  Subtask: {subtask_key}")
    else:
        print(f"  Subtask: (none - skipping subtask updates)")

    if args.dry_run:
        print("  [dry-run] Story field updates:")
        print(json.dumps(story_fields, indent=4, sort_keys=True))
        if subtask_key:
            print("  [dry-run] Subtask field updates:")
            print(json.dumps(subtask_fields, indent=4, sort_keys=True))
        else:
            print("  [dry-run] Subtask: skipped (no jira_key)")
    else:
        # Update Story fields first
        try:
            jira_update_issue_fields(story_key, story_fields)
            print(f"  ✓ Updated Story fields: {story_key}")
        except JiraApiError as e:
            # If error is about closed sprint, retry without sprint field
            if "can be assigned only active or future sprints" in str(e) and CF_SPRINT in story_fields:
                print(f"  ⚠ Warning: Sprint is closed, retrying without sprint field", file=sys.stderr)
                story_fields_no_sprint = {k: v for k, v in story_fields.items() if k != CF_SPRINT}
                try:
                    jira_update_issue_fields(story_key, story_fields_no_sprint)
                    print(f"  ✓ Updated Story fields (without sprint): {story_key}")
                except JiraApiError as e2:
                    print(f"  ✗ Failed to update Story {story_key}: {e2}", file=sys.stderr)
                    return False
            else:
                print(f"  ✗ Failed to update Story {story_key}: {e}", file=sys.stderr)
                return False

        # Update Subtask fields only if subtask_key exists
        if subtask_key:
            try:
                jira_update_issue_fields(subtask_key, subtask_fields)
                print(f"  ✓ Updated Subtask fields: {subtask_key}")
            except JiraApiError as e:
                msg = str(e)
                if "parent" in msg or "Parent" in msg:
                    print(f"  ⚠ Warning: subtask parent update failed; retrying without parent", file=sys.stderr)
                    subtask_fields_wo_parent = {k: v for k, v in subtask_fields.items() if k != "parent"}
                    try:
                        jira_update_issue_fields(subtask_key, subtask_fields_wo_parent)
                        print(f"  ✓ Updated Subtask fields (without parent): {subtask_key}")
                    except JiraApiError as e2:
                        print(f"  ✗ Failed to update Subtask {subtask_key}: {e2}", file=sys.stderr)
                        return False
                else:
                    print(f"  ✗ Failed to update Subtask {subtask_key}: {e}", file=sys.stderr)
                    return False

    if not args.no_transition:
        if not can_call_jira:
            print("  ⚠ Skipping transitions: Jira env vars not set.", file=sys.stderr)
        else:
            try:
                _maybe_transition(story_key, record.story.status, dry_run=args.dry_run)
            except Exception as e:
                print(f"  ⚠ Story transition warning: {e}", file=sys.stderr)
            if subtask_key:
                try:
                    _maybe_transition(subtask_key, record.subtask.status, dry_run=args.dry_run)
                except Exception as e:
                    print(f"  ⚠ Subtask transition warning: {e}", file=sys.stderr)

    return True


def main():
    parser = argparse.ArgumentParser(
        description="Update Jira Story + Subtask from local .md record(s)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Update a single file
  python3 update_user_story.py path/to/file.md

  # Update multiple files
  python3 update_user_story.py file1.md file2.md file3.md

  # Update all files in a directory
  python3 update_user_story.py current/Namespacing/Security/*.md
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
    parser.add_argument("--dry-run", action="store_true", help="Do not write to Jira; print intended updates")
    parser.add_argument(
        "--no-transition",
        action="store_true",
        help="Do not transition statuses (still updates fields)",
    )
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

