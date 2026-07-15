export type MutationStatus = 'open' | 'applied' | 'changed';

export type CliMutationResult = {
  id: string;
  file: string;
  applied: boolean;
  message: string;
  transaction_id?: string;
  restored?: boolean;
  blocked_reason?: string;
  state_path?: string;
  before_hash?: string;
  after_hash?: string;
  failed?: boolean;
};

export type CliMutationSummary = {
  applied: number;
  skipped: number;
  failed: number;
  results: CliMutationResult[];
};

export function buildMutationCliArgs(
  cliPath: string,
  command: string,
  resultsFile: string,
  commandArgs: string[],
): string[] {
  return [cliPath, command, '--file', resultsFile, ...commandArgs, '--json'];
}

export function parseMutationSummary(stdout: string): CliMutationSummary {
  const parsed = JSON.parse(stdout || '{}') as Partial<CliMutationSummary>;
  if (!Array.isArray(parsed.results)) {
    throw new Error('CodeGuardian CLI returned an invalid mutation result.');
  }
  return {
    applied: Number(parsed.applied || 0),
    skipped: Number(parsed.skipped || 0),
    failed: Number(parsed.failed || 0),
    results: parsed.results as CliMutationResult[],
  };
}

export function mutationStatuses(
  summary: CliMutationSummary,
  operation: 'apply' | 'undo',
): Record<string, MutationStatus> {
  const statuses: Record<string, MutationStatus> = {};
  for (const result of summary.results) {
    if (!result.id) {
      continue;
    }
    statuses[result.id] = result.applied
      ? operation === 'apply' ? 'applied' : 'open'
      : 'changed';
  }
  return statuses;
}
