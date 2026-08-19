"""Wire-format contract policy for the buffered (non-streaming) reply path.

The problem
-----------

The buffered Anthropic path returns the upstream reply with its headers
copied wholesale::

    response_headers = dict(response.headers)
    response_headers.pop("content-encoding", None)
    response_headers.pop("content-length", None)
    ...
    return Response(content=..., status_code=..., headers=response_headers)

``content-type`` rides along untouched. When the upstream answers a
``stream``-less request with ``text/event-stream``, that body reaches a
caller that asked for JSON, as a ``200`` it cannot parse. Clients report
it as an empty or malformed response and the turn is lost — the reply is
*present and complete*, just wearing the wrong wire format.

The buffered-stream (CCR) path already refuses this shape, logging the
offending ``content-type`` and returning ``upstream_protocol_error``
(#2952). The plain non-streaming path never got the same treatment: an
unparseable body there was assumed to mean "no CCR handling", so it was
logged at DEBUG and passed through.

The contract
------------

A caller that did not set ``stream: true`` must never receive an
event-stream body. Headroom owns both ends of that boundary, so it can
enforce it rather than let the mismatch reach the client.

Behaviour matrix
----------------

============================  ==============  =========  ====================
Client asked for streaming?   Upstream C-T    Status     Result
============================  ==============  =========  ====================
yes                           any             any        untouched
no                            application/…   any        untouched
no                            text/event-…    != 200     untouched (real error)
no                            text/event-…    200        recover, else refuse
============================  ==============  =========  ====================

Recovery reuses ``StreamingMixin._parse_sse_to_response``, the same
reconstruction the streaming path already runs for usage accounting, so
this adds no new parsing surface. Recovering in place — rather than
returning early — keeps the rest of the buffered path (CCR, turn hooks,
security scan, usage accounting) operating on a normal reply.

Non-200 is deliberately excluded: an error status is already actionable
by the client, and passing it through unchanged preserves the upstream's
own error payload.

Public API
----------

* :func:`is_event_stream` — media-type test, parameter- and case-tolerant.
* :func:`should_recover_sse_reply` — the gate above, as one predicate.
* :func:`json_reply_headers` — upstream headers with the wire format corrected.

Constraints (per project memory)
--------------------------------

* pure: no I/O, no logging, no config — the handler owns those.
* no regexes: media-type parsing is a single ``split``.
* no silent fallbacks: the caller refuses loudly when recovery fails.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

SSE_MEDIA_TYPE = "text/event-stream"
JSON_MEDIA_TYPE = "application/json"

# Headers that describe the *streamed* framing of the upstream body. Once the
# body has been rebuilt as a single JSON document they describe nothing, and
# a stale value is worse than an absent one.
_FRAMING_HEADERS = ("content-type", "content-length", "content-encoding", "transfer-encoding")


def media_type(content_type: str | None) -> str:
    """Return the bare media type, lower-cased, with parameters dropped.

    ``"text/event-stream; charset=utf-8"`` and ``"Text/Event-Stream"`` both
    yield ``"text/event-stream"``. Returns ``""`` for a missing header.
    """
    if not content_type:
        return ""
    return content_type.split(";", 1)[0].strip().lower()


def is_event_stream(content_type: str | None) -> bool:
    """True when ``content_type`` denotes an SSE body."""
    return media_type(content_type) == SSE_MEDIA_TYPE


def should_recover_sse_reply(
    *,
    client_requested_stream: bool,
    status_code: int,
    content_type: str | None,
) -> bool:
    """True when a buffered reply violates the caller's non-streaming contract.

    See the behaviour matrix in the module docstring. The three negative
    arms are all deliberate: a streaming caller *wants* SSE, a JSON
    content-type is already correct, and a non-200 carries an upstream
    error the client should see verbatim.
    """
    if client_requested_stream:
        return False
    if status_code != 200:
        return False
    return is_event_stream(content_type)


def json_reply_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Copy ``headers`` with the streamed-framing headers replaced by JSON.

    Every other upstream header is preserved — ``request-id``, the
    ``anthropic-*`` family and the rate-limit headers are what clients use
    for correlation and backoff, and dropping them to fix a content-type
    would trade one defect for another.
    """
    corrected = {
        key: value for key, value in headers.items() if key.lower() not in _FRAMING_HEADERS
    }
    corrected["content-type"] = JSON_MEDIA_TYPE
    return corrected
