# Customization Guide — CPI iFlow GitHub Sync (PowerShell Runbook)

End-to-end PowerShell runbook for onboarding **any** customer's SAP BTP
Integration Suite tenant onto this automation. Follow the phases in order; each
phase ends with a verification check — do not proceed past a failed check.

> **Document version:** 1.1 (2026-05-02)
> **Time required:** 60–90 minutes for the first customer; ~20 minutes for
> subsequent customers.
> **Risk profile:** Low — read-only OAuth client (`WorkspacePackagesRead`).
> Cannot modify or deploy iFlows in CPI even if the secret leaks.

---

## What this automation does

A scheduled GitHub Actions workflow:

1. Authenticates to the customer's BTP CPI tenant using OAuth client credentials.
2. Enumerates Integration Packages and (optionally) Value Mappings.
3. Downloads each iFlow as a ZIP via the
   `IntegrationDesigntimeArtifacts/$value` OData endpoint.
4. Unpacks each ZIP into `iflows/<PackageId>/<IFlowId>/` so file diffs are clean.
5. Commits any changes back to the repository.

The committed `iflows/` tree mirrors the project structure CPI exports manually
— it is the natural source feed for a downstream Git → CPI deploy automation.

---

## Onboarding overview

```
Phase 0  Workstation prereqs (one-time per operator)
Phase 1  BTP service instance + service key (per customer / tenant)
Phase 2  Populate config\customer.env on your workstation
Phase 3  Create the GitHub repository for the customer
Phase 4  Set workflow permissions on the repo
Phase 5  Push the four CPI_* secrets (and any optional variables)
Phase 6  Push the source code to GitHub
Phase 7  Trigger the first workflow run
Phase 8  Verify the iFlow packages landed in GitHub
Phase 9  Operations & credential rotation
```

---

## Phase 0 — Workstation prerequisites

Run these once per operator workstation. If your tools are already installed and
authenticated, skip to Phase 1.

### 0.1  PowerShell (built into Windows 10 / 11)

Open **PowerShell** (not the legacy `cmd`). Confirm version:

```powershell
$PSVersionTable.PSVersion
```

Anything 5.1 or higher works.

### 0.2  Git for Windows

```powershell
git --version
```

If not present:

```powershell
winget install --id Git.Git -e
# Close and reopen PowerShell so PATH refreshes
```

### 0.3  GitHub CLI (`gh`)

```powershell
gh --version
```

If not present:

```powershell
winget install --id GitHub.cli -e
# Close and reopen PowerShell so PATH refreshes
```

### 0.4  Authenticate `gh` to GitHub.com

```powershell
gh auth status
```

If not logged in:

```powershell
gh auth login
# Pick: GitHub.com  →  HTTPS  →  Login with a web browser
# Copy the one-time code shown, paste in the browser, complete OAuth.
```

Configure git to use the gh credential helper so `git push` works without
prompting:

```powershell
gh auth setup-git
```

### 0.5  PowerShell execution policy

Allow unsigned scripts (the bundled bootstrap helper) **for this session only**:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

`-Scope Process` reverts as soon as you close this PowerShell window — your
machine's overall security posture is unchanged.

**Verification — Phase 0:**

```powershell
git --version          # any version
gh --version           # any version
gh auth status         # "Logged in to github.com as <you>"
```

---

## Phase 1 — Create the BTP service instance and service key

Skip this if the customer's tenant already has a `Process Integration Runtime`
instance with plan `api` and role `WorkspacePackagesRead`.

### 1.1  Create the service instance

In the customer's BTP cockpit:

1. Navigate to **Subaccount › Services › Service Marketplace**.
2. Search for **Process Integration Runtime** and open the tile.
3. Click **Create** and use:

   | Field             | Value                                            |
   |-------------------|--------------------------------------------------|
   | Service           | Process Integration Runtime                      |
   | Plan              | `api`                                            |
   | Runtime           | Cloud Foundry                                    |
   | Space             | The space hosting the Cloud Integration tenant   |
   | Instance Name     | `cpi-github-sync`                                |
   | Parameters JSON   | `{ "roles": ["WorkspacePackagesRead"] }`         |

