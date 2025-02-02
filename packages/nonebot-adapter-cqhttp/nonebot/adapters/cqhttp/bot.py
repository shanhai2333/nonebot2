import re
import sys
import hmac
import json
import asyncio
from typing import Any, Dict, Tuple, Union, Optional, TYPE_CHECKING

import httpx
from nonebot.log import logger
from nonebot.typing import overrides
from nonebot.message import handle_event
from nonebot.adapters import Bot as BaseBot
from nonebot.utils import escape_tag, DataclassEncoder
from nonebot.drivers import Driver, ForwardDriver, WebSocketSetup
from nonebot.drivers import HTTPConnection, HTTPRequest, HTTPResponse, WebSocket

from .utils import log, escape
from .config import Config as CQHTTPConfig
from .message import Message, MessageSegment
from .event import Reply, Event, MessageEvent, get_event_model
from .exception import NetworkError, ApiNotAvailable, ActionFailed

if TYPE_CHECKING:
    from nonebot.config import Config


def get_auth_bearer(access_token: Optional[str] = None) -> Optional[str]:
    if not access_token:
        return None
    scheme, _, param = access_token.partition(" ")
    if scheme.lower() not in ["bearer", "token"]:
        return None
    return param


async def _check_reply(bot: "Bot", event: "Event"):
    """
    :说明:

      检查消息中存在的回复，去除并赋值 ``event.reply``, ``event.to_me``

    :参数:

      * ``bot: Bot``: Bot 对象
      * ``event: Event``: Event 对象
    """
    if not isinstance(event, MessageEvent):
        return

    try:
        index = list(map(lambda x: x.type == "reply",
                         event.message)).index(True)
    except ValueError:
        return
    msg_seg = event.message[index]
    try:
        event.reply = Reply.parse_obj(await
                                      bot.get_msg(message_id=msg_seg.data["id"]
                                                 ))
    except Exception as e:
        log("WARNING", f"Error when getting message reply info: {repr(e)}", e)
        return
    # ensure string comparation
    if str(event.reply.sender.user_id) == str(event.self_id):
        event.to_me = True
    del event.message[index]
    if len(event.message) > index and event.message[index].type == "at":
        del event.message[index]
    if len(event.message) > index and event.message[index].type == "text":
        event.message[index].data["text"] = event.message[index].data[
            "text"].lstrip()
        if not event.message[index].data["text"]:
            del event.message[index]
    if not event.message:
        event.message.append(MessageSegment.text(""))


def _check_at_me(bot: "Bot", event: "Event"):
    """
    :说明:

      检查消息开头或结尾是否存在 @机器人，去除并赋值 ``event.to_me``

    :参数:

      * ``bot: Bot``: Bot 对象
      * ``event: Event``: Event 对象
    """
    if not isinstance(event, MessageEvent):
        return

    # ensure message not empty
    if not event.message:
        event.message.append(MessageSegment.text(""))

    if event.message_type == "private":
        event.to_me = True
    else:

        def _is_at_me_seg(segment: MessageSegment):
            return segment.type == "at" and str(segment.data.get(
                "qq", "")) == str(event.self_id)

        # check the first segment
        if _is_at_me_seg(event.message[0]):
            event.to_me = True
            event.message.pop(0)
            if event.message and event.message[0].type == "text":
                event.message[0].data["text"] = event.message[0].data[
                    "text"].lstrip()
                if not event.message[0].data["text"]:
                    del event.message[0]
            if event.message and _is_at_me_seg(event.message[0]):
                event.message.pop(0)
                if event.message and event.message[0].type == "text":
                    event.message[0].data["text"] = event.message[0].data[
                        "text"].lstrip()
                    if not event.message[0].data["text"]:
                        del event.message[0]

        if not event.to_me:
            # check the last segment
            i = -1
            last_msg_seg = event.message[i]
            if last_msg_seg.type == "text" and \
                    not last_msg_seg.data["text"].strip() and \
                    len(event.message) >= 2:
                i -= 1
                last_msg_seg = event.message[i]

            if _is_at_me_seg(last_msg_seg):
                event.to_me = True
                del event.message[i:]

        if not event.message:
            event.message.append(MessageSegment.text(""))


def _check_nickname(bot: "Bot", event: "Event"):
    """
    :说明:

      检查消息开头是否存在昵称，去除并赋值 ``event.to_me``

    :参数:

      * ``bot: Bot``: Bot 对象
      * ``event: Event``: Event 对象
    """
    if not isinstance(event, MessageEvent):
        return

    first_msg_seg = event.message[0]
    if first_msg_seg.type != "text":
        return

    first_text = first_msg_seg.data["text"]

    nicknames = set(filter(lambda n: n, bot.config.nickname))
    if nicknames:
        # check if the user is calling me with my nickname
        nickname_regex = "|".join(nicknames)
        m = re.search(rf"^({nickname_regex})([\s,，]*|$)", first_text,
                      re.IGNORECASE)
        if m:
            nickname = m.group(1)
            log("DEBUG", f"User is calling me {nickname}")
            event.to_me = True
            first_msg_seg.data["text"] = first_text[m.end():]


