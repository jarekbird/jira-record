#!/usr/bin/env python3
"""
Sync local Markdown record(s) FROM Jira (reverse of update_user_story.py).

This script reads one or more `.md` file(s) that follow:
  `ga-jira/.cursor/rules/local-jira-records-story-subtask.mdc`

It then fetches BOTH Jira issues and updates the local `.md` file(s) to match:
  - User Story (Story)
  - Subtask (Sub-task)

Required environment variables (same as `ga-jira/mcp.json`):
  - JIRA_BASE_URL   (e.g. "https://gnapartners.atlassian.net")
  - JIRA_EMAIL
  - JIRA_API_TOKEN

Usage:
  python3 sync_from_jira.py file1.md file2.md [--dry-run]
  python3 sync_from_jira.py current/Namespacing/Security/*.md [--dry-run]
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


# Jira custom field IDs we care about (per rules / prior usage in this repo)
CF_TEAM = "customfield_10001"
CF_SPRINT = "customfield_10020"
# Story points fields
# - customfield_10037: "Story Points" - the field that appears in the Jira UI for Stories
# - customfield_10016: "Story point estimate" (JSW story points) - used for Subtasks
CF_STORY_POINTS_STORY = "customfield_10037"  # The field shown in Jira UI for Stories
CF_STORY_POINTS_SUBTASK = "customfield_10016"  # JSW story points for Subtasks
CF_PARENT_LINK = "customfield_10014"  # Feature parent for Stories in this Jira
CF_PM_OWNER = "customfield_10246"
CF_ACCEPTANCE = "customfield_10256"
CF_TECH_NOTES = "customfield_10356"
CF_QA_TEST = "customfield_10462"


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


def _extract_front_matter_block(md: str) -> str:
    """
    Return the YAML-like front matter block for our combo records.
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


def _parse_issue_data(issue: Dict[str, Any]) -> Dict[str, Any]:
    """Extract relevant fields from Jira issue response"""
    fields = issue.get("fields", {})
    
    # Determine issue type
    issue_type = fields.get("issuetype", {}).get("name", "")
    is_story = issue_type == "Story"
    is_subtask = issue_type == "Sub-task"
    
    # Status
    status = fields.get("status", {}).get("name", "")
    
    # Assignee
    assignee_obj = fields.get("assignee")
    assignee = assignee_obj.get("emailAddress", "") if assignee_obj else ""
    
    # Created
    created = fields.get("created", "")
    
    # Parent (for Story, get from parent field; for Subtask, get from parent key)
    parent_key = ""
    parent_field = fields.get("parent")
    if parent_field:
        parent_key = parent_field.get("key", "")
    
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
    
    # Story Points - use the appropriate field based on issue type
    if is_story:
        # Stories use customfield_10037 (the UI field)
        sp = fields.get(CF_STORY_POINTS_STORY)
        story_points = "" if sp is None else str(sp)
    elif is_subtask:
        # Subtasks use customfield_10016 (JSW story points)
        sp = fields.get(CF_STORY_POINTS_SUBTASK)
        story_points = "" if sp is None else str(sp)
    else:
        # Fallback: try both
        sp = fields.get(CF_STORY_POINTS_STORY) or fields.get(CF_STORY_POINTS_SUBTASK)
        story_points = "" if sp is None else str(sp)
    
    # PM Owner (for Story only)
    pm_owner = ""
    pm_field = fields.get(CF_PM_OWNER)
    if pm_field:
        pm_owner = pm_field.get("emailAddress", "") or pm_field.get("accountId", "")
    
    # Labels
    labels = fields.get("labels", [])
    
    return {
        "status": status,
        "assignee": assignee,
        "created": created,
        "parent_key": parent_key,
        "team_id": team_id,
        "sprint_id": sprint_id,
        "story_points": story_points,
        "pm_owner": pm_owner,
        "labels": labels,
    }


