# GitHub CLI (`gh`)

You have the `gh` CLI available, authenticated via the `GH_TOKEN` environment variable.

## Common Operations

### Pull Requests

```bash
# Create a PR from the current branch
gh pr create --title "Title" --body "Description"

# List open PRs
gh pr list

# View a specific PR
gh pr view 123

# Check CI status
gh pr checks 123
```

### Issues

```bash
# List issues
gh issue list

# View an issue
gh issue view 42

# Comment on an issue
gh issue comment 42 --body "Your comment"

# Close an issue
gh issue close 42
```

### Repository

```bash
# View repo info
gh repo view

# Search code in the repo
gh search code "query" --repo OWNER/REPO
```

## Tips

- The token is a GitHub App installation token with repo-scoped permissions.
- Prefer `gh` over raw `curl` for GitHub API calls — it handles auth and pagination.
- When creating PRs, always include a meaningful title and description.
- Use `gh pr create --fill` to auto-fill title/body from commits.
