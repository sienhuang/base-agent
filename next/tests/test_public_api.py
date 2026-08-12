import base_agent


def test_root_api_is_intentionally_small() -> None:
    assert set(base_agent.__all__) == {
        "Agent",
        "AgentDefinition",
        "Confirmation",
        "Event",
        "Flow",
        "FlowResult",
        "FlowStatus",
        "RunResult",
        "RunFailure",
        "RunHandle",
        "RunStatus",
        "SideEffect",
        "Skill",
        "SkillCatalog",
        "SkillDescriptor",
        "SkillPackage",
        "Stage",
        "StageResult",
        "ToolChoice",
        "Toolkit",
        "ToolReadiness",
        "VerificationEvidence",
        "load_prompt",
        "tool",
    }