> **Principle of least privilege.** Do not grant `WorkspacePackagesEdit` or
> `WorkspaceArtifactsDeploy` — sync is read-only by design.

### 1.2  Generate the service key

1. Open the new `cpi-github-sync` instance → **Service Keys → Create**.
2. Name the key `github-actions-sync-key`.
3. Open the key. You will use four fields:

   | Field in key payload                     | Maps to GitHub secret |
   |------------------------------------------|-----------------------|
   | `url`                                    | `CPI_TENANT_URL`      |
   | `tokenurl` (or `oauth.tokenurl`)         | `CPI_TOKEN_URL`       |
   | `clientid` (or `oauth.clientid`)         | `CPI_CLIENT_ID`       |
   | `clientsecret` (or `oauth.clientsecret`) | `CPI_CLIENT_SECRET`   |

> **Both URL fields must include the `https://` scheme.** A common first-run
> failure is copying just the host (`tenant.authentication.eu10.hana.ondemand.com/oauth/token`)
> — the script then fails with `Invalid URL: No scheme supplied`. Always paste
> the full `https://...` URL.

> **No trailing slash on `CPI_TENANT_URL`.** The script appends
> `/api/v1/...` itself.

**Verification — Phase 1:** the four field values are copied to a temporary,
secure scratch location (e.g. an open Notepad — do not save).

---

## Phase 2 — Populate `config\customer.env` on your workstation

This file is for **local smoke-testing only** and for feeding the bootstrap
helper that pushes secrets to GitHub. It is gitignored and must never be
committed.

### 2.1  Copy the template

```powershell
cd "C:\Satheesh Gopalsamy\Claude CoWork\cpi-iflow-gitops\BTP Integration Suite Automate Artifacts To GITHUB"
Copy-Item config\customer.env.template config\customer.env
notepad config\customer.env
```

### 2.2  Fill in the four `CPI_*` values

Paste the values from the BTP service key (Phase 1.2). Save the file.

```ini
CPI_TENANT_URL="https://<tenant>.it-cpi.cfapps.<region>.hana.ondemand.com"
CPI_TOKEN_URL="https://<tenant>.authentication.<region>.hana.ondemand.com/oauth/token"
CPI_CLIENT_ID="sb-..."
CPI_CLIENT_SECRET="..."
CUSTOMER_LABEL="ACME — DEV tenant"
```

### 2.3  (Recommended) Local smoke test

Before involving GitHub, prove the credentials work from your workstation:

```powershell
# Load env vars from customer.env into the current PowerShell session
Get-Content config\customer.env | ForEach-Object {
    if ($_ -match '^\s*([^#][^=]+?)\s*=\s*"?([^"]*)"?\s*$') {
        [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], "Process")
    }
}

pip install -r requirements.txt
python sync_iflows.py
```

Expected output ends with `✅ Sync complete.` and you'll see one
`📦 <PackageId>` line per package.

**If the smoke test fails — fix it now.** Nothing in later phases will work
until the credentials are good. See **Troubleshooting** at the end of this
guide.

**Verification — Phase 2:** local `python sync_iflows.py` returns exit code 0
and creates an `iflows\` directory on your workstation. (Delete it before
pushing to GitHub: `Remove-Item -Recurse -Force iflows`.)

---

## Phase 3 — Create the GitHub repository for the customer

One private repo per customer / tenant — clean blast radius and audit trail.

```powershell
gh repo create SatheeshGopalsamy/IFACPIPackages `
   --private `
   --description "CPI iFlow GitOps sync — IFA"
