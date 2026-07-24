# Stock Automation Harness Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create a repository-native foundation that sanitizes the three current n8n exports, audits their structure and secrets, and verifies the result through one PowerShell command.

**Architecture:** Use dependency-free ECMAScript modules and the Node built-in test runner for deterministic sanitization and audit logic. Keep the PowerShell entrypoint thin: it invokes the Node tests and then audits every committed workflow export. This plan deliberately stops before database schema capture, KRX network integration, ATR calculation, or production n8n changes.

**Tech Stack:** Node.js 20 or newer, ECMAScript modules, `node:test`, Windows PowerShell 5.1, Git, n8n workflow JSON.

## Global Constraints

- Work only in `C:\Users\USER\OneDrive\ドキュメント\[study] 주식 자동화`.
- Use Node.js 20 or newer and Windows PowerShell 5.1-compatible syntax.
- Add no third-party runtime or test dependency.
- Treat `C:\Users\USER\Downloads\관심종목 플로우.json`, `C:\Users\USER\Downloads\주식 뉴스 자동화.json`, and `C:\Users\USER\Downloads\chat.json` as read-only source exports.
- Never copy credential objects, API keys, tokens, passwords, or literal sensitive HTTP header values into the repository.
- Every committed workflow must have `active: false`.
- Do not import, activate, or modify a production n8n workflow.
- Do not connect to or modify PostgreSQL, KRX, Gemini, or Slack.
- Use test-first development for JavaScript behavior. Documentation, JSON generation, and the PowerShell wrapper follow the configuration-file exception approved in the design.
- Run `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/verify.ps1` before claiming completion.

---

## File Map

- `AGENTS.md`: concise repository map, commands, safety boundaries, and completion rule.
- `ARCHITECTURE.md`: current scope, data-flow boundary, and links to the approved design.
- `.gitignore`: local secrets, caches, logs, and generated execution artifacts.
- `package.json`: dependency-free Node scripts and runtime floor.
- `docs/decisions/0001-krx-open-api.md`: records KRX as the approved first market-data provider.
- `scripts/lib/sanitize-n8n-export.mjs`: pure sanitization functions.
- `scripts/sanitize-n8n-export.mjs`: command-line adapter for one input and one output.
- `tests/sanitize-n8n-export.test.mjs`: sanitizer behavior tests.
- `workflows/n8n/*.json`: sanitized, inactive copies of the three source exports.
- `scripts/lib/audit-workflows.mjs`: pure workflow validation and directory audit functions.
- `scripts/audit-workflows.mjs`: command-line audit adapter.
- `tests/audit-workflows.test.mjs`: workflow audit behavior tests.
- `scripts/verify.ps1`: single verification entrypoint.

## Deferred Plans

This foundation is followed by separate, independently reviewable plans:

1. Current workflow hardening: remove fake empty-news rows, expose Gemini parse failures, remove hard-coded dates, and define duplicate-news behavior.
2. PostgreSQL contract capture: obtain the current schema through a read-only export, version migrations, and test constraints.
3. KRX market-data ingestion: authenticate, validate fixtures, normalize OHLCV, and implement idempotent storage.
4. ATR and signal calculation: implement Wilder ATR, insufficient-history behavior, and an approved deterministic price formula.

---

### Task 1: Repository Navigation and Runtime Baseline

**Files:**
- Create: `AGENTS.md`
- Create: `ARCHITECTURE.md`
- Create: `.gitignore`
- Create: `package.json`
- Create: `docs/decisions/0001-krx-open-api.md`

**Interfaces:**
- Consumes: approved design at `docs/superpowers/specs/2026-07-24-stock-automation-harness-design.md`.
- Produces: `npm.cmd test`, `npm.cmd run audit:workflows`, and `npm run verify` command contracts used by every later task.

- [ ] **Step 1: Create the repository agent map**

Create `AGENTS.md` with this exact content:

