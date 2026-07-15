import { strict as assert } from 'node:assert';
import { test } from 'node:test';

import {
  buildMutationCliArgs,
  mutationStatuses,
  parseMutationSummary,
} from './cliProtocol';

test('builds mutation arguments without shell quoting', () => {
  const args = buildMutationCliArgs(
    'C:\\CodeGuardian tools\\codeguardian_cli.py',
    'apply',
    'C:\\Demo repository\\codeguardian-results.json',
    ['--id', 'suggestion one'],
  );

  assert.deepEqual(args, [
    'C:\\CodeGuardian tools\\codeguardian_cli.py',
    'apply',
    '--file',
    'C:\\Demo repository\\codeguardian-results.json',
    '--id',
    'suggestion one',
    '--json',
  ]);
});

test('parses a structured CLI mutation result', () => {
  const summary = parseMutationSummary(JSON.stringify({
    applied: 1,
    skipped: 1,
    failed: 0,
    results: [
      { id: 'one', file: 'app.py', applied: true, message: 'applied' },
      {
        id: 'two',
        file: 'app.py',
        applied: false,
        message: 'file changed',
        blocked_reason: 'Cannot undo safely because the file has changed.',
      },
    ],
  }));

  assert.equal(summary.applied, 1);
  assert.equal(summary.skipped, 1);
  assert.equal(summary.results[1].blocked_reason, 'Cannot undo safely because the file has changed.');
  assert.deepEqual(mutationStatuses(summary, 'apply'), { one: 'applied', two: 'changed' });
  assert.deepEqual(mutationStatuses(summary, 'undo'), { one: 'open', two: 'changed' });
});

test('rejects unstructured CLI output', () => {
  assert.throws(() => parseMutationSummary('{"message":"not a summary"}'), /invalid mutation result/);
});