def _update_yaml_field(content: str, section: str, field: str, value: str) -> str:
    """Update a YAML field in the front matter
    
    Args:
        section: Section name (e.g., "user_story", "subtask") or "" for top-level fields
        field: Field name
        value: Field value
    """
    # Format the value
    if not value:
        formatted_value = '""'
    else:
        formatted_value = f'"{value}"'
    
    if section:
        # Nested format: field is under a section (e.g., "user_story:\n  field: value")
        pattern = rf"({section}:\s*\n(?:\s*[^\n]*\n)*?\s*{field}:\s*)(?:""[^""]*""|'[^']*'|[^\n]*)"
        replacement = rf"\1{formatted_value}"
        
        if re.search(pattern, content):
            return re.sub(pattern, replacement, content)
        else:
            # Field doesn't exist, add it to the section
            section_pattern = rf"({section}:\s*\n)"
            match = re.search(section_pattern, content)
            if match:
                indent = "  "
                new_field = f"{match.group(1)}{indent}{field}: {formatted_value}\n"
                return content[:match.end()] + new_field + content[match.end():]
    else:
        # Simple format: field is at top level (e.g., "field: value")
        pattern = rf"(^{field}:\s*)(?:""[^""]*""|'[^']*'|[^\n]*)"
        replacement = rf"\1{formatted_value}"
        
        if re.search(pattern, content, re.MULTILINE):
            return re.sub(pattern, replacement, content, flags=re.MULTILINE)
        else:
            # Field doesn't exist, add it after the front matter start
            # Find the first field or the end of front matter
            first_field_match = re.search(r'^---\s*\n(.*?)(^[a-z_]+:)', content, re.MULTILINE | re.DOTALL)
            if first_field_match:
                # Insert before the first field
                insert_pos = first_field_match.end(1)
                new_field = f"{field}: {formatted_value}\n"
                return content[:insert_pos] + new_field + content[insert_pos:]
            else:
                # No fields yet, add after front matter start
                fm_start = content.find('---\n')
                if fm_start != -1:
                    insert_pos = fm_start + 4
                    new_field = f"{field}: {formatted_value}\n"
                    return content[:insert_pos] + new_field + content[insert_pos:]
    
    return content


def _update_yaml_list_field(content: str, section: str, field: str, values: List[str]) -> str:
    """Update a YAML list field in the front matter"""
    pattern = rf"({section}:\s*\n(?:\s*[^\n]*\n)*?\s*{field}:\s*)(?:\[[^\]]*\]|\[.*?\])"
    
    formatted_value = json.dumps(values) if values else "[]"
    replacement = rf"\1{formatted_value}"
    
    if re.search(pattern, content):
        return re.sub(pattern, replacement, content)
    else:
        # Add the field if it doesn't exist
        section_pattern = rf"({section}:\s*\n)"
        match = re.search(section_pattern, content)
        if match:
            indent = "  "
            new_field = f"{match.group(1)}{indent}{field}: {formatted_value}\n"
            return content[:match.end()] + new_field + content[match.end():]
    
    return content


