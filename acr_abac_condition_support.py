from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Literal, TypeAlias, assert_never


REPOSITORY_NAME_ATTRIBUTE: Final = (
    "@Request[Microsoft.ContainerRegistry/registries/repositories:name]"
)
# Microsoft Learn role definitions and condition pattern:
# https://learn.microsoft.com/azure/container-registry/container-registry-rbac-built-in-roles-directory-reference
# https://learn.microsoft.com/azure/container-registry/container-registry-rbac-abac-repository-permissions
ACR_REPOSITORY_ROLE_DATA_ACTIONS: Final = {
    "b93aa761-3e63-49ed-ac28-beffa264f7ac": (
        "Microsoft.ContainerRegistry/registries/repositories/content/read",
        "Microsoft.ContainerRegistry/registries/repositories/metadata/read",
    ),
    "2a1e307c-b015-4ebd-883e-5b7698a07328": (
        "Microsoft.ContainerRegistry/registries/repositories/content/read",
        "Microsoft.ContainerRegistry/registries/repositories/metadata/read",
        "Microsoft.ContainerRegistry/registries/repositories/content/write",
        "Microsoft.ContainerRegistry/registries/repositories/metadata/write",
    ),
}
ROLE_VARIABLE_IDS: Final = {
    "repositoryReaderRoleId": "b93aa761-3e63-49ed-ac28-beffa264f7ac",
    "repositoryWriterRoleId": "2a1e307c-b015-4ebd-883e-5b7698a07328",
}
TokenKind: TypeAlias = Literal["LPAREN", "RPAREN", "NOT_ACTION", "REPOSITORY", "AND", "OR"]
Token: TypeAlias = tuple[TokenKind, str]


@dataclass(frozen=True, slots=True)
class EvaluationContext:
    action: str
    repository: str


@dataclass(frozen=True, slots=True)
class ConditionParser:
    tokens: tuple[Token, ...]
    context: EvaluationContext
    position: int = 0

    def parse(self) -> bool:
        value, position = self._parse_or(self.position)
        assert position == len(self.tokens), self.tokens[position:]
        return value

    def _parse_or(self, position: int) -> tuple[bool, int]:
        value, position = self._parse_and(position)
        while position < len(self.tokens) and self.tokens[position][0] == "OR":
            right, position = self._parse_and(position + 1)
            value = value or right
        return value, position

    def _parse_and(self, position: int) -> tuple[bool, int]:
        value, position = self._parse_atom(position)
        while position < len(self.tokens) and self.tokens[position][0] == "AND":
            right, position = self._parse_atom(position + 1)
            value = value and right
        return value, position

    def _parse_atom(self, position: int) -> tuple[bool, int]:
        kind, operand = self.tokens[position]
        match kind:
            case "LPAREN":
                value, position = self._parse_or(position + 1)
                assert self.tokens[position][0] == "RPAREN"
                return value, position + 1
            case "NOT_ACTION":
                return self.context.action.casefold() != operand.casefold(), position + 1
            case "REPOSITORY":
                return self.context.repository.casefold() == operand.casefold(), position + 1
            case "RPAREN" | "AND" | "OR":
                raise AssertionError(f"Unexpected condition token: {kind}")
            case unreachable:
                assert_never(unreachable)


def evaluate_condition(condition: str, *, action: str, repository: str) -> bool:
    if condition.startswith("[format('"):
        condition = condition.removeprefix("[format('").split("', variables(", maxsplit=1)[0]
        condition = condition.replace("''", "'").replace("{{", "{").replace("}}", "}")
    tokens: list[Token] = []
    position = 0
    token_pattern = re.compile(
        rf"\s*(?:"
        rf"(?P<LPAREN>\()|(?P<RPAREN>\))|(?P<AND>AND\b)|(?P<OR>OR\b)|"
        rf"!\(ActionMatches\{{{{?''?(?P<NOT_ACTION>[^'}}]+?)''?\}}}}?\)|"
        rf"{re.escape(REPOSITORY_NAME_ATTRIBUTE)}\s+StringEqualsIgnoreCase\s+''?(?P<REPOSITORY>[^']+)''?"
        rf")"
    )
    while position < len(condition):
        token_match = token_pattern.match(condition, position)
        assert token_match is not None, condition[position:]
        kind = token_match.lastgroup
        assert kind is not None
        tokens.append((kind, token_match.group(kind)))
        position = token_match.end()
    return ConditionParser(
        tokens=tuple(tokens),
        context=EvaluationContext(action=action, repository=repository),
    ).parse()


def role_data_actions(role_definition_id: str) -> tuple[str, ...]:
    matching_roles = [
        role_id
        for variable_name, role_id in ROLE_VARIABLE_IDS.items()
        if variable_name in role_definition_id
    ]
    assert len(matching_roles) == 1, role_definition_id
    return ACR_REPOSITORY_ROLE_DATA_ACTIONS[matching_roles[0]]
