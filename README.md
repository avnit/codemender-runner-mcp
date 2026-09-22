# CodeMender Runner: Canonical CodeMender Deployment Scenarios

> [!NOTE]
> While we hold this code to a high standard, this is example code and
> should not be blindly applied in. Use these canoncial examples as the 
> foundation to build your deployed code upon rather than as ready to
> deploy. Your scenario will likely differ in subtle ways that will
> require modifications to the presented code before it it fit for
> purpose.

This repository contains a set of opinionated deployment scenarioes for
CodeMender.

Our philosophy for this repo is two fold:

1. The deployment scenario and associated workflow should work out of
   the box for most customers most of the time.
2. For customers with unique situtations, the scenario must provide the
   basic building blocks that can be used to solve these unique needs.

For example, while our github actions only runners are the ideal way to
deploy exclusively the github actions when there are no additional
constraints, the overall worklow still provides all the basic building
blocks (chaining workflow, on demand execution, parallel running) to
solve most constraints that would arise and necessitate a slightly
different setup.

## Deployment Scenarios (Repo Organization)

We organize this repo using the following folders and meanings:

- `cm-runner/`: Scenario independent codemender orchstrator. This is the
  core code that all scenarios use to orchestrate the exeuctions of
  CodeMender.  Provides the necessary abstraction layers and adapters.
  See `cm-runner/README.md` for more details.
- `github-actions/`: Deployment for running CM directly inside github
  action runners.
- `cloud-run-jobs/`: Runs codemender inside cloud run jobs. Implements
  architecture from [Scaling CodeMender in CI/CD
  Pipeline](https://docs.google.com/document/d/1dAzVngHN409VYC2Z7Dgx01GJGCudij5gDW9kPhmZf2U).
  Contains code for orchstrating the runtime from different repo hosting
  sites (github first).

## Contributing

1. Get access to this repo. Reach out to an existing Admin (ledoux@,
   TODO) and ask to be added as a contributor.
2. Open a PR.
3. Ask an admin or maintainer to review the PR. All code in this PR must
   be treated as if will be blindly applied to prod (even though we warn
   against doing so).

All code must be reviewed by a maintainer or admin.

All code must maintain 90%+ code coverage via the test suites.
