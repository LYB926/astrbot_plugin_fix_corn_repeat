from __future__ import annotations

import asyncio
import unittest
from functools import wraps
from types import MethodType, SimpleNamespace
from typing import Any

from patching import (
    EVENT_STATE_KEY,
    PLUGIN_ID,
    STATE_DELIVERED,
    STATE_IN_FLIGHT,
    STATE_READY,
    STATE_TERMINAL,
    CronRepeatGuardPatch,
    IncompatibleAstrBotError,
)


class RecordingLogger:
    def __init__(self) -> None:
        self.info_messages: list[str] = []
        self.warning_messages: list[str] = []

    def info(self, message: str) -> None:
        self.info_messages.append(message)

    def warning(self, message: str) -> None:
        self.warning_messages.append(message)


class FakeEvent:
    def __init__(self, extras: dict[str, Any] | None = None) -> None:
        self.extras = dict(extras or {})

    def get_extra(self, key: str, default: Any = None) -> Any:
        return self.extras.get(key, default)

    def set_extra(self, key: str, value: Any) -> None:
        self.extras[key] = value


class FakeCronEvent(FakeEvent):
    pass


class FakeToolManager:
    def __init__(self, tool: Any) -> None:
        self.tool = tool

    def get_builtin_tool(self, target: type[Any]) -> Any:
        assert isinstance(self.tool, target)
        return self.tool


def make_context(event: FakeEvent) -> SimpleNamespace:
    return SimpleNamespace(context=SimpleNamespace(event=event))


def make_active_event(job_id: str = "job-1") -> FakeCronEvent:
    return FakeCronEvent(
        {
            "cron_job": {
                "id": job_id,
                "type": "active_agent",
                "run_started_at": "2026-08-17T12:00:00+08:00",
            }
        }
    )


def make_tool_type(outcomes: list[Any] | None = None) -> type[Any]:
    configured_outcomes = list(outcomes or ["Message sent to session cron:group:1"])

    class FakeSendTool:
        handler = None

        def __init__(self) -> None:
            self.call_count = 0
            self.outcomes = list(configured_outcomes)

        async def call(self, context: Any, **kwargs: Any) -> Any:
            self.call_count += 1
            outcome = self.outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

    return FakeSendTool


def make_patch(
    target: type[Any],
    tool: Any,
    logger: RecordingLogger | None = None,
    *,
    manager: Any | None = None,
    version: str = "4.26.6",
) -> tuple[CronRepeatGuardPatch, RecordingLogger]:
    recording_logger = logger or RecordingLogger()
    patch = CronRepeatGuardPatch(
        manager or FakeToolManager(tool),
        recording_logger,
        target=target,
        cron_event_type=FakeCronEvent,
        astrbot_version=version,
    )
    return patch, recording_logger


