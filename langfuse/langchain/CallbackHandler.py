import typing

import pydantic

from langfuse._client.get_client import get_client
from langfuse._client.span import LangfuseGeneration, LangfuseSpan
from langfuse.logger import langfuse_logger

try:
    import langchain  # noqa

except ImportError as e:
    langfuse_logger.error(
        f"Could not import langchain. The langchain integration will not work. {e}"
    )

from typing import Any, Dict, List, Literal, Optional, Sequence, Set, Type, Union, cast
from uuid import UUID

from langfuse._utils import _get_timestamp
from langfuse.langchain.utils import _extract_model_name

try:
    from langchain.callbacks.base import (
        BaseCallbackHandler as LangchainBaseCallbackHandler,
    )
    from langchain.schema.agent import AgentAction, AgentFinish
    from langchain.schema.document import Document
    from langchain_core.messages import (
        AIMessage,
        BaseMessage,
        ChatMessage,
        FunctionMessage,
        HumanMessage,
        SystemMessage,
        ToolMessage,
    )
    from langchain_core.outputs import (
        ChatGeneration,
        LLMResult,
    )
except ImportError:
    raise ModuleNotFoundError(
        "Please install langchain to use the Langfuse langchain integration: 'pip install langchain'"
    )

LANGSMITH_TAG_HIDDEN: str = "langsmith:hidden"
CONTROL_FLOW_EXCEPTION_TYPES: Set[Type[BaseException]] = set()

try:
    from langgraph.errors import GraphBubbleUp

    CONTROL_FLOW_EXCEPTION_TYPES.add(GraphBubbleUp)
except ImportError:
    pass


