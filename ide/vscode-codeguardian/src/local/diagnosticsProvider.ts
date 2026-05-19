import * as vscode from 'vscode';
import { LocalFinding } from './rulesEngine';

export class CodeGuardianDiagnosticsProvider implements vscode.Disposable {
  private readonly collection = vscode.languages.createDiagnosticCollection('CodeGuardian Local');
  private readonly findingsByUri = new Map<string, LocalFinding[]>();

  setFindings(uri: vscode.Uri, findings: LocalFinding[]): void {
    this.findingsByUri.set(uri.toString(), findings);
    this.collection.set(uri, findings.map((finding) => toDiagnostic(finding)));
  }

  clear(uri?: vscode.Uri): void {
    if (uri) {
      this.findingsByUri.delete(uri.toString());
      this.collection.delete(uri);
      return;
    }
    this.findingsByUri.clear();
    this.collection.clear();
  }

  allFindings(): LocalFinding[] {
    return Array.from(this.findingsByUri.values()).flat();
  }

  dispose(): void {
    this.collection.dispose();
  }
}

export function toDiagnostic(finding: LocalFinding): vscode.Diagnostic {
  const diagnostic = new vscode.Diagnostic(
    new vscode.Range(
      finding.startLine,
      finding.startCharacter,
      finding.endLine,
      Math.max(finding.startCharacter + 1, finding.endCharacter),
    ),
    finding.message,
    toDiagnosticSeverity(finding.severity),
  );
  diagnostic.source = finding.source;
  diagnostic.code = finding.ruleId;
  return diagnostic;
}

export function toDiagnosticSeverity(severity: LocalFinding['severity']): vscode.DiagnosticSeverity {
  switch (severity) {
    case 'CRITICAL':
      return vscode.DiagnosticSeverity.Error;
    case 'MAJOR':
      return vscode.DiagnosticSeverity.Warning;
    case 'MINOR':
    default:
      return vscode.DiagnosticSeverity.Information;
  }
}
