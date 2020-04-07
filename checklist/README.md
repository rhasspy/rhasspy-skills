# Rhasspy Checklist Skill

Simple skill for [Rhasspy](https://github.com/rhasspy) that allows a user to confirm/disconfirm items on a checklist.

## Installing

Requires Python 3.7 or higher.

To install, clone the repository and create a virtual environment:

```bash
git clone https://github.com/rhasspy/rhasspy-skills.git
cd rhasspy-skills/checklist
make
```

## Running

After installing, use the `bin/rhasspy-checklist` script to run:

```bash
bin/rhasspy-checklist --help
```

This skill connects to Rhasspy's MQTT broker. If you have Rhasspy configured to use its internal broker, you must set a different MQTT port for the skill using `--port <mqtt_port>`. By default, Rhasspy runs its internal broker on port 12183. If you're running Rhasspy inside Docker, make sure to expose this port with `docker run ... -p 12183:12183 ...`

Example connecting to a Rhasspy server already running on the local machine:

```bash
bin/rhasspy-checklist --debug --port 12183
```

## Using

Send a JSON message to `rhasspy/checklist/start` to begin a new checklist. The following fields are available:

* `id: string` - Unique id for checklist (required)
* `items: list[object]` - Items in checklist (required)
    * `id: string` - Unique identifier for item (required)
    * `text: string` - Text to speak for this item (required)
    * `confirmIntent: string? = null` - Intent to confirm item (overrides start message)
    * `disconfirmIntent: string? = null` - Intent to disconfirm item (overrides start message)
    * `cancelIntent: string? = null` - Intent to cancel checklist (overrides start message)
* `endText: string = ""` - Text to speak when checklist is finished
* `confirmIntent: string? = null` - Default intent to confirm items
* `disconfirmIntent: string? = null` - Default intent to disconfirm items
* `cancelIntent: string? = null` - Default intent to cancel checklist
* `siteId: string = "default"` - Hermes siteID

The `confirmIntent`, `disconfirmIntent`, and `cancelIntent` fields are the names of Rhasspy intents for confirming/disconfirming a task and cancelling the checklist. These can be set in the `start` message as well as for individual `items`.

Once started, this skill will continue the dialogue session until all items have been confirmed/disconfirmed or the checklist has been cancelled. Once the session ends, a JSON message will be published on `rhasspy/checklist/finished` with the following fields:

* `id: string` - Unique identifier from start message
* `status: string` - Final status of checklist, one of:
    * "allConfirmed" - all items were confirmed
    * "someConfirmed" - at least one item was confirmed
    * "noneConfirmed" - no items were confirmed
    * "cancelled" - checklist was cancelled
* `confirmedIds: [string] = []` - Item identifiers that were confirmed
* `cancelledId: string? = null` - Item identifier when checklist was cancelled
* `siteId: string = "default"` - Hermes siteId

## Examples

Assume Rhasspy has the following intents in `sentences.ini` and has been trained:

```ini
[ChecklistConfirm]
confirm

[ChecklistDisconfirm]
negative

[ChecklistCancel]
cancel checklist
```

Let's start a checklist with two items, one named "item-1" and the other "item-2".
The following JSON is sent to the skill on the MQTT topic `rhasspy/checklist/start`:

```json
{
  "id": "checklist-1",
  "items": [
      { "id": "item-1", "text": "Please confirm item one." },
      { "id": "item-2", "text": "Please confirm item two." }
  ],
  "confirmIntent": "ChecklistConfirm",
  "disconfirmIntent": "ChecklistDisconfirm",
  "cancelIntent": "ChecklistCancel",
  "endText": "Checklist is complete."
}
```

You should hear Rhasspy speak the sentence "Please confirm item one". Say "confirm" or "negative". The skill will then move on to the next item. After all items are complete, a JSON message will be published to `rhasspy/checklist/finished` like:

```json
{
    "id": "checklist-1",
    "status": "allConfirmed",
    "confirmedIds": ["item-1", "item-2"],
    "cancelledId": null,
    "siteId": "default"
}
```

All confirmed item ids are provided as well as a status indicating if all/some/none of the items were confirmed. If the checklist was cancelled, `cancelledId` will be the id of the item is was cancelled on.

If an intent recognition failure occurs, the skill will repeat the last item.
