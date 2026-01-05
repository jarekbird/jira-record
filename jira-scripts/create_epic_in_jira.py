#!/usr/bin/env python3
"""
Create Jira Epic issue(s) from local Markdown record(s).

This script reads one or more `.md` file(s) that follow:
  `ga-jira/.cursor/rules/local-jira-records-epic.mdc`

It then creates the Epic issue(s) in Jira via Jira REST API v3 and updates
the local `.md` file(s) with the created jira_key.

Required environment variables (same as `ga-jira/mcp.json`):
  - JIRA_BASE_URL   (e.g. "https://gnapartners.atlassian.net")
  - JIRA_EMAIL
  - JIRA_API_TOKEN

Usage:
  python3 create_epic_in_jira.py file1.md file2.md [--dry-run]
  python3 create_epic_in_jira.py current/Epics/*Epic*.md [--dry-run]
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
from typing import Any, Dict, List, Optional

try:
    import urllib.request
    import urllib.parse
except Exception:  # pragma: no cover
    urllib = None  # type: ignore


# Jira custom field IDs for Epics
CF_TEAM = "customfield_10001"
CF_SPRINT = "customfield_10020"
CF_PM_OWNER = "customfield_10246"
CF_PROBLEM_STATEMENT = "customfield_10322"  # Text field for Epics
CF_STORY_POINTS = "customfield_10026"  # Epic story points

# Issue type IDs
ISSUE_TYPE_EPIC = "10186"
PROJECT_KEY = "WOR"


@dataclass(frozen=True)
class EpicMeta:
    jira_key: str  # Empty for DRAFT files
    status: str
    team_id: str
    sprint_id: str
    assignee: str
    pm_owner: str
    story_points: str
    labels: List[str]


@dataclass(frozen=True)
class ParsedEpicRecord:
    epic: EpicMeta
    summary: str
    description_adf: Optional[Dict[str, Any]]  # description as ADF
    problem_statement: str  # Problem Statement as plain text


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
    """Return the YAML front matter block."""
    if md.lstrip().startswith("---"):
        m = re.search(r"^---\s*\n(.*?)\n---\s*\n", md, flags=re.DOTALL)
        if not m:
            raise ValueError("Could not find terminating '---' for YAML front matter")
        return m.group(1)
    raise ValueError("Could not find YAML front matter (must start with '---')")


def _parse_inline_list(value: str) -> List[str]:
    v = value.strip()
    if v == "[]":
        return []
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


def _parse_yaml_value(line: str) -> str:
    """Extract value from a YAML key: value line."""
    if ":" not in line:
        return ""
    _, value = line.split(":", 1)
    value = value.strip()
    # Remove comments
    if " #" in value:
        value = value.split(" #", 1)[0].rstrip()
    # Dequote
    if value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
    if value.startswith("'") and value.endswith("'"):
        value = value[1:-1]
    return value


def _extract_description_adf(md: str) -> Optional[Dict[str, Any]]:
    """Extract description ADF JSON block from the markdown."""
    lines = md.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        
        # Look for ADF Version marker for description
        m = re.search(r"ADF Version\s+\((description)\)", line)
        if m:
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
                    return json.loads(raw)
                except json.JSONDecodeError as e:
                    raise ValueError(f"Invalid JSON in ADF block for description: {e}") from e
        i += 1
    
    return None


def _extract_problem_statement(md: str) -> str:
    """Extract Problem Statement text from markdown sections."""
    # Look for Problem Statement section or extract from Business Context
    # For now, we'll extract from a markdown section if present
    problem_match = re.search(r"^##\s+Problem Statement\s*\n(.*?)(?=\n##|\Z)", md, re.MULTILINE | re.DOTALL)
    if problem_match:
        return problem_match.group(1).strip()
    
    # Fallback: extract from Business Context
    context_match = re.search(r"^##\s+Business Context\s*\n(.*?)(?=\n##|\Z)", md, re.MULTILINE | re.DOTALL)
    if context_match:
        # Extract first paragraph or bullet points
        content = context_match.group(1).strip()
        # Remove markdown formatting for plain text
        content = re.sub(r'^[-*+]\s+', '', content, flags=re.MULTILINE)
        content = re.sub(r'\*\*(.*?)\*\*', r'\1', content)
        return content[:255]  # Limit to 255 chars for text field
    
    return ""


def _parse_epic_record(md: str) -> ParsedEpicRecord:
    """Parse an Epic markdown file."""
    fm = _extract_front_matter_block(md)
    
    # Parse front matter fields
    jira_key = ""
    status = ""
    team_id = ""
    sprint_id = ""
    assignee = ""
    pm_owner = ""
    story_points = ""
    labels: List[str] = []
    
    for line in fm.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("jira_key:"):
            jira_key = _parse_yaml_value(line)
        elif line.startswith("status:"):
            status = _parse_yaml_value(line)
        elif line.startswith("team_id:"):
            team_id = _parse_yaml_value(line)
        elif line.startswith("sprint_id:"):
            sprint_id = _parse_yaml_value(line)
        elif line.startswith("assignee:"):
            assignee = _parse_yaml_value(line)
        elif line.startswith("pm_owner:"):
            pm_owner = _parse_yaml_value(line)
        elif line.startswith("story_points:"):
            story_points = _parse_yaml_value(line)
        elif line.startswith("labels:"):
            labels = _parse_inline_list(_parse_yaml_value(line))
    
    # Extract summary from ## Summary section
    summary_match = re.search(r"^##\s+Summary\s*\n(.*?)(?=\n##|\Z)", md, re.MULTILINE | re.DOTALL)
    summary = summary_match.group(1).strip() if summary_match else ""
    
    if not summary:
        raise ValueError("Summary is required (## Summary section)")
    
    # Extract description ADF and problem statement
    description_adf = _extract_description_adf(md)
    problem_statement = _extract_problem_statement(md)
    
    epic = EpicMeta(
        jira_key=jira_key.strip(),
        status=status.strip(),
        team_id=team_id.strip(),
        sprint_id=sprint_id.strip(),
        assignee=assignee.strip(),
        pm_owner=pm_owner.strip(),
        story_points=story_points.strip(),
        labels=labels,
    )
    
    return ParsedEpicRecord(epic=epic, summary=summary, description_adf=description_adf, problem_statement=problem_statement)


def _maybe_int(v: str) -> Optional[int]:
    s = v.strip()
    if not s:
        return None
    if not re.match(r"^-?\d+$", s):
        return None
    return int(s)


def _maybe_float(v: str) -> Optional[float]:
    """Parse a string as a float, returning None if invalid."""
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


def _build_epic_create_fields(record: ParsedEpicRecord, *, allow_network: bool) -> Dict[str, Any]:
    """Build fields for creating an Epic issue."""
    e = record.epic
    fields: Dict[str, Any] = {
        "project": {"key": PROJECT_KEY},
        "issuetype": {"id": ISSUE_TYPE_EPIC},
        "summary": record.summary,
    }
    
    # Labels
    if e.labels and len(e.labels) > 0:
        fields["labels"] = e.labels
    
    # Team
    if e.team_id:
        fields[CF_TEAM] = e.team_id
    
    # Sprint
    sprint = _maybe_int(e.sprint_id)
    if sprint is not None and e.sprint_id.strip():
        fields[CF_SPRINT] = sprint
    
    # Story points
    sp = _maybe_float(e.story_points)
    if sp is not None:
        fields[CF_STORY_POINTS] = sp
    
    # PM Owner
    pm_id = jira_find_user_account_id(e.pm_owner, allow_network=allow_network)
    if pm_id:
        fields[CF_PM_OWNER] = {"accountId": pm_id}
    
    # Assignee
    asg_id = jira_find_user_account_id(e.assignee, allow_network=allow_network)
    if asg_id:
        fields["assignee"] = {"accountId": asg_id}
    
    # Description (ADF) - include if present
    if record.description_adf is not None:
        fields["description"] = _normalize_adf_marks(record.description_adf)
    
    return fields


def _build_epic_update_fields(record: ParsedEpicRecord) -> Dict[str, Any]:
    """Build fields for updating an Epic (after creation)."""
    fields: Dict[str, Any] = {}
    
    # Problem Statement (text field)
    if record.problem_statement:
        fields[CF_PROBLEM_STATEMENT] = record.problem_statement[:255]  # Limit to 255 chars
    
    return fields


def _update_file_with_jira_key(file_path: str, jira_key: str) -> None:
    """Update the jira_key in the local markdown file."""
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()
    
    # Update jira_key in front matter
    content = re.sub(
        r'^(jira_key:\s*)"[^"]*"',
        rf'\1"{jira_key}"',
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
    if not re.match(r'^DRAFT\s*-\s*(?:Epic\s*-\s*)?', old_filename, re.IGNORECASE):
        return None
    
    # Extract title (remove DRAFT - Epic - or DRAFT - prefix)
    title = re.sub(r'^DRAFT\s*-\s*(?:Epic\s*-\s*)?', '', old_filename, flags=re.IGNORECASE)
    # Remove .md extension
    title = title.replace('.md', '')
    
    # Create new filename: {KEY} - {title}.md
    new_filename = f"{jira_key} - {title}.md"
    new_path = os.path.join(dir_path, new_filename)
    
    # Rename the file
    os.rename(file_path, new_path)
    return new_path


def _process_single_file(md_path: str, args) -> bool:
    """Process a single Epic .md file and create in Jira. Returns True on success, False on failure."""
    try:
        with open(md_path, "r", encoding="utf-8") as f:
            md = f.read()
    except FileNotFoundError:
        print(f"✗ File not found: {md_path}", file=sys.stderr)
        return False

    try:
        record = _parse_epic_record(md)
    except Exception as e:
        print(f"✗ Failed to parse {md_path}: {e}", file=sys.stderr)
        return False

    # Skip if jira_key is already set (not a DRAFT)
    if record.epic.jira_key:
        print(f"\n{os.path.basename(md_path)}:")
        print(f"  Epic: {record.epic.jira_key} (already exists, skipping)")
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

    print(f"\n{os.path.basename(md_path)}:")
    print(f"  Creating Epic...")

    create_fields = _build_epic_create_fields(record, allow_network=can_call_jira and not args.dry_run)
    update_fields = _build_epic_update_fields(record)

    if args.dry_run:
        print("  [dry-run] Would create Epic with fields:")
        print(json.dumps(create_fields, indent=4, sort_keys=True))
        if update_fields:
            print("  [dry-run] Would update additional fields:")
            print(json.dumps(update_fields, indent=4, sort_keys=True))
    else:
        try:
            # Create the Epic
            result = jira_create_issue(create_fields)
            jira_key = result.get("key", "")
            if not jira_key:
                print(f"  ✗ Failed to create Epic: no key returned", file=sys.stderr)
                return False
            
            print(f"  ✓ Created Epic: {jira_key}")
            
            # Update additional fields if present
            if update_fields:
                try:
                    jira_update_issue_fields(jira_key, update_fields)
                    print(f"  ✓ Updated additional fields: {jira_key}")
                except JiraApiError as e:
                    print(f"  ⚠ Warning: Failed to update additional fields: {e}", file=sys.stderr)
                    # Continue anyway - the Epic was created
            
            # Update local file with jira_key
            _update_file_with_jira_key(md_path, jira_key)
            print(f"  ✓ Updated local file with jira_key: {jira_key}")
            
            # Rename file if it's a DRAFT
            new_path = _rename_file_with_jira_key(md_path, jira_key)
            if new_path:
                print(f"  ✓ Renamed file: {os.path.basename(md_path)} -> {os.path.basename(new_path)}")
            
        except JiraApiError as e:
            print(f"  ✗ Failed to create Epic: {e}", file=sys.stderr)
            return False

    return True


def main():
    parser = argparse.ArgumentParser(
        description="Create Jira Epic issue(s) from local .md record(s)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Create a single Epic
  python3 create_epic_in_jira.py path/to/epic.md

  # Create multiple Epics
  python3 create_epic_in_jira.py file1.md file2.md

  # Create all DRAFT Epics in a directory
  python3 create_epic_in_jira.py current/Epics/*Epic*.md
        """
    )
    parser.add_argument(
        "md_files",
        nargs="+",
        help="Path(s) to the local Epic .md file(s) (supports glob patterns)",
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