def _handle_api_result(result: Optional[Dict[str, Any]]) -> Any:
    """
    :说明:

      处理 API 请求返回值。

    :参数:

      * ``result: Optional[Dict[str, Any]]``: API 返回数据

    :返回:

        - ``Any``: API 调用返回数据

    :异常:

        - ``ActionFailed``: API 调用失败
    """
    if isinstance(result, dict):
        if result.get("status") == "failed":
            raise ActionFailed(**result)
        return result.get("data")


class ResultStore:
    _seq = 1
    _futures: Dict[int, asyncio.Future] = {}

    @classmethod
    def get_seq(cls) -> int:
        s = cls._seq
        cls._seq = (cls._seq + 1) % sys.maxsize
        return s

    @classmethod
    def add_result(cls, result: Dict[str, Any]):
        if isinstance(result.get("echo"), dict) and \
                isinstance(result["echo"].get("seq"), int):
            future = cls._futures.get(result["echo"]["seq"])
            if future:
                future.set_result(result)

    @classmethod
    async def fetch(cls, seq: int, timeout: Optional[float]) -> Dict[str, Any]:
        future = asyncio.get_event_loop().create_future()
        cls._futures[seq] = future
        try:
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            raise NetworkError("WebSocket API call timeout") from None
        finally:
            del cls._futures[seq]


