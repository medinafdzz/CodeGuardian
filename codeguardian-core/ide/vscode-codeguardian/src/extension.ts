import * as vscode from 'vscode';
import * as fs from 'fs';
import * as path from 'path';
import * as http from 'http';
import * as https from 'https';
import { execFile } from 'child_process';
import { URL } from 'url';
import {
  buildMutationCliArgs,
  mutationProgressTitle,
  mutationStatuses,
  parseMutationSummary,
} from './cliProtocol';

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
  optional_removed_imports?: string[];
  auxiliary_edits?: AuxiliaryEdit[];
  validation_status?: string;
  validation_notes?: string[];
  status?: string;
};

type AuxiliaryEdit = {
  original_code: string;
  proposed_code: string;
  description?: string;
};

type CliResult = {
  stdout: string;
  stderr: string;
  exitCode: number;
};

class ArtifactContextError extends Error {}

type SuggestionStatus = 'open' | 'applied' | 'changed';

type ResultsData = {
  suggestions: Suggestion[];
  dismissedIds: string[];
  artifact: ArtifactState;
  jenkinsWatch: JenkinsWatchStatus;
  autoDownload: boolean;
  applyAllowed: boolean;
  applyDisabledReason: string;
  credentialStatus: CredentialStatus;
  selectedPr: SelectedPullRequestState;
  profile: ProjectProfile;
  gitState: GitChangeState;
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

type GitChangeState = {
  isGitRepository: boolean;
  hasChanges: boolean;
  changeCount: number;
  message: string;
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
  timestamp?: number;
  duration?: number;
  estimatedDuration?: number;
  artifacts?: JenkinsArtifact[];
  actions?: Array<Record<string, unknown>>;
};

type JenkinsWatchState =
  | 'idle'
  | 'waiting_pr'
  | 'waiting_job'
  | 'queued'
  | 'running'
  | 'success'
  | 'artifact_ready'
  | 'artifact_downloaded'
  | 'failed'
  | 'timeout'
  | 'error'
  | 'unknown';

type JenkinsWatchStatus = {
  state: JenkinsWatchState;
  progress?: number;
  message: string;
  buildNumber?: number;
  jobUrl?: string;
  artifactUrl?: string;
  artifactReady: boolean;
  lastUpdatedAt: number;
  error?: string;
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
  links?: {
    html?: {
      href?: string;
    };
  };
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

type SelectedPullRequestState = {
  id?: string;
  title?: string;
  sourceBranch?: string;
  destinationBranch?: string;
  url?: string;
};

type ProjectProfile = {
  profile: string;
  validationCommand?: string;
  defaultTab: 'all' | 'issues' | 'optimizations' | 'applied' | 'changed' | 'dismissed';
  maxRecommended: number;
  allowApply: boolean;
  showOptimizations: boolean;
};

const DEFAULT_PROJECT_PROFILE: ProjectProfile = {
  profile: 'default',
  defaultTab: 'all',
  maxRecommended: 3,
  allowApply: true,
  showOptimizations: true,
};

const DISMISSED_KEY = 'codeguardian.dismissedSuggestionIds';
const ARTIFACT_STATE_KEY = 'codeguardian.artifactState';
const SELECTED_PR_KEY = 'codeguardian.selectedPrId';
const SELECTED_PR_DETAILS_KEY = 'codeguardian.selectedPrDetails';
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
let buildWatcher: JenkinsBuildWatcher | undefined;
let envConfigCache: Record<string, string> | undefined;
let envConfigCachePath: string | undefined;
let envConfigCacheMtime = 0;
let outputChannel: vscode.OutputChannel | undefined;
let workspaceSnapshot: { context: LocalGitContext; gitState: GitChangeState } = {
  context: { warnings: ['local Git context has not been loaded'] },
  gitState: {
    isGitRepository: false,
    hasChanges: false,
    changeCount: 0,
    message: 'Local Git state has not been loaded',
  },
};
let workspaceSnapshotRefresh: Promise<typeof workspaceSnapshot> | undefined;

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
  private artifactReadyDownloadInProgress = false;
  private currentData?: ResultsData;

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
        const ids = message.ids || (message.id ? [message.id] : []);
        this.view?.webview.postMessage({ command: 'operationComplete', ids, statuses: {} });
        await showTechnicalError('CodeGuardian could not complete the operation.', error);
      }
    });
    this.refresh();
  }

  refresh(): void {
    if (!this.view) {
      return;
    }
    this.currentData = loadResultsData(false);
    this.view.webview.html = renderDashboardHtml(this.view.webview, this.context.extensionUri, this.currentData);
  }

  async refreshStatuses(): Promise<void> {
    if (!this.view) {
      return;
    }
    this.view.webview.postMessage({
      command: 'statuses',
      statuses: await loadSuggestionStatuses(this.currentData?.suggestions),
    });
  }

  updateStatuses(statuses: Record<string, SuggestionStatus>, ids = Object.keys(statuses)): void {
    this.view?.webview.postMessage({ command: 'operationComplete', ids, statuses });
  }

  private async handleMessage(message: {
    command?: string;
    id?: string;
    ids?: string[];
    statuses?: Record<string, SuggestionStatus>;
  }): Promise<void> {
    const suggestion = message.id
      ? this.currentData?.suggestions.find((item) => item.id === message.id)
      : undefined;
    switch (message.command) {
      case 'refresh':
        await refreshWorkspaceSnapshot();
        this.refresh();
        this.view?.webview.postMessage({ command: 'statuses', statuses: await loadSuggestionStatuses() });
        void buildWatcher?.start('refresh');
        break;
      case 'download':
        if (isDownloadArtifactBlocked(buildWatcher?.status)) {
          vscode.window.showInformationMessage(downloadArtifactTooltip(buildWatcher?.status));
          break;
        }
        try {
          if (buildWatcher?.status.artifactReady && buildWatcher.status.artifactUrl) {
            await downloadResultsFromUrl(buildWatcher.status.artifactUrl, {
              buildNumber: buildWatcher.status.buildNumber ? String(buildWatcher.status.buildNumber) : undefined,
            });
          } else {
            await downloadLatestResults();
          }
          this.refresh();
        } finally {
          void buildWatcher?.start('download');
        }
        break;
      case 'downloadReadyArtifact':
        if (this.artifactReadyDownloadInProgress) {
          break;
        }
        this.artifactReadyDownloadInProgress = true;
        try {
          if (buildWatcher?.status.artifactReady && buildWatcher.status.artifactUrl) {
            await downloadResultsFromUrl(buildWatcher.status.artifactUrl, {
              buildNumber: buildWatcher.status.buildNumber ? String(buildWatcher.status.buildNumber) : undefined,
            });
            buildWatcher.markArtifactDownloaded();
          } else {
            const downloaded = await tryDownloadReadyArtifact();
            if (downloaded) {
              buildWatcher?.markArtifactDownloaded();
            }
          }
          this.refresh();
        } finally {
          this.artifactReadyDownloadInProgress = false;
        }
        break;
      case 'selectPr':
        await selectPullRequestAndDownload();
        this.refresh();
        void buildWatcher?.start('selectPr');
        break;
      case 'openPr':
        await openSelectedPullRequest();
        break;
      case 'loadStatuses':
        this.view?.webview.postMessage({
          command: 'statuses',
          statuses: await loadSuggestionStatuses(this.currentData?.suggestions),
        });
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
          const statuses = await applyOpenSuggestion(suggestion, this.currentData);
          this.updateStatuses(statuses, [suggestion.id]);
        }
        break;
      case 'undo':
        if (suggestion) {
          const statuses = await undoAppliedSuggestion(suggestion, this.currentData);
          this.updateStatuses(statuses, [suggestion.id]);
        }
        break;
      case 'applySelected':
        this.updateStatuses(
          await applySelectedOpenSuggestions(
            message.ids || [],
            this.currentData,
            message.statuses,
          ),
          message.ids || [],
        );
        break;
      case 'openGitDiff':
        await openGitDiff();
        break;
      case 'openLog':
        await openActivityLog();
        break;
      case 'undoSelected':
        this.updateStatuses(
          await undoSelectedAppliedSuggestions(
            message.ids || [],
            this.currentData,
            message.statuses,
          ),
          message.ids || [],
        );
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
      case 'watchBuild':
        await buildWatcher?.start('manual');
        this.refresh();
        break;
      case 'stopWatch':
        buildWatcher?.stop('Stopped by user.');
        this.refresh();
        break;
    }
  }
}

class JenkinsBuildWatcher {
  private timer?: NodeJS.Timeout;
  private startedAt = 0;
  private activeKey = '';
  status: JenkinsWatchStatus = idleJenkinsWatchStatus();

  constructor(
    private readonly provider: SuggestionsProvider,
    private readonly dashboard: DashboardProvider,
  ) {}

  async start(reason: string): Promise<void> {
    if (this.timer) {
      this.clearTimer();
    }
    this.startedAt = Date.now();
    this.activeKey = reason;
    this.setStatus({
      state: 'waiting_pr',
      message: 'Resolving Bitbucket pull request.',
      artifactReady: false,
      lastUpdatedAt: Date.now(),
    });
    await this.tick();
  }

  stop(message = 'Build watcher stopped.'): void {
    this.clearTimer();
    this.setStatus({
      ...this.status,
      state: 'idle',
      message,
      artifactReady: false,
      lastUpdatedAt: Date.now(),
    });
  }

  dispose(): void {
    this.clearTimer();
  }

  private async tick(): Promise<void> {
    if (this.hasTimedOut()) {
      this.clearTimer();
      this.setStatus({
        ...this.status,
        state: 'timeout',
        message: 'Jenkins build watch timed out.',
        artifactReady: false,
        lastUpdatedAt: Date.now(),
      });
      return;
    }

    try {
      const local = getLocalGitContext();
      if (!local.repository || !local.branch || !local.headCommit) {
        this.setStatus({
          state: 'unknown',
          message: local.warnings.join(', ') || 'Local Git context is unavailable.',
          artifactReady: false,
          lastUpdatedAt: Date.now(),
        });
        this.schedule();
        return;
      }

      const pr = await findCurrentBranchPullRequest();
      if (!pr) {
        this.setStatus({
          state: 'waiting_pr',
          message: 'No open PR found for current branch.',
          artifactReady: false,
          lastUpdatedAt: Date.now(),
        });
        this.schedule();
        return;
      }

      await setSelectedPullRequest(pr);
      const jobPath = [...baseJobParts(), `PR-${pr.id}`].join('/');
      const jobUrl = buildJobUrl(jobPathParts(jobPath));
      logInfo(`Jenkins job resolved: ${jobPath}`);
      const apiUrl = buildJenkinsJobApiUrl(configValue('jenkinsUrl'), jobPath, 'lastBuild/api/json');
      const build = await fetchJenkinsBuild(apiUrl);
      const next = jenkinsStatusFromBuild(build, jobUrl);
      this.setStatus(next);

      if (next.state === 'artifact_ready' && next.artifactUrl) {
        if (configBoolean('autoDownload', true)) {
          const artifact = await downloadResultsFromUrl(next.artifactUrl, {
            commit: extractBuildCommit(build),
            buildNumber: build.number ? String(build.number) : undefined,
          });
          this.provider.refresh();
          if (artifact.validation !== 'valid') {
            this.setStatus({
              ...next,
              state: 'running',
              message: `Build #${build.number || ''} artifact is stale; waiting for latest results.`,
              artifactReady: false,
              progress: undefined,
              lastUpdatedAt: Date.now(),
            });
            this.schedule();
            return;
          }
          this.setStatus({
            ...next,
            state: 'artifact_downloaded',
            message: `Build #${build.number || ''} results downloaded.`,
            artifactReady: false,
            progress: 100,
            lastUpdatedAt: Date.now(),
          });
          this.dashboard.refresh();
          vscode.window.showInformationMessage(`CodeGuardian results downloaded for build #${build.number || 'latest'}.`);
          this.clearTimer();
          return;
        }
        vscode.window.showInformationMessage('CodeGuardian artifact is ready.');
        this.clearTimer();
        return;
      }

      if (['failed', 'success', 'artifact_downloaded'].includes(next.state)) {
        this.clearTimer();
        return;
      }
      this.schedule(next.state === 'running');
    } catch (error) {
      const message = errorMessage(error);
      const state: JenkinsWatchState = /404|not found/i.test(message) ? 'waiting_job' : 'error';
      this.setStatus({
        state,
        message: state === 'waiting_job' ? 'Waiting for Jenkins PR job.' : 'Could not reach Jenkins.',
        artifactReady: false,
        error: state === 'error' ? message : undefined,
        lastUpdatedAt: Date.now(),
      });
      this.schedule();
    }
  }

  private schedule(running = false): void {
    this.clearTimer();
    const intervalSeconds = running
      ? Math.min(Math.max(5, configNumber('pollIntervalSeconds', 45)), 10)
      : Math.max(5, configNumber('pollIntervalSeconds', 45));
    this.timer = setTimeout(() => void this.tick(), intervalSeconds * 1000);
  }

  private clearTimer(): void {
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = undefined;
    }
  }

  private hasTimedOut(): boolean {
    const maxMs = Math.max(1, configNumber('maxBuildWatchMinutes', 30)) * 60 * 1000;
    return Boolean(this.startedAt && Date.now() - this.startedAt > maxMs);
  }

  private setStatus(status: JenkinsWatchStatus): void {
    if (status.state !== this.status.state || status.buildNumber !== this.status.buildNumber) {
      logInfo(`Jenkins status: ${status.state}${status.buildNumber ? ` build #${status.buildNumber}` : ''}`);
    }
    this.status = status;
    this.dashboard.refresh();
  }

  markArtifactDownloaded(): void {
    this.clearTimer();
    this.setStatus({
      ...this.status,
      state: 'artifact_downloaded',
      message: this.status.buildNumber ? `Build #${this.status.buildNumber} results downloaded.` : 'Results downloaded.',
      artifactReady: false,
      progress: 100,
      lastUpdatedAt: Date.now(),
    });
  }
}

