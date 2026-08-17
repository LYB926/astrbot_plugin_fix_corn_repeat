"""Runtime guard for AstrBot future-task repeat sends."""

from __future__ import annotations

import inspect
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import wraps
from importlib.metadata import PackageNotFoundError, version as distribution_version
from typing import Any, Protocol

PLUGIN_ID = "astrbot_plugin_fix_corn_repeat"
EVENT_STATE_KEY = "_astrbot_plugin_fix_corn_repeat_send_state"
STATE_READY = "ready"
STATE_IN_FLIGHT = "in_flight"
STATE_DELIVERED = "delivered"
STATE_TERMINAL = "terminal"
SUCCESS_PREFIX = "Message sent to session "

_MARKER_ATTR = "__astrbot_fix_corn_repeat_patch__"
_ORIGINAL_ATTR = "__astrbot_fix_corn_repeat_original__"
_OWNER_ATTR = "__astrbot_fix_corn_repeat_owner__"
_STATE_ATTR = "__astrbot_fix_corn_repeat_state__"
_CLASS_STATE_ATTR = "__astrbot_fix_corn_repeat_active_state__"
_EXPECTED_PARAMETER_NAMES = ("self", "context", "kwargs")
_SUPPORTED_MIN = (4, 26, 6)
_SUPPORTED_MAX_EXCLUSIVE = (4, 27, 0)
_VERSION_PATTERN = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:\D|$)")


class LoggerLike(Protocol):
    """Subset of the AstrBot logger used by the guard."""

    def info(self, message: str) -> Any: ...

    def warning(self, message: str) -> Any: ...


class IncompatibleAstrBotError(RuntimeError):
    """Raised when the runtime does not satisfy the guarded contract."""


ToolCall = Callable[..., Awaitable[Any]]


@dataclass
class _PatchState:
    active: bool = True


@dataclass
class _PatchRecord:
    target: type[Any]
    previous: ToolCall
    wrapper: ToolCall


