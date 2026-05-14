# CodeGuardian IDE Assistant

Minimal VS Code extension for local CodeGuardian results.

## Features

- Reads `codeguardian-results.json` from the current workspace.
- Downloads the latest archived Jenkins results artifact.
- Shows suggestions grouped by file.
- Opens the affected file and line.
- Previews original and proposed code in a virtual document.
- Applies one or more selected suggestions by calling the Python CLI.

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
- `codeguardian.jenkinsJobPath`: slash-separated Jenkins job path, for example `CodeGuardian/sample-mixed/PR-1`.
- `codeguardian.jenkinsUser`: optional Jenkins user.
- `codeguardian.jenkinsApiToken`: optional Jenkins API token.

The extension expects to be used from the repository workspace that contains the Python CLI or has `codeguardian.cliPath` configured to the core repository CLI.

Run `CodeGuardian: Download Latest Results` to fetch `codeguardian-results.json` from Jenkins before refreshing the suggestions.

## Limitations

- Local JSON plus optional Jenkins artifact download.
- No Bitbucket or SonarQube calls.
- The CLI refuses to apply when `original_code` no longer matches the local file.
