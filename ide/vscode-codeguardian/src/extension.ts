import * as vscode from 'vscode';
import * as fs from 'fs';
import * as path from 'path';
import * as http from 'http';
import * as https from 'https';
import { execFile, execFileSync } from 'child_process';
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
  status?: string;
};

type CliResult = {
  stdout: string;
  stderr: string;
};

type SuggestionStatus = 'open' | 'applied' | 'changed';

type ResultsData = {
  suggestions: Suggestion[];
  dismissedIds: string[];
  artifact: ArtifactState;
  applyAllowed: boolean;
  applyDisabledReason: string;
  credentialStatus: CredentialStatus;
};

type ArtifactState = {
  status: 'downloaded' | 'missing' | 'stale' | 'error' | 'unknown';
  validation: ArtifactValidationState;
  prId?: string;
  buildNumber?: string;
  commit?: string;
  localCommit?: string;
  downloadedAt?: string;
  message?: string;
};

type ArtifactValidationState = 'valid' | 'stale' | 'mismatch' | 'unknown';

type LocalGitContext = {
  workspace?: string;
  repository?: string;
  branch?: string;
  headCommit?: string;
  warnings: string[];
};

type JenkinsAuth = {
  user: string;
  token: string;
};

type JenkinsJob = {
  name: string;
  url?: string;
  color?: string;
};

type JenkinsArtifact = {
  fileName?: string;
  relativePath?: string;
};

type JenkinsBuild = {
  building?: boolean;
  result?: string | null;
  number?: number;
  url?: string;
  artifacts?: JenkinsArtifact[];
  actions?: Array<Record<string, unknown>>;
};

type JenkinsJobCandidate = JenkinsJob & {
  source: 'configured' | 'root';
};

type RepositoryInfo = {
  workspace: string;
  repo: string;
};

type CodeGuardianCredentials = {
  jenkinsUser?: string;
  jenkinsApiToken?: string;
  bitbucketEmail?: string;
  bitbucketApiToken?: string;
  source: 'secretStorage' | 'env' | 'settings' | 'missing';
};

type CredentialStatus = {
  configured: boolean;
  source: CodeGuardianCredentials['source'];
  message: string;
};

type BitbucketPullRequest = {
  id: number;
  title?: string;
  state?: string;
  source?: {
    branch?: {
      name?: string;
    };
  };
  destination?: {
    branch?: {
      name?: string;
    };
  };
};

const DISMISSED_KEY = 'codeguardian.dismissedSuggestionIds';
const ARTIFACT_STATE_KEY = 'codeguardian.artifactState';
const SELECTED_PR_KEY = 'codeguardian.selectedPrId';
const DIFF_SCHEME = 'codeguardian-diff';
const SECRET_KEYS = {
  jenkinsUser: 'codeguardian.jenkinsUser',
  jenkinsApiToken: 'codeguardian.jenkinsApiToken',
  bitbucketEmail: 'codeguardian.bitbucketEmail',
  bitbucketApiToken: 'codeguardian.bitbucketApiToken',
} as const;

let extensionContext: vscode.ExtensionContext | undefined;
let diffContentProvider: DiffContentProvider | undefined;
let credentialCache: CodeGuardianCredentials = { source: 'missing' };

class DiffContentProvider implements vscode.TextDocumentContentProvider {
  private readonly documents = new Map<string, string>();

  provideTextDocumentContent(uri: vscode.Uri): string {
    return this.documents.get(uri.toString()) || '';
  }

  set(uri: vscode.Uri, content: string): void {
    this.documents.set(uri.toString(), content);
  }
}

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

class DashboardProvider implements vscode.WebviewViewProvider {
  private view?: vscode.WebviewView;

  constructor(private readonly context: vscode.ExtensionContext) {}

  resolveWebviewView(webviewView: vscode.WebviewView): void {
    this.view = webviewView;
    webviewView.webview.options = {
      enableScripts: true,
      localResourceRoots: [this.context.extensionUri],
    };
    webviewView.webview.onDidReceiveMessage(async (message) => {
      try {
        await this.handleMessage(message);
      } catch (error) {
        vscode.window.showErrorMessage(errorMessage(error));
      }
    });
    this.refresh();
  }

  refresh(): void {
    if (!this.view) {
      return;
    }
    this.view.webview.html = renderDashboardHtml(this.view.webview, this.context.extensionUri, loadResultsData(false));
  }

  async refreshStatuses(): Promise<void> {
    if (!this.view) {
      return;
    }
    this.view.webview.postMessage({ command: 'statuses', statuses: await loadSuggestionStatuses() });
  }

  private async handleMessage(message: { command?: string; id?: string; ids?: string[] }): Promise<void> {
    const suggestion = message.id ? findSuggestionById(message.id) : undefined;
    switch (message.command) {
      case 'refresh':
        this.refresh();
        this.view?.webview.postMessage({ command: 'statuses', statuses: await loadSuggestionStatuses() });
        break;
      case 'download':
        await downloadLatestResults();
        this.refresh();
        break;
      case 'selectPr':
        await selectPullRequestAndDownload();
        this.refresh();
        break;
      case 'loadStatuses':
        this.view?.webview.postMessage({ command: 'statuses', statuses: await loadSuggestionStatuses() });
        break;
      case 'open':
        if (suggestion) {
          await openSuggestion(suggestion);
        }
        break;
      case 'preview':
        if (suggestion) {
          await previewSuggestion(suggestion);
        }
        break;
      case 'diff':
        if (suggestion) {
          await diffSuggestion(suggestion);
        }
        break;
      case 'apply':
        if (suggestion) {
          await applyOpenSuggestion(suggestion);
          this.view?.webview.postMessage({ command: 'statuses', statuses: await loadSuggestionStatuses() });
        }
        break;
      case 'undo':
        if (suggestion) {
          await undoAppliedSuggestion(suggestion);
          this.view?.webview.postMessage({ command: 'statuses', statuses: await loadSuggestionStatuses() });
        }
        break;
      case 'applySelected':
        await applySelectedOpenSuggestions(message.ids || []);
        this.view?.webview.postMessage({ command: 'statuses', statuses: await loadSuggestionStatuses() });
        this.refresh();
        break;
      case 'dismiss':
        if (message.id) {
          await dismissSuggestion(message.id);
          this.refresh();
        }
        break;
      case 'restoreDismissed':
        if (message.id) {
          await restoreDismissedSuggestion(message.id);
          this.refresh();
        }
        break;
      case 'clearDismissed':
        await clearDismissedSuggestions();
        this.refresh();
        break;
    }
  }
}

