import logging
import os
import pwd
import time
import asyncio
import json
import re

import aiohttp
import websockets
from slacker import Slacker

from opsdroid.connector import Connector
from opsdroid.message import Message


_LOGGER = logging.getLogger(__name__)


class ConnectorSlack(Connector):

    def __init__(self, config):
        """ Setup the connector """
        _LOGGER.debug("Starting Slack connector")
        self.name = "slack"
        self.config = config
        self.opsdroid = None
        self.default_room = config.get("default-room", "#general")
        self.icon_emoji = config.get("icon-emoji", ':robot_face:')
        self.token = config["api-token"]
        self.slack = Slacker(self.token)
        self.bot_name = config.get("bot-name", 'opsdroid')
        self.known_users = {}
        self.keepalive = None
        self.reconnecting = False
        self._message_id = 0

    async def connect(self, opsdroid=None):
        """ Connect to the chat service """
        if opsdroid is not None:
            self.opsdroid = opsdroid

        _LOGGER.info("Connecting to Slack")

        try:
            connection = await self.slack.rtm.start()
            self.ws = await websockets.connect(connection.body['url'])

            _LOGGER.debug("Connected as %s", self.bot_name)
            _LOGGER.debug("Using icon %s", self.icon_emoji)
            _LOGGER.debug("Default room is %s", self.default_room)
            _LOGGER.info("Connected successfully")

            if self.keepalive is None or self.keepalive.done():
                self.keepalive = self.opsdroid.eventloop.create_task(
                    self.keepalive_websocket()
                )
        except aiohttp.errors.ClientOSError as e:
            _LOGGER.error(e)
            _LOGGER.error("Failed to connect to Slack, retrying in 10")
            await self.reconnect(10)

    async def reconnect(self, delay=None):
        """Reconnect to the websocket."""
        try:
            self.reconnecting = True
            if delay is not None:
                await asyncio.sleep(delay)
            await self.connect()
        finally:
            self.reconnecting = False

    async def listen(self, opsdroid):
        """Listen for and parse new messages."""
        while True:
            try:
                content = await self.ws.recv()
            except websockets.exceptions.ConnectionClosed:
                _LOGGER.info("Slack websocket closed, reconnecting...")
                await self.reconnect(5)
                continue
            m = json.loads(content)
            if "type" in m and m["type"] == "message" and "user" in m:

                # Ignore bot messages
                if "subtype" in m and m["subtype"] == "bot_message":
                    continue

                # Lookup username
                _LOGGER.debug("Looking up sender username")
                try:
                    user_info = await self.lookup_username(m["user"])
                except ValueError:
                    continue

                # Replace usernames in the message
                _LOGGER.debug("Replacing userids in message with usernames")
                m["text"] = await self.replace_usernames(m["text"])

                message = Message(m["text"], user_info["name"], m["channel"], self)
                await opsdroid.parse(message)

    async def respond(self, message, attachments=None):
        """ Respond with a message """
        _LOGGER.debug("Responding with: '" + message.text +
                      "' in room " + message.room)
        await self.slack.chat.post_message(
            message.room,
            message.text,
            as_user=False,
            username=self.bot_name,
            icon_emoji=self.icon_emoji,
            attachments=attachments,
        )

    async def keepalive_websocket(self):
        while True:
            await asyncio.sleep(60)
            self._message_id += 1
            try:
                await self.ws.send(
                    json.dumps({'id': self._message_id, 'type': 'ping'}))
            except (websockets.exceptions.InvalidState,
                    websockets.exceptions.ConnectionClosed,
                    aiohttp.errors.ClientOSError,
                    TimeoutError):
                _LOGGER.info("Slack websocket closed, reconnecting...")
                if not self.reconnecting:
                    await self.reconnect()

    async def lookup_username(self, userid):
        # Check whether we've already looked up this user
        if userid in self.known_users:
            user_info = self.known_users[userid]
        else:
            response = await self.slack.users.info(userid)
            user_info = response.body["user"]
            if type(user_info) is dict:
                self.known_users[userid] = user_info
            else:
                raise ValueError("Returned user is not a dict.")
        return user_info

    async def replace_usernames(self, message):
        userids = re.findall(r"\<\@([A-Z0-9]+)\>", message)
        for userid in userids:
            user_info = await self.lookup_username(userid)
            message = message.replace("<@{userid}>".format(userid=userid),
                                      user_info["name"])
        return message
