"""Transient Todoist failures (429/5xx) are retried; permanent ones are not."""

from unittest.mock import patch

import pytest

URL = "https://api.todoist.com/api/v1/tasks"


@pytest.fixture
def client():
    from todoist_client import TodoistClient

    with patch("security_config.TODOIST_API_TOKEN", "tok"), patch("time.sleep"):
        yield TodoistClient()


def test_503_then_success_is_retried(requests_mock, client):
    requests_mock.post(
        URL, [{"status_code": 503, "text": ""}, {"status_code": 200, "json": {"id": "t1"}}]
    )
    assert client._post("tasks", {"content": "x"}) == {"id": "t1"}
    assert requests_mock.call_count == 2


def test_persistent_503_raises_transient_error_after_all_attempts(requests_mock, client):
    import config
    from todoist_client import TodoistError, TodoistTransientError

    requests_mock.post(URL, status_code=503, text="")
    with pytest.raises(TodoistTransientError) as excinfo:
        client._post("tasks", {"content": "x"})
    assert isinstance(excinfo.value, TodoistError)  # existing handlers still catch it
    assert requests_mock.call_count == config.LLM_RETRY_ATTEMPTS


def test_429_is_retried(requests_mock, client):
    requests_mock.post(
        URL, [{"status_code": 429, "text": ""}, {"status_code": 200, "json": {"id": "t2"}}]
    )
    assert client._post("tasks", {"content": "x"}) == {"id": "t2"}


def test_400_is_not_retried(requests_mock, client):
    from todoist_client import TodoistError, TodoistTransientError

    requests_mock.post(URL, status_code=400, text="bad")
    with pytest.raises(TodoistError) as excinfo:
        client._post("tasks", {"content": "x"})
    assert not isinstance(excinfo.value, TodoistTransientError)
    assert requests_mock.call_count == 1


def test_get_503_is_retried(requests_mock, client):
    requests_mock.get(
        "https://api.todoist.com/api/v1/projects",
        [{"status_code": 503, "text": ""}, {"status_code": 200, "json": []}],
    )
    assert client._get("projects") == []
