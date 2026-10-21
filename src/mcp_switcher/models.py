"""Strict public schemas. Keys are never client-selectable."""
from pydantic import BaseModel, ConfigDict, Field, field_validator


class Selection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # null = every permitted tool; a nonempty list = an explicit subset.
    servers: dict[str, list[str] | None] = Field(default_factory=dict, max_length=256)

    @field_validator("servers")
    @classmethod
    def normalize(cls, value):
        result = {}
        for server, tools in sorted(value.items()):
            if not server or len(server) > 256:
                raise ValueError("Invalid server identifier")
            if tools is not None:
                if len(tools) > 4096 or any(not t or len(t) > 512 for t in tools):
                    raise ValueError("Invalid tool selection")
                tools = sorted(set(tools))
                if not tools:
                    continue  # Zero tools means the server is disabled, never inheritance.
            result[server] = tools
        return result


class ProjectInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    selection: Selection
    version: int | None = Field(default=None, ge=1)


class ApplyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selection: Selection
    revision: str = Field(min_length=64, max_length=64)


class LoginInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bearer: str = Field(min_length=24, max_length=4096)


class Problem(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(message)