class CronRepeatGuardPatchTests(unittest.TestCase):
    def test_first_success_is_terminal_and_later_calls_are_suppressed(self) -> None:
        target = make_tool_type(
            [
                "Message sent to session cron:group:1",
                "Message sent to session cron:group:1",
            ]
        )
        tool = target()
        patch, logger = make_patch(target, tool)
        event = make_active_event()
        context = make_context(event)

        try:
            self.assertTrue(patch.install())
            self.assertIsNone(
                asyncio.run(tool.call(context, messages=[{"type": "plain"}]))
            )
            self.assertEqual(event.get_extra(EVENT_STATE_KEY), STATE_DELIVERED)

            self.assertIsNone(
                asyncio.run(tool.call(context, messages=[{"type": "plain"}]))
            )
            self.assertEqual(tool.call_count, 1)
            self.assertTrue(
                any("Suppressed" in message for message in logger.warning_messages)
            )
        finally:
            patch.uninstall()

    def test_validation_error_allows_one_corrected_attempt(self) -> None:
        error = "error: messages parameter is empty or invalid."
        target = make_tool_type([error, "Message sent to session cron:group:1"])
        tool = target()
        patch, _ = make_patch(target, tool)
        event = make_active_event()
        context = make_context(event)

        try:
            patch.install()
            self.assertEqual(asyncio.run(tool.call(context, messages=[])), error)
            self.assertEqual(event.get_extra(EVENT_STATE_KEY), STATE_READY)

            self.assertIsNone(
                asyncio.run(tool.call(context, messages=[{"type": "plain"}]))
            )
            self.assertEqual(event.get_extra(EVENT_STATE_KEY), STATE_DELIVERED)
            self.assertEqual(tool.call_count, 2)
        finally:
            patch.uninstall()

    def test_transport_exception_is_ambiguous_and_blocks_retry(self) -> None:
        target = make_tool_type(
            [RuntimeError("transport failed"), "Message sent to session cron:group:1"]
        )
        tool = target()
        patch, logger = make_patch(target, tool)
        event = make_active_event()
        context = make_context(event)

        try:
            patch.install()
            with self.assertRaisesRegex(RuntimeError, "transport failed"):
                asyncio.run(tool.call(context, messages=[{"type": "plain"}]))

            self.assertEqual(event.get_extra(EVENT_STATE_KEY), STATE_IN_FLIGHT)
            self.assertIsNone(
                asyncio.run(tool.call(context, messages=[{"type": "plain"}]))
            )
            self.assertEqual(tool.call_count, 1)
            self.assertTrue(
                any(
                    "unknown delivery status" in message
                    for message in logger.warning_messages
                )
            )
        finally:
            patch.uninstall()

    def test_terminal_native_results_never_trigger_a_second_send(self) -> None:
        cases = [(None, STATE_DELIVERED), ({"future": "result"}, STATE_TERMINAL)]
        for native_result, expected_state in cases:
            with self.subTest(native_result=native_result):
                target = make_tool_type(
                    [native_result, "Message sent to session cron:group:1"]
                )
                tool = target()
                patch, _ = make_patch(target, tool)
                event = make_active_event()
                context = make_context(event)

                try:
                    patch.install()
                    self.assertIsNone(asyncio.run(tool.call(context)))
                    self.assertEqual(
                        event.get_extra(EVENT_STATE_KEY), expected_state
                    )
                    self.assertIsNone(asyncio.run(tool.call(context)))
                    self.assertEqual(tool.call_count, 1)
                finally:
                    patch.uninstall()

    def test_two_cron_events_are_isolated(self) -> None:
        target = make_tool_type(
            [
                "Message sent to session cron:group:1",
                "Message sent to session cron:group:1",
            ]
        )
        tool = target()
        patch, _ = make_patch(target, tool)
        first = make_active_event("job-1")
        second = make_active_event("job-2")

        async def run_both() -> list[Any]:
            return await asyncio.gather(
                tool.call(make_context(first)),
                tool.call(make_context(second)),
            )

        try:
            patch.install()
            self.assertEqual(asyncio.run(run_both()), [None, None])
            self.assertEqual(tool.call_count, 2)
            self.assertEqual(first.get_extra(EVENT_STATE_KEY), STATE_DELIVERED)
            self.assertEqual(second.get_extra(EVENT_STATE_KEY), STATE_DELIVERED)
        finally:
            patch.uninstall()

    def test_concurrent_calls_for_one_event_enter_sender_once(self) -> None:
        class SlowSendTool:
            handler = None

            def __init__(self) -> None:
                self.call_count = 0
                self.started = asyncio.Event()
                self.release = asyncio.Event()

            async def call(self, context: Any, **kwargs: Any) -> str:
                self.call_count += 1
                self.started.set()
                await self.release.wait()
                return "Message sent to session cron:group:1"

        tool = SlowSendTool()
        patch, _ = make_patch(SlowSendTool, tool)
        event = make_active_event()
        context = make_context(event)

        async def run_concurrently() -> tuple[Any, Any]:
            first = asyncio.create_task(tool.call(context))
            await tool.started.wait()
            second = asyncio.create_task(tool.call(context))
            second_result = await second
            tool.release.set()
            first_result = await first
            return first_result, second_result

        try:
            patch.install()
            self.assertEqual(asyncio.run(run_concurrently()), (None, None))
            self.assertEqual(tool.call_count, 1)
            self.assertEqual(event.get_extra(EVENT_STATE_KEY), STATE_DELIVERED)
        finally:
            patch.uninstall()

    def test_non_target_events_are_transparent(self) -> None:
        target = make_tool_type(
            [
                "Message sent to session normal:group:1",
                "Message sent to session cron:group:1",
                "Message sent to session cron:group:1",
                "Message sent to session cron:group:1",
            ]
        )
        tool = target()
        patch, _ = make_patch(target, tool)
        events = [
            FakeEvent(),
            FakeCronEvent({"background_task_result": {"task_id": "task-1"}}),
            FakeCronEvent({"cron_job": {"type": "basic"}}),
            FakeCronEvent({"cron_job": "malformed"}),
        ]

        try:
            patch.install()
            results = [
                asyncio.run(tool.call(make_context(event))) for event in events
            ]
            self.assertTrue(
                all(result.startswith("Message sent to session ") for result in results)
            )
            self.assertEqual(tool.call_count, 4)
            self.assertTrue(
                all(EVENT_STATE_KEY not in event.extras for event in events)
            )
        finally:
            patch.uninstall()

    def test_install_is_idempotent_and_uninstall_restores_original(self) -> None:
        target = make_tool_type()
        tool = target()
        original = target.call
        patch, _ = make_patch(target, tool)

        self.assertTrue(patch.install())
        wrapper = target.call
        self.assertTrue(patch.install())
        self.assertIs(target.call, wrapper)

        patch.uninstall()
        self.assertIs(target.call, original)
        self.assertFalse(patch.installed)

    def test_hot_reload_replaces_stale_wrapper_without_stacking(self) -> None:
        target = make_tool_type()
        tool = target()
        original = target.call
        first, first_logger = make_patch(target, tool)
        second, _ = make_patch(target, tool)

        try:
            first.install()
            first_wrapper = target.call
            second.install()
            second_wrapper = target.call

            self.assertIsNot(second_wrapper, first_wrapper)
            self.assertFalse(first.installed)
            first.uninstall()
            self.assertIs(target.call, second_wrapper)
            self.assertFalse(first_logger.warning_messages)

            event = make_active_event()
            self.assertIsNone(asyncio.run(tool.call(make_context(event))))
            self.assertEqual(tool.call_count, 1)
        finally:
            second.uninstall()

        self.assertIs(target.call, original)

    def test_later_third_party_wrapper_is_preserved_on_uninstall(self) -> None:
        target = make_tool_type()
        tool = target()
        original = target.call
        patch, logger = make_patch(target, tool)
        patch.install()
        plugin_wrapper = target.call

        async def third_party_wrapper(
            self: Any,
            context: Any,
            **kwargs: Any,
        ) -> Any:
            return await plugin_wrapper(self, context, **kwargs)

        target.call = third_party_wrapper
        try:
            patch.uninstall()
            self.assertIs(target.call, third_party_wrapper)
            self.assertTrue(logger.warning_messages)

            result = asyncio.run(tool.call(make_context(make_active_event())))
            self.assertTrue(result.startswith("Message sent to session "))
            self.assertEqual(tool.call_count, 1)
        finally:
            target.call = original

    def test_hot_reload_preserves_third_party_wraps_wrapper(self) -> None:
        target = make_tool_type()
        tool = target()
        original = target.call
        first, first_logger = make_patch(target, tool)
        second, _ = make_patch(target, tool)
        first.install()
        plugin_wrapper = target.call

        @wraps(plugin_wrapper)
        async def third_party_wrapper(
            self: Any,
            context: Any,
            **kwargs: Any,
        ) -> Any:
            return await plugin_wrapper(self, context, **kwargs)

        target.call = third_party_wrapper
        try:
            second.install()
            second_wrapper = target.call
            self.assertIsNot(second_wrapper, third_party_wrapper)
            self.assertFalse(first.installed)

            first.uninstall()
            self.assertIs(target.call, second_wrapper)
            self.assertFalse(first_logger.warning_messages)

            event = make_active_event()
            self.assertIsNone(asyncio.run(tool.call(make_context(event))))
            self.assertEqual(tool.call_count, 1)

            second.uninstall()
            self.assertIs(target.call, third_party_wrapper)
        finally:
            first.uninstall()
            second.uninstall()
            target.call = original

    def test_unsupported_versions_fail_without_modifying_target(self) -> None:
        for version in ("4.26.5", "4.27.0", "unknown"):
            with self.subTest(version=version):
                target = make_tool_type()
                tool = target()
                original = target.call
                patch, _ = make_patch(target, tool, version=version)

                with self.assertRaises(IncompatibleAstrBotError):
                    patch.install()
                self.assertIs(target.call, original)

    def test_incompatible_signature_fails_without_modifying_target(self) -> None:
        class BrokenSendTool:
            handler = None

            async def call(self, context: Any) -> str:
                return "Message sent"

        tool = BrokenSendTool()
        original = BrokenSendTool.call
        patch, _ = make_patch(BrokenSendTool, tool)

        with self.assertRaisesRegex(IncompatibleAstrBotError, "expected"):
            patch.install()
        self.assertIs(BrokenSendTool.call, original)

    def test_non_async_call_fails_without_modifying_target(self) -> None:
        class SyncSendTool:
            handler = None

            def call(self, context: Any, **kwargs: Any) -> str:
                return "Message sent"

        tool = SyncSendTool()
        original = SyncSendTool.call
        patch, _ = make_patch(SyncSendTool, tool)

        with self.assertRaisesRegex(IncompatibleAstrBotError, "async"):
            patch.install()
        self.assertIs(SyncSendTool.call, original)

    def test_cached_instance_call_shadow_is_rejected(self) -> None:
        target = make_tool_type()
        tool = target()
        original = target.call

        async def shadow(self: Any, context: Any, **kwargs: Any) -> str:
            return "Message sent by shadow"

        tool.call = MethodType(shadow, tool)
        patch, _ = make_patch(target, tool)

        with self.assertRaisesRegex(IncompatibleAstrBotError, "instance-level"):
            patch.install()
        self.assertIs(target.call, original)

    def test_cached_handler_bypass_is_rejected(self) -> None:
        target = make_tool_type()
        tool = target()
        original = target.call
        tool.handler = object()
        patch, _ = make_patch(target, tool)

        with self.assertRaisesRegex(IncompatibleAstrBotError, "handler"):
            patch.install()
        self.assertIs(target.call, original)

    def test_unstable_tool_manager_is_rejected(self) -> None:
        target = make_tool_type()
        original = target.call

        class UnstableManager:
            def get_builtin_tool(self, requested: type[Any]) -> Any:
                return requested()

        patch, _ = make_patch(target, target(), manager=UnstableManager())

        with self.assertRaisesRegex(IncompatibleAstrBotError, "stable cached"):
            patch.install()
        self.assertIs(target.call, original)

    def test_malformed_stale_wrapper_is_rejected(self) -> None:
        target = make_tool_type()
        tool = target()
        original = target.call

        async def malformed(self: Any, context: Any, **kwargs: Any) -> str:
            return "Message sent"

        setattr(malformed, "__astrbot_fix_corn_repeat_patch__", PLUGIN_ID)
        setattr(malformed, "__astrbot_fix_corn_repeat_owner__", malformed)
        target.call = malformed
        patch, _ = make_patch(target, tool)

        try:
            with self.assertRaisesRegex(IncompatibleAstrBotError, "malformed stale"):
                patch.install()
            self.assertIs(target.call, malformed)
        finally:
            target.call = original


if __name__ == "__main__":
    unittest.main()
