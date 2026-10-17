"""The slice of Slack the channel touches: Bolt's AsyncWebClient (chat_postMessage / chat_update /
chat_postEphemeral), an AsyncApp holding it, the plugin context's Slack registrations, and block_actions bodies
shaped as Slack sends them. A failure is an ApiError carrying `.response["error"]`, as slack_sdk's SlackApiError."""


class ApiError(Exception):
    def __init__(self, error):
        super().__init__(f"The request to the Slack API failed. (error: {error})")
        self.response = {"ok": False, "error": error}


class Client:
    def __init__(self, order=None):
        self.posts, self.updates, self.ephemerals = [], [], []
        self.fail = {}  # method -> [error or None, ...]: the next calls raise these (None: succeeds), in order
        self.order = order if order is not None else []
        self.count = 0

    def _call(self, method, kw, into):
        self.order.append(method)
        error = (self.fail.get(method) or [None]).pop(0)
        if error:
            raise ApiError(error)
        into.append(kw)
        self.count += 1
        return {"ok": True, "ts": kw.get("ts") or f"{self.count}.000100"}

    async def chat_postMessage(self, **kw):
        return self._call("chat_postMessage", kw, self.posts)

    async def chat_update(self, **kw):
        return self._call("chat_update", kw, self.updates)

    async def chat_postEphemeral(self, **kw):
        return self._call("chat_postEphemeral", kw, self.ephemerals)


class App:
    def __init__(self, client=None):
        self.client = client or Client()


class Ctx:
    def __init__(self, settings=None):
        self.platform_handlers, self.action_handlers = [], []
        self.settings = settings or {}

    def register_platform_handler(self, platform, factory):
        self.platform_handlers.append((platform, factory))

    def register_slack_action_handler(self, action_id, callback):
        self.action_handlers.append((action_id, callback))

    def get_config(self, key, default):
        return self.settings.get(key, default)


class Ack:
    def __init__(self, order):
        self.order, self.calls = order, 0

    async def __call__(self, *a, **kw):
        self.order.append("ack")
        self.calls += 1


def body(user, channel, block_id, action_id, value=None, team="T1", message_ts="1.000100", thread_ts=None,
         state=None):
    """A block_actions payload, as Slack sends it to a Socket Mode app."""
    action = {"type": "button", "action_id": action_id, "block_id": block_id,
              **({"value": value} if value is not None else {})}
    return {
        "type": "block_actions",
        "user": {"id": user, "team_id": team},
        "team": {"id": team},
        "channel": {"id": channel},
        "container": {"type": "message", "message_ts": message_ts, "channel_id": channel,
                      **({"thread_ts": thread_ts} if thread_ts else {})},
        "actions": [action],
        "state": {"values": state or {}},
    }, action