```markdown
# Stock Automation Agent Guide

## Start Here

- Architecture: `ARCHITECTURE.md`
- Approved harness design: `docs/superpowers/specs/2026-07-24-stock-automation-harness-design.md`
- Active implementation plans: `docs/superpowers/plans/`
- Provider decisions: `docs/decisions/`
- Sanitized n8n exports: `workflows/n8n/`

## Commands

- Full verification: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/verify.ps1`
- Node tests: `npm.cmd test`
- Workflow audit: `npm.cmd run audit:workflows`

## Rules

- Use KRX Open API as the first daily OHLCV provider.
- Keep Gemini limited to sentiment and market-scenario analysis.
- Do not calculate or store entry, target, or stop prices without validated OHLCV and ATR.
- Never commit credentials, API keys, tokens, passwords, or production data.
- Keep committed n8n workflows inactive and sanitized.
- Treat n8n activation, production database changes, real Slack sends, and external pushes as approval-required actions.
- Parallel agents may perform read-only audits, but writes to the same workflow, migration sequence, or file must be serialized.
- Do not claim completion without fresh output from the full verification command.
```

- [ ] **Step 2: Create the architecture entrypoint**

Create `ARCHITECTURE.md` with this exact content:

````markdown
# Stock Automation Architecture

## Current Foundation Scope

This repository versions sanitized n8n workflow exports and the local verification harness. It does not yet contain a verified PostgreSQL schema snapshot, KRX network integration, ATR implementation, or production deployment configuration.

## Intended Data Flow

```text
KRX daily OHLCV
  -> boundary validation
  -> normalized idempotent storage
  -> minimum-history check
  -> Wilder ATR

Naver news
  -> normalization and deduplication
  -> Gemini sentiment validation
  -> validated OHLCV and ATR lookup
  -> deterministic signal calculation
  -> PostgreSQL
  -> Slack notification
```

## Source of Truth

- Harness design: `docs/superpowers/specs/2026-07-24-stock-automation-harness-design.md`
- Provider decision: `docs/decisions/0001-krx-open-api.md`
- Repository workflow copies: `workflows/n8n/`
- Full verification command: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/verify.ps1`

Production n8n state and production PostgreSQL state remain external and must be revalidated before any operational change.
````

- [ ] **Step 3: Record the provider decision**

Create `docs/decisions/0001-krx-open-api.md` with this exact content:

```markdown
# Decision 0001: KRX Open API for Daily Market Data

Date: 2026-07-24

Status: Accepted

## Decision

Use KRX Open API as the first provider for daily OHLCV collection.

## Reason

The current project needs verified daily prices and ATR before it can create price-based signals. KRX provides an official daily-market-data path without making brokerage-account integration part of the first milestone.

## Consequences

- KRX credentials remain outside Git.
- The first ingestion interface targets daily OHLCV, not real-time quotes or orders.
- KIS remains outside the current scope and can be added as a separate adapter if real-time or brokerage functions are approved.
- Price signals remain disabled until OHLCV history and ATR pass their validation gates.
```

- [ ] **Step 4: Add runtime metadata and ignore rules**

Create `package.json`:

```json
{
  "name": "stock-automation-harness",
  "version": "0.1.0",
  "private": true,
  "type": "module",
  "engines": {
    "node": ">=20"
  },
  "scripts": {
    "test": "node --test",
    "audit:workflows": "node scripts/audit-workflows.mjs workflows/n8n",
    "verify": "powershell -NoProfile -ExecutionPolicy Bypass -File scripts/verify.ps1"
  }
}
```

Create `.gitignore`:

```gitignore
.env
.env.*
!.env.example
node_modules/
coverage/
logs/
tmp/
*.log
*.local.json
```

- [ ] **Step 5: Verify the baseline files**

Run:

```powershell
node -e "const p=require('./package.json'); if(p.private!==true || p.type!=='module' || p.engines.node!=='>=20') process.exit(1)"
rg -n "scripts/verify.ps1|KRX Open API|production" AGENTS.md ARCHITECTURE.md docs/decisions/0001-krx-open-api.md
```

Expected: Node exits `0`; `rg` finds the verification command, KRX decision, and production boundaries.

- [ ] **Step 6: Commit the repository baseline**

```powershell
git add AGENTS.md ARCHITECTURE.md .gitignore package.json docs/decisions/0001-krx-open-api.md
git commit -m "docs: add stock automation repository baseline"
```

