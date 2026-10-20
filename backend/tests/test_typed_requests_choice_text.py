"""Department and location values written as SharePoint lookup objects (#386)."""

from app.repositories.postgres.typed_requests import choice_text

RAW = '{"@odata.type":"#Microsoft.Azure.Connectors.SharePoint.SPListExpandedReference","Id":5,"Value":"Hardware"}'


def test_a_lookup_object_as_json_text_gives_its_name():
    assert choice_text(RAW) == "Hardware"


def test_a_lookup_object_as_a_dict_gives_its_name():
    assert choice_text({"Id": 6, "Value": "Toronto Warden"}) == "Toronto Warden"


def test_a_plain_name_is_kept_and_trimmed():
    assert choice_text(" Hardware ") == "Hardware"


def test_blank_and_broken_values():
    assert choice_text(None) is None
    assert choice_text("") is None
    assert choice_text("{not json") == "{not json"           # not an object: kept as typed
