"""Shared signal for "this request reached nobody".

The three request services each fan a request out to its approving managers.
A single manager's send failing is tolerable — the others still get notified —
but every send failing is not, because it leaves a Pending request that no one
has been told about.

`change_processor` treats a raised exception as "do not record this item as
processed", which is what makes SharePoint re-deliver it on the next delta
query. Swallowing a total failure would forfeit that retry, so the services
raise this instead.
"""


class NotificationsFailed(Exception):
    """Raised when every manager notification for a request failed.

    Carries the counts so the log line and any caller can report how wide the
    failure was without re-deriving it.
    """

    def __init__(self, request_label: str, attempted: int):
        """Build the exception with enough context to read the log line alone.

        Args:
            request_label: Human-readable request identifier, e.g.
                "Leave request #3402". Goes into the message so the log names
                the request without a second lookup.
            attempted: How many managers were on the list. Distinguishes
                "one supervisor, one failure" from "all four failed".
        """
        self.request_label = request_label      # kept for callers/tests, not just the message
        self.attempted = attempted              # ditto — assert on a number, not a string
        super().__init__(
            f"{request_label}: all {attempted} manager notification(s) failed — "
            "nobody was notified"
        )