---

### Task 2: Tested n8n Export Sanitizer

**Files:**
- Create: `tests/sanitize-n8n-export.test.mjs`
- Create: `scripts/lib/sanitize-n8n-export.mjs`
- Create: `scripts/sanitize-n8n-export.mjs`

**Interfaces:**
- Consumes: a parsed n8n workflow object or an input JSON path.
- Produces: `sanitizeWorkflow(workflow)`, `serializeWorkflow(workflow)`, and CLI usage `node scripts/sanitize-n8n-export.mjs <input> <output>`.

- [ ] **Step 1: Write failing sanitizer tests**

Create `tests/sanitize-n8n-export.test.mjs`:

```javascript
import test from "node:test";
import assert from "node:assert/strict";

import {
  sanitizeWorkflow,
  serializeWorkflow,
} from "../scripts/lib/sanitize-n8n-export.mjs";

function sampleWorkflow() {
  return {
    id: "workflow-id",
    versionId: "version-id",
    active: true,
    tags: [{ id: "tag-id", name: "prod" }],
    pinData: { Trigger: [{ json: { secret: "sample" } }] },
    meta: { instanceId: "instance-id" },
    nodes: [
      {
        name: "HTTP Request",
        type: "n8n-nodes-base.httpRequest",
        credentials: {
          httpHeaderAuth: { id: "credential-id", name: "Naver" },
        },
        parameters: {
          headerParameters: {
            parameters: [
              { name: "X-Naver-Client-Id", value: "literal-client-id" },
              { name: "X-Naver-Client-Secret", value: "literal-secret" },
              { name: "Accept", value: "application/json" },
            ],
          },
        },
      },
    ],
    connections: {},
  };
}

test("sanitizer removes workflow and node credential metadata", () => {
  const result = sanitizeWorkflow(sampleWorkflow());

  assert.equal(result.active, false);
  assert.equal("id" in result, false);
  assert.equal("versionId" in result, false);
  assert.equal("tags" in result, false);
  assert.equal("pinData" in result, false);
  assert.equal("meta" in result, false);
  assert.equal("credentials" in result.nodes[0], false);
});

test("sanitizer replaces sensitive literal headers with environment expressions", () => {
  const result = sanitizeWorkflow(sampleWorkflow());
  const headers =
    result.nodes[0].parameters.headerParameters.parameters;

  assert.deepEqual(headers, [
    {
      name: "X-Naver-Client-Id",
      value: "={{ $env.X_NAVER_CLIENT_ID }}",
    },
    {
      name: "X-Naver-Client-Secret",
      value: "={{ $env.X_NAVER_CLIENT_SECRET }}",
    },
    { name: "Accept", value: "application/json" },
  ]);
});

test("sanitizer does not mutate the source workflow", () => {
  const source = sampleWorkflow();
  sanitizeWorkflow(source);

  assert.equal(source.active, true);
  assert.equal(source.nodes[0].credentials.httpHeaderAuth.id, "credential-id");
});

test("serializer produces stable pretty JSON with one trailing newline", () => {
  assert.equal(
    serializeWorkflow({ connections: {}, nodes: [], active: false }),
    '{\n  "connections": {},\n  "nodes": [],\n  "active": false\n}\n',
  );
});

test("sanitizer rejects a document without a nodes array", () => {
  assert.throws(
    () => sanitizeWorkflow({ connections: {} }),
    /workflow\.nodes must be an array/,
  );
});
```

- [ ] **Step 2: Run the tests and confirm the expected failure**

Run:

```powershell
node --test tests/sanitize-n8n-export.test.mjs
```

Expected: FAIL with `ERR_MODULE_NOT_FOUND` for `scripts/lib/sanitize-n8n-export.mjs`.

- [ ] **Step 3: Implement the pure sanitizer**

Create `scripts/lib/sanitize-n8n-export.mjs`:

