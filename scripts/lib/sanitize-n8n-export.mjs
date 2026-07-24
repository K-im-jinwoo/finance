import {
  environmentName,
  isDirectSensitiveKey,
  isEnvironmentExpression,
  isSensitiveHeaderName,
} from "./workflow-security.mjs";

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

function sanitizeSensitiveValue(value, path, name) {
  if (typeof value !== "string") {
    throw new TypeError(`Sensitive value at ${path} must be a string`);
  }
  if (isEnvironmentExpression(value)) {
    return value;
  }
  return `={{ $env.${environmentName(name)} }}`;
}

function sanitizeObject(value, path = "$") {
  if (Array.isArray(value)) {
    return value.map((child, index) =>
      sanitizeObject(child, `${path}[${index}]`),
    );
  }

  if (value === null || typeof value !== "object") {
    return value;
  }

  const result = {};
  for (const [key, child] of Object.entries(value)) {
    if (key === "credentials") {
      continue;
    }
    if (isDirectSensitiveKey(key)) {
      result[key] = sanitizeSensitiveValue(
        child,
        `${path}.${key}`,
        key,
      );
      continue;
    }
    result[key] = sanitizeObject(child, `${path}.${key}`);
  }

  if (
    isSensitiveHeaderName(result.name) &&
    Object.hasOwn(result, "value")
  ) {
    result.value = sanitizeSensitiveValue(
      result.value,
      `${path}.value`,
      result.name,
    );
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
