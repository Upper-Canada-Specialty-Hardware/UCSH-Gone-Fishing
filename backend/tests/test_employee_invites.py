"""Add Employee gives the person access to the SharePoint site (#133).

Graph is mocked. What is covered: a person already in the tenant is only added
to the site members group; anyone else is invited as a guest first; someone
already in the group is not an error; any Graph failure is recorded and
returned, never raised; and nothing happens while invites are off.
"""

import asyncio
import itertools
import time

import httpx
import pytest

from app.config import settings
from app.services import employee_invites

_counter = itertools.count()


def _employee_id() -> str:
    """An employee id no earlier run has used (the test database keeps rows)."""
    return f"{next(_counter)}{time.time_ns()}"


def _http_error(status: int, text: str) -> httpx.HTTPStatusError:
    """A Graph-style failure as graph_client raises it."""
    request = httpx.Request("POST", "https://graph.microsoft.com/v1.0/x")
    response = httpx.Response(status, request=request, text=text)
    return httpx.HTTPStatusError(text, request=request, response=response)


@pytest.fixture
def graph(monkeypatch):
    """Fake Graph: .users maps email to object id; records every call.

    Returns:
        An object with .users, .calls, .group_error and .invite_error.
    """
    class Graph:
        users: dict = {}
        calls: list = []
        group_error = None
        invite_error = None
    g = Graph()
    g.users, g.calls = {}, []
    monkeypatch.setattr(settings, "INVITES_ENABLED", True)
    monkeypatch.setattr(settings, "SITE_MEMBERS_GROUP_ID", "group-1")
    monkeypatch.setattr(settings, "INVITE_REDIRECT_URL", "")
    monkeypatch.setattr(settings, "DASHBOARD_FRONTEND_URL", "https://ooo.example.com")

    async def fake_get(path, params=None):
        g.calls.append(("GET", path, params))
        email = params["$filter"].split("'")[1]
        return {"value": [{"id": g.users[email]}] if email in g.users else []}

    async def fake_post(path, json=None):
        g.calls.append(("POST", path, json))
        if path == "/invitations":
            if g.invite_error:
                raise g.invite_error
            return {"invitedUser": {"id": "guest-9"}}
        if g.group_error:
            raise g.group_error
        return {}

    async def not_on_site(email):
        return email in g.on_site

    g.on_site = set()
    monkeypatch.setattr(employee_invites.graph_client, "get", fake_get)
    monkeypatch.setattr(employee_invites.graph_client, "post", fake_post)
    monkeypatch.setattr(employee_invites, "_already_on_site", not_on_site)
    return g


def _invite(email="lee@ucsh.com"):
    employee_id = _employee_id()
    return employee_id, asyncio.run(employee_invites.invite_employee(employee_id, email, "Lee New"))


def test_someone_in_the_tenant_is_only_added_to_the_group(graph):
    graph.users["lee@ucsh.com"] = "user-1"
    _, result = _invite()

    assert result["status"] == "in_tenant" and result["group_added"]
    posts = [c for c in graph.calls if c[0] == "POST"]
    assert [p[1] for p in posts] == ["/groups/group-1/members/$ref"]
    assert posts[0][2]["@odata.id"].endswith("/directoryObjects/user-1")


def test_someone_outside_the_tenant_is_invited_then_added(graph):
    _, result = _invite("lee@gmail.com")

    assert result["status"] == "invited" and result["user_id"] == "guest-9"
    invite = next(c[2] for c in graph.calls if c[1] == "/invitations")
    assert invite["invitedUserEmailAddress"] == "lee@gmail.com"
    assert invite["inviteRedirectUrl"] == "https://ooo.example.com/#/request"
    assert invite["sendInvitationMessage"] is True
    assert any(c[1] == "/groups/group-1/members/$ref" for c in graph.calls)


def test_an_existing_member_is_not_an_error(graph):
    graph.users["lee@ucsh.com"] = "user-1"
    graph.group_error = _http_error(400, "One or more added object references already exist")
    _, result = _invite()
    assert result["status"] == "in_tenant" and result["detail"] == "Already a site member."


def test_a_graph_refusal_is_recorded_not_raised(graph):
    graph.invite_error = _http_error(403, '{"error": {"message": "Insufficient privileges"}}')
    employee_id, result = _invite("lee@gmail.com")

    assert result["status"] == "failed" and "Insufficient privileges" in result["detail"]
    listed = asyncio.run(employee_invites.list_invites())
    assert next(r for r in listed if r["employee_id"] == employee_id)["status"] == "failed"


def test_quotes_in_an_address_cannot_break_the_filter(graph):
    _invite("o'neil@ucsh.com")
    get = next(c for c in graph.calls if c[0] == "GET")
    assert "'o''neil@ucsh.com'" in get[2]["$filter"]


def test_nothing_is_sent_while_invites_are_off(graph, monkeypatch):
    monkeypatch.setattr(settings, "INVITES_ENABLED", False)
    _, result = _invite()
    assert result["status"] == "skipped" and graph.calls == []


def test_no_group_configured_fails_without_calling_graph(graph, monkeypatch):
    monkeypatch.setattr(settings, "SITE_MEMBERS_GROUP_ID", "")
    _, result = _invite()
    assert result["status"] == "failed" and "SITE_MEMBERS_GROUP_ID" in result["detail"]
    assert graph.calls == []


def test_a_resend_counts_attempts_on_the_same_row(graph):
    graph.users["lee@ucsh.com"] = "user-1"
    employee_id = _employee_id()
    for _ in range(2):
        asyncio.run(employee_invites.invite_employee(employee_id, "lee@ucsh.com", "Lee New"))
    rows = [r for r in asyncio.run(employee_invites.list_invites()) if r["employee_id"] == employee_id]
    assert len(rows) == 1 and rows[0]["attempts"] == 2


def test_someone_already_on_the_site_is_left_alone(graph):
    # Already in the site's user list, perhaps through an account with another
    # email: no lookup, no invite, so no duplicate guest is created.
    graph.on_site.add("lee@ucsh.com")
    _, result = _invite()
    assert result["status"] == "on_site" and graph.calls == []