```javascript
const REMOVED_TOP_LEVEL_KEYS = new Set([
  "id",
  "versionId",
  "createdAt",
  "updatedAt",
  "shared",
  "tags",
  "meta",
  "pinData",
  "staticData",
  "triggerCount",
]);

const SENSITIVE_NAME_PATTERN =
  /(authorization|client[-_ ]?id|client[-_ ]?secret|api[-_ ]?key|password|token)/i;

function envName(name) {
  return String(name)
    .trim()
    .replace(/[^A-Za-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .toUpperCase();
}

function sanitizeObject(value) {
  if (Array.isArray(value)) {
    return value.map(sanitizeObject);
  }

  if (value === null || typeof value !== "object") {
    return value;
  }

  const result = {};
  for (const [key, child] of Object.entries(value)) {
    if (key === "credentials") {
      continue;
    }
    result[key] = sanitizeObject(child);
  }

  if (
    typeof result.name === "string" &&
    SENSITIVE_NAME_PATTERN.test(result.name) &&
    typeof result.value === "string" &&
    !result.value.trim().startsWith("={{")
  ) {
    result.value = `={{ $env.${envName(result.name)} }}`;
  }

  return result;
}

export function sanitizeWorkflow(workflow) {
  if (
    workflow === null ||
    typeof workflow !== "object" ||
    Array.isArray(workflow)
  ) {
    throw new TypeError("workflow must be an object");
  }
  if (!Array.isArray(workflow.nodes)) {
    throw new TypeError("workflow.nodes must be an array");
  }

  const result = sanitizeObject(workflow);
  for (const key of REMOVED_TOP_LEVEL_KEYS) {
    delete result[key];
  }
  result.active = false;
  return result;
}

export function serializeWorkflow(workflow) {
  return `${JSON.stringify(workflow, null, 2)}\n`;
}
```

- [ ] **Step 4: Implement the CLI adapter**

Create `scripts/sanitize-n8n-export.mjs`:

```javascript
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";

import {
  sanitizeWorkflow,
  serializeWorkflow,
} from "./lib/sanitize-n8n-export.mjs";

const [, , inputArg, outputArg] = process.argv;

if (!inputArg || !outputArg) {
  console.error(
    "Usage: node scripts/sanitize-n8n-export.mjs <input> <output>",
  );
  process.exit(2);
}

const inputPath = resolve(inputArg);
const outputPath = resolve(outputArg);
const source = JSON.parse(await readFile(inputPath, "utf8"));
const sanitized = sanitizeWorkflow(source);

await mkdir(dirname(outputPath), { recursive: true });
await writeFile(outputPath, serializeWorkflow(sanitized), "utf8");
console.log(`Sanitized workflow written: ${outputPath}`);
```

- [ ] **Step 5: Run the sanitizer tests**

Run:

```powershell
node --test tests/sanitize-n8n-export.test.mjs
```

Expected: 5 tests pass and 0 tests fail.

- [ ] **Step 6: Commit the sanitizer**

```powershell
git add scripts/lib/sanitize-n8n-export.mjs scripts/sanitize-n8n-export.mjs tests/sanitize-n8n-export.test.mjs
git commit -m "feat: add tested n8n export sanitizer"
```

---

### Task 3: Sanitize and Import the Current Workflow Exports

**Files:**
- Create: `workflows/n8n/관심종목-플로우.json`
- Create: `workflows/n8n/주식-뉴스-자동화.json`
- Create: `workflows/n8n/chat.json`

**Interfaces:**
- Consumes: the three read-only source paths and the Task 2 CLI.
- Produces: inactive repository copies with no credential objects or literal sensitive headers.

- [ ] **Step 1: Generate all three repository copies**

Run:

```powershell
node scripts/sanitize-n8n-export.mjs "C:\Users\USER\Downloads\관심종목 플로우.json" "workflows\n8n\관심종목-플로우.json"
node scripts/sanitize-n8n-export.mjs "C:\Users\USER\Downloads\주식 뉴스 자동화.json" "workflows\n8n\주식-뉴스-자동화.json"
node scripts/sanitize-n8n-export.mjs "C:\Users\USER\Downloads\chat.json" "workflows\n8n\chat.json"
```

Expected: three `Sanitized workflow written:` messages.

- [ ] **Step 2: Verify parseability, inactive state, and credential removal**

Run:

