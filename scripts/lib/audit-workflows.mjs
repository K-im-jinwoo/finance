import { readdir, readFile } from "node:fs/promises";
import { join } from "node:path";

const SENSITIVE_NAME_PATTERN =
  /(authorization|client[-_ ]?id|client[-_ ]?secret|api[-_ ]?key|password|token)/i;

function finding(file, code, detail) {
  return { file, code, detail };
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
    workflow === null ||
    typeof workflow !== "object" ||
    Array.isArray(workflow) ||
    !Array.isArray(workflow.nodes) ||
    workflow.connections === null ||
    typeof workflow.connections !== "object"
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
  for (const node of workflow.nodes) {
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

    if (
      typeof object.name === "string" &&
      SENSITIVE_NAME_PATTERN.test(object.name) &&
      typeof object.value === "string" &&
      object.value.trim() !== "" &&
      !object.value.trim().startsWith("={{")
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
