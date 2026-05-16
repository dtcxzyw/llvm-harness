import json
import os
import warnings
from typing import List

from openai import NOT_GIVEN, OpenAI

from harness.lms.agent import (
  AgentBase,
  AgentConfig,
  AgentHooks,
  ChatMessageFunctionCall,
  ChatMessageFunctionCallOutput,
  ChatMessageMessage,
)
from harness.lms.meter import GlobalMeter


@warnings.deprecated("Use GPTGenericAgent instead")
class GPTAgent(AgentBase):
  def __init__(self, config: AgentConfig):
    super().__init__(config)
    if self.reasoning_effort == "NOT_GIVEN":
      self.reasoning_effort = NOT_GIVEN
    api_key = os.environ.get("LLVM_HARNESS_LM_API_KEY")
    base_url = os.environ.get("LLVM_HARNESS_LM_API_ENDPOINT") or None
    self.client = OpenAI(api_key=api_key, base_url=base_url)

  def render_message_list(self) -> List[dict]:
    messages = []
    if self.tools.has_deferred_tools():
      messages.append(
        {
          "role": "user",
          "content": "NOTE: "
          'Some of the provided tools may be marked by "[deferred]" in their description. '
          "This means their descriptions and specifications are not fully provided. "
          "Therefore, for these tools, be sure to use `tool_search` to load the full "
          "description and specification before calling them.",
        }
      )
    for message in self.history:
      if isinstance(message, ChatMessageMessage):
        messages.append(
          {
            "role": message.role,
            "content": message.content,
          }
        )
      elif isinstance(message, ChatMessageFunctionCall):
        messages.append(
          {
            "role": "assistant",
            "content": "",
            "tool_calls": [
              {
                "id": message.call_id,
                "function": {
                  "arguments": message.arguments,
                  "name": message.name,
                },
                "type": "function",
                "index": 0,
              }
            ],
          }
        )
      elif isinstance(message, ChatMessageFunctionCallOutput):
        messages.append(
          {
            "role": "tool",
            "tool_call_id": message.call_id,
            "content": message.output,
          }
        )

    return messages

  def run(
    self,
    hooks: AgentHooks,
  ) -> str:
    while True:
      self.console.print(GlobalMeter.format_status(self.meter))
      self.meter.record_round()

      remaining_tools = self._get_remaining_tools()
      completion = self._completion_api_with_backoff(
        model=self.model,
        messages=self.render_message_list(),
        temperature=self.temperature,
        top_p=self.top_p,
        max_tokens=self.max_completion_tokens,
        max_completion_tokens=self.max_completion_tokens,
        reasoning_effort=self.reasoning_effort,
        tools=(
          [tool.spec().render_in_openai_format() for tool in remaining_tools]
          or NOT_GIVEN
        ),
        tool_choice="auto",
        parallel_tool_calls=False,
      )

      # Update tokens that we have consumed
      if completion.usage:
        cached = 0
        if completion.usage.prompt_tokens_details:
          cached = completion.usage.prompt_tokens_details.cached_tokens
        self.meter.record_usage(
          input_tokens=completion.usage.prompt_tokens,
          cached_tokens=cached,
          output_tokens=completion.usage.completion_tokens,
        )

      response = completion.choices[0].message

      if not response.tool_calls:
        # Handle normal response
        content = response.content
        self.append_assistant_message(content)
        proceed, content = hooks.post_response(content)
        self.append_user_message(content)
        if proceed:
          continue
        else:
          return content

      # Handle tool calls
      for tool_call in response.tool_calls:
        name = tool_call.function.name
        args_text = tool_call.function.arguments
        arguments = json.loads(args_text)
        self.append_function_tool_call(
          call_id=tool_call.id,
          name=name,
          arguments=args_text,
        )
        if hooks.pre_tool_call:
          proceed, pre_result = hooks.pre_tool_call(name, arguments)
          if not proceed:
            self.append_function_tool_call_output(
              call_id=tool_call.id, result=str(pre_result)
            )
            continue
          if isinstance(pre_result, dict):
            arguments = pre_result
        result = self.perform_tool_call(name, arguments)
        proceed, result = hooks.post_tool_call(name, args_text, result)
        if not proceed:
          self.append_user_message(result)
          return result
        self.append_function_tool_call_output(call_id=tool_call.id, result=result)

  def _completion_api(self, **kwargs):
    return self.client.chat.completions.create(**kwargs)
