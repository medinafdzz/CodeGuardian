import * as vscode from 'vscode';
import { execFile } from 'child_process';
import { LocalAnalysisConfig } from './localConfig';
import { analyzeText, LocalFinding, shouldIgnoreFile } from './rulesEngine';

const INCLUDE_PATTERN = '**/*.{py,js,jsx,ts,tsx,java,kt,go,cs,php,rb}';
const EXCLUDE_PATTERN = '**/{node_modules,target,build,dist,.venv,venv,__pycache__,.git}/**';

export async function scanDocument(document: vscode.TextDocument, config: LocalAnalysisConfig): Promise<LocalFinding[]> {
  if (!isScannableDocument(document)) {
    return [];
  }
  return analyzeText(document.getText(), document.languageId, document.uri.fsPath, {
    rules: config.rules,
    maxFindings: config.maxFindings,
  });
}

export async function scanWorkspace(config: LocalAnalysisConfig): Promise<Map<vscode.Uri, LocalFinding[]>> {
  const results = new Map<vscode.Uri, LocalFinding[]>();
  const openDocuments = vscode.workspace.textDocuments.filter(isScannableDocument);
  for (const document of openDocuments) {
    results.set(document.uri, await scanDocument(document, config));
  }

  const remaining = Math.max(0, config.maxFindings - Array.from(results.values()).flat().length);
  if (remaining <= 0) {
    return results;
  }

  const files = await vscode.workspace.findFiles(INCLUDE_PATTERN, EXCLUDE_PATTERN, 200);
  for (const uri of files) {
    if (results.has(uri) || shouldIgnoreFile(uri.fsPath)) {
      continue;
    }
    try {
      const document = await vscode.workspace.openTextDocument(uri);
      results.set(uri, await scanDocument(document, config));
      if (Array.from(results.values()).flat().length >= config.maxFindings) {
        break;
      }
    } catch {
      // Ignore unreadable local files; diagnostics should never break editing.
    }
  }
  return results;
}

export async function scanGitChangedFiles(config: LocalAnalysisConfig): Promise<Map<vscode.Uri, LocalFinding[]> | undefined> {
  const workspace = vscode.workspace.workspaceFolders?.[0];
  if (!workspace) {
    return undefined;
  }
  const changedPaths = await gitChangedPaths(workspace.uri.fsPath);
  if (!changedPaths.length) {
    return undefined;
  }
  const results = new Map<vscode.Uri, LocalFinding[]>();
  for (const relativePath of changedPaths) {
    const uri = vscode.Uri.joinPath(workspace.uri, ...relativePath.split('/'));
    try {
      const document = await vscode.workspace.openTextDocument(uri);
      if (!isScannableDocument(document)) {
        continue;
      }
      results.set(uri, await scanDocument(document, config));
      if (Array.from(results.values()).flat().length >= config.maxFindings) {
        break;
      }
    } catch {
      // Deleted or unreadable files can appear in Source Control; skip them.
    }
  }
  return results;
}

function isScannableDocument(document: vscode.TextDocument): boolean {
  return document.uri.scheme === 'file' && !shouldIgnoreFile(document.uri.fsPath);
}

function gitChangedPaths(cwd: string): Promise<string[]> {
  return new Promise((resolve) => {
    execFile('git', ['status', '--porcelain', '-z'], { cwd }, (error, stdout) => {
      if (error) {
        resolve([]);
        return;
      }
      const paths: string[] = [];
      const entries = stdout.split('\0').filter(Boolean);
      for (let i = 0; i < entries.length; i += 1) {
        const entry = entries[i];
        const status = entry.slice(0, 2);
        if (status.includes('D')) {
          continue;
        }
        if (status.startsWith('R') || status.startsWith('C')) {
          i += 1;
          if (entries[i]) {
            paths.push(normalizeGitPath(entries[i]));
          }
          continue;
        }
        paths.push(normalizeGitPath(entry.slice(3)));
      }
      resolve(Array.from(new Set(paths)).filter(Boolean));
    });
  });
}

function normalizeGitPath(value: string): string {
  return value.trim().replace(/\\/g, '/').replace(/^"|"$/g, '');
}
