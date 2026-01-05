#!/usr/bin/env python3
"""
Sync local Markdown Epic record(s) FROM Jira.

This script reads one or more `.md` file(s) that follow:
  `ga-jira/.cursor/rules/local-jira-records-epic.mdc`

It then fetches the Epic issue from Jira and updates the local `.md` file(s) to match.

Required environment variables (same as `ga-jira/mcp.json`):
  - JIRA_BASE_URL   (e.g. "https://gnapartners.atlassian.net")
  - JIRA_EMAIL
  - JIRA_API_TOKEN

Usage:
  python3 pull_epic_from_jira.py file1.md file2.md [--dry-run]
  python3 pull_epic_from_jira.py current/Epics/*Epic*.md [--dry-run]
"""

from __future__ import annotations

import argparse
import base64
import glob
import json
import os
import re
import sys
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
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            # Handle export KEY=value
            if line.startswith("export "):
                line = line[7:].strip()

            if "=" not in line:
                continue

            key, sep, value = line.partition("=")
            key = key.strip()
            value = value.strip()

            # Remove quotes if present
            if (value.startswith('"') and value.endswith('"')) or (
                value.startswith("'") and value.endswith("'")
            ):
                value = value[1:-1]

            if override or key not in os.environ:
                os.environ[key] = value

    return True


def _env(key: str) -> str:
    val = os.getenv(key)
    if not val:
        raise JiraApiError(f"Environment variable {key} is not set")
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
    """Fetch issue details from Jira"""
    q: Dict[str, str] = {}
    if fields:
        q["fields"] = ",".join(fields)
    return _jira_request("GET", f"/rest/api/3/issue/{issue_key}", query=q)


def _parse_epic_data(issue: Dict[str, Any]) -> Dict[str, Any]:
    """Extract relevant fields from Jira Epic issue response"""
    fields = issue.get("fields", {})
    
    # Status
    status = fields.get("status", {}).get("name", "")
    
    # Summary
    summary = fields.get("summary", "")
    
    # Assignee
    assignee_obj = fields.get("assignee")
    assignee = assignee_obj.get("emailAddress", "") if assignee_obj else ""
    
    # Reporter
    reporter_obj = fields.get("reporter")
    reporter = reporter_obj.get("emailAddress", "") or reporter_obj.get("accountId", "") if reporter_obj else ""
    
    # Created/Updated
    created = fields.get("created", "")
    updated = fields.get("updated", "")
    
    # Team
    team_field = fields.get(CF_TEAM)
    team_id = team_field.get("id", "") if team_field else ""
    
    # Sprint
    sprint_field = fields.get(CF_SPRINT)
    sprint_id = ""
    if sprint_field:
        if isinstance(sprint_field, list) and len(sprint_field) > 0:
            sprint_id = str(sprint_field[0].get("id", ""))
        elif isinstance(sprint_field, dict):
            sprint_id = str(sprint_field.get("id", ""))
    
    # Story Points
    sp = fields.get(CF_STORY_POINTS)
    story_points = "" if sp is None else str(sp)
    
    # PM Owner
    pm_owner = ""
    pm_field = fields.get(CF_PM_OWNER)
    if pm_field:
        pm_owner = pm_field.get("emailAddress", "") or pm_field.get("accountId", "")
    
    # Labels
    labels = fields.get("labels", [])
    
    # ADF fields
    description = fields.get("description")
    
    # Problem Statement (text field)
    problem_statement = fields.get(CF_PROBLEM_STATEMENT, "")
    
    return {
        "summary": summary,
        "status": status,
        "assignee": assignee,
        "reporter": reporter,
        "created": created,
        "updated": updated,
        "team_id": team_id,
        "sprint_id": sprint_id,
        "story_points": story_points,
        "pm_owner": pm_owner,
        "labels": labels,
        "description": description,
        "problem_statement": problem_statement,
    }


def _update_yaml_field(content: str, field: str, value: str) -> str:
    """Update a YAML field in the front matter"""
    if not value:
        formatted_value = '""'
    else:
        formatted_value = f'"{value}"'
    
    # Pattern: field: "value" or field: value
    pattern = rf"(^{field}:\s*)(?:""[^""]*""|'[^']*'|[^\n]*)"
    replacement = rf"\1{formatted_value}"
    
    if re.search(pattern, content, re.MULTILINE):
        return re.sub(pattern, replacement, content, flags=re.MULTILINE)
    else:
        # Field doesn't exist, add it after the front matter start
        fm_start = content.find('---\n')
        if fm_start != -1:
            insert_pos = fm_start + 4
            new_field = f"{field}: {formatted_value}\n"
            return content[:insert_pos] + new_field + content[insert_pos:]
    
    return content