export async function activate(context: vscode.ExtensionContext): Promise<void> {
  extensionContext = context;
  outputChannel = vscode.window.createOutputChannel('CodeGuardian');
  context.subscriptions.push(outputChannel);
  logInfo('Extension activated.');
  diffContentProvider = new DiffContentProvider();
  context.subscriptions.push(vscode.workspace.registerTextDocumentContentProvider(DIFF_SCHEME, diffContentProvider));
  await initializeCredentials();
  logInfo(`Credentials source: ${credentialCache.source}`);
  await refreshWorkspaceSnapshot();
  loadProjectProfile();
  const provider = new SuggestionsProvider();
  const dashboard = new DashboardProvider(context);
  buildWatcher = new JenkinsBuildWatcher(provider, dashboard);
  const tree = vscode.window.createTreeView('codeguardianSuggestions', {
    treeDataProvider: provider,
    canSelectMany: true,
  });
  context.subscriptions.push(tree);
  context.subscriptions.push(vscode.window.registerWebviewViewProvider('codeguardianDashboard', dashboard));

  context.subscriptions.push(vscode.commands.registerCommand('codeguardian.refreshSuggestions', async () => {
    await refreshWorkspaceSnapshot();
    provider.refresh();
    dashboard.refresh();
    void buildWatcher?.start('refresh');
  }));
  context.subscriptions.push(vscode.commands.registerCommand('codeguardian.downloadLatestResults', async () => {
    try {
      if (isDownloadArtifactBlocked(buildWatcher?.status)) {
        vscode.window.showInformationMessage(downloadArtifactTooltip(buildWatcher?.status));
        return;
      }
      await downloadLatestResults();
      provider.refresh();
      dashboard.refresh();
      void buildWatcher?.start('download');
    } catch (error) {
      vscode.window.showErrorMessage(`CodeGuardian results download failed: ${String(error)}`);
      void buildWatcher?.start('download');
    }
  }));
  context.subscriptions.push(vscode.commands.registerCommand('codeguardian.selectPullRequest', async () => {
    try {
      await selectPullRequestAndDownload();
      provider.refresh();
      dashboard.refresh();
      void buildWatcher?.start('selectPr');
    } catch (error) {
      vscode.window.showErrorMessage(`CodeGuardian PR selection failed: ${errorMessage(error)}`);
    }
  }));
  context.subscriptions.push(vscode.commands.registerCommand('codeguardian.watchJenkinsBuild', async () => {
    await buildWatcher?.start('manual');
  }));
  context.subscriptions.push(vscode.commands.registerCommand('codeguardian.stopJenkinsBuildWatch', () => {
    buildWatcher?.stop('Stopped by user.');
    dashboard.refresh();
  }));
  context.subscriptions.push(vscode.commands.registerCommand('codeguardian.openGitDiff', () => openGitDiff()));
  context.subscriptions.push(vscode.commands.registerCommand('codeguardian.openActivityLog', () => openActivityLog()));
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
      dashboard.updateStatuses(await applyOpenSuggestion(node.suggestion));
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
      dashboard.updateStatuses(await applySelectedOpenSuggestions(
        suggestions.map((suggestion) => suggestion.id),
      ));
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
  startGitHeadWatcher(context, provider, dashboard);
  startGitStatusWatcher(context, dashboard);
  if (configBoolean('watchBuildOnStartup', true) && shouldWatchBuildOnStartup()) {
    void buildWatcher.start('startup');
  }
}

export function deactivate(): void {
  buildWatcher?.dispose();
}

function logInfo(message: string): void {
  outputChannel?.appendLine(`[${new Date().toISOString()}] INFO ${message}`);
}

function logWarn(message: string): void {
  outputChannel?.appendLine(`[${new Date().toISOString()}] WARN ${message}`);
}

function logError(message: string): void {
  outputChannel?.appendLine(`[${new Date().toISOString()}] ERROR ${message}`);
}

async function showTechnicalError(message: string, error: unknown): Promise<void> {
  logError(`${message} ${errorMessage(error)}`);
  const action = await vscode.window.showErrorMessage(message, 'Show details');
  if (action === 'Show details') {
    outputChannel?.show(true);
  }
}

function workspaceRoot(): vscode.WorkspaceFolder {
  const folder = vscode.workspace.workspaceFolders?.[0];
  if (!folder) {
    throw new Error('Open a workspace before using CodeGuardian.');
  }
  return folder;
}

function configValue(name: string): string {
  const envValue = envConfigValue(name);
  if (envValue) {
    return envValue;
  }
  return vscode.workspace.getConfiguration('codeguardian').get<string>(name) || '';
}

function configBoolean(name: string, fallback = false): boolean {
  const envValue = envConfigValue(name);
  if (envValue) {
    return ['1', 'true', 'yes', 'on'].includes(envValue.toLowerCase());
  }
  return vscode.workspace.getConfiguration('codeguardian').get<boolean>(name) ?? fallback;
}

function configNumber(name: string, fallback: number): number {
  const envValue = envConfigValue(name);
  if (envValue && !Number.isNaN(Number(envValue))) {
    return Number(envValue);
  }
  return vscode.workspace.getConfiguration('codeguardian').get<number>(name) || fallback;
}

function envConfigValue(name: string): string {
  const mappings: Record<string, string> = {
    resultsFile: 'CODEGUARDIAN_RESULTS_FILE',
    pythonPath: 'CODEGUARDIAN_PYTHON_PATH',
    cliPath: 'CODEGUARDIAN_CLI_PATH',
    jenkinsArtifactUrl: 'CODEGUARDIAN_JENKINS_ARTIFACT_URL',
    jenkinsUrl: 'CODEGUARDIAN_JENKINS_URL',
    jenkinsJobPath: 'CODEGUARDIAN_JENKINS_JOB_PATH',
    jenkinsBuildSelector: 'CODEGUARDIAN_JENKINS_BUILD_SELECTOR',
    jenkinsArtifactName: 'CODEGUARDIAN_JENKINS_ARTIFACT_NAME',
    autoDownload: 'CODEGUARDIAN_AUTO_DOWNLOAD',
    pollIntervalSeconds: 'CODEGUARDIAN_POLL_INTERVAL_SECONDS',
    watchBuildOnStartup: 'CODEGUARDIAN_WATCH_BUILD_ON_STARTUP',
    watchBuildOnGitChange: 'CODEGUARDIAN_WATCH_BUILD_ON_GIT_CHANGE',
    maxBuildWatchMinutes: 'CODEGUARDIAN_MAX_BUILD_WATCH_MINUTES',
    allowApplyWithUnknownArtifact: 'CODEGUARDIAN_ALLOW_APPLY_WITH_UNKNOWN_ARTIFACT',
  };
  const key = mappings[name];
  const value = key ? getWorkspaceEnvConfig()[key] || '' : '';
  return ['resultsFile', 'pythonPath', 'cliPath'].includes(name) ? normalizeEnvPathValue(value) : value;
}

function getWorkspaceEnvConfig(): Record<string, string> {
  const envFile = findWorkspaceEnvFile();
  const mtime = envFile ? fs.statSync(envFile).mtimeMs : 0;
  if (envConfigCache && envConfigCachePath === envFile && envConfigCacheMtime === mtime) {
    return envConfigCache;
  }
  envConfigCache = envFile ? parseEnvFile(envFile) : {};
  envConfigCachePath = envFile;
  envConfigCacheMtime = mtime;
  return envConfigCache;
}

function normalizeEnvPathValue(value: string): string {
  return value.replace(/\\\\/g, '\\');
}

function idleJenkinsWatchStatus(): JenkinsWatchStatus {
  return {
    state: 'idle',
    message: 'Jenkins watcher idle.',
    artifactReady: false,
    lastUpdatedAt: Date.now(),
  };
}

function shouldWatchBuildOnStartup(): boolean {
  const data = loadResultsData(false);
  return !data.suggestions.length || ['stale', 'mismatch', 'unknown'].includes(data.artifact.validation);
}

function startGitHeadWatcher(
  context: vscode.ExtensionContext,
  provider: SuggestionsProvider,
  dashboard: DashboardProvider,
): void {
  if (!configBoolean('watchBuildOnGitChange', true)) {
    return;
  }
  let lastHead = gitHeadStateKey(getLocalGitContext());
  const interval = setInterval(() => {
    void refreshWorkspaceSnapshot().then(({ context: nextContext }) => {
      const key = gitHeadStateKey(nextContext);
      if (nextContext.headCommit && key !== lastHead) {
        lastHead = key;
        provider.refresh();
        dashboard.refresh();
        void buildWatcher?.start('git-change');
      }
    });
  }, Math.max(5, configNumber('pollIntervalSeconds', 45)) * 1000);
  context.subscriptions.push({ dispose: () => clearInterval(interval) });
}

function gitHeadStateKey(context: LocalGitContext): string {
  return `${context.branch || ''}:${context.headCommit || ''}`;
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
  const intervalMs = Math.max(5, configNumber('pollIntervalSeconds', 45)) * 1000;
  const timer = setInterval(() => void poll(), intervalMs);
  context.subscriptions.push({ dispose: () => clearInterval(timer) });
}

function startGitStatusWatcher(context: vscode.ExtensionContext, dashboard: DashboardProvider): void {
  let lastState = gitChangeStateKey(currentGitChangeState());
  const timer = setInterval(() => {
    void refreshWorkspaceSnapshot().then(({ gitState }) => {
      const nextState = gitChangeStateKey(gitState);
      if (nextState !== lastState) {
        lastState = nextState;
        dashboard.refresh();
      }
    });
  }, 5000);
  context.subscriptions.push({ dispose: () => clearInterval(timer) });
}