```

If the repo already exists, skip this step. The `--private` flag is critical:
even unzipped, an iFlow can reveal endpoint URLs, queue names, and credential
aliases.

**Verification — Phase 3:**

```powershell
gh repo view SatheeshGopalsamy/IFACPIPackages
```

…opens the repo summary. The repo is empty — this is correct.

---

## Phase 4 — Set workflow permissions on the repo

The workflow needs permission to commit synced changes back to the repo.

```powershell
gh api -X PUT "repos/SatheeshGopalsamy/IFACPIPackages/actions/permissions/workflow" `
       -f default_workflow_permissions=write `
       -F can_approve_pull_request_reviews=false
```

Or via UI: **Settings → Actions → General → Workflow permissions →
Read and write permissions → Save**.

**Verification — Phase 4:**

```powershell
gh api "repos/SatheeshGopalsamy/IFACPIPackages/actions/permissions/workflow"
```

Returns JSON containing `"default_workflow_permissions": "write"`.

---

## Phase 5 — Push the four CPI_* secrets to the repo

Two equivalent options.

### Option A — Bootstrap script (recommended)

The script reads `config\customer.env` and pushes all four secrets in one go.
It also pushes any optional non-secret variables you set in the env file
(`PACKAGES_FILTER`, `PACKAGES_EXCLUDE`, `INCLUDE_VALUE_MAPS`,
`INCLUDE_SAP_PACKAGES`, `CUSTOMER_LABEL`).

```powershell
cd "C:\Satheesh Gopalsamy\Claude CoWork\cpi-iflow-gitops\BTP Integration Suite Automate Artifacts To GITHUB\scripts"
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\bootstrap-customer.ps1 -Repo "SatheeshGopalsamy/IFACPIPackages" -EnvFile "..\config\customer.env"
cd ..
```

The script asks for a `[y/N]` confirmation before pushing. Expect four `ok`
lines for the secrets.

### Option B — Direct `gh` commands

```powershell
gh secret set CPI_TENANT_URL    --repo SatheeshGopalsamy/IFACPIPackages --body "https://<tenant>.it-cpi.cfapps.<region>.hana.ondemand.com"
gh secret set CPI_TOKEN_URL     --repo SatheeshGopalsamy/IFACPIPackages --body "https://<tenant>.authentication.<region>.hana.ondemand.com/oauth/token"
gh secret set CPI_CLIENT_ID     --repo SatheeshGopalsamy/IFACPIPackages --body "sb-..."
gh secret set CPI_CLIENT_SECRET --repo SatheeshGopalsamy/IFACPIPackages --body "..."

# Optional non-secret variables
gh variable set CUSTOMER_LABEL --repo SatheeshGopalsamy/IFACPIPackages --body "IFA"
```

### Option C — GitHub UI

`Settings → Secrets and variables → Actions → New repository secret`. Add the
four secrets manually.

**Verification — Phase 5:**

```powershell
gh secret   list --repo SatheeshGopalsamy/IFACPIPackages
gh variable list --repo SatheeshGopalsamy/IFACPIPackages
```

You should see all four `CPI_*` secrets listed (values are not displayed —
this is correct).

---

## Phase 6 — Push the source code to GitHub

Initialize git in the project folder and push to the new empty repo.

```powershell
cd "C:\Satheesh Gopalsamy\Claude CoWork\cpi-iflow-gitops\BTP Integration Suite Automate Artifacts To GITHUB"

git init -b main
git config user.name  "Satheesh Gopalsamy"
git config user.email "satheesh.gopalsamy@gmail.com"

# Sanity check: confirm customer.env is NOT in the staged tree
git status

git add .
git commit -m "feat: customer-agnostic CPI iFlow sync automation"

