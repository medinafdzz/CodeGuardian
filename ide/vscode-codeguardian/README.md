# CodeGuardian IDE Assistant

Minimal VS Code extension for local CodeGuardian results.

## Features

- Reads `codeguardian-results.json` from the current workspace.
- Downloads the latest archived Jenkins results artifact.
- Lets the developer choose a Jenkins PR job when several PRs exist.
- Watches the Jenkins PR build inferred from the local branch and Bitbucket PR id.
- Validates that the downloaded artifact matches the local repository and commit before allowing apply.
- Shows a review dashboard with summary counters, filters and suggestions grouped by file.
- Opens the affected file and line.
- Previews original and proposed code in a virtual document.
- Opens the local Git diff after applying suggestions.
- Applies one or more selected suggestions by calling the Python CLI.
- Detects overlapping selected suggestions before batch apply.
- Supports optional project rule profiles through `.codeguardian.json`.
- Provides a `CodeGuardian` Activity Log output channel.
- Uses progressive rendering with `Show 50 more` for large pull requests.
- Imports Jenkins and Bitbucket credentials from `.env` into VS Code SecretStorage.

The `Review Dashboard` view is the main UI. The `File Tree` view remains available as a compact native tree.

## Development

```bash
npm install
npm run compile
```

Open this folder in VS Code and run the extension host.

## Configuration

- `codeguardian.resultsFile`: defaults to `codeguardian-results.json`.
- `codeguardian.pythonPath`: defaults to `python`.
- `codeguardian.cliPath`: defaults to `tools/codeguardian_cli.py`.
- `codeguardian.jenkinsArtifactUrl`: optional full Jenkins artifact URL.
- `codeguardian.jenkinsUrl`: base Jenkins URL.
- `codeguardian.jenkinsJobPath`: slash-separated Jenkins base multibranch job path, for example `CodeGuardian/sample-mixed`. Do not include `PR-2`; the extension appends `PR-<id>` automatically.
- `codeguardian.jenkinsUser`: optional Jenkins user.
- `codeguardian.jenkinsApiToken`: optional Jenkins API token.
- `codeguardian.bitbucketEmail`: optional Bitbucket email for automatic PR detection.
- `codeguardian.bitbucketApiToken`: optional Bitbucket API token or app password for automatic PR detection.
- `codeguardian.autoDownload`: defaults to `true`; downloads new artifacts automatically when a successful watched build archives the results.
- `codeguardian.pollIntervalSeconds`: polling interval, default `45`.
- `codeguardian.allowApplyWithUnknownArtifact`: defaults to `false`. Allows applying suggestions when the artifact lacks enough metadata to validate local context.
- `codeguardian.watchBuildOnStartup`: defaults to `true`; starts watching when the current artifact is missing or stale.
- `codeguardian.watchBuildOnGitChange`: defaults to `true`; starts watching when local branch or `HEAD` changes.
- `codeguardian.maxBuildWatchMinutes`: defaults to `30`; stops a build watch after this timeout.

The extension expects to be used from the repository workspace that contains the Python CLI or has `codeguardian.cliPath` configured to the core repository CLI.

Run `CodeGuardian: Download Latest Results` to fetch `codeguardian-results.json` from Jenkins before refreshing the suggestions. The extension first tries to match the current local Git branch to an open Bitbucket PR, then falls back to Jenkins job metadata or manual selection.

Run `CodeGuardian: Select Pull Request` to choose between available Jenkins jobs named `PR-*`.

## Project rule profiles

Repositories can define a workspace-local `.codeguardian.json` file to tune the IDE assistant for a project or team. This file is optional; if it is missing or invalid, CodeGuardian falls back to the default behavior and writes a warning to the `CodeGuardian` Activity Log.

Example:

```json
{
  "profile": "ESS",
  "validationCommand": "python ess_check.py --phase generation",
  "defaultTab": "Issues",
  "maxRecommended": 5,
  "allowApply": true,
  "showOptimizations": false
}
```

Supported fields:

- `profile`: display name for the active project profile, for example `default`, `ESS`, `ATM Tools`, `security-only` or `performance`.
- `validationCommand`: optional project validation command documented for the team workflow.
- `defaultTab`: initial dashboard tab. Supported values are `All`, `Issues`, `Optimizations`, `Applied`, `Changed` and `Dismissed`.
- `maxRecommended`: number of suggestions shown in `Recommended fixes`.
- `allowApply`: when `false`, disables `Apply` and `Apply Selected` even if the artifact is otherwise valid.
- `showOptimizations`: when `false`, optimization suggestions are hidden from the default `All` tab but remain accessible through explicit filters/tabs.

Do not store secrets, tokens, passwords or API keys in `.codeguardian.json`. Use `.env` plus VS Code SecretStorage for credentials.

## Apply safety and productivity tools

Before `Apply Selected`, CodeGuardian checks selected ready suggestions for overlapping edits in the same file. It blocks the batch when line ranges overlap, when the same `original_code` appears more than once, or when one `original_code` block contains another. In that case, apply the suggestions one by one so each patch can be validated independently.

