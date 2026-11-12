"""Context schema refreshed by source/scripts/generate_context_contract.py; edit the JSON Schema."""
from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict

from .objects import TrialKnowledgeBinding
from .rsi import KnowledgeContextItem
from .runtime import ContextCommit
from .runtime_control import CreditBudget, ExecutionStamp

SCHEMA_SHA256 = "742370bf96c5bc2543695006dcdce2531acc0411c347a9950aed9319901077aa"
SCHEMA: dict[str, Any] = {'$schema': 'https://json-schema.org/draft/2020-12/schema',
 '$id': 'https://kernelcat.local/contracts/execution/v1/context.schema.json',
 'title': 'Bounded parent context wire for finite worker requests',
 '$ref': '#/$defs/ContextSource',
 '$defs': {'JsonValue': {'description': 'Lossless Node context values. '
                                        'Integer-valued numbers must fit the '
                                        'safe integer range; larger '
                                        'identifiers use strings.',
                         'oneOf': [{'type': 'null'},
                                   {'type': 'boolean'},
                                   {'type': 'string'},
                                   {'type': 'integer',
                                    'minimum': -9007199254740991,
                                    'maximum': 9007199254740991},
                                   {'type': 'number',
                                    'not': {'type': 'integer'}},
                                   {'type': 'array',
                                    'items': {'$ref': '#/$defs/JsonValue'}},
                                   {'type': 'object',
                                    'additionalProperties': {'$ref': '#/$defs/JsonValue'}}]},
           'JsonObject': {'type': 'object',
                          'additionalProperties': {'$ref': '#/$defs/JsonValue'}},
           'Projection': {'type': 'object',
                          'properties': {'version': {'type': 'string',
                                                     'minLength': 1},
                                         'history_omitted': {'type': 'integer',
                                                             'minimum': 0,
                                                             'maximum': 9007199254740991},
                                         'authority': {'const': 'journal'},
                                         'context_byte_limit': {'type': 'integer',
                                                                'minimum': 4096,
                                                                'maximum': 2097152},
                                         'latest_action_id': {'oneOf': [{'type': 'string'},
                                                                        {'type': 'null'}]},
                                         'latest_model_action_id': {'type': 'string'},
                                         'turns_omitted': {'type': 'integer',
                                                           'minimum': 0,
                                                           'maximum': 9007199254740991}},
                          'required': ['version',
                                       'history_omitted',
                                       'authority',
                                       'context_byte_limit',
                                       'latest_action_id'],
                          'additionalProperties': False},
           'ContextBudget': {'type': 'object',
                             'properties': {'limit': {'type': ['integer',
                                                               'null'],
                                                      'minimum': 1,
                                                      'maximum': 9007199254740991},
                                            'spent': {'type': 'integer',
                                                      'minimum': 0,
                                                      'maximum': 9007199254740991},
                                            'reserved': {'type': 'integer',
                                                         'minimum': 0,
                                                         'maximum': 9007199254740991}},
                             'required': ['limit', 'spent', 'reserved'],
                             'additionalProperties': False},
           'ContextSource': {'type': 'object',
                             'properties': {'task': {'$ref': '#/$defs/JsonObject'},
                                            'skills': {'type': 'array',
                                                       'items': {'$ref': '#/$defs/JsonObject'}},
                                            'policy': {'$ref': '#/$defs/JsonObject'},
                                            'budget': {'$ref': '#/$defs/ContextBudget'},
                                            'history': {'type': 'array',
                                                        'items': {'$ref': '#/$defs/JsonObject'}},
                                            'projection': {'$ref': '#/$defs/Projection'},
                                            'turns': {'type': 'array',
                                                      'items': {'$ref': '#/$defs/JsonObject'}},
                                            'inputs': {'type': 'array',
                                                       'items': {'$ref': '#/$defs/JsonObject'}},
                                            'agent': {'$ref': '#/$defs/JsonObject'},
                                            'runtime': {'$ref': 'https://kernelcat.local/contracts/execution/v1/runtime_control.schema.json#/$defs/ExecutionStamp'},
                                            'knowledge': {'$ref': 'https://kernelcat.local/contracts/lab/v1/objects.schema.json#/$defs/TrialKnowledgeBinding'},
                                            'rsi': {'type': 'array',
                                                    'maxItems': 100,
                                                    'items': {'$ref': 'https://kernelcat.local/contracts/execution/v1/rsi.schema.json#/$defs/KnowledgeContextItem'}},
                                            'goal_progress': {'$ref': '#/$defs/GoalProgress'}},
                             'required': ['task',
                                          'skills',
                                          'policy',
                                          'budget',
                                          'history',
                                          'projection'],
                             'additionalProperties': False},
           'ModelContextRequest': {'type': 'object',
                                   'properties': {'route': {'type': 'string',
                                                            'minLength': 1},
                                                  'context': {'$ref': '#/$defs/ContextSource'},
                                                  'commit': {'$ref': 'https://kernelcat.local/contracts/execution/v1/runtime.schema.json#/$defs/ContextCommit'}},
                                   'required': ['route', 'context'],
                                   'additionalProperties': False},
           'GoalProgress': {'type': 'object',
                            'description': 'Journal observation before this '
                                           'model request; remaining calls '
                                           'include this request and are '
                                           'shared with concurrent models. '
                                           'This is a projection, never '
                                           'admission authority.',
                            'properties': {'goal_id': {'type': ['string',
                                                                'null']},
                                           'observed_seq': {'type': 'integer',
                                                            'minimum': 0,
                                                            'maximum': 9007199254740991},
                                           'max_model_turns': {'type': 'integer',
                                                               'minimum': 1,
                                                               'maximum': 9007199254740991},
                                           'model_calls_admitted': {'type': 'integer',
                                                                    'minimum': 0,
                                                                    'maximum': 9007199254740991},
                                           'remaining_calls_including_this_request': {'type': 'integer',
                                                                                      'minimum': 0,
                                                                                      'maximum': 9007199254740991},
                                           'current_request_cost': {'const': 1},
                                           'counting': {'const': 'all_model_calls_including_compact_child_review'},
                                           'concurrent_calls_may_consume_remaining': {'const': True}},
                            'required': ['goal_id',
                                         'observed_seq',
                                         'max_model_turns',
                                         'model_calls_admitted',
                                         'remaining_calls_including_this_request',
                                         'current_request_cost',
                                         'counting',
                                         'concurrent_calls_may_consume_remaining'],
                            'additionalProperties': False}}}