git remote add origin https://github.com/SatheeshGopalsamy/IFACPIPackages.git
git push -u origin main
```

> If `git status` lists `config/customer.env`, stop. Verify `.gitignore` is
> committed and rerun `git add .`. The customer's secret values must never
> reach GitHub as a file.

**Verification — Phase 6:**

```powershell
gh repo view SatheeshGopalsamy/IFACPIPackages --web
```

Browser opens the repo. You see `sync_iflows.py`, `.github/workflows/sync-iflows.yml`,
`README.md`, `CUSTOMIZATION_GUIDE.md`, `requirements.txt`, etc. The **Actions**
tab shows the workflow `Sync CPI iFlows to Repo`.

---

## Phase 7 — Trigger the first workflow run

```powershell
gh workflow run sync-iflows.yml --repo SatheeshGopalsamy/IFACPIPackages `
   -f packages_filter="" `
   -f include_value_maps=false `
   -f customer_label="IFA — first run"
```

Watch it execute. The first run takes 1–5 minutes depending on tenant size.

```powershell
# List recent runs
gh run list --repo SatheeshGopalsamy/IFACPIPackages --workflow sync-iflows.yml --limit 3

# Stream the latest run's logs
gh run watch --repo SatheeshGopalsamy/IFACPIPackages
```

**Expected log highlights:**

In *Pull iFlows from CPI tenant*:

```
INFO Requesting OAuth access token …
INFO Token acquired (length=4184)
INFO Found 18 package(s) total, 5 in scope.
📦 ECommIntegration  (E-Comm Integration)
  ↳ iflow: IF_001_GETCustomer v1.0.7
...
✅ Sync complete.
```

In *Show working-tree status*:

```
Working tree status (porcelain):
?? iflows/ECommIntegration/IF_001_GETCustomer/
?? iflows/ECommIntegration/IF_002_CreateCustomer/
... (one ?? line per new path)
```

In *Commit & push changes*:

```
Staged changes:
 iflows/ECommIntegration/IF_001_GETCustomer/META-INF/MANIFEST.MF | ...
 ...
[main abc1234] chore(cpi-sync): update iFlows from IFA (manual run)
```

**Verification — Phase 7:** run completes with green check. The repo's commit
list now shows a `chore(cpi-sync): update iFlows from <label>` commit.

---

## Phase 8 — Verify the iFlow packages landed in GitHub

```powershell
git pull
ls iflows
ls iflows\ECommIntegration
ls iflows\ECommIntegration\IF_001_GETCustomer
```

You should see one folder per package, each containing per-iFlow folders with
the standard CPI project layout:

```
iflows\ECommIntegration\IF_001_GETCustomer\
├── META-INF\MANIFEST.MF
└── src\main\resources\
    ├── scenarioflows\integrationflow\IF_001_GETCustomer.iflw
    ├── script\
    ├── mapping\
    └── parameters.prop
```

Open one `.iflw` file in a text editor — it contains BPMN XML representing the
integration flow. Discrepancies between the file count and the source tenant
indicate API permission gaps or filter misconfiguration.

**Verification — Phase 8:**

```powershell
# Quick package + iFlow count
(Get-ChildItem iflows -Directory).Count                   # = number of packages
(Get-ChildItem iflows -Recurse -Directory -Depth 1).Count # ≈ packages + iFlows
```

The numbers should match the `iFlows synced : N` line from the workflow log.

---

## Phase 9 — Operations & credential rotation

### 9.1  Routine tasks

| Task                                                             | Frequency        | Owner                  |
|------------------------------------------------------------------|------------------|------------------------|
| Review failed workflow runs                                      | Weekly           | DevOps / Integration   |
| Rotate `CPI_CLIENT_SECRET`                                       | Every 12 months  | BTP Tenant Admin       |
| Confirm role on service instance has not drifted                 | Quarterly        | BTP Tenant Admin       |
| Review repository commit history for unexpected diffs            | Monthly          | Integration Lead       |
| Re-run a failed sync after fixing tenant-side issue              | Ad hoc           | DevOps                 |

### 9.2  Credential rotation procedure

