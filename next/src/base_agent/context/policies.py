"""Built-in ContextPolicy implementations."""

from __future__ import annotations

from base_agent.context.models import ContextInput, ContextTrace, ContextView


class KeepAllContextPolicy:
    """Preserve the current Harness behavior while providing a policy seam."""

    @property
    def name(self) -> str:
        return "keep_all"

    def select(self, context: ContextInput, /) -> ContextView:
        message_count = len(context.transcript)
        return ContextView(
            system_prompt=context.system_prompt,
            messages=context.transcript,
            trace=ContextTrace(
                policy=self.name,
                source_system_prompt_fingerprint=context.system_prompt_fingerprint,
                selected_system_prompt_fingerprint=context.system_prompt_fingerprint,
                selected_system_block_ids=context.system_context.block_ids,
                source_message_count=message_count,
                selected_message_count=message_count,
                selected_message_ids=tuple(
                    message.message_id
                    for message in context.transcript
                    if message.message_id is not None
                ),
                memory_candidate_count=len(context.memory.records),
            ),
        )
