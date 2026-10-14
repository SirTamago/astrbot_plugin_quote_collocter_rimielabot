"""本地测试用的最小 AstrBot 桩。

作用：让 `main.py` 可以在**不安装 AstrBot** 的情况下被导入并调用，
从而在本地跑单元/集成测试。

用法（在测试模块最顶部调用，必须早于 `import main`）::

    from stub_astrbot import install
    env = install()          # 注入桩模块，并把工作目录切到临时目录
    import main

注意：
- `install()` 会把进程工作目录切到临时目录，因为 `main.py` 里
  `QUOTES_ROOT = Path("data") / "plugin_data" / "quote_collocter"` 是相对路径，
  这样测试不会污染仓库里的 data/。
- 桩只实现插件实际用到的那部分接口（装饰器、Comp 组件、request、logger 等），
  不是 AstrBot 的完整模拟，因此它验证的是"插件自身逻辑"，不是框架行为。
"""

from __future__ import annotations

import os
import sys
import tempfile
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# --------------------------------------------------------------------------- #
# 消息组件桩
# --------------------------------------------------------------------------- #
class Plain:
    def __init__(self, text: str = ""):
        self.text = text

    def __repr__(self):
        return f"Plain({self.text!r})"


class Image:
    """图片组件桩：支持 fromFileSystem 与 convert_to_file_path。"""

    def __init__(self, path: str | None = None, file: str | None = None,
                 url: str | None = None):
        self.path = path
        self.file = file
        self.url = url

    @staticmethod
    def fromFileSystem(path):
        return Image(path=str(path))

    async def convert_to_file_path(self):
        if not self.path or not os.path.exists(self.path):
            raise RuntimeError("本地缓存不存在")
        return self.path

    def __repr__(self):
        return f"Image({self.path or self.file or self.url})"


class At:
    def __init__(self, qq=None):
        self.qq = qq


class Reply:
    def __init__(self, id=None):
        self.id = id


class Poke:
    def __init__(self, id=None):
        self.id = id


# --------------------------------------------------------------------------- #
# 事件与消息对象桩
# --------------------------------------------------------------------------- #
class _MessageObj:
    def __init__(self, group_id="100", user_id="10001", message=None,
                 raw_message=None, message_id="m1"):
        self.group_id = group_id
        self.message = message or []
        self.raw_message = raw_message
        self.message_id = message_id
        self.sender = types.SimpleNamespace(user_id=user_id)


class _Api:
    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []

    async def call_action(self, action, **kwargs):
        self.calls.append((action, kwargs))
        return self.responses.get(action)


class _Bot:
    def __init__(self, responses=None):
        self.api = _Api(responses)


class AstrMessageEvent:
    """测试用事件：记录 yield 出去的结果，并提供 handler 需要的接口。"""

    def __init__(self, group_id="100", user_id="10001", message=None,
                 raw_message=None, message_str="", message_id="m1",
                 responses=None, bot_id="99999"):
        self.message_obj = _MessageObj(
            group_id=group_id,
            user_id=user_id,
            message=message or [],
            raw_message=raw_message,
            message_id=message_id,
        )
        self.message_str = message_str
        self.bot = _Bot(responses)
        self.self_id = bot_id
        self.results = []
        self.sent = []
        self.stopped = False

    # --- 插件用到的 event 接口 ---
    def get_sender_id(self):
        return self.message_obj.sender.user_id

    def get_self_id(self):
        return self.self_id

    def plain_result(self, text):
        return ("plain", text)

    def chain_result(self, chain):
        return ("chain", list(chain))

    def stop_event(self):
        self.stopped = True

    async def send(self, result):
        self.sent.append(result)


class AiocqhttpMessageEvent(AstrMessageEvent):
    pass


def collect(agen):
    """同步跑完一个只 yield、不含 await 的 handler（兼容旧式用法）。"""
    out = []
    try:
        while True:
            out.append(agen.send(None))
    except StopIteration:
        pass
    return out


async def _drain(agen):
    return [item async for item in agen]


def drive(agen):
    """跑完一个指令/事件 handler（async generator），返回它 yield 出的结果列表。"""
    import asyncio

    return asyncio.run(_drain(agen))


def run(coro):
    """跑一个协程（例如 Web API handler 或普通 async 方法）。"""
    import asyncio

    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# filter / Star / Context / web / logger 桩
# --------------------------------------------------------------------------- #
class EventMessageType:
    GROUP_MESSAGE = "group_message"
    FRIEND_MESSAGE = "friend_message"
    ALL = "all"


