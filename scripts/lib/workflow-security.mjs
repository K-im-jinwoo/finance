const ENV_EXPRESSION_PATTERN = /^={{ \$env\.[A-Z_][A-Z0-9_]* }}$/;

const TOKEN_QUALIFIERS = new Set([
  "access",
  "api",
  "auth",
  "bearer",
  "csrf",
  "github",
  "id",
  "jwt",
  "oauth",
  "refresh",
  "session",
  "slack",
]);

const SENSITIVE_VALUE_SUFFIXES = new Set([
  "data",
  "digest",
  "hash",
  "header",
  "id",
  "key",
  "pem",
  "value",
]);

const METADATA_SUFFIXES = new Set([
  "alg",
  "algorithm",
  "endpoint",
  "expiration",
  "expires",
  "expiry",
  "format",
  "length",
  "location",
  "name",
  "policy",
  "ttl",
  "type",
  "url",
]);

const KNOWN_SECRET_PATTERNS = [
  /\bgh[pousr]_[A-Za-z0-9]{36,255}\b/,
  /\bgithub_pat_[A-Za-z0-9_]{20,255}\b/,
  /\bxox[baprs]-[A-Za-z0-9-]{10,}\b/,
  /-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----/,
  /\bAKIA[A-Z0-9]{16}\b/,
];

function semanticTokens(name) {
  return String(name)
    .trim()
    .replace(/([A-Z]+)([A-Z][a-z])/g, "$1 $2")
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .split(/[^A-Za-z0-9]+/)
    .filter(Boolean)
    .map((token) => token.toLowerCase());
}

function hasSensitiveValueEnding(tokens, endIndex) {
  const suffixes = tokens.slice(endIndex + 1);
  if (suffixes.some((token) => METADATA_SUFFIXES.has(token))) {
    return false;
  }
  return suffixes.every((token) => SENSITIVE_VALUE_SUFFIXES.has(token));
}

function hasSensitiveToken(tokens, expected) {
  return tokens.some(
    (token, index) =>
      token === expected && hasSensitiveValueEnding(tokens, index),
  );
}

function hasSensitivePair(tokens, first, second) {
  return tokens.some(
    (token, index) =>
      token === first &&
      tokens[index + 1] === second &&
      hasSensitiveValueEnding(tokens, index + 1),
  );
}

export function isDirectSensitiveKey(name) {
  if (typeof name !== "string") {
    return false;
  }

  const tokens = semanticTokens(name);
  if (
    hasSensitiveToken(tokens, "authorization") ||
    hasSensitiveToken(tokens, "password") ||
    hasSensitiveToken(tokens, "secret") ||
    hasSensitivePair(tokens, "client", "id") ||
    hasSensitivePair(tokens, "api", "key") ||
    hasSensitivePair(tokens, "access", "key") ||
    hasSensitivePair(tokens, "private", "key") ||
    hasSensitivePair(tokens, "signing", "key")
  ) {
    return true;
  }

  return tokens.some(
    (token, index) =>
      token === "token" &&
      (index === 0 || TOKEN_QUALIFIERS.has(tokens[index - 1])) &&
      hasSensitiveValueEnding(tokens, index),
  );
}

export function isSensitiveHeaderName(name) {
  if (typeof name !== "string") {
    return false;
  }
  const tokens = semanticTokens(name);
  return (
    isDirectSensitiveKey(name) ||
    tokens[tokens.length - 1] === "token"
  );
}

export function isEnvironmentExpression(value) {
  return typeof value === "string" && ENV_EXPRESSION_PATTERN.test(value);
}

export function hasKnownSecretSignature(value) {
  if (typeof value !== "string") {
    return false;
  }
  if (isEnvironmentExpression(value)) {
    return false;
  }
  return KNOWN_SECRET_PATTERNS.some((pattern) => pattern.test(value));
}

export function environmentName(name) {
  return String(name)
    .trim()
    .replace(/([a-z0-9])([A-Z])/g, "$1_$2")
    .replace(/[^A-Za-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .toUpperCase();
}
