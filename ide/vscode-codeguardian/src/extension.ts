import * as vscode from 'vscode';
import * as fs from 'fs';
import * as path from 'path';
import * as http from 'http';
import * as https from 'https';
import { execFile } from 'child_process';
import { URL } from 'url';

type Suggestion = {
  id: string;
  source: string;
  severity: string;
  file: string;
  line: number;
  target_name: string;
  problem: string;
  solution: string;
  original_code: string;
  proposed_code: string;
  required_imports?: string[];
};

type CliResult = {
  stdout: string;
  stderr: string;
};

class FileNode extends vscode.TreeItem {
  constructor(public readonly filePath: string, public readonly suggestions: Suggestion[]) {
    super(filePath, vscode.TreeItemCollapsibleState.Collapsed);
    this.contextValue = 'file';
    this.description = `${suggestions.length}`;
  }
}

class SuggestionNode extends vscode.TreeItem {
  constructor(public readonly suggestion: Suggestion) {
    const target = suggestion.target_name || short(suggestion.problem, 50);
    super(`${suggestion.severity || 'INFO'} L${suggestion.line}: ${target}`, vscode.TreeItemCollapsibleState.None);
    this.contextValue = 'suggestion';
    this.description = suggestion.source;
    this.tooltip = suggestion.problem;
    this.command = {
      command: 'codeguardian.previewSuggestion',
      title: 'Preview Suggestion',
      arguments: [this],
    };
  }
}

class SuggestionsProvider implements vscode.TreeDataProvider<FileNode | SuggestionNode> {
  private readonly emitter = new vscode.EventEmitter<FileNode | SuggestionNode | undefined | null | void>();
  readonly onDidChangeTreeData = this.emitter.event;
  private suggestions: Suggestion[] = [];

  refresh(): void {
    this.suggestions = loadSuggestions();
    this.emitter.fire();
  }

  getTreeItem(element: FileNode | SuggestionNode): vscode.TreeItem {
    return element;
  }

  getChildren(element?: FileNode | SuggestionNode): Thenable<Array<FileNode | SuggestionNode>> {
    if (element instanceof FileNode) {
      return Promise.resolve(element.suggestions.map((suggestion) => new SuggestionNode(suggestion)));
    }
    const grouped = new Map<string, Suggestion[]>();
    for (const suggestion of this.suggestions) {
      const fileItems = grouped.get(suggestion.file) || [];
      fileItems.push(suggestion);
      grouped.set(suggestion.file, fileItems);
    }
    return Promise.resolve(Array.from(grouped.entries()).map(([file, items]) => new FileNode(file, items)));
  }
}

export function activate(context: vscode.ExtensionContext): void {
  const provider = new SuggestionsProvider();
  const tree = vscode.window.createTreeView('codeguardianSuggestions', {
    treeDataProvider: provider,
    canSelectMany: true,
  });
  context.subscriptions.push(tree);

  context.subscriptions.push(vscode.commands.registerCommand('codeguardian.refreshSuggestions', () => {
    provider.refresh();
  }));
  context.subscriptions.push(vscode.commands.registerCommand('codeguardian.downloadLatestResults', async () => {
    try {
      await downloadLatestResults();
      provider.refresh();
    } catch (error) {
      vscode.window.showErrorMessage(`CodeGuardian results download failed: ${String(error)}`);
    }
  }));
  context.subscriptions.push(vscode.commands.registerCommand('codeguardian.openSuggestion', (node?: SuggestionNode) => {
    if (node) {
      openSuggestion(node.suggestion);
    }
  }));
  context.subscriptions.push(vscode.commands.registerCommand('codeguardian.previewSuggestion', (node?: SuggestionNode) => {
    if (node) {
      previewSuggestion(node.suggestion);
    }
  }));
  context.subscriptions.push(vscode.commands.registerCommand('codeguardian.applySuggestion', async (node?: SuggestionNode) => {
    if (node) {
      await applySuggestion(node.suggestion);
      provider.refresh();
    }
  }));
  context.subscriptions.push(vscode.commands.registerCommand(
    'codeguardian.applySelectedSuggestion',
    async (node?: SuggestionNode, selected?: SuggestionNode[]) => {
      const selectedNodes = selected?.length ? selected : tree.selection.filter(isSuggestionNode);
      const suggestions = selectedNodes.length ? selectedNodes.map((item) => item.suggestion) : node ? [node.suggestion] : [];
      if (!suggestions.length) {
        vscode.window.showInformationMessage('Select one or more CodeGuardian suggestions first.');
        return;
      }
      await applySelectedSuggestions(suggestions);
      provider.refresh();
    }
  ));

  provider.refresh();
}

