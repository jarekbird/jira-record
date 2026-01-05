# Jira Record Repository

A blank canvas repository for local Jira issue storage and integration. This repository provides a structured way to manage Jira issues (Epics, Features, User Stories, and Subtasks) as local Markdown files with bidirectional synchronization to Jira.

## Overview

This repository is designed to:
- Store Jira issues as local Markdown files for offline editing and version control
- Synchronize changes bidirectionally between local files and Jira
- Organize issues in a folder structure that makes sense for your project
- Provide automation scripts for common Jira operations

## Repository Structure

```
jira-record/
├── .cursor/
│   └── rules/              # Cursor AI rules for Jira issue management
├── jira-scripts/           # Python scripts for Jira operations
│   ├── create_epic_in_jira.py
│   ├── create_feature_in_jira.py
│   ├── create_story_in_jira.py
│   ├── pull_epic_from_jira.py
│   ├── pull_feature_from_jira.py
│   ├── pull_from_jira.py
│   ├── sync_epic_to_jira.py
│   ├── sync_feature_to_jira.py
│   └── sync_to_jira.py
├── issues/                 # Jira issues organized by project/feature (create your own structure)
└── .env                    # Environment variables (not in git)
```

## Prerequisites

- Python 3.7 or higher
- Access to a Jira instance (Atlassian Cloud)
- Jira API token (see [Getting Your API Token](#getting-your-api-token))

## Setup

### 1. Environment Configuration

Create a `.env` file in the repository root with your Jira credentials:

```bash
JIRA_BASE_URL=https://your-instance.atlassian.net
JIRA_EMAIL=your-email@example.com
JIRA_API_TOKEN=your-api-token
```

**Getting Your API Token:**
1. Go to https://id.atlassian.com/manage-profile/security/api-tokens
2. Click "Create API token"
3. Copy the token and add it to your `.env` file

### 2. Organize Your Issues

Create a folder structure under `issues/` that makes sense for your project. For example:

```
issues/
├── ProjectA/
│   ├── WOR-123 - Feature Name.md
│   └── WOR-124 - User Story Name.md
└── ProjectB/
    └── WOR-125 - Another Feature.md
```

## Available Scripts

### Epic Management

- **`create_epic_in_jira.py`** - Create Epic issues from DRAFT markdown files
- **`pull_epic_from_jira.py`** - Pull Epic data from Jira to update local files
- **`sync_epic_to_jira.py`** - Sync local Epic changes to Jira

### Feature Management

- **`create_feature_in_jira.py`** - Create Feature issues from DRAFT markdown files
- **`pull_feature_from_jira.py`** - Pull Feature data from Jira to update local files
- **`sync_feature_to_jira.py`** - Sync local Feature changes to Jira

### User Story and Subtask Management

- **`create_story_in_jira.py`** - Create User Story + Subtask issues from DRAFT markdown files
- **`pull_from_jira.py`** - Pull User Story + Subtask data from Jira to update local files
- **`sync_to_jira.py`** - Sync local User Story + Subtask changes to Jira

## Usage Examples

### Creating a New Issue

1. Create a DRAFT markdown file following the template (see `.cursor/rules/` for templates)
2. Run the appropriate create script:

```bash
python3 jira-scripts/create_feature_in_jira.py issues/ProjectA/DRAFT - Feature - My Feature.md
```

The script will:
- Create the issue in Jira
- Update the file with the `jira_key`
- Rename the file to include the Jira key (e.g., `WOR-123 - My Feature.md`)

### Pulling Data from Jira

To sync local files with current Jira data:

```bash
python3 jira-scripts/pull_feature_from_jira.py issues/ProjectA/WOR-123*.md
```

### Syncing Local Changes to Jira

To push local changes to Jira:

```bash
python3 jira-scripts/sync_feature_to_jira.py issues/ProjectA/WOR-123*.md
```

### Batch Operations

All scripts support glob patterns and multiple files:

```bash
# Sync all Features in a folder
python3 jira-scripts/sync_feature_to_jira.py issues/ProjectA/*Feature*.md

# Sync multiple specific files
python3 jira-scripts/sync_to_jira.py file1.md file2.md file3.md
```

### Dry Run Mode

Preview changes without making them:

```bash
python3 jira-scripts/sync_feature_to_jira.py issues/ProjectA/WOR-123*.md --dry-run
```

## Markdown File Structure

Each Jira issue is stored as a Markdown file with:
- **YAML front matter** - Metadata (jira_key, status, assignee, etc.)
- **Markdown content** - Human-readable issue content
- **ADF JSON blocks** - Rich text fields in collapsible code blocks

See `.cursor/rules/` for detailed templates:
- `local-jira-records-epic.mdc` - Epic structure
- `local-jira-records-feature.mdc` - Feature structure
- `local-jira-records-story-subtask.mdc` - User Story + Subtask structure

## Script Features

- **Automatic .env loading** - Searches common locations for environment files
- **Error handling** - Handles closed sprints, invalid transitions, etc.
- **ADF normalization** - Automatically fixes ADF formatting issues
- **User lookup** - Resolves email addresses to Jira account IDs
- **Status transitions** - Automatically transitions issues to desired status

## Reference Documentation

For detailed information, see:
- `.cursor/rules/jira-scripts-usage.mdc` - Script usage guidelines
- `.cursor/rules/local-jira-records-epic.mdc` - Epic structure template
- `.cursor/rules/local-jira-records-feature.mdc` - Feature structure template
- `.cursor/rules/local-jira-records-story-subtask.mdc` - User Story + Subtask structure template

## Troubleshooting

### "Missing required env var" Error
- Ensure your `.env` file exists in the repository root
- Check that all three required variables are set: `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`

### "Failed to create issue" Error
- Verify your API token is valid and not expired
- Check that you have permission to create issues in the target project
- Ensure the issue type ID matches your Jira configuration

### "No available transition" Error
- The desired status transition may not be available in your Jira workflow
- Check the current status and available transitions in Jira
- Use `--no-transition` flag to skip status updates if needed

## Contributing

This is a template repository. Customize it for your organization's needs:
- Adjust folder structure in `issues/`
- Modify scripts if your Jira instance has custom fields
- Update `.cursor/rules/` to match your workflows

