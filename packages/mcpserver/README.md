# CodeMender MCP Server (`packages/mcpserver`)

Run CodeMender as an [MCP](https://modelcontextprotocol.io) server on Cloud Run.
An MCP client (Claude Code, Claude Desktop, Gemini CLI, …) calls tools with a
repository name; each call launches a **new, isolated Cloud Run Job execution**
of the sandboxed `cm-runner` image, and the results land in **BigQuery**.

```
MCP client ──HTTP──▶ Cloud Run service: codemender-mcp ──run_job(overrides)──▶ Cloud Run Job: codemender-runner
                       │  clone repo (GitHub token)               (new execution per call, no repo creds)
                       │  stage snapshot + manifest ──▶ GCS ◀── reads inputs / writes receipt + outputs
                       │  watch receipt.json ◀──────────────────────────────┘
                       ├──▶ BigQuery: codemender.jobs / codemender.findings
                       └──▶ GitHub: push codemender/fix-<id> branch + open PR (cm_fix)
```

The GitHub token stays on the MCP server. It is passed to git per command
(`http.extraHeader`), never written into `.git/config`, so the snapshot sent to
the sandbox job contains no credentials (same model as ADR-0002).

## Tools

| Tool | What it does |
|---|---|
| `cm_find(repo, ref="", wait_seconds=0)` | Full-repository scan (`cm-runner find-full`). Every finding becomes a row in `findings` with status `OPEN`. |
| `cm_validate(finding_id, wait_seconds=0)` | Runs `cm verify` on one finding. Sets `status` to `VERIFIED`, `FALSE_POSITIVE` or `INCONCLUSIVE` (and `verify_status`, `dismiss_reason`, `verified_sarif`). |
| `cm_fix(finding_id, patch_mode="", wait_seconds=0)` | Runs `cm fix`. Stores the unified diff in `findings.patch` (`status=FIXED`), then patches the repo: `pr` (default) pushes `codemender/fix-<id>` and opens a PR, `branch` pushes only, `none` stores only. Sets `status=PATCHED`, `patch_branch`, `pr_url`. |
| `cm_apply_patch(finding_id, patch_mode="pr")` | Re-push a stored patch (e.g. if the push/PR step failed). No new job. |
| `cm_job_status(job_id, wait_seconds=0)` | `STAGING` → `RUNNING` → `SUCCEEDED`/`FAILED`, with result summary or error (incl. the tail of `runner.log`). |
| `cm_list_findings(repo="", status="", scan_job_id="", limit=100)` | List findings. |
| `cm_get_finding(finding_id, include_sarif=False)` | Full finding incl. patch and verdict. |

`find`/`validate`/`fix` return a `job_id` immediately (scans take minutes to
hours). Pass `wait_seconds` to block, or poll `cm_job_status`. Validate and fix
reuse the scan's repository snapshot (same commit), re-cloning that commit only
if the snapshot has expired from the bucket.

## BigQuery tables

Created automatically on startup in `${BQ_DATASET}` (default `codemender`); the
schema lives in [`store.py`](src/cm_mcp/store.py).

- **`jobs`** — one row per tool-triggered job: `job_id, kind, status, repo, ref,
  commit_sha, finding_id, patch_mode, execution, gcs_prefix, payload_uri,
  finding_count, error, result (JSON), created_at, updated_at, completed_at`.
- **`findings`** — one row per finding per scan: `finding_id, scan_job_id, repo,
  ref, commit_sha, fingerprint, rule_id, severity, message, file_path,
  start_line, end_line, status, verify_status, dismiss_reason, fix_status,
  sarif, verified_sarif, patch, patch_branch, patch_commit, pr_url, ...`.

Rows are written with DML / load jobs (not streaming inserts) so validate/fix can
`UPDATE` them immediately.

```sql
-- Open, exploitable findings for a repo's latest scan
SELECT finding_id, severity, rule_id, file_path, start_line, message
FROM `PROJECT.codemender.findings`
WHERE repo = 'acme/widgets' AND status = 'VERIFIED'
  AND scan_job_id = (SELECT job_id FROM `PROJECT.codemender.jobs`
                     WHERE kind = 'find' AND repo = 'acme/widgets' AND status = 'SUCCEEDED'
                     ORDER BY created_at DESC LIMIT 1);
```

## Deploy

**Recommended:** [`deploy.sh`](../../deploy.sh) at the repo root does all of the
steps below (APIs, secrets, images, Terraform) in one command:

```bash
./deploy.sh --plan-only --project my-project --github-owner my-org   # preview
./deploy.sh --project my-project --github-owner my-org               # deploy
./deploy.sh --code-only                                              # roll out code changes
```

See the [root README](../../README.md#quickstart-codemender-mcp-server-on-cloud-run-deploysh)
for its options.

### Manual (Terraform)

The MCP server is an opt-in addition to the existing Cloud Run setup
([`terraform/README.md`](../../terraform/README.md)).

1. Store secrets in Secret Manager (GitHub token needs `repo` scope; use a
   fine-grained token limited to the repos you scan):

   ```bash
   printf '%s' "$GITHUB_TOKEN" | gcloud secrets create codemender-github-token --data-file=-
   openssl rand -hex 32 | tr -d '\n' | gcloud secrets create codemender-mcp-auth-token --data-file=-
   ```

2. In `terraform/terraform.tfvars`:

   ```hcl
   execution_mode             = "cloud_run"
   enable_mcp_server          = true
   runner_timeout             = "10800s"   # full scans outlast the 1h default
   mcp_github_token_secret_id = "codemender-github-token"
   mcp_auth_token_secret_id   = "codemender-mcp-auth-token"
   mcp_invoker_members        = ["user:you@example.com"]
   ```

3. `terraform plan` then `terraform apply`. This (re)builds the runner image
   (which now includes `find-full`) and the `codemender-mcp` image, and outputs
   `mcp_endpoint`.

Updates later: `make deploy-cloudrun` (runner job image) and `make deploy-mcp`
(MCP service image).

The service runs with `min_instances = 1` and CPU always allocated because job
watchers run in background threads after a tool call returns. If an instance
restarts, `cm_job_status` (and startup recovery) reconcile running jobs from
GCS, so no results are lost.

## Connect a client

**IAM-authenticated (default, service is private).** Proxy locally with your
gcloud identity (needs `roles/run.invoker`, see `mcp_invoker_members`):

```bash
gcloud run services proxy codemender-mcp --region us-central1 --port 8080
claude mcp add --transport http codemender http://localhost:8080/mcp
```

**Bearer token** (set `mcp_allow_unauthenticated = true` together with
`mcp_auth_token_secret_id`):

```bash
claude mcp add --transport http codemender https://codemender-mcp-XXXX.run.app/mcp \
  --header "Authorization: Bearer $(gcloud secrets versions access latest --secret codemender-mcp-auth-token)"
```

Then, e.g.: *"Scan acme/widgets with codemender, validate the high-severity
findings and fix the verified ones."*

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `GOOGLE_CLOUD_PROJECT` | — (required) | Project of job, bucket and BigQuery |
| `CLOUD_RUN_REGION` | `us-central1` | Region of the runner job |
| `CLOUD_RUN_JOB_NAME` | `codemender-runner` | Runner job to execute |
| `GCS_TRANSIT_BUCKET` | `codemender-reports-<project>` | Transit bucket |
| `BQ_DATASET` / `BQ_LOCATION` | `codemender` / `US` | BigQuery dataset |
| `GITHUB_TOKEN` | — | Clone private repos, push branches, open PRs |
| `GITHUB_API_URL` | `https://api.github.com` | GitHub Enterprise Server API |
| `CM_MCP_AUTH_TOKEN` | — | Require `Authorization: Bearer <token>` |
| `CM_MCP_DEFAULT_PATCH_MODE` | `pr` | `pr`, `branch` or `none` |
| `CM_MCP_POLL_INTERVAL_SECONDS` | `20` | Receipt polling interval |
| `CM_MCP_JOB_WATCH_TIMEOUT_SECONDS` | `14400` | Mark jobs failed after this |

## Local development

```bash
pip install -e ./packages/codemender -e "./packages/mcpserver[gcp]" httpx pytest
PYTHONPATH=packages/codemender/src:packages/mcpserver/src pytest packages/mcpserver
make run-mcp   # runs the container against real GCP using your gcloud ADC
```
