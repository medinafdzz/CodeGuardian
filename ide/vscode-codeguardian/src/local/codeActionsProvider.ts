import * as vscode from 'vscode';

export class CodeGuardianCodeActionsProvider implements vscode.CodeActionProvider {
  provideCodeActions(
    document: vscode.TextDocument,
    range: vscode.Range,
    context: vscode.CodeActionContext,
  ): vscode.CodeAction[] {
    const actions: vscode.CodeAction[] = [];
    for (const diagnostic of context.diagnostics) {
      if (diagnostic.source !== 'CodeGuardian Local' || diagnostic.code !== 'tls-verify-disabled') {
        continue;
      }
      const line = document.lineAt(range.start.line);
      if (!/\bverify\s*=\s*False\b/i.test(line.text)) {
        continue;
      }
      const replacement = line.text
        .replace(/\s*,\s*verify\s*=\s*False/i, '')
        .replace(/verify\s*=\s*False\s*,\s*/i, '')
        .replace(/\bverify\s*=\s*False\b/i, '');
      const action = new vscode.CodeAction('CodeGuardian: remove verify=False', vscode.CodeActionKind.QuickFix);
      action.diagnostics = [diagnostic];
      action.isPreferred = true;
      action.edit = new vscode.WorkspaceEdit();
      action.edit.replace(document.uri, line.range, replacement);
      actions.push(action);
    }
    return actions;
  }
}