class PermissionType:
    ADMIN = "admin"
    MEMBER = "member"


class Filter:
    EventMessageType = EventMessageType
    PermissionType = PermissionType

    @staticmethod
    def _deco(*args, **kwargs):
        def wrap(func):
            return func

        return wrap

    command = staticmethod(_deco)
    event_message_type = staticmethod(_deco)
    permission_type = staticmethod(_deco)
    llm_tool = staticmethod(_deco)


class Logger:
    def __init__(self):
        self.records: list[tuple[str, str]] = []

    def _add(self, level, msg, *args):
        self.records.append((level, str(msg)))

    def info(self, msg, *args):
        self._add("info", msg)

    def warning(self, msg, *args):
        self._add("warn", msg)

    def error(self, msg, *args):
        self._add("error", msg)

    def text(self) -> str:
        return "\n".join(m for _, m in self.records)


class Star:
    def __init__(self, context):
        self.context = context


class Context:
    def __init__(self, admins=("10001",)):
        self.apis: list[tuple[str, list[str]]] = []
        self._config = {"admins_id": list(admins)}

    def register_web_api(self, route, handler, methods, desc):
        self.apis.append((route, list(methods)))

    def get_config(self, umo=None):
        return self._config


class _Query:
    def __init__(self, data=None):
        self._data = data or {}

    def get(self, key, default=None, type=None):  # noqa: A002 - 对齐 astrbot.api.web
        value = self._data.get(key, default)
        if type is not None and value is not None:
            return type(value)
        return value

    def getlist(self, key):
        value = self._data.get(key)
        return value if isinstance(value, list) else ([] if value is None else [value])


class Request:
    """`astrbot.api.web.request` 的桩，测试里可直接改 query / body。"""

    def __init__(self, query=None, body=None):
        self.query = _Query(query)
        self.body = body or {}
        self.method = "GET"
        self.plugin_name = "quote_collocter"

    def reset(self, query=None, body=None):
        """复用同一个 request 对象（main.py 在 import 时已持有引用）。"""
        self.query = _Query(query)
        self.body = body or {}

    async def json(self, default=None):
        return self.body if self.body is not None else (default or {})


def json_response(data=None, **kwargs):
    return {"ok": data}


def error_response(message, **kwargs):
    return {"err": message}


# --------------------------------------------------------------------------- #
# 安装
# --------------------------------------------------------------------------- #
class Env:
    def __init__(self, workdir: Path):
        self.workdir = workdir
        self.logger = Logger()
        self.request = Request()
        self.data_root = workdir / "data" / "plugin_data" / "quote_collocter"

    # 便捷方法
    def group_dir(self, group_id: str) -> Path:
        return self.data_root / str(group_id)

    def put_image(self, group_id: str, filename: str, content: bytes = b"x") -> Path:
        d = self.group_dir(group_id)
        d.mkdir(parents=True, exist_ok=True)
        path = d / filename
        path.write_bytes(content)
        return path

    def read_json(self, path: Path) -> dict:
        import json

        return json.loads(path.read_text(encoding="utf-8"))


def _make_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


_ENV: "Env | None" = None


def install(workdir: str | Path | None = None) -> Env:
    """注入 AstrBot 桩模块：必须在 `import main` 之前调用。

    可重复调用（多个测试模块共用一个进程时返回同一个环境）。
    """
    global _ENV
    if _ENV is not None:
        return _ENV

    work = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="quote_plugin_test_"))
    work.mkdir(parents=True, exist_ok=True)
    os.chdir(work)

    env = Env(work)

    _make_module("aiohttp", ClientSession=object)
    _make_module("astrbot")
    _make_module("astrbot.api", AstrBotConfig=dict, logger=env.logger)
    _make_module(
        "astrbot.api.message_components",
        Plain=Plain,
        Image=Image,
        At=At,
        Reply=Reply,
        Poke=Poke,
    )
    _make_module(
        "astrbot.api.event",
        AstrMessageEvent=AstrMessageEvent,
        filter=Filter,
    )
    _make_module("astrbot.api.star", Context=Context, Star=Star)
    _make_module(
        "astrbot.api.web",
        json_response=json_response,
        error_response=error_response,
        request=env.request,
    )
    _make_module("astrbot.core")
    _make_module("astrbot.core.platform")
    _make_module("astrbot.core.platform.sources")
    _make_module("astrbot.core.platform.sources.aiocqhttp")
    _make_module(
        "astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event",
        AiocqhttpMessageEvent=AiocqhttpMessageEvent,
    )

    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    _ENV = env
    return env
