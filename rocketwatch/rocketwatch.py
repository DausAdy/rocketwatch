import io
import logging
import traceback
from pathlib import Path
from typing import Optional

from discord import (
    app_commands, 
    Thread, 
    File, 
    Object, 
    User
)
from discord.abc import GuildChannel, PrivateChannel
from discord.ext.commands import Bot, Context
from discord.ext import commands

from utils.cfg import cfg
from utils.retry import retry_async

log = logging.getLogger("rocketwatch")
log.setLevel(cfg["log_level"])


class RocketWatch(Bot):
    async def _load_plugins(self):
        chain = cfg["rocketpool.chain"]
        storage = cfg["rocketpool.manual_addresses.rocketStorage"]
        log.info(f"Running using storage contract {storage} (Chain: {chain})")

        log.info('Loading plugins')
        included_modules = set(cfg["modules.include"] or [])
        excluded_modules = set(cfg["modules.exclude"] or [])

        def should_load_plugin(_plugin: str) -> bool:
            # inclusion takes precedence in case of collision
            if _plugin in included_modules:
                log.debug(f"Plugin {_plugin} explicitly included")
                return True
            elif _plugin in excluded_modules:
                log.debug(f"Plugin {_plugin} explicitly excluded")
                return False
            elif len(included_modules) > 0:
                log.debug(f"Plugin {_plugin} implicitly excluded")
                return False
            else:
                log.debug(f"Plugin {_plugin} implicitly included")
                return True

        for path in Path("plugins").glob('**/*.py'):
            plugin_name = path.stem
            if not should_load_plugin(plugin_name):
                log.warning(f"Skipping plugin {plugin_name}")
                continue

            log.info(f"Loading plugin \"{plugin_name}\"")
            try:
                extension_name = f"plugins.{plugin_name}.{plugin_name}"
                await self.load_extension(extension_name)
            except Exception:
                log.exception(f"Failed to load plugin \"{plugin_name}\"")

        log.info('Finished loading plugins')

    async def setup_hook(self) -> None:
        await self._load_plugins()

    async def on_ready(self):
        log.info(f"Logged in as {self.user.name} ({self.user.id})")
        
        if cfg["modules.enable_commands"] is None:
            log.info("Command sync behavior not specified, skipping")
            return

        if not cfg["modules.enable_commands"]:
            log.info("Commands disabled, clearing tree...")
            self.tree.clear_commands(guild=None)

        log.info("Syncing command tree...")
        await self.tree.sync()
        
        # use faster local sync
        await self.tree.sync(guild=Object(id=cfg["discord.owner.server_id"]))
        await self.tree.sync(guild=Object(id=cfg["rocketpool.support.server_id"]))

    async def on_command_error(self, ctx: Context, exception: Exception) -> None:
        log.info(f"/{ctx.command.name} called by {ctx.author} in #{ctx.channel.name} ({ctx.guild}) failed")
        if isinstance(exception, commands.errors.MaxConcurrencyReached):
            msg = "Someone else is already using this command. Please try again later"
        elif isinstance(exception, app_commands.errors.CommandOnCooldown):
            msg = f"Slow down! You are using this command too fast. Please try again in {exception.retry_after:.0f} seconds"
        else:
            msg = "An unexpected error occurred and has been reported to the developer. Please try again later"

        try:
            await self.report_error(exception, ctx)
            await ctx.send(content=msg, ephemeral=True)
        except Exception:
            log.exception("Failed to alert user")

    async def get_or_fetch_channel(self, channel_id: int) -> GuildChannel | PrivateChannel | Thread:
        return self.get_channel(channel_id) or await self.fetch_channel(channel_id)

    async def get_or_fetch_user(self, user_id: int) -> User:
        return self.get_user(user_id) or await self.fetch_user(user_id)

    async def report_error(self, exception: Exception, ctx: Optional[Context] = None, *args) -> None:
        err_description = f"`{repr(exception)[:150]}`"
        if args:
            args_fmt = "\n".join(f"args[{i}] = {arg}" for i, arg in enumerate(args))
            err_description += f"\n```{args_fmt}```"
        if ctx:
            err_description += (
                f"\n```"
                f"{ctx.command.name = }\n"
                f"{ctx.command.params = }\n"
                f"{ctx.channel = }\n"
                f"{ctx.author = }"
                f"```"
            )

        error = getattr(exception, "original", exception)
        err_trace = "".join(traceback.format_exception(type(error), error, error.__traceback__))
        log.error(err_trace)

        try:
            channel = await self.get_or_fetch_channel(cfg["discord.channels.errors"])
            file = File(io.BytesIO(err_trace.encode()), "exception.txt")
            await retry_async(tries=5, delay=5)(channel.send)(err_description, file=file)
        except Exception:
            log.exception("Failed to send message. Max retries reached.")
