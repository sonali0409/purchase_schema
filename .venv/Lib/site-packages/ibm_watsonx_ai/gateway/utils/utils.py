#  -----------------------------------------------------------------------------------------
#  (C) Copyright IBM Corp. 2026.
#  https://opensource.org/licenses/BSD-3-Clause
#  -----------------------------------------------------------------------------------------
import logging

from ibm_watsonx_ai.gateway import Gateway
from ibm_watsonx_ai.utils import get_from_json
from ibm_watsonx_ai.wml_client_error import WMLClientError

logger = logging.getLogger(__name__)


def get_max_input_tokens(
    gateway: Gateway, model_id: str, max_completion_tokens: int = 1024
) -> int:
    """Get maximum number of tokens allowed as input for a given model.

    :param gateway: initialized Model Gateway instance
    :type gateway: Gateway

    :param model_id: unique model ID
    :type model_id: str

    :param max_completion_tokens: the maximum number of tokens that can be generated in the chat completion, defaults to 1024
    :type max_completion_tokens: int, optional

    :return: the maximum number of tokens allowed as input for a given model
    :rtype: int
    """
    model_details = gateway.models.get_details(model_id=model_id)
    model_context_window = get_from_json(model_details, ["metadata", "context_window"])

    if model_context_window is None:
        error_msg = (
            f"Maximum input tokens for the model id `{model_id}` cannot be calculated"
        )
        reason_msg = "The `context_window` cannot be found in the model metadata"
        raise WMLClientError(error_msg=error_msg, reason=reason_msg)

    return model_context_window - max_completion_tokens


def drop_nones(extra: dict, **params: object) -> dict:
    """Return a dict of *params* with ``None`` values removed, merged with *extra*.

    Only ``None`` is dropped.  Falsy-but-meaningful values such as ``False``,
    ``0``, ``0.0``, and ``[]`` are **kept** so that callers can forward
    parameters like ``logprobs=False``, ``store=False``,
    ``parallel_tool_calls=False``, ``temperature=0``, or ``stop=[]`` to the
    API without them being silently discarded.

    *extra* is a pass-through dict (typically ``**kwargs`` from the caller)
    that is merged first so explicit keyword arguments take precedence.

    Example::

        call_params = drop_nones(kwargs, temperature=temperature, seed=seed)
    """
    return {k: v for k, v in {**extra, **params}.items() if v is not None}
