# Release process

`slim-tg-mcp` releases via **PyPI Trusted Publishing (OIDC)** — no
long-lived API token lives anywhere. Every `vX.Y.Z` tag pushed to
GitHub triggers a workflow that re-runs the test matrix, builds, and
publishes via a short-lived OIDC-issued token.

If something here disagrees with `.github/workflows/publish.yml`, the
workflow is the source of truth.

---

## 1. One-time setup (already done — for reference)

Done once on PyPI. You only need to redo this if:
- You revoke + recreate the project on PyPI
- You move the repo to a different GitHub org/name
- You rename the workflow file

### 1.1 Register the trusted publisher on PyPI

For an EXISTING project (slim-tg-mcp already exists on PyPI):

1. Log in at https://pypi.org as the project owner.
2. Go to **Project → slim-tg-mcp → Settings → Publishing**:
   https://pypi.org/manage/project/slim-tg-mcp/settings/publishing/
3. Under "Add a new trusted publisher", choose **GitHub**.
4. Fill in **exactly** these values:

   | Field | Value |
   |---|---|
   | PyPI Project Name | `slim-tg-mcp` |
   | Owner | `haoyu-haoyu` |
   | Repository name | `slim-tg-mcp` |
   | Workflow filename | `publish.yml` |
   | Environment name | `pypi` |

5. Click "Add". The publisher appears in the list.

For a BRAND-NEW project (PyPI doesn't have the name yet), use the
account-level **"Pending Publisher"** flow instead:
https://pypi.org/manage/account/publishing/ → "Add a new pending
publisher" → fill in the same 5 fields plus the project name PyPI
should reserve. PyPI then creates the project on the first
successful OIDC publish.

### 1.2 Create the GitHub environment

1. GitHub repo → **Settings → Environments → New environment**.
2. Name: `pypi` (must match `environment.name` in `publish.yml`).
3. Optional but recommended:
   - **Required reviewers** = yourself. Adds a one-click manual
     approval step before the publish actually fires. Tag pushes
     queue the workflow; you click "Approve" before it runs the
     publish step. Stops accidental tag pushes from auto-shipping.
   - **Deployment branches** = `Selected → main` (and any release
     branches). Tags must be on those branches.

### 1.3 Verify the binding

After step 1.1 + 1.2, you can test the wiring without actually
shipping anything:

```bash
# Trigger the workflow manually with workflow_dispatch (no tag).
# It runs the test matrix + build, then SKIPS the publish job (which
# is gated by `if: startsWith(github.ref, 'refs/tags/v')`). The
# build job's tag/version check is also gated, so a manual run on
# `main` produces a clean green build with no upload.
gh workflow run publish.yml
```

Or push a throwaway tag pointing at a known-good commit. PyPI lets
you delete a release file within ~24 h via the project's "Files"
page, but the **version number itself is permanently burned** —
you can never re-upload `slim-tg-mcp==X.Y.Z` once that exact
version was ever taken. Use a `0.0.0a1`-style pre-release version
for binding tests if you want to throw it away cleanly.

---

## 2. Cutting a release

```bash
# 1. Bump version in pyproject.toml.
#    The publish workflow REFUSES to publish if the tag doesn't
#    match `v<pyproject.version>` — defense against 'wrong tag'
#    accidents.
$EDITOR pyproject.toml      # version = "0.6.0"

# 2. Update CHANGELOG.md with the new section.

# 3. Commit + push to main.
git add pyproject.toml CHANGELOG.md
git commit -m "release: v0.6.0"
git push origin main

# 4. Tag and push the tag. THIS triggers the workflow.
git tag -a v0.6.0 -m "v0.6.0"
git push origin v0.6.0

# 5. Watch the run.
gh run watch
# or open https://github.com/haoyu-haoyu/slim-tg-mcp/actions

# 6. If you set "Required reviewers" on the pypi environment in
#    1.2, GitHub will ASK you to approve the publish step. Click
#    "Review deployments" → check the box → "Approve and deploy".

# 7. Verify it's live.
pip index versions slim-tg-mcp   # should list 0.6.0
# or visit https://pypi.org/project/slim-tg-mcp/0.6.0/
```

---

## 3. What happens under the hood

```
git push origin v0.6.0
        │
        ▼
┌───────────────────────────────────────────────┐
│ GitHub Actions: publish.yml                   │
│                                               │
│ 1. test job (Python 3.10/3.11/3.12)           │
│    ↓ all green required                       │
│ 2. build job:                                 │
│    - Verify tag matches pyproject version     │
│    - python -m build → wheel + sdist          │
│    - twine check → metadata sane              │
│    - upload artifacts to GitHub               │
│    ↓                                          │
│ 3. publish job (environment: pypi):           │
│    - GitHub mints an OIDC ID token            │
│      (short-lived, scoped to this run)        │
│    - pypa/gh-action-pypi-publish hands the    │
│      OIDC token to PyPI's /pypi/oidc endpoint │
│    - PyPI verifies the workflow + repo +      │
│      environment match the registered         │
│      Trusted Publisher                        │
│    - PyPI returns a 15-minute API token       │
│    - twine upload to /legacy/ with that token │
│    ↓                                          │
│ 4. Done. The 15-minute token is now expired   │
│    and there's nothing persistent to leak.    │
└───────────────────────────────────────────────┘
```

The OIDC token never touches disk anywhere — it lives in the
GitHub runner's memory for the few seconds the action takes. There
is no API key in repo secrets, no `.pypirc`, no env var anywhere
that survives the run.

---

## 4. Failure modes & how to recover

### "PyPI rejected the OIDC token"
- The trusted publisher entry on PyPI doesn't match the workflow.
  Re-check the 5 fields in §1.1. The most common slip-up is the
  `environment: pypi` field (must match exactly, case-sensitive).
- The tag was pushed from a non-`main` branch and your environment
  has a `Deployment branches` restriction.

### "tag does not match pyproject version"
- The version-check step in the build job caught the discrepancy.
  Bump `pyproject.toml`, commit, then re-tag. If you'd already
  pushed the bad tag, delete it on remote (`git push --delete
  origin vX.Y.Z`) before re-pushing.

### "File already exists"
- You can never re-upload the same `<package>-<version>` to PyPI.
  Bump to `vX.Y.Z+1` (or `.post1` for a metadata-only fix) and
  re-tag.

### Required-reviewer blocked indefinitely
- A queued deployment waiting for approval times out after 30 days
  of inactivity. The fix is to dismiss + retag, not to fight the
  timeout.

---

## 5. Why Trusted Publishing instead of an API token

| Property | Long-lived API token | Trusted Publishing (OIDC) |
|---|---|---|
| Lives somewhere on disk / in repo secrets | Yes (always) | No |
| Can be exfiltrated by a malicious dependency / leaked log line | Yes | No (never persisted) |
| Expires automatically | No (manual rotation) | Yes (15 min) |
| Scope | Account-wide or project | Project, per-workflow |
| Compromise blast radius | Full re-publish until rotated | One workflow run |
| Recovery from compromise | Revoke + recreate token + reconfigure CI | Already auto-expired |
| Setup cost | Generate token + paste into secret | One PyPI form, once |

Trusted Publishing is the new default for everything that can use
it. Don't go back to long-lived tokens unless you have a hard
constraint (e.g., GitLab self-hosted without OIDC support yet).
