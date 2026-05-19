import * as vscode from 'vscode';
import { CodeGuardianCodeActionsProvider } from './codeActionsProvider';
import { CodeGuardianDiagnosticsProvider } from './diagnosticsProvider';
import { loadLocalAnalysisConfig, LocalAnalysisConfig } from './localConfig';
import { scanDocument, scanGitChangedFiles, scanWorkspace } from './localScanRunner';
import { writeLocalResults } from './localResultsStore';
import { LocalFinding } from './rulesEngine';

type Logger = {
  info(message: string): void;
  warn(message: string): void;
};

export class LocalAnalysisService implements vscode.Disposable {
  private config: LocalAnalysisConfig = loadLocalAnalysisConfig();
  private enabled = this.config.enabled;
  private readonly diagnostics = new CodeGuardianDiagnosticsProvider();
  private readonly timers = new Map<string, NodeJS.Timeout>();
  private lastScanAt?: Date;

  constructor(private readonly context: vscode.ExtensionContext, private readonly logger: Logger) {}

  register(): void {
    this.context.subscriptions.push(this.diagnostics);
    this.context.subscriptions.push(vscode.languages.registerCodeActionsProvider(
      [{ scheme: 'file' }],
      new CodeGuardianCodeActionsProvider(),
      { providedCodeActionKinds: [vscode.CodeActionKind.QuickFix] },
    ));
    this.context.subscriptions.push(vscode.workspace.onDidChangeTextDocument((event) => this.onDocumentChanged(event.document)));
    this.context.subscriptions.push(vscode.workspace.onDidSaveTextDocument((document) => this.onDocumentSaved(document)));
    this.context.subscriptions.push(vscode.workspace.onDidOpenTextDocument((document) => {
      if (this.enabled && this.config.onSave) {
        void this.scanSingleDocument(document);
      }
    }));
    this.context.subscriptions.push(vscode.commands.registerCommand('codeguardian.runLocalScan', () => this.runLocalScan()));
    this.context.subscriptions.push(vscode.commands.registerCommand('codeguardian.clearLocalDiagnostics', () => this.clearDiagnostics()));
    this.context.subscriptions.push(vscode.commands.registerCommand('codeguardian.toggleLocalAnalysis', () => this.toggle()));
    this.context.subscriptions.push(vscode.commands.registerCommand('codeguardian.openLocalResults', () => this.openLocalResults()));
    if (this.enabled) {
      this.logger.info('Local analysis enabled.');
      void this.scanVisibleDocuments();
    } else {
      this.logger.info('Local analysis disabled.');
    }
  }

  async runLocalScan(): Promise<void> {
    this.reloadConfig();
    this.enabled = true;
    this.logger.info('Local scan started.');
    const changedResults = await scanGitChangedFiles(this.config);
    if (changedResults && changedResults.size > 0) {
      this.diagnostics.clear();
      for (const [uri, findings] of changedResults) {
        this.diagnostics.setFindings(uri, findings);
      }
      this.logger.info(`Local scan used Git changed files: ${changedResults.size} file(s).`);
    } else {
      const active = vscode.window.activeTextEditor?.document;
      if (active && active.uri.scheme === 'file') {
        await this.scanSingleDocument(active);
        this.logger.info('Local scan fallback used active editor.');
      } else {
        const results = await scanWorkspace(this.config);
        this.diagnostics.clear();
        for (const [uri, findings] of results) {
          this.diagnostics.setFindings(uri, findings);
        }
        this.logger.info(`Local scan fallback used workspace files: ${results.size} file(s).`);
      }
    }
    this.lastScanAt = new Date();
    const findings = this.diagnostics.allFindings();
    writeLocalResults(findings);
    this.logger.info(`Local scan completed: ${findings.length} findings.`);
    vscode.window.showInformationMessage(`CodeGuardian Local scan completed: ${findings.length} finding(s).`);
  }

  clearDiagnostics(): void {
    this.diagnostics.clear();
    this.logger.info('Local diagnostics cleared.');
    vscode.window.showInformationMessage('CodeGuardian Local diagnostics cleared.');
  }

  toggle(): void {
    this.enabled = !this.enabled;
    this.reloadConfig();
    if (!this.enabled) {
      this.diagnostics.clear();
    } else {
      void this.scanVisibleDocuments();
    }
    this.logger.info(`Local analysis ${this.enabled ? 'enabled' : 'disabled'}.`);
    vscode.window.showInformationMessage(`CodeGuardian Local analysis ${this.enabled ? 'enabled' : 'disabled'}.`);
  }

  async openLocalResults(): Promise<void> {
    const workspace = vscode.workspace.workspaceFolders?.[0];
    if (!workspace) {
      vscode.window.showWarningMessage('Open a workspace before opening CodeGuardian local results.');
      return;
    }
    const uri = vscode.Uri.joinPath(workspace.uri, 'codeguardian-local-results.json');
    try {
      const document = await vscode.workspace.openTextDocument(uri);
      await vscode.window.showTextDocument(document);
    } catch {
      vscode.window.showInformationMessage('No CodeGuardian local results found. Run Local Scan first.');
    }
  }

  dispose(): void {
    for (const timer of this.timers.values()) {
      clearTimeout(timer);
    }
    this.timers.clear();
    this.diagnostics.dispose();
  }

  private onDocumentChanged(document: vscode.TextDocument): void {
    if (!this.enabled || !this.config.onType) {
      return;
    }
    this.scheduleScan(document);
  }

  private onDocumentSaved(document: vscode.TextDocument): void {
    if (!this.enabled || !this.config.onSave) {
      return;
    }
    void this.scanSingleDocument(document);
  }

  private scheduleScan(document: vscode.TextDocument): void {
    const key = document.uri.toString();
    const existing = this.timers.get(key);
    if (existing) {
      clearTimeout(existing);
    }
    const timer = setTimeout(() => {
      this.timers.delete(key);
      void this.scanSingleDocument(document);
    }, Math.max(100, this.config.debounceMs));
    this.timers.set(key, timer);
  }

  private async scanSingleDocument(document: vscode.TextDocument): Promise<void> {
    this.reloadConfig();
    const findings = await scanDocument(document, this.config);
    this.diagnostics.setFindings(document.uri, findings);
    this.lastScanAt = new Date();
  }

  private async scanVisibleDocuments(): Promise<void> {
    for (const editor of vscode.window.visibleTextEditors) {
      await this.scanSingleDocument(editor.document);
    }
  }

  private reloadConfig(): void {
    this.config = loadLocalAnalysisConfig();
  }

  status(): { enabled: boolean; findings: number; lastScanAt?: Date; analyzer: string } {
    return {
      enabled: this.enabled,
      findings: this.diagnostics.allFindings().length,
      lastScanAt: this.lastScanAt,
      analyzer: 'Rules',
    };
  }
}
