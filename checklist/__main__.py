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
from dataclasses_json import LetterCase, dataclass_json
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

_LOGGER = logging.getLogger("checklist")

# -----------------------------------------------------------------------------


@dataclass_json(letter_case=LetterCase.CAMEL)
@dataclass
class ChecklistItem:
    """Single item from a checklist."""

    # Unique identifier for item
    id: str

    # Text to speak for this item
    text: str

    # Intent to confirm item (overrides start message)
    confirm_intent: typing.Optional[str] = None

    # Intent to disconfirm item (overrides start message)
    disconfirm_intent: typing.Optional[str] = None

    # Intent to cancel checklist (overrides start message)
    cancel_intent: typing.Optional[str] = None


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
    confirm_intent: typing.Optional[str] = None

    # Default intent to disconfirm items
    disconfirm_intent: typing.Optional[str] = None

    # Default intent to cancel checklist
    cancel_intent: typing.Optional[str] = None

    # Hermes siteID
    site_id: str = "default"

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
    confirmed_ids: typing.List[str] = field(default_factory=list)

    # Item identifier when checklist was cancelled
    cancelled_id: typing.Optional[str] = None

    # Hermes site_id
    site_id: str = "default"

    @classmethod
    def topic(cls, **kwargs) -> str:
        """MQTT topic for message."""
        return "rhasspy/checklist/finished"


# -----------------------------------------------------------------------------


class ChecklistClient(HermesClient):
    """Listens for and responds to checklist messages."""

    def __init__(self, mqtt_client, site_ids: typing.Optional[typing.List[str]] = None):
        super().__init__("checklist", mqtt_client, site_ids=site_ids)

        self.checklist_items: typing.Deque[ChecklistItem] = deque()
        self.session_id = ""
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
            site_id=start_message.site_id,
        )
        self.session_id = ""
        self.checklist_items = deque(start_message.items)

        # Complete intents with defaults
        for item in self.checklist_items:
            item.confirm_intent = (
                item.confirm_intent or self.start_message.confirm_intent
            )
            item.disconfirm_intent = (
                item.disconfirm_intent or self.start_message.disconfirm_intent
            )
            item.cancel_intent = item.cancel_intent or self.start_message.cancel_intent

        _LOGGER.debug(self.checklist_items)

        # First item
        self.current_item = self.checklist_items.popleft()
        intent_filter = [
            intent
            for intent in [
                self.current_item.confirm_intent,
                self.current_item.disconfirm_intent,
                self.current_item.cancel_intent,
            ]
            if intent
        ]

        assert intent_filter, "Need confirm/disconfirm/cancel intent"

        # Start new session
        yield DialogueStartSession(
            init=DialogueAction(
                can_be_enqueued=True,
                text=self.current_item.text,
                intent_filter=intent_filter,
                send_intent_not_recognized=True,
            ),
            custom_data=self.start_message.id,
            site_id=start_message.site_id,
        )

    async def maybe_next_item(self, nlu_intent: NluIntent):
        """Checks intent for confirm/disconfirm/cancel and maybe continues session."""
        assert self.current_item, "No current item"
        assert self.start_message, "No start message"
        assert self.finished_message, "No finished message"

        if nlu_intent.intent.intent_name == self.current_item.cancel_intent:
            _LOGGER.debug("Cancelled on item %s", self.current_item)
            self.finished_message.cancelled_id = self.current_item.id
            self.checklist_items.clear()
        elif nlu_intent.intent.intent_name == self.current_item.confirm_intent:
            _LOGGER.debug("Confirmed item %s", self.current_item)
            self.finished_message.confirmed_ids.append(self.current_item.id)
        elif nlu_intent.intent.intent_name == self.current_item.disconfirm_intent:
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
                session_id=self.session_id, text=self.start_message.endText
            )

    async def repeat_item(self):
        """Continues session with current item."""
        assert self.current_item, "No current item"

        intent_filter = [
            intent
            for intent in [
                self.current_item.confirm_intent,
                self.current_item.disconfirm_intent,
                self.current_item.cancel_intent,
            ]
            if intent
        ]

        assert intent_filter, "Need confirm/disconfirm/cancel intent"

        yield DialogueContinueSession(
            session_id=self.session_id,
            text=self.current_item.text,
            intent_filter=intent_filter,
            send_intent_not_recognized=True,
        )

    async def end_checklist(self):
        """Finishes a checklist."""
        assert self.start_message, "No start message"
        assert self.finished_message, "No finished message"

        # Determine status
        if self.finished_message.cancelled_id:
            self.finished_message.status = ChecklistFinishStatus.CANCELLED
        elif len(self.start_message.items) == len(self.finished_message.confirmed_ids):
            self.finished_message.status = ChecklistFinishStatus.ALL_CONFIRMED
        elif self.finished_message.confirmed_ids:
            self.finished_message.status = ChecklistFinishStatus.SOME_CONFIRMED
        else:
            self.finished_message.status = ChecklistFinishStatus.NONE_CONFIRMED

        yield self.finished_message

        # Reset
        self.start_message = None
        self.finished_message = None
        self.session_id = ""
        self.current_item = None

    async def on_message(
        self,
        message: Message,
        site_id: typing.Optional[str] = None,
        session_id: typing.Optional[str] = None,
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
                if self.start_message and (
                    message.custom_data == self.start_message.id
                ):
                    self.session_id = message.session_id
            elif isinstance(message, NluIntent):
                # hermes/intent/<intent_name>
                if message.session_id == self.session_id:
                    async for next_message in self.maybe_next_item(message):
                        yield next_message
            elif isinstance(message, DialogueIntentNotRecognized):
                # hermes/dialogueManager/intentNotRecognized
                if message.session_id == self.session_id:
                    async for repeat_message in self.repeat_item():
                        yield repeat_message
            elif isinstance(message, DialogueSessionEnded):
                # hermes/dialogueManager/sessionEnded
                if message.session_id == self.session_id:
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
    hermes_client = ChecklistClient(mqtt_client, site_ids=args.site_id)

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
