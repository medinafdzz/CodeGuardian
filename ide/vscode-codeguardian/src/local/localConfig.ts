import * as fs from 'fs';
import * as path from 'path';
import * as vscode from 'vscode';

export type LocalAnalysisConfig = {
  enabled: boolean;
  mode: 'manual' | 'onSave' | 'onType';
  onType: boolean;
  onSave: boolean;
  debounceMs: number;
  rules: string[];
  maxFindings: number;
  useAI: boolean;
  useDockerMcp: boolean;
};

export const DEFAULT_LOCAL_ANALYSIS_CONFIG: LocalAnalysisConfig = {
  enabled: false,
  mode: 'manual',
  onType: false,
  onSave: true,
  debounceMs: 600,
  rules: ['secrets', 'tls', 'subprocess', 'sql', 'debug'],
  maxFindings: 50,
  useAI: false,
  useDockerMcp: false,
};

export function loadLocalAnalysisConfig(): LocalAnalysisConfig {
  const settings = vscode.workspace.getConfiguration('codeguardian.localAnalysis');
  const settingsConfig: LocalAnalysisConfig = {
    ...DEFAULT_LOCAL_ANALYSIS_CONFIG,
    enabled: settings.get<boolean>('enabled') ?? DEFAULT_LOCAL_ANALYSIS_CONFIG.enabled,
    onType: settings.get<boolean>('onType') ?? DEFAULT_LOCAL_ANALYSIS_CONFIG.onType,
    onSave: settings.get<boolean>('onSave') ?? DEFAULT_LOCAL_ANALYSIS_CONFIG.onSave,
    debounceMs: settings.get<number>('debounceMs') ?? DEFAULT_LOCAL_ANALYSIS_CONFIG.debounceMs,
    maxFindings: settings.get<number>('maxFindings') ?? DEFAULT_LOCAL_ANALYSIS_CONFIG.maxFindings,
  };
  const workspace = vscode.workspace.workspaceFolders?.[0];
  if (!workspace) {
    return settingsConfig;
  }
  const configPath = path.join(workspace.uri.fsPath, '.codeguardian.json');
  if (!fs.existsSync(configPath)) {
    return settingsConfig;
  }
  try {
    const parsed = JSON.parse(fs.readFileSync(configPath, 'utf8'));
    const local = parsed.localAnalysis || {};
    return {
      enabled: booleanValue(local.enabled, settingsConfig.enabled),
      mode: ['manual', 'onSave', 'onType'].includes(local.mode) ? local.mode : settingsConfig.mode,
      onType: booleanValue(local.onType, settingsConfig.onType),
      onSave: booleanValue(local.onSave, settingsConfig.onSave),
      debounceMs: numberValue(local.debounceMs, settingsConfig.debounceMs),
      rules: Array.isArray(local.rules) && local.rules.every((item: unknown) => typeof item === 'string')
        ? local.rules
        : settingsConfig.rules,
      maxFindings: numberValue(local.maxFindings, settingsConfig.maxFindings),
      useAI: false,
      useDockerMcp: false,
    };
  } catch {
    return settingsConfig;
  }
}

function booleanValue(value: unknown, fallback: boolean): boolean {
  return typeof value === 'boolean' ? value : fallback;
}

function numberValue(value: unknown, fallback: number): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback;
}
