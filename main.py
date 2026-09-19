"""Inject a per-conversation ``X-Opencode-Session`` header into AstrBot LLM calls.

OpenCode Go keys its GPU context cache by a client supplied session identifier.
AstrBot builds provider request headers exactly once, at provider construction
time: ``astrbot/core/provider/headers.py:6-24`` only stringifies the configured
mapping and performs no placeholder substitution. A static ``custom_headers``
entry therefore cannot express "one value per conversation".

This plugin derives the value from the conversation chain and merges it into each
individual request through the SDK's per-call ``extra_headers`` argument. Shared
provider/client state -- the configured headers of the provider and the client's
private default header mapping -- is only ever read, never written, so concurrent
conversations cannot leak into each other's requests.
"""

from __future__ import annotations

import contextvars
import functools
import inspect
import logging
import uuid
from typing import TYPE_CHECKING, Any

from astrbot.api.event import filter
from astrbot.api.star import Star

if TYPE_CHECKING:
    from collections.abc import Iterator

    from astrbot.api.event import AstrMessageEvent
    from astrbot.api.provider import ProviderRequest

logger = logging.getLogger(__name__)

HEADER_NAME = "X-Opencode-Session"
"""Default header name, in canonical casing.

The casing is load bearing: the OpenAI SDK merges its ``default_headers`` with
the per-call headers through a case-sensitive ``{**default, **call}`` dict merge
before httpx normalises anything, so only an exactly-equal key replaces a value
the user hardcoded into ``custom_headers``. A differently cased key produces two
separate headers instead of an override.
"""

# --- Configuration -----------------------------------------------------------
# Populated from the WebUI config in ``_conf_schema.json`` when the plugin is
# instantiated. Module level because ``wrap_provider`` and the wrappers it
# installs are module level too, and they must observe config changes without
# re-installing anything.
TARGET_HEADER: str = HEADER_NAME
MATCH_MODE: str = "base_url"
HOST_KEYWORDS: tuple[str, ...] = ("opencode",)
MATCH_MODES: tuple[str, ...] = ("base_url", "provider_id", "provider_type")

SESSION_KEY: contextvars.ContextVar[str] = contextvars.ContextVar(
    "opencode_session_key",
    default="",
)
"""The value injected as ``X-Opencode-Session``; "" means "do not inject"."""

SESSION_SID: contextvars.ContextVar[str] = contextvars.ContextVar(
    "opencode_session_sid",
    default="",
)
"""The session id that ``SESSION_KEY`` currently describes.

Acts as a guard so the ``text_chat`` fallback never downgrades a key the hook
already resolved from ``conversation.cid`` into ``uuid5(session_id)``.
"""

WRAP_FLAG = "__oc_session_wrapped__"
ORIGINALS_ATTR = "__oc_session_originals__"
WARNED_ATTR = "__oc_session_case_warned__"

_CREATE_CHAINS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("chat", "completions"), "create"),
    (("responses",), "create"),
    # ``GET /models`` is a separate resource, but AstrBot's provider "test"
    # button reaches it through ``provider.get_models()``
    # (openai_source.py:437-447 -> client.models.list). Upstream rejects that
    # request too when the session header is absent, so it must be wrapped.
    (("models",), "list"),
)
_TEXT_METHODS = ("text_chat", "text_chat_stream")
_MAX_KEY_LENGTH = 128


def _clean(value: Any) -> str:
    """Return ``value`` as a stripped, header-safe string.

    Args:
        value: Any candidate token; ``None`` is treated as absent.

    Returns:
        The cleaned token, or "" when it is empty, too long, or contains control
        characters that are illegal inside an HTTP header value.
    """
    text = str(value).strip() if value is not None else ""
    if not text or any(ord(char) < 0x20 for char in text):
        return ""
    return text[:_MAX_KEY_LENGTH]


def _derive(session_id: str) -> str:
    """Derive a stable session key from an opaque session id.

    Args:
        session_id: Non-empty session identifier.

    Returns:
        The deterministic UUIDv5 string for ``session_id``. There is no clock,
        no randomness and no I/O involved, so a persisted mapping table is never
        needed: the same session id always yields the same key.
    """
    return str(uuid.uuid5(uuid.NAMESPACE_URL, session_id))


