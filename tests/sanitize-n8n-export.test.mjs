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
