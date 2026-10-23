"""Shared model/tool contracts; enforced again at the API execution boundary."""

import hashlib
import json

from jsonschema import Draft202012Validator


def object_schema(properties=None, required=None):
    return {"type": "object", "properties": properties or {}, "required": required or [], "additionalProperties": False}


PATH = {"type": "string", "minLength": 1, "maxLength": 2048}
TEXT = {"type": "string", "maxLength": 500000}
TOOL_SCHEMAS = {
    "read_artifact": object_schema({"uri": {"type": "string", "pattern": "^artifact://[a-f0-9]{64}$"}, "offset": {"type": "integer", "minimum": 0}}, ["uri"]),
    "repository_search": object_schema({"query": {"type": "string", "minLength": 1, "maxLength": 3000}, "limit": {"type": "integer", "minimum": 1, "maximum": 10}}, ["query"]),
    "list_files": object_schema({"path": {"type": "string", "maxLength": 2048}}),
    "read_file": object_schema({"path": PATH, "start_line": {"type": "integer", "minimum": 1}, "end_line": {"type": "integer", "minimum": 1}}, ["path"]),
    "replace_text": object_schema({"path": PATH, "old_text": {**TEXT, "minLength": 1}, "new_text": TEXT, "replace_all": {"type": "boolean"}}, ["path", "old_text", "new_text"]),
    "write_file": object_schema({"path": PATH, "content": TEXT}, ["path", "content"]),
    "run_command": object_schema({"command": {"type": "string", "minLength": 1, "maxLength": 8000}, "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 60}}, ["command"]),
    "run_tests": object_schema(),
    "get_diff": object_schema(),
}
DECISION_SCHEMA = object_schema({
    "action": {"type": "string", "enum": ["tool", "finish"]},
    "tool": {"type": "string", "enum": ["", *TOOL_SCHEMAS]},
    "arguments": {"type": "object"},
    "summary": {"type": "string", "maxLength": 16000},
}, ["action", "tool", "arguments", "summary"])
MUTATING_TOOLS = {"replace_text", "write_file", "run_command"}


def validate_decision(value):
    Draft202012Validator(DECISION_SCHEMA).validate(value)
    if value["action"] == "tool":
        validate_tool(value["tool"], value["arguments"])
    elif value["tool"] or value["arguments"]:
        raise ValueError("A finish decision must have empty tool and arguments")
    return value


def validate_tool(name, arguments):
    if name not in TOOL_SCHEMAS:
        raise ValueError("Unknown tool")
    Draft202012Validator(TOOL_SCHEMAS[name]).validate(arguments)
    if name == "read_file" and arguments.get("end_line", arguments.get("start_line", 1) + 199) < arguments.get("start_line", 1):
        raise ValueError("end_line precedes start_line")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
