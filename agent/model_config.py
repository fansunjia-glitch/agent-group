"""Request dialects for services exposing OpenAI Chat Completions."""

from urllib.parse import urlsplit


def resolve_api_mode(api_mode=None, base_url=None):
    """Only the official DeepSeek endpoint opts into its extensions implicitly.

    A proxy may select the dialect explicitly. Model names do not select a
    dialect: a generic endpoint can serve a DeepSeek model without accepting
    DeepSeek's non-standard request fields.
    """
    if api_mode is not None:
        if api_mode not in ("openai", "deepseek"):
            raise ValueError("api_mode must be openai or deepseek")
        return api_mode
    hostname = urlsplit(str(base_url or "")).hostname
    return "deepseek" if hostname == "api.deepseek.com" else "openai"


def completion_options(api_mode, user_id, *, thinking=False):
    if api_mode == "deepseek":
        return {"extra_body": {"thinking": {"type": "enabled" if thinking else "disabled"},
                               "user_id": user_id}}
    # `user` is the standard Chat Completions identity field. Keep the same
    # anonymous stable ID used locally, without a provider-specific body key.
    return {"user": user_id}


def completion_messages(messages, api_mode):
    """Keep stored history intact when switching away from DeepSeek."""
    if api_mode == "deepseek":
        return messages
    return [{key: value for key, value in message.items() if key != "reasoning_content"}
            for message in messages]
