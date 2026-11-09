"""Claude client. Every call's usage must be passed to CostGuard.record()."""

from anthropic import AsyncAnthropic, AuthenticationError, BadRequestError, NotFoundError

from ultron.config import Settings
from ultron.errors import UserFacingError


class LLMError(UserFacingError):
    pass


def make_client(settings: Settings) -> AsyncAnthropic:
    return AsyncAnthropic(api_key=settings.anthropic_api_key.get_secret_value())


async def check_access(settings: Settings) -> str:
    """Look up both configured models via the Models API (free: no tokens used)."""
    async with make_client(settings) as client:
        try:
            for model in (settings.model_fast, settings.model_smart):
                await client.models.retrieve(model)
        except AuthenticationError as exc:
            raise LLMError(
                "Anthropic rejected ANTHROPIC_API_KEY. Check it at console.anthropic.com."
            ) from exc
        except BadRequestError as exc:
            if "workspace" not in str(exc).lower():
                raise
            raise LLMError(
                "This API key isn't tied to a workspace. In console.anthropic.com go to "
                "Settings → Workspaces → Default → API keys, create a key there, and put it "
                "in ANTHROPIC_API_KEY."
            ) from exc
        except NotFoundError as exc:
            raise LLMError(
                f"Model '{model}' isn't available to this key. Check MODEL_FAST / MODEL_SMART."
            ) from exc
    return f"key works; {settings.model_fast} and {settings.model_smart} available"
