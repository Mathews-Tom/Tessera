"""Executable MCP contract checks.

The public tool catalogue is used by docs, the stdio bridge, and the
HTTP dispatcher. These tests pin the contract to implementation
constants so defaults and schemas cannot drift silently.
"""

from __future__ import annotations

import pytest

from tessera.daemon import dispatch, stdio_bridge
from tessera.mcp_surface import tools as mcp


@pytest.mark.unit
def test_contract_lists_dispatchable_tools_in_order() -> None:
    names = [contract.name for contract in mcp.MCP_TOOL_CONTRACTS]
    assert names == [
        "capture",
        "recall",
        "show",
        "list_facets",
        "stats",
        "forget",
        "learn_skill",
        "get_skill",
        "list_skills",
        "resolve_person",
        "list_people",
        "register_agent_profile",
        "get_agent_profile",
        "list_agent_profiles",
        "register_checklist",
        "record_retrospective",
        "list_checks_for_agent",
    ]
    assert set(names) == set(dispatch._HANDLERS)


@pytest.mark.unit
def test_stdio_bridge_uses_executable_contract() -> None:
    contract_by_name = {contract.name: contract for contract in mcp.MCP_TOOL_CONTRACTS}
    for tool in stdio_bridge._TOOLS:
        contract = contract_by_name[tool.name]
        assert tool.description == contract.description
        assert tool.inputSchema == contract.input_schema


@pytest.mark.unit
def test_contract_defaults_match_dispatch_and_tool_limits() -> None:
    recall = _contract("recall")
    recall_props = recall.input_schema["properties"]
    assert recall_props["k"]["default"] == 10
    assert recall_props["k"]["minimum"] == 1
    assert recall_props["k"]["maximum"] == 100
    assert recall.response_budget_tokens == mcp.RECALL_RESPONSE_BUDGET

    list_facets = _contract("list_facets")
    list_props = list_facets.input_schema["properties"]
    assert list_props["limit"]["default"] == 20
    assert list_props["limit"]["minimum"] == 1
    assert list_props["limit"]["maximum"] == 100
    assert list_facets.input_schema["required"] == ["facet_type"]


def _contract(name: str) -> mcp.ToolContract:
    for contract in mcp.MCP_TOOL_CONTRACTS:
        if contract.name == name:
            return contract
    raise AssertionError(f"missing contract {name}")