class CronRepeatGuardPatch:
    """Make a successful active-agent Cron send terminal and at-most-once."""

    def __init__(
        self,
        tool_manager: Any,
        logger: LoggerLike,
        *,
        target: type[Any] | None = None,
        cron_event_type: type[Any] | None = None,
        astrbot_version: str | None = None,
    ) -> None:
        self.tool_manager = tool_manager
        self.logger = logger
        self._target = target
        self._cron_event_type = cron_event_type
        self._astrbot_version = astrbot_version
        self._state = _PatchState(active=False)
        self._record: _PatchRecord | None = None

    @property
    def installed(self) -> bool:
        """Whether this instance currently owns an active wrapper."""

        return self._record is not None and self._state.active

    def install(self) -> bool:
        """Validate the runtime and install the repeat-send guard."""

        if self.installed:
            return True

        target, cron_event_type, astrbot_version = self._resolve_runtime()
        self._validate_version(astrbot_version)
        self._validate_event_type(cron_event_type)

        current = getattr(target, "call", None)
        if not callable(current):
            raise IncompatibleAstrBotError(
                f"{target.__name__}.call is missing or is not callable."
            )

        previous, stale_states = self._unwrap_own_wrappers(current)
        self._validate_signature(target, previous)
        self._probe_cached_tool(target, current)

        old_class_state = getattr(target, _CLASS_STATE_ATTR, None)
        new_state = _PatchState()
        wrapper = self._build_wrapper(previous, cron_event_type, new_state)

        try:
            target.call = wrapper
            self._probe_cached_tool(target, wrapper)
        except Exception as exc:
            if getattr(target, "call", None) is wrapper:
                target.call = current
            if isinstance(exc, IncompatibleAstrBotError):
                raise
            raise IncompatibleAstrBotError(
                f"could not install {target.__name__}.call wrapper: {exc}"
            ) from exc

        for stale_state in stale_states:
            stale_state.active = False
        if old_class_state is not None and hasattr(old_class_state, "active"):
            old_class_state.active = False

        setattr(target, _CLASS_STATE_ATTR, new_state)
        self._state = new_state
        self._record = _PatchRecord(
            target=target,
            previous=previous,
            wrapper=wrapper,
        )
        self.logger.info(
            "Future-task repeat-send guard installed: "
            f"AstrBot={astrbot_version}, target={target.__name__}.call."
        )
        return True

    def uninstall(self) -> None:
        """Deactivate this guard and restore the method if still owned."""

        record = self._record
        was_active = self._state.active
        self._state.active = False

        if record is None:
            return

        current = getattr(record.target, "call", None)
        if current is record.wrapper:
            record.target.call = record.previous
        elif was_active:
            self.logger.warning(
                "Could not restore "
                f"{record.target.__name__}.call because another component "
                "replaced it after this plugin loaded. The old guard has "
                "been deactivated."
            )

        if getattr(record.target, _CLASS_STATE_ATTR, None) is self._state:
            delattr(record.target, _CLASS_STATE_ATTR)
        self._record = None

    def _resolve_runtime(self) -> tuple[type[Any], type[Any], str]:
        target = self._target
        cron_event_type = self._cron_event_type
        astrbot_version = self._astrbot_version

        try:
            if target is None:
                from astrbot.core.tools.message_tools import SendMessageToUserTool

                target = SendMessageToUserTool
            if cron_event_type is None:
                from astrbot.core.cron.events import CronMessageEvent

                cron_event_type = CronMessageEvent
            if astrbot_version is None:
                astrbot_version = distribution_version("astrbot")
        except (ImportError, AttributeError, PackageNotFoundError) as exc:
            raise IncompatibleAstrBotError(
                "required AstrBot Cron/send-tool APIs could not be imported."
            ) from exc

        if not isinstance(target, type) or not isinstance(cron_event_type, type):
            raise IncompatibleAstrBotError(
                "resolved AstrBot send tool or Cron event is not a class."
            )
        return target, cron_event_type, astrbot_version

    @staticmethod
    def _validate_version(raw_version: str) -> None:
        match = _VERSION_PATTERN.match(raw_version)
        if match is None:
            raise IncompatibleAstrBotError(
                f"could not parse AstrBot version {raw_version!r}."
            )

        parsed = tuple(int(part) for part in match.groups())
        if not (_SUPPORTED_MIN <= parsed < _SUPPORTED_MAX_EXCLUSIVE):
            raise IncompatibleAstrBotError(
                "supported AstrBot range is >=4.26.6,<4.27; "
                f"detected {raw_version}."
            )

    @staticmethod
    def _validate_event_type(cron_event_type: type[Any]) -> None:
        if not callable(getattr(cron_event_type, "get_extra", None)):
            raise IncompatibleAstrBotError(
                f"{cron_event_type.__name__}.get_extra is unavailable."
            )
        if not callable(getattr(cron_event_type, "set_extra", None)):
            raise IncompatibleAstrBotError(
                f"{cron_event_type.__name__}.set_extra is unavailable."
            )

    @staticmethod
    def _unwrap_own_wrappers(
        method: ToolCall,
    ) -> tuple[ToolCall, list[Any]]:
        stale_states: list[Any] = []
        current = method

        while (
            getattr(current, _MARKER_ATTR, None) == PLUGIN_ID
            and getattr(current, _OWNER_ATTR, None) is current
        ):
            state = getattr(current, _STATE_ATTR, None)
            original = getattr(current, _ORIGINAL_ATTR, None)
            if state is not None and hasattr(state, "active"):
                stale_states.append(state)
            if not callable(original):
                raise IncompatibleAstrBotError(
                    "detected a malformed stale repeat-send wrapper."
                )
            current = original

        return current, stale_states

    @staticmethod
    def _validate_signature(target: type[Any], method: ToolCall) -> None:
        if not inspect.iscoroutinefunction(method):
            raise IncompatibleAstrBotError(
                f"{target.__name__}.call must be an async function."
            )

        try:
            parameters = tuple(
                inspect.signature(method, follow_wrapped=False).parameters.values()
            )
        except (TypeError, ValueError) as exc:
            raise IncompatibleAstrBotError(
                f"could not inspect {target.__name__}.call."
            ) from exc

        names = tuple(parameter.name for parameter in parameters)
        kinds = tuple(parameter.kind for parameter in parameters)
        expected_kinds = (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.VAR_KEYWORD,
        )
        if names != _EXPECTED_PARAMETER_NAMES or kinds != expected_kinds:
            raise IncompatibleAstrBotError(
                "expected "
                f"{target.__name__}.call(self, context, **kwargs), got "
                f"parameters {names!r}."
            )

    def _probe_cached_tool(
        self,
        target: type[Any],
        expected_method: ToolCall,
    ) -> None:
        getter = getattr(self.tool_manager, "get_builtin_tool", None)
        if not callable(getter):
            raise IncompatibleAstrBotError(
                "LLM tool manager has no callable get_builtin_tool method."
            )

        try:
            tool = getter(target)
            second_lookup = getter(target)
        except Exception as exc:
            raise IncompatibleAstrBotError(
                f"could not resolve cached {target.__name__}: {exc}"
            ) from exc

        if tool is not second_lookup:
            raise IncompatibleAstrBotError(
                "get_builtin_tool does not return a stable cached instance."
            )
        if not isinstance(tool, target):
            raise IncompatibleAstrBotError(
                f"cached send tool is not an instance of {target.__name__}."
            )
        if getattr(tool, "handler", None) is not None:
            raise IncompatibleAstrBotError(
                "cached send tool has a handler that would bypass call()."
            )

        try:
            instance_attributes = vars(tool)
        except TypeError as exc:
            raise IncompatibleAstrBotError(
                "cached send tool does not expose instance attributes."
            ) from exc
        if "call" in instance_attributes:
            raise IncompatibleAstrBotError(
                "cached send tool has an instance-level call override."
            )

        effective_call = getattr(tool, "call", None)
        if getattr(effective_call, "__self__", None) is not tool:
            raise IncompatibleAstrBotError(
                "cached send tool call is not bound to the cached instance."
            )
        if getattr(effective_call, "__func__", None) is not expected_method:
            raise IncompatibleAstrBotError(
                "cached send tool does not resolve the expected class method."
            )

    def _build_wrapper(
        self,
        previous: ToolCall,
        cron_event_type: type[Any],
        state: _PatchState,
    ) -> ToolCall:
        patch = self
        logger = self.logger

        @wraps(previous)
        async def guarded_call(self: Any, context: Any, **kwargs: Any) -> Any:
            if not state.active:
                return await previous(self, context, **kwargs)

            event = patch._get_event(context)
            if not patch._is_active_agent_cron(event, cron_event_type):
                return await previous(self, context, **kwargs)

            event_state = event.get_extra(EVENT_STATE_KEY, STATE_READY)
            if event_state != STATE_READY:
                logger.warning(
                    "Suppressed a repeated future-task send: "
                    f"{patch._event_label(event)}, state={event_state!r}."
                )
                return None

            # No await occurs between the state check and this assignment, so
            # concurrent calls on the same asyncio event cannot both enter the
            # underlying sender.
            event.set_extra(EVENT_STATE_KEY, STATE_IN_FLIGHT)
            try:
                result = await previous(self, context, **kwargs)
            except BaseException as exc:
                logger.warning(
                    "Future-task send ended with unknown delivery status; "
                    "further sends in this execution will be suppressed: "
                    f"{patch._event_label(event)}, error={type(exc).__name__}."
                )
                raise

            if isinstance(result, str) and result.startswith("error:"):
                event.set_extra(EVENT_STATE_KEY, STATE_READY)
                return result

            if result is None or (
                isinstance(result, str) and result.startswith(SUCCESS_PREFIX)
            ):
                event.set_extra(EVENT_STATE_KEY, STATE_DELIVERED)
                logger.info(
                    "Future-task send completed; terminating this Agent run: "
                    f"{patch._event_label(event)}."
                )
                return None

            event.set_extra(EVENT_STATE_KEY, STATE_TERMINAL)
            logger.warning(
                "Future-task send returned an unrecognized non-error result; "
                "treating it as terminal to prevent repeat delivery: "
                f"{patch._event_label(event)}, "
                f"result_type={type(result).__name__}."
            )
            return None

        setattr(guarded_call, _MARKER_ATTR, PLUGIN_ID)
        setattr(guarded_call, _ORIGINAL_ATTR, previous)
        # functools.wraps copies the wrapped function's __dict__. A self-owner
        # reference lets hot reload distinguish this exact wrapper from a
        # third-party wrapper that merely inherited our marker attributes.
        setattr(guarded_call, _OWNER_ATTR, guarded_call)
        setattr(guarded_call, _STATE_ATTR, state)
        return guarded_call

    @staticmethod
    def _get_event(context: Any) -> Any | None:
        agent_context = getattr(context, "context", None)
        return getattr(agent_context, "event", None)

    @staticmethod
    def _is_active_agent_cron(event: Any, cron_event_type: type[Any]) -> bool:
        if not isinstance(event, cron_event_type):
            return False
        cron_job = event.get_extra("cron_job")
        return isinstance(cron_job, dict) and cron_job.get("type") == "active_agent"

    @staticmethod
    def _event_label(event: Any) -> str:
        cron_job = event.get_extra("cron_job", {})
        if not isinstance(cron_job, dict):
            return "job_id=unknown"
        return (
            f"job_id={cron_job.get('id', 'unknown')!r}, "
            f"run_started_at={cron_job.get('run_started_at', 'unknown')!r}"
        )


__all__ = [
    "CronRepeatGuardPatch",
    "EVENT_STATE_KEY",
    "IncompatibleAstrBotError",
    "PLUGIN_ID",
    "STATE_DELIVERED",
    "STATE_IN_FLIGHT",
    "STATE_READY",
    "STATE_TERMINAL",
    "SUCCESS_PREFIX",
]