def sync_file_from_jira(file_path: str, *, dry_run: bool = False) -> bool:
    """Sync a single .md file from Jira"""
    if not os.path.exists(file_path):
        print(f"Error: File not found: {file_path}")
        return False
    
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()
    
    # Extract User Story and Subtask keys - handle both formats:
    # 1. user_story:/subtask: format (with nested jira_key)
    # 2. Simple top-level format (just jira_key at root level)
    story_match = re.search(r'user_story:\s*\n\s*jira_key:\s*"([^"]+)"', content)
    subtask_match = re.search(r'subtask:\s*\n\s*jira_key:\s*"([^"]+)"', content)
    
    # If not found in nested format, try simple top-level format
    if not story_match:
        story_match = re.search(r'^jira_key:\s*"([^"]+)"', content, re.MULTILINE)
    
    if not story_match:
        print(f"Error: Could not find jira_key in {file_path}")
        return False
    
    story_key = story_match.group(1)
    subtask_key = subtask_match.group(1) if subtask_match else None
    
    # Determine if this is the simple format (no user_story: section)
    is_simple_format = not re.search(r'^user_story:\s*$', content, re.MULTILINE)
    
    print(f"\n{os.path.basename(file_path)}:")
    print(f"  Story:   {story_key}")
    if subtask_key:
        print(f"  Subtask: {subtask_key}")
    
    # Fetch Story from Jira
    try:
        story_issue = jira_get_issue(
            story_key,
            fields=[
                "status",
                "assignee",
                "created",
                "parent",
                "issuetype",
                CF_TEAM,
                CF_SPRINT,
                CF_STORY_POINTS_STORY,
                CF_PM_OWNER,
                "labels",
            ],
        )
        story_data = _parse_issue_data(story_issue)
        print(f"  ✓ Fetched Story: status={story_data['status']}, assignee={story_data['assignee'] or 'unassigned'}")
    except Exception as e:
        print(f"  ✗ Error fetching Story: {e}")
        return False
    
    # Fetch Subtask from Jira
    subtask_data = None
    if subtask_key:
        try:
            subtask_issue = jira_get_issue(
                subtask_key,
                fields=[
                    "status",
                    "assignee",
                    "created",
                    "parent",
                    "issuetype",
                    CF_TEAM,
                    CF_SPRINT,
                    CF_STORY_POINTS_SUBTASK,
                ],
            )
            subtask_data = _parse_issue_data(subtask_issue)
            print(f"  ✓ Fetched Subtask: status={subtask_data['status']}, assignee={subtask_data['assignee'] or 'unassigned'}")
        except Exception as e:
            print(f"  ✗ Error fetching Subtask: {e}")
            return False
    
    if dry_run:
        print("  [DRY RUN] Would update local file with:")
        print(f"    Story: {story_data}")
        if subtask_data:
            print(f"    Subtask: {subtask_data}")
        if is_simple_format:
            print("  [DRY RUN] Would convert file to nested format (user_story:/subtask:)")
        return True
    
    # If simple format, convert to nested format first
    if is_simple_format:
        # Extract the body content (everything after the front matter)
        fm_end = content.find('---\n', 4)  # Find second ---
        if fm_end == -1:
            fm_end = content.find('\n---\n', 3)
            if fm_end != -1:
                fm_end += 1
        if fm_end == -1:
            body = ""
            old_fm = content
        else:
            body = content[fm_end + 4:]  # Skip the closing ---
            old_fm = content[:fm_end + 4]
        
        # Extract existing fields from simple format
        existing_jira_key = story_key
        existing_issue_type = re.search(r'^issue_type:\s*"([^"]+)"', old_fm, re.MULTILINE)
        existing_issue_type = existing_issue_type.group(1) if existing_issue_type else "Story"
        
        # Build new nested format front matter
        new_fm = "---\n"
        new_fm += "# User Story metadata\n"
        new_fm += "user_story:\n"
        new_fm += f'  jira_key: "{existing_jira_key}"      # User Story key\n'
        new_fm += f'  issue_type: "{existing_issue_type}"\n'
        new_fm += f'  parent_key: "{story_data["parent_key"]}"\n'
        new_fm += f'  status: "{story_data["status"]}"\n'
        new_fm += f'  team_id: "{story_data["team_id"]}"\n'
        new_fm += f'  sprint_id: "{story_data["sprint_id"]}"\n'
        new_fm += f'  pm_owner: "{story_data["pm_owner"]}"\n'
        new_fm += f'  assignee: "{story_data["assignee"]}"\n'
        new_fm += f'  story_points: "{story_data["story_points"]}"\n'
        new_fm += f'  labels: {json.dumps(story_data["labels"])}                # Optional\n'
        new_fm += f'  created_at: "{story_data["created"]}"\n'
        new_fm += "\n"
        new_fm += "# Subtask metadata\n"
        new_fm += "subtask:\n"
        if subtask_data:
            new_fm += f'  jira_key: "{subtask_key}"      # Subtask key\n'
            new_fm += f'  issue_type: "Subtask"\n'
            new_fm += f'  parent_key: "{existing_jira_key}"   # User Story key - matches user_story.jira_key\n'
            new_fm += f'  status: "{subtask_data["status"]}"\n'
            new_fm += f'  assignee: "{subtask_data["assignee"]}"\n'
            new_fm += f'  team_id: "{subtask_data["team_id"]}"\n'
            new_fm += f'  sprint_id: "{subtask_data["sprint_id"]}"\n'
            new_fm += f'  story_points: "{subtask_data["story_points"]}"\n'
            new_fm += f'  created_at: "{subtask_data["created"]}"\n'
        else:
            new_fm += f'  jira_key: ""      # Subtask key\n'
            new_fm += f'  issue_type: "Subtask"\n'
            new_fm += f'  parent_key: "{existing_jira_key}"   # User Story key - matches user_story.jira_key\n'
            new_fm += f'  status: "Dev Ready"\n'
            new_fm += f'  assignee: ""\n'
            new_fm += f'  team_id: ""\n'
            new_fm += f'  sprint_id: ""\n'
            new_fm += f'  story_points: ""\n'
            new_fm += f'  created_at: ""\n'
        new_fm += "---\n"
        
        # Reconstruct content with new format
        content = new_fm + "\n" + body
        print("  ✓ Converted file to nested format (user_story:/subtask:)")
    else:
        # Nested format: do full front matter replacement for consistency
        # Extract the body content (everything after the front matter)
        fm_end = content.find('---\n', 4)  # Find second ---
        if fm_end == -1:
            fm_end = content.find('\n---\n', 3)
            if fm_end != -1:
                fm_end += 1
        if fm_end == -1:
            body = ""
        else:
            body = content[fm_end + 4:]  # Skip the closing ---
            # Remove any extra blank lines after front matter
            body = body.lstrip('\n')
        
        # Extract existing jira_key and issue_type from current file
        existing_story_key_match = re.search(r'user_story:\s*\n\s*jira_key:\s*"([^"]+)"', content)
        existing_story_key = existing_story_key_match.group(1) if existing_story_key_match else story_key
        
        existing_story_issue_type_match = re.search(r'user_story:\s*\n\s*issue_type:\s*"([^"]+)"', content)
        existing_story_issue_type = existing_story_issue_type_match.group(1) if existing_story_issue_type_match else "Story"
        
        existing_subtask_key_match = re.search(r'subtask:\s*\n\s*jira_key:\s*"([^"]+)"', content)
        existing_subtask_key = existing_subtask_key_match.group(1) if existing_subtask_key_match else (subtask_key if subtask_key else "")
        
        # Build complete new front matter with all fields from Jira
        new_fm = "---\n"
        new_fm += "# User Story metadata\n"
        new_fm += "user_story:\n"
        new_fm += f'  jira_key: "{existing_story_key}"      # User Story key\n'
        new_fm += f'  issue_type: "{existing_story_issue_type}"\n'
        new_fm += f'  parent_key: "{story_data["parent_key"]}"\n'
        new_fm += f'  status: "{story_data["status"]}"\n'
        new_fm += f'  team_id: "{story_data["team_id"]}"\n'
        new_fm += f'  sprint_id: "{story_data["sprint_id"]}"\n'
        new_fm += f'  pm_owner: "{story_data["pm_owner"]}"\n'
        new_fm += f'  assignee: "{story_data["assignee"]}"\n'
        new_fm += f'  story_points: "{story_data["story_points"]}"\n'
        new_fm += f'  labels: {json.dumps(story_data["labels"])}                # Optional\n'
        new_fm += f'  created_at: "{story_data["created"]}"\n'
        new_fm += "\n"
        new_fm += "# Subtask metadata\n"
        new_fm += "subtask:\n"
        if subtask_data:
            new_fm += f'  jira_key: "{existing_subtask_key}"      # Subtask key\n'
            new_fm += f'  issue_type: "Subtask"\n'
            new_fm += f'  parent_key: "{existing_story_key}"   # User Story key - matches user_story.jira_key\n'
            new_fm += f'  status: "{subtask_data["status"]}"\n'
            new_fm += f'  assignee: "{subtask_data["assignee"]}"\n'
            new_fm += f'  team_id: "{subtask_data["team_id"]}"\n'
            new_fm += f'  sprint_id: "{subtask_data["sprint_id"]}"\n'
            new_fm += f'  story_points: "{subtask_data["story_points"]}"\n'
            new_fm += f'  created_at: "{subtask_data["created"]}"\n'
        else:
            new_fm += f'  jira_key: "{existing_subtask_key}"      # Subtask key\n'
            new_fm += f'  issue_type: "Subtask"\n'
            new_fm += f'  parent_key: "{existing_story_key}"   # User Story key - matches user_story.jira_key\n'
            new_fm += f'  status: "Dev Ready"\n'
            new_fm += f'  assignee: ""\n'
            new_fm += f'  team_id: ""\n'
            new_fm += f'  sprint_id: ""\n'
            new_fm += f'  story_points: ""\n'
            new_fm += f'  created_at: ""\n'
        new_fm += "---\n"
        
        # Reconstruct content with new front matter
        content = new_fm + "\n" + body
        print("  ✓ Replaced front matter with full update from Jira")
    
    # Write updated content
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content)
    
    print(f"  ✓ Updated local file")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sync local Markdown record(s) FROM Jira",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Sync a single file
  python3 sync_from_jira.py path/to/file.md

  # Sync multiple files
  python3 sync_from_jira.py file1.md file2.md file3.md

  # Sync all files in a directory
  python3 sync_from_jira.py current/Namespacing/Security/*.md
        """
    )
    parser.add_argument(
        "files",
        nargs="+",
        help="Path(s) to the .md file(s) to sync from Jira (supports glob patterns)",
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
                success = sync_file_from_jira(file_path, dry_run=args.dry_run)
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