JsonValue = None | bool | str | int | float | list['JsonValue'] | dict[str, 'JsonValue']

JsonObject = dict[str, JsonValue]

class Projection(TypedDict):
    version: str
    history_omitted: int
    authority: Literal['journal']
    context_byte_limit: int
    latest_action_id: str | None
    latest_model_action_id: NotRequired[str]
    turns_omitted: NotRequired[int]

class ContextBudget(TypedDict):
    limit: int | None
    spent: int
    reserved: int

class GoalProgress(TypedDict):
    goal_id: str | None
    observed_seq: int
    max_model_turns: int
    model_calls_admitted: int
    remaining_calls_including_this_request: int
    current_request_cost: Literal[1]
    counting: Literal['all_model_calls_including_compact_child_review']
    concurrent_calls_may_consume_remaining: Literal[True]

class ContextSource(TypedDict):
    task: JsonObject
    skills: list[JsonObject]
    policy: JsonObject
    budget: ContextBudget
    goal_progress: NotRequired[GoalProgress]
    history: list[JsonObject]
    projection: Projection
    turns: NotRequired[list[JsonObject]]
    inputs: NotRequired[list[JsonObject]]
    agent: NotRequired[JsonObject]
    runtime: NotRequired[ExecutionStamp]
    knowledge: NotRequired[TrialKnowledgeBinding]
    rsi: NotRequired[list[KnowledgeContextItem]]

class ModelContextRequest(TypedDict):
    route: str
    context: ContextSource
    commit: NotRequired[ContextCommit]
