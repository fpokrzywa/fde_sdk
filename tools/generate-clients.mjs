import {readFile, writeFile, mkdir} from "node:fs/promises";
import {compile} from "json-schema-to-typescript";

const schema = JSON.parse(await readFile(new URL("../schemas/contracts.json", import.meta.url), "utf8"));
const output = await compile(schema, "ShifdeContracts", {
  bannerComment: "/* Generated from Pydantic contracts. Run pnpm generate; do not edit. */",
  additionalProperties: false,
});
const destination = new URL("../packages/sdk_typescript/src/generated/", import.meta.url);
await mkdir(destination, {recursive: true});
await writeFile(new URL("contracts.ts", destination), output);
// The runtime validator uses the identical schema, not a second handwritten contract.
await writeFile(new URL("schema.ts", destination), "/* Generated; do not edit. */\nexport const schema = " + JSON.stringify(schema) + " as const;\n");
