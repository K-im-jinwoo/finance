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
  workflow.nodes[0].parameters.apiKey = "cli-secret-must-not-print";
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
  assert.match(output, /\[LITERAL_SENSITIVE_VALUE\]/);
  assert.equal(output.includes("cli-secret-must-not-print"), false);
});
