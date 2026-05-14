# CodeGuardian IDE Assistant

Minimal VS Code extension for local CodeGuardian results.

## Features

- Reads `codeguardian-results.json` from the current workspace.
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

The extension expects to be used from the repository workspace that contains the exported results file and the Python CLI.

## Limitations

- Local JSON only.
- No Bitbucket, Jenkins or SonarQube calls.
- The CLI refuses to apply when `original_code` no longer matches the local file.
