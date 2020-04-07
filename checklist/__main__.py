#!/usr/bin/env python3
"""
Rhasspy skill to help user go through a checklist of items.

Items can be confirmed or disconfirmed.
Checklist can be cancelled.

Author: Michael Hansen (https://synesthesiam.com)
"""

import argparse
import asyncio
import logging
import typing
from collections import deque
from dataclasses import dataclass, field
from enum import Enum

import paho.mqtt.client as mqtt
import rhasspyhermes.cli as hermes_cli
from rhasspyhermes.base import Message
from rhasspyhermes.client import GeneratorType, HermesClient
from rhasspyhermes.dialogue import (
    DialogueAction,
    DialogueContinueSession,
    DialogueEndSession,
    DialogueIntentNotRecognized,
    DialogueSessionEnded,
    DialogueSessionStarted,
    DialogueStartSession,
)
from rhasspyhermes.nlu import NluIntent
from rhasspyhermes.utils import only_fields

_LOGGER = logging.getLogger("checklist")

# -----------------------------------------------------------------------------


@dataclass
class ChecklistItem:
    """Single item from a checklist."""

    # Unique identifier for item
    id: str

    # Text to speak for this item
    text: str

    # Intent to confirm item (overrides start message)
    confirmIntent: typing.Optional[str] = None

    # Intent to disconfirm item (overrides start message)
    disconfirmIntent: typing.Optional[str] = None

    # Intent to cancel checklist (overrides start message)
    cancelIntent: typing.Optional[str] = None

    @classmethod
    def from_dict(cls, item_dict: typing.Dict[str, typing.Any]):
        """Parse message from dictionary."""
        return cls(**only_fields(cls, item_dict))


@dataclass
class StartChecklist(Message):
    """Begins session for a new checklist."""

    # Unique id for checklist
    id: str

    # Items in checklist
    items: typing.List[ChecklistItem]

    # Text to speak when checklist is finished
    endText: str = ""

    # Default intent to confirm items
    confirmIntent: typing.Optional[str] = None

    # Default intent to disconfirm items
    disconfirmIntent: typing.Optional[str] = None

    # Default intent to cancel checklist
    cancelIntent: typing.Optional[str] = None

    # Hermes siteID
    siteId: str = "default"

    @classmethod
    def from_dict(cls, message_dict: typing.Dict[str, typing.Any]):
        """Parse message from dictionary."""
        items = message_dict.pop("items", [])
        items = [ChecklistItem.from_dict(item) for item in items]

        return cls(items=items, **only_fields(cls, message_dict))

    @classmethod
    def topic(cls, **kwargs) -> str:
        """MQTT topic for message."""
        return "rhasspy/checklist/start"


class ChecklistFinishStatus(str, Enum):
    """Status of finishing a checklist."""

    # Default status
    UNKNOWN = "unknown"

    # All items were confirmed
    ALL_CONFIRMED = "allConfirmed"

    # At least one item was confirmed
    SOME_CONFIRMED = "someConfirmed"

    # No items were confirmed
    NONE_CONFIRMED = "noneConfirmed"

    # Checklist was cancelled
    CANCELLED = "cancelled"


@dataclass
class ChecklistFinished(Message):
    """Indicates checklist has finished."""

    # Unique identifier from start message
    id: str

    # Final status of checklist
    status: ChecklistFinishStatus

    # Item identifiers that were confirmed
    confirmedIds: typing.List[str] = field(default_factory=list)

    # Item identifier when checklist was cancelled
    cancelledId: typing.Optional[str] = None

    # Hermes siteId
    siteId: str = "default"

    @classmethod
    def topic(cls, **kwargs) -> str:
        """MQTT topic for message."""
        return "rhasspy/checklist/finished"


# -----------------------------------------------------------------------------


