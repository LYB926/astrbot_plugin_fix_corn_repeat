"""AstrBot plugin entry point."""

from astrbot.api import logger
from astrbot.api.star import Context, Star

from .patching import CronRepeatGuardPatch, IncompatibleAstrBotError


class CronRepeatGuardPlugin(Star):
    """Install and remove the future-task repeat-send guard."""

    def __init__(self, context: Context) -> None:
        super().__init__(context)
        self._patch: CronRepeatGuardPatch | None = None

    async def initialize(self) -> None:
        """Install the runtime guard after validating AstrBot's contract."""

        patch = CronRepeatGuardPatch(self.context.get_llm_tool_manager(), logger)
        try:
            patch.install()
        except IncompatibleAstrBotError as exc:
            logger.error(
                "Future-task repeat-send guard is disabled because this "
                f"AstrBot runtime is incompatible: {exc}"
            )
            return

        self._patch = patch

    async def terminate(self) -> None:
        """Restore AstrBot's native method when the plugin is unloaded."""

        if self._patch is not None:
            self._patch.uninstall()
            self._patch = None
