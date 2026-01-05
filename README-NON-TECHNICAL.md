# Jira Record Repository - Getting Started Guide

Welcome! This repository helps you manage Jira issues (Epics, Features, User Stories, and Subtasks) as local files that sync with Jira. You can edit issues offline, track changes, and keep everything organized.

## What This Repository Does

This repository lets you:
- Store Jira issues as local files you can edit
- Automatically sync changes between your files and Jira
- Organize issues in folders that make sense for your project
- Work offline and sync when ready

## Getting Started with Cursor

Follow these prompts in Cursor to get everything set up:

### Step 0: Check and Install Python

**First, check if Python is installed:**

**Prompt to Cursor:**
```
Check if Python 3 is installed on this system. If it's not installed, provide instructions for installing Python 3.7 or higher for my operating system.
```

**If Python is not installed, use one of these prompts based on your system:**

**For macOS:**
```
I'm on macOS. Help me install Python 3 using Homebrew, or provide instructions for downloading it from python.org.
```

**For Windows:**
```
I'm on Windows. Help me install Python 3 from the Microsoft Store or from python.org, and make sure it's added to my PATH.
```

**For Linux:**
```
I'm on Linux. Help me install Python 3 using my package manager (apt, yum, etc.).
```

**Verify Python is working:**

**Prompt to Cursor:**
```
Verify that Python 3 is installed and accessible. Check the version to make sure it's 3.7 or higher.
```

### Step 1: Set Up Your Environment

**Prompt to Cursor:**
```
Create a .env file in the jira-record directory with placeholder values for:
- JIRA_BASE_URL (e.g., https://your-instance.atlassian.net)
- JIRA_EMAIL (your email address)
- JIRA_API_TOKEN (placeholder text explaining where to get this)
```

**Then manually:**
1. Get your Jira API token from: https://id.atlassian.com/manage-profile/security/api-tokens
2. Click "Create API token" and copy it
3. Update the `.env` file with your actual values

### Step 2: Create Your First Folder Structure

**Prompt to Cursor:**
```
Create a folder structure in jira-record/issues/ for organizing Jira issues. 
Create a main folder called "MyProject" as an example.
```

### Step 3: Create Your First Issue Template

**Prompt to Cursor:**
```
Create a DRAFT Feature markdown file in jira-record/issues/MyProject/ following the template from .cursor/rules/local-jira-records-feature.mdc. 
Name it "DRAFT - Feature - My First Feature.md" and include placeholder content for:
- Summary
- Business Problem
- High-Level Scope
- Success Metrics
```

### Step 4: Create the Issue in Jira

**Prompt to Cursor:**
```
Create this DRAFT Feature in Jira.
```

### Step 5: Pull an Existing Issue from Jira

**Prompt to Cursor:**
```
I have an existing Jira Feature with key WOR-123. Create a local markdown file for it and pull the latest data from Jira.
```

### Step 6: Make Changes and Sync

**Prompt to Cursor:**
```
I've edited the Feature file. Sync my changes to Jira.
```

## Common Tasks

### Creating a New Epic

**Prompt to Cursor:**
```
Create a DRAFT Epic markdown file in jira-record/issues/MyProject/ following the template from .cursor/rules/local-jira-records-epic.mdc. 
Name it "DRAFT - Epic - My Epic.md" with placeholder content, then create it in Jira.
```

### Creating a User Story with Subtask

**Prompt to Cursor:**
```
Create a DRAFT User Story markdown file in jira-record/issues/MyProject/ following the template from .cursor/rules/local-jira-records-story-subtask.mdc. 
Name it "DRAFT - Story - My User Story.md" with placeholder content, then create it in Jira.
```

### Updating Multiple Issues

**Prompt to Cursor:**
```
I've edited several Feature files in jira-record/issues/MyProject/. 
Sync all the changes to Jira.
```

### Pulling Latest Data from Jira

**Prompt to Cursor:**
```
Pull the latest data from Jira for all Feature files in jira-record/issues/MyProject/.
```

## Understanding the File Structure

Each Jira issue is stored as a Markdown file with:
- **Header section** - Contains metadata like issue key, status, assignee
- **Content sections** - Your issue description, requirements, etc.
- **Technical sections** - Hidden code blocks with data for Jira (you don't need to edit these)

## Tips

1. **Let Cursor handle it** - Cursor automatically knows which scripts to use based on the file type and what you're asking to do. Just describe what you want to accomplish.

2. **DRAFT files** - Files starting with "DRAFT" don't have a Jira key yet. Ask Cursor to "create this in Jira" and it will handle it.

3. **File naming** - After creating an issue, the file is automatically renamed to include the Jira key (e.g., `WOR-123 - Feature Name.md`).

4. **Preview changes** - Ask Cursor to "show me what would happen" or "dry run" before syncing to see changes without applying them.

5. **Organize by project** - Create folders under `issues/` for different projects, features, or teams.

## Example Workflow

1. **Create a new Feature:**
   - Ask Cursor to create a DRAFT Feature file
   - Fill in the content
   - Ask Cursor to "create this in Jira"

2. **Edit an existing issue:**
   - Open the markdown file
   - Make your changes
   - Ask Cursor to "sync my changes to Jira"

3. **Get latest from Jira:**
   - Ask Cursor to "pull the latest data from Jira" for your file
   - Your local file updates with the latest Jira data

## Need Help?

If something doesn't work:
- Check that your `.env` file has the correct values
- Make sure your Jira API token is valid
- Try using `--dry-run` to see what the script would do
- Ask Cursor to help troubleshoot the error message

## Getting More Help

The templates in `.cursor/rules/` provide detailed structure for each issue type. Cursor will automatically reference these when helping you create or manage Jira issues.

