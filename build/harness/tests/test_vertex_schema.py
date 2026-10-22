"""Gemini schema sanitising keeps parameter names that collide with JSON-schema keywords."""
from bsp_harness.providers.vertex import sanitize_schema


def test_property_named_title_survives():
    schema = {"title": "SearchArgs", "type": "object", "additionalProperties": False,
              "properties": {"title": {"title": "Title", "type": "string"},
                             "default": {"type": ["integer", "null"], "default": 5},
                             "query": {"anyOf": [{"type": "string"}, {"type": "null"}], "title": "Query"}},
              "required": ["title", "query", "ghost"]}
    out = sanitize_schema(schema)
    assert set(out["properties"]) == {"title", "default", "query"}
    assert out["properties"]["title"] == {"type": "string"}
    assert out["properties"]["default"] == {"type": "integer", "nullable": True}
    assert out["properties"]["query"]["nullable"] is True and out["properties"]["query"]["type"] == "string"
    assert out["required"] == ["title", "query"], "dangling required names dropped"
    assert "title" not in out and "additionalProperties" not in out


def test_every_mcp_tool_schema_is_gemini_clean():
    """Required names must always point at a surviving property."""
    def walk(node):
        if isinstance(node, dict):
            if isinstance(node.get("required"), list):
                assert set(node["required"]) <= set(node.get("properties", {})), node
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    # a representative collision: many tools take `title` / `query` / `default`
    walk(sanitize_schema({"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"]}))
