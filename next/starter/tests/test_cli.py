from agent_app.cli import _run
from agent_app.config import Settings


async def test_cli_prints_streamed_text_without_duplicate_final_output(
    capsys,
) -> None:
    exit_code = await _run(
        "hello",
        settings=Settings(provider="offline"),
        show_tool_results=False,
        show_execution_trace=False,
        stream=True,
    )

    assert exit_code == 0
    assert capsys.readouterr().out == "Starter Agent received: hello\n"


async def test_cli_rejects_streaming_before_running_unsupported_provider(
    capsys,
) -> None:
    exit_code = await _run(
        "hello",
        settings=Settings(
            provider="codex-cli",
            cli_executable="definitely-missing-codex",
        ),
        show_tool_results=False,
        show_execution_trace=False,
        stream=True,
    )

    assert exit_code == 2
    assert capsys.readouterr().out == (
        "configuration error: Provider 'codex-cli' does not support streaming\n"
    )


async def test_cli_prints_skill_to_tool_execution_trace(capsys) -> None:
    exit_code = await _run(
        'tool=calculate_sum calls=1 tool_args={"left":1,"right":2}',
        settings=Settings(provider="offline"),
        show_tool_results=True,
        show_execution_trace=True,
        stream=False,
    )

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "skill.selected skill=python-calculator version=1.0.0" in captured.err
    assert (
        "skill.resource.used skill=python-calculator type=tool "
        "resource=calculate_sum tool=calculate_sum call=call-1"
    ) in captured.err
    assert ("tool.started tool=calculate_sum call=call-1") in captured.err
    assert ("tool.completed tool=calculate_sum call=call-1 status=success") in captured.err
    assert "call-1: status=success, data={'sum': 3}" in captured.out


async def test_cli_distinguishes_selected_and_loaded_skill(capsys) -> None:
    exit_code = await _run(
        ('tool=skill_view calls=1 tool_args={"name":"value-recording"}'),
        settings=Settings(provider="offline"),
        show_tool_results=False,
        show_execution_trace=True,
        stream=False,
    )

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "skill.selected skill=value-recording version=1.0.0" in captured.err
    assert "skill.loaded skill=value-recording version=1.0.0 file=SKILL.md" in captured.err
    assert captured.err.index("skill.selected") < captured.err.index("skill.loaded")
