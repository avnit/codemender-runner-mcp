# CodeMender GitLab CI Integration

Scans each merge request with CodeMender (`cm-runner find-diff`) in a gVisor
(`runsc`) sandbox on your own GitLab runner. Findings reach GitLab through the
job's SARIF report (`artifacts:reports:sarif`). If you add a GitLab token, a
single MR comment, updated on each run, also summarizes them.

[SPEC-GITLAB-CI](../docs/openspec/specs/gitlab-ci/spec.md) defines the
behavior, and its [design](../docs/openspec/specs/gitlab-ci/design.md) explains
the decisions and the security model.

To set it up:

1. Set up a dedicated runner.
1. Give the job access to Google Cloud.
1. Optionally, add a GitLab token for the MR comment.
1. Add the job to your pipeline.

## 1. Set up a dedicated runner

The job starts gVisor itself, so it needs a Docker-executor runner with
privileged mode:

```toml
[[runners]]
  name = "codemender-gvisor-runner"
  executor = "docker"
  [runners.docker]
    image = "ubuntu:22.04"
    privileged = true
```

- Give the runner the tag `codemender-gvisor`, clear **Run untagged jobs**, and
  make it available only to the projects that use CodeMender.
- Anyone who can push a branch to those projects can run any job on this
  runner, and a privileged job can take over the host
  ([GitLab docs](https://docs.gitlab.com/runner/security/#reduce-the-security-risk-of-using-privileged-containers)).
  So treat the host as untrusted:
  - Don't run anything else on it or keep other secrets there.
  - Don't give the VM a cloud identity with permissions, such as a Google
    Cloud service account with roles, an AWS instance profile, or an Azure
    managed identity. Also block jobs from reaching the metadata server
    (`169.254.169.254`).
  - If you can, run each job on a fresh VM, separate from the runner itself, so
    a job can't tamper with later jobs or read the runner's token. The Docker
    Autoscaler executor does this with `capacity_per_instance = 1` and
    `max_use_count = 1`.
- Limit egress to your GitLab instance, the registries the runner pulls images
  from, and the Google endpoints listed in spec REQ-0002, with a proxy or a
  firewall. A privileged job can change the host's own firewall, so prefer
  controls outside the VM. If jobs go through a proxy, set `HTTPS_PROXY` and
  `NO_PROXY` for them, for example with `environment` in `config.toml`.

A `cloudrun` backend for unprivileged runners is planned (REQ-0003).

## 2. Give the job access to Google Cloud

The job signs in with Workload Identity Federation: GitLab gives it an ID token,
and Google exchanges that for a short-lived token for a service account, so
there's no key to store. Follow
[Google's guide](https://cloud.google.com/iam/docs/workload-identity-federation-with-deployment-pipelines)
with service account impersonation and the settings below. Do steps 1 and 2
once, and steps 3 and 4 for each GitLab project.

1. Create a workload identity pool and an OIDC provider with:
   - **Issuer URL:** your GitLab URL, such as `https://gitlab.com` or
     `https://gitlab.example.com`.
   - **Audience:** the default audience. The template requests an ID token for
     it.
   - **Attribute mapping:**
     - `google.subject` = `assertion.project_id + ':' + assertion.job_id`
     - `attribute.project_id` = `assertion.project_id`
     - `attribute.namespace_id` = `assertion.namespace_id` (Google's guide asks
       for a mapping for each field the condition checks)
   - **Attribute condition:** `assertion.namespace_id == 'GROUP_ID'`, where
     `GROUP_ID` is the numeric ID of the group or subgroup that directly
     contains your projects. For more than one group, join the checks with
     `||`. Google's guide requires a condition like this for GitLab.com. Use
     IDs, not paths: a path can be renamed and then claimed by someone else.
   - Don't require `ref_protected`. It's `false` in merge request pipelines for
     unprotected branches.
   - If Google can't reach your GitLab instance, upload the instance's public
     keys (from `/oauth/discovery/keys`) with `--jwk-json-path`, and upload
     them again if they change.
1. Create a service account and grant it only the Vertex AI User role
   (`roles/aiplatform.user`) on the project you'll set as `GCP_PROJECT_ID`,
   which needs the Vertex AI API enabled.
1. For each GitLab project, grant the Workload Identity User role
   (`roles/iam.workloadIdentityUser`) on the service account to:

   ```text
   principalSet://iam.googleapis.com/projects/PROJECT_NUMBER/locations/global/workloadIdentityPools/POOL_ID/attribute.project_id/GITLAB_PROJECT_ID
   ```

   `PROJECT_NUMBER` is the number of the project that holds the pool.
1. In the GitLab project's (or its group's) **Settings > CI/CD > Variables**,
   add these variables. Don't mark them Protected: protected variables don't
   reach merge request pipelines for feature branches.
   - `GCP_PROJECT_ID`: the project from step 2.
   - `GCP_WIF_PROVIDER`:
     `projects/PROJECT_NUMBER/locations/global/workloadIdentityPools/POOL_ID/providers/PROVIDER_ID`
   - `GCP_SERVICE_ACCOUNT`: the service account's email.

Don't define `GCP_OIDC_TOKEN`; the template requests it. Merge requests from
forks aren't scanned (REQ-0008).

> [!NOTE]
> **Why not map `google.subject` to `assertion.sub`, as Google's guide does?**
> GitLab's `sub` looks like
> `project_path:GROUP/PROJECT:ref_type:branch:ref:BRANCH`, and `google.subject`
> can't be longer than 127 characters. With nested groups or long branch names,
> the token exchange fails for those branches only. The mapping above stays
> short and still identifies the project and the job.

## 3. Optionally, add a GitLab token for the MR comment

Without a token, the scan still runs and its findings still reach GitLab
through the SARIF report; there's just no MR comment.

1. Create a project access token with the Reporter role, the `api` scope and an
   expiry date. Or give a GitLab service account the Reporter role in the
   project and create a token for it with the same scope and an expiry date.
   Rotate the token before it expires.
1. Add it as the CI/CD variable `CODEMENDER_GITLAB_TOKEN`, Masked but not
   Protected, since protected variables don't reach merge request pipelines for
   feature branches.

Any job in the project can read this variable, and anyone who can push a branch
can add a job, so assume the token can leak. The Reporter role limits the
damage: the token can't push code or change settings. Members can also run a
fork MR's pipeline in your project, where it can read the variable too. Review
the fork's changes before you do, or turn this off by setting
`ci_allow_fork_pipelines_to_run_in_parent_project` to `false` with the projects
API
([GitLab docs](https://docs.gitlab.com/ci/pipelines/merge_request_pipelines/#prevent-pipelines-from-fork-projects)).
[Design](../docs/openspec/specs/gitlab-ci/design.md) Decision 4 explains this
choice and the trusted comment service to revisit after v1.

## 4. Add the job to your pipeline

Keep a copy of `templates/codemender-scan.yml` in a project on your GitLab
instance, and include it at a pinned commit:

```yaml
include:
  - project: 'my-group/codemender-ci'
    ref: 'COMMIT_SHA'
    file: '/templates/codemender-scan.yml'
```

- Pin `ref:` to a full 40-character commit SHA, or to a protected tag written
  as `refs/tags/TAG`, so changes to the template project don't reach your
  pipelines unreviewed.
- Whoever runs a pipeline needs access to the template project: at least the
  Reporter role if it's private. If it's internal, any signed-in user who isn't
  external has access
  ([GitLab docs](https://docs.gitlab.com/ci/yaml/#includeproject)).
- Alternatives: package the template as a
  [CI/CD component](https://docs.gitlab.com/ci/components/#use-a-component),
  include it from a public URL with `include:remote` and
  [`integrity:`](https://docs.gitlab.com/ci/yaml/#includeintegrity), or copy the
  jobs into your `.gitlab-ci.yml`.

The template adds `codemender-scan`, which runs only in merge request
pipelines. If your other jobs run in branch pipelines, a push to a branch with
an open MR then starts two pipelines, and **Pipelines must succeed** goes by
the merge request pipeline. To get one pipeline, run your other jobs in merge
request pipelines too, and switch between branch and merge request pipelines
with `workflow:rules`
([GitLab docs](https://docs.gitlab.com/ci/yaml/workflow/#switch-between-branch-pipelines-and-merge-request-pipelines)).

To change a setting, redefine it in a job with the same name in your
`.gitlab-ci.yml`, and GitLab merges it into the template's job
([GitLab docs](https://docs.gitlab.com/ci/yaml/includes/#override-included-configuration-values)):

```yaml
codemender-scan:
  variables:
    GIT_DEPTH: "200"
```

- **Large repositories:** The template clones the full history
  (`GIT_DEPTH: "0"`) so the MR's base commit is there. Lower it as shown above;
  if the base commit is then missing, the job fails and says to raise it. A
  project or group CI/CD variable named `GIT_DEPTH` also overrides the
  template's value.
- **Long scans:** Raise `timeout:` (30 minutes in the template). REQ-0008
  explains how the job splits it.
- **Mirrored images:** Set `image: name:` in the job, keeping a digest. A
  plain `image:` string replaces the template's whole `image:` setting,
  including `entrypoint: [""]`, which clears the image's `cm-runner` entrypoint
  so GitLab can run the job's script.
- **Rules:** Don't redefine `rules:`. GitLab replaces arrays instead of merging
  them, so yours would drop the template's rules, such as the ones that skip
  forks and merge trains. To turn the scan off, set the CI/CD variable
  `CODEMENDER_SCAN_ON_MR` to `false`.

Spec REQ-0001 lists the other optional variables. As in step 2, don't mark them
Protected.

To block merges on findings, have your security team add a
[merge request approval policy](https://docs.gitlab.com/user/application_security/policies/merge_request_approval_policies/),
preferably for the whole group. Its `scan_finding` rule should cover the
[types GitLab assigns](https://docs.gitlab.com/user/application_security/detect/sarif/#assigned-report-types)
to CodeMender's findings: SAST, secret detection and dependency scanning.

## Running the unit tests

```bash
python3 -m unittest discover -s gitlab-ci/tests -v
```