class LangchainCallbackHandler(LangchainBaseCallbackHandler):
    def __init__(self, *, public_key: Optional[str] = None) -> None:
        self.client = get_client(public_key=public_key)

        self.runs: Dict[UUID, Union[LangfuseSpan, LangfuseGeneration]] = {}
        self.prompt_to_parent_run_map: Dict[UUID, Any] = {}
        self.updated_completion_start_time_memo: Set[UUID] = set()

        self.last_trace_id: Optional[str] = None

    def on_llm_new_token(
        self,
        token: str,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> Any:
        """Run on new LLM token. Only available when streaming is enabled."""
        langfuse_logger.debug(
            f"on llm new token: run_id: {run_id} parent_run_id: {parent_run_id}"
        )
        if (
            run_id in self.runs
            and isinstance(self.runs[run_id], LangfuseGeneration)
            and run_id not in self.updated_completion_start_time_memo
        ):
            current_generation = cast(LangfuseGeneration, self.runs[run_id])
            current_generation.update(completion_start_time=_get_timestamp())

            self.updated_completion_start_time_memo.add(run_id)

    def get_langchain_run_name(
        self, serialized: Optional[Dict[str, Any]], **kwargs: Any
    ) -> str:
        """Retrieve the name of a serialized LangChain runnable.

        The prioritization for the determination of the run name is as follows:
        - The value assigned to the "name" key in `kwargs`.
        - The value assigned to the "name" key in `serialized`.
        - The last entry of the value assigned to the "id" key in `serialized`.
        - "<unknown>".

        Args:
            serialized (Optional[Dict[str, Any]]): A dictionary containing the runnable's serialized data.
            **kwargs (Any): Additional keyword arguments, potentially including the 'name' override.

        Returns:
            str: The determined name of the Langchain runnable.
        """
        if "name" in kwargs and kwargs["name"] is not None:
            return str(kwargs["name"])

        if serialized is None:
            return "<unknown>"

        try:
            return str(serialized["name"])
        except (KeyError, TypeError):
            pass

        try:
            return str(serialized["id"][-1])
        except (KeyError, TypeError):
            pass

        return "<unknown>"

    def on_retriever_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> Any:
        """Run when Retriever errors."""
        try:
            self._log_debug_event(
                "on_retriever_error", run_id, parent_run_id, error=error
            )

            if run_id is None or run_id not in self.runs:
                raise Exception("run not found")

            self.runs[run_id].update(
                level="ERROR",
                status_message=str(error),
                input=kwargs.get("inputs"),
            ).end()

        except Exception as e:
            langfuse_logger.exception(e)

    def on_chain_start(
        self,
        serialized: Optional[Dict[str, Any]],
        inputs: Dict[str, Any],
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Any:
        try:
            self._log_debug_event(
                "on_chain_start", run_id, parent_run_id, inputs=inputs
            )
            self._register_langfuse_prompt(
                run_id=run_id, parent_run_id=parent_run_id, metadata=metadata
            )

            span_name = self.get_langchain_run_name(serialized, **kwargs)
            span_metadata = self.__join_tags_and_metadata(tags, metadata)
            span_level = "DEBUG" if tags and LANGSMITH_TAG_HIDDEN in tags else None

            if parent_run_id is None:
                self.runs[run_id] = self.client.start_span(
                    name=span_name,
                    metadata=span_metadata,
                    input=inputs,
                    level=cast(
                        Optional[Literal["DEBUG", "DEFAULT", "WARNING", "ERROR"]],
                        span_level,
                    ),
                )
            else:
                self.runs[run_id] = cast(
                    LangfuseSpan, self.runs[parent_run_id]
                ).start_span(
                    name=span_name,
                    metadata=span_metadata,
                    input=inputs,
                    level=cast(
                        Optional[Literal["DEBUG", "DEFAULT", "WARNING", "ERROR"]],
                        span_level,
                    ),
                )

            self.last_trace_id = self.runs[run_id].trace_id

        except Exception as e:
            langfuse_logger.exception(e)

    def _register_langfuse_prompt(
        self,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID],
        metadata: Optional[Dict[str, Any]],
    ) -> None:
        """We need to register any passed Langfuse prompt to the parent_run_id so that we can link following generations with that prompt.

        If parent_run_id is None, we are at the root of a trace and should not attempt to register the prompt, as there will be no LLM invocation following it.
        Otherwise it would have been traced in with a parent run consisting of the prompt template formatting and the LLM invocation.
        """
        if not parent_run_id:
            return

        langfuse_prompt = metadata and metadata.get("langfuse_prompt", None)

        if langfuse_prompt:
            self.prompt_to_parent_run_map[parent_run_id] = langfuse_prompt

        # If we have a registered prompt that has not been linked to a generation yet, we need to allow _children_ of that chain to link to it.
        # Otherwise, we only allow generations on the same level of the prompt rendering to be linked, not if they are nested.
        elif parent_run_id in self.prompt_to_parent_run_map:
            registered_prompt = self.prompt_to_parent_run_map[parent_run_id]
            self.prompt_to_parent_run_map[run_id] = registered_prompt

    def _deregister_langfuse_prompt(self, run_id: Optional[UUID]) -> None:
        if run_id in self.prompt_to_parent_run_map:
            del self.prompt_to_parent_run_map[run_id]

    def on_agent_action(
        self,
        action: AgentAction,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> Any:
        """Run on agent action."""
        try:
            self._log_debug_event(
                "on_agent_action", run_id, parent_run_id, action=action
            )

            if run_id not in self.runs:
                raise Exception("run not found")

            self.runs[run_id].update(
                output=action,
                input=kwargs.get("inputs"),
            ).end()

        except Exception as e:
            langfuse_logger.exception(e)

    def on_agent_finish(
        self,
        finish: AgentFinish,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> Any:
        try:
            self._log_debug_event(
                "on_agent_finish", run_id, parent_run_id, finish=finish
            )
            if run_id not in self.runs:
                raise Exception("run not found")

            self.runs[run_id].update(
                output=finish,
                input=kwargs.get("inputs"),
            ).end()

        except Exception as e:
            langfuse_logger.exception(e)

    def on_chain_end(
        self,
        outputs: Dict[str, Any],
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> Any:
        try:
            self._log_debug_event(
                "on_chain_end", run_id, parent_run_id, outputs=outputs
            )

            if run_id not in self.runs:
                raise Exception("run not found")

            self.runs[run_id].update(
                output=outputs,
                input=kwargs.get("inputs"),
            ).end()

            del self.runs[run_id]

            self._deregister_langfuse_prompt(run_id)
        except Exception as e:
            langfuse_logger.exception(e)

    def on_chain_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        tags: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> None:
        try:
            self._log_debug_event("on_chain_error", run_id, parent_run_id, error=error)
            if run_id in self.runs:
                if any(isinstance(error, t) for t in CONTROL_FLOW_EXCEPTION_TYPES):
                    level = None
                else:
                    level = "ERROR"

                self.runs[run_id].update(
                    level=cast(
                        Optional[Literal["DEBUG", "DEFAULT", "WARNING", "ERROR"]], level
                    ),
                    status_message=str(error) if level else None,
                    input=kwargs.get("inputs"),
                ).end()

                del self.runs[run_id]
            else:
                langfuse_logger.warning(
                    f"Run ID {run_id} already popped from run map. Could not update run with error message"
                )

        except Exception as e:
            langfuse_logger.exception(e)

    def on_chat_model_start(
        self,
        serialized: Optional[Dict[str, Any]],
        messages: List[List[BaseMessage]],
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Any:
        try:
            self._log_debug_event(
                "on_chat_model_start", run_id, parent_run_id, messages=messages
            )
            self.__on_llm_action(
                serialized,
                run_id,
                cast(
                    List,
                    _flatten_comprehension(
                        [self._create_message_dicts(m) for m in messages]
                    ),
                ),
                parent_run_id,
                tags=tags,
                metadata=metadata,
                **kwargs,
            )
        except Exception as e:
            langfuse_logger.exception(e)

    def on_llm_start(
        self,
        serialized: Optional[Dict[str, Any]],
        prompts: List[str],
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Any:
        try:
            self._log_debug_event(
                "on_llm_start", run_id, parent_run_id, prompts=prompts
            )
            self.__on_llm_action(
                serialized,
                run_id,
                cast(List, prompts[0] if len(prompts) == 1 else prompts),
                parent_run_id,
                tags=tags,
                metadata=metadata,
                **kwargs,
            )
        except Exception as e:
            langfuse_logger.exception(e)

    def on_tool_start(
        self,
        serialized: Optional[Dict[str, Any]],
        input_str: str,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Any:
        try:
            self._log_debug_event(
                "on_tool_start", run_id, parent_run_id, input_str=input_str
            )

            if parent_run_id is None or parent_run_id not in self.runs:
                raise Exception("parent run not found")
            meta = self.__join_tags_and_metadata(tags, metadata)

            if not meta:
                meta = {}

            meta.update(
                {key: value for key, value in kwargs.items() if value is not None}
            )

            self.runs[run_id] = cast(LangfuseSpan, self.runs[parent_run_id]).start_span(
                name=self.get_langchain_run_name(serialized, **kwargs),
                input=input_str,
                metadata=meta,
                level="DEBUG" if tags and LANGSMITH_TAG_HIDDEN in tags else None,
            )

        except Exception as e:
            langfuse_logger.exception(e)

    def on_retriever_start(
        self,
        serialized: Optional[Dict[str, Any]],
        query: str,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Any:
        try:
            self._log_debug_event(
                "on_retriever_start", run_id, parent_run_id, query=query
            )
            span_name = self.get_langchain_run_name(serialized, **kwargs)
            span_metadata = self.__join_tags_and_metadata(tags, metadata)
            span_level = "DEBUG" if tags and LANGSMITH_TAG_HIDDEN in tags else None

            if parent_run_id is None:
                self.runs[run_id] = self.client.start_span(
                    name=span_name,
                    metadata=span_metadata,
                    input=query,
                    level=cast(
                        Optional[Literal["DEBUG", "DEFAULT", "WARNING", "ERROR"]],
                        span_level,
                    ),
                )
            else:
                self.runs[run_id] = cast(
                    LangfuseSpan, self.runs[parent_run_id]
                ).start_span(
                    name=span_name,
                    input=query,
                    metadata=span_metadata,
                    level=cast(
                        Optional[Literal["DEBUG", "DEFAULT", "WARNING", "ERROR"]],
                        span_level,
                    ),
                )

        except Exception as e:
            langfuse_logger.exception(e)

    def on_retriever_end(
        self,
        documents: Sequence[Document],
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> Any:
        try:
            self._log_debug_event(
                "on_retriever_end", run_id, parent_run_id, documents=documents
            )
            if run_id is None or run_id not in self.runs:
                raise Exception("run not found")

            self.runs[run_id].update(
                output=documents,
                input=kwargs.get("inputs"),
            ).end()

            del self.runs[run_id]

        except Exception as e:
            langfuse_logger.exception(e)

    def on_tool_end(
        self,
        output: str,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> Any:
        try:
            self._log_debug_event("on_tool_end", run_id, parent_run_id, output=output)
            if run_id is None or run_id not in self.runs:
                raise Exception("run not found")

            self.runs[run_id].update(
                output=output,
                input=kwargs.get("inputs"),
            ).end()

            del self.runs[run_id]

        except Exception as e:
            langfuse_logger.exception(e)

    def on_tool_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> Any:
        try:
            self._log_debug_event("on_tool_error", run_id, parent_run_id, error=error)
            if run_id is None or run_id not in self.runs:
                raise Exception("run not found")

            self.runs[run_id].update(
                status_message=str(error),
                level="ERROR",
                input=kwargs.get("inputs"),
            ).end()

            del self.runs[run_id]

        except Exception as e:
            langfuse_logger.exception(e)

    def __on_llm_action(
        self,
        serialized: Optional[Dict[str, Any]],
        run_id: UUID,
        prompts: List[Any],
        parent_run_id: Optional[UUID] = None,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        try:
            tools = kwargs.get("invocation_params", {}).get("tools", None)
            if tools and isinstance(tools, list):
                prompts.extend([{"role": "tool", "content": tool} for tool in tools])

            model_name = self._parse_model_and_log_errors(
                serialized=serialized, metadata=metadata, kwargs=kwargs
            )
            registered_prompt = (
                self.prompt_to_parent_run_map.get(parent_run_id)
                if parent_run_id is not None
                else None
            )

            if registered_prompt:
                self._deregister_langfuse_prompt(parent_run_id)

            content = {
                "name": self.get_langchain_run_name(serialized, **kwargs),
                "input": prompts,
                "metadata": self.__join_tags_and_metadata(tags, metadata),
                "model": model_name,
                "model_parameters": self._parse_model_parameters(kwargs),
                "prompt": registered_prompt,
            }

            if parent_run_id is not None and parent_run_id in self.runs:
                self.runs[run_id] = cast(
                    LangfuseSpan, self.runs[parent_run_id]
                ).start_generation(**content)  # type: ignore
            else:
                self.runs[run_id] = self.client.start_generation(**content)  # type: ignore

            self.last_trace_id = self.runs[run_id].trace_id

        except Exception as e:
            langfuse_logger.exception(e)

    @staticmethod
    def _parse_model_parameters(kwargs: Dict[str, Any]) -> Dict[str, Any]:
        """Parse the model parameters from the kwargs."""
        if kwargs["invocation_params"].get("_type") == "IBM watsonx.ai" and kwargs[
            "invocation_params"
        ].get("params"):
            kwargs["invocation_params"] = {
                **kwargs["invocation_params"],
                **kwargs["invocation_params"]["params"],
            }
            del kwargs["invocation_params"]["params"]
        return {
            key: value
            for key, value in {
                "temperature": kwargs["invocation_params"].get("temperature"),
                "max_tokens": kwargs["invocation_params"].get("max_tokens"),
                "max_completion_tokens": kwargs["invocation_params"].get(
                    "max_completion_tokens"
                ),
                "top_p": kwargs["invocation_params"].get("top_p"),
                "frequency_penalty": kwargs["invocation_params"].get(
                    "frequency_penalty"
                ),
                "presence_penalty": kwargs["invocation_params"].get("presence_penalty"),
                "request_timeout": kwargs["invocation_params"].get("request_timeout"),
                "decoding_method": kwargs["invocation_params"].get("decoding_method"),
                "min_new_tokens": kwargs["invocation_params"].get("min_new_tokens"),
                "max_new_tokens": kwargs["invocation_params"].get("max_new_tokens"),
                "stop_sequences": kwargs["invocation_params"].get("stop_sequences"),
            }.items()
            if value is not None
        }

    def _parse_model_and_log_errors(
        self,
        *,
        serialized: Optional[Dict[str, Any]],
        metadata: Optional[Dict[str, Any]],
        kwargs: Dict[str, Any],
    ) -> Optional[str]:
        """Parse the model name and log errors if parsing fails."""
        try:
            model_name = _parse_model_name_from_metadata(
                metadata
            ) or _extract_model_name(serialized, **kwargs)

            if model_name:
                return model_name

        except Exception as e:
            langfuse_logger.exception(e)

        self._log_model_parse_warning()
        return None

    def _log_model_parse_warning(self) -> None:
        if not hasattr(self, "_model_parse_warning_logged"):
            langfuse_logger.warning(
                "Langfuse was not able to parse the LLM model. The LLM call will be recorded without model name. Please create an issue: https://github.com/langfuse/langfuse/issues/new/choose"
            )

            self._model_parse_warning_logged = True

    def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> Any:
        try:
            self._log_debug_event(
                "on_llm_end", run_id, parent_run_id, response=response, kwargs=kwargs
            )
            if run_id not in self.runs:
                raise Exception("Run not found, see docs what to do in this case.")
            else:
                response_generation = response.generations[-1][-1]
                extracted_response = (
                    self._convert_message_to_dict(response_generation.message)
                    if isinstance(response_generation, ChatGeneration)
                    else _extract_raw_response(response_generation)
                )

                llm_usage = _parse_usage(response)

                # e.g. azure returns the model name in the response
                model = _parse_model(response)
                langfuse_generation = cast(LangfuseGeneration, self.runs[run_id])
                langfuse_generation.update(
                    output=extracted_response,
                    usage=llm_usage,
                    usage_details=llm_usage,
                    input=kwargs.get("inputs"),
                    model=model,
                )
                langfuse_generation.end()

                del self.runs[run_id]

        except Exception as e:
            langfuse_logger.exception(e)

        finally:
            self.updated_completion_start_time_memo.discard(run_id)

    def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> Any:
        try:
            self._log_debug_event("on_llm_error", run_id, parent_run_id, error=error)
            if run_id in self.runs:
                generation = self.runs[run_id]
                generation.update(
                    status_message=str(error),
                    level="ERROR",
                    input=kwargs.get("inputs"),
                )
                generation.end()

                del self.runs[run_id]

        except Exception as e:
            langfuse_logger.exception(e)

    def __join_tags_and_metadata(
        self,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        trace_metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        final_dict = {}
        if tags is not None and len(tags) > 0:
            final_dict["tags"] = tags
        if metadata is not None:
            final_dict.update(metadata)
        if trace_metadata is not None:
            final_dict.update(trace_metadata)
        return _strip_langfuse_keys_from_dict(final_dict) if final_dict != {} else None

    def _convert_message_to_dict(self, message: BaseMessage) -> Dict[str, Any]:
        # assistant message
        if isinstance(message, HumanMessage):
            message_dict = {"role": "user", "content": message.content}
        elif isinstance(message, AIMessage):
            message_dict = {"role": "assistant", "content": message.content}
        elif isinstance(message, SystemMessage):
            message_dict = {"role": "system", "content": message.content}
        elif isinstance(message, ToolMessage):
            message_dict = {
                "role": "tool",
                "content": message.content,
                "tool_call_id": message.tool_call_id,
            }
        elif isinstance(message, FunctionMessage):
            message_dict = {"role": "function", "content": message.content}
        elif isinstance(message, ChatMessage):
            message_dict = {"role": message.role, "content": message.content}
        else:
            raise ValueError(f"Got unknown type {message}")
        if "name" in message.additional_kwargs:
            message_dict["name"] = message.additional_kwargs["name"]

        if message.additional_kwargs:
            message_dict["additional_kwargs"] = message.additional_kwargs  # type: ignore

        return message_dict

    def _create_message_dicts(
        self, messages: List[BaseMessage]
    ) -> List[Dict[str, Any]]:
        return [self._convert_message_to_dict(m) for m in messages]

    def _log_debug_event(
        self,
        event_name: str,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        langfuse_logger.debug(
            f"Event: {event_name}, run_id: {str(run_id)[:5]}, parent_run_id: {str(parent_run_id)[:5]}"
        )


def _extract_raw_response(last_response: Any) -> Any:
    """Extract the response from the last response of the LLM call."""
    # We return the text of the response if not empty
    if last_response.text is not None and last_response.text.strip() != "":
        return last_response.text.strip()
    elif hasattr(last_response, "message"):
        # Additional kwargs contains the response in case of tool usage
        return last_response.message.additional_kwargs
    else:
        # Not tool usage, some LLM responses can be simply empty
        return ""


def _flatten_comprehension(matrix: Any) -> Any:
    return [item for row in matrix for item in row]


def _parse_usage_model(usage: typing.Union[pydantic.BaseModel, dict]) -> Any:
    # maintains a list of key translations. For each key, the usage model is checked
    # and a new object will be created with the new key if the key exists in the usage model
    # All non matched keys will remain on the object.

    if hasattr(usage, "__dict__"):
        usage = usage.__dict__

    conversion_list = [
        # https://pypi.org/project/langchain-anthropic/ (works also for Bedrock-Anthropic)
        ("input_tokens", "input"),
        ("output_tokens", "output"),
        ("total_tokens", "total"),
        # https://cloud.google.com/vertex-ai/generative-ai/docs/multimodal/get-token-count
        ("prompt_token_count", "input"),
        ("candidates_token_count", "output"),
        ("total_token_count", "total"),
        # Bedrock: https://docs.aws.amazon.com/bedrock/latest/userguide/monitoring-cw.html#runtime-cloudwatch-metrics
        ("inputTokenCount", "input"),
        ("outputTokenCount", "output"),
        ("totalTokenCount", "total"),
        # langchain-ibm https://pypi.org/project/langchain-ibm/
        ("input_token_count", "input"),
        ("generated_token_count", "output"),
    ]

    usage_model = cast(Dict, usage.copy())  # Copy all existing key-value pairs

    # Skip OpenAI usage types as they are handled server side
    if not all(
        openai_key in usage_model
        for openai_key in ["prompt_tokens", "completion_tokens", "total_tokens"]
    ):
        for model_key, langfuse_key in conversion_list:
            if model_key in usage_model:
                captured_count = usage_model.pop(model_key)
                final_count = (
                    sum(captured_count)
                    if isinstance(captured_count, list)
                    else captured_count
                )  # For Bedrock, the token count is a list when streamed

                usage_model[langfuse_key] = (
                    final_count  # Translate key and keep the value
                )

    if isinstance(usage_model, dict):
        if "input_token_details" in usage_model:
            input_token_details = usage_model.pop("input_token_details", {})

            for key, value in input_token_details.items():
                usage_model[f"input_{key}"] = value

                if "input" in usage_model:
                    usage_model["input"] = max(0, usage_model["input"] - value)

        if "output_token_details" in usage_model:
            output_token_details = usage_model.pop("output_token_details", {})

            for key, value in output_token_details.items():
                usage_model[f"output_{key}"] = value

                if "output" in usage_model:
                    usage_model["output"] = max(0, usage_model["output"] - value)

        # Vertex AI
        if "prompt_tokens_details" in usage_model and isinstance(
            usage_model["prompt_tokens_details"], list
        ):
            prompt_tokens_details = usage_model.pop("prompt_tokens_details")

            for item in prompt_tokens_details:
                if (
                    isinstance(item, dict)
                    and "modality" in item
                    and "token_count" in item
                ):
                    value = item["token_count"]
                    usage_model[f"input_modality_{item['modality']}"] = value

                    if "input" in usage_model:
                        usage_model["input"] = max(0, usage_model["input"] - value)

        # Vertex AI
        if "candidates_tokens_details" in usage_model and isinstance(
            usage_model["candidates_tokens_details"], list
        ):
            candidates_tokens_details = usage_model.pop("candidates_tokens_details")

            for item in candidates_tokens_details:
                if (
                    isinstance(item, dict)
                    and "modality" in item
                    and "token_count" in item
                ):
                    value = item["token_count"]
                    usage_model[f"output_modality_{item['modality']}"] = value

                    if "output" in usage_model:
                        usage_model["output"] = max(0, usage_model["output"] - value)

        # Vertex AI
        if "cache_tokens_details" in usage_model and isinstance(
            usage_model["cache_tokens_details"], list
        ):
            cache_tokens_details = usage_model.pop("cache_tokens_details")

            for item in cache_tokens_details:
                if (
                    isinstance(item, dict)
                    and "modality" in item
                    and "token_count" in item
                ):
                    value = item["token_count"]
                    usage_model[f"cached_modality_{item['modality']}"] = value

                    if "input" in usage_model:
                        usage_model["input"] = max(0, usage_model["input"] - value)

    usage_model = {k: v for k, v in usage_model.items() if not isinstance(v, str)}

    return usage_model if usage_model else None


def _parse_usage(response: LLMResult) -> Any:
    # langchain-anthropic uses the usage field
    llm_usage_keys = ["token_usage", "usage"]
    llm_usage = None
    if response.llm_output is not None:
        for key in llm_usage_keys:
            if key in response.llm_output and response.llm_output[key]:
                llm_usage = _parse_usage_model(response.llm_output[key])
                break

    if hasattr(response, "generations"):
        for generation in response.generations:
            for generation_chunk in generation:
                if generation_chunk.generation_info and (
                    "usage_metadata" in generation_chunk.generation_info
                ):
                    llm_usage = _parse_usage_model(
                        generation_chunk.generation_info["usage_metadata"]
                    )
                    break

                message_chunk = getattr(generation_chunk, "message", {})
                response_metadata = getattr(message_chunk, "response_metadata", {})

                chunk_usage = (
                    (
                        response_metadata.get("usage", None)  # for Bedrock-Anthropic
                        if isinstance(response_metadata, dict)
                        else None
                    )
                    or (
                        response_metadata.get(
                            "amazon-bedrock-invocationMetrics", None
                        )  # for Bedrock-Titan
                        if isinstance(response_metadata, dict)
                        else None
                    )
                    or getattr(message_chunk, "usage_metadata", None)  # for Ollama
                )

                if chunk_usage:
                    llm_usage = _parse_usage_model(chunk_usage)
                    break

    return llm_usage


def _parse_model(response: LLMResult) -> Any:
    # langchain-anthropic uses the usage field
    llm_model_keys = ["model_name"]
    llm_model = None
    if response.llm_output is not None:
        for key in llm_model_keys:
            if key in response.llm_output and response.llm_output[key]:
                llm_model = response.llm_output[key]
                break

    return llm_model


def _parse_model_name_from_metadata(metadata: Optional[Dict[str, Any]]) -> Any:
    if metadata is None or not isinstance(metadata, dict):
        return None

    return metadata.get("ls_model_name", None)


def _strip_langfuse_keys_from_dict(metadata: Optional[Dict[str, Any]]) -> Any:
    if metadata is None or not isinstance(metadata, dict):
        return metadata

    langfuse_metadata_keys = ["langfuse_prompt"]

    metadata_copy = metadata.copy()

    for key in langfuse_metadata_keys:
        metadata_copy.pop(key, None)

    return metadata_copy
