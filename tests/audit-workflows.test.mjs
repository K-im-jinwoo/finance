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
