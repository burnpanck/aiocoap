# This file is part of the Python aiocoap library project.
#
# Copyright (c) 2012-2014 Maciej Wasilak <http://sixpinetrees.blogspot.com/>,
#               2013-2014 Christian Amsüss <c.amsuess@energyharvesting.at>
#
# aiocoap is free software, this file is published under the MIT license as
# described in the accompanying LICENSE file.

import asyncio
from collections import namedtuple
import functools

from . import error
from .numbers import INTERNAL_SERVER_ERROR
from .util.asyncio import py38args

class PlumbingRequest:
    """Low-level meeting point between a request and a any responses that come
    back on it.

    A single request message is placed in the PlumbingRequest at creation time.
    Any responses, as well as any exception happening in the course of
    processing, are passed back to the requester along the PlumbingRequest. A
    response can carry an indication of whether it is final; an exception
    always is.

    This object is used both on the client side (where the Context on behalf of
    the application creates a PlumbingRequest and passes it to the network
    transports that send the request and fill in any responses) and on the
    server side (where the Context creates one for an incoming request and
    eventually lets the server implementation populate it with responses).

    This currently follows a callback dispatch style. (It may be developed into
    something where only awaiting a response drives the proces, though).

    Currently, the requester sets up the object, connects callbacks, and then
    passes the PlumbingRequest on to whatever creates the response.

    The creator of responses is notified by the PlumbingRequest of a loss of
    interest in a response when there are no more callback handlers registered
    by registering an on_interest_end callback. As the response callbacks need
    to be already in place when the PlumbingRequest is passed on to the
    responder, the absence event callbacks is signalled by callign the callback
    immediately on registration.

    To accurately model "loss of interest", it is important to use the
    two-phase setup of first registering actual callbacks and then producing
    events and/or placing on_interest_end callbacks; this is not clearly
    expressed in type or state yet. (One possibility would be for the
    PlumbingRequest to carry a preparation boolean, and which prohibits event
    sending during preparation and is_interest=True callback creation
    afterwards)."""

    Event = namedtuple("Event", ("message", "exception", "is_last"))

    # called by the initiator of the request

    def __init__(self, request, log):
        self.request = request
        self.log = log

        self._event_callbacks = []
        """list[(callback, is_interest)], or None during event processing, or
        False when there were no more event callbacks and an the
        on_interest_end callbacks have already been called"""

    def __repr__(self):
        return '<%s at %#x around %r with %r callbacks>'%(type(self).__name__, id(self), self.request, len(self._event_callbacks) if self._event_callbacks else self._event_callbacks)

    def _any_interest(self):
        return any(is_interest for (cb, is_interest) in self._event_callbacks)

    def poke(self):
        """Ask the responder for a life sign. It is up to the responder to
        ignore this (eg. because the responder is the library/application and
        can't be just gone), to issue a generic transport-dependent 'ping' to
        see whether the connection is still alive, or to retransmit the request
        if it is an observation over an unreliable channel.

        In any case, no status is reported directly to the poke, but if
        whatever the responder does fails, it will send an appropriate error
        message as a response."""
        raise NotImplementedError()

    def on_event(self, callback, is_interest=True):
        """Call callback on any event. The callback must return True to be
        called again after an event. Callbacks must not produce new events or
        deregister unrelated event handlers.

        If is_interest=False, the callback will not be counted toward the
        active callbacks, and will receive a (None, None, is_last=True) event
        eventually.

        To unregister the handler, call the returned closure; this can trigger
        on_interest_end callbacks.
        """
        self._event_callbacks.append((callback, is_interest))
        return functools.partial(self._unregister_on_event, callback)

    def _unregister_on_event(self, callback):
        if self._event_callbacks is False:
            # They wouldn't be called any more so they're already dropped.a
            # It's OK that the caller cleans up after itself: Sure it could
            # register an on_interest_end, but that's really not warranted if
            # all it wants to know is whether it'll have to execute cleanup
            # when it's shutting down or not.
            return

        self._event_callbacks = [(cb, i) for (cb, i) in self._event_callbacks if callback is not cb]
        if not self._any_interest():
            self._end()

    def on_interest_end(self, callback):
        """Register a callback that will be called exactly once -- either right
        now if there is not even a current indicated interest, or at a last
        event, or when no more interests are present"""

        if self._event_callbacks is False:
            # Happens, for example, when a proxy receives multiple requests on a single token
            self.log.warning("on_interest_end callback %r added after %r has already ended", callback, self)
            callback()
            return

        if self._any_interest():
            self._event_callbacks.append((
                lambda e: ((callback(), False) if e.is_last else (None, True))[1],
                False
                ))
        else:
            callback()

    def _end(self):
        cbs = self._event_callbacks
        self._event_callbacks = False
        tombstone = self.Event(None, None, True)
        [cb(tombstone) for (cb, _) in cbs]

    # called by the responding side

    def _add_event(self, event):
        if self._event_callbacks is False:
            # Happens, for example, when a proxy receives multiple requests on a single token
            self.log.warning("Response %r added after %r has already ended", event, self)
            return

        cbs = self._event_callbacks
        # Force an error when during event handling an event is generated
        self._event_callbacks = None
        surviving = [(cb, is_interest) for (cb, is_interest) in cbs if cb(event)]

        self._event_callbacks = surviving

        if not self._any_interest():
            self._end()

    def add_response(self, response, is_last=False):
        self._add_event(self.Event(response, None, is_last))

    def add_exception(self, exception):
        self._add_event(self.Event(None, exception, True))

