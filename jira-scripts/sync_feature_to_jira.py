#!/usr/bin/env python3
"""
Update Jira for local Feature Markdown record(s).

This script reads one or more `.md` file(s) that follow:
  `ga-jira/.cursor/rules/local-jira-records-feature.mdc`

It then updates the Feature issue via Jira REST API v3.

Required environment variables (same as `ga-jira/mcp.json`):
  - JIRA_BASE_URL   (e.g. "https://gnapartners.atlassian.net")
  - JIRA_EMAIL
  - JIRA_API_TOKEN

Usage:
  python3 sync_feature_to_jira.py file1.md file2.md [--dry-run] [--no-transition]
  python3 sync_feature_to_jira.py current/Namespacing/Reporting/*Feature*.md [--dry-run] [--no-transition]
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


# Jira custom field IDs for Features
CF_TEAM = "customfield_10001"
CF_SPRINT = "customfield_10020"
CF_PM_OWNER = "customfield_10246"
CF_BUSINESS_PROBLEM = "customfield_10255"
CF_HIGH_LEVEL_SCOPE = "customfield_10323"
CF_SUCCESS_METRICS = "customfield_10391"
CF_TECH_NOTES = "customfield_10356"
CF_STORY_POINTS = "customfield_10026"  # Feature story points


@dataclass(frozen=True)
class FeatureMeta:
    jira_key: str
    parent_key: str  # Epic key
    status: str
    team_id: str
    sprint_id: str
    assignee: str
    pm_owner: str
    story_points: str
    labels: List[str]


@dataclass(frozen=True)
class ParsedFeatureRecord:
    feature: FeatureMeta
    summary: str
    adf_fields: Dict[str, Any]  # description, business_problem, high_level_scope, success_metrics, tech_notes


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


def _extract_adf_blocks(md: str) -> Dict[str, Any]:
    """Extract ADF JSON blocks from the markdown for Feature fields."""
    fields: Dict[str, Any] = {}
    
    lines = md.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        
        # Look for ADF Version markers
        m = re.search(r"ADF Version\s+\((description|customfield_\d+)\)", line)
        if m:
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
                    fields[field_id] = parsed
                except json.JSONDecodeError as e:
                    raise ValueError(f"Invalid JSON in ADF block for {field_id}: {e}") from e
            i = k  # jump to closing fence
        i += 1
    
    return fields


def _parse_feature_record(md: str) -> ParsedFeatureRecord:
    """Parse a Feature markdown file."""
    fm = _extract_front_matter_block(md)
    
    # Parse front matter fields
    jira_key = ""
    parent_key = ""
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
        elif line.startswith("parent_key:"):
            parent_key = _parse_yaml_value(line)
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
    
    if not jira_key:
        raise ValueError("jira_key is required")
    
    # Extract summary from ## Summary section
    summary_match = re.search(r"^##\s+Summary\s*\n(.*?)(?=\n##|\Z)", md, re.MULTILINE | re.DOTALL)
    summary = summary_match.group(1).strip() if summary_match else ""
    
    # Extract ADF blocks
    adf_fields = _extract_adf_blocks(md)
    
    feature = FeatureMeta(
        jira_key=jira_key.strip(),
        parent_key=parent_key.strip(),
        status=status.strip(),
        team_id=team_id.strip(),
        sprint_id=sprint_id.strip(),
        assignee=assignee.strip(),
        pm_owner=pm_owner.strip(),
        story_points=story_points.strip(),
        labels=labels,
    )
    
    return ParsedFeatureRecord(feature=feature, summary=summary, adf_fields=adf_fields)


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


def _build_feature_update(record: ParsedFeatureRecord, *, allow_network: bool) -> Dict[str, Any]:
    f = record.feature
    fields: Dict[str, Any] = {}
    
    # Summary
    if record.summary:
        fields["summary"] = record.summary
    
    # Parent (Epic)
    if f.parent_key:
        fields["parent"] = {"key": f.parent_key}
    
    # Labels
    if f.labels and len(f.labels) > 0:
        fields["labels"] = f.labels
    
    # Team
    if f.team_id:
        fields[CF_TEAM] = f.team_id
    
    # Sprint
    sprint = _maybe_int(f.sprint_id)
    if sprint is not None and f.sprint_id.strip():
        fields[CF_SPRINT] = sprint
    
    # Story points
    sp = _maybe_float(f.story_points)
    if sp is not None:
        fields[CF_STORY_POINTS] = sp
    
    # PM Owner
    pm_id = jira_find_user_account_id(f.pm_owner, allow_network=allow_network)
    if pm_id:
        fields[CF_PM_OWNER] = {"accountId": pm_id}
    elif f.pm_owner and not allow_network:
        fields[CF_PM_OWNER] = {"accountId": f"<unresolved:{f.pm_owner}>"}
    
    # Assignee
    asg_id = jira_find_user_account_id(f.assignee, allow_network=allow_network)
    if asg_id:
        fields["assignee"] = {"accountId": asg_id}
    elif f.assignee and not allow_network:
        fields["assignee"] = {"accountId": f"<unresolved:{f.assignee}>"}
    
    # ADF fields - normalize and include if present
    if "description" in record.adf_fields and record.adf_fields["description"] is not None:
        fields["description"] = _normalize_adf_marks(record.adf_fields["description"])
    if CF_BUSINESS_PROBLEM in record.adf_fields and record.adf_fields[CF_BUSINESS_PROBLEM] is not None:
        fields[CF_BUSINESS_PROBLEM] = _normalize_adf_marks(record.adf_fields[CF_BUSINESS_PROBLEM])
    if CF_HIGH_LEVEL_SCOPE in record.adf_fields and record.adf_fields[CF_HIGH_LEVEL_SCOPE] is not None:
        fields[CF_HIGH_LEVEL_SCOPE] = _normalize_adf_marks(record.adf_fields[CF_HIGH_LEVEL_SCOPE])
    if CF_SUCCESS_METRICS in record.adf_fields and record.adf_fields[CF_SUCCESS_METRICS] is not None:
        fields[CF_SUCCESS_METRICS] = _normalize_adf_marks(record.adf_fields[CF_SUCCESS_METRICS])
    if CF_TECH_NOTES in record.adf_fields and record.adf_fields[CF_TECH_NOTES] is not None:
        fields[CF_TECH_NOTES] = _normalize_adf_marks(record.adf_fields[CF_TECH_NOTES])
    
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
    """Process a single Feature .md file and update Jira. Returns True on success, False on failure."""
    try:
        with open(md_path, "r", encoding="utf-8") as f:
            md = f.read()
    except FileNotFoundError:
        print(f"✗ File not found: {md_path}", file=sys.stderr)
        return False

    try:
        record = _parse_feature_record(md)
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

    feature_key = record.feature.jira_key

    feature_fields = _build_feature_update(record, allow_network=can_call_jira and not args.dry_run)

    print(f"\n{os.path.basename(md_path)}:")
    print(f"  Feature: {feature_key}")

    if args.dry_run:
        print("  [dry-run] Feature field updates:")
        print(json.dumps(feature_fields, indent=4, sort_keys=True))
    else:
        # Update Feature fields
        try:
            jira_update_issue_fields(feature_key, feature_fields)
            print(f"  ✓ Updated Feature fields: {feature_key}")
        except JiraApiError as e:
            # If error is about closed sprint, retry without sprint field
            if "can be assigned only active or future sprints" in str(e) and CF_SPRINT in feature_fields:
                print(f"  ⚠ Warning: Sprint is closed, retrying without sprint field", file=sys.stderr)
                feature_fields_no_sprint = {k: v for k, v in feature_fields.items() if k != CF_SPRINT}
                try:
                    jira_update_issue_fields(feature_key, feature_fields_no_sprint)
                    print(f"  ✓ Updated Feature fields (without sprint): {feature_key}")
                except JiraApiError as e2:
                    print(f"  ✗ Failed to update Feature {feature_key}: {e2}", file=sys.stderr)
                    return False
            else:
                print(f"  ✗ Failed to update Feature {feature_key}: {e}", file=sys.stderr)
                return False

    if not args.no_transition:
        if not can_call_jira:
            print("  ⚠ Skipping transitions: Jira env vars not set.", file=sys.stderr)
        else:
            try:
                _maybe_transition(feature_key, record.feature.status, dry_run=args.dry_run)
            except Exception as e:
                print(f"  ⚠ Feature transition warning: {e}", file=sys.stderr)

    return True


def main():
    parser = argparse.ArgumentParser(
        description="Update Jira Feature from local .md record(s)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Update a single file
  python3 sync_feature_to_jira.py path/to/feature.md

  # Update multiple files
  python3 sync_feature_to_jira.py file1.md file2.md

  # Update all Feature files in a directory
  python3 sync_feature_to_jira.py current/Namespacing/Reporting/*Feature*.md
        """
    )
    parser.add_argument(
        "md_files",
        nargs="+",
        help="Path(s) to the local Feature .md file(s) (supports glob patterns)",
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

