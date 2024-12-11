import time
from typing import List, Optional, Sequence, Set, Union

from jupyterlab_chat.ychat import YChat
from langchain_core.chat_history import BaseChatMessageHistory
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.pydantic_v1 import BaseModel, PrivateAttr

from .constants import BOT
from .models import HumanChatMessage

HUMAN_MSG_ID_KEY = "_jupyter_ai_human_msg_id"


class YChatHistory(BaseChatMessageHistory):
    """
    An implementation of `BaseChatMessageHistory` that returns the preceding `k`
    exchanges (`k * 2` messages) from the given YChat model.

    If `k` is set to `None`, then this class returns all preceding messages.
    """

    def __init__(self, ychat: YChat, k: Optional[int]):
        self.ychat = ychat
        self.k = k

    @property
    def messages(self) -> List[BaseMessage]:  # type:ignore[override]
        """
        Returns the last `2 * k` messages preceding the latest message. If
        `k` is set to `None`, return all preceding messages.
        """
        # TODO: consider bounding history based on message size (e.g. total
        # char/token count) instead of message count.
        all_messages = self.ychat.get_messages()

        # gather last k * 2 messages and return
        # we exclude the last message since that is the HumanChatMessage just
        # submitted by a user.
        messages: List[BaseMessage] = []
        start_idx = 0 if self.k is None else -2 * self.k - 1
        for message in all_messages[start_idx:-1]:
            if message["sender"] == BOT["username"]:
                messages.append(AIMessage(content=message["body"]))
            else:
                messages.append(HumanMessage(content=message["body"]))

        return messages

    def add_message(self, message: BaseMessage) -> None:
        # do nothing when other LangChain objects call this method, since
        # message history is maintained by the `YChat` shared document.
        return

    def clear(self):
        raise NotImplementedError()


class BoundedChatHistory(BaseChatMessageHistory, BaseModel):
    """
    An in-memory implementation of `BaseChatMessageHistory` that stores up to
    `k` exchanges between a user and an LLM.

    For example, when `k=2`, `BoundedChatHistory` will store up to 2 human
    messages and 2 AI messages. If `k` is set to `None` all messages are kept.
    """

    k: Union[int, None]
    clear_time: float = 0.0
    cleared_msgs: Set[str] = set()
    _all_messages: List[BaseMessage] = PrivateAttr(default_factory=list)

    @property
    def messages(self) -> List[BaseMessage]:  # type:ignore[override]
        if self.k is None:
            return self._all_messages
        return self._all_messages[-self.k * 2 :]

    async def aget_messages(self) -> List[BaseMessage]:
        return self.messages

    def add_message(self, message: BaseMessage) -> None:
        """Add a self-created message to the store"""
        if HUMAN_MSG_ID_KEY not in message.additional_kwargs:
            # human message id must be added to allow for targeted clearing of messages.
            # `WrappedBoundedChatHistory` should be used instead to add messages.
            raise ValueError(
                "Message must have a human message ID to be added to the store."
            )
        self._all_messages.append(message)

    async def aadd_messages(self, messages: Sequence[BaseMessage]) -> None:
        """Add messages to the store"""
        self.add_messages(messages)

    def clear(self, human_msg_ids: Optional[List[str]] = None) -> None:
        """Clears conversation exchanges. If `human_msg_id` is provided, only
        clears the respective human message and its reply. Otherwise, clears
        all messages."""
        if human_msg_ids:
            self._all_messages = [
                m
                for m in self._all_messages
                if m.additional_kwargs[HUMAN_MSG_ID_KEY] not in human_msg_ids
            ]
            self.cleared_msgs.update(human_msg_ids)
        else:
            self._all_messages = []
            self.cleared_msgs = set()
            self.clear_time = time.time()

    async def aclear(self) -> None:
        self.clear()


class WrappedBoundedChatHistory(BaseChatMessageHistory, BaseModel):
    """
    Wrapper around `BoundedChatHistory` that only appends an `AgentChatMessage`
    if the `HumanChatMessage` it is replying to was not cleared. If a chat
    handler is replying to a `HumanChatMessage`, it should pass this object via
    the `last_human_msg` configuration parameter.

    For example, a chat handler that is streaming a reply to a
    `HumanChatMessage` should be called via:

    ```py
    async for chunk in self.llm_chain.astream(
        {"input": message.body},
        config={"configurable": {"last_human_msg": message}},
    ):
        ...
    ```

    Reference: https://python.langchain.com/v0.1/docs/expression_language/how_to/message_history/
    """

    history: BoundedChatHistory
    last_human_msg: HumanChatMessage

    @property
    def messages(self) -> List[BaseMessage]:  # type:ignore[override]
        return self.history.messages

    def add_message(self, message: BaseMessage) -> None:
        # prevent adding pending messages to the store if clear was triggered.
        if (
            self.last_human_msg.time > self.history.clear_time
            and self.last_human_msg.id not in self.history.cleared_msgs
        ):
            message.additional_kwargs[HUMAN_MSG_ID_KEY] = self.last_human_msg.id
            self.history.add_message(message)

    async def aadd_messages(self, messages: Sequence[BaseMessage]) -> None:
        self.add_messages(messages)

    def clear(self) -> None:
        self.history.clear()
