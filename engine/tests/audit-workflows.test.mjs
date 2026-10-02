import test from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
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

function knownSecretSamples() {
  return {
    github: ["gh", "p_", "A".repeat(36)].join(""),
    slack: ["xo", "xb-", "1".repeat(12), "-", "A".repeat(24)].join(""),
    pem: [
      "-----BEGIN ",
      "PRIVATE KEY",
      "-----\nsynthetic\n-----END ",
      "PRIVATE KEY",
      "-----",
    ].join(""),
    aws: ["AK", "IA", "A".repeat(16)].join(""),
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

test("audit reports direct sensitive keys and non-environment expressions without exposing values", () => {
  const workflow = validWorkflow();
  workflow.nodes[0].parameters = {
    clientSecret: "do-not-print-direct",
    tokenizer: "not-sensitive",
    headers: [
      { name: "Authorization", value: "={{ $json.do_not_print_header }}" },
    ],
  };

  const findings = auditWorkflow(workflow, "secret.json");

  assert.deepEqual(findings, [
    {
      file: "secret.json",
      code: "LITERAL_SENSITIVE_VALUE",
      detail: "Sensitive value must use an environment expression at $.nodes[0].parameters.clientSecret",
    },
    {
      file: "secret.json",
      code: "LITERAL_SENSITIVE_VALUE",
      detail: "Sensitive value must use an environment expression at $.nodes[0].parameters.headers[0]",
    },
  ]);
  assert.equal(JSON.stringify(findings).includes("do-not-print-direct"), false);
  assert.equal(JSON.stringify(findings).includes("do_not_print_header"), false);
});

test("audit recognizes bounded secret aliases across naming styles", () => {
  const workflow = validWorkflow();
  workflow.nodes[0].parameters = {
    accessToken: "literal",
    refresh_token: "literal",
    "bearer-token": "literal",
    passwordHash: "literal",
    password_value: "literal",
    privateKey: "literal",
    ssh_private_key: "literal",
    "signing-key": "literal",
    signingSecret: "literal",
    webhook_secret: "literal",
    tokenization: "not-sensitive",
    keyValue: "not-sensitive",
  };

  const findings = auditWorkflow(workflow, "aliases.json");

  assert.equal(findings.length, 10);
  assert.equal(
    findings.every((item) => item.code === "LITERAL_SENSITIVE_VALUE"),
    true,
  );
  assert.equal(
    findings.some((item) => item.detail.endsWith(".tokenization")),
    false,
  );
  assert.equal(
    findings.some((item) => item.detail.endsWith(".keyValue")),
    false,
  );
});

test("audit recognizes approved value suffix chains for direct and header keys", () => {
  const workflow = validWorkflow();
  workflow.nodes[0].parameters = {
    secretKey: "literal",
    client_secret_key: "literal",
    "api-secret-key": "literal",
    privateKeyPem: "literal",
    authorization_header: "literal",
    headers: [
      { name: "X-Custom-Secret-Key", value: "literal" },
      { name: "X_Custom_Client_Secret_Key", value: "literal" },
      { name: "X-Custom-Private-Key-Pem", value: "literal" },
      { name: "X_Custom_Authorization_Header", value: "literal" },
    ],
  };

  const findings = auditWorkflow(workflow, "suffixes.json");

  assert.equal(findings.length, 9);
  assert.equal(
    findings.every((item) => item.code === "LITERAL_SENSITIVE_VALUE"),
    true,
  );
});

test("audit ignores credential metadata suffixes across naming styles", () => {
  const workflow = validWorkflow();
  workflow.nodes[0].parameters = {
    tokenUrl: "https://example.invalid/token",
    token_endpoint: "https://example.invalid/token",
    "token-expiry": 3600,
    passwordPolicy: "strong",
    password_location: "header",
    "password-format": "opaque",
    secretName: "reference-name",
    secret_ttl: 60,
    "secret-type": "reference",
    privateKeyAlgorithm: "synthetic-algorithm",
    private_key_format: "synthetic-format",
    "signing-key-length": 2048,
    accessTokenExpiry: 3600,
    refresh_token_expires: 7200,
    "bearer-token-ttl": 1800,
    clientSecretLocation: "vault",
    api_key_name: "X-Api-Key",
    authorizationUrl: "https://example.invalid/authorize",
    secretKeyAlgorithm: "synthetic-algorithm",
    "client-secret-key-location": "vault",
    authorizationHeaderName: "Authorization",
  };

  assert.deepEqual(auditWorkflow(workflow, "metadata.json"), []);
});

test("audit detects known secret signatures by path without exposing values", () => {
  const workflow = validWorkflow();
  const samples = knownSecretSamples();
  workflow.nodes[0].parameters.metadata = {
    githubCredential: samples.github,
    slackCredential: samples.slack,
    privateMaterial: samples.pem,
    awsCredential: samples.aws,
    computedValue: `={{ "${samples.aws}" }}`,
    safeReference: "={{ $env.AWS_ACCESS_KEY_ID }}",
  };

  const findings = auditWorkflow(workflow, "known.json");

  assert.deepEqual(findings, [
    {
      file: "known.json",
      code: "KNOWN_SECRET_SIGNATURE",
      detail: "Known secret signature found at $.nodes[0].parameters.metadata.githubCredential",
    },
    {
      file: "known.json",
      code: "KNOWN_SECRET_SIGNATURE",
      detail: "Known secret signature found at $.nodes[0].parameters.metadata.slackCredential",
    },
    {
      file: "known.json",
      code: "KNOWN_SECRET_SIGNATURE",
      detail: "Known secret signature found at $.nodes[0].parameters.metadata.privateMaterial",
    },
    {
      file: "known.json",
      code: "KNOWN_SECRET_SIGNATURE",
      detail: "Known secret signature found at $.nodes[0].parameters.metadata.awsCredential",
    },
    {
      file: "known.json",
      code: "KNOWN_SECRET_SIGNATURE",
      detail: "Known secret signature found at $.nodes[0].parameters.metadata.computedValue",
    },
  ]);
  for (const value of Object.values(samples)) {
    assert.equal(JSON.stringify(findings).includes(value), false);
  }
});

test("audit detects known signatures in primitive array elements", () => {
  const workflow = validWorkflow();
  const synthesizedSecret = knownSecretSamples().slack;
  workflow.nodes[0].parameters.values = ["safe", synthesizedSecret];

  const findings = auditWorkflow(workflow, "array.json");

  assert.deepEqual(findings, [
    {
      file: "array.json",
      code: "KNOWN_SECRET_SIGNATURE",
      detail: "Known secret signature found at $.nodes[0].parameters.values[1]",
    },
  ]);
  assert.equal(JSON.stringify(findings).includes(synthesizedSecret), false);
});

test("audit rejects array connections and malformed nodes", () => {
  const arrayConnections = validWorkflow();
  arrayConnections.connections = [];
  assert.deepEqual(auditWorkflow(arrayConnections, "connections.json"), [
    {
      file: "connections.json",
      code: "INVALID_WORKFLOW_SHAPE",
      detail: "Workflow requires object nodes[] and connections{}",
    },
  ]);

  const malformedNodes = validWorkflow();
  malformedNodes.nodes = [
    null,
    [],
    { name: " ", type: "n8n-nodes-base.noOp" },
    { name: "Missing type", type: "" },
  ];
  assert.deepEqual(auditWorkflow(malformedNodes, "nodes.json"), [
    {
      file: "nodes.json",
      code: "INVALID_NODE_SHAPE",
      detail: "Node at index 0 requires a plain object with non-empty name and type",
    },
    {
      file: "nodes.json",
      code: "INVALID_NODE_SHAPE",
      detail: "Node at index 1 requires a plain object with non-empty name and type",
    },
    {
      file: "nodes.json",
      code: "INVALID_NODE_SHAPE",
      detail: "Node at index 2 requires a plain object with non-empty name and type",
    },
    {
      file: "nodes.json",
      code: "INVALID_NODE_SHAPE",
      detail: "Node at index 3 requires a plain object with non-empty name and type",
    },
  ]);
});

test("directory audit reports invalid JSON and continues to an active peer", async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "workflow-audit-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const active = validWorkflow();
  active.active = true;
  await writeFile(
    join(directory, "active.json"),
    JSON.stringify(active),
    "utf8",
  );
  await writeFile(join(directory, "invalid.json"), "{", "utf8");

  assert.deepEqual(await auditDirectory(directory), [
    {
      file: "active.json",
      code: "WORKFLOW_ACTIVE",
      detail: "Committed workflow must have active=false",
    },
    {
      file: "invalid.json",
      code: "INVALID_JSON",
      detail: "Workflow file is not valid JSON",
    },
  ]);
});

test("audit CLI exits 1, prints categories, and never prints secret values", async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "workflow-audit-cli-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const workflow = validWorkflow();
  const synthesizedSecret = knownSecretSamples().github;
  workflow.nodes[0].parameters.metadata = synthesizedSecret;
  await writeFile(
    join(directory, "secret.json"),
    JSON.stringify(workflow),
    "utf8",
  );

  const cliPath = fileURLToPath(
    new URL("../scripts/audit-workflows.mjs", import.meta.url),
  );
  const result = spawnSync(process.execPath, [cliPath, directory], {
    encoding: "utf8",
  });
  const output = `${result.stdout}${result.stderr}`;

  assert.equal(result.status, 1);
  assert.match(output, /\[KNOWN_SECRET_SIGNATURE\]/);
  assert.equal(output.includes(synthesizedSecret), false);
});
