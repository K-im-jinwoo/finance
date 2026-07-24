const DIRECT_SENSITIVE_KEY_PATTERN =
  /^(authorization|client[-_]?(id|secret)|api[-_]?key|password|token)$/i;

const SENSITIVE_HEADER_NAME_PATTERN =
  /(?:^|[-_ ])(authorization|client[-_ ]?(id|secret)|api[-_ ]?key|password|token)$/i;

const ENV_EXPRESSION_PATTERN = /^={{ \$env\.[A-Z_][A-Z0-9_]* }}$/;

export function isDirectSensitiveKey(name) {
  return (
    typeof name === "string" &&
    DIRECT_SENSITIVE_KEY_PATTERN.test(name)
  );
}

export function isSensitiveHeaderName(name) {
  return (
    typeof name === "string" &&
    SENSITIVE_HEADER_NAME_PATTERN.test(name.trim())
  );
}

export function isEnvironmentExpression(value) {
  return typeof value === "string" && ENV_EXPRESSION_PATTERN.test(value);
}

export function environmentName(name) {
  return String(name)
    .trim()
    .replace(/([a-z0-9])([A-Z])/g, "$1_$2")
    .replace(/[^A-Za-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .toUpperCase();
}