def _update_yaml_list_field(content: str, field: str, values: List[str]) -> str:
    """Update a YAML list field in the front matter"""
    pattern = rf"(^{field}:\s*)(?:\[[^\]]*\]|\[.*?\])"
    
    formatted_value = json.dumps(values) if values else "[]"
    replacement = rf"\1{formatted_value}"
    
    if re.search(pattern, content, re.MULTILINE):
        return re.sub(pattern, replacement, content, flags=re.MULTILINE)
    else:
        # Add the field if it doesn't exist
        fm_start = content.find('---\n')
        if fm_start != -1:
            insert_pos = fm_start + 4
            new_field = f"{field}: {formatted_value}\n"
            return content[:insert_pos] + new_field + content[insert_pos:]
    
    return content


def _update_adf_block(content: str, section_title: str, field_id: str, adf: Optional[Dict[str, Any]]) -> str:
    """Update or create an ADF block in the markdown"""
    if adf is None:
        return content
    
    # Find the section
    section_pattern = rf"^##\s+{section_title}\s*$"
    section_match = re.search(section_pattern, content, re.MULTILINE)
    
    if not section_match:
        # Section doesn't exist, add it at the end (before References if it exists)
        ref_match = re.search(r"^##\s+References", content, re.MULTILINE)
        if ref_match:
            insert_pos = ref_match.start()
        else:
            insert_pos = len(content)
        
        # Add the section with ADF block
        new_section = f"\n## {section_title}\n\n[To be populated from Jira]\n\n<details>\n<summary>ADF Version ({field_id})</summary>\n\n```json\n{json.dumps(adf, indent=2)}\n```\n</details>\n"
        return content[:insert_pos] + new_section + content[insert_pos:]
    
    # Section exists, find the ADF block
    section_start = section_match.start()
    
    # Look for existing ADF block
    adf_pattern = rf"<details>\s*<summary>ADF Version\s+\({re.escape(field_id)}\)</summary>"
    adf_match = re.search(adf_pattern, content[section_start:], re.DOTALL)
    
    adf_json = json.dumps(adf, indent=2)
    new_adf_block = f"<details>\n<summary>ADF Version ({field_id})</summary>\n\n```json\n{adf_json}\n```\n</details>"
    
    if adf_match:
        # Replace existing ADF block
        adf_start = section_start + adf_match.start()
        # Find the closing </details>
        details_end = content.find("</details>", adf_start)
        if details_end != -1:
            details_end += len("</details>")
            # Include any trailing newlines
            while details_end < len(content) and content[details_end] == '\n':
                details_end += 1
            return content[:adf_start] + new_adf_block + "\n" + content[details_end:]
    else:
        # No ADF block exists, add it after the section content
        # Find the next ## section or end of file
        next_section = re.search(r"^##\s+", content[section_start + 10:], re.MULTILINE)
        if next_section:
            insert_pos = section_start + 10 + next_section.start()
        else:
            insert_pos = len(content)
        
        # Find the end of the current section's content (before next section)
        section_content_end = insert_pos
        # Look backwards for the last non-empty line
        while section_content_end > section_start and content[section_content_end - 1] in '\n\r':
            section_content_end -= 1
        
        return content[:section_content_end] + "\n\n" + new_adf_block + "\n" + content[section_content_end:]
    
    return content