function gitChangeStateKey(state: GitChangeState): string {
  return `${state.isGitRepository}:${state.hasChanges}:${state.changeCount}`;
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

async function currentGitBranch(): Promise<string> {
  return runWorkspaceCommand('git', ['branch', '--show-current']);
}

function currentGitHeadSync(): string | undefined {
  return workspaceSnapshot.context.headCommit;
}

function currentGitChangeState(): GitChangeState {
  return workspaceSnapshot.gitState;
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
  return workspaceSnapshot.context;
}

async function optionalWorkspaceCommand(args: string[]): Promise<string | undefined> {
  try {
    return await runWorkspaceCommand('git', args);
  } catch {
    return undefined;
  }
}

async function refreshWorkspaceSnapshot(): Promise<typeof workspaceSnapshot> {
  if (workspaceSnapshotRefresh) {
    return workspaceSnapshotRefresh;
  }
  workspaceSnapshotRefresh = (async () => {
    const [remote, branch, headCommit, status] = await Promise.all([
      optionalWorkspaceCommand(['remote', 'get-url', 'origin']),
      optionalWorkspaceCommand(['branch', '--show-current']),
      optionalWorkspaceCommand(['rev-parse', 'HEAD']),
      optionalWorkspaceCommand(['status', '--porcelain']),
    ]);
    let repository: RepositoryInfo | undefined;
    if (remote) {
      try {
        repository = repositoryInfoFromRemote(remote);
      } catch {
        repository = undefined;
      }
    }
    const warnings: string[] = [];
    if (!repository) warnings.push('local repository could not be detected');
    if (!branch) warnings.push('local branch could not be detected');
    if (!headCommit) warnings.push('local HEAD commit could not be detected');
    const isGitRepository = status !== undefined && headCommit !== undefined;
    const changes = status?.split(/\r?\n/).filter((line) => line.trim()).length || 0;
    workspaceSnapshot = {
      context: {
        workspace: repository?.workspace,
        repository: repository?.repo,
        branch,
        headCommit,
        warnings,
      },
      gitState: {
        isGitRepository,
        hasChanges: changes > 0,
        changeCount: changes,
        message: !isGitRepository
          ? 'Current workspace is not a Git repository'
          : changes > 0
            ? `${changes} local Git change${changes === 1 ? '' : 's'}`
            : 'No local Git changes to show',
      },
    };
    return workspaceSnapshot;
  })();
  try {
    return await workspaceSnapshotRefresh;
  } finally {
    workspaceSnapshotRefresh = undefined;
  }
}

function loadProjectProfile(): ProjectProfile {
  const file = path.join(workspaceRoot().uri.fsPath, '.codeguardian.json');
  if (!fs.existsSync(file)) {
    logInfo('Project profile missing; using defaults.');
    return DEFAULT_PROJECT_PROFILE;
  }
  try {
    const raw = JSON.parse(fs.readFileSync(file, 'utf8')) as Record<string, unknown>;
    const profile = normalizeProjectProfile(raw);
    logInfo(`Project profile loaded: ${profile.profile}`);
    return profile;
  } catch (error) {
    logWarn(`Invalid .codeguardian.json; using defaults. ${errorMessage(error)}`);
    return DEFAULT_PROJECT_PROFILE;
  }
}

function normalizeProjectProfile(raw: Record<string, unknown>): ProjectProfile {
  const profile = typeof raw.profile === 'string' && raw.profile.trim() ? raw.profile.trim() : DEFAULT_PROJECT_PROFILE.profile;
  const defaultTab = normalizeTab(String(raw.defaultTab || DEFAULT_PROJECT_PROFILE.defaultTab));
  const maxRecommended = typeof raw.maxRecommended === 'number' && Number.isFinite(raw.maxRecommended)
    ? Math.max(0, Math.min(20, Math.floor(raw.maxRecommended)))
    : DEFAULT_PROJECT_PROFILE.maxRecommended;
  return {
    profile,
    validationCommand: typeof raw.validationCommand === 'string' ? raw.validationCommand : undefined,
    defaultTab,
    maxRecommended,
    allowApply: typeof raw.allowApply === 'boolean' ? raw.allowApply : DEFAULT_PROJECT_PROFILE.allowApply,
    showOptimizations: typeof raw.showOptimizations === 'boolean' ? raw.showOptimizations : DEFAULT_PROJECT_PROFILE.showOptimizations,
  };
}

function normalizeTab(value: string): ProjectProfile['defaultTab'] {
  const normalized = value.toLowerCase().replace(/\s+/g, '');
  if (['all', 'issues', 'optimizations', 'applied', 'changed', 'dismissed'].includes(normalized)) {
    return normalized as ProjectProfile['defaultTab'];
  }
  logWarn(`Invalid defaultTab in .codeguardian.json: ${value}`);
  return DEFAULT_PROJECT_PROFILE.defaultTab;
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
  const profile = loadProjectProfile();
  const empty = (): ResultsData => ({
    suggestions: [],
    dismissedIds: dismissedSuggestionIds(),
    artifact: { status: 'missing', validation: 'unknown', message: 'no local results file' },
    jenkinsWatch: buildWatcher?.status || idleJenkinsWatchStatus(),
    autoDownload: configBoolean('autoDownload', true),
    applyAllowed: false,
    applyDisabledReason: 'No CodeGuardian results artifact is loaded.',
    credentialStatus: getCredentialStatus(),
    selectedPr: selectedPullRequestState(),
    profile,
    gitState: currentGitChangeState(),
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
    const applyAllowed = profile.allowApply && isApplyAllowedForArtifact(artifact);
    logInfo(`Suggestions loaded: ${Array.isArray(data.suggestions) ? data.suggestions.length : 0}. Artifact validation: ${artifact.validation}.`);
    return {
      suggestions: Array.isArray(data.suggestions) ? data.suggestions : [],
      dismissedIds: dismissedSuggestionIds(),
      artifact,
      jenkinsWatch: buildWatcher?.status || idleJenkinsWatchStatus(),
      autoDownload: configBoolean('autoDownload', true),
      applyAllowed,
      applyDisabledReason: profile.allowApply ? applyDisabledReason(artifact) : 'Apply is disabled by .codeguardian.json.',
      credentialStatus: getCredentialStatus(),
      selectedPr: selectedPullRequestState(),
      profile,
      gitState: currentGitChangeState(),
    };
  } catch (error) {
    logError(`Failed to load CodeGuardian results: ${errorMessage(error)}`);
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
  logInfo(`Dismissed suggestion: ${id}`);
}

async function restoreDismissedSuggestion(id: string): Promise<void> {
  const ids = new Set(dismissedSuggestionIds());
  ids.delete(id);
  await extensionContext?.workspaceState.update(DISMISSED_KEY, Array.from(ids));
  logInfo(`Restored dismissed suggestion: ${id}`);
}

async function clearDismissedSuggestions(): Promise<void> {
  await extensionContext?.workspaceState.update(DISMISSED_KEY, []);
  logInfo('Cleared dismissed suggestions.');
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
  logInfo(`Artifact state: ${state.status}, validation: ${state.validation}.`);
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

function isDownloadArtifactBlocked(status?: JenkinsWatchStatus): boolean {
  return ['running', 'queued', 'waiting_job', 'waiting_pr'].includes(status?.state || '');
}

function downloadArtifactTooltip(status?: JenkinsWatchStatus): string {
  return isDownloadArtifactBlocked(status) ? 'Blocked while Jenkins build is running' : 'Download latest CodeGuardian artifact';
}

function selectedPullRequestState(): SelectedPullRequestState {
  return extensionContext?.workspaceState.get<SelectedPullRequestState>(SELECTED_PR_DETAILS_KEY, {}) || {};
}

async function setSelectedPullRequest(pr: BitbucketPullRequest | SelectedPullRequestState): Promise<void> {
  const id = String(pr.id || '');
  if (!id) {
    return;
  }
  const isBitbucketPr = 'source' in pr || 'destination' in pr;
  const selected = pr as SelectedPullRequestState;
  const details: SelectedPullRequestState = {
    id,
    title: pr.title,
    sourceBranch: isBitbucketPr ? (pr as BitbucketPullRequest).source?.branch?.name : selected.sourceBranch,
    destinationBranch: isBitbucketPr ? (pr as BitbucketPullRequest).destination?.branch?.name : selected.destinationBranch,
    url: isBitbucketPr ? (pr as BitbucketPullRequest).links?.html?.href : selected.url,
  };
  await extensionContext?.workspaceState.update(SELECTED_PR_KEY, id);
  await extensionContext?.workspaceState.update(SELECTED_PR_DETAILS_KEY, details);
  logInfo(`Pull request selected: PR #${id}${details.title ? ` ${details.title}` : ''}`);
}

async function openSelectedPullRequest(): Promise<void> {
  const selected = selectedPullRequestState();
  if (!selected.id) {
    vscode.window.showInformationMessage('No pull request selected.');
    return;
  }
  let url = selected.url;
  if (!url) {
    try {
      const repo = await currentRepositoryInfo();
      url = `https://bitbucket.org/${repo.workspace}/${repo.repo}/pull-requests/${selected.id}`;
    } catch {
      vscode.window.showWarningMessage('No pull request URL available.');
      return;
    }
  }
  await vscode.env.openExternal(vscode.Uri.parse(url));
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

function hasAnyCredential(credentials: Partial<CodeGuardianCredentials>): boolean {
  return Boolean(credentials.jenkinsUser || credentials.jenkinsApiToken || credentials.bitbucketEmail || credentials.bitbucketApiToken);
}

async function importCredentialsFromEnv(showMessages: boolean): Promise<boolean> {
  if (!extensionContext) {
    return false;
  }
  envConfigCache = undefined;
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

async function downloadResultsFromUrl(url: string, metadata: Partial<ArtifactState> = {}): Promise<ArtifactState> {
  logInfo('Artifact download started.');
  await refreshWorkspaceSnapshot();
  const body = await downloadText(url, jenkinsAuth());
  let parsed: Record<string, unknown>;

  try {
    parsed = JSON.parse(body);
  } catch (error) {
    await setArtifactState({ status: 'error', validation: 'unknown', downloadedAt: new Date().toISOString(), message: `invalid JSON from ${url}` });
    logError(`Artifact download failed: invalid JSON. ${errorMessage(error)}`);
    throw new Error(`Downloaded Jenkins artifact is not valid JSON: ${String(error)}`);
  }

  fs.writeFileSync(resultsPath(), body, 'utf8');
  const effectiveParsed = artifactMetadata(parsed).headCommit || !metadata.commit
    ? parsed
    : { ...parsed, head_commit: metadata.commit };
  const artifactState: ArtifactState = {
    status: 'downloaded',
    validation: validateArtifactContext(effectiveParsed, getLocalGitContext()).state,
    prId: stringFromMetadata(parsed, ['pull_request', 'pullRequest', 'pr_id', 'prId']),
    buildNumber: stringFromMetadata(parsed, ['build_number', 'buildNumber', 'build']) || metadata.buildNumber || 'latest successful build',
    commit: artifactMetadata(parsed).headCommit || metadata.commit,
    localCommit: currentGitHeadSync(),
    downloadedAt: new Date().toISOString(),
    message: 'downloaded from Jenkins artifact',
  };
  await setArtifactState(artifactState);
  logInfo(`Artifact downloaded to ${resultsPath()}.`);
  vscode.window.showInformationMessage(`Downloaded CodeGuardian results to ${resultsPath()}`);
  return artifactState;
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
    const bitbucketPr = await findOpenBitbucketPullRequestById(Number(match[1]));
    await setSelectedPullRequest(bitbucketPr || { id: match[1] });
  }
  let ready: { artifactUrl: string; buildKey: string; buildNumber?: string; commit?: string } | undefined;
  if (selected.url) {
    try {
      ready = await readyArtifactForJob(selected.url, false);
    } catch (error) {
      if (/404|not found/i.test(errorMessage(error))) {
        vscode.window.showInformationMessage('Selected pull request. CodeGuardian artifact is not ready yet.');
        void buildWatcher?.start('select-pr');
        return;
      }
      throw error;
    }
  }
  if (ready) {
    await downloadResultsFromUrl(ready.artifactUrl, { commit: ready.commit, buildNumber: ready.buildNumber });
    return;
  }
  if (selected.url) {
    vscode.window.showInformationMessage('Selected pull request. CodeGuardian artifact is not ready yet.');
    void buildWatcher?.start('select-pr');
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
    await setSelectedPullRequest(bitbucketPr);
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

async function findOpenBitbucketPullRequestById(id: number): Promise<BitbucketPullRequest | undefined> {
  const auth = bitbucketAuth();
  if (!auth) {
    return undefined;
  }
  try {
    const repo = await currentRepositoryInfo();
    const prs = await listOpenBitbucketPullRequests(repo, auth);
    return prs.find((pr) => pr.id === id);
  } catch {
    return undefined;
  }
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
    .map((job) => ({ ...job, url: normalizeJenkinsUrl(job.url), source }));
}

async function listRootPullRequestJobs(): Promise<JenkinsJobCandidate[]> {
  const baseUrl = configValue('jenkinsUrl').trim().replace(/\/+$/, '');
  const tree = encodeURIComponent('jobs[name,url,color]');
  const data = await downloadJson(`${baseUrl}/api/json?tree=${tree}`);
  const jobs = Array.isArray(data.jobs) ? data.jobs as JenkinsJob[] : [];
  return jobs
    .filter((job) => /PR-\d+/i.test(job.name))
    .map((job) => ({ ...job, url: normalizeJenkinsUrl(job.url), source: 'root' as const }));
}

function normalizeJenkinsUrl(url: string | undefined): string | undefined {
  if (!url) {
    return url;
  }
  const configuredBase = configValue('jenkinsUrl').trim().replace(/\/+$/, '');
  if (!configuredBase) {
    return url;
  }
  try {
    const parsed = new URL(url);
    return `${configuredBase}${parsed.pathname}${parsed.search}${parsed.hash}`;
  } catch {
    return url;
  }
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
  const jobUrl = normalizeJenkinsUrl(job.url);
  if (!jobUrl || !branch) {
    return false;
  }
  try {
    const tree = encodeURIComponent('name,displayName,fullDisplayName,description,url,actions[*],lastBuild[actions[*],url]');
    const data = await downloadJson(`${jobUrl.replace(/\/+$/, '')}/api/json?tree=${tree}`);
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
  const jobUrl = normalizeJenkinsUrl(job.url);
  if (jobUrl) {
    return `${jobUrl.replace(/\/+$/, '')}/${encodeURIComponent(buildSelector)}/artifact/${encodeURIComponent(artifactName)}`;
  }
  if (job.source === 'configured') {
    return buildArtifactUrlForJob([...baseJobParts(), job.name], buildSelector, artifactName);
  }
  return buildArtifactUrlForJob([job.name], buildSelector, artifactName);
}

function jobUrlFromCandidate(job: JenkinsJobCandidate): string {
  const jobUrl = normalizeJenkinsUrl(job.url);
  if (jobUrl) {
    return jobUrl.replace(/\/+$/, '');
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

function buildJenkinsJobApiUrl(jenkinsUrl: string, jobPath: string, apiPath = 'lastBuild/api/json'): string {
  const baseUrl = jenkinsUrl.trim().replace(/\/+$/, '');
  const parts = jobPath.split('/').map((part) => part.trim()).filter(Boolean);
  const suffix = apiPath.split('/').map(encodeURIComponent).join('/');
  if (!baseUrl || !parts.length) {
    throw new Error('Configure codeguardian.jenkinsUrl and codeguardian.jenkinsJobPath first.');
  }
  return `${baseUrl}/${jenkinsJobUrlPath(parts)}/${suffix}`;
}

async function fetchJenkinsBuild(apiUrl: string): Promise<JenkinsBuild> {
  const tree = 'number,building,result,timestamp,duration,estimatedDuration,url,artifacts[fileName,relativePath],actions[lastBuiltRevision[SHA1],buildsByBranchName[*],parameters[name,value]]';
  const separator = apiUrl.includes('?') ? '&' : '?';
  return await downloadJson(`${apiUrl}${separator}tree=${encodeURIComponent(tree)}`) as JenkinsBuild;
}

function jenkinsStatusFromBuild(build: JenkinsBuild, jobUrl: string): JenkinsWatchStatus {
  const artifact = findCodeGuardianArtifact(build);
  const buildNumber = build.number;
  const base: JenkinsWatchStatus = {
    state: 'unknown',
    message: 'Jenkins build status unknown.',
    buildNumber,
    jobUrl,
    artifactReady: false,
    lastUpdatedAt: Date.now(),
  };

  if (!buildNumber) {
    return { ...base, state: 'queued', message: 'Waiting for Jenkins build to start.' };
  }
  if (build.building) {
    const progress = estimatedBuildProgress(build);
    return {
      ...base,
      state: 'running',
      progress,
      message: progress ? `Build #${buildNumber} running - ${progress}%` : `Build #${buildNumber} running.`,
    };
  }
  if (build.result === 'SUCCESS') {
    if (artifact?.relativePath) {
      const buildUrl = (normalizeJenkinsUrl(build.url) || `${normalizeJenkinsUrl(jobUrl)?.replace(/\/+$/, '') || jobUrl.replace(/\/+$/, '')}/${build.number}`).replace(/\/+$/, '');
      return {
        ...base,
        state: 'artifact_ready',
        progress: 100,
        artifactReady: true,
        artifactUrl: `${buildUrl}/artifact/${artifact.relativePath.split('/').map(encodeURIComponent).join('/')}`,
        message: `Build #${buildNumber} artifact ready.`,
      };
    }
    return {
      ...base,
      state: 'success',
      progress: 100,
      message: `Build #${buildNumber} completed but codeguardian-results.json was not archived.`,
    };
  }
  if (['FAILURE', 'ABORTED', 'UNSTABLE'].includes(String(build.result || '').toUpperCase())) {
    return {
      ...base,
      state: 'failed',
      progress: 100,
      message: `Build #${buildNumber} ${build.result}.`,
    };
  }
  return {
    ...base,
    state: 'queued',
    message: `Build #${buildNumber} queued.`,
  };
}

function findCodeGuardianArtifact(build: JenkinsBuild): JenkinsArtifact | undefined {
  const artifactName = configValue('jenkinsArtifactName').trim() || 'codeguardian-results.json';
  return (build.artifacts || []).find((item) =>
    item.fileName === artifactName || Boolean(item.relativePath?.endsWith(artifactName))
  );
}

function estimatedBuildProgress(build: JenkinsBuild): number | undefined {
  if (!build.timestamp || !build.estimatedDuration || build.estimatedDuration <= 0) {
    return undefined;
  }
  const elapsed = Date.now() - build.timestamp;
  return Math.max(1, Math.min(95, Math.floor((elapsed / build.estimatedDuration) * 100)));
}

async function readyArtifactForJob(
  jobUrl: string,
  throwWhenNotReady: boolean,
): Promise<{ artifactUrl: string; buildKey: string; buildNumber?: string; commit?: string } | undefined> {
  const normalizedJobUrl = normalizeJenkinsUrl(jobUrl) || jobUrl;
  const buildSelector = configValue('jenkinsBuildSelector').trim() || 'lastBuild';
  const artifactName = configValue('jenkinsArtifactName').trim() || 'codeguardian-results.json';
  const apiUrl = `${normalizedJobUrl.replace(/\/+$/, '')}/${encodeURIComponent(buildSelector)}/api/json?tree=building,result,number,url,artifacts[fileName,relativePath],actions[lastBuiltRevision[SHA1],buildsByBranchName[*],parameters[name,value]]`;
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

  const buildUrl = (normalizeJenkinsUrl(build.url) || `${normalizedJobUrl.replace(/\/+$/, '')}/${build.number}`).replace(/\/+$/, '');
  return {
    artifactUrl: `${buildUrl}/artifact/${artifact.relativePath.split('/').map(encodeURIComponent).join('/')}`,
    buildKey: `${normalizedJobUrl}#${build.number || buildSelector}`,
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
  return parts.map((part) => `job/${encodeURIComponent(decodePathSegment(part))}`).join('/');
}

function decodePathSegment(part: string): string {
  try {
    return decodeURIComponent(part);
  } catch {
    return part;
  }
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
    jenkinsWatch: data.jenkinsWatch,
    applyAllowed: data.applyAllowed,
    applyDisabledReason: data.applyDisabledReason,
    credentialStatus: data.credentialStatus,
    selectedPr: data.selectedPr,
    profile: data.profile,
    gitState: data.gitState,
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
      gap: 8px;
      padding: 8px;
    }
    .header-card {
      border: 1px solid var(--vscode-panel-border);
      border-radius: 6px;
      padding: 6px;
      background: var(--vscode-editorWidget-background);
      display: flex;
      flex-direction: column;
      gap: 6px;
    }
    .context-row {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto auto;
      gap: 8px;
      align-items: center;
    }
    .context-line {
      color: var(--vscode-sideBarTitle-foreground);
      font-weight: 700;
      font-size: 13px;
      line-height: 18px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .actions-row {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 6px;
    }
    .next-step {
      color: var(--vscode-descriptionForeground);
      font-size: 12px;
      line-height: 1.35;
      padding-top: 2px;
    }
    .wide-action {
      grid-column: 1 / -1;
    }
    .pr-row {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 6px;
      grid-column: 1 / -1;
    }
    .compact-action {
      min-width: 72px;
      white-space: nowrap;
    }
    .banner {
      padding: 0;
      display: flex;
      flex-wrap: wrap;
      gap: 5px;
      align-items: center;
      max-height: 52px;
      overflow: hidden;
    }
    .banner-tag {
      border: 1px solid var(--vscode-badge-background);
      border-radius: 999px;
      padding: 2px 8px;
      font-size: 11px;
      font-weight: 600;
      line-height: 16px;
      color: var(--vscode-badge-foreground);
      background: var(--vscode-badge-background);
      white-space: nowrap;
    }
    .banner-tag.success {
      border-color: var(--vscode-testing-iconPassed);
      background: color-mix(in srgb, var(--vscode-testing-iconPassed) 24%, transparent);
      color: var(--vscode-foreground);
    }
    .banner-tag.warning {
      border-color: var(--vscode-editorWarning-foreground);
      background: color-mix(in srgb, var(--vscode-editorWarning-foreground) 24%, transparent);
      color: var(--vscode-foreground);
    }
    .banner-tag.error {
      border-color: var(--vscode-testing-iconFailed);
      background: color-mix(in srgb, var(--vscode-testing-iconFailed) 24%, transparent);
      color: var(--vscode-foreground);
    }
    .banner-tag.unknown {
      border-color: var(--vscode-descriptionForeground);
      background: var(--vscode-input-background);
      color: var(--vscode-descriptionForeground);
    }
    .build-status {
      padding: 0;
      display: flex;
      flex-direction: column;
      gap: 4px;
    }
    .build-line {
      display: flex;
      align-items: center;
      gap: 6px;
      color: var(--vscode-descriptionForeground);
      font-size: 12px;
      min-width: 0;
    }
    .build-line .message {
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .progress {
      height: 5px;
      border-radius: 999px;
      overflow: hidden;
      background: var(--vscode-editorWidget-border, var(--vscode-panel-border));
    }
    .progress-fill {
      height: 100%;
      width: 0;
      background: var(--vscode-progressBar-background, var(--vscode-button-background));
      transition: width .2s ease;
    }
    .button {
      border: 1px solid var(--vscode-button-border, var(--vscode-panel-border));
      color: var(--vscode-button-secondaryForeground);
      background: var(--vscode-button-secondaryBackground);
      border-radius: 3px;
      padding: 5px 8px;
      cursor: pointer;
      text-align: center;
    }
    .button:hover {
      background: var(--vscode-button-secondaryHoverBackground);
    }
    .button:focus {
      outline: 1px solid var(--vscode-focusBorder);
      outline-offset: 1px;
    }
    .summary {
      color: var(--vscode-descriptionForeground);
      font-size: 12px;
      line-height: 1.35;
    }
    .filters {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 6px;
    }
    .filter-bar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      color: var(--vscode-descriptionForeground);
      font-size: 12px;
    }
    .filter-actions {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }
    .advanced-header {
      border: 0;
      padding: 0;
      color: var(--vscode-textLink-foreground);
      background: transparent;
      cursor: pointer;
      text-align: left;
      font-size: 12px;
    }
    .advanced-header:hover {
      color: var(--vscode-textLink-activeForeground);
    }
    .advanced-filters.collapsed {
      display: none;
    }
    .link-button {
      border: 0;
      padding: 0;
      color: var(--vscode-textLink-foreground);
      background: transparent;
      cursor: pointer;
    }
    .link-button:hover {
      color: var(--vscode-textLink-activeForeground);
    }
    .link-button:disabled {
      color: var(--vscode-disabledForeground);
      cursor: not-allowed;
    }
    .tabs {
      display: flex;
      gap: 4px;
      overflow-x: auto;
      padding-bottom: 1px;
    }
    .filter-label {
      color: var(--vscode-descriptionForeground);
      font-size: 11px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: .04em;
      margin-top: -2px;
    }
    .tab {
      border: 1px solid var(--vscode-button-border, var(--vscode-panel-border));
      border-radius: 999px;
      color: var(--vscode-button-secondaryForeground);
      background: var(--vscode-button-secondaryBackground);
      padding: 3px 9px;
      cursor: pointer;
      white-space: nowrap;
      font-size: 12px;
    }
    .tab:hover {
      background: var(--vscode-button-secondaryHoverBackground);
    }
    .tab.active {
      color: var(--vscode-button-foreground);
      background: var(--vscode-button-background);
      border-color: var(--vscode-focusBorder);
    }
    #search,
    .filters input,
    .filters select {
      min-width: 0;
      border: 1px solid var(--vscode-input-border, transparent);
      color: var(--vscode-input-foreground);
      background: var(--vscode-input-background);
      border-radius: 3px;
      padding: 4px 6px;
    }
    #search {
      width: 100%;
      box-sizing: border-box;
    }
    .filters-header {
      margin-top: 2px;
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
    .section-title.collapsible {
      cursor: pointer;
    }
    .section-title-main {
      display: flex;
      align-items: center;
      gap: 6px;
      min-width: 0;
    }
    .section-chevron {
      color: var(--vscode-descriptionForeground);
      font-size: 12px;
      width: 10px;
    }
    .file-tree-list {
      display: flex;
      flex-direction: column;
      gap: 4px;
      padding-top: 6px;
    }
    .file-tree-item {
      display: flex;
      justify-content: space-between;
      gap: 8px;
      color: var(--vscode-descriptionForeground);
      font-size: 12px;
      line-height: 1.35;
    }
    .file-tree-item span:first-child {
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .section-heading {
      display: flex;
      flex-direction: column;
      gap: 2px;
    }
    .section-subtitle {
      color: var(--vscode-descriptionForeground);
      font-size: 11px;
      font-weight: 400;
      text-transform: none;
      letter-spacing: 0;
    }
    .recommended {
      border: 1px solid var(--vscode-panel-border);
      border-radius: 6px;
      background: var(--vscode-editorWidget-background);
      padding: 6px;
      margin-top: 4px;
    }
    .recommended .section-title {
      margin-top: 0;
      border-top: 0;
      padding-top: 0;
    }
    .recommended-list {
      display: flex;
      flex-direction: column;
      gap: 3px;
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
      grid-template-columns: 16px 18px minmax(0, 1fr) auto;
      gap: 4px;
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
      font-weight: 500;
    }
    .compact-path {
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
      white-space: nowrap;
    }
    .status.applied {
      color: var(--vscode-testing-iconPassed);
    }
    .status.changed {
      color: var(--vscode-testing-iconFailed);
    }
    .status.dismissed {
      color: var(--vscode-descriptionForeground);
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
      display: -webkit-box;
      -webkit-line-clamp: 2;
      -webkit-box-orient: vertical;
      overflow: hidden;
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
      border-color: var(--vscode-panel-border);
      color: var(--vscode-disabledForeground);
      background: var(--vscode-input-background);
      opacity: 1;
      cursor: not-allowed;
    }
    .button:disabled:hover {
      background: var(--vscode-input-background);
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
    .results-placeholder {
      border: 1px solid var(--vscode-panel-border);
      border-radius: 6px;
      padding: 12px;
      background: var(--vscode-editorWidget-background);
      color: var(--vscode-descriptionForeground);
      line-height: 1.4;
    }
    .results-placeholder h3 {
      margin: 0 0 6px;
      color: var(--vscode-sideBarTitle-foreground);
      font-size: 13px;
    }
    .results-placeholder p {
      margin: 0;
    }
    .show-more {
      width: 100%;
      margin-top: 8px;
    }
    .paging-info {
      color: var(--vscode-descriptionForeground);
      font-size: 12px;
      margin-top: 6px;
    }
  </style>
</head>
<body>
  <div class="shell">
    <div class="header-card">
      <div class="context-row">
        <div class="context-line" id="contextLine">CodeGuardian &middot; PR: Not selected &middot; Profile: default</div>
        <button class="button dashboard-action compact-action" id="openPr" disabled>Open PR</button>
        <button class="button secondary compact-action" id="selectPr">Select PR</button>
      </div>
      <div class="banner" id="artifactBanner"></div>
      <div class="build-status" id="buildStatus"></div>
      <div class="actions-row">
        <button class="button dashboard-action" id="refresh">Refresh</button>
        <button class="button dashboard-action" id="download">Download Results</button>
        <button class="button secondary" id="openGitDiff">Open Git Diff</button>
        <button class="button" id="applySelected" disabled>Apply Selected</button>
        <button class="button secondary" id="undoSelected" disabled>Undo Selected</button>
        <button class="button secondary" id="openLog">Activity Log</button>
      </div>
      <div class="selection-summary" id="selectionSummary"></div>
      <div class="next-step" id="nextStep"></div>
    </div>
    <div id="recommended"></div>
    <div class="summary" id="summary"></div>
    <div class="filter-bar filters-header"><span id="filterCount">Filters &middot; 0 active</span><div class="filter-actions"><button class="link-button" id="clearFilters">Clear filters</button><button class="link-button" id="clearDismissed">Clear dismissed</button></div></div>
    <input class="wide" id="search" type="search" placeholder="Search file">
    <div class="filter-label">Filter by type</div>
    <div class="tabs" id="tabs"></div>
    <button class="advanced-header" id="advancedToggle">Advanced filters</button>
    <div class="filters advanced-filters collapsed" id="advancedFilters">
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
      tab: 'all',
      limit: 0,
      currentFile: false,
      showDismissed: false,
      advancedExpanded: false,
      fileTreeExpanded: false,
      allSuggestionsExpanded: true,
      visibleLimit: 50,
      expandedId: '',
      scrollToId: '',
      statuses: {},
      selectedIds: new Set(),
      busyIds: new Set()
    };
    const dismissedIds = new Set(state.dismissedIds || []);

    const byId = (id) => document.getElementById(id);
    const norm = (value) => String(value || '').toLowerCase();
    const titleOf = (item) => item.target_name || item.problem || item.solution || item.id;
    const severityOf = (item) => item.severity || item.source || 'info';
    let artifactReadyDownloadRequested = false;

    byId('download').addEventListener('click', () => {
      if (isDownloadArtifactBlocked()) return;
      vscode.postMessage({ command: 'download' });
    });
    byId('refresh').addEventListener('click', () => vscode.postMessage({ command: 'refresh' }));
    byId('selectPr').addEventListener('click', () => vscode.postMessage({ command: 'selectPr' }));
    byId('openPr').addEventListener('click', () => vscode.postMessage({ command: 'openPr' }));
    byId('applySelected').addEventListener('click', () => {
      const ids = Array.from(filters.selectedIds);
      const statuses = statusesFor(ids);
      ids.forEach((id) => filters.busyIds.add(id));
      render();
      vscode.postMessage({ command: 'applySelected', ids, statuses });
    });
    byId('undoSelected').addEventListener('click', () => {
      const ids = Array.from(filters.selectedIds);
      const statuses = statusesFor(ids);
      ids.forEach((id) => filters.busyIds.add(id));
      render();
      vscode.postMessage({ command: 'undoSelected', ids, statuses });
    });
    byId('openGitDiff').addEventListener('click', () => vscode.postMessage({ command: 'openGitDiff' }));
    byId('openLog').addEventListener('click', () => vscode.postMessage({ command: 'openLog' }));
    byId('clearDismissed').addEventListener('click', () => vscode.postMessage({ command: 'clearDismissed' }));
    byId('clearFilters').addEventListener('click', () => clearFilters());
    byId('advancedToggle').addEventListener('click', () => {
      filters.advancedExpanded = !filters.advancedExpanded;
      render();
    });
    window.addEventListener('message', (event) => {
      const message = event.data;
      if (message?.command === 'statuses' && message.statuses) {
        filters.statuses = message.statuses;
        render();
      }
      if (message?.command === 'operationComplete') {
        filters.statuses = { ...filters.statuses, ...(message.statuses || {}) };
        for (const id of message.ids || []) {
          filters.busyIds.delete(id);
        }
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
          filters.visibleLimit = 50;
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
        { value: 'open', label: 'Ready' },
        { value: 'applied', label: 'Applied' },
        { value: 'changed', label: 'Needs refresh' },
      ]);
      renderTabs();
    }

    function fillSelect(id, options) {
      byId(id).innerHTML = options.map((option) => '<option value="' + escapeHtml(option.value) + '">' + escapeHtml(option.label) + '</option>').join('');
    }

    function renderTabs() {
      const tabs = [
        ['all', 'All'],
        ['issues', 'Issues'],
        ['optimizations', 'Optimizations'],
        ['applied', 'Applied'],
        ['changed', 'Changed'],
        ['dismissed', 'Dismissed'],
      ];
      byId('tabs').innerHTML = tabs.map(([value, label]) =>
        '<button class="tab' + (filters.tab === value ? ' active' : '') + '" data-tab="' + value + '">' + label + '</button>'
      ).join('');
      for (const tab of byId('tabs').querySelectorAll('.tab')) {
        tab.addEventListener('click', () => {
          filters.tab = tab.getAttribute('data-tab') || 'all';
          filters.visibleLimit = 50;
          if (filters.tab === 'dismissed') {
            byId('showDismissed').checked = true;
            filters.showDismissed = true;
          }
          render();
        });
      }
    }

    function filteredSuggestions() {
      const search = norm(filters.search);
      const profile = state.profile || {};
      let items = state.suggestions.filter((item) => {
        const itemStatus = statusOf(item.id, item);
        if (profile.showOptimizations === false && filters.tab === 'all' && isOptimization(item)) return false;
        if (!filters.showDismissed && dismissedIds.has(item.id)) return false;
        if (filters.tab === 'issues' && isOptimization(item)) return false;
        if (filters.tab === 'optimizations' && !isOptimization(item)) return false;
        if (filters.tab === 'applied' && itemStatus !== 'applied') return false;
        if (filters.tab === 'changed' && itemStatus !== 'changed') return false;
        if (filters.tab === 'dismissed' && !dismissedIds.has(item.id)) return false;
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

    function isOptimization(item) {
      return norm(item.source) === 'optimization';
    }

    function render() {
      renderTopActions();
      renderFilterCount();
      renderAdvancedFilters();
      renderTabs();
      renderArtifactBanner();
      renderJenkinsBuildStatus();
      renderSelectionSummary();
      renderNextStep();
      renderSummary();
      if (shouldHideSuggestions()) {
        filters.expandedId = '';
        byId('recommended').innerHTML = '';
        byId('list').innerHTML = staleResultsPlaceholder();
        return;
      }
      const items = filteredSuggestions();
      const visibleItems = items.slice(0, filters.visibleLimit);
      if (filters.expandedId && !items.find((item) => item.id === filters.expandedId)) {
        filters.expandedId = '';
      }
      renderRecommended(items);
      renderList(visibleItems, items.length);
    }

    function shouldHideSuggestions() {
      const watchState = norm((state.jenkinsWatch || {}).state);
      const artifactState = norm((state.artifact || {}).validation);
      if (hasCurrentValidArtifact()) {
        return false;
      }
      return ['waiting', 'waiting_pr', 'waiting_job', 'queued', 'running'].includes(watchState) ||
        artifactState === 'stale' ||
        artifactState === 'mismatch';
    }

    function hasCurrentValidArtifact() {
      const artifact = state.artifact || {};
      return norm(artifact.validation) === 'valid' && norm(artifact.status || 'downloaded') === 'downloaded';
    }

    function staleResultsPlaceholder() {
      const watchState = norm((state.jenkinsWatch || {}).state);
      const artifactState = norm((state.artifact || {}).validation);
      if (watchState === 'failed') {
        return placeholderPanel('Jenkins build failed', 'Jenkins build failed. Latest suggestions are not available.');
      }
      if (artifactState === 'stale' || artifactState === 'mismatch') {
        return placeholderPanel('Waiting for latest CodeGuardian results', 'Results are outdated for the current commit. Wait for Jenkins or download valid results.');
      }
      return placeholderPanel('Waiting for latest CodeGuardian results', 'Jenkins is analyzing the current PR. Suggestions will appear when the new results artifact is available.');
    }

    function placeholderPanel(title, text) {
      return '<div class="results-placeholder"><h3>' + escapeHtml(title) + '</h3><p>' + escapeHtml(text) + '</p></div>';
    }

    function renderTopActions() {
      const download = byId('download');
      const blocked = isDownloadArtifactBlocked();
      download.disabled = blocked;
      download.title = blocked ? 'Blocked while Jenkins build is running' : 'Download latest CodeGuardian artifact';

      const selectPr = byId('selectPr');
      const openPr = byId('openPr');
      const openGitDiff = byId('openGitDiff');
      const contextLine = byId('contextLine');
      const selectedPr = state.selectedPr || {};
      const hasPr = Boolean(selectedPr.id);
      const profileName = (state.profile || {}).profile || 'default';
      const prLabel = hasPr ? truncatePrLabel(selectedPr.title || ('#' + selectedPr.id)) : 'Not selected';
      contextLine.textContent = joinStatus(['CodeGuardian', 'PR: ' + prLabel, 'Profile: ' + profileName]);
      contextLine.title = hasPr ? 'PR #' + selectedPr.id + (selectedPr.title ? ': ' + selectedPr.title : '') + ' | Profile: ' + profileName : 'No pull request selected | Profile: ' + profileName;
      selectPr.textContent = hasPr ? 'Change PR' : 'Select PR';
      selectPr.title = hasPr
        ? 'PR #' + selectedPr.id + (selectedPr.title ? ': ' + selectedPr.title : '')
        : 'Select pull request';
      selectPr.className = hasPr ? 'button dashboard-action' : 'button';
      openPr.disabled = !hasPr;
      openPr.title = hasPr ? 'Open PR #' + selectedPr.id + (selectedPr.title ? ': ' + selectedPr.title : '') : 'No pull request selected';
      const gitState = state.gitState || {};
      openGitDiff.disabled = !gitState.isGitRepository || !gitState.hasChanges;
      openGitDiff.title = !gitState.isGitRepository
        ? 'Current workspace is not a Git repository'
        : gitState.hasChanges ? gitState.message : 'No local Git changes to show';

      const clearDismissed = byId('clearDismissed');
      clearDismissed.disabled = dismissedIds.size === 0;
      clearDismissed.title = dismissedIds.size === 0 ? 'No dismissed suggestions' : 'Clear locally dismissed suggestions';
    }

    function isDownloadArtifactBlocked() {
      return ['running', 'queued', 'waiting', 'waiting_job', 'waiting_pr'].includes(norm((state.jenkinsWatch || {}).state)) &&
        !hasCurrentValidArtifact();
    }

    function truncatePrLabel(value) {
      const text = String(value || '').trim();
      return text.length > 34 ? text.slice(0, 31).trimEnd() + '...' : text;
    }

    function renderFilterCount() {
      const count = activeFilterCount();
      byId('filterCount').textContent = 'Filters ' + String.fromCharCode(183) + ' ' + count + ' active';
      byId('clearFilters').disabled = count === 0;
    }

    function renderAdvancedFilters() {
      byId('advancedFilters').className = 'filters advanced-filters' + (filters.advancedExpanded ? '' : ' collapsed');
      byId('advancedToggle').textContent = (filters.advancedExpanded ? String.fromCharCode(9662) : String.fromCharCode(8250)) + ' Advanced filters';
    }

    function activeFilterCount() {
      return [
        filters.search.trim(),
        filters.severity !== 'all',
        filters.source !== 'all',
        filters.status !== 'all',
        filters.currentFile,
        filters.showDismissed,
        filters.tab !== 'all',
      ].filter(Boolean).length;
    }

    function clearFilters() {
      filters.search = '';
      filters.severity = 'all';
      filters.source = 'all';
      filters.status = 'all';
      filters.currentFile = false;
      filters.showDismissed = false;
      filters.advancedExpanded = false;
      filters.tab = 'all';
      filters.visibleLimit = 50;
      byId('search').value = '';
      byId('severity').value = 'all';
      byId('source').value = 'all';
      byId('status').value = 'all';
      byId('currentFile').checked = false;
      byId('showDismissed').checked = false;
      render();
    }

    function renderArtifactBanner() {
      const artifact = state.artifact || {};
      const downloadedAt = artifact.downloadedAt ? new Date(artifact.downloadedAt).toLocaleString() : 'not downloaded';
      const validation = norm(artifact.validation || 'unknown');
      const commit = artifact.commit || '';
      const localCommit = artifact.localCommit || '';
      const commitMatches = commit && localCommit && (commit.startsWith(localCommit) || localCommit.startsWith(commit));
      const repoMessage = artifact.message || '';
      const credentials = state.credentialStatus || { configured: false, source: 'missing', message: 'Credentials: missing' };
      const tags = [
        bannerTag('Artifact ' + artifactStatusText(validation), artifactClass(validation, artifact.status), (artifact.validation || 'unknown').toUpperCase()),
        bannerTag('Commit ' + (commit && commitMatches ? 'OK' : commit ? 'check' : 'unknown'), commit ? (commitMatches ? 'success' : validation === 'stale' ? 'error' : 'warning') : 'warning', commit ? shortHash(commit) + (commitMatches ? ', matches local HEAD' : localCommit ? ', local HEAD is ' + shortHash(localCommit) : '') : 'missing commit metadata'),
        bannerTag('Credentials ' + (credentials.configured ? 'OK' : 'missing'), credentialClass(credentials.source), credentials.message || 'Credentials: missing'),
        bannerTag(artifact.downloadedAt ? 'Downloaded' : 'Not downloaded', artifact.downloadedAt ? 'success' : 'unknown', downloadedAt),
        bannerTag('Repo ' + repoStatusText(validation, repoMessage), repoClass(validation, repoMessage), repoMessage || 'repository metadata unavailable')
      ];
      byId('artifactBanner').innerHTML = tags.join('');
    }

    function artifactStatusText(validation) {
      if (validation === 'valid') return 'valid';
      if (validation === 'stale') return 'stale';
      if (validation === 'mismatch') return 'mismatch';
      return 'unknown';
    }

    function repoStatusText(validation, message) {
      if (validation === 'valid') return 'OK';
      if (validation === 'mismatch') return 'mismatch';
      if (norm(message).includes('missing repository')) return 'unknown';
      return validation === 'unknown' ? 'unknown' : 'check';
    }

    function bannerTag(label, tone, title) {
      return '<span class="banner-tag ' + escapeHtml(tone) + '" title="' + escapeHtml(title) + '">' + escapeHtml(label) + '</span>';
    }

    function artifactClass(validation, status) {
      if (validation === 'valid') return 'success';
      if (validation === 'mismatch' || status === 'error') return 'error';
      if (validation === 'stale' || validation === 'unknown') return 'warning';
      return 'unknown';
    }

    function credentialClass(source) {
      if (source === 'secretStorage' || source === 'env') return 'success';
      if (source === 'settings') return 'warning';
      if (source === 'missing') return 'error';
      return 'unknown';
    }

    function buildClass(watch, artifact) {
      if (watch.state === 'artifact_ready' || watch.state === 'artifact_downloaded') return 'success';
      if (watch.state === 'running' || watch.state === 'queued' || watch.state === 'waiting_job' || watch.state === 'waiting_pr') return 'warning';
      if (watch.state === 'failed' || watch.state === 'timeout' || watch.state === 'error' || artifact.status === 'error') return 'error';
      return artifact.buildNumber ? 'success' : 'unknown';
    }

    function buildTooltip(watch, artifact) {
      if (watch.buildNumber) {
        return 'Build #' + watch.buildNumber + (watch.progress ? ' running, estimated progress ' + watch.progress + '%' : '') + (watch.message ? ' - ' + watch.message : '');
      }
      return artifact.buildNumber ? 'Build ' + artifact.buildNumber : 'Build unavailable';
    }

    function repoClass(validation, message) {
      if (validation === 'mismatch') return 'error';
      if (validation === 'valid') return 'success';
      if (norm(message).includes('missing repository')) return 'warning';
      return validation === 'unknown' ? 'warning' : 'unknown';
    }

    function renderJenkinsBuildStatus() {
      const watch = state.jenkinsWatch || { state: 'idle', message: 'Jenkins watcher idle.', artifactReady: false };
      if (norm(watch.state) === 'artifact_ready' && state.autoDownload && watch.artifactUrl && !artifactReadyDownloadRequested) {
        artifactReadyDownloadRequested = true;
        vscode.postMessage({ command: 'downloadReadyArtifact' });
      }
      if (norm(watch.state) !== 'artifact_ready') {
        artifactReadyDownloadRequested = false;
      }
      const tone = jenkinsTone(watch.state);
      const status = jenkinsDisplayText(watch);
      const tooltip = jenkinsTooltip(watch);
      const progress = shouldShowProgress(watch)
        ? '<div class="progress" title="' + escapeHtml(String(watch.progress) + '%') + '"><div class="progress-fill" style="width:' + Math.max(0, Math.min(100, watch.progress)) + '%"></div></div>'
        : '';
      byId('buildStatus').innerHTML =
        '<div class="build-line ' + tone + '" title="' + escapeHtml(tooltip) + '">' +
        '<span class="message">' + escapeHtml(status) + '</span>' +
        '</div>' + progress;
    }

    function shouldShowProgress(watch) {
      return norm(watch.state) === 'running' && typeof watch.progress === 'number';
    }

    function jenkinsDisplayText(watch) {
      const value = norm(watch.state);
      const build = watch.buildNumber ? 'Build #' + watch.buildNumber : '';
      if (value === 'idle' || (!watch.buildNumber && !['waiting_pr', 'waiting_job', 'queued', 'running'].includes(value))) return joinStatus(['Jenkins', 'Idle']);
      if (value === 'waiting_pr' || value === 'waiting_job' || value === 'queued') return joinStatus(['Jenkins', 'Waiting']);
      if (value === 'running') return joinStatus(['Jenkins', build, typeof watch.progress === 'number' ? 'Running ' + watch.progress + '%' : 'Running']);
      if (value === 'artifact_ready') return joinStatus(['Jenkins', build, 'Artifact ready']);
      if (value === 'artifact_downloaded') return joinStatus(['Jenkins', build, 'Results downloaded']);
      if (value === 'failed') return joinStatus(['Jenkins', build, 'Failed']);
      if (value === 'timeout') return joinStatus(['Jenkins', 'Watch timeout']);
      if (value === 'error') return joinStatus(['Jenkins', 'Unavailable']);
      if (value === 'success') return joinStatus(['Jenkins', build, 'Completed']);
      return joinStatus(['Jenkins', 'Idle']);
    }

    function joinStatus(parts) {
      return parts.filter(Boolean).join(' ' + String.fromCharCode(183) + ' ');
    }

    function jenkinsTooltip(watch) {
      return norm(watch.state) === 'idle' ? 'No active PR build detected.' : (watch.message || jenkinsDisplayText(watch));
    }

    function jenkinsTone(state) {
      const value = norm(state);
      if (['success', 'artifact_ready', 'artifact_downloaded'].includes(value)) return 'success';
      if (['running', 'queued', 'waiting_pr', 'waiting_job'].includes(value)) return 'warning';
      if (['failed', 'timeout', 'error'].includes(value)) return 'error';
      return 'unknown';
    }

    function renderSelectionSummary() {
      const selected = Array.from(filters.selectedIds);
      const summary = selectionSummary(selected);
      byId('selectionSummary').textContent = selected.length
        ? summary.selected + ' selected - ' + summary.ready + ' ready - ' + summary.applied + ' applied - ' + summary.skipped + ' skipped' + skippedReasonText(summary)
        : 'No suggestions selected';
      const hidden = shouldHideSuggestions();
      byId('applySelected').disabled = hidden || summary.ready === 0 || !state.applyAllowed;
      byId('applySelected').title = hidden ? 'Latest CodeGuardian results are not ready' : state.applyAllowed ? '' : state.applyDisabledReason;
      byId('undoSelected').disabled = summary.applied === 0 || !state.applyAllowed;
      byId('undoSelected').title = state.applyAllowed ? '' : state.applyDisabledReason;
    }

    function renderNextStep() {
      byId('nextStep').textContent = 'Next step: ' + nextStepMessage();
    }

    function nextStepMessage() {
      const watchState = norm((state.jenkinsWatch || {}).state);
      const artifactState = norm((state.artifact || {}).validation);
      if (artifactState === 'stale' || artifactState === 'mismatch') {
        return 'download valid results for the current commit.';
      }
      if (!hasCurrentValidArtifact() && ['running', 'queued', 'waiting', 'waiting_pr', 'waiting_job'].includes(watchState)) {
        return 'wait for Jenkins to finish. Results will be available when the build completes.';
      }
      if (watchState === 'failed') {
        return 'refresh or wait for a successful Jenkins build.';
      }
      if (!state.suggestions.length) {
        return 'refresh results or check Jenkins build status.';
      }
      if (filters.selectedIds.size > 0) {
        return 'review the diff or apply selected suggestions.';
      }
      if (state.suggestions.some((item) => statusOf(item.id, item) === 'applied')) {
        return 'review Git diff or run project checks.';
      }
      if (state.suggestions.some((item) => statusOf(item.id, item) === 'open' && !dismissedIds.has(item.id))) {
        return 'review the recommended fixes.';
      }
      return 'refresh results or check Jenkins build status.';
    }

    function selectionSummary(ids) {
      const summary = { selected: ids.length, ready: 0, applied: 0, changed: 0, dismissed: 0, skipped: 0 };
      for (const id of ids) {
        const item = state.suggestions.find((candidate) => candidate.id === id);
        const status = statusOf(id, item);
        if (dismissedIds.has(id)) summary.dismissed += 1;
        if (status === 'applied') summary.applied += 1;
        if (status === 'changed') summary.changed += 1;
        if (status === 'open' && !dismissedIds.has(id)) summary.ready += 1;
      }
      summary.skipped = summary.selected - summary.ready;
      return summary;
    }

    function skippedReasonText(summary) {
      const reasons = [];
      if (summary.applied) reasons.push(summary.applied + ' applied');
      if (summary.changed) reasons.push(summary.changed + ' needs refresh');
      if (summary.dismissed) reasons.push(summary.dismissed + ' dismissed');
      return reasons.length ? ' (' + reasons.join(', ') + ')' : '';
    }

    function renderSummary() {
      const total = state.suggestions.length;
      const critical = state.suggestions.filter((item) => ['blocker', 'critical'].includes(norm(item.severity))).length;
      const optimization = state.suggestions.filter((item) => norm(item.source) === 'optimization').length;
      const visible = filteredSuggestions().length;
      const dismissed = dismissedIds.size;
      byId('summary').textContent =
        total + ' total ' + String.fromCharCode(183) + ' ' +
        critical + ' critical ' + String.fromCharCode(183) + ' ' +
        optimization + ' optimizations ' + String.fromCharCode(183) + ' ' +
        visible + ' visible ' + String.fromCharCode(183) + ' ' +
        dismissed + ' dismissed';
    }

    function renderList(items, totalFiltered) {
      if (!state.suggestions.length) {
        byId('list').innerHTML = '<div class="empty">No local results found. Use Download Results or configure codeguardian.resultsFile.</div>';
        return;
      }
      if (!items.length) {
        byId('list').innerHTML = '<div class="empty">No suggestions match the current filters.</div>';
        return;
      }
      const paging = '<div class="paging-info">Showing ' + items.length + ' of ' + totalFiltered + ' suggestions</div>' +
        (items.length < totalFiltered ? '<button class="button secondary show-more" id="showMore">Show 50 more</button>' : '');
      byId('list').innerHTML = allSuggestionsBlock(items, items) + fileTreeBlock(items) + paging;
      for (const item of items) {
        wireSuggestionControls(item, '');
      }
      byId('showMore')?.addEventListener('click', () => {
        filters.visibleLimit += 50;
        render();
      });
      byId('fileTreeHeader')?.addEventListener('click', () => {
        filters.fileTreeExpanded = !filters.fileTreeExpanded;
        render();
      });
      byId('allSuggestionsHeader')?.addEventListener('click', () => {
        filters.allSuggestionsExpanded = !filters.allSuggestionsExpanded;
        render();
      });
      wireDetail(items, '');
      scrollToExpandedSuggestion();
    }

    function wireSuggestionControls(item, prefix) {
      const row = byId(prefix + 'suggestion-' + item.id);
      if (row) {
        row.addEventListener('click', () => {
          filters.expandedId = filters.expandedId === item.id ? '' : item.id;
          render();
        });
        row.addEventListener('dblclick', () => vscode.postMessage({ command: 'open', id: item.id }));
      }
      const checkbox = byId(prefix + 'select-' + item.id);
      if (checkbox) {
        checkbox.addEventListener('click', (event) => event.stopPropagation());
        checkbox.addEventListener('change', () => {
          if (checkbox.checked) {
            filters.selectedIds.add(item.id);
          } else {
            filters.selectedIds.delete(item.id);
          }
          render();
        });
      }
    }

    function scrollToExpandedSuggestion() {
      if (!filters.scrollToId) return;
      const target = byId('suggestion-' + filters.scrollToId);
      filters.scrollToId = '';
      target?.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }

    function renderRecommended(items) {
      const recommendations = recommendedSuggestions(items);
      if (!recommendations.length) {
        byId('recommended').innerHTML = '';
        return;
      }
      byId('recommended').innerHTML =
        '<section class="recommended">' +
        '<div class="section-title"><div class="section-heading"><span>Recommended fixes</span><span class="section-subtitle">' +
        recommendations.length + ' ready to review</span></div><span class="count">' + recommendations.length + '</span></div>' +
        '<div class="recommended-list">' + recommendations.map((item) => suggestionBlock(item, recommendations, 'rec-')).join('') + '</div></section>';
      for (const item of recommendations) {
        wireSuggestionControls(item, 'rec-');
      }
      wireDetail(recommendations, 'rec-');
    }

    function recommendedSuggestions(items) {
      const maxRecommended = Math.max(0, Number((state.profile || {}).maxRecommended || 3));
      return items
        .filter((item) => statusOf(item.id, item) === 'open' && (!dismissedIds.has(item.id) || filters.showDismissed))
        .sort((a, b) => severityRank(b) - severityRank(a) || sourceRank(b) - sourceRank(a) || Number(a.line || 0) - Number(b.line || 0))
        .slice(0, maxRecommended);
    }

    function sourceRank(item) {
      return isOptimization(item) ? 0 : 1;
    }

    function fileTreeBlock(sectionItems) {
      if (!sectionItems.length) return '';
      const groups = groupByFile(sectionItems);
      const header = collapsibleHeader('fileTreeHeader', 'File Tree', groups.size + ' files', sectionItems.length, filters.fileTreeExpanded);
      if (!filters.fileTreeExpanded) return header;
      return header + '<div class="file-tree-list">' +
        Array.from(groups.entries()).map(([file, values]) =>
          '<div class="file-tree-item"><span title="' + escapeHtml(file) + '">' + escapeHtml(file) + '</span><span class="count">' + values.length + '</span></div>'
        ).join('') +
        '</div>';
    }

    function allSuggestionsBlock(sectionItems, allItems) {
      if (!sectionItems.length) return '';
      const groups = groupByFile(sectionItems);
      const header = collapsibleHeader('allSuggestionsHeader', 'All suggestions', sectionItems.length + ' visible', sectionItems.length, filters.allSuggestionsExpanded);
      if (!filters.allSuggestionsExpanded) return header;
      return header +
        Array.from(groups.entries()).map(([file, values]) => {
          return '<section class="file"><div class="file-header"><span>' + escapeHtml(file) + '</span><span class="count">' + values.length + '</span></div>' +
            values.map((item) => suggestionBlock(item, allItems)).join('') + '</section>';
        }).join('');
    }

    function groupByFile(items) {
      const groups = new Map();
      for (const item of items) {
        if (!groups.has(item.file)) groups.set(item.file, []);
        groups.get(item.file).push(item);
      }
      return groups;
    }

    function collapsibleHeader(id, title, subtitle, count, expanded) {
      const chevron = expanded ? '&#9662;' : '&#8250;';
      return '<div class="section-title collapsible" id="' + id + '">' +
        '<div class="section-title-main"><span class="section-chevron">' + chevron + '</span><div class="section-heading"><span>' + escapeHtml(title) + '</span><span class="section-subtitle">' + escapeHtml(subtitle) + '</span></div></div>' +
        '<span class="count">' + count + '</span></div>';
    }

    function suggestionBlock(item, items, prefix = '') {
      return suggestionRow(item, prefix) + (item.id === filters.expandedId ? detailBlock(item, items, prefix) : '');
    }

    function suggestionRow(item, prefix = '') {
      const selected = item.id === filters.expandedId ? ' selected' : '';
      const chevron = item.id === filters.expandedId ? '&#9662;' : '&#8250;';
      const itemStatus = statusOf(item.id, item);
      const checked = filters.selectedIds.has(item.id) ? ' checked' : '';
      const checkboxDisabled = ['open', 'applied'].includes(itemStatus) && !dismissedIds.has(item.id) ? '' : ' disabled';
      const statusMark = statusLabel(dismissedIds.has(item.id) ? 'dismissed' : itemStatus);
      return '<div id="' + prefix + 'suggestion-' + escapeHtml(item.id) + '" class="suggestion' + selected + '">' +
        '<div class="chevron">' + chevron + '</div>' +
        '<label class="selectbox"><input id="' + prefix + 'select-' + escapeHtml(item.id) + '" type="checkbox"' + checked + checkboxDisabled + '></label>' +
        '<div class="title">' + escapeHtml(titleOf(item)) + '</div>' +
        statusMark +
        '<div class="meta">' +
        '<span class="pill ' + escapeHtml(norm(severityOf(item))) + '">' + escapeHtml(severityOf(item)) + '</span>' +
        '<span class="pill ' + escapeHtml(norm(item.source)) + '">' + escapeHtml(item.source || 'unknown') + '</span>' +
        '<span>L' + escapeHtml(item.line || '-') + '</span>' +
        '<span class="compact-path">' + escapeHtml(compactFile(item.file)) + '</span>' +
        '</div></div>';
    }

    function detailBlock(item, items, prefix = '') {
      const index = items.findIndex((candidate) => candidate.id === item.id);
      const prevDisabled = index <= 0 ? ' disabled' : '';
      const nextDisabled = index >= items.length - 1 ? ' disabled' : '';
      const itemStatus = statusOf(item.id, item);
      const isDismissed = dismissedIds.has(item.id);
      const isBusy = filters.busyIds.has(item.id);
      const applyDisabled = !isBusy && (itemStatus === 'open' || itemStatus === 'applied') && !dismissedIds.has(item.id) && state.applyAllowed ? '' : ' disabled';
      const applyLabel = isBusy ? itemStatus === 'applied' ? 'Undoing...' : 'Applying...' : itemStatus === 'applied' ? 'Undo' : 'Apply';
      const applyCommand = itemStatus === 'applied' ? 'undo' : 'apply';
      const applyTitle = state.applyAllowed ? '' : ' title="' + escapeHtml(state.applyDisabledReason) + '"';
      const dismissCommand = isDismissed ? 'restoreDismissed' : 'dismiss';
      const dismissLabel = isDismissed ? 'Restore' : 'Dismiss';
      const extraEdits = extraEditsSummary(item);
      return '<div class="detail" id="' + prefix + 'detail-' + escapeHtml(item.id) + '"><h3>' + escapeHtml(titleOf(item)) + '</h3>' +
        '<div class="meta"><span>' + escapeHtml(item.file) + ':' + escapeHtml(item.line || '-') + '</span>' + statusLabel(isDismissed ? 'dismissed' : itemStatus) + '<span class="pill ' + escapeHtml(norm(severityOf(item))) + '">' + escapeHtml(severityOf(item)) + '</span><span class="pill ' + escapeHtml(norm(item.source)) + '">' + escapeHtml(item.source || 'unknown') + '</span></div>' +
        '<p><strong>Problem:</strong> ' + escapeHtml(item.problem || '') + '</p>' +
        '<p><strong>Proposal:</strong> ' + escapeHtml(item.solution || '') + '</p>' +
        (extraEdits ? '<p><strong>Additional file changes:</strong> ' + escapeHtml(extraEdits) + '</p>' : '') +
        '<div class="detail-actions">' +
        '<button class="button secondary" id="' + actionId(prefix, 'previous') + '"' + prevDisabled + '>Previous</button>' +
        '<button class="button secondary" id="' + actionId(prefix, 'open') + '">Locate</button>' +
        '<button class="button secondary" id="' + actionId(prefix, 'preview') + '">Details</button>' +
        '<button class="button secondary" id="' + actionId(prefix, 'diff') + '">Diff</button>' +
        '<button class="button" id="' + actionId(prefix, 'apply') + '" data-command="' + applyCommand + '"' + applyTitle + applyDisabled + '>' + applyLabel + '</button>' +
        '<button class="button secondary" id="' + actionId(prefix, 'dismiss') + '" data-command="' + dismissCommand + '">' + dismissLabel + '</button>' +
        '<button class="button secondary" id="' + actionId(prefix, 'next') + '"' + nextDisabled + '>Next</button>' +
        '</div></div>';
    }

    function actionId(prefix, name) {
      return prefix + name;
    }

    function extraEditsSummary(item) {
      const parts = [];
      if (Array.isArray(item.required_imports) && item.required_imports.length) {
        parts.push(item.required_imports.length + ' required import(s)');
      }
      if (Array.isArray(item.optional_removed_imports) && item.optional_removed_imports.length) {
        parts.push(item.optional_removed_imports.length + ' removable import(s)');
      }
      if (Array.isArray(item.auxiliary_edits) && item.auxiliary_edits.length) {
        parts.push(item.auxiliary_edits.length + ' auxiliary edit(s)');
      }
      return parts.join(', ');
    }

    function wireDetail(items, prefix = '') {
      const item = items.find((candidate) => candidate.id === filters.expandedId);
      if (!item || !byId(prefix + 'detail-' + item.id)) return;
      const index = items.findIndex((candidate) => candidate.id === item.id);
      byId(actionId(prefix, 'open'))?.addEventListener('click', () => vscode.postMessage({ command: 'open', id: item.id }));
      byId(actionId(prefix, 'preview'))?.addEventListener('click', () => vscode.postMessage({ command: 'preview', id: item.id }));
      byId(actionId(prefix, 'diff'))?.addEventListener('click', () => vscode.postMessage({ command: 'diff', id: item.id }));
      byId(actionId(prefix, 'dismiss'))?.addEventListener('click', () => {
        const command = byId(actionId(prefix, 'dismiss'))?.getAttribute('data-command') || 'dismiss';
        vscode.postMessage({ command, id: item.id });
      });
      byId(actionId(prefix, 'apply'))?.addEventListener('click', () => {
        const command = byId(actionId(prefix, 'apply'))?.getAttribute('data-command') || 'apply';
        if (!filters.busyIds.has(item.id) && state.applyAllowed && (statusOf(item.id, item) === 'open' || statusOf(item.id, item) === 'applied')) {
          filters.busyIds.add(item.id);
          render();
          vscode.postMessage({ command, id: item.id });
        }
      });
      byId(actionId(prefix, 'previous'))?.addEventListener('click', () => {
        if (index > 0) {
          filters.expandedId = items[index - 1].id;
          render();
        }
      });
      byId(actionId(prefix, 'next'))?.addEventListener('click', () => {
        if (index < items.length - 1) {
          filters.expandedId = items[index + 1].id;
          render();
        }
      });
    }

    function statusOf(id, item) {
      return filters.statuses[id] || item?.status || 'open';
    }

    function statusesFor(ids) {
      const statuses = {};
      for (const id of ids) {
        const item = state.suggestions.find((candidate) => candidate.id === id);
        statuses[id] = statusOf(id, item);
      }
      return statuses;
    }

    function statusLabel(status) {
      const value = norm(status);
      const labels = {
        open: 'Ready',
        applied: 'Applied',
        changed: 'Needs refresh',
        dismissed: 'Dismissed'
      };
      const tooltips = {
        open: 'OPEN - original code still matches and can be applied',
        applied: 'APPLIED - proposed code is already present',
        changed: 'CHANGED - original/proposed code could not be matched safely',
        dismissed: 'DISMISSED - hidden locally'
      };
      return '<span class="status ' + escapeHtml(value) + '" title="' + escapeHtml(tooltips[value] || status) + '">' + escapeHtml(labels[value] || status) + '</span>';
    }

    function compactFile(file) {
      const parts = String(file || '').split(/[\\/]/).filter(Boolean);
      return parts.length ? parts[parts.length - 1] : file || '';
    }

    function shortHash(value) {
      return String(value || 'unknown').slice(0, 7);
    }

    function escapeHtml(value) {
      return String(value ?? '').replace(/[&<>"']/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
    }

    setupOptions();
    filters.tab = (state.profile && state.profile.defaultTab) || filters.tab;
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
  const requiredImports = suggestion.required_imports || [];
  const removedImports = suggestion.optional_removed_imports || [];
  const auxiliaryEdits = suggestion.auxiliary_edits || [];
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
    ...(requiredImports.length ? [
      '',
      '## Required imports',
      '```',
      requiredImports.join('\n'),
      '```',
    ] : []),
    ...(removedImports.length ? [
      '',
      '## Optional removed imports',
      '```',
      removedImports.join('\n'),
      '```',
    ] : []),
    ...(auxiliaryEdits.length ? [
      '',
      '## Auxiliary edits',
      ...auxiliaryEdits.flatMap((edit, index) => [
        '',
        `### Edit ${index + 1}${edit.description ? ` - ${edit.description}` : ''}`,
        'Original:',
        '```',
        edit.original_code || '',
        '```',
        'Proposed:',
        '```',
        edit.proposed_code || '',
        '```',
      ]),
    ] : []),
  ].join('\n');
  const document = await vscode.workspace.openTextDocument({ content, language: 'markdown' });
  await vscode.window.showTextDocument(document, { preview: true });
}

async function diffSuggestion(suggestion: Suggestion): Promise<void> {
  if (!diffContentProvider) {
    await previewSuggestion(suggestion);
    return;
  }
  const filePath = absoluteWorkspacePath(suggestion.file);
  const leftUri = vscode.Uri.file(filePath);
  const currentText = fs.readFileSync(filePath, 'utf8');
  const proposedAlreadyPresent = containsNormalizedBlock(currentText, suggestion.proposed_code || '');
  const preview = buildFullFileDiffPreview(currentText, suggestion);
  if (!preview) {
    if (proposedAlreadyPresent) {
      vscode.window.showInformationMessage('Suggestion appears to be already applied.');
      return;
    }
    vscode.window.showWarningMessage('Original block no longer matches. Refresh suggestions.');
    return;
  }

  const safeId = encodeURIComponent(suggestion.id.replace(/[^\w.-]/g, '_'));
  const extension = path.extname(suggestion.file) || '.txt';
  const proposedUri = vscode.Uri.parse(`${DIFF_SCHEME}:/${safeId}/${path.basename(suggestion.file, extension)}.preview${extension}`);
  diffContentProvider.set(proposedUri, preview.text);
  await vscode.commands.executeCommand(
    'vscode.diff',
    leftUri,
    proposedUri,
    `CodeGuardian Diff: ${path.basename(suggestion.file)}:${suggestion.line || '-'}`,
    { selection: new vscode.Range(Math.max(0, preview.startLine - 1), 0, Math.max(0, preview.endLine - 1), 0) }
  );
}

async function openGitDiff(): Promise<void> {
  logInfo('Open Git Diff requested.');
  await refreshWorkspaceSnapshot();
  const gitState = currentGitChangeState();
  if (!gitState.isGitRepository) {
    logWarn('Open Git Diff failed: workspace is not a Git repository.');
    vscode.window.showWarningMessage('Workspace is not a Git repository.');
    return;
  }
  if (!gitState.hasChanges) {
    logInfo('Open Git Diff: no local changes.');
    vscode.window.showInformationMessage('No local changes to show.');
    return;
  }
  await vscode.commands.executeCommand('workbench.view.scm');
}

async function openActivityLog(): Promise<void> {
  logInfo('Activity Log opened.');
  outputChannel?.show(true);
  await vscode.commands.executeCommand('workbench.panel.output.focus');
}

function buildFullFileDiffPreview(currentText: string, suggestion: Suggestion): { text: string; startLine: number; endLine: number } | undefined {
  const original = suggestion.original_code || '';
  const proposed = suggestion.proposed_code || '';
  if (!original || !proposed) {
    return undefined;
  }

  const exactIndex = currentText.indexOf(original);
  if (exactIndex >= 0) {
    const startLine = lineNumberAtOffset(currentText, exactIndex);
    const endLine = startLine + original.replace(/\r\n/g, '\n').split('\n').length - 1;
    const replaced = currentText.slice(0, exactIndex) + withOriginalTrailingNewline(proposed, original) + currentText.slice(exactIndex + original.length);
    return {
      text: applyAdditionalPreviewEdits(replaced, suggestion),
      startLine,
      endLine,
    };
  }

  const located = locateNormalizedLineBlock(currentText, original);
  if (!located) {
    return undefined;
  }
  const lines = currentText.split(/(?<=\n)/);
  const originalBlock = lines.slice(located.startIndex, located.endIndex).join('');
  const replaced = lines.slice(0, located.startIndex).join('') + withOriginalTrailingNewline(proposed, originalBlock) + lines.slice(located.endIndex).join('');
  return {
    text: applyAdditionalPreviewEdits(replaced, suggestion),
    startLine: located.startIndex + 1,
    endLine: located.endIndex,
  };
}

function applyAdditionalPreviewEdits(text: string, suggestion: Suggestion): string {
  let result = removeOptionalImportsPreview(text, suggestion.optional_removed_imports || []);
  result = applyAuxiliaryEditsPreview(result, suggestion.auxiliary_edits || []);
  return addRequiredImportsPreview(result, suggestion.file, suggestion.required_imports || []);
}

function removeOptionalImportsPreview(text: string, imports: string[]): string {
  const removable = new Set(imports.map((item) => item.trim()).filter(Boolean));
  if (!removable.size) {
    return text;
  }
  return text.split(/(?<=\n)/).filter((line) => !removable.has(line.replace(/\r?\n$/, '').trim())).join('');
}

function applyAuxiliaryEditsPreview(text: string, edits: AuxiliaryEdit[]): string {
  let result = text;
  for (const edit of edits) {
    const original = edit.original_code || '';
    const proposed = edit.proposed_code || '';
    if (!original || result.indexOf(original) < 0) {
      continue;
    }
    result = result.replace(original, withOriginalTrailingNewline(proposed, original));
  }
  return result;
}

function addRequiredImportsPreview(text: string, file: string, imports: string[]): string {
  const required = Array.from(new Set(imports.map((item) => item.trim()).filter(Boolean)));
  if (!required.length) {
    return text;
  }
  const lowerFile = file.toLowerCase();
  if (lowerFile.endsWith('.py')) {
    return addPythonImportsPreview(text, required);
  }
  if (lowerFile.endsWith('.java')) {
    return addJavaImportsPreview(text, required);
  }
  return text;
}

function addPythonImportsPreview(text: string, imports: string[]): string {
  const missing = imports.filter((item) => !hasLine(text, item) && (item.startsWith('import ') || item.startsWith('from ')));
  if (!missing.length) {
    return text;
  }
  const newline = text.includes('\r\n') ? '\r\n' : '\n';
  const lines = text.split(/(?<=\n)/);
  let index = 0;
  if (lines[index]?.startsWith('#!')) {
    index += 1;
  }
  if (lines[index] && /^#.*coding[:=]\s*[-\w.]+/.test(lines[index])) {
    index += 1;
  }
  while (index < lines.length && lines[index].trim().startsWith('from __future__ import ')) {
    index += 1;
  }
  let scan = index;
  while (scan < lines.length && !lines[scan].trim()) {
    scan += 1;
  }
  if (scan < lines.length && (lines[scan].trim().startsWith('import ') || lines[scan].trim().startsWith('from '))) {
    index = scan;
    while (index < lines.length && (lines[index].trim().startsWith('import ') || lines[index].trim().startsWith('from ') || !lines[index].trim())) {
      index += 1;
    }
  }
  const insertion = missing.map((item) => item.replace(/\r?\n$/, '') + newline);
  let remainderIndex = index;
  while (remainderIndex < lines.length && !lines[remainderIndex].trim()) {
    remainderIndex += 1;
  }
  insertion.push(newline);
  return [...lines.slice(0, index), ...insertion, ...lines.slice(remainderIndex)].join('');
}

function addJavaImportsPreview(text: string, imports: string[]): string {
  const missing = imports.filter((item) => !hasLine(text, item) && item.startsWith('import ') && item.endsWith(';'));
  if (!missing.length) {
    return text;
  }
  const newline = text.includes('\r\n') ? '\r\n' : '\n';
  const lines = text.split(/(?<=\n)/);
  let packageIndex = -1;
  let lastImportIndex = -1;
  lines.forEach((line, index) => {
    const trimmed = line.trim();
    if (trimmed.startsWith('package ') && trimmed.endsWith(';')) {
      packageIndex = index;
    }
    if (trimmed.startsWith('import ') && trimmed.endsWith(';')) {
      lastImportIndex = index;
    }
  });
  const insertIndex = lastImportIndex >= 0 ? lastImportIndex + 1 : packageIndex >= 0 ? packageIndex + 1 : 0;
  const insertion = missing.map((item) => item.replace(/\r?\n$/, '') + newline);
  if (lastImportIndex < 0 && packageIndex >= 0) {
    insertion.unshift(newline);
  }
  return [...lines.slice(0, insertIndex), ...insertion, ...lines.slice(insertIndex)].join('');
}

function hasLine(text: string, expected: string): boolean {
  const normalized = expected.trim();
  return text.split(/\r\n|\r|\n/).some((line) => line.trim() === normalized);
}

function locateNormalizedLineBlock(text: string, block: string): { startIndex: number; endIndex: number } | undefined {
  const textLines = text.split(/(?<=\n)/);
  const blockLines = normalizeBlock(block).split('\n');
  if (!blockLines.length || !blockLines[0]) {
    return undefined;
  }
  for (let start = 0; start <= textLines.length - blockLines.length; start += 1) {
    const candidate = textLines.slice(start, start + blockLines.length).map((line) => line.trim()).join('\n').trim();
    if (candidate === blockLines.join('\n')) {
      return { startIndex: start, endIndex: start + blockLines.length };
    }
  }
  return undefined;
}

function withOriginalTrailingNewline(proposed: string, original: string): string {
  const hasTrailingNewline = /\r?\n$/.test(original);
  return hasTrailingNewline && !/\r?\n$/.test(proposed) ? `${proposed}${original.endsWith('\r\n') ? '\r\n' : '\n'}` : proposed;
}

function lineNumberAtOffset(text: string, offset: number): number {
  return text.slice(0, offset).split(/\r\n|\r|\n/).length;
}

async function applyOpenSuggestion(
  suggestion: Suggestion,
  data = loadResultsData(false),
): Promise<Record<string, SuggestionStatus>> {
  logInfo(`Apply started: ${suggestion.id}`);
  if (!data.applyAllowed) {
    logWarn(`Apply skipped: ${data.applyDisabledReason}`);
    vscode.window.showWarningMessage(data.applyDisabledReason);
    return {};
  }
  return applySuggestionsWithCli([suggestion], data.artifact.commit);
}

async function undoAppliedSuggestion(
  suggestion: Suggestion,
  data = loadResultsData(false),
): Promise<Record<string, SuggestionStatus>> {
  if (!data.applyAllowed) {
    vscode.window.showWarningMessage(data.applyDisabledReason);
    return {};
  }
  return undoSuggestionsWithCli([suggestion], data.artifact.commit);
}

async function undoSelectedAppliedSuggestions(
  ids: string[],
  data = loadResultsData(false),
  knownStatuses?: Record<string, SuggestionStatus>,
): Promise<Record<string, SuggestionStatus>> {
  if (!data.applyAllowed) {
    vscode.window.showWarningMessage(data.applyDisabledReason);
    return {};
  }
  if (!ids.length) {
    vscode.window.showInformationMessage('Select one or more applied CodeGuardian suggestions first.');
    return {};
  }
  const suggestions = data.suggestions.filter((suggestion) => ids.includes(suggestion.id));
  const statuses = knownStatuses || await loadSuggestionStatuses(data.suggestions);
  const appliedSuggestions = suggestions.filter((suggestion) => (statuses[suggestion.id] || suggestion.status || 'open') === 'applied');
  const skipped = suggestions.length - appliedSuggestions.length;
  if (!appliedSuggestions.length) {
    vscode.window.showInformationMessage(`No selected CodeGuardian suggestions are applied. Skipped ${skipped}.`);
    return {};
  }
  const answer = await vscode.window.showWarningMessage(
    `Undo ${appliedSuggestions.length} applied suggestion(s)?\n\nSelected: ${suggestions.length}\nApplied: ${appliedSuggestions.length}\nSkipped: ${skipped}`,
    'Undo'
  );
  if (answer !== 'Undo') {
    return {};
  }
  return undoSuggestionsWithCli(appliedSuggestions, data.artifact.commit);
}

async function applySelectedOpenSuggestions(
  ids: string[],
  data = loadResultsData(false),
  knownStatuses?: Record<string, SuggestionStatus>,
): Promise<Record<string, SuggestionStatus>> {
  logInfo(`Apply Selected started: ${ids.length} selected.`);
  if (!data.applyAllowed) {
    logWarn(`Apply Selected skipped: ${data.applyDisabledReason}`);
    vscode.window.showWarningMessage(data.applyDisabledReason);
    return {};
  }
  if (!ids.length) {
    vscode.window.showInformationMessage('Select one or more ready CodeGuardian suggestions first.');
    return {};
  }
  const suggestions = data.suggestions.filter((suggestion) => ids.includes(suggestion.id));
  const statuses = knownStatuses || await loadSuggestionStatuses(data.suggestions);
  const openSuggestions = suggestions.filter((suggestion) => (statuses[suggestion.id] || suggestion.status || 'open') === 'open');
  const applied = suggestions.filter((suggestion) => (statuses[suggestion.id] || suggestion.status || 'open') === 'applied').length;
  const changed = suggestions.filter((suggestion) => (statuses[suggestion.id] || suggestion.status || 'open') === 'changed').length;
  const skipped = suggestions.length - openSuggestions.length;
  if (!openSuggestions.length) {
    logInfo(`Apply Selected skipped: no ready suggestions. Selected ${suggestions.length}.`);
    vscode.window.showInformationMessage(`No selected CodeGuardian suggestions are ready. Skipped ${skipped}.`);
    return {};
  }
  const conflicts = detectSuggestionConflicts(openSuggestions);
  if (conflicts.length) {
    const names = conflicts.slice(0, 5).map((suggestion) => suggestion.target_name || suggestion.id).join(', ');
    logWarn(`Apply Selected blocked by conflicts: ${names}`);
    vscode.window.showWarningMessage(`Some selected suggestions modify overlapping code. Apply them one by one.${names ? ` Conflicts: ${names}` : ''}`);
    return {};
  }
  const reason = [
    applied ? `${applied} already applied` : '',
    changed ? `${changed} need refresh` : '',
  ].filter(Boolean).join(', ');
  const message = `Apply ${openSuggestions.length} ready suggestion(s)?\n\nSelected: ${suggestions.length}\nReady: ${openSuggestions.length}\nSkipped: ${skipped}${reason ? ` (${reason})` : ''}`;
  const answer = await vscode.window.showWarningMessage(message, 'Apply');
  if (answer !== 'Apply') {
    return {};
  }
  return applySuggestionsWithCli(openSuggestions, data.artifact.commit);
}

async function applySuggestionsWithCli(
  suggestions: Suggestion[],
  expectedCommit?: string,
): Promise<Record<string, SuggestionStatus>> {
  if (warnAboutDirtyTarget(suggestions, 'applying')) {
    return {};
  }
  const ids = suggestions.map((suggestion) => suggestion.id);
  const command = ids.length === 1 ? 'apply' : 'apply-selected';
  const args = ids.length === 1 ? ['--id', ids[0]] : ['--ids', ids.join(',')];
  return runMutation(command, args, 'apply', ids, expectedCommit);
}

async function undoSuggestionsWithCli(
  suggestions: Suggestion[],
  expectedCommit?: string,
): Promise<Record<string, SuggestionStatus>> {
  if (warnAboutDirtyTarget(suggestions, 'undoing')) {
    return {};
  }
  const ids = suggestions.map((suggestion) => suggestion.id);
  const command = ids.length === 1 ? 'undo' : 'undo-selected';
  const args = ids.length === 1 ? ['--id', ids[0]] : ['--ids', ids.join(',')];
  return runMutation(command, args, 'undo', ids, expectedCommit);
}

function warnAboutDirtyTarget(suggestions: Suggestion[], action: 'applying' | 'undoing'): boolean {
  const targets = new Set(suggestions.map((suggestion) => comparableFilePath(absoluteWorkspacePath(suggestion.file))));
  const dirtyDocument = vscode.workspace.textDocuments.find((document) => (
    document.uri.scheme === 'file'
    && document.isDirty
    && targets.has(comparableFilePath(document.uri.fsPath))
  ));
  if (!dirtyDocument) {
    return false;
  }
  const relative = vscode.workspace.asRelativePath(dirtyDocument.uri, false);
  logWarn(`Mutation blocked because ${relative} has unsaved changes.`);
  vscode.window.showWarningMessage(`Save the changes in ${relative} before ${action} a CodeGuardian suggestion.`);
  return true;
}

function comparableFilePath(filePath: string): string {
  const normalized = path.normalize(filePath);
  return process.platform === 'win32' ? normalized.toLowerCase() : normalized;
}

async function verifyArtifactCommit(expectedCommit?: string): Promise<void> {
  if (!expectedCommit) {
    return;
  }
  const currentCommit = await optionalWorkspaceCommand(['rev-parse', 'HEAD']);
  if (!currentCommit) {
    throw new ArtifactContextError('CodeGuardian could not verify the current Git commit.');
  }
  const matches = currentCommit.startsWith(expectedCommit) || expectedCommit.startsWith(currentCommit);
  if (!matches) {
    throw new ArtifactContextError(
      `The artifact was generated for ${shortHash(expectedCommit)}, but the local repository is at ${shortHash(currentCommit)}.`,
    );
  }
}

async function runMutation(
  command: string,
  commandArgs: string[],
  operation: 'apply' | 'undo',
  ids: string[],
  expectedCommit?: string,
): Promise<Record<string, SuggestionStatus>> {
  const python = configValue('pythonPath') || 'python';
  const cli = absoluteWorkspacePath(configValue('cliPath') || 'tools/codeguardian_cli.py');
  const startedAt = Date.now();
  let result: CliResult;
  try {
    result = await vscode.window.withProgress(
      {
        location: vscode.ProgressLocation.Notification,
        title: mutationProgressTitle(operation, ids.length),
        cancellable: false,
      },
      async () => {
        await verifyArtifactCommit(expectedCommit);
        return runCliWithFallback(
          python,
          buildMutationCliArgs(cli, command, resultsPath(), commandArgs),
        );
      },
    );
  } catch (error) {
    if (error instanceof ArtifactContextError) {
      logWarn(`${operation} blocked: ${error.message}`);
      vscode.window.showWarningMessage(`${error.message} Refresh the CodeGuardian results before continuing.`);
      return {};
    }
    throw error;
  }
  logCliResult(operation, result, Date.now() - startedAt);
  const summary = parseMutationSummary(result.stdout);
  const statuses = mutationStatuses(summary, operation);
  const hasProblems = summary.skipped > 0 || summary.failed > 0 || result.exitCode !== 0;
  if (!hasProblems && summary.applied > 0) {
    const message = operation === 'apply'
      ? summary.applied === 1 ? 'Suggestion applied.' : `${summary.applied} suggestions applied.`
      : summary.applied === 1 ? 'Suggestion undone.' : `${summary.applied} suggestions undone.`;
    vscode.window.showInformationMessage(message);
  }
  if (hasProblems) {
    const completed = operation === 'apply' ? 'applied' : 'undone';
    const notCompleted = summary.skipped + summary.failed;
    const firstProblem = summary.results.find((item) => !item.applied);
    const reason = firstProblem?.blocked_reason || firstProblem?.message;
    const warning = summary.applied === 0 && reason
      ? reason
      : `${summary.applied} ${completed}; ${notCompleted} skipped or failed.${reason ? ` ${reason}` : ''}`;
    const choice = await vscode.window.showWarningMessage(
      warning,
      'Show details',
    );
    if (choice === 'Show details') {
      outputChannel?.show(true);
    }
  }
  return statuses;
}

function logCliResult(operation: string, result: CliResult, elapsedMs: number): void {
  logInfo(`${operation} CLI finished in ${elapsedMs} ms with exit code ${result.exitCode}.`);
  if (result.stdout.trim()) {
    outputChannel?.appendLine(result.stdout.trim());
  }
  if (result.stderr.trim()) {
    outputChannel?.appendLine(result.stderr.trim());
  }
}

function detectSuggestionConflicts(suggestions: Suggestion[]): Suggestion[] {
  const conflicted = new Map<string, Suggestion>();
  const byFile = new Map<string, Suggestion[]>();
  for (const suggestion of suggestions) {
    const items = byFile.get(suggestion.file) || [];
    items.push(suggestion);
    byFile.set(suggestion.file, items);
  }
  for (const items of byFile.values()) {
    for (let i = 0; i < items.length; i += 1) {
      for (let j = i + 1; j < items.length; j += 1) {
        if (suggestionsConflict(items[i], items[j])) {
          conflicted.set(items[i].id, items[i]);
          conflicted.set(items[j].id, items[j]);
        }
      }
    }
  }
  return Array.from(conflicted.values());
}

function suggestionsConflict(a: Suggestion, b: Suggestion): boolean {
  const aRange = suggestionLineRange(a);
  const bRange = suggestionLineRange(b);
  if (aRange.start <= bRange.end && bRange.start <= aRange.end) {
    return true;
  }
  const aOriginal = normalizeBlock(a.original_code || '');
  const bOriginal = normalizeBlock(b.original_code || '');
  if (!aOriginal || !bOriginal) {
    return false;
  }
  return aOriginal === bOriginal || aOriginal.includes(bOriginal) || bOriginal.includes(aOriginal);
}

function suggestionLineRange(suggestion: Suggestion): { start: number; end: number } {
  const start = Number(suggestion.line || 0);
  const lines = String(suggestion.original_code || '').split(/\r\n|\r|\n/).length;
  return { start, end: start + Math.max(1, lines) - 1 };
}

async function loadSuggestionStatuses(
  suggestions = loadSuggestions(),
): Promise<Record<string, SuggestionStatus>> {
  const python = configValue('pythonPath') || 'python';
  const cli = absoluteWorkspacePath(configValue('cliPath') || 'tools/codeguardian_cli.py');
  const result = await runCliWithFallback(python, [cli, 'status', '--file', resultsPath()]);
  if (result.exitCode !== 0) {
    throw new Error(result.stderr || result.stdout || 'CodeGuardian CLI status failed.');
  }
  const parsed = JSON.parse(result.stdout || '{"suggestions":[]}');
  const statuses: Record<string, SuggestionStatus> = {};
  for (const item of parsed.suggestions || []) {
    if (item.id && ['open', 'applied', 'changed'].includes(item.status)) {
      statuses[item.id] = item.status;
    }
  }
  overlayOpenDocumentStatuses(statuses, suggestions);
  return statuses;
}

function overlayOpenDocumentStatuses(
  statuses: Record<string, SuggestionStatus>,
  suggestions: Suggestion[],
): void {
  const openDocuments = new Map<string, string>();
  for (const document of vscode.workspace.textDocuments) {
    if (document.uri.scheme !== 'file') {
      continue;
    }
    const relative = vscode.workspace.asRelativePath(document.uri, false).replace(/\\/g, '/');
    openDocuments.set(relative, document.getText());
  }

  for (const suggestion of suggestions) {
    const text = openDocuments.get(String(suggestion.file || '').replace(/\\/g, '/'));
    if (text === undefined) {
      continue;
    }
    if (containsNormalizedBlock(text, suggestion.proposed_code || '') && requiredPreviewEditsPresent(text, suggestion)) {
      statuses[suggestion.id] = 'applied';
    } else if (containsNormalizedBlock(text, suggestion.original_code || '')) {
      statuses[suggestion.id] = 'open';
    } else {
      statuses[suggestion.id] = 'changed';
    }
  }
}

function requiredPreviewEditsPresent(text: string, suggestion: Suggestion): boolean {
  const imports = suggestion.required_imports || [];
  if (imports.some((item) => !hasLine(text, item))) {
    return false;
  }
  for (const edit of suggestion.auxiliary_edits || []) {
    if (edit.proposed_code && !containsNormalizedBlock(text, edit.proposed_code)) {
      return false;
    }
  }
  return true;
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
      const code = (error as NodeJS.ErrnoException | undefined)?.code;
      if (error && typeof code !== 'number') {
        reject(error);
        return;
      }
      resolve({ stdout, stderr, exitCode: typeof code === 'number' ? code : 0 });
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

function firstLine(text: string): string {
  return String(text || '').split(/\r?\n/).find((line) => line.trim())?.trim() || '';
}
