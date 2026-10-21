"""Background tasks tied to the app's lifetime.

Tracked tasks are cancelled at shutdown (``cleanup_task_background_tasks`` in
tasks_api) and can be found again by name, which is how one-per-session work
such as a title generation is kept from running twice at once.
"""

from __future__ import annotations

import asyncio

from aiohttp import web

from .app_keys import TASK_BACKGROUND_TASKS_KEY


def track_background_task(app: web.Application, task: asyncio.Task) -> None:
    task_set = app.get(TASK_BACKGROUND_TASKS_KEY)
    if isinstance(task_set, set):
        task_set.add(task)
        task.add_done_callback(task_set.discard)


def find_background_task(app: web.Application, name: str) -> asyncio.Task | None:
    """A tracked task with this name that has not finished, or None."""

    task_set = app.get(TASK_BACKGROUND_TASKS_KEY)
    if not isinstance(task_set, set):
        return None
    for task in task_set:
        if task.get_name() == name and not task.done():
            return task
    return None
