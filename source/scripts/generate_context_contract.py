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
text = re.sub(r'class GoalProgress\(TypedDict\):.*?(?=class ContextSource)', '', text, flags=re.S)
text = text.replace('class ContextSource(TypedDict):', "class GoalProgress(TypedDict):\n    goal_id: str | None\n    observed_seq: int\n    max_model_turns: int\n    model_calls_admitted: int\n    remaining_calls_including_this_request: int\n    current_request_cost: Literal[1]\n    counting: Literal['all_model_calls_including_compact_child_review']\n    concurrent_calls_may_consume_remaining: Literal[True]\n\n" + 'class ContextSource(TypedDict):')
text = text.replace('    goal_progress: NotRequired[GoalProgress]\n', '')
text = text.replace('    budget: ContextBudget\n', '    budget: ContextBudget\n    goal_progress: NotRequired[GoalProgress]\n')
python.write_text(text)
text = typescript.read_text()
start = text.index("export const SCHEMA_SHA256 =")
end = text.index(" as const;", start) + len(" as const;")
text = text[:start] + f'export const SCHEMA_SHA256 = "{sha}";\nexport const SCHEMA = ' + json.dumps(schema, indent=2, ensure_ascii=False) + ' as const;' + text[end:]
text = re.sub(r'export type ContextBudget = CreditBudget;|export interface ContextBudget \{\n  readonly limit: number \| null;\n  readonly spent: number;\n  readonly reserved: number;\n\}',
    'export interface ContextBudget {\n  readonly limit: number | null;\n  readonly spent: number;\n  readonly reserved: number;\n}', text)
text = re.sub(r'export interface GoalProgress \{.*?(?=export interface ContextSource)', '', text, flags=re.S)
text = text.replace('export interface ContextSource {', 'export interface GoalProgress {\n  readonly goal_id: string | null;\n  readonly observed_seq: number;\n  readonly max_model_turns: number;\n  readonly model_calls_admitted: number;\n  readonly remaining_calls_including_this_request: number;\n  readonly current_request_cost: 1;\n  readonly counting: "all_model_calls_including_compact_child_review";\n  readonly concurrent_calls_may_consume_remaining: true;\n}\n\n' + 'export interface ContextSource {')
text = text.replace('  readonly goal_progress?: GoalProgress;\n', '')
text = text.replace('  readonly budget: ContextBudget;\n', '  readonly budget: ContextBudget;\n  readonly goal_progress?: GoalProgress;\n')
typescript.write_text(text)
