"""Shared signal for "this request reached nobody".

The three request services each fan a request out to its approving managers.
A single manager's send failing is tolerable — the others still get notified —
but every send failing is not, because it leaves a Pending request that no one
has been told about.

`change_processor` treats a raised exception as "do not record this item as
processed", leaving it eligible to be dispatched again the next time anything
edits it. That is weaker than a true retry: the delta token is advanced before
the dispatch loop, so the failed item is never re-delivered by a later delta
query, and startup catch-up only re-drives items that have no manager assigned
— which a request failing at the notification step already does. Raising is
therefore mainly what keeps the failure loud and the item unmarked; swallowing
it would record the request as done with nobody told.
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
