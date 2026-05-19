import * as fs from 'fs';
import * as path from 'path';
import * as vscode from 'vscode';
import { LocalFinding } from './rulesEngine';

export function writeLocalResults(findings: LocalFinding[]): void {
  const workspace = vscode.workspace.workspaceFolders?.[0];
  if (!workspace) {
    return;
  }
  const filePath = path.join(workspace.uri.fsPath, 'codeguardian-local-results.json');
  const payload = {
    mode: 'local',
    generated_at: new Date().toISOString(),
    source: 'CodeGuardian Local',
    findings: findings.map((finding) => ({
      id: finding.id,
      ruleId: finding.ruleId,
      file: vscode.workspace.asRelativePath(finding.filePath, false).replace(/\\/g, '/'),
      line: finding.startLine + 1,
      severity: finding.severity,
      message: finding.message,
      source: finding.source,
      original_code: finding.originalCode,
      proposed_code: finding.proposedCode,
      explanation: finding.explanation,
    })),
  };
  fs.writeFileSync(filePath, JSON.stringify(payload, null, 2), 'utf8');
}