def _resolve_session(event: Any, req: Any) -> tuple[str, str]:
    """Resolve the conversation key using the frozen three-source order.

    Args:
        event: The message event carrying ``unified_msg_origin``.
        req: The provider request carrying ``conversation`` and ``session_id``.

    Returns:
        A ``(key, session_id)`` pair. ``key`` is "" when no source yields a
        value; in that case the header must be omitted entirely rather than
        degraded into a constant shared by every broken request.
    """
    conversation = getattr(req, "conversation", None)
    cid = _clean(getattr(conversation, "cid", None)) if conversation is not None else ""
    session_id = _clean(getattr(req, "session_id", None)) or _clean(
        getattr(event, "unified_msg_origin", None)
    )
    if cid:
        return cid, session_id
    if session_id:
        return _derive(session_id), session_id
    return "", ""


def _session_id_from_call(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    """Read the explicit ``session_id`` of a provider call.

    ``session_id`` is the second positional parameter of ``Provider.text_chat``
    (``astrbot/core/provider/provider.py:100-103``), but every in-tree caller
    passes it as a keyword argument, so both forms are honoured.

    Args:
        args: Positional arguments of the provider call.
        kwargs: Keyword arguments of the provider call.

    Returns:
        The cleaned session id, or "" when the call does not carry one.
    """
    session_id = kwargs.get("session_id")
    if session_id is None and len(args) > 1:
        session_id = args[1]
    return _clean(session_id)


def _apply_session_from_call(args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
    """Bind the conversation key from an explicit ``session_id`` argument.

    This is the fallback for provider calls that never reach ``on_llm_request``
    (for example the tool-loop re-queries). It only ever *sets*: a reset would
    hand a stale key to the streaming HTTP call, which happens in a later
    pipeline stage but still inside this same task.

    Args:
        args: Positional arguments of the provider call.
        kwargs: Keyword arguments of the provider call.
    """
    session_id = _session_id_from_call(args, kwargs)
    if not session_id or session_id == SESSION_SID.get():
        # Nothing to bind, or the hook already resolved this very conversation,
        # possibly from a cid, which must win over a derived value.
        return
    SESSION_SID.set(session_id)
    SESSION_KEY.set(_derive(session_id))


def _iter_creates(client: Any) -> Iterator[tuple[tuple[tuple[str, ...], str], Any, Any]]:
    """Yield every request entry point of an OpenAI-style client.

    Not just ``create``: ``models.list`` is a second outbound path that AstrBot
    reaches when fetching the model list, and it needs the same header.

    Args:
        client: Candidate SDK client; ``None`` yields nothing.

    Yields:
        ``((attribute_chain, method), holder, callable)`` triples. Chains are
        resolved dynamically, so a client without ``responses`` or ``models``
        degrades silently.
    """
    if client is None:
        return
    for chain, method in _CREATE_CHAINS:
        holder = client
        for attribute in chain:
            holder = getattr(holder, attribute, None)
            if holder is None:
                break
        if holder is None:
            continue
        func = getattr(holder, method, None)
        if callable(func):
            yield (chain, method), holder, func


def _resource_url(holder: Any) -> str:
    """Return the base URL configured for an OpenAI-style resource.

    Used only for diagnostics: injection is never gated on the URL.

    Args:
        holder: Object owning the wrapped callable.

    Returns:
        The lower-cased base URL, or "" when it cannot be determined.
    """
    url = getattr(holder, "base_url", None)
    if url is None:
        client = getattr(holder, "_client", None)
        url = getattr(client, "base_url", None)
    return str(url).lower() if url is not None else ""


def _match_candidate(provider: Any, holder: Any) -> str:
    """Return the single string the configured ``MATCH_MODE`` compares against.

    The mode picks one field only, so the behaviour stays predictable:

    - ``base_url`` (default): the provider's configured URL. This is the strict
      choice; note it cannot match a request routed through a self-hosted relay,
      whose URL is a LAN address unrelated to the upstream domain.
    - ``provider_id``: the provider's ID, e.g. ``opencode/deepseek-v4-flash``.
      This is what still carries the upstream name when a relay is in front.
    - ``provider_type``: the provider's type, e.g. ``chat_completion``.

    Args:
        provider: Provider instance owning the client.
        holder: Object owning the wrapped callable.

    Returns:
        The lower-cased candidate string, or "" when it cannot be read.
    """
    if MATCH_MODE == "provider_id":
        try:
            value = getattr(provider.meta(), "id", None)
        except Exception:
            value = None
    elif MATCH_MODE == "provider_type":
        try:
            value = getattr(provider.meta(), "provider_type", None)
        except Exception:
            value = None
    else:
        value = _resource_url(holder)
    return str(value).lower() if value else ""


def _should_inject(provider: Any, holder: Any) -> bool:
    """Decide whether the header belongs on this provider's requests.

    Matching is an AND of two independent conditions: a keyword list that is not
    empty, and the configured mode's field containing one of those keywords. An
    empty keyword list means "no filter", so clearing it can never silently
    disable injection.

    Args:
        provider: Provider instance owning the client.
        holder: Object owning the wrapped callable.

    Returns:
        True when the header should be injected for this resource.
    """
    if not HOST_KEYWORDS:
        return True
    candidate = _match_candidate(provider, holder)
    if not candidate:
        return False
    return any(keyword in candidate for keyword in HOST_KEYWORDS)


def _install_create(
    holder: Any,
    label: str,
    method: str,
    current: Any,
    originals: dict[str, Any],
    provider: Any = None,
) -> None:
    """Install the header injection wrapper on one client entry point.

    Args:
        holder: Object owning the callable (a completions/responses/models
            resource).
        label: Stable key used to remember the original callable.
        method: Attribute name to replace, e.g. ``create`` or ``list``.
        current: The currently bound callable.
        originals: Per-provider map of label to the original callable.
        provider: Provider instance owning the client.
    """
    original = originals.get(label) or current
    originals[label] = original
    # ``models.list`` carries no conversation context when the dashboard calls it
    # (``provider.get_models()``, config_service.py:1735, outside the LLM
    # pipeline). Upstream rejects a request whose session header is missing, so
    # this one path falls back to a fresh identifier rather than sending
    # nothing. The value is deliberately *not* stable across calls: every other
    # path keeps the "no session identity => no header" rule, and a single
    # shared constant would make unrelated requests look like one session.
    is_contextless = method == "list"

    @functools.wraps(original)
    async def create_wrapper(*args: Any, **kwargs: Any) -> Any:
        key = SESSION_KEY.get()
        if not key and is_contextless:
            key = str(uuid.uuid4())
        if key and _should_inject(provider, holder):
            header_name = TARGET_HEADER or HEADER_NAME
            headers = dict(kwargs.get("extra_headers") or {})
            # Canonical key first, so the dedupe below can never delete it.
            headers[header_name] = key
            for name in [
                name
                for name in headers
                if name != header_name and str(name).lower() == header_name.lower()
            ]:
                del headers[name]
            headers[header_name] = key
            kwargs["extra_headers"] = headers
        elif key:
            logger.debug(
                "skipped X-Opencode-Session injection: match_mode=%s candidate=%r "
                "does not contain any of %s",
                MATCH_MODE,
                _match_candidate(provider, holder),
                HOST_KEYWORDS,
            )
        return await original(*args, **kwargs)

    setattr(holder, method, create_wrapper)


def _install_text_method(
    provider: Any,
    name: str,
    current: Any,
    originals: dict[str, Any],
) -> None:
    """Install the context binding wrapper on one provider text method.

    Args:
        provider: Provider instance owning the method.
        name: Method name, either ``text_chat`` or ``text_chat_stream``.
        current: The currently bound method.
        originals: Per-provider map of name to the original method.
    """
    original = originals.get(name) or current
    originals[name] = original

    if inspect.isasyncgenfunction(original):
        # ``text_chat_stream`` is an async generator function. A plain function
        # wrapper is sufficient and preferable: an async generator body runs in
        # the caller's context on every resumption, so binding the key before the
        # generator is created is enough, and no forwarding layer is needed.
        @functools.wraps(original)
        def stream_wrapper(*args: Any, **kwargs: Any) -> Any:
            # The hook may never have run on this path, so make sure the
            # ``create`` wrapper that consumes the key is in place first.
            wrap_provider(provider)
            # Intentionally no reset; see the streaming note in the hook.
            _apply_session_from_call(args, kwargs)
            return original(*args, **kwargs)

        setattr(provider, name, stream_wrapper)
        return

    # ``text_chat`` is a coroutine function and stays one. ``functools.wraps``
    # only sets ``__wrapped__``, which ``inspect.iscoroutinefunction`` does not
    # follow, so a synchronic wrapper would silently flip that attribute and any
    # code branching on it would take the wrong path.
    @functools.wraps(original)
    async def chat_wrapper(*args: Any, **kwargs: Any) -> Any:
        # The hook may never have run on this path, so make sure the ``create``
        # wrapper that consumes the key is in place first.
        wrap_provider(provider)
        # Intentionally no reset; see the streaming note in the hook.
        _apply_session_from_call(args, kwargs)
        return await original(*args, **kwargs)

    setattr(provider, name, chat_wrapper)


def _provider_label(provider: Any) -> str:
    """Return a short label identifying one provider in log output.

    Args:
        provider: Provider instance to describe.

    Returns:
        The configured provider id when it can be read safely, otherwise the
        provider's class name. Never raises: an unreadable id must not suppress
        the warning it is attached to.
    """
    try:
        label = getattr(provider.meta(), "id", None)
        if label:
            return str(label)
    except Exception:
        pass
    return type(provider).__name__ or "unknown"


def _warn_case_variant_headers(provider: Any) -> None:
    """Warn once when configured headers would produce a duplicate header.

    A ``custom_headers`` entry whose name differs from the target header only in
    casing cannot be overridden, because the SDK merges headers
    case-sensitively: both the hardcoded value and the per-conversation value
    would be sent, and the upstream service may pick the hardcoded one. The
    user's configuration is reported, never modified.

    Args:
        provider: Provider instance whose configured headers are inspected.
    """
    if getattr(provider, WARNED_ATTR, False):
        return
    headers = getattr(provider, "custom_headers", None) or getattr(
        provider,
        "request_headers",
        None,
    )
    if not isinstance(headers, dict):
        return
    target = TARGET_HEADER or HEADER_NAME
    variants = [
        str(name)
        for name in headers
        if str(name) != target and str(name).lower() == target.lower()
    ]
    if not variants:
        return
    setattr(provider, WARNED_ATTR, True)
    logger.warning(
        "Provider %s defines %s with the non-canonical casing %s. "
        "The OpenAI SDK merges headers case-sensitively, so both that hardcoded "
        "value and the per-conversation value would be sent and the upstream "
        "service may pick the hardcoded one, which disables per-session cache "
        "affinity. Remove that custom_headers entry, or rename it to %s.",
        _provider_label(provider),
        target,
        variants,
        target,
    )


def wrap_provider(provider: Any) -> bool:
    """Idempotently install the header injection wrappers on one provider.

    Every call rebuilds the wrappers from the originals recorded on the first
    call, so repeated calls never stack layers. Rebuilding on every call is also
    what makes a plugin hot reload self-healing: a reload creates a brand new
    module-level ContextVar while the provider and its client survive, so a
    wrapper still bound to the previous module would keep reading a variable that
    nothing writes any more.

    Args:
        provider: Candidate provider instance; anything unusable is skipped.

    Returns:
        True when at least one wrapper is installed on the provider.
    """
    if provider is None:
        return False
    originals = getattr(provider, ORIGINALS_ATTR, None)
    if not isinstance(originals, dict):
        originals = {}
        setattr(provider, ORIGINALS_ATTR, originals)

    wrapped = False
    client = getattr(provider, "client", None)
    for (chain, method), holder, current in _iter_creates(client):
        _install_create(
            holder,
            ".".join((*chain, method)),
            method,
            current,
            originals,
            provider,
        )
        wrapped = True
    for name in _TEXT_METHODS:
        method = getattr(provider, name, None)
        if callable(method):
            _install_text_method(provider, name, method, originals)
            wrapped = True
    if wrapped:
        setattr(provider, WRAP_FLAG, True)
    _warn_case_variant_headers(provider)
    return wrapped


def unwrap_provider(provider: Any) -> None:
    """Restore the provider methods this plugin replaced.

    Args:
        provider: Provider instance previously passed to ``wrap_provider``.
    """
    originals = getattr(provider, ORIGINALS_ATTR, None)
    if not isinstance(originals, dict) or not originals:
        return
    client = getattr(provider, "client", None)
    for label, original in originals.items():
        holder_name, _, attribute = label.rpartition(".")
        target: Any = provider
        if holder_name:
            target = client
            for part in holder_name.split("."):
                target = getattr(target, part, None)
                if target is None:
                    break
        if target is not None:
            setattr(target, attribute, original)
    originals.clear()
    provider.__dict__.pop(ORIGINALS_ATTR, None)
    provider.__dict__.pop(WRAP_FLAG, None)


class OpencodeSessionPlugin(Star):
    """Bind a per-conversation ``X-Opencode-Session`` header to targeted LLM calls."""

    def __init__(self, context: Any, config: Any = None) -> None:
        """Read the WebUI configuration into the module-level settings.

        AstrBot parses ``_conf_schema.json`` and passes the resulting mapping
        here on every instantiation, so the settings must be re-applied each
        time rather than initialised once. Anything unusable keeps its default.

        Args:
            context: AstrBot plugin context.
            config: Mapping produced from ``_conf_schema.json``; ``None`` when
                the plugin declares no schema.
        """
        super().__init__(context)
        global TARGET_HEADER, MATCH_MODE, HOST_KEYWORDS

        if isinstance(config, dict):
            header = config.get("target_header")
            if isinstance(header, str) and header.strip():
                TARGET_HEADER = header.strip()
            else:
                TARGET_HEADER = HEADER_NAME

            mode = config.get("match_mode")
            MATCH_MODE = mode if mode in MATCH_MODES else "base_url"

            keywords = config.get("host_keywords")
            if isinstance(keywords, (list, tuple)):
                HOST_KEYWORDS = tuple(
                    str(item).strip().lower() for item in keywords if str(item).strip()
                )

        logger.debug(
            "opencode session injection target: header=%s match_mode=%s keywords=%s",
            TARGET_HEADER,
            MATCH_MODE,
            HOST_KEYWORDS,
        )

    @filter.on_llm_request()
    async def on_llm_request(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
    ) -> None:
        """Record the conversation key and keep every provider wrapped.

        Args:
            event: The message event being processed.
            req: The provider request about to be sent to the model.
        """
        try:
            key, session_id = _resolve_session(event, req)
            # Intentionally no reset: the streaming HTTP call happens in a later
            # pipeline stage but inside this same task, so resetting here would
            # expose the previous conversation's key to it.
            SESSION_KEY.set(key)
            SESSION_SID.set(session_id)
            for provider in self.context.get_all_providers():
                wrap_provider(provider)
        except Exception:
            logger.debug("opencode session injection skipped", exc_info=True)

    async def initialize(self) -> None:
        """Wrap the providers that already exist when the plugin is activated."""
        try:
            for provider in self.context.get_all_providers():
                wrap_provider(provider)
        except Exception:
            logger.debug("opencode session prewrap skipped", exc_info=True)

    async def terminate(self) -> None:
        """Restore the provider methods this plugin replaced."""
        try:
            for provider in self.context.get_all_providers():
                unwrap_provider(provider)
        except Exception:
            logger.debug("opencode session unwrap skipped", exc_info=True)