```powershell
# 1. In BTP cockpit: cpi-github-sync → Service Keys → Create new key
#    (do NOT delete the existing one yet)

# 2. Update the local customer.env with new clientid/clientsecret, then push:
cd "C:\Satheesh Gopalsamy\Claude CoWork\cpi-iflow-gitops\BTP Integration Suite Automate Artifacts To GITHUB\scripts"
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\bootstrap-customer.ps1 -Repo "SatheeshGopalsamy/IFACPIPackages" -EnvFile "..\config\customer.env"

# 3. Trigger a manual run and confirm success
gh workflow run sync-iflows.yml --repo SatheeshGopalsamy/IFACPIPackages -f customer_label="rotation test"
gh run watch --repo SatheeshGopalsamy/IFACPIPackages

# 4. In BTP: delete the old service key
```

### 9.3  Decommissioning

```powershell
# 1. Disable the workflow (does not delete history)
gh workflow disable sync-iflows.yml --repo SatheeshGopalsamy/IFACPIPackages
# UI alternative: Actions tab → Sync CPI iFlows to Repo → ⋯ → Disable workflow

# 2. In BTP cockpit: delete the service key (revokes OAuth client immediately)

# 3. Optionally remove secrets and the service instance
gh secret delete CPI_TENANT_URL    --repo SatheeshGopalsamy/IFACPIPackages
gh secret delete CPI_TOKEN_URL     --repo SatheeshGopalsamy/IFACPIPackages
gh secret delete CPI_CLIENT_ID     --repo SatheeshGopalsamy/IFACPIPackages
gh secret delete CPI_CLIENT_SECRET --repo SatheeshGopalsamy/IFACPIPackages
```

The CPI tenant is unaffected at all stages — the automation is read-only.

---

## Multi-environment / multi-tenant landscapes

This automation handles **one tenant per repo**. For a customer with DEV/QA/PRD:

- **Recommended:** one repo per environment (e.g. `IFACPIPackages-dev`,
  `-qa`, `-prd`). Run Phases 1–8 once per repo. Clean blast radius, separate
  audit trails, separate access control.
- **Alternative:** one repo, three workflow files
  (`sync-iflows-dev.yml`, `-qa.yml`, `-prd.yml`) and three sets of secrets
  prefixed `CPI_DEV_*`, `CPI_QA_*`, `CPI_PRD_*`. Requires editing the workflow
  `env:` block — see comments at the top of `sync-iflows.yml`.

For full bidirectional GitOps (Git → CPI deploy with approval gates), follow
the v2.0 deployment guide separately — this repo is the inbound foundation.

---

## Troubleshooting (real failures captured during onboarding)

| Symptom                                                             | Cause                                                            | Fix                                                                                                                |
|---------------------------------------------------------------------|------------------------------------------------------------------|--------------------------------------------------------------------------------------------------------------------|
| `bootstrap-customer.ps1 : The term ... is not recognized`           | Running script from current directory without `.\` prefix        | Use `.\bootstrap-customer.ps1 -Repo ... -EnvFile ...`                                                              |
| `running scripts is disabled on this system`                        | PowerShell execution policy blocks unsigned scripts              | `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` before the script call                                |
| `GitHub CLI (gh) is required.`                                      | `gh` not installed on this workstation                           | `winget install --id GitHub.cli -e`, then close + reopen PowerShell, then `gh auth login`                          |
| `Invalid URL '***': No scheme supplied`                             | `CPI_TOKEN_URL` or `CPI_TENANT_URL` missing the `https://` prefix | Update the secret with the full URL including scheme: `gh secret set CPI_TOKEN_URL --body "https://..."`           |
| `401 Unauthorized` on token request                                 | Wrong `CPI_CLIENT_ID` / `CPI_CLIENT_SECRET`, or trailing whitespace | Re-copy from the BTP service key. Beware of trailing newlines pasted from the cockpit JSON                          |
| `403 Forbidden` on `/IntegrationPackages`                           | Service instance missing `WorkspacePackagesRead`                  | Recreate the service instance with the correct `roles` parameter, generate a new key, update the four secrets       |
| `404` on `/IntegrationDesigntimeArtifacts`                          | Package is SAP-shipped (read-only)                                | Expected. Script skips automatically. Use `PACKAGES_FILTER` to scope to custom packages                            |
| Workflow says `No changes detected — nothing to commit` on the **first** run, but iFlows clearly downloaded | Old workflow used `git diff` before staging; untracked files don't show in diff | Already fixed in workflow v1.1 (current). If you see this, you're on an older copy — pull the latest `sync-iflows.yml` |
| `git push` fails with `403`                                         | Workflow permissions are read-only                                | `gh api -X PUT ".../actions/permissions/workflow" -f default_workflow_permissions=write` (Phase 4)                  |
| Schedule never fires                                                | Repository inactive >60 days disables schedules                   | Push any commit to the default branch to re-enable                                                                 |
| Workflow times out on a large iFlow                                 | Script's `REQUEST_TIMEOUT` too low                                | Raise `REQUEST_TIMEOUT` env var (default 120s) via repo variable                                                   |