```powershell
$files = Get-ChildItem -LiteralPath 'workflows\n8n' -Filter '*.json'
if ($files.Count -ne 3) {
    throw "Expected 3 workflow files, found $($files.Count)."
}
foreach ($file in $files) {
    $raw = Get-Content -LiteralPath $file.FullName -Raw -Encoding UTF8
    $workflow = $raw | ConvertFrom-Json
    if ($workflow.active -ne $false -or $null -eq $workflow.nodes -or $null -eq $workflow.connections) {
        throw "Invalid workflow shape: $($file.FullName)"
    }
    if ($raw -match '"credentials"\s*:') {
        throw "Credential object found: $($file.FullName)"
    }
}
```

Expected: exit code `0` with no output.

- [ ] **Step 3: Inspect the staged content for suspicious literals**

Run:

```powershell
rg -n -i "sk-[a-z0-9]|AIza[a-z0-9_-]|bearer [a-z0-9._-]+|client-secret.*literal|password.*literal" workflows/n8n
```

Expected: no matches and `rg` exit code `1`.

- [ ] **Step 4: Commit the sanitized exports**

```powershell
git add workflows/n8n
git commit -m "chore: add sanitized n8n workflow exports"
```

---

### Task 4: Tested Workflow Structure and Secret Audit

**Files:**
- Create: `tests/audit-workflows.test.mjs`
- Create: `scripts/lib/audit-workflows.mjs`
- Create: `scripts/audit-workflows.mjs`

**Interfaces:**
- Consumes: parsed workflow objects or a directory containing `.json` exports.
- Produces: `auditWorkflow(workflow, fileName)`, `auditDirectory(directory)`, and a CLI that exits `1` when findings exist.

- [ ] **Step 1: Write failing audit tests**

Create `tests/audit-workflows.test.mjs`:

```javascript
import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";

import {
  auditDirectory,
  auditWorkflow,
} from "../scripts/lib/audit-workflows.mjs";

function validWorkflow() {
  return {
    name: "workflow",
    active: false,
    nodes: [
      { name: "Trigger", type: "n8n-nodes-base.manualTrigger", parameters: {} },
    ],
    connections: {},
  };
}

test("valid inactive workflow has no findings", () => {
  assert.deepEqual(auditWorkflow(validWorkflow(), "valid.json"), []);
});

test("audit reports active workflows and duplicate node names", () => {
  const workflow = validWorkflow();
  workflow.active = true;
  workflow.nodes.push({ ...workflow.nodes[0] });

  assert.deepEqual(auditWorkflow(workflow, "bad.json"), [
    {
      file: "bad.json",
      code: "WORKFLOW_ACTIVE",
      detail: "Committed workflow must have active=false",
    },
    {
      file: "bad.json",
      code: "DUPLICATE_NODE_NAME",
      detail: "Duplicate node name: Trigger",
    },
  ]);
});

test("audit reports credential objects and literal sensitive headers", () => {
  const workflow = validWorkflow();
  workflow.nodes[0].credentials = { api: { id: "id", name: "name" } };
  workflow.nodes[0].parameters.headers = [
    { name: "Authorization", value: "Bearer literal-token" },
  ];

  assert.deepEqual(auditWorkflow(workflow, "secret.json"), [
    {
      file: "secret.json",
      code: "CREDENTIAL_OBJECT",
      detail: "credentials key found at $.nodes[0].credentials",
    },
    {
      file: "secret.json",
      code: "LITERAL_SENSITIVE_VALUE",
      detail: "Sensitive value must use an environment expression at $.nodes[0].parameters.headers[0]",
    },
  ]);
});

test("directory audit reports invalid JSON without stopping other files", async () => {
  const directory = await mkdtemp(join(tmpdir(), "workflow-audit-"));
  await writeFile(
    join(directory, "valid.json"),
    JSON.stringify(validWorkflow()),
    "utf8",
  );
  await writeFile(join(directory, "invalid.json"), "{", "utf8");

  assert.deepEqual(await auditDirectory(directory), [
    {
      file: "invalid.json",
      code: "INVALID_JSON",
      detail: "Workflow file is not valid JSON",
    },
  ]);
});
```