def sync_epic_from_jira(file_path: str, *, dry_run: bool = False) -> bool:
    """Sync a single Epic .md file from Jira"""
    if not os.path.exists(file_path):
        print(f"Error: File not found: {file_path}")
        return False
    
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()
    
    # Extract Epic key from front matter
    jira_key_match = re.search(r'^jira_key:\s*"([^"]+)"', content, re.MULTILINE)
    
    if not jira_key_match:
        print(f"Error: Could not find jira_key in {file_path}")
        return False
    
    epic_key = jira_key_match.group(1)
    
    print(f"\n{os.path.basename(file_path)}:")
    print(f"  Epic: {epic_key}")
    
    # Fetch Epic from Jira
    try:
        epic_issue = jira_get_issue(
            epic_key,
            fields=[
                "summary",
                "status",
                "assignee",
                "reporter",
                "created",
                "updated",
                "issuetype",
                "labels",
                CF_TEAM,
                CF_SPRINT,
                CF_STORY_POINTS,
                CF_PM_OWNER,
                "description",
                CF_PROBLEM_STATEMENT,
            ],
        )
        epic_data = _parse_epic_data(epic_issue)
        print(f"  ✓ Fetched Epic: status={epic_data['status']}, assignee={epic_data['assignee'] or 'unassigned'}")
    except Exception as e:
        print(f"  ✗ Error fetching Epic: {e}")
        return False
    
    if dry_run:
        print("  [DRY RUN] Would update local file with:")
        print(f"    {epic_data}")
        return True
    
    # Update front matter fields
    content = _update_yaml_field(content, "status", epic_data["status"])
    content = _update_yaml_field(content, "assignee", epic_data["assignee"])
    content = _update_yaml_field(content, "team_id", epic_data["team_id"])
    content = _update_yaml_field(content, "sprint_id", epic_data["sprint_id"])
    content = _update_yaml_field(content, "story_points", epic_data["story_points"])
    content = _update_yaml_field(content, "pm_owner", epic_data["pm_owner"])
    content = _update_yaml_field(content, "created_at", epic_data["created"])
    content = _update_yaml_field(content, "updated_at", epic_data["updated"])
    content = _update_yaml_list_field(content, "labels", epic_data["labels"])
    
    # Update Summary section
    if epic_data["summary"]:
        summary_pattern = r"^##\s+Summary\s*\n(.*?)(?=\n##|\Z)"
        summary_replacement = f"## Summary\n\n{epic_data['summary']}\n"
        if re.search(summary_pattern, content, re.MULTILINE | re.DOTALL):
            content = re.sub(summary_pattern, summary_replacement, content, flags=re.MULTILINE | re.DOTALL)
        else:
            # Add Summary section if it doesn't exist
            fm_end = content.find('---\n', 4)
            if fm_end != -1:
                content = content[:fm_end + 4] + "\n" + summary_replacement + content[fm_end + 4:]
    
    # Update description ADF block
    if epic_data["description"]:
        content = _update_adf_block(content, r"Description", "description", epic_data["description"])
    
    # Update Problem Statement section (text field)
    if epic_data["problem_statement"]:
        problem_pattern = r"^##\s+Problem Statement\s*\n(.*?)(?=\n##|\Z)"
        problem_replacement = f"## Problem Statement\n\n{epic_data['problem_statement']}\n"
        if re.search(problem_pattern, content, re.MULTILINE | re.DOTALL):
            content = re.sub(problem_pattern, problem_replacement, content, flags=re.MULTILINE | re.DOTALL)
        else:
            # Add Problem Statement section if it doesn't exist
            # Insert after Summary or at end
            summary_match = re.search(r"^##\s+Summary", content, re.MULTILINE)
            if summary_match:
                next_section = re.search(r"^##\s+", content[summary_match.end():], re.MULTILINE)
                if next_section:
                    insert_pos = summary_match.end() + next_section.start()
                else:
                    insert_pos = len(content)
            else:
                fm_end = content.find('---\n', 4)
                insert_pos = fm_end + 4 if fm_end != -1 else len(content)
            content = content[:insert_pos] + "\n" + problem_replacement + content[insert_pos:]
    
    # Write updated content
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content)
    
    print(f"  ✓ Updated local file")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sync local Markdown Epic record(s) FROM Jira",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Sync a single file
  python3 pull_epic_from_jira.py path/to/file.md

  # Sync multiple files
  python3 pull_epic_from_jira.py file1.md file2.md file3.md

  # Sync all Epic files in a directory
  python3 pull_epic_from_jira.py current/Epics/*Epic*.md
        """
    )
    parser.add_argument(
        "files",
        nargs="+",
        help="Path(s) to the Epic .md file(s) to sync from Jira (supports glob patterns)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be updated without making changes",
    )
    
    args = parser.parse_args()
    
    # Load .env file
    script_dir = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(script_dir) if script_dir else os.getcwd()
    
    env_paths = [
        os.path.join(script_dir, ".env"),
        os.path.join(os.getcwd(), ".env"),
        os.path.join(repo_root, ".env"),
    ]
    
    loaded = False
    for env_path in env_paths:
        if load_dotenv(env_path):
            loaded = True
            break
    
    if not loaded:
        print("Warning: No .env file found. Using environment variables only.")
    
    # Expand any glob patterns in the file list
    expanded_files = []
    for pattern in args.files:
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
    
    try:
        for file_path in expanded_files:
            try:
                success = sync_epic_from_jira(file_path, dry_run=args.dry_run)
                if success:
                    success_count += 1
                else:
                    failed_count += 1
            except Exception as e:
                print(f"✗ Error processing {file_path}: {e}", file=sys.stderr)
                failed_count += 1
        
        print(f"\n{'='*60}")
        print(f"Summary: {success_count} succeeded, {failed_count} failed")
        print(f"{'='*60}")
        
        return 0 if failed_count == 0 else 1
    except KeyboardInterrupt:
        print("\nInterrupted by user")
        return 130
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

