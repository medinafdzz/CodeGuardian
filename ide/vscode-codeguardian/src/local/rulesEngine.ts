export type LocalSeverity = 'CRITICAL' | 'MAJOR' | 'MINOR';

export type LocalFinding = {
  id: string;
  ruleId: string;
  filePath: string;
  startLine: number;
  startCharacter: number;
  endLine: number;
  endCharacter: number;
  severity: LocalSeverity;
  message: string;
  source: 'CodeGuardian Local';
  originalCode?: string;
  proposedCode?: string;
  explanation?: string;
};

export type LocalRulesConfig = {
  rules: string[];
  maxFindings: number;
};

const DEFAULT_RULES = ['secrets', 'tls', 'subprocess', 'sql', 'debug'];

type RuleMatch = {
  ruleId: string;
  group: string;
  severity: LocalSeverity;
  message: string;
  regex: RegExp;
  proposedCode?: (match: RegExpExecArray, line: string, languageId: string) => string | undefined;
};

const RULES: RuleMatch[] = [
  {
    ruleId: 'hardcoded-secret',
    group: 'secrets',
    severity: 'CRITICAL',
    message: 'Suspicious hardcoded secret detected. Move this value to a secure environment variable or secret store.',
    regex: /\b(password|passwd|pwd|secret|token|api[_-]?key|apikey|private[_-]?key|admin_password)\b\s*(?::[^=]+)?=\s*(['"])(?=.{4,})(?!\$\{)(?!process\.env)(?!System\.getenv)(?!os\.getenv)([^'"]+)\2/gi,
    proposedCode: (match, line, languageId) => {
      const name = normalizeEnvName(match[1]);
      if (languageId === 'python') {
        return line.replace(match[0], `${match[1]} = os.getenv("${name}")`);
      }
      if (languageId === 'java') {
        return line.replace(match[0], `${match[1]} = System.getenv("${name}")`);
      }
      if (['javascript', 'typescript', 'javascriptreact', 'typescriptreact'].includes(languageId)) {
        return line.replace(match[0], `${match[1]} = process.env.${name}`);
      }
      return undefined;
    },
  },
  {
    ruleId: 'tls-verify-disabled',
    group: 'tls',
    severity: 'CRITICAL',
    message: 'TLS certificate verification is disabled.',
    regex: /\bverify\s*=\s*False\b|rejectUnauthorized\s*:\s*false\b/gi,
    proposedCode: (match, line) => line.replace(/\s*,?\s*verify\s*=\s*False\s*,?/i, '').replace(/rejectUnauthorized\s*:\s*false\s*,?/i, ''),
  },
  {
    ruleId: 'shell-true',
    group: 'subprocess',
    severity: 'MAJOR',
    message: 'shell=True can execute through a shell and increase command injection risk.',
    regex: /\bshell\s*=\s*True\b/g,
  },
  {
    ruleId: 'sql-concat',
    group: 'sql',
    severity: 'MAJOR',
    message: 'SQL query appears to be built with string concatenation. Use parameterized queries/placeholders.',
    regex: /(["'`][^"'`]*(select|insert|update|delete)\b[^"'`]*["'`]\s*(\+|\.\s*format\(|%\s*\(|f["'`]))|(\+\s*["'`][^"'`]*(where|and|or|values)\b[^"'`]*["'`])/i,
  },
  {
    ruleId: 'debug-print',
    group: 'debug',
    severity: 'MINOR',
    message: 'Debug output found in source code. Remove it before committing if it is not intentional.',
    regex: /\b(print|console\.log)\s*\(/g,
  },
];

const IGNORE_PATH_PARTS = [
  '/node_modules/',
  '/target/',
  '/build/',
  '/dist/',
  '/.venv/',
  '/venv/',
  '/__pycache__/',
  '/.git/',
];

export function analyzeText(
  text: string,
  languageId: string,
  filePath: string,
  config: Partial<LocalRulesConfig> = {},
): LocalFinding[] {
  if (shouldIgnoreFile(filePath)) {
    return [];
  }
  const enabled = new Set(config.rules?.length ? config.rules : DEFAULT_RULES);
  const maxFindings = Math.max(1, config.maxFindings ?? 50);
  const findings: LocalFinding[] = [];
  const lines = text.split(/\r\n|\r|\n/);
  let inJavaTrustMethod = false;
  let javaTrustMethodLine = 0;
  let javaTrustMethodName = '';

  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    const uncommented = stripInlineComment(line, languageId);
    if (!uncommented.trim()) {
      continue;
    }

    if (enabled.has('tls') && languageId === 'java') {
      const method = uncommented.match(/\bvoid\s+(checkServerTrusted|checkClientTrusted)\s*\([^)]*\)\s*\{\s*$/);
      if (method) {
        inJavaTrustMethod = true;
        javaTrustMethodLine = index;
        javaTrustMethodName = method[1];
      } else if (inJavaTrustMethod && uncommented.trim() === '}') {
        findings.push({
          id: stableId(filePath, 'tls-verify-disabled', javaTrustMethodLine, 0),
          ruleId: 'tls-verify-disabled',
          filePath,
          startLine: javaTrustMethodLine,
          startCharacter: Math.max(0, lines[javaTrustMethodLine].indexOf(javaTrustMethodName)),
          endLine: index,
          endCharacter: line.length,
          severity: 'CRITICAL',
          message: `${javaTrustMethodName} appears to trust certificates without validation.`,
          source: 'CodeGuardian Local',
          originalCode: lines.slice(javaTrustMethodLine, index + 1).join('\n'),
          explanation: 'Empty Java trust manager methods bypass TLS certificate validation.',
        });
        inJavaTrustMethod = false;
      } else if (inJavaTrustMethod && uncommented.trim() && !/^\s*\/\//.test(uncommented)) {
        inJavaTrustMethod = false;
      }
    }

    for (const rule of RULES) {
      if (!enabled.has(rule.group)) {
        continue;
      }
      rule.regex.lastIndex = 0;
      let match: RegExpExecArray | null;
      while ((match = rule.regex.exec(uncommented)) !== null) {
        const start = match.index;
        const end = start + match[0].length;
        const proposedCode = rule.proposedCode?.(match, line, languageId);
        findings.push({
          id: stableId(filePath, rule.ruleId, index, start),
          ruleId: rule.ruleId,
          filePath,
          startLine: index,
          startCharacter: start,
          endLine: index,
          endCharacter: end,
          severity: rule.severity,
          message: rule.message,
          source: 'CodeGuardian Local',
          originalCode: line,
          proposedCode,
        });
        if (findings.length >= maxFindings) {
          return findings;
        }
      }
    }
    if (findings.length >= maxFindings) {
      return findings;
    }
  }
  return findings.slice(0, maxFindings);
}

export function shouldIgnoreFile(filePath: string): boolean {
  const normalized = `/${filePath.replace(/\\/g, '/')}`;
  return IGNORE_PATH_PARTS.some((part) => normalized.includes(part));
}

function stripInlineComment(line: string, languageId: string): string {
  if (languageId === 'python') {
    return stripOutsideQuotes(line, '#');
  }
  return stripOutsideQuotes(line, '//');
}

function stripOutsideQuotes(line: string, marker: string): string {
  let quote = '';
  for (let i = 0; i < line.length; i += 1) {
    const char = line[i];
    if ((char === '"' || char === "'") && line[i - 1] !== '\\') {
      quote = quote === char ? '' : quote || char;
    }
    if (!quote && line.slice(i, i + marker.length) === marker) {
      return line.slice(0, i);
    }
  }
  return line;
}

function normalizeEnvName(value: string): string {
  return value.replace(/([a-z])([A-Z])/g, '$1_$2').replace(/[^a-zA-Z0-9]+/g, '_').toUpperCase();
}

function stableId(filePath: string, ruleId: string, line: number, character: number): string {
  return `${ruleId}:${filePath.replace(/\\/g, '/')}:${line + 1}:${character + 1}`;
}