- [ ] **Step 2: Run the audit tests and confirm the expected failure**

Run:

```powershell
node --test tests/audit-workflows.test.mjs
```

Expected: FAIL with `ERR_MODULE_NOT_FOUND` for `scripts/lib/audit-workflows.mjs`.

- [ ] **Step 3: Implement workflow auditing**

Create `scripts/lib/audit-workflows.mjs`:

```javascript
import { readdir, readFile } from "node:fs/promises";
import { join } from "node:path";

const SENSITIVE_NAME_PATTERN =
  /(authorization|client[-_ ]?id|client[-_ ]?secret|api[-_ ]?key|password|token)/i;

function finding(file, code, detail) {
  return { file, code, detail };
}

function walk(value, path, visit) {
  if (Array.isArray(value)) {
    value.forEach((child, index) => walk(child, `${path}[${index}]`, visit));
    return;
  }
  if (value === null || typeof value !== "object") {
    return;
  }

  visit(value, path);
  for (const [key, child] of Object.entries(value)) {
    if (child !== null && typeof child === "object") {
      walk(child, `${path}.${key}`, visit);
    }
  }
}

export function auditWorkflow(workflow, fileName) {
  const findings = [];

  if (
    workflow === null ||
    typeof workflow !== "object" ||
    Array.isArray(workflow) ||
    !Array.isArray(workflow.nodes) ||
    workflow.connections === null ||
    typeof workflow.connections !== "object"
  ) {
    return [
      finding(
        fileName,
        "INVALID_WORKFLOW_SHAPE",
        "Workflow requires object nodes[] and connections{}",
      ),
    ];
  }

  if (workflow.active !== false) {
    findings.push(
      finding(
        fileName,
        "WORKFLOW_ACTIVE",
        "Committed workflow must have active=false",
      ),
    );
  }

  const seenNames = new Set();
  for (const node of workflow.nodes) {
    if (seenNames.has(node.name)) {
      findings.push(
        finding(
          fileName,
          "DUPLICATE_NODE_NAME",
          `Duplicate node name: ${node.name}`,
        ),
      );
    }
    seenNames.add(node.name);
  }

  walk(workflow, "$", (object, path) => {
    if (Object.hasOwn(object, "credentials")) {
      findings.push(
        finding(
          fileName,
          "CREDENTIAL_OBJECT",
          `credentials key found at ${path}.credentials`,
        ),
      );
    }

    if (
      typeof object.name === "string" &&
      SENSITIVE_NAME_PATTERN.test(object.name) &&
      typeof object.value === "string" &&
      object.value.trim() !== "" &&
      !object.value.trim().startsWith("={{")
    ) {
      findings.push(
        finding(
          fileName,
          "LITERAL_SENSITIVE_VALUE",
          `Sensitive value must use an environment expression at ${path}`,
        ),
      );
    }
  });

  return findings;
}

export async function auditDirectory(directory) {
  const files = (await readdir(directory))
    .filter((name) => name.endsWith(".json"))
    .sort();
  const findings = [];

  for (const file of files) {
    try {
      const workflow = JSON.parse(
        await readFile(join(directory, file), "utf8"),
      );
      findings.push(...auditWorkflow(workflow, file));
    } catch (error) {
      if (error instanceof SyntaxError) {
        findings.push(
          finding(file, "INVALID_JSON", "Workflow file is not valid JSON"),
        );
        continue;
      }
      throw error;
    }
  }

  return findings;
}
```

- [ ] **Step 4: Implement the audit CLI**

Create `scripts/audit-workflows.mjs`:

```javascript
import { resolve } from "node:path";

import { auditDirectory } from "./lib/audit-workflows.mjs";

const directory = resolve(process.argv[2] ?? "workflows/n8n");
const findings = await auditDirectory(directory);

if (findings.length > 0) {
  for (const item of findings) {
    console.error(`${item.file} [${item.code}] ${item.detail}`);
  }
  process.exit(1);
}

console.log(`Workflow audit passed: ${directory}`);
```

- [ ] **Step 5: Run focused and repository audit verification**

Run:

