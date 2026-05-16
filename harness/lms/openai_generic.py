import os
from typing import Dict, List, Tuple

from openai import NOT_GIVEN, OpenAI

from harness.lms.agent import AgentConfig
from harness.lms.generic import GenericAgent


class GPTGenericAgent(GenericAgent):
  def __init__(self, config: AgentConfig):
    super().__init__(config)
    if self.reasoning_effort == "NOT_GIVEN":
      self.reasoning_effort = NOT_GIVEN
    api_key = os.environ.get("LLVM_HARNESS_LM_API_KEY")
    base_url = os.environ.get("LLVM_HARNESS_LM_API_ENDPOINT") or None
    self.client = OpenAI(api_key=api_key, base_url=base_url)

  def _complete_chat(self, messages: List[Dict]) -> Tuple[str, str]:
    completion = self._completion_api_with_backoff(
      model=self.model,
      messages=messages,
      temperature=self.temperature,
      top_p=self.top_p,
      max_tokens=self.max_completion_tokens,
      max_completion_tokens=self.max_completion_tokens,
      reasoning_effort=self.reasoning_effort,
      stream=True,
      stream_options={"include_usage": True},
    )

    reasoning_content = ""
    answer_content = ""

    for chunk in completion:
      # Update tokens that we have consumed
      if chunk.usage:
        cached = 0
        if (
          chunk.usage.prompt_tokens_details
          and chunk.usage.prompt_tokens_details.cached_tokens
        ):
          cached = chunk.usage.prompt_tokens_details.cached_tokens
        self.meter.record_usage(
          input_tokens=chunk.usage.prompt_tokens or 0,
          cached_tokens=cached,
          output_tokens=chunk.usage.completion_tokens or 0,
        )

      # Get assistant's reasoning and answer from the response content
      if not chunk.choices:
        continue

      delta = chunk.choices[0].delta

      if hasattr(delta, "reasoning_content") and delta.reasoning_content is not None:
        reasoning_content += delta.reasoning_content

      if hasattr(delta, "content") and delta.content:
        answer_content += delta.content

    if (
      not reasoning_content
      and "<think>" in answer_content
      and "</think>" in answer_content
    ):
      think_begin = answer_content.index("<think>") + len("<think>")
      think_end = answer_content.rindex("</think>")
      reasoning_content = answer_content[think_begin:think_end]
      answer_content = (
        answer_content[: think_begin - len("<think>")]
        + "\n"
        + answer_content[think_end + len("</think>") :]
      )

    return reasoning_content, answer_content

  def _completion_api(self, **kwargs):
    return self.client.chat.completions.create(**kwargs)
