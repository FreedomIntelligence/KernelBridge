"""Refresh the embedded context schema and its budget type in both clients."""
import hashlib
import json
from pathlib import Path
import pprint
import re

root = Path(__file__).resolve().parents[1]
raw = (root / "contracts/execution/v1/context.schema.json").read_bytes()
schema = json.loads(raw)
sha = hashlib.sha256(raw).hexdigest()
python = root / "packages/executor/src/kernelcat_executor/generated/context.py"
typescript = root / "apps/agent/src/generated/context.ts"
text = python.read_text()
start = text.index("SCHEMA_SHA256 =")
end = text.index("\nJsonValue =", start)
text = text[:start] + f'SCHEMA_SHA256 = "{sha}"\nSCHEMA: dict[str, Any] = ' + pprint.pformat(schema, sort_dicts=False) + '\n' + text[end:]
text = re.sub(r'ContextBudget = CreditBudget|class ContextBudget\(TypedDict\):\n    limit: int \| None\n    spent: int\n    reserved: int',
    'class ContextBudget(TypedDict):\n    limit: int | None\n    spent: int\n    reserved: int', text)
python.write_text(text)
text = typescript.read_text()
start = text.index("export const SCHEMA_SHA256 =")
end = text.index(" as const;", start) + len(" as const;")
text = text[:start] + f'export const SCHEMA_SHA256 = "{sha}";\nexport const SCHEMA = ' + json.dumps(schema, indent=2, ensure_ascii=False) + ' as const;' + text[end:]
text = re.sub(r'export type ContextBudget = CreditBudget;|export interface ContextBudget \{\n  readonly limit: number \| null;\n  readonly spent: number;\n  readonly reserved: number;\n\}',
    'export interface ContextBudget {\n  readonly limit: number | null;\n  readonly spent: number;\n  readonly reserved: number;\n}', text)
typescript.write_text(text)