export function deactivate(): void {}

function workspaceRoot(): vscode.WorkspaceFolder {
  const folder = vscode.workspace.workspaceFolders?.[0];
  if (!folder) {
    throw new Error('Open a workspace before using CodeGuardian.');
  }
  return folder;
}

function configValue(name: string): string {
  return vscode.workspace.getConfiguration('codeguardian').get<string>(name) || '';
}

function absoluteWorkspacePath(relativePath: string): string {
  return path.join(workspaceRoot().uri.fsPath, relativePath);
}

function resultsPath(): string {
  return absoluteWorkspacePath(configValue('resultsFile') || 'codeguardian-results.json');
}

function loadSuggestions(): Suggestion[] {
  try {
    const file = resultsPath();
    if (!fs.existsSync(file)) {
      vscode.window.showInformationMessage(`CodeGuardian results file not found: ${file}`);
      return [];
    }
    const data = JSON.parse(fs.readFileSync(file, 'utf8'));
    return Array.isArray(data.suggestions) ? data.suggestions : [];
  } catch (error) {
    vscode.window.showErrorMessage(`Failed to load CodeGuardian results: ${String(error)}`);
    return [];
  }
}

async function downloadLatestResults(): Promise<void> {
  const url = buildJenkinsArtifactUrl();
  const user = configValue('jenkinsUser');
  const token = configValue('jenkinsApiToken');
  const body = await downloadText(url, user && token ? { user, token } : undefined);

  try {
    JSON.parse(body);
  } catch (error) {
    throw new Error(`Downloaded Jenkins artifact is not valid JSON: ${String(error)}`);
  }

  fs.writeFileSync(resultsPath(), body, 'utf8');
  vscode.window.showInformationMessage(`Downloaded CodeGuardian results to ${resultsPath()}`);
}

function buildJenkinsArtifactUrl(): string {
  const directUrl = configValue('jenkinsArtifactUrl').trim();
  if (directUrl) {
    return directUrl;
  }

  const baseUrl = configValue('jenkinsUrl').trim().replace(/\/+$/, '');
  const jobPath = configValue('jenkinsJobPath').trim();
  const buildSelector = configValue('jenkinsBuildSelector').trim() || 'lastSuccessfulBuild';
  const artifactName = configValue('jenkinsArtifactName').trim() || 'codeguardian-results.json';
  if (!baseUrl || !jobPath) {
    throw new Error(
      'Configure codeguardian.jenkinsArtifactUrl, or configure both codeguardian.jenkinsUrl and codeguardian.jenkinsJobPath.'
    );
  }

  const encodedJobPath = jobPath
    .split('/')
    .filter((part) => part.length > 0)
    .map((part) => `job/${encodeURIComponent(part)}`)
    .join('/');
  return `${baseUrl}/${encodedJobPath}/${encodeURIComponent(buildSelector)}/artifact/${encodeURIComponent(artifactName)}`;
}

function downloadText(url: string, auth?: { user: string; token: string }, redirects = 0): Promise<string> {
  return new Promise((resolve, reject) => {
    const parsed = new URL(url);
    const client = parsed.protocol === 'https:' ? https : http;
    const headers: Record<string, string> = {};
    if (auth) {
      headers.Authorization = `Basic ${Buffer.from(`${auth.user}:${auth.token}`).toString('base64')}`;
    }

    const request = client.get(parsed, { headers }, (response) => {
      const status = response.statusCode || 0;
      const location = response.headers.location;
      if ([301, 302, 303, 307, 308].includes(status) && location) {
        response.resume();
        if (redirects >= 5) {
          reject(new Error('Too many redirects while downloading Jenkins artifact.'));
          return;
        }
        const redirected = new URL(location, parsed).toString();
        downloadText(redirected, auth, redirects + 1).then(resolve, reject);
        return;
      }
      if (status < 200 || status >= 300) {
        response.resume();
        reject(new Error(`Jenkins artifact download failed with HTTP ${status}.`));
        return;
      }

      const chunks: Buffer[] = [];
      response.on('data', (chunk: Buffer) => chunks.push(chunk));
      response.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')));
    });
    request.on('error', reject);
  });
}