export async function activate(context: vscode.ExtensionContext): Promise<void> {
  extensionContext = context;
  diffContentProvider = new DiffContentProvider();
  context.subscriptions.push(vscode.workspace.registerTextDocumentContentProvider(DIFF_SCHEME, diffContentProvider));
  await initializeCredentials();
  const provider = new SuggestionsProvider();
  const dashboard = new DashboardProvider(context);
  const tree = vscode.window.createTreeView('codeguardianSuggestions', {
    treeDataProvider: provider,
    canSelectMany: true,
  });
  context.subscriptions.push(tree);
  context.subscriptions.push(vscode.window.registerWebviewViewProvider('codeguardianDashboard', dashboard));

  context.subscriptions.push(vscode.commands.registerCommand('codeguardian.refreshSuggestions', () => {
    provider.refresh();
    dashboard.refresh();
  }));
  context.subscriptions.push(vscode.commands.registerCommand('codeguardian.downloadLatestResults', async () => {
    try {
      await downloadLatestResults();
      provider.refresh();
      dashboard.refresh();
    } catch (error) {
      vscode.window.showErrorMessage(`CodeGuardian results download failed: ${String(error)}`);
    }
  }));
  context.subscriptions.push(vscode.commands.registerCommand('codeguardian.selectPullRequest', async () => {
    try {
      await selectPullRequestAndDownload();
      provider.refresh();
      dashboard.refresh();
    } catch (error) {
      vscode.window.showErrorMessage(`CodeGuardian PR selection failed: ${errorMessage(error)}`);
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
  context.subscriptions.push(vscode.commands.registerCommand('codeguardian.diffSuggestion', (node?: SuggestionNode) => {
    if (node) {
      diffSuggestion(node.suggestion);
    }
  }));
  context.subscriptions.push(vscode.commands.registerCommand('codeguardian.importCredentialsFromEnv', async () => {
    const imported = await importCredentialsFromEnv(true);
    credentialCache = await getCodeGuardianCredentials();
    dashboard.refresh();
    vscode.window.showInformationMessage(imported ? 'CodeGuardian credentials imported into SecretStorage.' : 'No CodeGuardian credentials found in .env files.');
  }));
  context.subscriptions.push(vscode.commands.registerCommand('codeguardian.clearStoredCredentials', async () => {
    await clearStoredCredentials();
    credentialCache = await getCodeGuardianCredentials();
    dashboard.refresh();
    vscode.window.showInformationMessage('CodeGuardian stored credentials cleared.');
  }));
  context.subscriptions.push(vscode.commands.registerCommand('codeguardian.showCredentialStatus', () => {
    const status = getCredentialStatus();
    vscode.window.showInformationMessage(`CodeGuardian credentials: ${status.message}`);
  }));
  context.subscriptions.push(vscode.commands.registerCommand('codeguardian.applySuggestion', async (node?: SuggestionNode) => {
    if (node) {
      await applyOpenSuggestion(node.suggestion);
      provider.refresh();
      dashboard.refresh();
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
      await applySelectedOpenSuggestions(suggestions.map((suggestion) => suggestion.id));
      provider.refresh();
      dashboard.refresh();
    }
  ));

  provider.refresh();
  dashboard.refresh();
  let statusRefreshTimer: NodeJS.Timeout | undefined;
  const scheduleStatusRefresh = () => {
    if (statusRefreshTimer) {
      clearTimeout(statusRefreshTimer);
    }
    statusRefreshTimer = setTimeout(() => {
      void dashboard.refreshStatuses();
    }, 400);
  };
  context.subscriptions.push(vscode.workspace.onDidChangeTextDocument(scheduleStatusRefresh));
  context.subscriptions.push({ dispose: () => statusRefreshTimer && clearTimeout(statusRefreshTimer) });
  const refreshStatuses = vscode.workspace.onDidSaveTextDocument(() => {
    provider.refresh();
    dashboard.refresh();
  });
  context.subscriptions.push(refreshStatuses);
  startAutoDownloadPolling(context, provider, dashboard);
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

function configBoolean(name: string): boolean {
  return vscode.workspace.getConfiguration('codeguardian').get<boolean>(name) || false;
}

function configNumber(name: string, fallback: number): number {
  return vscode.workspace.getConfiguration('codeguardian').get<number>(name) || fallback;
}

function startAutoDownloadPolling(
  context: vscode.ExtensionContext,
  provider: SuggestionsProvider,
  dashboard: DashboardProvider,
): void {
  if (!configBoolean('autoDownload')) {
    return;
  }

  let lastDownloadedBuild = '';
  let running = false;
  const poll = async () => {
    if (running) {
      return;
    }
    running = true;
    try {
      const result = await tryDownloadReadyArtifact();
      if (result && result !== lastDownloadedBuild) {
        lastDownloadedBuild = result;
        provider.refresh();
        dashboard.refresh();
        vscode.window.showInformationMessage('CodeGuardian results updated from Jenkins.');
      }
    } catch {
      // Polling is best-effort; manual download keeps reporting detailed errors.
    } finally {
      running = false;
    }
  };

  void poll();
  const intervalMs = Math.max(15, configNumber('pollIntervalSeconds', 45)) * 1000;
  const timer = setInterval(() => void poll(), intervalMs);
  context.subscriptions.push({ dispose: () => clearInterval(timer) });
}

function runWorkspaceCommand(command: string, args: string[]): Promise<string> {
  return new Promise((resolve, reject) => {
    execFile(command, args, { cwd: workspaceRoot().uri.fsPath }, (error, stdout, stderr) => {
      if (error) {
        reject(new Error(stderr || stdout || error.message));
        return;
      }
      resolve(stdout.trim());
    });
  });
}

function runWorkspaceCommandSync(command: string, args: string[]): string {
  return execFileSync(command, args, {
    cwd: workspaceRoot().uri.fsPath,
    encoding: 'utf8',
    stdio: ['ignore', 'pipe', 'pipe'],
  }).trim();
}

async function currentGitBranch(): Promise<string> {
  return runWorkspaceCommand('git', ['branch', '--show-current']);
}

function currentGitBranchSync(): string | undefined {
  try {
    return runWorkspaceCommandSync('git', ['branch', '--show-current']) || undefined;
  } catch {
    return undefined;
  }
}

function currentGitHeadSync(): string | undefined {
  try {
    return runWorkspaceCommandSync('git', ['rev-parse', 'HEAD']) || undefined;
  } catch {
    return undefined;
  }
}

function currentRepositoryInfoSync(): RepositoryInfo | undefined {
  try {
    return repositoryInfoFromRemote(runWorkspaceCommandSync('git', ['remote', 'get-url', 'origin']));
  } catch {
    return undefined;
  }
}

async function currentRepositorySlug(): Promise<string> {
  const remote = await runWorkspaceCommand('git', ['remote', 'get-url', 'origin']);
  const normalized = remote.replace(/\\/g, '/').replace(/\.git$/, '');
  const parts = normalized.split('/');
  return parts[parts.length - 1] || path.basename(workspaceRoot().uri.fsPath);
}

async function currentRepositoryInfo(): Promise<RepositoryInfo> {
  const remote = await runWorkspaceCommand('git', ['remote', 'get-url', 'origin']);
  return repositoryInfoFromRemote(remote);
}

function repositoryInfoFromRemote(remote: string): RepositoryInfo {
  const normalized = remote
    .replace(/\\/g, '/')
    .replace(/\.git$/, '')
    .replace(/^git@bitbucket\.org:/, 'https://bitbucket.org/');
  const parts = normalized.split('/').filter(Boolean);
  const repo = parts[parts.length - 1] || path.basename(workspaceRoot().uri.fsPath);
  const workspace = parts[parts.length - 2] || '';
  if (!workspace || !repo) {
    throw new Error(`Could not detect Bitbucket workspace/repository from origin: ${remote}`);
  }
  return { workspace, repo };
}

function getLocalGitContext(): LocalGitContext {
  const warnings: string[] = [];
  const repo = currentRepositoryInfoSync();
  const branch = currentGitBranchSync();
  const headCommit = currentGitHeadSync();
  if (!repo) {
    warnings.push('local repository could not be detected');
  }
  if (!branch) {
    warnings.push('local branch could not be detected');
  }
  if (!headCommit) {
    warnings.push('local HEAD commit could not be detected');
  }
  return {
    workspace: repo?.workspace,
    repository: repo?.repo,
    branch,
    headCommit,
    warnings,
  };
}

function absoluteWorkspacePath(relativePath: string): string {
  if (path.isAbsolute(relativePath)) {
    return relativePath;
  }
  return path.join(workspaceRoot().uri.fsPath, relativePath);
}

function resultsPath(): string {
  return absoluteWorkspacePath(configValue('resultsFile') || 'codeguardian-results.json');
}

function loadSuggestions(): Suggestion[] {
  return loadResultsData().suggestions;
}

function loadResultsData(showMessage = true): ResultsData {
  const empty = (): ResultsData => ({
    suggestions: [],
    dismissedIds: dismissedSuggestionIds(),
    artifact: { status: 'missing', validation: 'unknown', message: 'no local results file' },
    applyAllowed: false,
    applyDisabledReason: 'No CodeGuardian results artifact is loaded.',
    credentialStatus: getCredentialStatus(),
  });
  try {
    const file = resultsPath();
    if (!fs.existsSync(file)) {
      if (showMessage) {
        vscode.window.showInformationMessage(`CodeGuardian results file not found: ${file}`);
      }
      return empty();
    }
    const data = JSON.parse(fs.readFileSync(file, 'utf8'));
    const artifact = currentArtifactState(data);
    return {
      suggestions: Array.isArray(data.suggestions) ? data.suggestions : [],
      dismissedIds: dismissedSuggestionIds(),
      artifact,
      applyAllowed: isApplyAllowedForArtifact(artifact),
      applyDisabledReason: applyDisabledReason(artifact),
      credentialStatus: getCredentialStatus(),
    };
  } catch (error) {
    vscode.window.showErrorMessage(`Failed to load CodeGuardian results: ${String(error)}`);
    return empty();
  }
}

function dismissedSuggestionIds(): string[] {
  return extensionContext?.workspaceState.get<string[]>(DISMISSED_KEY, []) || [];
}

async function dismissSuggestion(id: string): Promise<void> {
  const ids = new Set(dismissedSuggestionIds());
  ids.add(id);
  await extensionContext?.workspaceState.update(DISMISSED_KEY, Array.from(ids));
}

async function restoreDismissedSuggestion(id: string): Promise<void> {
  const ids = new Set(dismissedSuggestionIds());
  ids.delete(id);
  await extensionContext?.workspaceState.update(DISMISSED_KEY, Array.from(ids));
}

async function clearDismissedSuggestions(): Promise<void> {
  await extensionContext?.workspaceState.update(DISMISSED_KEY, []);
}

function currentArtifactState(data?: Record<string, unknown>): ArtifactState {
  const stored = extensionContext?.workspaceState.get<ArtifactState>(ARTIFACT_STATE_KEY);
  if (data) {
    const metadata = artifactMetadata(data);
    const effectiveData = metadata.headCommit || !stored?.commit
      ? data
      : { ...data, head_commit: stored.commit };
    const validation = validateArtifactContext(effectiveData, getLocalGitContext());
    return {
      status: stored?.status || 'downloaded',
      validation: validation.state,
      prId: metadata.prId,
      buildNumber: metadata.buildNumber,
      commit: metadata.headCommit || stored?.commit,
      localCommit: validation.localCommit,
      downloadedAt: stored?.downloadedAt || stringFromMetadata(data, ['generated_at', 'generatedAt', 'created_at']),
      message: validation.message,
    };
  }
  return stored || { status: 'missing', validation: 'unknown', message: 'no local results file' };
}

async function setArtifactState(state: ArtifactState): Promise<void> {
  await extensionContext?.workspaceState.update(ARTIFACT_STATE_KEY, state);
}

function stringFromMetadata(data: Record<string, unknown>, keys: string[]): string | undefined {
  for (const key of keys) {
    const value = valueFromPath(data, key);
    if (typeof value === 'string' || typeof value === 'number') {
      return String(value);
    }
  }
  return undefined;
}

function valueFromPath(data: Record<string, unknown>, pathKey: string): unknown {
  const parts = pathKey.split('.');
  let current: unknown = data;
  for (const part of parts) {
    if (!current || typeof current !== 'object' || !(part in current)) {
      return undefined;
    }
    current = (current as Record<string, unknown>)[part];
  }
  return current;
}

function artifactMetadata(data: Record<string, unknown>): {
  workspace?: string;
  repository?: string;
  prId?: string;
  headCommit?: string;
  branch?: string;
  buildNumber?: string;
} {
  const repository = stringFromMetadata(data, [
    'repository',
    'repo',
    'repo_slug',
    'repository.name',
    'repository.slug',
  ]);
  const workspace = stringFromMetadata(data, [
    'workspace',
    'workspace_slug',
    'repository.workspace',
    'repository.workspace.slug',
    'repository.workspace.name',
  ]);
  return {
    workspace,
    repository: repository?.includes('/') ? repository.split('/').pop() : repository,
    prId: stringFromMetadata(data, ['pull_request', 'pullRequest', 'pr_id', 'prId', 'pullrequest.id']),
    headCommit: stringFromMetadata(data, ['head_commit', 'headCommit', 'commit', 'source.commit.hash', 'pullrequest.source.commit.hash']),
    branch: stringFromMetadata(data, ['branch', 'source_branch', 'sourceBranch', 'source.branch.name', 'pullrequest.source.branch.name']),
    buildNumber: stringFromMetadata(data, ['build_number', 'buildNumber', 'run_id', 'runId', 'build']),
  };
}

function validateArtifactContext(
  data: Record<string, unknown>,
  local: LocalGitContext,
): { state: ArtifactValidationState; message: string; localCommit?: string } {
  const metadata = artifactMetadata(data);
  const details: string[] = [];

  if (local.warnings.length) {
    details.push(local.warnings.join(', '));
  }

  if (metadata.repository && local.repository && normalizeName(metadata.repository) !== normalizeName(local.repository)) {
    return {
      state: 'mismatch',
      message: `artifact repo ${metadata.repository} does not match local repo ${local.repository}`,
      localCommit: local.headCommit,
    };
  }
  if (metadata.workspace && local.workspace && normalizeName(metadata.workspace) !== normalizeName(local.workspace)) {
    return {
      state: 'mismatch',
      message: `artifact workspace ${metadata.workspace} does not match local workspace ${local.workspace}`,
      localCommit: local.headCommit,
    };
  }

  const selectedPr = extensionContext?.workspaceState.get<string>(SELECTED_PR_KEY);
  if (metadata.prId && selectedPr && metadata.prId !== selectedPr) {
    return {
      state: 'mismatch',
      message: `artifact PR ${metadata.prId} does not match selected PR ${selectedPr}`,
      localCommit: local.headCommit,
    };
  }

  if (metadata.headCommit && local.headCommit && !local.headCommit.startsWith(metadata.headCommit) && !metadata.headCommit.startsWith(local.headCommit)) {
    return {
      state: 'stale',
      message: `generated for ${shortHash(metadata.headCommit)}, local HEAD is ${shortHash(local.headCommit)}`,
      localCommit: local.headCommit,
    };
  }

  const hasEnoughMetadata = Boolean(metadata.headCommit && metadata.repository && local.headCommit && local.repository);
  if (!hasEnoughMetadata) {
    const missing = [
      metadata.headCommit ? '' : 'missing commit metadata',
      metadata.repository ? '' : 'missing repository metadata',
      local.headCommit ? '' : 'missing local HEAD',
      local.repository ? '' : 'missing local repo',
    ].filter(Boolean).join(', ');
    return {
      state: 'unknown',
      message: missing || details.join(', ') || 'not enough metadata to validate artifact',
      localCommit: local.headCommit,
    };
  }

  return {
    state: 'valid',
    message: `matches ${local.repository}@${shortHash(local.headCommit || '')}`,
    localCommit: local.headCommit,
  };
}

function normalizeName(value: string): string {
  return value.replace(/\\/g, '/').replace(/\.git$/, '').split('/').pop()?.toLowerCase() || value.toLowerCase();
}

function shortHash(value: string): string {
  return value ? value.slice(0, 7) : 'unknown';
}

function isApplyAllowedForArtifact(artifact: ArtifactState): boolean {
  if (artifact.validation === 'valid') {
    return true;
  }
  return artifact.validation === 'unknown' && configBoolean('allowApplyWithUnknownArtifact');
}

function applyDisabledReason(artifact: ArtifactState): string {
  if (isApplyAllowedForArtifact(artifact)) {
    return '';
  }
  if (artifact.validation === 'stale') {
    return `Apply disabled because the artifact is stale: ${artifact.message || 'commit mismatch'}.`;
  }
  if (artifact.validation === 'mismatch') {
    return `Apply disabled because the artifact does not match this workspace: ${artifact.message || 'context mismatch'}.`;
  }
  return `Apply disabled because artifact validation is UNKNOWN: ${artifact.message || 'missing metadata'}.`;
}

async function initializeCredentials(): Promise<void> {
  credentialCache = await getCodeGuardianCredentials();
  warnIfEnvFileIsNotIgnored();
}

async function getCodeGuardianCredentials(): Promise<CodeGuardianCredentials> {
  const fromSecrets = await credentialsFromSecretStorage();
  if (hasAnyCredential(fromSecrets)) {
    return { ...fromSecrets, source: 'secretStorage' };
  }

  const imported = await importCredentialsFromEnv(false);
  if (imported) {
    const importedSecrets = await credentialsFromSecretStorage();
    return { ...importedSecrets, source: 'env' };
  }

  const fromSettings = credentialsFromSettings();
  if (hasAnyCredential(fromSettings)) {
    void vscode.window.showWarningMessage('CodeGuardian is using credentials from settings. Import them into SecretStorage for safer storage.');
    return { ...fromSettings, source: 'settings' };
  }

  return { source: 'missing' };
}

async function credentialsFromSecretStorage(): Promise<Partial<CodeGuardianCredentials>> {
  if (!extensionContext) {
    return {};
  }
  return {
    jenkinsUser: await extensionContext.secrets.get(SECRET_KEYS.jenkinsUser),
    jenkinsApiToken: await extensionContext.secrets.get(SECRET_KEYS.jenkinsApiToken),
    bitbucketEmail: await extensionContext.secrets.get(SECRET_KEYS.bitbucketEmail),
    bitbucketApiToken: await extensionContext.secrets.get(SECRET_KEYS.bitbucketApiToken),
  };
}

function credentialsFromSettings(): Partial<CodeGuardianCredentials> {
  return {
    jenkinsUser: configValue('jenkinsUser'),
    jenkinsApiToken: configValue('jenkinsApiToken'),
    bitbucketEmail: configValue('bitbucketEmail'),
    bitbucketApiToken: configValue('bitbucketApiToken'),
  };
}

function hasAnyCredential(credentials: Partial<CodeGuardianCredentials>): boolean {
  return Boolean(credentials.jenkinsUser || credentials.jenkinsApiToken || credentials.bitbucketEmail || credentials.bitbucketApiToken);
}

async function importCredentialsFromEnv(showMessages: boolean): Promise<boolean> {
  if (!extensionContext) {
    return false;
  }
  const envFile = findWorkspaceEnvFile();
  if (!envFile) {
    if (showMessages) {
      vscode.window.showInformationMessage('No .env or .codeguardian.env file found in the workspace root.');
    }
    return false;
  }

  const values = parseEnvFile(envFile);
  const mappings: Array<[string, keyof typeof SECRET_KEYS]> = [
    ['CODEGUARDIAN_JENKINS_USER', 'jenkinsUser'],
    ['CODEGUARDIAN_JENKINS_API_TOKEN', 'jenkinsApiToken'],
    ['CODEGUARDIAN_BITBUCKET_EMAIL', 'bitbucketEmail'],
    ['CODEGUARDIAN_BITBUCKET_API_TOKEN', 'bitbucketApiToken'],
  ];
  let imported = false;
  for (const [envKey, secretKey] of mappings) {
    const value = values[envKey];
    if (value) {
      await extensionContext.secrets.store(SECRET_KEYS[secretKey], value);
      imported = true;
    }
  }
  if (imported) {
    warnIfEnvFileIsNotIgnored();
  }
  return imported;
}

async function clearStoredCredentials(): Promise<void> {
  if (!extensionContext) {
    return;
  }
  await Promise.all(Object.values(SECRET_KEYS).map((key) => extensionContext?.secrets.delete(key)));
}

function getCredentialStatus(): CredentialStatus {
  const configured = Boolean(
    credentialCache.jenkinsUser ||
    credentialCache.jenkinsApiToken ||
    credentialCache.bitbucketEmail ||
    credentialCache.bitbucketApiToken
  );
  if (!configured) {
    return { configured: false, source: 'missing', message: 'Credentials: missing' };
  }
  const sourceLabel = credentialCache.source === 'env' ? 'imported from .env' : credentialCache.source;
  return { configured: true, source: credentialCache.source, message: `Credentials: configured (${sourceLabel})` };
}

function findWorkspaceEnvFile(): string | undefined {
  const root = workspaceRoot().uri.fsPath;
  for (const name of ['.env', '.codeguardian.env']) {
    const candidate = path.join(root, name);
    if (fs.existsSync(candidate) && fs.statSync(candidate).isFile()) {
      return candidate;
    }
  }
  return undefined;
}

function parseEnvFile(filePath: string): Record<string, string> {
  const values: Record<string, string> = {};
  const content = fs.readFileSync(filePath, 'utf8');
  for (const rawLine of content.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith('#')) {
      continue;
    }
    const index = line.indexOf('=');
    if (index <= 0) {
      continue;
    }
    const key = line.slice(0, index).trim();
    let value = line.slice(index + 1).trim();
    if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
      value = value.slice(1, -1);
    }
    values[key] = value;
  }
  return values;
}

function warnIfEnvFileIsNotIgnored(): void {
  const envFile = findWorkspaceEnvFile();
  if (!envFile || isEnvFileIgnored(path.basename(envFile))) {
    return;
  }
  vscode.window.showWarningMessage(`${path.basename(envFile)} exists but is not excluded by .gitignore. Do not commit CodeGuardian credentials.`);
}

function isEnvFileIgnored(fileName: string): boolean {
  const gitignore = path.join(workspaceRoot().uri.fsPath, '.gitignore');
  if (!fs.existsSync(gitignore)) {
    return false;
  }
  const lines = fs.readFileSync(gitignore, 'utf8')
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line && !line.startsWith('#'));
  return lines.includes(fileName) || lines.includes('*.env') || (fileName === '.env' && lines.includes('.env'));
}

function findSuggestionById(id: string): Suggestion | undefined {
  return loadSuggestions().find((suggestion) => suggestion.id === id);
}

async function downloadLatestResults(): Promise<void> {
  const directUrl = configValue('jenkinsArtifactUrl').trim();
  if (directUrl) {
    await downloadResultsFromUrl(directUrl);
    return;
  }
  const ready = await resolveReadyArtifact(true);
  if (!ready) {
    throw new Error('Jenkins build is not ready yet or codeguardian-results.json was not archived.');
  }
  await downloadResultsFromUrl(ready.artifactUrl, { commit: ready.commit, buildNumber: ready.buildNumber });
}

async function tryDownloadReadyArtifact(): Promise<string | undefined> {
  const directUrl = configValue('jenkinsArtifactUrl').trim();
  if (directUrl) {
    await downloadResultsFromUrl(directUrl);
    return directUrl;
  }

  const ready = await resolveReadyArtifact(false);
  if (!ready) {
    return undefined;
  }
  await downloadResultsFromUrl(ready.artifactUrl, { commit: ready.commit, buildNumber: ready.buildNumber });
  return ready.buildKey;
}

async function downloadResultsFromUrl(url: string, metadata: Partial<ArtifactState> = {}): Promise<void> {
  const body = await downloadText(url, jenkinsAuth());
  let parsed: Record<string, unknown>;

  try {
    parsed = JSON.parse(body);
  } catch (error) {
    await setArtifactState({ status: 'error', validation: 'unknown', downloadedAt: new Date().toISOString(), message: `invalid JSON from ${url}` });
    throw new Error(`Downloaded Jenkins artifact is not valid JSON: ${String(error)}`);
  }

  fs.writeFileSync(resultsPath(), body, 'utf8');
  const effectiveParsed = artifactMetadata(parsed).headCommit || !metadata.commit
    ? parsed
    : { ...parsed, head_commit: metadata.commit };
  await setArtifactState({
    status: 'downloaded',
    validation: validateArtifactContext(effectiveParsed, getLocalGitContext()).state,
    prId: stringFromMetadata(parsed, ['pull_request', 'pullRequest', 'pr_id', 'prId']),
    buildNumber: stringFromMetadata(parsed, ['build_number', 'buildNumber', 'build']) || metadata.buildNumber || 'latest successful build',
    commit: artifactMetadata(parsed).headCommit || metadata.commit,
    localCommit: currentGitHeadSync(),
    downloadedAt: new Date().toISOString(),
    message: 'downloaded from Jenkins artifact',
  });
  vscode.window.showInformationMessage(`Downloaded CodeGuardian results to ${resultsPath()}`);
}

async function selectPullRequestAndDownload(): Promise<void> {
  const jobs = await listPullRequestJobs();
  if (!jobs.length) {
    vscode.window.showInformationMessage('No Jenkins PR jobs found.');
    return;
  }

  const selected = await pickPullRequestJob(jobs, 'Choose the PR artifact to download');
  if (!selected) {
    return;
  }

  const match = selected.name.match(/PR-(\d+)/i);
  if (match) {
    await extensionContext?.workspaceState.update(SELECTED_PR_KEY, match[1]);
  }
  const ready = selected.url ? await readyArtifactForJob(selected.url, true) : undefined;
  if (ready) {
    await downloadResultsFromUrl(ready.artifactUrl, { commit: ready.commit, buildNumber: ready.buildNumber });
    return;
  }
  const url = buildArtifactUrlForJobCandidate(selected);
  await downloadResultsFromUrl(url);
}

function buildJenkinsArtifactUrl(): string {
  const directUrl = configValue('jenkinsArtifactUrl').trim();
  if (directUrl) {
    return directUrl;
  }

  const baseUrl = configValue('jenkinsUrl').trim().replace(/\/+$/, '');
  const jobPath = configuredJobPath();
  const buildSelector = configValue('jenkinsBuildSelector').trim() || 'lastSuccessfulBuild';
  const artifactName = configValue('jenkinsArtifactName').trim() || 'codeguardian-results.json';
  if (!baseUrl || !jobPath) {
    throw new Error(
      'Configure codeguardian.jenkinsArtifactUrl, or configure both codeguardian.jenkinsUrl and codeguardian.jenkinsJobPath.'
    );
  }

  return buildArtifactUrlForJob(jobPathParts(jobPath), buildSelector, artifactName);
}

async function resolveReadyArtifact(allowPrompt: boolean): Promise<{ artifactUrl: string; buildKey: string; buildNumber?: string; commit?: string } | undefined> {
  const jobUrl = await resolveCurrentBranchJobUrl(allowPrompt);
  if (!jobUrl) {
    return undefined;
  }
  return readyArtifactForJob(jobUrl, allowPrompt);
}

async function resolveCurrentBranchJobUrl(allowPrompt: boolean): Promise<string | undefined> {
  const bitbucketPr = await findCurrentBranchPullRequest();
  if (bitbucketPr) {
    await extensionContext?.workspaceState.update(SELECTED_PR_KEY, String(bitbucketPr.id));
    const job = await findJenkinsJobForPrId(bitbucketPr.id);
    return job ? jobUrlFromCandidate(job) : jobUrlForPrId(bitbucketPr.id);
  }

  const candidates = await listPullRequestJobs();
  if (!candidates.length) {
    throw new Error('No Jenkins PR jobs found for the current repository.');
  }

  const branch = await currentGitBranch();
  const matching = [];
  for (const candidate of candidates) {
    if (await jobMentionsBranch(candidate, branch)) {
      matching.push(candidate);
    }
  }

  if (matching.length === 1) {
    return jobUrlFromCandidate(matching[0]);
  }
  if (candidates.length === 1) {
    return jobUrlFromCandidate(candidates[0]);
  }
  if (!allowPrompt) {
    return undefined;
  }

  const selected = await pickPullRequestJob(
    matching.length ? matching : candidates,
    matching.length
      ? `Several Jenkins PR jobs mention branch ${branch}. Choose one.`
      : `No Jenkins PR job metadata matched branch ${branch}. Choose one.`
  );
  if (!selected) {
    throw new Error('No pull request selected.');
  }
  return jobUrlFromCandidate(selected);
}

async function listPullRequestJobs(): Promise<JenkinsJobCandidate[]> {
  const baseUrl = configValue('jenkinsUrl').trim().replace(/\/+$/, '');
  if (!baseUrl) {
    throw new Error('Configure codeguardian.jenkinsUrl before selecting a pull request.');
  }

  const configuredParts = baseJobParts();
  const candidates: JenkinsJobCandidate[] = [];
  if (configuredParts.length) {
    candidates.push(...await listPullRequestJobsAtPath(configuredParts, 'configured'));
  }

  candidates.push(...await listRootPullRequestJobs());
  const repoSlug = await currentRepositorySlug();
  const unique = new Map<string, JenkinsJobCandidate>();
  for (const candidate of candidates) {
    const key = candidate.url || candidate.name;
    if (!unique.has(key) && jobLooksRelevantToRepo(candidate, repoSlug)) {
      unique.set(key, candidate);
    }
  }
  return Array.from(unique.values()).sort((a, b) => prNumber(a.name) - prNumber(b.name));
}

async function findCurrentBranchPullRequest(): Promise<BitbucketPullRequest | undefined> {
  const auth = bitbucketAuth();
  if (!auth) {
    return undefined;
  }

  const repo = await currentRepositoryInfo();
  const branch = await currentGitBranch();
  const prs = await listOpenBitbucketPullRequests(repo, auth);
  const matches = prs.filter((pr) => pr.source?.branch?.name === branch);
  if (matches.length === 1) {
    return matches[0];
  }
  if (matches.length > 1) {
    const selected = await vscode.window.showQuickPick(
      matches.map((pr) => ({
        label: `PR-${pr.id}`,
        description: pr.title,
        detail: `${pr.source?.branch?.name || ''} -> ${pr.destination?.branch?.name || ''}`,
        pr,
      })),
      {
        title: 'Select Bitbucket pull request',
        placeHolder: `Several open PRs use branch ${branch}`,
      }
    );
    return selected?.pr;
  }
  return undefined;
}

async function listOpenBitbucketPullRequests(
  repo: RepositoryInfo,
  auth: JenkinsAuth,
): Promise<BitbucketPullRequest[]> {
  const values: BitbucketPullRequest[] = [];
  let page = 1;
  for (;;) {
    const url = `https://api.bitbucket.org/2.0/repositories/${encodeURIComponent(repo.workspace)}/${encodeURIComponent(repo.repo)}/pullrequests?state=OPEN&page=${page}&pagelen=50`;
    const data = JSON.parse(await downloadText(url, auth)) as { values?: BitbucketPullRequest[]; next?: string };
    values.push(...(data.values || []));
    if (!data.next || !data.values?.length) {
      break;
    }
    page += 1;
  }
  return values;
}

function buildArtifactUrlForPrId(prId: number): string {
  const baseParts = baseJobParts();
  if (baseParts.length) {
    return buildArtifactUrlForJob([...baseParts, `PR-${prId}`]);
  }

  return buildArtifactUrlForJob([`PR-${prId}`]);
}

async function findJenkinsJobForPrId(prId: number): Promise<JenkinsJobCandidate | undefined> {
  const jobs = await listPullRequestJobs();
  const exact = jobs.find((job) => job.name.toUpperCase() === `PR-${prId}`.toUpperCase());
  if (exact) {
    return exact;
  }
  return jobs.find((job) => new RegExp(`(^|[^0-9])PR-${prId}([^0-9]|$)`, 'i').test(job.name));
}

async function listPullRequestJobsAtPath(parts: string[], source: 'configured' | 'root'): Promise<JenkinsJobCandidate[]> {
  const baseUrl = configValue('jenkinsUrl').trim().replace(/\/+$/, '');
  const tree = encodeURIComponent('jobs[name,url,color]');
  const url = `${baseUrl}/${jenkinsJobUrlPath(parts)}/api/json?tree=${tree}`;
  const data = await downloadJson(url);
  const jobs = Array.isArray(data.jobs) ? data.jobs as JenkinsJob[] : [];
  return jobs
    .filter((job) => /^PR-\d+$/i.test(job.name))
    .map((job) => ({ ...job, source }));
}

async function listRootPullRequestJobs(): Promise<JenkinsJobCandidate[]> {
  const baseUrl = configValue('jenkinsUrl').trim().replace(/\/+$/, '');
  const tree = encodeURIComponent('jobs[name,url,color]');
  const data = await downloadJson(`${baseUrl}/api/json?tree=${tree}`);
  const jobs = Array.isArray(data.jobs) ? data.jobs as JenkinsJob[] : [];
  return jobs
    .filter((job) => /PR-\d+/i.test(job.name))
    .map((job) => ({ ...job, source: 'root' as const }));
}

async function pickPullRequestJob(jobs: JenkinsJobCandidate[], placeHolder: string): Promise<JenkinsJobCandidate | undefined> {
  const selected = await vscode.window.showQuickPick(
    jobs.map((job) => ({
      label: job.name,
      description: job.color ? `status: ${job.color}` : undefined,
      detail: job.url,
      job,
    })),
    {
      title: 'Select CodeGuardian pull request',
      placeHolder,
    }
  );
  return selected?.job;
}

async function jobMentionsBranch(job: JenkinsJobCandidate, branch: string): Promise<boolean> {
  if (!job.url || !branch) {
    return false;
  }
  try {
    const tree = encodeURIComponent('name,displayName,fullDisplayName,description,url,actions[*],lastBuild[actions[*],url]');
    const data = await downloadJson(`${job.url.replace(/\/+$/, '')}/api/json?tree=${tree}`);
    const serialized = JSON.stringify(data).toLowerCase();
    return serialized.includes(branch.toLowerCase()) || serialized.includes(encodeURIComponent(branch).toLowerCase());
  } catch {
    return false;
  }
}

function jobLooksRelevantToRepo(job: JenkinsJobCandidate, repoSlug: string): boolean {
  if (job.source === 'configured') {
    return true;
  }
  const normalizedRepo = repoSlug.toLowerCase();
  return job.name.toLowerCase().includes(normalizedRepo) || (job.url || '').toLowerCase().includes(normalizedRepo);
}

function baseJobParts(): string[] {
  const parts = jobPathParts(configuredJobPath());
  if (parts.length && /^PR-\d+$/i.test(parts[parts.length - 1])) {
    return parts.slice(0, -1);
  }
  return parts;
}

function configuredJobPath(): string {
  return configValue('jenkinsJobPath').trim();
}

function jobPathParts(jobPath: string): string[] {
  return jobPath.split('/').filter((part) => part.length > 0);
}

function buildArtifactUrlForJob(
  parts: string[],
  buildSelector = configValue('jenkinsBuildSelector').trim() || 'lastSuccessfulBuild',
  artifactName = configValue('jenkinsArtifactName').trim() || 'codeguardian-results.json',
): string {
  const baseUrl = configValue('jenkinsUrl').trim().replace(/\/+$/, '');
  if (!baseUrl || !parts.length) {
    throw new Error('Configure codeguardian.jenkinsUrl and codeguardian.jenkinsJobPath first.');
  }
  return `${baseUrl}/${jenkinsJobUrlPath(parts)}/${encodeURIComponent(buildSelector)}/artifact/${encodeURIComponent(artifactName)}`;
}

function buildArtifactUrlForJobCandidate(
  job: JenkinsJobCandidate,
  buildSelector = configValue('jenkinsBuildSelector').trim() || 'lastSuccessfulBuild',
  artifactName = configValue('jenkinsArtifactName').trim() || 'codeguardian-results.json',
): string {
  if (job.url) {
    return `${job.url.replace(/\/+$/, '')}/${encodeURIComponent(buildSelector)}/artifact/${encodeURIComponent(artifactName)}`;
  }
  if (job.source === 'configured') {
    return buildArtifactUrlForJob([...baseJobParts(), job.name], buildSelector, artifactName);
  }
  return buildArtifactUrlForJob([job.name], buildSelector, artifactName);
}

function jobUrlFromCandidate(job: JenkinsJobCandidate): string {
  if (job.url) {
    return job.url.replace(/\/+$/, '');
  }
  if (job.source === 'configured') {
    return buildJobUrl([...baseJobParts(), job.name]);
  }
  return buildJobUrl([job.name]);
}

function jobUrlForPrId(prId: number): string {
  const baseParts = baseJobParts();
  return buildJobUrl(baseParts.length ? [...baseParts, `PR-${prId}`] : [`PR-${prId}`]);
}

function buildJobUrl(parts: string[]): string {
  const baseUrl = configValue('jenkinsUrl').trim().replace(/\/+$/, '');
  if (!baseUrl || !parts.length) {
    throw new Error('Configure codeguardian.jenkinsUrl and codeguardian.jenkinsJobPath first.');
  }
  return `${baseUrl}/${jenkinsJobUrlPath(parts)}`;
}

async function readyArtifactForJob(
  jobUrl: string,
  throwWhenNotReady: boolean,
): Promise<{ artifactUrl: string; buildKey: string; buildNumber?: string; commit?: string } | undefined> {
  const buildSelector = configValue('jenkinsBuildSelector').trim() || 'lastBuild';
  const artifactName = configValue('jenkinsArtifactName').trim() || 'codeguardian-results.json';
  const apiUrl = `${jobUrl.replace(/\/+$/, '')}/${encodeURIComponent(buildSelector)}/api/json?tree=building,result,number,url,artifacts[fileName,relativePath],actions[lastBuiltRevision[SHA1],buildsByBranchName[*],parameters[name,value]]`;
  const build = await downloadJson(apiUrl) as JenkinsBuild;
  if (build.building) {
    if (throwWhenNotReady) {
      throw new Error('Jenkins build is still running.');
    }
    return undefined;
  }
  if (build.result !== 'SUCCESS') {
    if (throwWhenNotReady) {
      throw new Error(`Jenkins build is not successful yet: ${build.result || 'unknown'}.`);
    }
    return undefined;
  }

  const artifact = (build.artifacts || []).find((item) => item.fileName === artifactName || item.relativePath === artifactName);
  if (!artifact?.relativePath) {
    if (throwWhenNotReady) {
      throw new Error(`Jenkins build finished but ${artifactName} was not archived.`);
    }
    return undefined;
  }

  const buildUrl = (build.url || `${jobUrl.replace(/\/+$/, '')}/${build.number}`).replace(/\/+$/, '');
  return {
    artifactUrl: `${buildUrl}/artifact/${artifact.relativePath.split('/').map(encodeURIComponent).join('/')}`,
    buildKey: `${jobUrl}#${build.number || buildSelector}`,
    buildNumber: build.number ? String(build.number) : buildSelector,
    commit: extractBuildCommit(build),
  };
}

function extractBuildCommit(build: JenkinsBuild): string | undefined {
  for (const action of build.actions || []) {
    const direct = valueFromPath(action, 'lastBuiltRevision.SHA1');
    if (typeof direct === 'string' && direct) {
      return direct;
    }
    const parameters = action.parameters;
    if (Array.isArray(parameters)) {
      for (const parameter of parameters) {
        const item = parameter as Record<string, unknown>;
        if (item.name === 'GIT_COMMIT' || item.name === 'BITBUCKET_COMMIT') {
          return typeof item.value === 'string' ? item.value : undefined;
        }
      }
    }
    const branches = action.buildsByBranchName;
    if (branches && typeof branches === 'object') {
      for (const value of Object.values(branches as Record<string, unknown>)) {
        const sha = value && typeof value === 'object' ? (value as Record<string, unknown>).SHA1 : undefined;
        if (typeof sha === 'string' && sha) {
          return sha;
        }
      }
    }
  }
  return undefined;
}

function jenkinsJobUrlPath(parts: string[]): string {
  return parts.map((part) => `job/${encodeURIComponent(part)}`).join('/');
}

function prNumber(name: string): number {
  const match = name.match(/\d+/);
  return match ? Number(match[0]) : Number.MAX_SAFE_INTEGER;
}

function jenkinsAuth(): JenkinsAuth | undefined {
  const user = credentialCache.jenkinsUser || configValue('jenkinsUser');
  const token = credentialCache.jenkinsApiToken || configValue('jenkinsApiToken');
  return user && token ? { user, token } : undefined;
}

function bitbucketAuth(): JenkinsAuth | undefined {
  const user = credentialCache.bitbucketEmail || configValue('bitbucketEmail');
  const token = credentialCache.bitbucketApiToken || configValue('bitbucketApiToken');
  return user && token ? { user, token } : undefined;
}

async function downloadJson(url: string): Promise<Record<string, unknown>> {
  return JSON.parse(await downloadText(url, jenkinsAuth())) as Record<string, unknown>;
}

function downloadText(url: string, auth?: JenkinsAuth, redirects = 0): Promise<string> {
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

function renderDashboardHtml(webview: vscode.Webview, extensionUri: vscode.Uri, data: ResultsData): string {
  const nonce = String(Date.now());
  const activeFile = currentWorkspaceFile();
  const payload = JSON.stringify({
    suggestions: data.suggestions,
    dismissedIds: data.dismissedIds,
    artifact: data.artifact,
    applyAllowed: data.applyAllowed,
    applyDisabledReason: data.applyDisabledReason,
    credentialStatus: data.credentialStatus,
    activeFile,
  }).replace(/</g, '\\u003c');
  return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src ${webview.cspSource} 'unsafe-inline'; script-src 'nonce-${nonce}';">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>CodeGuardian Review</title>
  <style>
    :root {
      color-scheme: dark light;
    }
    body {
      padding: 0;
      margin: 0;
      color: var(--vscode-foreground);
      background: var(--vscode-sideBar-background);
      font: var(--vscode-font-size) var(--vscode-font-family);
    }
    button, input, select {
      font: inherit;
    }
    .shell {
      display: flex;
      flex-direction: column;
      gap: 10px;
      padding: 10px;
    }
    .toolbar {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 6px;
    }
    .wide-action {
      grid-column: 1 / -1;
    }
    .banner {
      border: 1px solid var(--vscode-panel-border);
      border-radius: 4px;
      padding: 7px;
      background: var(--vscode-editorWidget-background);
      color: var(--vscode-descriptionForeground);
      line-height: 1.35;
    }
    .banner strong {
      color: var(--vscode-foreground);
    }
    .button {
      border: 1px solid var(--vscode-button-border, transparent);
      color: var(--vscode-button-foreground);
      background: var(--vscode-button-background);
      border-radius: 3px;
      padding: 5px 8px;
      cursor: pointer;
      text-align: center;
    }
    .button:hover {
      background: var(--vscode-button-hoverBackground);
    }
    .button.secondary {
      color: var(--vscode-button-secondaryForeground);
      background: var(--vscode-button-secondaryBackground);
    }
    .button.secondary:hover {
      background: var(--vscode-button-secondaryHoverBackground);
    }
    .summary {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 6px;
    }
    .metric {
      border: 1px solid var(--vscode-sideBarSectionHeader-border, var(--vscode-panel-border));
      border-radius: 4px;
      padding: 7px;
      background: var(--vscode-editorWidget-background);
    }
    .metric-value {
      font-weight: 700;
      font-size: 18px;
      line-height: 22px;
    }
    .metric-label {
      color: var(--vscode-descriptionForeground);
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: .04em;
    }
    .filters {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 6px;
    }
    .filters input,
    .filters select {
      min-width: 0;
      border: 1px solid var(--vscode-input-border, transparent);
      color: var(--vscode-input-foreground);
      background: var(--vscode-input-background);
      border-radius: 3px;
      padding: 4px 6px;
    }
    .filters .wide {
      grid-column: 1 / -1;
    }
    label.check {
      display: flex;
      align-items: center;
      gap: 6px;
      color: var(--vscode-descriptionForeground);
      font-size: 12px;
      grid-column: 1 / -1;
    }
    .file {
      margin-top: 8px;
      border-top: 1px solid var(--vscode-sideBarSectionHeader-border, var(--vscode-panel-border));
      padding-top: 8px;
    }
    .section-title {
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-top: 12px;
      padding: 5px 0;
      border-top: 1px solid var(--vscode-panel-border);
      color: var(--vscode-sideBarTitle-foreground);
      font-weight: 700;
      text-transform: uppercase;
      font-size: 11px;
      letter-spacing: .05em;
    }
    .file-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      margin-bottom: 4px;
      color: var(--vscode-sideBarTitle-foreground);
      font-weight: 600;
      word-break: break-all;
    }
    .count {
      flex: 0 0 auto;
      min-width: 18px;
      color: var(--vscode-badge-foreground);
      background: var(--vscode-badge-background);
      border-radius: 10px;
      padding: 1px 6px;
      text-align: center;
      font-size: 11px;
    }
    .suggestion {
      display: grid;
      grid-template-columns: 16px 18px 1fr auto;
      gap: 3px;
      align-items: center;
      border-radius: 4px;
      padding: 6px;
      cursor: pointer;
    }
    .suggestion:hover,
    .suggestion.selected {
      background: var(--vscode-list-hoverBackground);
    }
    .suggestion.selected {
      outline: 1px solid var(--vscode-focusBorder);
    }
    .meta {
      display: flex;
      align-items: center;
      gap: 5px;
      flex-wrap: wrap;
      color: var(--vscode-descriptionForeground);
      font-size: 11px;
    }
    .title {
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .selectbox {
      display: flex;
      align-items: center;
      justify-content: center;
    }
    .selectbox input {
      margin: 0;
    }
    .chevron {
      color: var(--vscode-descriptionForeground);
      font-size: 12px;
      line-height: 1;
      text-align: center;
      user-select: none;
    }
    .status {
      font-weight: 700;
      font-size: 10px;
      padding: 1px 5px;
      border-radius: 3px;
      background: var(--vscode-badge-background);
      color: var(--vscode-badge-foreground);
    }
    .status.applied {
      color: var(--vscode-testing-iconPassed);
    }
    .status.changed {
      color: var(--vscode-testing-iconFailed);
    }
    .meta {
      grid-column: 3 / 5;
    }
    .pill {
      border-radius: 3px;
      padding: 1px 5px;
      background: var(--vscode-badge-background);
      color: var(--vscode-badge-foreground);
      text-transform: uppercase;
      font-size: 10px;
      letter-spacing: .03em;
    }
    .pill.critical,
    .pill.blocker {
      background: var(--vscode-inputValidation-errorBackground);
      color: var(--vscode-inputValidation-errorForeground);
    }
    .pill.major,
    .pill.high {
      background: var(--vscode-inputValidation-warningBackground);
      color: var(--vscode-inputValidation-warningForeground);
    }
    .pill.optimization {
      background: var(--vscode-charts-blue);
      color: var(--vscode-editor-background);
    }
    .detail {
      margin: 6px 0 8px 0;
      border-left: 2px solid var(--vscode-focusBorder);
      padding: 8px 0 0 8px;
      background: var(--vscode-sideBar-background);
    }
    .detail h3 {
      margin: 0 0 6px;
      font-size: 13px;
    }
    .detail p {
      margin: 6px 0;
      line-height: 1.35;
    }
    pre {
      margin: 6px 0;
      padding: 8px;
      overflow: auto;
      border-radius: 4px;
      background: var(--vscode-textCodeBlock-background);
      font-family: var(--vscode-editor-font-family);
      font-size: var(--vscode-editor-font-size);
    }
    .detail-actions {
      display: grid;
      grid-template-columns: repeat(7, minmax(0, 1fr));
      gap: 6px;
      margin-top: 8px;
    }
    .button:disabled {
      opacity: .45;
      cursor: not-allowed;
    }
    .selection-summary {
      color: var(--vscode-descriptionForeground);
      font-size: 12px;
      line-height: 1.3;
    }
    .empty {
      padding: 16px 8px;
      color: var(--vscode-descriptionForeground);
      text-align: center;
      line-height: 1.4;
    }
  </style>
</head>
<body>
  <div class="shell">
    <div class="toolbar">
      <button class="button" id="download">Download Artifact</button>
      <button class="button secondary" id="refresh">Refresh Suggestions</button>
      <button class="button secondary wide-action" id="selectPr">Select Open PR</button>
      <button class="button wide-action" id="applySelected" disabled>Apply Selected</button>
    </div>
    <div class="banner" id="artifactBanner"></div>
    <div class="selection-summary" id="selectionSummary"></div>
    <div class="summary" id="summary"></div>
    <div class="filters">
      <input class="wide" id="search" type="search" placeholder="Search file, function or text">
      <select id="severity"></select>
      <select id="source"></select>
      <select id="limit">
        <option value="0">Show all</option>
        <option value="3">Focus top 3</option>
        <option value="5">Focus top 5</option>
        <option value="6">Focus top 6</option>
      </select>
      <select id="status"></select>
      <label class="check"><input id="currentFile" type="checkbox"> Current file only</label>
      <label class="check"><input id="showDismissed" type="checkbox"> Show dismissed</label>
      <button class="button secondary wide-action" id="clearDismissed">Clear dismissed</button>
    </div>
    <div id="list"></div>
  </div>
  <script nonce="${nonce}">
    const vscode = acquireVsCodeApi();
    const state = ${payload};
    const filters = {
      search: '',
      severity: 'all',
      source: 'all',
      status: 'all',
      limit: 0,
      currentFile: false,
      showDismissed: false,
      expandedId: '',
      statuses: {},
      selectedIds: new Set()
    };
    const dismissedIds = new Set(state.dismissedIds || []);

    const byId = (id) => document.getElementById(id);
    const norm = (value) => String(value || '').toLowerCase();
    const titleOf = (item) => item.target_name || item.problem || item.solution || item.id;
    const severityOf = (item) => item.severity || item.source || 'info';

    byId('download').addEventListener('click', () => vscode.postMessage({ command: 'download' }));
    byId('refresh').addEventListener('click', () => vscode.postMessage({ command: 'refresh' }));
    byId('selectPr').addEventListener('click', () => vscode.postMessage({ command: 'selectPr' }));
    byId('applySelected').addEventListener('click', () => {
      const openIds = Array.from(filters.selectedIds).filter((id) => statusOf(id) === 'open' && !dismissedIds.has(id));
      vscode.postMessage({ command: 'applySelected', ids: openIds });
    });
    byId('clearDismissed').addEventListener('click', () => vscode.postMessage({ command: 'clearDismissed' }));
    window.addEventListener('message', (event) => {
      const message = event.data;
      if (message?.command === 'statuses' && message.statuses) {
        filters.statuses = message.statuses;
        render();
      }
    });
    for (const id of ['search', 'severity', 'source', 'status', 'limit', 'currentFile', 'showDismissed']) {
      byId(id).addEventListener('input', () => {
        filters.search = byId('search').value;
        filters.severity = byId('severity').value;
        filters.source = byId('source').value;
        filters.status = byId('status').value;
        filters.limit = Number(byId('limit').value || 0);
        filters.currentFile = byId('currentFile').checked;
        filters.showDismissed = byId('showDismissed').checked;
        render();
      });
    }

    function uniqueValues(key, fallback) {
      const values = Array.from(new Set(state.suggestions.map((item) => item[key]).filter(Boolean)));
      return ['all', ...values.sort((a, b) => String(a).localeCompare(String(b)))].map((value) => ({
        value,
        label: value === 'all' ? fallback : String(value)
      }));
    }

    function setupOptions() {
      fillSelect('severity', uniqueValues('severity', 'All severities'));
      fillSelect('source', uniqueValues('source', 'All sources'));
      fillSelect('status', [
        { value: 'all', label: 'All statuses' },
        { value: 'open', label: 'OPEN' },
        { value: 'applied', label: 'APPLIED' },
        { value: 'changed', label: 'CHANGED' },
      ]);
    }

    function fillSelect(id, options) {
      byId(id).innerHTML = options.map((option) => '<option value="' + escapeHtml(option.value) + '">' + escapeHtml(option.label) + '</option>').join('');
    }

    function filteredSuggestions() {
      const search = norm(filters.search);
      let items = state.suggestions.filter((item) => {
        const itemStatus = statusOf(item.id, item);
        if (!filters.showDismissed && dismissedIds.has(item.id)) return false;
        if (filters.severity !== 'all' && item.severity !== filters.severity) return false;
        if (filters.source !== 'all' && item.source !== filters.source) return false;
        if (filters.status !== 'all' && itemStatus !== filters.status) return false;
        if (filters.currentFile && state.activeFile && item.file !== state.activeFile) return false;
        if (!search) return true;
        return norm(item.file).includes(search) || norm(item.target_name).includes(search) || norm(item.problem).includes(search) || norm(item.solution).includes(search);
      });
      items = items.sort((a, b) => severityRank(b) - severityRank(a) || String(a.file).localeCompare(String(b.file)) || Number(a.line || 0) - Number(b.line || 0));
      return filters.limit > 0 ? items.slice(0, filters.limit) : items;
    }

    function severityRank(item) {
      const value = norm(item.severity || item.source);
      if (value.includes('blocker')) return 5;
      if (value.includes('critical')) return 4;
      if (value.includes('high') || value.includes('major')) return 3;
      if (value.includes('medium')) return 2;
      if (value.includes('minor') || value.includes('low')) return 1;
      return 0;
    }

    function render() {
      renderArtifactBanner();
      renderSelectionSummary();
      renderSummary();
      const items = filteredSuggestions();
      if (filters.expandedId && !items.find((item) => item.id === filters.expandedId)) {
        filters.expandedId = '';
      }
      renderList(items);
    }

    function renderArtifactBanner() {
      const artifact = state.artifact || {};
      const status = artifact.status || 'unknown';
      const downloadedAt = artifact.downloadedAt ? new Date(artifact.downloadedAt).toLocaleString() : 'not downloaded';
      const pr = artifact.prId || 'unknown PR';
      const build = artifact.buildNumber || 'latest successful build';
      const message = artifact.message ? ' | ' + artifact.message : '';
      const credentials = state.credentialStatus?.message || 'Credentials: missing';
      byId('artifactBanner').innerHTML =
        '<strong>Artifact:</strong> ' + escapeHtml((artifact.validation || status).toUpperCase()) +
        ' | <strong>PR:</strong> ' + escapeHtml(pr) +
        ' | <strong>Build:</strong> ' + escapeHtml(build) +
        ' | <strong>Commit:</strong> ' + escapeHtml((artifact.commit || artifact.localCommit || 'unknown').slice(0, 7)) +
        ' | ' + escapeHtml(credentials) +
        ' | <strong>Last download:</strong> ' + escapeHtml(downloadedAt) +
        escapeHtml(message);
    }

    function renderSelectionSummary() {
      const selected = Array.from(filters.selectedIds);
      const open = selected.filter((id) => statusOf(id) === 'open' && !dismissedIds.has(id));
      byId('selectionSummary').textContent = selected.length ? selected.length + ' selected / ' + open.length + ' applicable' : 'No suggestions selected';
      byId('applySelected').disabled = open.length === 0 || !state.applyAllowed;
      byId('applySelected').title = state.applyAllowed ? '' : state.applyDisabledReason;
    }

    function renderSummary() {
      const total = state.suggestions.length;
      const critical = state.suggestions.filter((item) => ['blocker', 'critical'].includes(norm(item.severity))).length;
      const optimization = state.suggestions.filter((item) => norm(item.source) === 'optimization').length;
      const visible = filteredSuggestions().length;
      const dismissed = dismissedIds.size;
      byId('summary').innerHTML = [
        metric(total, 'Total'),
        metric(critical, 'Critical'),
        metric(optimization, 'Optimization'),
        metric(visible, 'Visible'),
        metric(dismissed, 'Dismissed')
      ].join('');
    }

    function metric(value, label) {
      return '<div class="metric"><div class="metric-value">' + value + '</div><div class="metric-label">' + label + '</div></div>';
    }

    function renderList(items) {
      if (!state.suggestions.length) {
        byId('list').innerHTML = '<div class="empty">No local results found. Use Download Artifact or configure codeguardian.resultsFile.</div>';
        return;
      }
      if (!items.length) {
        byId('list').innerHTML = '<div class="empty">No suggestions match the current filters.</div>';
        return;
      }
      const issueItems = items.filter((item) => norm(item.source) !== 'optimization');
      const optimizationItems = items.filter((item) => norm(item.source) === 'optimization');
      byId('list').innerHTML = [
        sectionBlock('Issues', issueItems, items),
        sectionBlock('Optimizations', optimizationItems, items)
      ].filter(Boolean).join('');
      for (const item of items) {
        const row = byId('suggestion-' + item.id);
        if (row) {
          row.addEventListener('click', () => {
            filters.expandedId = filters.expandedId === item.id ? '' : item.id;
            render();
          });
          row.addEventListener('dblclick', () => vscode.postMessage({ command: 'open', id: item.id }));
        }
        const checkbox = byId('select-' + item.id);
        if (checkbox) {
          checkbox.addEventListener('click', (event) => event.stopPropagation());
          checkbox.addEventListener('change', () => {
            if (checkbox.checked) {
              filters.selectedIds.add(item.id);
            } else {
              filters.selectedIds.delete(item.id);
            }
            renderSelectionSummary();
          });
        }
      }
      wireDetail(items);
    }

    function sectionBlock(title, sectionItems, allItems) {
      if (!sectionItems.length) return '';
      const groups = new Map();
      for (const item of sectionItems) {
        if (!groups.has(item.file)) groups.set(item.file, []);
        groups.get(item.file).push(item);
      }
      return '<div class="section-title"><span>' + escapeHtml(title) + '</span><span class="count">' + sectionItems.length + '</span></div>' +
        Array.from(groups.entries()).map(([file, values]) => {
          return '<section class="file"><div class="file-header"><span>' + escapeHtml(file) + '</span><span class="count">' + values.length + '</span></div>' +
            values.map((item) => suggestionBlock(item, allItems)).join('') + '</section>';
        }).join('');
    }

    function suggestionBlock(item, items) {
      return suggestionRow(item) + (item.id === filters.expandedId ? detailBlock(item, items) : '');
    }

    function suggestionRow(item) {
      const selected = item.id === filters.expandedId ? ' selected' : '';
      const chevron = item.id === filters.expandedId ? '&#9662;' : '&#8250;';
      const itemStatus = statusOf(item.id, item);
      const checked = filters.selectedIds.has(item.id) ? ' checked' : '';
      const checkboxDisabled = itemStatus === 'open' && !dismissedIds.has(item.id) ? '' : ' disabled';
      const statusMark = '<span class="status ' + escapeHtml(itemStatus) + '">' + escapeHtml(itemStatus.toUpperCase()) + '</span>';
      return '<div id="suggestion-' + escapeHtml(item.id) + '" class="suggestion' + selected + '">' +
        '<div class="chevron">' + chevron + '</div>' +
        '<label class="selectbox"><input id="select-' + escapeHtml(item.id) + '" type="checkbox"' + checked + checkboxDisabled + '></label>' +
        '<div class="title">' + escapeHtml(titleOf(item)) + '</div>' +
        statusMark +
        '<div class="meta">' +
        '<span class="pill ' + escapeHtml(norm(severityOf(item))) + '">' + escapeHtml(severityOf(item)) + '</span>' +
        '<span>L' + escapeHtml(item.line || '-') + '</span>' +
        '<span class="pill ' + escapeHtml(norm(item.source)) + '">' + escapeHtml(item.source || 'unknown') + '</span>' +
        '</div></div>';
    }

    function detailBlock(item, items) {
      const index = items.findIndex((candidate) => candidate.id === item.id);
      const prevDisabled = index <= 0 ? ' disabled' : '';
      const nextDisabled = index >= items.length - 1 ? ' disabled' : '';
      const itemStatus = statusOf(item.id, item);
      const isDismissed = dismissedIds.has(item.id);
      const applyDisabled = (itemStatus === 'open' || itemStatus === 'applied') && !dismissedIds.has(item.id) && state.applyAllowed ? '' : ' disabled';
      const applyLabel = itemStatus === 'applied' ? 'Undo' : 'Apply';
      const applyCommand = itemStatus === 'applied' ? 'undo' : 'apply';
      const applyTitle = state.applyAllowed ? '' : ' title="' + escapeHtml(state.applyDisabledReason) + '"';
      const dismissCommand = isDismissed ? 'restoreDismissed' : 'dismiss';
      const dismissLabel = isDismissed ? 'Restore' : 'Dismiss';
      return '<div class="detail" id="detail-' + escapeHtml(item.id) + '"><h3>' + escapeHtml(titleOf(item)) + '</h3>' +
        '<div class="meta"><span>' + escapeHtml(item.file) + ':' + escapeHtml(item.line || '-') + '</span><span class="status ' + escapeHtml(itemStatus) + '">' + escapeHtml(itemStatus.toUpperCase()) + '</span><span class="pill ' + escapeHtml(norm(severityOf(item))) + '">' + escapeHtml(severityOf(item)) + '</span><span class="pill ' + escapeHtml(norm(item.source)) + '">' + escapeHtml(item.source || 'unknown') + '</span></div>' +
        '<p><strong>Problem:</strong> ' + escapeHtml(item.problem || '') + '</p>' +
        '<p><strong>Proposal:</strong> ' + escapeHtml(item.solution || '') + '</p>' +
        '<div class="detail-actions">' +
        '<button class="button secondary" id="previous"' + prevDisabled + '>Previous</button>' +
        '<button class="button secondary" id="open">Locate</button>' +
        '<button class="button secondary" id="preview">Details</button>' +
        '<button class="button secondary" id="diff">Diff</button>' +
        '<button class="button" id="apply" data-command="' + applyCommand + '"' + applyTitle + applyDisabled + '>' + applyLabel + '</button>' +
        '<button class="button secondary" id="dismiss" data-command="' + dismissCommand + '">' + dismissLabel + '</button>' +
        '<button class="button secondary" id="next"' + nextDisabled + '>Next</button>' +
        '</div></div>';
    }

    function wireDetail(items) {
      const item = items.find((candidate) => candidate.id === filters.expandedId);
      if (!item) return;
      const index = items.findIndex((candidate) => candidate.id === item.id);
      byId('open')?.addEventListener('click', () => vscode.postMessage({ command: 'open', id: item.id }));
      byId('preview')?.addEventListener('click', () => vscode.postMessage({ command: 'preview', id: item.id }));
      byId('diff')?.addEventListener('click', () => vscode.postMessage({ command: 'diff', id: item.id }));
      byId('dismiss')?.addEventListener('click', () => {
        const command = byId('dismiss')?.getAttribute('data-command') || 'dismiss';
        vscode.postMessage({ command, id: item.id });
      });
      byId('apply')?.addEventListener('click', () => {
        const command = byId('apply')?.getAttribute('data-command') || 'apply';
        if (state.applyAllowed && (statusOf(item.id, item) === 'open' || statusOf(item.id, item) === 'applied')) {
          vscode.postMessage({ command, id: item.id });
        }
      });
      byId('previous')?.addEventListener('click', () => {
        if (index > 0) {
          filters.expandedId = items[index - 1].id;
          render();
        }
      });
      byId('next')?.addEventListener('click', () => {
        if (index < items.length - 1) {
          filters.expandedId = items[index + 1].id;
          render();
        }
      });
    }

    function statusOf(id, item) {
      return filters.statuses[id] || item?.status || 'open';
    }

    function escapeHtml(value) {
      return String(value ?? '').replace(/[&<>"']/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
    }

    setupOptions();
    render();
    vscode.postMessage({ command: 'loadStatuses' });
  </script>
</body>
</html>`;
}

function currentWorkspaceFile(): string {
  const editor = vscode.window.activeTextEditor;
  if (!editor) {
    return '';
  }
  const relative = vscode.workspace.asRelativePath(editor.document.uri, false);
  return relative.replace(/\\/g, '/');
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

async function diffSuggestion(suggestion: Suggestion): Promise<void> {
  if (!diffContentProvider) {
    await previewSuggestion(suggestion);
    return;
  }
  const extension = path.extname(suggestion.file) || '.txt';
  const safeId = encodeURIComponent(suggestion.id.replace(/[^\w.-]/g, '_'));
  const originalUri = vscode.Uri.parse(`${DIFF_SCHEME}:/${safeId}/original${extension}`);
  const proposedUri = vscode.Uri.parse(`${DIFF_SCHEME}:/${safeId}/proposed${extension}`);
  diffContentProvider.set(originalUri, suggestion.original_code || '');
  diffContentProvider.set(proposedUri, suggestion.proposed_code || '');
  await vscode.commands.executeCommand(
    'vscode.diff',
    originalUri,
    proposedUri,
    `CodeGuardian Diff: ${path.basename(suggestion.file)}:${suggestion.line || '-'}`
  );
}

async function applyOpenSuggestion(suggestion: Suggestion): Promise<void> {
  const data = loadResultsData(false);
  if (!data.applyAllowed) {
    vscode.window.showWarningMessage(data.applyDisabledReason);
    return;
  }
  const statuses = await loadSuggestionStatuses();
  const status = statuses[suggestion.id] || suggestion.status || 'open';
  if (status !== 'open') {
    vscode.window.showInformationMessage(`CodeGuardian suggestion ${suggestion.id} is ${status.toUpperCase()} and was not applied.`);
    return;
  }
  await applySuggestion(suggestion);
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

async function undoAppliedSuggestion(suggestion: Suggestion): Promise<void> {
  const data = loadResultsData(false);
  if (!data.applyAllowed) {
    vscode.window.showWarningMessage(data.applyDisabledReason);
    return;
  }
  const statuses = await loadSuggestionStatuses();
  const status = statuses[suggestion.id] || suggestion.status || 'open';
  if (status !== 'applied') {
    vscode.window.showInformationMessage(`CodeGuardian suggestion ${suggestion.id} is ${status.toUpperCase()} and cannot be undone.`);
    return;
  }

  const answer = await vscode.window.showWarningMessage(
    `Undo CodeGuardian suggestion ${suggestion.id} in ${suggestion.file}?`,
    { modal: true },
    'Undo'
  );
  if (answer !== 'Undo') {
    return;
  }

  const python = configValue('pythonPath') || 'python';
  const cli = absoluteWorkspacePath(configValue('cliPath') || 'tools/codeguardian_cli.py');
  const result = await runCliWithFallback(python, [cli, 'undo', '--file', resultsPath(), '--id', suggestion.id]);
  vscode.window.showInformationMessage(result.stdout || 'CodeGuardian suggestion undone.');
}

async function applySelectedOpenSuggestions(ids: string[]): Promise<void> {
  const data = loadResultsData(false);
  if (!data.applyAllowed) {
    vscode.window.showWarningMessage(data.applyDisabledReason);
    return;
  }
  if (!ids.length) {
    vscode.window.showInformationMessage('Select one or more OPEN CodeGuardian suggestions first.');
    return;
  }
  const suggestions = loadSuggestions().filter((suggestion) => ids.includes(suggestion.id));
  const statuses = await loadSuggestionStatuses();
  const openSuggestions = suggestions.filter((suggestion) => (statuses[suggestion.id] || suggestion.status || 'open') === 'open');
  const skipped = suggestions.length - openSuggestions.length;
  if (!openSuggestions.length) {
    vscode.window.showInformationMessage(`No selected CodeGuardian suggestions are OPEN. Skipped ${skipped}.`);
    return;
  }
  if (skipped > 0) {
    vscode.window.showInformationMessage(`Skipping ${skipped} selected suggestion(s) that are already APPLIED or CHANGED.`);
  }
  await applySelectedSuggestions(openSuggestions);
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

async function loadSuggestionStatuses(): Promise<Record<string, SuggestionStatus>> {
  const python = configValue('pythonPath') || 'python';
  const cli = absoluteWorkspacePath(configValue('cliPath') || 'tools/codeguardian_cli.py');
  const result = await runCliWithFallback(python, [cli, 'status', '--file', resultsPath()]);
  const parsed = JSON.parse(result.stdout || '{"suggestions":[]}');
  const statuses: Record<string, SuggestionStatus> = {};
  for (const item of parsed.suggestions || []) {
    if (item.id && ['open', 'applied', 'changed'].includes(item.status)) {
      statuses[item.id] = item.status;
    }
  }
  overlayOpenDocumentStatuses(statuses);
  return statuses;
}

function overlayOpenDocumentStatuses(statuses: Record<string, SuggestionStatus>): void {
  const openDocuments = new Map<string, string>();
  for (const document of vscode.workspace.textDocuments) {
    if (document.uri.scheme !== 'file') {
      continue;
    }
    const relative = vscode.workspace.asRelativePath(document.uri, false).replace(/\\/g, '/');
    openDocuments.set(relative, document.getText());
  }

  for (const suggestion of loadSuggestions()) {
    const text = openDocuments.get(String(suggestion.file || '').replace(/\\/g, '/'));
    if (text === undefined) {
      continue;
    }
    if (containsNormalizedBlock(text, suggestion.proposed_code || '')) {
      statuses[suggestion.id] = 'applied';
    } else if (containsNormalizedBlock(text, suggestion.original_code || '')) {
      statuses[suggestion.id] = 'open';
    } else {
      statuses[suggestion.id] = 'changed';
    }
  }
}

function containsNormalizedBlock(text: string, block: string): boolean {
  const normalized = normalizeBlock(block);
  return normalized.length > 0 && normalizeBlock(text).includes(normalized);
}

function normalizeBlock(text: string): string {
  return String(text || '')
    .replace(/\r\n/g, '\n')
    .split('\n')
    .map((line) => line.trim())
    .join('\n')
    .trim();
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
        reject(new Error(`CodeGuardian apply failed: ${message}`));
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

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function short(text: string, max: number): string {
  const normalized = (text || '').replace(/\s+/g, ' ').trim();
  return normalized.length <= max ? normalized : `${normalized.slice(0, max - 3)}...`;
}
