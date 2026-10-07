# CodeMender GitHub Actions Integration (Workstream 2)

A GitHub Action that runs CodeMender (`cm-runner`) on pull requests and reports
the results back on the PR.

> **Status: M1 (`init` + `find-diff`).** On every pull request the action
> authenticates to Google Cloud via Workload Identity Federation (WIF), runs
> `cm-runner init` and `cm-runner find-diff` in a `docker run` container under
> gVisor, uploads `report.sarif` and `report.md` as workflow artifacts, and
> posts a top-level Markdown PR comment with any vulnerabilities found. `fix`
> and `verify` will be wired in next (see [Roadmap](#roadmap)).

## How it fits together

```mermaid
flowchart LR
    gh["GitHub PR event"] --> wf["Repo's workflow file"]
    wf --> action["actions/codemender/action.yml"]
    action --> runner["src/gha_runner.py"]
    runner -->|"launcher: docker run under gVisor (init + find-diff), no GitHub token"| cm["cm-runner container"]
    cm -->|"SARIF JSON on stdout + /out/report.{sarif,md}"| runner
    runner -->|"vcs/github.py"| pr["PR comment"]
```

- **cm-runner** (`codemender-runner/`) is environment-agnostic. It runs
  CodeMender and never talks to GitHub (ADR-0002).
- **This action** decides *where* cm-runner runs (the launcher), and it's the
  only part that holds the GitHub token and posts to the PR.

## Directory structure

Follows the `.github/` convention: workflows in `workflows/`, actions in
`actions/<name>/` (GitHub requires the file to be named `action.yml`). The
action's source lives with the action, so the directory is self-contained.

```text
github-actions/
├── actions/
│   └── codemender/
│       ├── action.yml             # Composite action: installs gVisor, pulls the image, runs src/gha_runner.py
│       ├── requirements.txt       # Dependencies of this action (not of cm-runner)
│       ├── src/
│       │   ├── gha_runner.py      # Entry point: reads the GitHub event, runs cm-runner, posts the result
│       │   ├── launcher/
│       │   │   ├── base.py        # Launcher interface + ADR-0001 output contract (NDJSON, exit codes)
│       │   │   └── local_docker.py  # `docker run` on the GitHub Actions VM, under gVisor by default
│       │   └── vcs/
│       │       └── github.py      # GitHub REST API: create/update the CodeMender PR comment
│       └── tests/                 # Unit tests (no network, no Docker needed)
└── workflows/
    └── cm-merge-scan.yml          # Workflow template to copy into a repo's .github/workflows/
```

## Distribution model

`codemender-runner` is a private repository, so customer repositories can't
reference this action (`uses: cloud-gtm/codemender-runner/...`), and we can't
change settings on their behalf. Instead, we **send customers a tarball of this
directory**; they copy it into their repository and run it from there. Nothing
in the action depends on access to this repository.

A published, versioned action may follow later (see [Roadmap](#roadmap)).

## Packaging for a customer (maintainers)

Build the tarball from a committed ref with `git archive`, so only tracked files
are included (no local notes, caches, or uncommitted changes):

```bash
# From the root of codemender-runner
git archive --format=tar.gz --prefix=codemender-action/ \
  -o codemender-action.tar.gz HEAD:github-actions
```

The package mirrors a repository's `.github/` directory:

| Path | Copy to | Needed at runtime? |
| :--- | :--- | :--- |
| `actions/codemender/` (`action.yml`, `requirements.txt`, `src/`) | `.github/actions/codemender/` | Yes: this is the action |
| `actions/codemender/tests/` | — | No: included for transparency |
| `workflows/cm-merge-scan.yml` | `.github/workflows/cm-merge-scan.yml` | Yes: the workflow that calls the action |
| `README.md` | — | Setup only: these instructions |

The customer also needs a **cm-runner image they can pull** (see step 3
below). The image is not part of the tarball.

## Using it in a repository (customers)

### 1. Copy the action into your repository

Extract the package and copy the action to `.github/actions/codemender/`:

```bash
tar -xzf codemender-action.tar.gz          # creates ./codemender-action/

# From the root of your repository
mkdir -p .github/actions/codemender
rsync -a --delete --exclude tests --exclude __pycache__ \
  /path/to/codemender-action/actions/codemender/ .github/actions/codemender/
```

Result:

```text
.github/actions/codemender/
├── action.yml
├── requirements.txt
└── src/
    ├── gha_runner.py
    ├── launcher/
    │   ├── __init__.py
    │   ├── base.py
    │   └── local_docker.py
    └── vcs/
        ├── __init__.py
        └── github.py
```

To upgrade, extract the new package, re-run the same command, and commit.

### 2. Add the workflow

```bash
mkdir -p .github/workflows
cp /path/to/codemender-action/workflows/cm-merge-scan.yml .github/workflows/
```

It checks out the repository (required, so the local action exists on the
runner) and then runs `uses: ./.github/actions/codemender`. It runs only on
PRs targeting `main` or `master`; edit `branches:` if your default branch is
named differently.

### 3. Configure the cm-runner image and Google Cloud (WIF)

Set the required inputs in `.github/workflows/cm-merge-scan.yml`:

- **`runner_image`**: Set to a cm-runner image your repository can pull, pinned
  to a commit SHA tag (not `:latest`). The action uses the workflow's automatic
  token (scoped by `permissions:`) to pull the image and to post the comment as
  `github-actions[bot]`. Only if the image is in a registry the repository's
  token can't read (e.g. another org), pass a token with
  `registry_token: ${{ secrets.<NAME> }}`. It is used only for `docker pull`.
- **`gcp_project_id`**, **`gcp_workload_identity_provider`**, and
  **`gcp_service_account`**: `cm` calls Vertex AI during `init` and `find-diff`.
  The action authenticates via `google-github-actions/auth@v2` using keyless
  Workload Identity Federation (`id-token: write`) and mounts the short-lived
  ADC file read-only into the container.

> [!NOTE]
> How customers build and host their own cm-runner image (e.g. in their own
> registry) is still to be documented (see `docs/guides/calling_cm_runner.md`).

### 4. Open a pull request

A **CodeMender** comment appears on the PR with either a clean scan message or a
summary table of vulnerabilities found in the diff (with links to the affected
lines and collapsible analysis details). `report.sarif` and `report.md` are also
uploaded as a workflow artifact (`codemender-report-pr-<N>`). Later pushes
update the same comment.

### Inputs

| Input | Default | Description |
| :--- | :--- | :--- |
| `runner_image` | — (**required**) | cm-runner image. Pin a commit SHA tag, not `:latest`. |
| `gcp_project_id` | — (**required**) | Google Cloud project ID used by `cm` for Vertex AI API calls. |
| `gcp_workload_identity_provider` | — (**required**) | Full Workload Identity Provider resource name (`projects/<num>/locations/global/workloadIdentityPools/<pool>/providers/<provider>`). |
| `gcp_service_account` | — (**required**) | Service account email that Workload Identity Federation impersonates (needs `roles/aiplatform.user`). |
| `registry_token` | *(workflow token)* | Optional. Used **only** to pull the image, when the repository's token can't read it. |
| `log_level` | `info` | cm-runner log level. Logs appear in the job log. |
| `container_runtime` | `runsc` | `runsc` runs cm-runner under gVisor, which `cm`'s sandbox needs (see [Security model](#security-model)). `runc` is an explicit opt-out: plain Docker, where `cm`'s sandbox can't start. |
| `gvisor_release` | `20260921.0` | gVisor release installed when `container_runtime` is `runsc`: `yyyymmdd` or `yyyymmdd.N` ([versions](https://gvisor.dev/docs/user_guide/install/#versions)). Pinned so every run uses the same build. |
| `diff_workers` | `'8'` | Number of concurrent workers for `cm-runner find-diff` (1..16). |
| `artifact_retention_days` | `'3'` | Days to retain the uploaded `codemender-report-pr-<N>` artifact (`report.sarif` and `report.md`). |

### Required workflow permissions

| Permission | Why |
| :--- | :--- |
| `contents: read` | Check out the repository (`fetch-depth: 0` so the PR base commit is present for `find-diff`) |
| `id-token: write` | Request a GitHub OIDC token for Google Cloud Workload Identity Federation |
| `packages: read` | Pull the cm-runner image from GHCR with the workflow token |
| `pull-requests: write` | Post / update the CodeMender comment |

## Internal testing (cloud-gtm)

To test with the upstream development image from a repository that can't read
`cloud-gtm` packages (e.g. a personal sandbox):

1. Set `runner_image` to a commit tag of
   `ghcr.io/cloud-gtm/codemender-runner` that has been published, e.g.
   `ghcr.io/cloud-gtm/codemender-runner:d1bc34c...`.
2. Create a classic PAT with only the `read:packages` scope and authorize it
   for `cloud-gtm` SSO (Settings → Developer settings → Tokens (classic) →
   Configure SSO).
3. Save it as a repository secret (e.g. `CM_REGISTRY_TOKEN`) and add
   `registry_token: ${{ secrets.CM_REGISTRY_TOKEN }}` to the workflow.

## Security model

There are two sandbox layers:

| Layer | Owner | Protects against |
| :--- | :--- | :--- |
| **Outer**: the container (`docker run`, under gVisor) | This action (the launcher) | cm-runner reaching the GitHub token, the host, or other jobs |
| **Inner**: `cm`'s own process sandbox | cm-runner (configures `cm`) | The agent's tool calls (builds, PoCs) escaping the workspace or network |

- `docker run` never receives `GITHUB_TOKEN`, `--env-file`, or bare `-e KEY`
  passthroughs. Only explicit, non-GitHub environment variables
  (`GOOGLE_APPLICATION_CREDENTIALS`, `GOOGLE_CLOUD_PROJECT`, and Git
  `safe.directory` config for `/workspace`) are passed into the container, along
  with isolated staging bind mounts (`/workspace`, `/root/.codemender`, `/out`,
  and read-only `/creds/adc.json`).
- The GitHub token is used only on the host: to pull the image (followed by
  `docker logout`) and to post the comment.
- cm-runner output is treated as untrusted. Values are escaped before being
  rendered, and comments are truncated to GitHub's limit.
- Fork PRs run only if the repository allows it (see below).

### Why gVisor

`cm`'s inner sandbox creates Linux namespaces. Docker's default profile blocks
that, so under plain `docker run` the sandbox fails with
`failed to create sandbox box: ... operation not permitted`.

The action therefore installs [gVisor](https://gvisor.dev) (a pinned release,
checked against its sha512) and the launcher runs the container with:

```text
docker run --rm --runtime=runsc --cap-add SYS_ADMIN <image> ...
```

- **`SYS_ADMIN` is granted by gVisor only.** Under `runsc`, the container's
  system calls are handled by gVisor's user-space kernel, so the capability
  applies inside gVisor, not to the runner VM's real kernel. That's what lets
  `cm` create its namespaces safely.
- **Never used:** `--privileged`, or `--security-opt seccomp=unconfined` /
  `apparmor=unconfined`. Those would hand the same capability to the real
  kernel.
- **`runc` opt-out:** `container_runtime: runc` skips gVisor and adds no
  capabilities. Only the outer container then protects the runner, and `cm`'s
  sandbox can't start.
- **Cost:** the install step downloads ~130 MB and logs how long it took.

### Sandbox network: closed by default

cm-runner sets `cm`'s sandbox network profile from
`CODEMENDER_SANDBOX_NETWORK_PROFILE`. The default is `permissive-closed`: the
agent's commands (builds, tests, PoCs during `verify`/`fix`) get no network.
`permissive-open` gives them full network access and must be set explicitly,
e.g. for projects whose build or tests download dependencies. `find` is
unaffected, as its agent has no network tools.

### Known limitation: violations aren't reported under gVisor

Under gVisor, `cm`'s "Sandbox violations detected" output is **always empty**.
`cm` collects violations with `SECCOMP_FILTER_FLAG_NEW_LISTENER`, which gVisor
doesn't support. Blocking still works: network, out-of-workspace reads, and
out-of-workspace writes are denied; they just aren't listed.

### Fork pull requests

By default, GitHub gives fork PR workflows a read-only token and no secrets,
so the image pull or the PR comment fails. For **private or internal**
repositories, an org (or repo) admin can allow them:

Settings → Actions → General → *Fork pull request workflows in private
repositories*, then tick:

- Run workflows from fork pull requests
- Send write tokens to workflows from pull requests
- Send secrets and variables to workflows from pull requests
- (Optional) Require approval for fork pull request workflows

Without these settings, the job fails and the log names the setting to enable.

- **Trust:** a fork author can edit the workflow or the copied action, and it
  runs with the write token and secrets. This is fine only when forks come from
  trusted people (for example, employees in a corporate org).
- **Public repositories** don't have these settings. They would need a
  `pull_request` + `workflow_run` split (see [Roadmap](#roadmap)). Don't use
  `pull_request_target` with a checkout of the fork's code.
- **To check at M1:** whether GCP Workload Identity Federation tokens are
  issued to fork workflows under these settings.

## Running the tests

```bash
cd github-actions/actions/codemender
python3 -m unittest discover -s tests -v
```

## Roadmap

| Milestone | What it adds | Blocked on |
| :--- | :--- | :--- |
| M0 | `cm-runner version` via `docker run` under gVisor, PR comment | — |
| **M1** (this) | Real findings posted to the PR (`init` + `find-diff`), SARIF/Markdown artifacts, GCP auth (WIF) | — |
| M2 | One-click fix suggestions on the PR | `cm-runner fix` |
| M3 | `@codemender verify <id>` and the Security Gate status check | `cm-runner verify` |
| Later | Cloud Run / GKE launchers | Workstream 3 |
| Later | Official release as a published, versioned action (`uses: <org>/<action>@v1`) instead of copying the code in | A public home for the action; the copy-in version working end to end |
| Later | Fork PRs in **public** repos: split into an unprivileged `pull_request` job and a privileged `workflow_run` job that posts results | A customer need for public repos |