async function openSuggestion(suggestion: Suggestion): Promise<void> {
  const document = await vscode.workspace.openTextDocument(absoluteWorkspacePath(suggestion.file));
  const editor = await vscode.window.showTextDocument(document);
  const line = Math.max(0, (suggestion.line || 1) - 1);
  const position = new vscode.Position(line, 0);
  editor.selection = new vscode.Selection(position, position);
  editor.revealRange(new vscode.Range(position, position), vscode.TextEditorRevealType.InCenter);
}

async function previewSuggestion(suggestion: Suggestion): Promise<void> {
  const content = [
    `# CodeGuardian Suggestion ${suggestion.id}`,
    '',
    `File: ${suggestion.file}:${suggestion.line}`,
    `Severity: ${suggestion.severity}`,
    `Source: ${suggestion.source}`,
    '',
    '## Problem',
    suggestion.problem || '',
    '',
    '## Solution',
    suggestion.solution || '',
    '',
    '## Original code',
    '```',
    suggestion.original_code || '',
    '```',
    '',
    '## Proposed code',
    '```',
    suggestion.proposed_code || '',
    '```',
  ].join('\n');
  const document = await vscode.workspace.openTextDocument({ content, language: 'markdown' });
  await vscode.window.showTextDocument(document, { preview: true });
}

async function applySuggestion(suggestion: Suggestion): Promise<void> {
  const answer = await vscode.window.showWarningMessage(
    `Apply CodeGuardian suggestion ${suggestion.id} to ${suggestion.file}?`,
    { modal: true },
    'Apply'
  );
  if (answer !== 'Apply') {
    return;
  }

  const python = configValue('pythonPath') || 'python';
  const cli = absoluteWorkspacePath(configValue('cliPath') || 'tools/codeguardian_cli.py');
  const result = await runCliWithFallback(python, [cli, 'apply', '--file', resultsPath(), '--id', suggestion.id]);
  vscode.window.showInformationMessage(result.stdout || 'CodeGuardian suggestion applied.');
}

async function applySelectedSuggestions(suggestions: Suggestion[]): Promise<void> {
  const ids = suggestions.map((suggestion) => suggestion.id);
  const answer = await vscode.window.showWarningMessage(
    `Apply ${ids.length} CodeGuardian suggestion(s)?`,
    { modal: true },
    'Apply'
  );
  if (answer !== 'Apply') {
    return;
  }

  const python = configValue('pythonPath') || 'python';
  const cli = absoluteWorkspacePath(configValue('cliPath') || 'tools/codeguardian_cli.py');
  const result = await runCliWithFallback(python, [
    cli,
    'apply-selected',
    '--file',
    resultsPath(),
    '--ids',
    ids.join(','),
  ]);
  vscode.window.showInformationMessage(result.stdout || 'CodeGuardian suggestions applied.');
}

async function runCliWithFallback(command: string, args: string[]): Promise<CliResult> {
  try {
    return await runCli(command, args);
  } catch (error) {
    if (command !== 'python' || !isMissingExecutable(error)) {
      throw error;
    }
    return runCli('python3', args);
  }
}

function runCli(command: string, args: string[]): Promise<CliResult> {
  return new Promise((resolve, reject) => {
    execFile(command, args, { cwd: workspaceRoot().uri.fsPath }, (error, stdout, stderr) => {
      if (error) {
        const message = stderr || stdout || error.message;
        vscode.window.showErrorMessage(`CodeGuardian apply failed: ${message}`);
        reject(error);
        return;
      }
      resolve({ stdout, stderr });
    });
  });
}

function isMissingExecutable(error: unknown): boolean {
  const code = (error as NodeJS.ErrnoException | undefined)?.code;
  return code === 'ENOENT';
}

function isSuggestionNode(node: FileNode | SuggestionNode): node is SuggestionNode {
  return node instanceof SuggestionNode;
}

function short(text: string, max: number): string {
  const normalized = (text || '').replace(/\s+/g, ' ').trim();
  return normalized.length <= max ? normalized : `${normalized.slice(0, max - 3)}...`;
}
