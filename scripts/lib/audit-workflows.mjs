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

function walk(value, path, visitObject, visitPrimitive) {
  if (Array.isArray(value)) {
    value.forEach((child, index) =>
      walk(child, `${path}[${index}]`, visitObject, visitPrimitive),
    );
    return;
  }
  if (value === null || typeof value !== "object") {
    visitPrimitive(value, path);
    return;
  }

  visitObject(value, path);
  for (const [key, child] of Object.entries(value)) {
    walk(child, `${path}.${key}`, visitObject, visitPrimitive);
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

  const handledSensitivePaths = new Set();
  walk(
    workflow,
    "$",
    (object, path) => {
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
        if (!isDirectSensitiveKey(key)) {
          continue;
        }
        const valuePath = `${path}.${key}`;
        handledSensitivePaths.add(valuePath);
        if (!isEnvironmentExpression(value)) {
          findings.push(
            finding(
              fileName,
              "LITERAL_SENSITIVE_VALUE",
              `Sensitive value must use an environment expression at ${valuePath}`,
            ),
          );
        }
      }

      if (hasSensitiveHeaderValue) {
        handledSensitivePaths.add(`${path}.value`);
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
    },
    (value, path) => {
      if (
        !handledSensitivePaths.has(path) &&
        hasKnownSecretSignature(value)
      ) {
        findings.push(
          finding(
            fileName,
            "KNOWN_SECRET_SIGNATURE",
            `Known secret signature found at ${path}`,
          ),
        );
      }
    },
  );

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
