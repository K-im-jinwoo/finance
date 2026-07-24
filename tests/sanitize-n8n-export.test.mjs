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

test("sanitizer replaces recursively nested direct sensitive keys", () => {
  const source = sampleWorkflow();
  source.nodes[0].parameters.authentication = {
    clientId: "literal-client-id",
    client_secret: "literal-client-secret",
    "api-key": "literal-api-key",
    password: "literal-password",
    token: "literal-token",
    authorization: "Bearer literal-token",
    tokenizer: "not-sensitive",
  };

  const authentication =
    sanitizeWorkflow(source).nodes[0].parameters.authentication;

  assert.deepEqual(authentication, {
    clientId: "={{ $env.CLIENT_ID }}",
    client_secret: "={{ $env.CLIENT_SECRET }}",
    "api-key": "={{ $env.API_KEY }}",
    password: "={{ $env.PASSWORD }}",
    token: "={{ $env.TOKEN }}",
    authorization: "={{ $env.AUTHORIZATION }}",
    tokenizer: "not-sensitive",
  });
});

test("sanitizer replaces non-environment expressions in sensitive values", () => {
  const source = sampleWorkflow();
  source.nodes[0].parameters.apiKey = "={{ $json.secret }}";
  source.nodes[0].parameters.headers = [
    { name: "Authorization", value: "={{ $json.authorization }}" },
  ];

  const parameters = sanitizeWorkflow(source).nodes[0].parameters;

  assert.equal(parameters.apiKey, "={{ $env.API_KEY }}");
  assert.equal(
    parameters.headers[0].value,
    "={{ $env.AUTHORIZATION }}",
  );
});

test("sanitizer fails closed for non-string sensitive values", () => {
  const directKey = sampleWorkflow();
  directKey.nodes[0].parameters.token = { source: "literal" };
  assert.throws(
    () => sanitizeWorkflow(directKey),
    /Sensitive value at .*token must be a string/,
  );

  const header = sampleWorkflow();
  header.nodes[0].parameters.headers = [
    { name: "Authorization", value: ["literal"] },
  ];
  assert.throws(
    () => sanitizeWorkflow(header),
    /Sensitive value at .*value must be a string/,
  );
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
