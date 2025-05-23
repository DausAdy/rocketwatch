import time
import pickle
import asyncio
import logging

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from enum import Enum
from functools import partial
from typing import Optional, Any

import pymongo
from cronitor import Monitor
from discord.ext import commands, tasks
from eth_typing import BlockIdentifier, BlockNumber
from motor.motor_asyncio import AsyncIOMotorClient
from web3.datastructures import MutableAttributeDict

from rocketwatch import RocketWatch
from plugins.support_utils.support_utils import generate_template_embed
from utils.status import StatusPlugin
from utils.cfg import cfg
from utils.embeds import assemble, Embed
from utils.event import EventPlugin
from utils.shared_w3 import w3

log = logging.getLogger("event_core")
log.setLevel(cfg["log_level"])


class EventCore(commands.Cog):
    class State(Enum):
        OK = 0
        ERROR = 1

        def __str__(self) -> str:
            return self.name

    def __init__(self, bot: RocketWatch):
        self.bot = bot
        self.state = self.State.OK
        self.channels = cfg["discord.channels"]
        self.db = AsyncIOMotorClient(cfg["mongodb.uri"]).rocketwatch
        self.head_block: BlockIdentifier = cfg["events.genesis"]
        self.block_batch_size = cfg["events.block_batch_size"]
        self.monitor = Monitor("gather-new-events", api_key=cfg["other.secrets.cronitor"])
        self.loop.start()

    def cog_unload(self) -> None:
        self.loop.cancel()

    @tasks.loop(seconds=12)
    async def loop(self) -> None:
        p_id = time.time()
        self.monitor.ping(state="run", series=p_id)

        try:
            await self.gather_new_events()
            await self.process_event_queue()
            await self.update_status_messages()
            await self.on_success()
            self.monitor.ping(state="complete", series=p_id)
        except Exception as error:
            await self.on_error(error)
            self.monitor.ping(state="fail", series=p_id)

    @loop.before_loop
    async def before_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def on_success(self) -> None:
        if self.state == self.State.ERROR:
            self.state = self.State.OK
            self.loop.change_interval(seconds=12)

    async def on_error(self, error: Exception) -> None:
        await self.bot.report_error(error)
        if self.state == self.State.OK:
            self.state = self.State.ERROR
            self.loop.change_interval(seconds=30)

        try:
            await self.show_service_interrupt()
        except Exception as err:
            await self.bot.report_error(err)

    async def gather_new_events(self) -> None:
        log.info("Gathering messages from submodules")
        log.debug(f"{self.head_block = }")

        latest_block = w3.eth.get_block_number()
        submodules = [cog for cog in self.bot.cogs.values() if isinstance(cog, EventPlugin)]
        log.debug(f"Running {len(submodules)} submodules")

        if self.head_block == "latest":
            # already caught up to head, just fetch new events
            target_block = "latest"
            to_block = latest_block
            gather_fns = [sm.get_new_events for sm in submodules]
            # prevent losing state if process is interrupted before updating db
            self.head_block = cfg["events.genesis"]
        else:
            # behind chain head, let's see how far
            last_event_entry = await self.db.event_queue.find().sort(
                "block_number", pymongo.DESCENDING
            ).limit(1).to_list(None)
            if last_event_entry:
                self.head_block = max(self.head_block, last_event_entry[0]["block_number"])

            last_checked_entry = await self.db.last_checked_block.find_one({"_id": "events"})
            if last_checked_entry:
                self.head_block = max(self.head_block, last_checked_entry["block"])

            if (latest_block - self.head_block) < self.block_batch_size:
                # close enough to catch up in a single request
                target_block = "latest"
                to_block = latest_block
            else:
                # too far, advance one batch
                target_block = self.head_block + self.block_batch_size
                to_block = target_block

            from_block: BlockNumber = self.head_block + 1
            if to_block < from_block:
                log.warning(f"Skipping empty block range [{from_block}, {to_block}]")
                return

            log.info(f"Checking block range [{from_block}, {to_block}]")

            gather_fns = []
            for sm in submodules:
                fn = partial(sm.get_past_events, from_block=from_block, to_block=to_block)
                gather_fns.append(fn)
                if target_block == "latest":
                    sm.start_tracking(to_block + 1)

        log.debug(f"{target_block = }")

        with ThreadPoolExecutor() as executor:
            loop = asyncio.get_running_loop()
            futures = [loop.run_in_executor(executor, gather_fn) for gather_fn in gather_fns]
            results = await asyncio.gather(*futures)

        channels = cfg["discord.channels"]
        events: list[dict[str, Any]] = []

        for result in results:
            for event in result:
                if await self.db.event_queue.find_one({"_id": event.unique_id}):
                    log.debug(f"Event {event} already exists, skipping")
                    continue

                # select channel dynamically from config based on event_name prefix
                channel_candidates = [value for key, value in channels.items() if event.event_name.startswith(key)]
                channel_id = channel_candidates[0] if channel_candidates else channels["default"]
                events.append({
                    "_id": event.unique_id,
                    "embed": pickle.dumps(event.embed),
                    "topic": event.topic,
                    "event_name": event.event_name,
                    "block_number": event.block_number,
                    "score": event.get_score(),
                    "time_seen": datetime.now(),
                    "image": pickle.dumps(event.image) if event.image else None,
                    "thumbnail": pickle.dumps(event.thumbnail) if event.thumbnail else None,
                    "channel_id": channel_id,
                    "message_id": None
                })

        log.info(f"{len(events)} new events gathered, updating DB")
        if events:
            await self.db.event_queue.insert_many(events)

        self.head_block = target_block
        self.db.last_checked_block.replace_one(
            {"_id": "events"},
            {"_id": "events", "block": to_block},
            upsert=True
        )

    async def process_event_queue(self) -> None:
        log.debug("Processing events in queue")
        # get all channels with unprocessed events
        channels = await self.db.event_queue.distinct("channel_id", {"message_id": None})
        if not channels:
            log.debug("No pending events in queue")
            return

        def try_load(_entry: dict, _key: str) -> Optional[Any]:
            try:
                serialized = _entry.get(_key)
                return pickle.loads(serialized) if serialized else None
            except Exception as err:
                self.bot.report_error(err)
                return None

        for channel_id in channels:
            db_events: list[dict] = await self.db.event_queue.find(
                {"channel_id": channel_id, "message_id": None}
            ).sort("score", pymongo.ASCENDING).to_list(None)

            log.debug(f"Found {len(db_events)} events for channel {channel_id}.")
            channel = await self.bot.get_or_fetch_channel(channel_id)

            for state_message in await self.db.state_messages.find({"channel_id": channel_id}).to_list(None):
                msg = await channel.fetch_message(state_message["message_id"])
                await msg.delete()
                await self.db.state_messages.delete_one({"channel_id": channel_id})

            for event_entry in db_events:
                embed: Optional[Embed] = try_load(event_entry, "embed")
                files = []

                if embed and (image := try_load(event_entry, "image")):
                    file_name = f"{event_entry['event_name']}_img.png"
                    files.append(image.to_file(file_name))
                    embed.set_image(url=f"attachment://{file_name}")

                if embed and (thumbnail := try_load(event_entry, "thumbnail")):
                    file_name = f"{event_entry['event_name']}_thumb.png"
                    files.append(thumbnail.to_file(file_name))
                    embed.set_thumbnail(url=f"attachment://{file_name}")

                # post event message
                msg = await channel.send(embed=embed, files=files)
                # add message id to event
                await self.db.event_queue.update_one(
                    {"_id": event_entry["_id"]},
                    {"$set": {"message_id": msg.id}}
                )

        log.info("Processed all events in queue")

    async def update_status_messages(self) -> None:
        configs = cfg.get("events.status_message", {})
        for state_message in (await self.db.state_messages.find().to_list(None)):
            if state_message["_id"] not in configs:
                log.debug(f"No config for state message ID {state_message['_id']}, removing message")
                await self._replace_or_add_status("", None, state_message)

        for channel_name, config in configs.items():
            log.debug(f"Updating state message for channel {channel_name}")
            await self._update_status_message(channel_name, config)

    async def _update_status_message(self, channel_name: str, config: dict) -> None:
        state_message = await self.db.state_messages.find_one({"_id": channel_name})
        if state_message:
            age = datetime.now() - state_message["sent_at"]
            cooldown = timedelta(seconds=config["cooldown"])
            if (age < cooldown) and (state_message["state"] == str(self.State.OK)):
                log.debug(f"State message for {channel_name} not past cooldown: {age} < {cooldown}")
                return

        if not (embed := await generate_template_embed(self.db, "announcement")):
            try:
                plugin: StatusPlugin = self.bot.cogs.get(config["plugin"])
                embed = await plugin.get_status()
            except Exception as err:
                await self.bot.report_error(err)
                return

        embed.timestamp = datetime.now()
        embed.set_footer(text=f"Tracking {cfg['rocketpool.chain']} using {len(self.bot.cogs)} plugins")
        for field in config["fields"]:
            embed.add_field(**field)

        await self._replace_or_add_status(channel_name, embed, state_message)

    async def show_service_interrupt(self) -> None:
        embed = assemble(MutableAttributeDict({"event_name": "service_interrupted"}))
        for channel_name in cfg.get("events.status_message", {}).keys():
            state_message = await self.db.state_messages.find_one({"_id": channel_name})
            if (not state_message) or (state_message["state"] != str(self.state.ERROR)):
                await self._replace_or_add_status(channel_name, embed, state_message)

    async def _replace_or_add_status(
            self,
            target_channel: str,
            embed: Optional[Embed],
            prev_status: Optional[dict]
    ) -> None:
        target_channel_id = self.channels.get(target_channel) or self.channels["default"]

        if embed and prev_status and (prev_status["channel_id"] == target_channel_id):
            log.debug(f"Replacing existing status message for channel {target_channel}")
            channel = await self.bot.get_or_fetch_channel(target_channel_id)
            msg = await channel.fetch_message(prev_status["message_id"])
            await msg.edit(embed=embed)
            await self.db.state_messages.update_one(
                prev_status,
                {"$set": {"sent_at": datetime.now(), "state": str(self.state)}}
            )
            return

        if prev_status:
            log.debug(f"Deleting status message for channel {target_channel}")
            channel = await self.bot.get_or_fetch_channel(prev_status["channel_id"])
            msg = await channel.fetch_message(prev_status["message_id"])
            await msg.delete()
            await self.db.state_messages.delete_one(prev_status)

        if embed:
            log.debug(f"Creating new status message for channel {target_channel}")
            channel = await self.bot.get_or_fetch_channel(target_channel_id)
            msg = await channel.send(embed=embed, silent=True)
            await self.db.state_messages.insert_one({
                "_id"       : target_channel,
                "channel_id": target_channel_id,
                "message_id": msg.id,
                "sent_at"   : datetime.now(),
                "state"     : str(self.state)
            })


async def setup(bot):
    await bot.add_cog(EventCore(bot))
