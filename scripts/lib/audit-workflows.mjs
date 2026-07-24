import { readdir, readFile } from "node:fs/promises";
import { join } from "node:path";

import {
  hasKnownSecretSignature,
  isDirectSensitiveKey,
  isEnvironmentExpression,
  isSensitiveHeaderName,
} from "./workflow-security.mjs";

function finding(file, code, detail) {
  return { file, code, detail };
}

function isPlainObject(value) {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    return false;
  }
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function walk(value, path, visit) {
  if (Array.isArray(value)) {
    value.forEach((child, index) => walk(child, `${path}[${index}]`, visit));
    return;
  }
  if (value === null || typeof value !== "object") {
    return;
  }

  visit(value, path);
  for (const [key, child] of Object.entries(value)) {
    if (child !== null && typeof child === "object") {
      walk(child, `${path}.${key}`, visit);
    }
  }
}

export function auditWorkflow(workflow, fileName) {
  const findings = [];

  if (
    !isPlainObject(workflow) ||
    !Array.isArray(workflow.nodes) ||
    !isPlainObject(workflow.connections)
  ) {
    return [
      finding(
        fileName,
        "INVALID_WORKFLOW_SHAPE",
        "Workflow requires object nodes[] and connections{}",
      ),
    ];
  }

  if (workflow.active !== false) {
    findings.push(
      finding(
        fileName,
        "WORKFLOW_ACTIVE",
        "Committed workflow must have active=false",
      ),
    );
  }

  const seenNames = new Set();
  for (const [index, node] of workflow.nodes.entries()) {
    if (
      !isPlainObject(node) ||
      typeof node.name !== "string" ||
      node.name.trim() === "" ||
      typeof node.type !== "string" ||
      node.type.trim() === ""
    ) {
      findings.push(
        finding(
          fileName,
          "INVALID_NODE_SHAPE",
          `Node at index ${index} requires a plain object with non-empty name and type`,
        ),
      );
      continue;
    }
    if (seenNames.has(node.name)) {
      findings.push(
        finding(
          fileName,
          "DUPLICATE_NODE_NAME",
          `Duplicate node name: ${node.name}`,
        ),
      );
    }
    seenNames.add(node.name);
  }

  walk(workflow, "$", (object, path) => {
    if (Object.hasOwn(object, "credentials")) {
      findings.push(
        finding(
          fileName,
          "CREDENTIAL_OBJECT",
          `credentials key found at ${path}.credentials`,
        ),
      );
    }

    const hasSensitiveHeaderValue =
      isSensitiveHeaderName(object.name) &&
      Object.hasOwn(object, "value");
    for (const [key, value] of Object.entries(object)) {
      if (
        isDirectSensitiveKey(key) &&
        !isEnvironmentExpression(value)
      ) {
        findings.push(
          finding(
            fileName,
            "LITERAL_SENSITIVE_VALUE",
            `Sensitive value must use an environment expression at ${path}.${key}`,
          ),
        );
      } else if (
        !(hasSensitiveHeaderValue && key === "value") &&
        hasKnownSecretSignature(value)
      ) {
        findings.push(
          finding(
            fileName,
            "KNOWN_SECRET_SIGNATURE",
            `Known secret signature found at ${path}.${key}`,
          ),
        );
      }
    }

    if (
      hasSensitiveHeaderValue &&
      !isEnvironmentExpression(object.value)
    ) {
      findings.push(
        finding(
          fileName,
          "LITERAL_SENSITIVE_VALUE",
          `Sensitive value must use an environment expression at ${path}`,
        ),
      );
    }
  });

  return findings;
}

export async function auditDirectory(directory) {
  const files = (await readdir(directory))
    .filter((name) => name.endsWith(".json"))
    .sort();
  const findings = [];

  for (const file of files) {
    try {
      const workflow = JSON.parse(
        await readFile(join(directory, file), "utf8"),
      );
      findings.push(...auditWorkflow(workflow, file));
    } catch (error) {
      if (error instanceof SyntaxError) {
        findings.push(
          finding(file, "INVALID_JSON", "Workflow file is not valid JSON"),
        );
        continue;
      }
      throw error;
    }
  }

  return findings;
}
