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
TRANSPORT_INJECT: bool = True
CONTEXTLESS_SESSION_ID: str = "test"
RANDOM_CONTEXTLESS: bool = False
"""When set, every contextless request gets its own fresh UUID."""

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
_HTTP_MARK_ATTR = "__oc_session_http_marked__"
"""Marks the patched ``AsyncOpenAI.__init__`` so it is never stacked twice."""

_wrap_targets: list[Any] = []
"""Providers this plugin has wrapped, used to reach their httpx clients."""

_openai_original_init: Any = None
_openai_patched_cls: Any = None
"""Bookkeeping for the ``AsyncOpenAI.__init__`` patch."""

_CREATE_CHAINS: tuple[tuple[str, ...], ...] = (
    ("chat", "completions"),
    ("responses",),
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


def _iter_creates(client: Any) -> Iterator[tuple[tuple[str, ...], Any, Any]]:
    """Yield every ``create`` callable of an OpenAI-style client.

    Args:
        client: Candidate SDK client; ``None`` yields nothing.

    Yields:
        ``(attribute_chain, holder, callable)`` triples. Chains are resolved
        dynamically, so a client without ``responses`` degrades silently.
    """
    if client is None:
        return
    for chain in _CREATE_CHAINS:
        holder = client
        for attribute in chain:
            holder = getattr(holder, attribute, None)
            if holder is None:
                break
        if holder is None:
            continue
        func = getattr(holder, "create", None)
        if callable(func):
            yield chain, holder, func


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


def _contextless_key() -> str:
    """Return the value used when a request carries no conversation context.

    The dashboard's model-list test builds a throwaway provider instance
    (``dashboard/services/config_service.py:1694``) that this plugin never wraps,
    and it must still send a non-empty value because upstream answers
    ``400 MissingSessionID`` otherwise.

    Three modes, in order of precedence:

    1. ``random_contextless_value`` on -- a fresh UUID per request. This never
       collides with a real conversation's cache and never makes unrelated test
       requests look like one session.
    2. a configured ``contextless_session_id``.
    3. a random UUID, when the configured value is blank.

    Returns:
        A non-empty header-safe string.
    """
    if RANDOM_CONTEXTLESS:
        return str(uuid.uuid4())
    configured = _clean(CONTEXTLESS_SESSION_ID)
    return configured if configured else str(uuid.uuid4())


def install_http_injector() -> None:
    """Make sure every SDK client carries a default value for the header.

    The plugin normally injects per conversation through the SDK's per-call
    ``extra_headers``, but that only reaches clients it has wrapped. The
    dashboard's model-list test builds a throwaway provider instance
    (``dashboard/services/config_service.py:1694``) and calls ``get_models()``
    directly, so no wrapper is involved.

    Two things cover that path:

    1. already-live clients are given the default now, and
    2. ``AsyncOpenAI.__init__`` is patched so clients created later -- including
       that throwaway instance, which is built while the request is served -- get
       the default too.
    """
    _install_openai_ctor_patch()
    for holder in _sdk_clients():
        _install_on_client(holder)


def _install_openai_ctor_patch() -> None:
    """Patch ``AsyncOpenAI.__init__`` so new clients get the transport tag."""
    global _openai_original_init, _openai_patched_cls
    if not TRANSPORT_INJECT:
        return
    try:
        from openai import AsyncOpenAI
    except Exception:
        return
    current = AsyncOpenAI.__init__
    if getattr(current, _HTTP_MARK_ATTR, False):
        return  # already patched by this module
    _openai_original_init = current
    _openai_patched_cls = AsyncOpenAI

    @functools.wraps(current)
    def patched_init(self: Any, *args: Any, **kwargs: Any) -> None:
        current(self, *args, **kwargs)
        try:
            _install_on_client(self)
        except Exception:
            logger.debug("could not tag a new SDK client", exc_info=True)

    setattr(patched_init, _HTTP_MARK_ATTR, True)
    AsyncOpenAI.__init__ = patched_init


def _remove_openai_ctor_patch() -> None:
    """Restore the original ``AsyncOpenAI.__init__``."""
    global _openai_original_init, _openai_patched_cls
    if _openai_patched_cls is not None and _openai_original_init is not None:
        _openai_patched_cls.__init__ = _openai_original_init
    _openai_original_init = None
    _openai_patched_cls = None


async def remove_http_injector() -> None:
    """Undo :func:`install_http_injector`.

    Only the constructor patch is removed. A default header already written into
    a client's ``_custom_headers`` is left alone: the plugin never replaces a
    value the user configured, and unwrapping cannot tell the two apart.
    """
    _remove_openai_ctor_patch()


def _sdk_clients() -> list[Any]:
    """Return every live OpenAI SDK client this plugin should tag requests for.

    The providers currently held by AstrBot are the reliable source; clients
    created and discarded elsewhere (the dashboard's model-list test) are covered
    by the ``AsyncOpenAI`` constructor patch instead.

    Returns:
        Candidate SDK client objects, ignoring anything unusable.
    """
    clients: list[Any] = []
    for provider in _wrap_targets:
        client = getattr(provider, "client", None)
        if client is not None and client not in clients:
            clients.append(client)
    return clients


def _install_on_client(client: Any) -> None:
    """Give one SDK client a default value for the session header.

    Writing ``_custom_headers`` covers **every** request that client will ever
    make, including paths this plugin never wraps -- which is exactly what the
    dashboard's model-list test needs, because it builds a throwaway provider
    instance and calls ``get_models()`` directly.

    The per-conversation value still wins wherever the plugin does wrap a call:
    ``create_wrapper`` puts the same canonical key into ``extra_headers``, and the
    SDK's case-sensitive merge lets the per-call value override this default.

    Args:
        client: An ``AsyncOpenAI``-alike exposing ``_custom_headers``.
    """
    if not TRANSPORT_INJECT:
        return
    headers = getattr(client, "_custom_headers", None)
    if not isinstance(headers, dict):
        return
    header_name = TARGET_HEADER or HEADER_NAME
    # Case-insensitive on purpose: adding the canonical spelling next to a
    # differently-cased entry the user already configured would produce two
    # headers on the wire instead of one, which is the exact failure this plugin
    # exists to avoid.
    for name in headers:
        if str(name).lower() == header_name.lower():
            return
    headers[header_name] = _contextless_key()


def _install_create(
    holder: Any,
    label: str,
    current: Any,
    originals: dict[str, Any],
    provider: Any = None,
) -> None:
    """Install the header injection wrapper on one ``create`` entry point.

    Args:
        holder: Object owning the callable (a completions/responses resource).
        label: Stable key used to remember the original callable.
        current: The currently bound callable.
        originals: Per-provider map of label to the original callable.
        provider: Provider instance owning the client.
    """
    original = originals.get(label) or current
    originals[label] = original

    @functools.wraps(original)
    async def create_wrapper(*args: Any, **kwargs: Any) -> Any:
        key = SESSION_KEY.get()
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

    setattr(holder, "create", create_wrapper)


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
    for chain, holder, current in _iter_creates(client):
        _install_create(
            holder,
            ".".join((*chain, "create")),
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
        if provider not in _wrap_targets:
            _wrap_targets.append(provider)
        # Tag the httpx layer too, so requests that never reach a wrapped method
        # (the dashboard's throwaway model-list provider) still carry the header.
        _install_on_client(client)
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
        global TRANSPORT_INJECT, CONTEXTLESS_SESSION_ID, RANDOM_CONTEXTLESS

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

            if "transport_inject" in config:
                TRANSPORT_INJECT = bool(config.get("transport_inject"))

            if "contextless_session_id" in config:
                fallback = config.get("contextless_session_id")
                CONTEXTLESS_SESSION_ID = (
                    fallback.strip() if isinstance(fallback, str) else ""
                )

            RANDOM_CONTEXTLESS = bool(config.get("random_contextless_value", False))

        logger.debug(
            "opencode session injection target: header=%s match_mode=%s keywords=%s "
            "transport_inject=%s contextless_session_id=%r random_contextless=%s",
            TARGET_HEADER,
            MATCH_MODE,
            HOST_KEYWORDS,
            TRANSPORT_INJECT,
            CONTEXTLESS_SESSION_ID,
            RANDOM_CONTEXTLESS,
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
        """Wrap existing providers and start tagging the HTTP transport.

        The transport patch is what covers the dashboard's model-list test, which
        builds its own throwaway provider instance instead of reusing one of the
        providers wrapped here.
        """
        try:
            for provider in self.context.get_all_providers():
                wrap_provider(provider)
            install_http_injector()
        except Exception:
            logger.debug("opencode session prewrap skipped", exc_info=True)

    async def terminate(self) -> None:
        """Restore the provider methods and transport this plugin replaced."""
        try:
            for provider in self.context.get_all_providers():
                unwrap_provider(provider)
            await remove_http_injector()
        except Exception:
            logger.debug("opencode session unwrap skipped", exc_info=True)
