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

test("sanitizer recognizes bounded secret aliases across naming styles", () => {
  const source = sampleWorkflow();
  source.nodes[0].parameters.aliases = {
    accessToken: "literal",
    refresh_token: "literal",
    "bearer-token": "literal",
    authToken: "literal",
    id_token: "literal",
    "session-token": "literal",
    passwordHash: "literal",
    password_value: "literal",
    privateKey: "literal",
    ssh_private_key: "literal",
    "signing-key": "literal",
    clientSecret: "literal",
    signing_secret: "literal",
    "webhook-secret": "literal",
    tokenization: "not-sensitive",
    keyValue: "not-sensitive",
  };

  const aliases = sanitizeWorkflow(source).nodes[0].parameters.aliases;

  assert.deepEqual(aliases, {
    accessToken: "={{ $env.ACCESS_TOKEN }}",
    refresh_token: "={{ $env.REFRESH_TOKEN }}",
    "bearer-token": "={{ $env.BEARER_TOKEN }}",
    authToken: "={{ $env.AUTH_TOKEN }}",
    id_token: "={{ $env.ID_TOKEN }}",
    "session-token": "={{ $env.SESSION_TOKEN }}",
    passwordHash: "={{ $env.PASSWORD_HASH }}",
    password_value: "={{ $env.PASSWORD_VALUE }}",
    privateKey: "={{ $env.PRIVATE_KEY }}",
    ssh_private_key: "={{ $env.SSH_PRIVATE_KEY }}",
    "signing-key": "={{ $env.SIGNING_KEY }}",
    clientSecret: "={{ $env.CLIENT_SECRET }}",
    signing_secret: "={{ $env.SIGNING_SECRET }}",
    "webhook-secret": "={{ $env.WEBHOOK_SECRET }}",
    tokenization: "not-sensitive",
    keyValue: "not-sensitive",
  });
});

test("sanitizer recognizes approved value suffix chains for direct keys", () => {
  const source = sampleWorkflow();
  source.nodes[0].parameters.suffixedSecrets = {
    secretKey: "literal",
    client_secret_key: "literal",
    "api-secret-key": "literal",
    privateKeyPem: "literal",
    authorization_header: "literal",
  };

  assert.deepEqual(
    sanitizeWorkflow(source).nodes[0].parameters.suffixedSecrets,
    {
      secretKey: "={{ $env.SECRET_KEY }}",
      client_secret_key: "={{ $env.CLIENT_SECRET_KEY }}",
      "api-secret-key": "={{ $env.API_SECRET_KEY }}",
      privateKeyPem: "={{ $env.PRIVATE_KEY_PEM }}",
      authorization_header: "={{ $env.AUTHORIZATION_HEADER }}",
    },
  );
});

test("sanitizer recognizes approved suffixes in custom sensitive headers", () => {
  const source = sampleWorkflow();
  source.nodes[0].parameters.headers = [
    { name: "X-Custom-Secret-Key", value: "literal" },
    { name: "X_Custom_Client_Secret_Key", value: "literal" },
    { name: "X-Custom-Private-Key-Pem", value: "literal" },
    { name: "X_Custom_Authorization_Header", value: "literal" },
  ];

  assert.deepEqual(
    sanitizeWorkflow(source).nodes[0].parameters.headers,
    [
      { name: "X-Custom-Secret-Key", value: "={{ $env.X_CUSTOM_SECRET_KEY }}" },
      { name: "X_Custom_Client_Secret_Key", value: "={{ $env.X_CUSTOM_CLIENT_SECRET_KEY }}" },
      { name: "X-Custom-Private-Key-Pem", value: "={{ $env.X_CUSTOM_PRIVATE_KEY_PEM }}" },
      { name: "X_Custom_Authorization_Header", value: "={{ $env.X_CUSTOM_AUTHORIZATION_HEADER }}" },
    ],
  );
});

test("sanitizer preserves credential metadata suffixes across naming styles", () => {
  const source = sampleWorkflow();
  const metadata = {
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
  source.nodes[0].parameters.metadata = metadata;

  assert.deepEqual(sanitizeWorkflow(source).nodes[0].parameters.metadata, metadata);
});

test("sanitizer fails closed on known secret signatures under neutral keys", () => {
  for (const [label, value] of Object.entries(knownSecretSamples())) {
    const source = sampleWorkflow();
    source.nodes[0].parameters.metadata = { [label]: value };

    assert.throws(
      () => sanitizeWorkflow(source),
      /Known secret signature at .* cannot be sanitized/,
    );
  }
});

test("sanitizer exempts only exact environment expressions from signature checks", () => {
  const source = sampleWorkflow();
  const exactEnvironmentExpression = "={{ $env.AWS_ACCESS_KEY_ID }}";
  source.nodes[0].parameters.safeReference = exactEnvironmentExpression;

  assert.equal(
    sanitizeWorkflow(source).nodes[0].parameters.safeReference,
    exactEnvironmentExpression,
  );

  const unsafeSource = sampleWorkflow();
  unsafeSource.nodes[0].parameters.computedValue = `={{ "${knownSecretSamples().aws}" }}`;
  assert.throws(
    () => sanitizeWorkflow(unsafeSource),
    /Known secret signature at .*computedValue cannot be sanitized/,
  );
});

test("sanitizer fails closed for known signatures in string arrays", () => {
  const source = sampleWorkflow();
  source.nodes[0].parameters.values = ["safe", knownSecretSamples().slack];

  assert.throws(
    () => sanitizeWorkflow(source),
    /Known secret signature at .*values\[1\] cannot be sanitized/,
  );
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
