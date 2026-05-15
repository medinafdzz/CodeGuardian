# CodeGuardian IDE Assistant

Minimal VS Code extension for local CodeGuardian results.

## Features

- Reads `codeguardian-results.json` from the current workspace.
- Downloads the latest archived Jenkins results artifact.
- Lets the developer choose a Jenkins PR job when several PRs exist.
- Validates that the downloaded artifact matches the local repository and commit before allowing apply.
- Shows a review dashboard with summary counters, filters and suggestions grouped by file.
- Opens the affected file and line.
- Previews original and proposed code in a virtual document.
- Applies one or more selected suggestions by calling the Python CLI.
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
- `codeguardian.jenkinsJobPath`: slash-separated Jenkins job path. Use `CodeGuardian/sample-mixed` to select PRs or `CodeGuardian/sample-mixed/PR-1` to pin one PR.
- `codeguardian.jenkinsUser`: optional Jenkins user.
- `codeguardian.jenkinsApiToken`: optional Jenkins API token.
- `codeguardian.bitbucketEmail`: optional Bitbucket email for automatic PR detection.
- `codeguardian.bitbucketApiToken`: optional Bitbucket API token or app password for automatic PR detection.
- `codeguardian.autoDownload`: polls Jenkins and downloads new artifacts automatically.
- `codeguardian.pollIntervalSeconds`: polling interval, default `45`.
- `codeguardian.allowApplyWithUnknownArtifact`: defaults to `false`. Allows applying suggestions when the artifact lacks enough metadata to validate local context.

The extension expects to be used from the repository workspace that contains the Python CLI or has `codeguardian.cliPath` configured to the core repository CLI.

Run `CodeGuardian: Download Latest Results` to fetch `codeguardian-results.json` from Jenkins before refreshing the suggestions. The extension first tries to match the current local Git branch to an open Bitbucket PR, then falls back to Jenkins job metadata or manual selection.

Run `CodeGuardian: Select Pull Request` to choose between available Jenkins jobs named `PR-*`.

Enable `codeguardian.autoDownload` to let the extension poll Jenkins and refresh automatically when a successful build archives `codeguardian-results.json`.

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
```

Run `CodeGuardian: Import Credentials From .env` to store them securely. The extension also attempts import on activation if SecretStorage is empty.

Credential precedence:

1. VS Code SecretStorage
2. `.env` / `.codeguardian.env` import
3. Existing VS Code settings fallback

Settings fallback remains for compatibility, but tokens should not be stored in `settings.json`. The extension never shows token values in the UI and does not write credentials into `codeguardian-results.json`.

Run `CodeGuardian: Clear Stored Credentials` to remove stored secrets, or `CodeGuardian: Show Credential Status` to check whether credentials are configured.

Do not commit `.env` or `.codeguardian.env`. They are ignored by this repository `.gitignore`.

## Limitations

- Local JSON plus optional Jenkins artifact download.
- No Bitbucket or SonarQube calls.
- The CLI refuses to apply when `original_code` no longer matches the local file.
