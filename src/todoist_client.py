"""Thin Todoist REST v2 client for ClearFeed action dispatch.

Wraps the Todoist REST API v2. Uses requests + Bearer auth.
Project IDs are resolved by name and cached per instance to minimize API calls.
"""

import logging
import sys
import os
from typing import Any

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import security_config
import config
from utils import retry

log = logging.getLogger(__name__)


class TodoistError(Exception):
    """Raised when the Todoist API returns an error or a network failure occurs."""


def to_api_priority(human_priority: int | str | None) -> int | None:
    """Convert human-scale priority (1=most urgent, 4=normal) to Todoist REST API scale.

    Todoist REST inverts the UI scale: API 4 = P1 (red/urgent), API 1 = P4 (normal).
    Transform: api_priority = 5 - human_priority.

    Args:
        human_priority: 1–4 on the human scale, or None/empty string to omit the field.

    Returns:
        Todoist REST API priority integer (1–4, inverted), or None to omit the field.
    """
    if human_priority is None or human_priority == "":
        return None

    if isinstance(human_priority, str):
        try:
            human_priority = int(human_priority)
        except ValueError:
            log.warning(
                "[todoist] non-numeric priority %r — omitting field", human_priority
            )
            return None

    if human_priority < 1:
        log.warning(
            "[todoist] priority %d below valid range — clamping to 1", human_priority
        )
        human_priority = 1
    elif human_priority > 4:
        log.warning(
            "[todoist] priority %d above valid range — clamping to 4", human_priority
        )
        human_priority = 4

    return 5 - human_priority


class TodoistClient:
    """REST v2 Todoist client. Instantiate once per action-dispatch run."""

    def __init__(self) -> None:
        self._token = security_config.TODOIST_API_TOKEN
        if not self._token:
            raise TodoistError(
                "TODOIST_API_TOKEN is empty — set it in Secrets/todoist_keys.py"
            )
        self._session = requests.Session()
        self._session.headers.update({"Authorization": f"Bearer {self._token}"})
        self._project_cache: dict[str, str] = {}  # name (lowercase) -> id

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @retry(
        attempts=config.LLM_RETRY_ATTEMPTS,
        base_delay=config.LLM_RETRY_BASE_DELAY,
        exceptions=(requests.RequestException,),
    )
    def _get(self, path: str) -> Any:
        """GET {TODOIST_BASE_URL}/{path} and return parsed JSON."""
        url = f"{config.TODOIST_BASE_URL}/{path}"
        resp = self._session.get(url, timeout=config.TODOIST_TIMEOUT_SECONDS)
        try:
            resp.raise_for_status()
        except requests.HTTPError as exc:
            raise TodoistError(
                f"Todoist GET {path} failed ({resp.status_code}): {resp.text}"
            ) from exc
        return resp.json()

    @staticmethod
    def _unwrap_list(data: Any) -> list:
        """Extract the item list from a v1 envelope or pass through a bare list.

        Todoist API v1 wraps paginated list responses in an object:
          {"results": [...], "next_cursor": null}
        Older / non-paginated calls returned a bare list.
        This handles both so the rest of the client code is format-agnostic.
        """
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            # Try common envelope keys in precedence order
            for key in ("results", "items", "projects", "sections", "tasks"):
                if key in data and isinstance(data[key], list):
                    return data[key]
        log.warning(
            "[todoist] unexpected response shape — expected list, got %s",
            type(data).__name__,
        )
        return []

    @retry(
        attempts=config.LLM_RETRY_ATTEMPTS,
        base_delay=config.LLM_RETRY_BASE_DELAY,
        exceptions=(requests.RequestException,),
    )
    def _post(self, path: str, payload: dict) -> Any:
        """POST {TODOIST_BASE_URL}/{path} with JSON body and return parsed JSON."""
        url = f"{config.TODOIST_BASE_URL}/{path}"
        resp = self._session.post(
            url, json=payload, timeout=config.TODOIST_TIMEOUT_SECONDS
        )
        try:
            resp.raise_for_status()
        except requests.HTTPError as exc:
            raise TodoistError(
                f"Todoist POST {path} failed ({resp.status_code}): {resp.text}"
            ) from exc
        return resp.json()

    # ------------------------------------------------------------------
    # Project resolution
    # ------------------------------------------------------------------

    def resolve_project_id(self, name: str) -> str:
        """Return the Todoist project id for the given name.

        Caches all project ids on first call. Auto-creates the project if it
        doesn't exist (a single POST /projects call).
        """
        key = name.strip().lower()
        if key in self._project_cache:
            return self._project_cache[key]

        projects = self._unwrap_list(self._get("projects"))
        for proj in projects:
            self._project_cache[proj["name"].lower()] = proj["id"]

        if key in self._project_cache:
            return self._project_cache[key]

        # Project not found — create it
        log.info("[todoist] project %r not found — creating it", name)
        new_proj = self._post("projects", {"name": name.strip()})
        project_id = new_proj["id"]
        self._project_cache[key] = project_id
        log.info("[todoist] created project %r (id=%s)", name, project_id)
        return project_id

    def resolve_section_id(self, project_id: str, section_name: str) -> str | None:
        """Return section id for section_name in project_id, or None if not found.

        Sections are not auto-created — a missing section is logged and silently
        ignored (task is created at project root).
        """
        sections = self._unwrap_list(self._get(f"sections?project_id={project_id}"))
        for sec in sections:
            if sec["name"].lower() == section_name.strip().lower():
                return sec["id"]
        log.warning(
            "[todoist] section %r not found in project %s — task placed at root",
            section_name,
            project_id,
        )
        return None

    # ------------------------------------------------------------------
    # Task creation
    # ------------------------------------------------------------------

    def create_task(
        self,
        project_id: str,
        content: str,
        description: str | None = None,
        priority: int | str | None = None,
        labels: list[str] | None = None,
        due_string: str | None = None,
        due_date: str | None = None,
        section_id: str | None = None,
    ) -> str:
        """Create a task in Todoist and return its id.

        Args:
            project_id: resolved project id.
            content: task title (plain text or minimal Markdown).
            description: task body (Markdown).
            priority: human-scale 1–4 (1=most urgent, 4=normal). Inverted to
                Todoist REST scale by to_api_priority before the request is sent.
                None or empty string omits the field (Todoist defaults to P4).
            labels: list of label strings to attach.
            due_string: natural-language due date, e.g. "tomorrow 9am".
            section_id: optional section; task created at project root when None.

        Returns:
            The new task's Todoist id string.
        """
        api_priority = to_api_priority(priority)
        payload: dict[str, Any] = {
            "project_id": project_id,
            "content": content,
        }
        if api_priority is not None:
            payload["priority"] = api_priority
        if description:
            payload["description"] = description
        if labels:
            payload["labels"] = labels
        if due_date:
            payload["due_date"] = due_date
        elif due_string:
            payload["due_string"] = due_string
        if section_id:
            payload["section_id"] = section_id

        task = self._post("tasks", payload)
        log.info("[todoist] task created: id=%s content=%r", task["id"], content[:80])
        return task["id"]

    def create_reminder(self, task_id: str, due_datetime: str) -> None:
        """Create an absolute-time reminder on an existing task.

        Args:
            task_id: Todoist task id.
            due_datetime: ISO 8601 datetime string, e.g. "2026-06-13T19:55:00".
        """
        self._post("reminders", {"task_id": task_id, "due": {"date": due_datetime}})
        log.info("[todoist] reminder created: task_id=%s at %s", task_id, due_datetime)