---

## Appendix A — Files in this repo

| Path                                       | Purpose                                       |
|--------------------------------------------|-----------------------------------------------|
| `sync_iflows.py`                           | Sync script — env-var-driven, tenant-agnostic |
| `.github/workflows/sync-iflows.yml`        | GitHub Actions workflow (v1.1)                |
| `requirements.txt`                         | Python dependencies                           |
| `config/customer.env.template`             | Local smoke-test template (copy per customer) |
| `scripts/bootstrap-customer.sh`            | Linux/macOS helper — push secrets via `gh`    |
| `scripts/bootstrap-customer.ps1`           | Windows helper — push secrets via `gh`        |
| `CODEOWNERS`                               | Replace placeholder team handles before use   |
| `CUSTOMIZATION_GUIDE.md`                   | This file                                     |
| `README.md`                                | Top-level entry point                         |
| `iflows/`                                  | **Auto-populated** by the workflow            |

---

## Appendix B — OData endpoints used (reference)

| Endpoint                                                                                  | Method | Purpose                          |
|-------------------------------------------------------------------------------------------|--------|----------------------------------|
| `<token_url>`                                                                             | POST   | Acquire access token             |
| `/api/v1/IntegrationPackages`                                                             | GET    | Enumerate all packages           |
| `/api/v1/IntegrationPackages('{id}')/IntegrationDesigntimeArtifacts`                      | GET    | List iFlows in a package         |
| `/api/v1/IntegrationPackages('{id}')/ValueMappingDesigntimeArtifacts`                     | GET    | List value maps (optional)       |
| `/api/v1/IntegrationDesigntimeArtifacts(Id='{id}',Version='{ver}')/$value`                | GET    | Download iFlow as ZIP            |
| `/api/v1/ValueMappingDesigntimeArtifacts(Id='{id}',Version='{ver}')/$value`               | GET    | Download value map as ZIP        |

---

## Appendix C — Configuration matrix

All configuration is environment-driven; nothing is hard-coded.

| Knob                                | Where it lives                               | Required |
|-------------------------------------|----------------------------------------------|----------|
| `CPI_TENANT_URL`                    | GitHub repo secret                           | Yes      |
| `CPI_TOKEN_URL`                     | GitHub repo secret                           | Yes      |
| `CPI_CLIENT_ID`                     | GitHub repo secret                           | Yes      |
| `CPI_CLIENT_SECRET`                 | GitHub repo secret                           | Yes      |
| `PACKAGES_FILTER`                   | repo variable **or** workflow_dispatch input | No       |
| `PACKAGES_EXCLUDE`                  | repo variable **or** workflow_dispatch input | No       |
| `INCLUDE_VALUE_MAPS`                | repo variable **or** workflow_dispatch input | No       |
| `INCLUDE_SAP_PACKAGES`              | repo variable **or** workflow_dispatch input | No       |
| `CUSTOMER_LABEL`                    | repo variable **or** workflow_dispatch input | No       |
| Cron schedule                       | `.github/workflows/sync-iflows.yml`          | No       |