class Bot(BaseBot):
    """
    CQHTTP 协议 Bot 适配。继承属性参考 `BaseBot <./#class-basebot>`_ 。
    """
    cqhttp_config: CQHTTPConfig

    @property
    @overrides(BaseBot)
    def type(self) -> str:
        """
        - 返回: ``"cqhttp"``
        """
        return "cqhttp"

    @classmethod
    def register(cls, driver: Driver, config: "Config"):
        super().register(driver, config)
        cls.cqhttp_config = CQHTTPConfig(**config.dict())
        if not isinstance(driver, ForwardDriver) and cls.cqhttp_config.ws_urls:
            logger.warning(
                f"Current driver {cls.config.driver} don't support forward connections"
            )
        elif isinstance(driver, ForwardDriver) and cls.cqhttp_config.ws_urls:
            for self_id, url in cls.cqhttp_config.ws_urls.items():
                try:
                    headers = {
                        "authorization":
                            f"Bearer {cls.cqhttp_config.access_token}"
                    } if cls.cqhttp_config.access_token else {}
                    driver.setup_websocket(
                        WebSocketSetup("cqhttp", self_id, url, headers=headers))
                except Exception as e:
                    logger.opt(colors=True, exception=e).error(
                        f"<r><bg #f8bbd0>Bad url {escape_tag(url)} for bot {escape_tag(self_id)} "
                        "in cqhttp forward websocket</bg #f8bbd0></r>")

    @classmethod
    @overrides(BaseBot)
    async def check_permission(
            cls, driver: Driver,
            request: HTTPConnection) -> Tuple[Optional[str], HTTPResponse]:
        """
        :说明:

          CQHTTP (OneBot) 协议鉴权。参考 `鉴权 <https://github.com/howmanybots/onebot/blob/master/v11/specs/communication/authorization.md>`_
        """
        x_self_id = request.headers.get("x-self-id")
        x_signature = request.headers.get("x-signature")
        token = get_auth_bearer(request.headers.get("authorization"))
        cqhttp_config = CQHTTPConfig(**driver.config.dict())

        # 检查self_id
        if not x_self_id:
            log("WARNING", "Missing X-Self-ID Header")
            return None, HTTPResponse(400, b"Missing X-Self-ID Header")

        # 检查签名
        secret = cqhttp_config.secret
        if secret and isinstance(request, HTTPRequest):
            if not x_signature:
                log("WARNING", "Missing Signature Header")
                return None, HTTPResponse(401, b"Missing Signature")
            sig = hmac.new(secret.encode("utf-8"), request.body,
                           "sha1").hexdigest()
            if x_signature != "sha1=" + sig:
                log("WARNING", "Signature Header is invalid")
                return None, HTTPResponse(403, b"Signature is invalid")

        access_token = cqhttp_config.access_token
        if access_token and access_token != token and isinstance(
                request, WebSocket):
            log(
                "WARNING", "Authorization Header is invalid"
                if token else "Missing Authorization Header")
            return None, HTTPResponse(
                403, b"Authorization Header is invalid"
                if token else b"Missing Authorization Header")
        return str(x_self_id), HTTPResponse(204, b'')

    @overrides(BaseBot)
    async def handle_message(self, message: bytes):
        """
        :说明:

          调用 `_check_reply <#async-check-reply-bot-event>`_, `_check_at_me <#check-at-me-bot-event>`_, `_check_nickname <#check-nickname-bot-event>`_ 处理事件并转换为 `Event <#class-event>`_
        """
        data: dict = json.loads(message)

        if not data:
            return

        if "post_type" not in data:
            ResultStore.add_result(data)
            return

        try:
            post_type = data['post_type']
            detail_type = data.get(f"{post_type}_type")
            detail_type = f".{detail_type}" if detail_type else ""
            sub_type = data.get("sub_type")
            sub_type = f".{sub_type}" if sub_type else ""
            models = get_event_model(post_type + detail_type + sub_type)
            for model in models:
                try:
                    event = model.parse_obj(data)
                    break
                except Exception as e:
                    log("DEBUG", "Event Parser Error", e)
            else:
                event = Event.parse_obj(data)

            # Check whether user is calling me
            await _check_reply(self, event)
            _check_at_me(self, event)
            _check_nickname(self, event)

            await handle_event(self, event)
        except Exception as e:
            logger.opt(colors=True, exception=e).error(
                f"<r><bg #f8bbd0>Failed to handle event. Raw: {escape_tag(str(data))}</bg #f8bbd0></r>"
            )

    @overrides(BaseBot)
    async def _call_api(self, api: str, **data) -> Any:
        log("DEBUG", f"Calling API <y>{api}</y>")
        if isinstance(self.request, WebSocket):
            seq = ResultStore.get_seq()
            json_data = json.dumps(
                {
                    "action": api,
                    "params": data,
                    "echo": {
                        "seq": seq
                    }
                },
                cls=DataclassEncoder)
            await self.request.send(json_data)
            return _handle_api_result(await ResultStore.fetch(
                seq, self.config.api_timeout))

        elif isinstance(self.request, HTTPRequest):
            api_root = self.config.api_root.get(self.self_id)
            if not api_root:
                raise ApiNotAvailable
            elif not api_root.endswith("/"):
                api_root += "/"

            headers = {"Content-Type": "application/json"}
            if self.cqhttp_config.access_token is not None:
                headers[
                    "Authorization"] = "Bearer " + self.cqhttp_config.access_token

            try:
                async with httpx.AsyncClient(headers=headers) as client:
                    response = await client.post(
                        api_root + api,
                        content=json.dumps(data, cls=DataclassEncoder),
                        timeout=self.config.api_timeout)

                if 200 <= response.status_code < 300:
                    result = response.json()
                    return _handle_api_result(result)
                raise NetworkError(f"HTTP request received unexpected "
                                   f"status code: {response.status_code}")
            except httpx.InvalidURL:
                raise NetworkError("API root url invalid")
            except httpx.HTTPError:
                raise NetworkError("HTTP request failed")

    @overrides(BaseBot)
    async def call_api(self, api: str, **data) -> Any:
        """
        :说明:

          调用 CQHTTP 协议 API

        :参数:

          * ``api: str``: API 名称
          * ``**data: Any``: API 参数

        :返回:

          - ``Any``: API 调用返回数据

        :异常:

          - ``NetworkError``: 网络错误
          - ``ActionFailed``: API 调用失败
        """
        return await super().call_api(api, **data)

    @overrides(BaseBot)
    async def send(self,
                   event: Event,
                   message: Union[str, Message, MessageSegment],
                   at_sender: bool = False,
                   **kwargs) -> Any:
        """
        :说明:

          根据 ``event``  向触发事件的主体发送消息。

        :参数:

          * ``event: Event``: Event 对象
          * ``message: Union[str, Message, MessageSegment]``: 要发送的消息
          * ``at_sender: bool``: 是否 @ 事件主体
          * ``**kwargs``: 覆盖默认参数

        :返回:

          - ``Any``: API 调用返回数据

        :异常:

          - ``ValueError``: 缺少 ``user_id``, ``group_id``
          - ``NetworkError``: 网络错误
          - ``ActionFailed``: API 调用失败
        """
        message = escape(message, escape_comma=False) if isinstance(
            message, str) else message
        msg = message if isinstance(message, Message) else Message(message)

        at_sender = at_sender and bool(getattr(event, "user_id", None))

        params = {}
        if getattr(event, "user_id", None):
            params["user_id"] = getattr(event, "user_id")
        if getattr(event, "group_id", None):
            params["group_id"] = getattr(event, "group_id")
        params.update(kwargs)

        if "message_type" not in params:
            if params.get("group_id", None):
                params["message_type"] = "group"
            elif params.get("user_id", None):
                params["message_type"] = "private"
            else:
                raise ValueError("Cannot guess message type to reply!")

        if at_sender and params["message_type"] != "private":
            params["message"] = MessageSegment.at(params["user_id"]) + " " + msg
        else:
            params["message"] = msg
        return await self.send_msg(**params)
