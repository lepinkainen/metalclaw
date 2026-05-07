from pydantic import BaseModel, Field

from registry import _EMPTY_PARAMETERS, _schema_from_model, tool


def test_required_field_listed_in_required():
    class M(BaseModel):
        name: str = Field(description="required")

    schema = _schema_from_model(M)
    assert schema["type"] == "object"
    assert schema["required"] == ["name"]
    assert schema["properties"]["name"] == {"type": "string", "description": "required"}


def test_field_with_default_is_optional():
    class M(BaseModel):
        count: int = Field(default=5, description="optional with default")

    schema = _schema_from_model(M)
    assert schema["required"] == []
    assert schema["properties"]["count"]["type"] == "integer"


def test_optional_pipe_none_flattens_anyof_and_drops_default():
    class M(BaseModel):
        nick: str | None = Field(default=None, description="optional")

    schema = _schema_from_model(M)
    spec = schema["properties"]["nick"]
    assert spec == {"type": "string", "description": "optional"}
    assert "anyOf" not in spec
    assert "default" not in spec
    assert schema["required"] == []


def test_true_union_keeps_anyof():
    class M(BaseModel):
        value: str | int = Field(description="multi-type")

    schema = _schema_from_model(M)
    spec = schema["properties"]["value"]
    assert "anyOf" in spec
    assert spec["anyOf"] == [{"type": "string"}, {"type": "integer"}]
    assert schema["required"] == ["value"]


def test_title_and_defs_stripped():
    class Inner(BaseModel):
        x: int

    class M(BaseModel):
        inner: Inner

    schema = _schema_from_model(M)
    assert "title" not in schema
    assert "$defs" not in schema
    assert "definitions" not in schema


def test_strips_per_field_titles():
    class M(BaseModel):
        name: str

    schema = _schema_from_model(M)
    assert "title" not in schema["properties"]["name"]


def test_tool_decorator_no_args_emits_empty_parameters():
    @tool(description="zero-arg tool")
    def _zero_arg() -> dict:
        return {}

    assert _zero_arg.__name__ == "_zero_arg"
    from registry import TOOLS
    schema = TOOLS["_zero_arg"].schema["function"]["parameters"]
    assert schema == _EMPTY_PARAMETERS


def test_tool_decorator_with_args_derives_schema():
    class _Args(BaseModel):
        q: str = Field(description="query")

    @tool(description="echo", args=_Args)
    def _echo(q: str) -> str:
        return q

    from registry import TOOLS
    schema = TOOLS["_echo"].schema["function"]
    assert schema["name"] == "_echo"
    assert schema["description"] == "echo"
    assert schema["parameters"]["required"] == ["q"]
    assert schema["parameters"]["properties"]["q"]["type"] == "string"
