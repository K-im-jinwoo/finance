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
