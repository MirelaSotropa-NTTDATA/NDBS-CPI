# CPI iFlow → GitHub Sync (Customer-Agnostic Template)

Pulls every iFlow (and optionally Value Mappings) out of an SAP BTP
Integration Suite tenant on a schedule, and commits the unzipped sources to
GitHub. The committed tree is the same project structure CPI exports manually,
which makes it the natural source-of-truth for downstream **package import
automation** (Git → CPI deploy).

This repository is **tenant-agnostic** — onboard any customer by configuring
GitHub secrets and (optionally) a few repo variables. No code changes required.

---

## Quick start (PowerShell)

The full step-by-step is in **[CUSTOMIZATION_GUIDE.md](CUSTOMIZATION_GUIDE.md)**.
The condensed flow:

```powershell
# Phase 0 — workstation prereqs (one-time)
winget install --id Git.Git -e
winget install --id GitHub.cli -e
gh auth login
gh auth setup-git

# Phase 1 — BTP cockpit: create Process Integration Runtime instance (plan=api,
# role=WorkspacePackagesRead) and a service key. Capture url, tokenurl,
# clientid, clientsecret.

# Phase 2 — populate config\customer.env from the template
cd "C:\path\to\BTP Integration Suite Automate Artifacts To GITHUB"
Copy-Item config\customer.env.template config\customer.env
notepad config\customer.env

# Phase 3 — create the customer's GitHub repo
gh repo create <owner>/<repo> --private --description "CPI iFlow GitOps sync"

# Phase 4 — workflow permissions: Read+Write
gh api -X PUT "repos/<owner>/<repo>/actions/permissions/workflow" `
       -f default_workflow_permissions=write `
       -F can_approve_pull_request_reviews=false

# Phase 5 — push the four CPI_* secrets
cd scripts
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\bootstrap-customer.ps1 -Repo "<owner>/<repo>" -EnvFile "..\config\customer.env"
cd ..

# Phase 6 — push the source code
git init -b main
git add .
git commit -m "feat: customer-agnostic CPI iFlow sync automation"
git remote add origin https://github.com/<owner>/<repo>.git
git push -u origin main

# Phase 7 — trigger the first run
gh workflow run sync-iflows.yml --repo <owner>/<repo> -f customer_label="<label>"
gh run watch --repo <owner>/<repo>

# Phase 8 — verify
git pull
ls iflows
```

---

## Repository layout

```
.
├── .github/workflows/sync-iflows.yml   # GitHub Actions — schedule + dispatch
├── sync_iflows.py                      # Python sync script (env-var driven)
├── requirements.txt                    # Python dependencies
├── config/customer.env.template        # Local smoke-test template
├── scripts/
│   ├── bootstrap-customer.ps1          # Push secrets via gh CLI (Windows)
│   └── bootstrap-customer.sh           # Push secrets via gh CLI (Linux/macOS)
├── CODEOWNERS                          # Edit team handles before use
├── CUSTOMIZATION_GUIDE.md              # Full PowerShell runbook (Phases 0–9)
├── README.md                           # This file
└── iflows/                             # Auto-populated by the workflow
    └── <PackageId>/
        └── <IFlowId>/
            ├── META-INF/MANIFEST.MF
            └── src/main/resources/
                ├── scenarioflows/integrationflow/*.iflw
                ├── script/
                ├── mapping/
                └── parameters.prop
```

## Configuration matrix

All configuration is environment-driven; nothing customer-specific is
hard-coded.

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

## Security model

The OAuth client used here is granted only `WorkspacePackagesRead`. It cannot
modify or deploy iFlows in CPI even if the secret leaks. Secrets live in
GitHub repository secrets and are auto-masked in run logs. The committed
repository is the source of truth for any downstream import automation; direct
edits to the CPI tenant should be detected by the next scheduled sync as
unexpected commits and reconciled.

## When to use this vs. the bidirectional v2 GitOps pipeline

- **Use this repo** when the customer wants version control, audit traceability,
  and a clean source feed for an import / deploy pipeline that is owned
  separately (or doesn't yet exist).
- **Use the v2 GitOps deployment guide** when the customer wants the same
  inbound sync **plus** Git → CPI deploy with multi-environment promotion
  (DEV → QA → PRD) and approval gates. The v2 pipeline embeds this repo's
  inbound flow as Part A.

## Where to go next

- **Onboarding a new customer:** **[CUSTOMIZATION_GUIDE.md](CUSTOMIZATION_GUIDE.md)** — full PowerShell runbook (Phases 0–9).
- **Troubleshooting:** see the *Troubleshooting* table at the end of the guide.
- **Rotating credentials:** see *Phase 9 — Operations & credential rotation*.
