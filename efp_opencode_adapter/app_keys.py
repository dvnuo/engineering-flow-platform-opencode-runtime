from __future__ import annotations
from aiohttp import web

SETTINGS_KEY = web.AppKey('settings', object)
STATE_PATHS_KEY = web.AppKey('state_paths', object)
SESSION_STORE_KEY = web.AppKey('session_store', object)
TASK_STORE_KEY = web.AppKey('task_store', object)
CHATLOG_STORE_KEY = web.AppKey('chatlog_store', object)
USAGE_TRACKER_KEY = web.AppKey('usage_tracker', object)
EVENT_BUS_KEY = web.AppKey('event_bus', object)
TASK_BACKGROUND_TASKS_KEY = web.AppKey('task_background_tasks', object)
OPENCODE_CLIENT_KEY = web.AppKey('opencode_client', object)
PORTAL_METADATA_CLIENT_KEY = web.AppKey('portal_metadata_client', object)
RECOVERY_MANAGER_KEY = web.AppKey('recovery_manager', object)
EVENT_BRIDGE_KEY = web.AppKey('event_bridge', object)
EVENT_BRIDGE_TASK_KEY = web.AppKey('event_bridge_task', object)
FILE_SERVICE_KEY = web.AppKey('file_service', object)
ATTACHMENT_SERVICE_KEY = web.AppKey('attachment_service', object)
