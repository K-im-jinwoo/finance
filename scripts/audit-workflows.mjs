import { resolve } from "node:path";

import { auditDirectory } from "./lib/audit-workflows.mjs";

const directory = resolve(process.argv[2] ?? "workflows/n8n");
const findings = await auditDirectory(directory);

if (findings.length > 0) {
  for (const item of findings) {
    console.error(`${item.file} [${item.code}] ${item.detail}`);
  }
  process.exit(1);
}

console.log(`Workflow audit passed: ${directory}`);
