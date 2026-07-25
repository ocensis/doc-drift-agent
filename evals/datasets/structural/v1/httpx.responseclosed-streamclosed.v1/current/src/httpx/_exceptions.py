class StreamError(RuntimeError):
    """
    The base class for stream exceptions.

    The developer made an error in accessing the request stream in
    an invalid way.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)


class StreamConsumed(StreamError):
    """
    Attempted to read or stream content, but the content has already
    been streamed.
    """

    def __init__(self) -> None:
        message = (
            "Attempted to read or stream some content, but the content has "
            "already been streamed. For requests, this could be due to passing "
            "a generator as request content, and then receiving a redirect "
            "response or a secondary request as part of an authentication flow."
            "For responses, this could be due to attempting to stream the response "
            "content more than once."
        )
        super().__init__(message)


class StreamClosed(StreamError):
    """
    Attempted to read or stream response content, but the request has been
    closed.
    """

    def __init__(self) -> None:
        message = (
            "Attempted to read or stream content, but the stream has " "been closed."
        )
        super().__init__(message)


class ResponseNotRead(StreamError):
    """
    Attempted to access streaming response content, without having called `read()`.
    """

    def __init__(self) -> None:
        message = "Attempted to access streaming response content, without having called `read()`."
        super().__init__(message)


class RequestNotRead(StreamError):
    """
    Attempted to access streaming request content, without having called `read()`.
    """

    def __init__(self) -> None:
        message = "Attempted to access streaming request content, without having called `read()`."
        super().__init__(message)