class ChecklistClient(HermesClient):
    """Listens for and responds to checklist messages."""

    def __init__(self, mqtt_client, siteIds: typing.Optional[typing.List[str]] = None):
        super().__init__("checklist", mqtt_client, siteIds=siteIds)

        self.checklist_items: typing.Deque[ChecklistItem] = deque()
        self.sessionId = ""
        self.current_item: typing.Optional[ChecklistItem] = None
        self.start_message: typing.Optional[StartChecklist] = None
        self.finished_message: typing.Optional[ChecklistFinished] = None

        self.subscribe(
            StartChecklist,
            NluIntent,
            DialogueIntentNotRecognized,
            DialogueSessionStarted,
            DialogueSessionEnded,
        )

    async def start_checklist(self, start_message: StartChecklist):
        """Starts a new checklist."""
        assert start_message.items, "No checklist items"

        self.start_message = start_message
        self.finished_message = ChecklistFinished(
            id=start_message.id,
            status=ChecklistFinishStatus.UNKNOWN,
            siteId=start_message.siteId,
        )
        self.sessionId = ""
        self.checklist_items = deque(start_message.items)

        # Complete intents with defaults
        for item in self.checklist_items:
            item.confirmIntent = item.confirmIntent or self.start_message.confirmIntent
            item.disconfirmIntent = (
                item.disconfirmIntent or self.start_message.disconfirmIntent
            )
            item.cancelIntent = item.cancelIntent or self.start_message.cancelIntent

        _LOGGER.debug(self.checklist_items)

        # First item
        self.current_item = self.checklist_items.popleft()
        intent_filter = [
            intent
            for intent in [
                self.current_item.confirmIntent,
                self.current_item.disconfirmIntent,
                self.current_item.cancelIntent,
            ]
            if intent
        ]

        assert intent_filter, "Need confirm/disconfirm/cancel intent"

        # Start new session
        yield DialogueStartSession(
            init=DialogueAction(
                canBeEnqueued=True,
                text=self.current_item.text,
                intentFilter=intent_filter,
                sendIntentNotRecognized=True,
            ),
            customData=self.start_message.id,
            siteId=start_message.siteId,
        )

    async def maybe_next_item(self, nlu_intent: NluIntent):
        """Checks intent for confirm/disconfirm/cancel and maybe continues session."""
        assert self.current_item, "No current item"
        assert self.start_message, "No start message"
        assert self.finished_message, "No finished message"

        if nlu_intent.intent.intentName == self.current_item.cancelIntent:
            _LOGGER.debug("Cancelled on item %s", self.current_item)
            self.finished_message.cancelledId = self.current_item.id
            self.checklist_items.clear()
        elif nlu_intent.intent.intentName == self.current_item.confirmIntent:
            _LOGGER.debug("Confirmed item %s", self.current_item)
            self.finished_message.confirmedIds.append(self.current_item.id)
        elif nlu_intent.intent.intentName == self.current_item.disconfirmIntent:
            _LOGGER.debug("Disconfirmed item %s", self.current_item)

        if self.checklist_items:
            # Next item
            self.current_item = self.checklist_items.popleft()

            # Continue session
            async for item_message in self.repeat_item():
                yield item_message
        else:
            # End session
            yield DialogueEndSession(
                sessionId=self.sessionId, text=self.start_message.endText
            )

    async def repeat_item(self):
        """Continues session with current item."""
        assert self.current_item, "No current item"

        intent_filter = [
            intent
            for intent in [
                self.current_item.confirmIntent,
                self.current_item.disconfirmIntent,
                self.current_item.cancelIntent,
            ]
            if intent
        ]

        assert intent_filter, "Need confirm/disconfirm/cancel intent"

        yield DialogueContinueSession(
            sessionId=self.sessionId,
            text=self.current_item.text,
            intentFilter=intent_filter,
            sendIntentNotRecognized=True,
        )

    async def end_checklist(self):
        """Finishes a checklist."""
        assert self.start_message, "No start message"
        assert self.finished_message, "No finished message"

        # Determine status
        if self.finished_message.cancelledId:
            self.finished_message.status = ChecklistFinishStatus.CANCELLED
        elif len(self.start_message.items) == len(self.finished_message.confirmedIds):
            self.finished_message.status = ChecklistFinishStatus.ALL_CONFIRMED
        elif self.finished_message.confirmedIds:
            self.finished_message.status = ChecklistFinishStatus.SOME_CONFIRMED
        else:
            self.finished_message.status = ChecklistFinishStatus.NONE_CONFIRMED

        yield self.finished_message

        # Reset
        self.start_message = None
        self.finished_message = None
        self.sessionId = ""
        self.current_item = None

    async def on_message(
        self,
        message: Message,
        siteId: typing.Optional[str] = None,
        sessionId: typing.Optional[str] = None,
        topic: typing.Optional[str] = None,
    ) -> GeneratorType:
        """Received message from MQTT broker."""

        try:

            if isinstance(message, StartChecklist):
                # rhasspy/checklist/start
                async for start_message in self.start_checklist(message):
                    yield start_message
            elif isinstance(message, DialogueSessionStarted):
                # hermes/dialogueManager/sessionStarted
                if self.start_message and (message.customData == self.start_message.id):
                    self.sessionId = message.sessionId
            elif isinstance(message, NluIntent):
                # hermes/intent/<intentName>
                if message.sessionId == self.sessionId:
                    async for next_message in self.maybe_next_item(message):
                        yield next_message
            elif isinstance(message, DialogueIntentNotRecognized):
                # hermes/dialogueManager/intentNotRecognized
                if message.sessionId == self.sessionId:
                    async for repeat_message in self.repeat_item():
                        yield repeat_message
            elif isinstance(message, DialogueSessionEnded):
                # hermes/dialogueManager/sessionEnded
                if message.sessionId == self.sessionId:
                    async for end_message in self.end_checklist():
                        yield end_message
            else:
                _LOGGER.warning("Unexpected message: %s", message)

        except Exception:
            _LOGGER.exception("on_message")


# -----------------------------------------------------------------------------


def main():
    """Main entry point."""
    # Parse command-line arguments
    parser = argparse.ArgumentParser(prog="checklist")
    hermes_cli.add_hermes_args(parser)

    args = parser.parse_args()

    # Add default MQTT arguments
    hermes_cli.setup_logging(args)
    _LOGGER.debug(args)

    # Create MQTT client
    mqtt_client = mqtt.Client()
    hermes_client = ChecklistClient(mqtt_client, siteIds=args.siteId)

    # Try to connect
    _LOGGER.debug("Connecting to %s:%s", args.host, args.port)
    hermes_cli.connect(mqtt_client, args)
    mqtt_client.loop_start()

    try:
        # Run main loop
        asyncio.run(hermes_client.handle_messages_async())
    except KeyboardInterrupt:
        pass
    finally:
        mqtt_client.loop_stop()


if __name__ == "__main__":
    main()
