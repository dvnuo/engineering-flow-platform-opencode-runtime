"""Project the profile's image-analysis settings into the inspect-image CLI config.

Image analysis always runs on AI Platform through the inspect-image CLI
(GitHub Copilot's models no longer accept images). When the profile turned it
on (``llm.vision.enabled``) or the chat provider itself is AI Platform, and the
AI Platform account plus the deployment-managed chat and iB2B endpoints are
present, this writes a config file of inspect-image's own under the adapter
state directory and returns the variables the OpenCode child must carry:

- ``INSPECT_IMAGE_CONFIG``: the file; inspect-image prefers it over the
  ``EFP_CONFIG`` the adapter exports for the other CLIs.
- ``EFP_INSPECT_IMAGE_AI_PLATFORM_USERNAME`` / ``_PASSWORD`` / ``_USERCASE``:
  referenced from the file as ``${NAME}``, so the password never lands on disk.

inspect-image exchanges the short-lived iB2B JWT itself and keeps it in
``token_file``; it rewrites its config file on every refresh, which is why it
gets a file of its own rather than the shared mobile-auto config.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml

from .mobile_cli_config import _chmod_best_effort, _clean_secret, _clean_text
from .path_utils import path_exists
from .settings import Settings

INSPECT_IMAGE_CONFIG_ENV = "INSPECT_IMAGE_CONFIG"
USERNAME_ENV = "EFP_INSPECT_IMAGE_AI_PLATFORM_USERNAME"
PASSWORD_ENV = "EFP_INSPECT_IMAGE_AI_PLATFORM_PASSWORD"
USERCASE_ENV = "EFP_INSPECT_IMAGE_AI_PLATFORM_USERCASE"
MANAGED_ENV_VARS = (INSPECT_IMAGE_CONFIG_ENV, USERNAME_ENV, PASSWORD_ENV, USERCASE_ENV)
COPILOT_VISION_VIA_CHAT_ENV = "EFP_COPILOT_VISION_VIA_CHAT"
# Must stay aligned with the Portal catalog (app/contracts/llm_catalog.py
# AI_PLATFORM_MODELS) and the native runtime (efp_runtime/llm/models.py).
AI_PLATFORM_MODEL_IDS = ("gpt-5.4", "gpt-5.6-luna", "gpt-5.6-sol", "gpt-5.6-terra")
DEFAULT_AI_PLATFORM_MODEL = "gpt-5.4"
# The user-facing per-file upload cap, shared with the Portal and the native
# runtime (server.resolve_upload_client_max_size adds transport headroom on
# top of the same value).
MAX_UPLOAD_MB_ENV = "EFP_MAX_UPLOAD_MB"
DEFAULT_MAX_UPLOAD_MB = 25
DEFAULT_CHAT_URI = "/v1/api/v1/chat/completions"
DEFAULT_IB2B_URI = "/dsp/rest-sts/DSP_iB2B/iB2B_tokenTranslator_v2?_action=translate"
DEFAULT_TRUST_TOKEN_HEADER = "X-XXXX-E2E-Trust-Token"
DEFAULT_TRACKING_PREFIX = "EFP"
_TRUE_VALUES = {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class ImageAnalysisSettings:
    model: str
    chat_host: str
    chat_uri: str
    ib2b_host: str
    ib2b_uri: str
    username: str
    password: str
    usercase: str
    trust_token_header: str
    tracking_prefix: str


@dataclass(frozen=True)
class InspectImageConfigResult:
    configured: bool
    reason: str | None
    model: str | None
    path: str
    env: dict[str, str]
    warnings: list[str] = field(default_factory=list)

    def status(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "reason": self.reason,
            "model": self.model,
            "config_path": self.path if self.configured else None,
        }


def _flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return _clean_text(value).lower() in _TRUE_VALUES


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def normalize_provider_id(value: Any) -> str:
    text = _clean_text(value).lower().replace("-", "_").replace(" ", "_")
    if text == "ai_platform":
        return "ai_platform"
    if text in {"", "github_copilot", "github", "copilot"}:
        return "github_copilot"
    return text


def chat_provider_accepts_images(
    llm_config: Mapping[str, Any] | None,
    *,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """True when the configured chat provider can take image parts in a request.

    AI Platform chat/completions is multimodal. GitHub Copilot's models no
    longer accept images; ``EFP_COPILOT_VISION_VIA_CHAT=1`` flips that back on
    the day vision returns. Any other provider is treated like Copilot.
    """
    if normalize_provider_id(_mapping(llm_config).get("provider")) == "ai_platform":
        return True
    env = os.environ if environ is None else environ
    return _clean_text(env.get(COPILOT_VISION_VIA_CHAT_ENV, "")).lower() in _TRUE_VALUES


def max_image_bytes() -> int:
    """inspect-image's size limit: whatever the upload API accepts.

    Every image the handoff names came through the attachment API, which
    caps a file at EFP_MAX_UPLOAD_MB (25 MiB by default), so inspect-image
    must take the same size or an upload that succeeded is handed to a
    command that then refuses it. inspect-image's own default is 3 MiB.
    """
    raw = os.getenv(MAX_UPLOAD_MB_ENV, str(DEFAULT_MAX_UPLOAD_MB))
    try:
        mb = int(str(raw).strip())
    except (TypeError, ValueError):
        mb = DEFAULT_MAX_UPLOAD_MB
    if mb <= 0:
        mb = DEFAULT_MAX_UPLOAD_MB
    return mb * 1024 * 1024


def coerce_vision_model(model: Any) -> str:
    """A model AI Platform serves; anything else lands on the AI Platform default."""
    text = _clean_text(model)
    if "/" in text:
        text = text.split("/", 1)[1].strip()
    return text if text in AI_PLATFORM_MODEL_IDS else DEFAULT_AI_PLATFORM_MODEL


def image_analysis_requested(llm: Mapping[str, Any] | None) -> bool:
    """The member turned image analysis on, or chats on AI Platform already."""
    llm = _mapping(llm)
    return _flag(_mapping(llm.get("vision")).get("enabled")) or normalize_provider_id(llm.get("provider")) == "ai_platform"


def resolve_image_analysis_settings(
    runtime_config: Mapping[str, Any] | None,
) -> tuple[ImageAnalysisSettings | None, str | None]:
    """Settings for inspect-image, or (None, why not)."""
    llm = _mapping(_mapping(runtime_config).get("llm"))
    if not image_analysis_requested(llm):
        return None, "image analysis is off for this profile"
    ai_platform = _mapping(llm.get("ai_platform"))
    auth = _mapping(ai_platform.get("auth"))
    chat = _mapping(ai_platform.get("chat"))
    ib2b = _mapping(ai_platform.get("ib2b"))
    username = _clean_text(auth.get("username"))
    password = _clean_secret(auth.get("password"))
    usercase = _clean_text(auth.get("usercase"))
    if not (username and password and usercase):
        return None, "AI Platform username, password, and usercase are required"
    chat_host = _clean_text(chat.get("host")).rstrip("/")
    if not chat_host:
        return None, "AI Platform chat endpoint is not configured"
    ib2b_host = _clean_text(ib2b.get("host")).rstrip("/")
    if not ib2b_host:
        return None, "AI Platform iB2B endpoint is not configured"
    vision = _mapping(llm.get("vision"))
    model = _clean_text(vision.get("model"))
    if not model and normalize_provider_id(llm.get("provider")) == "ai_platform":
        model = _clean_text(llm.get("model"))
    return (
        ImageAnalysisSettings(
            model=coerce_vision_model(model),
            chat_host=chat_host,
            chat_uri=_clean_text(chat.get("uri")) or DEFAULT_CHAT_URI,
            ib2b_host=ib2b_host,
            ib2b_uri=_clean_text(ib2b.get("uri")) or DEFAULT_IB2B_URI,
            username=username,
            password=password,
            usercase=usercase,
            trust_token_header=_clean_text(auth.get("trust_token_header")) or DEFAULT_TRUST_TOKEN_HEADER,
            tracking_prefix=_clean_text(auth.get("tracking_prefix")) or DEFAULT_TRACKING_PREFIX,
        ),
        None,
    )


def build_inspect_image_config(settings: ImageAnalysisSettings, *, token_file: Path) -> dict[str, Any]:
    """The YAML inspect-image reads; credentials are environment references."""
    return {
        "version": 1,
        "inspect_image": {
            "provider": "ai_platform",
            "defaults": {"model": settings.model},
            "limits": {"max_image_bytes": max_image_bytes()},
        },
        "ai_platform": {
            "chat": {"host": settings.chat_host, "uri": settings.chat_uri},
            "ib2b": {"host": settings.ib2b_host, "uri": settings.ib2b_uri},
            "auth": {
                "username": "${" + USERNAME_ENV + "}",
                "password": "${" + PASSWORD_ENV + "}",
                "usercase": "${" + USERCASE_ENV + "}",
                "trust_token_header": settings.trust_token_header,
                "tracking_prefix": settings.tracking_prefix,
                "token_file": token_file.as_posix(),
            },
        },
    }


def inspect_image_config_path(settings: Settings) -> Path:
    return settings.adapter_state_dir / "inspect-image" / "config.yaml"


def inspect_image_token_path(settings: Settings) -> Path:
    return settings.adapter_state_dir / "inspect-image" / "ai_platform_token"


def write_inspect_image_config(settings: Settings, runtime_config: Mapping[str, Any] | None) -> InspectImageConfigResult:
    """Write (or remove) inspect-image's config and return what the child env needs."""
    warnings: list[str] = []
    path = inspect_image_config_path(settings)
    token_path = inspect_image_token_path(settings)
    resolved, reason = resolve_image_analysis_settings(runtime_config)
    if resolved is None:
        for stale in (path, token_path):
            if path_exists(stale):
                try:
                    stale.unlink()
                except OSError:
                    warnings.append(f"unable to remove stale inspect-image file {stale.name}")
        return InspectImageConfigResult(configured=False, reason=reason, model=None, path=str(path), env={}, warnings=warnings)

    payload = build_inspect_image_config(resolved, token_file=token_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _chmod_best_effort(path.parent, 0o700, warnings, "unable to set inspect-image config directory permissions")
    except OSError as exc:
        raise OSError("unable to create inspect-image config directory") from exc
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp_path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8")
    _chmod_best_effort(tmp_path, 0o600, warnings, "unable to set inspect-image config file permissions")
    tmp_path.replace(path)
    _chmod_best_effort(path, 0o600, warnings, "unable to set inspect-image config file permissions")
    # inspect-image keeps the short-lived JWT in token_file; start from none so
    # a changed account never reuses a token issued for the previous one.
    if path_exists(token_path):
        try:
            token_path.unlink()
        except OSError:
            warnings.append("unable to remove the previous inspect-image token")
    return InspectImageConfigResult(
        configured=True,
        reason=None,
        model=resolved.model,
        path=str(path),
        env={
            INSPECT_IMAGE_CONFIG_ENV: str(path),
            USERNAME_ENV: resolved.username,
            PASSWORD_ENV: resolved.password,
            USERCASE_ENV: resolved.usercase,
        },
        warnings=warnings,
    )