def run_driving_plumbing_request(plumbing_request, coroutine, log, name=None):
    """Create a task from a coroutine where the end of the coroutine produces a
    terminal event on the plumbing request, and lack of interest in the
    plumbing request cancels the task.

    The coroutine will typically produce output into the plumbing request; that
    connection is set up by the caller like as in
    ``run_driving_plumbing_request(pr, render_to(pr))``.

    The create task is not returned, as the only sensible operation on it would
    be cancellation and that's already set up from the plumbing request.
    """
    # FIXME This does not try to render exceptions that are passed around into
    # messages; should it? (Or should this really be two wrappers where one
    # takes a tasks's final exception to finish the PlumbingRequest and the
    # other turns exceptions into messages?)

    from .message import Message

    async def wrapped():
        try:
            await coroutine
        except error.RenderableError as e:
            # the repr() here is quite imporant for garbage collection
            log.info("Render request raised a renderable error (%s), responding accordingly.", repr(e))
            try:
                msg = e.to_message()
                if msg is None:
                    # This deserves a separate check because the ABC checks
                    # that should ensure that the default to_message method is
                    # never used in concrete classes fails due to the metaclass
                    # conflict between ABC and Exceptions
                    raise ValueError("Exception to_message failed to produce a message on %r" % e)
            except Exception as e2:
                log.error("Rendering the renderable exception failed: %r", e2, exc_info=e2)
                msg = Message(code=INTERNAL_SERVER_ERROR)
            plumbing_request.add_response(msg, is_last=True)
        except asyncio.CancelledError:
            # This currently only happens in the OSCORE plugtest server's
            # custom code; in general, this would indicate that the network
            # peer has indicated loss of interest (by closing the TCP
            # connection or sending an ICMP unreachable), in which case
            # rendering will be cancelled too (but currently is not --
            # currently, only cancellation_future is set)
            #
            # (Technically, there is no reason to keep this clause in here as
            # the canceled task itself will not complain if the CancelledError
            # raises out of it, and CancelledError is only BaseException and
            # would thus not be caught by the catch-all; this is more for
            # awareness).
            pass
        except Exception as e:
            plumbing_request.add_response(Message(code=INTERNAL_SERVER_ERROR), is_last=True)
            log.error("An exception occurred while rendering a resource: %r", e, exc_info=e)

    asyncio.create_task(
            wrapped(),
            **py38args(name=name),
            )
