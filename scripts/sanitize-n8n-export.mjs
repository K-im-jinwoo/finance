import { mkdir, readFile, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";

import {
  sanitizeWorkflow,
  serializeWorkflow,
} from "./lib/sanitize-n8n-export.mjs";

const [, , inputArg, outputArg] = process.argv;

if (!inputArg || !outputArg) {
  console.error(
    "Usage: node scripts/sanitize-n8n-export.mjs <input> <output>",
  );
  process.exit(2);
}

const inputPath = resolve(inputArg);
const outputPath = resolve(outputArg);
const source = JSON.parse(await readFile(inputPath, "utf8"));
const sanitized = sanitizeWorkflow(source);

await mkdir(dirname(outputPath), { recursive: true });
await writeFile(outputPath, serializeWorkflow(sanitized), "utf8");
console.log(`Sanitized workflow written: ${outputPath}`);