```powershell
node --test tests/audit-workflows.test.mjs
npm.cmd run audit:workflows
```

Expected: 4 tests pass, then the committed workflow directory audit exits `0`.

- [ ] **Step 6: Commit workflow auditing**

```powershell
git add scripts/lib/audit-workflows.mjs scripts/audit-workflows.mjs tests/audit-workflows.test.mjs
git commit -m "test: enforce safe inactive workflow exports"
```

---

### Task 5: Single Verification Entrypoint

**Files:**
- Create: `scripts/verify.ps1`

**Interfaces:**
- Consumes: `npm.cmd test` and `npm.cmd run audit:workflows`.
- Produces: one PowerShell command that exits `0` only if both checks pass.

- [ ] **Step 1: Create the PowerShell verification wrapper**

Create `scripts/verify.ps1`:

```powershell
$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $projectRoot

try {
    & npm.cmd test
    if ($LASTEXITCODE -ne 0) {
        throw "Node tests failed with exit code $LASTEXITCODE."
    }

    & npm.cmd run audit:workflows
    if ($LASTEXITCODE -ne 0) {
        throw "Workflow audit failed with exit code $LASTEXITCODE."
    }

    Write-Output 'Verification passed.'
}
finally {
    Pop-Location
}
```

- [ ] **Step 2: Confirm the wrapper detects a failing audit**

Make a temporary copy outside the repository workflow directory and run the CLI against it:

```powershell
$caseDir = Join-Path $env:TEMP 'stock-automation-invalid-workflow'
New-Item -ItemType Directory -Force -Path $caseDir | Out-Null
Set-Content -LiteralPath (Join-Path $caseDir 'active.json') -Encoding UTF8 -Value '{"name":"active","active":true,"nodes":[],"connections":{}}'
node scripts/audit-workflows.mjs $caseDir
```

Expected: exit code `1` with `[WORKFLOW_ACTIVE]`.

Remove only the exact temporary directory after resolving and checking it is below `$env:TEMP`:

```powershell
$resolvedCaseDir = (Resolve-Path -LiteralPath $caseDir).Path
$resolvedTemp = (Resolve-Path -LiteralPath $env:TEMP).Path
if (-not $resolvedCaseDir.StartsWith($resolvedTemp, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to remove a path outside TEMP: $resolvedCaseDir"
}
Remove-Item -LiteralPath $resolvedCaseDir -Recurse -Force
```

- [ ] **Step 3: Run the complete verification command**

Run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/verify.ps1
```

Expected:

```text
tests 9
pass 9
fail 0
Workflow audit passed:
Verification passed.
```

Node may print additional timing lines. The required evidence is exit code `0`, 9 passing tests, 0 failing tests, and both final success messages.

- [ ] **Step 4: Check documentation links and Git whitespace**

Run:

```powershell
$paths = @(
    'docs/superpowers/specs/2026-07-24-stock-automation-harness-design.md',
    'docs/superpowers/plans/2026-07-24-harness-foundation.md',
    'docs/decisions/0001-krx-open-api.md',
    'workflows/n8n'
)
foreach ($path in $paths) {
    if (-not (Test-Path -LiteralPath $path)) {
        throw "Missing documented path: $path"
    }
}
git diff --check
```

Expected: all documented paths exist and `git diff --check` emits no errors.

- [ ] **Step 5: Commit the verification entrypoint**

```powershell
git add scripts/verify.ps1
git commit -m "test: add stock automation verification entrypoint"
```

- [ ] **Step 6: Perform final plan-level verification**

Run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/verify.ps1
git status --short
git log -6 --oneline
```

Expected: verification exits `0`; `git status --short` is empty; the last six commits contain the design commit and the five foundation implementation commits.

## Plan Completion Boundary

Completion of this plan proves only the following:

- Repository guidance and KRX provider decision are versioned.
- The three current n8n exports have sanitized, inactive repository copies.
- Sanitization and workflow structural auditing have automated tests.
- One command verifies the committed foundation.

It does not prove that production n8n runs, the live PostgreSQL schema matches prior evidence, KRX authentication works, OHLCV has been stored, ATR is correct in production, or Slack notifications have been delivered.