`Open Git Diff` opens the VS Code Source Control view when local changes exist, so developers can review the result after applying suggestions. If there are no local changes, CodeGuardian shows `No local changes to show.`

`Open Activity Log` opens the `CodeGuardian` OutputChannel. It records concise, timestamped events such as activation, profile loading, credential source, PR selection, Jenkins build status changes, artifact download/validation, apply attempts, conflicts and dismiss actions. It never logs secrets or token values.

For large pull requests, the dashboard renders the first 50 filtered suggestions and shows `Show 50 more` to progressively render additional results. Search, filters and tabs apply to the full dataset first; pagination is applied after filtering. `Recommended fixes` is not affected by pagination.

## Automatic Jenkins PR build watching

The extension can infer the Jenkins PR job without a full PR job URL:

1. Reads the local Bitbucket remote with `git remote get-url origin`.
2. Reads the local branch with `git branch --show-current`.
3. Uses Bitbucket REST to find the open PR whose source branch matches the local branch.
4. Builds the Jenkins PR job path as `<jenkinsJobPath>/PR-<prId>`.
5. Polls `<jenkinsUrl>/job/.../lastBuild/api/json`.

For example:

```text
codeguardian.jenkinsUrl = http://localhost:8080
codeguardian.jenkinsJobPath = CodeGuardian/sample-mixed
detected PR = 2
resolved job = CodeGuardian/sample-mixed/PR-2
```

When Jenkins reports a successful build containing `codeguardian-results.json`, `codeguardian.autoDownload=true` downloads the artifact, refreshes suggestions and revalidates repository/commit context. If auto-download is disabled, the dashboard shows the artifact as ready and the normal `Download Results` button can download it.

Build progress is estimated from Jenkins `timestamp` and `estimatedDuration`. It is capped below 100% while the build is still running; completed builds show 100%.

Use these commands for manual control:

```text
CodeGuardian: Watch Jenkins Build
CodeGuardian: Stop Jenkins Build Watch
```

## Artifact validation

Before enabling `Apply`, CodeGuardian compares `codeguardian-results.json` with the current workspace:

- local repository from `git remote get-url origin`
- local branch from `git branch --show-current`
- local commit from `git rev-parse HEAD`
- artifact metadata such as `repository`, `workspace`, `pull_request`, `head_commit`, `branch`, `build_number` or `run_id`

The dashboard banner shows:

- `VALID`: enough metadata exists and matches the local workspace.
- `STALE`: the artifact commit does not match local `HEAD`.
- `MISMATCH`: the artifact repository, workspace or selected PR does not match.
- `UNKNOWN`: metadata is missing or local Git context cannot be fully detected.

`Apply`, `Undo` and `Apply Selected` are disabled for `STALE`, `MISMATCH` and, by default, `UNKNOWN`. Set `codeguardian.allowApplyWithUnknownArtifact` to `true` only when using legacy artifacts without metadata and after verifying the file manually.

## Credentials

Use SecretStorage for Jenkins and Bitbucket credentials. The extension can import them from a workspace-root `.env` or `.codeguardian.env` file:

```env
CODEGUARDIAN_JENKINS_USER=my-user
CODEGUARDIAN_JENKINS_API_TOKEN=my-jenkins-token
CODEGUARDIAN_BITBUCKET_EMAIL=my-email@example.com
CODEGUARDIAN_BITBUCKET_API_TOKEN=my-bitbucket-token
CODEGUARDIAN_JENKINS_URL=http://localhost:8080
CODEGUARDIAN_JENKINS_JOB_PATH=CodeGuardian/sample-mixed
CODEGUARDIAN_AUTO_DOWNLOAD=true
CODEGUARDIAN_POLL_INTERVAL_SECONDS=45
CODEGUARDIAN_CLI_PATH=C:\Users\ajmedinaf\VSCode\TFG\codeguardian-core\tools\codeguardian_cli.py
CODEGUARDIAN_RESULTS_FILE=codeguardian-results.json
CODEGUARDIAN_PYTHON_PATH=python
```

Run `CodeGuardian: Import Credentials From .env` to store them securely. The extension also attempts import on activation if SecretStorage is empty.

Non-secret configuration such as Jenkins URL, Jenkins job path, CLI path, results file and Python executable is read directly from the workspace `.env`, so it does not need to be placed in User `settings.json`.

Credential precedence:

1. VS Code SecretStorage
2. `.env` / `.codeguardian.env` import

Credentials are not read from User `settings.json`. The extension never shows token values in the UI and does not write credentials into `codeguardian-results.json`.

Run `CodeGuardian: Clear Stored Credentials` to remove stored secrets, or `CodeGuardian: Show Credential Status` to check whether credentials are configured.

Do not commit `.env` or `.codeguardian.env`. They are ignored by this repository `.gitignore`.

## Limitations

- Local JSON plus optional Jenkins artifact download.
- No Bitbucket or SonarQube calls.
- The CLI refuses to apply when `original_code` no longer matches the local file.
- Build progress is estimated; Jenkins does not always provide exact progress.
- Automatic PR detection requires Bitbucket credentials.
- Jenkins PR jobs must follow the `PR-<id>` multibranch convention.
