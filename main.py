import base64
import io
import re
from os import path
import os
from pathlib import Path
import threading
import time
import uuid
import wave
from queue import Queue, Empty
from pyexpat.errors import messages

from websocket._exceptions import WebSocketConnectionClosedException
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig
import dashscope
from dashscope.audio.tts_v2 import VoiceEnrollmentService, SpeechSynthesizerObjectPool
from dashscope.audio.tts_v2 import AudioFormat as SpeechSynthesizerAudioFormat

from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.provider.entities import LLMResponse
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
import astrbot.api.message_components as Comp
from dashscope.audio.qwen_tts_realtime import QwenTtsRealtime, QwenTtsRealtimeCallback, AudioFormat
from collections import defaultdict, deque

SSML_PROMPOT_TEMPLATE = r"""
<history>
{history}
</history>

<rule>
【必须遵守的 SSML 规则）】

1. 所有内容必须在 <speak></speak> 内；可用一个或者多个<speak>达到复杂的组合，不要嵌套 <speak>。
2. 只能使用以下标签：<speak>, <break/>, <sub>, <phoneme>, 其他一律禁止。
3. <speak> 允许的属性只有这些（其余禁止）：

   * rate：语速，尽量较小变化，取值为 [0.5,2] 的小数，例如 0.9 / 1 / 1.05 / 1.1 / 1.1005
   * pitch：音高，尽量较小变化，取值为 [0.5,2] 的小数，例如 0.9 / 1 / 1.05 / 1.1 / 1.1005
   * volume：音量，取值为 [0,100] 的整数，例如 40 / 50 / 80
   * effect：可选音效（robot/lolita/lowpass/echo/eq/lpfilter/hpfilter）
   * effectValue：当 effect 为 eq/lpfilter/hpfilter 时按规范填写

4. <break time="..."/> 只允许：

   * 秒：1s~10s 的整数秒
   * 毫秒：50ms~10000ms 的整数毫秒
     连续 break 总时长不要超过 10s（超过会被截断）。
5. <phoneme alphabet="string" ph="string">文本</phoneme> :
   * alphabet 只允许 
      - "py"：拼音
      - "cmu"：音标
   * ph : 指定具体的拼音或音标,字与字的拼音用空格分隔，拼音的数目必须与字数一致,每个拼音由发音部分和音调组成，其中音调为 1 到 5 的整数，5 表示轻声。
   ```xml
   <speak>
   去<phoneme alphabet="py" ph="dian3 dang4 hang2">典当行</phoneme>把这个玩意<phoneme alphabet="py" ph="dang4 diao4">当掉</phoneme>
   </speak>

   <speak>
   How to spell <phoneme alphabet="cmu" ph="S AY N">sin</phoneme>?
   </speak>
   ```
6. <sub alias="string"></sub> :
   * alias：将某段文本替换为更适合朗读的文本。
   如：将 “W3C” 读成 “网络协议标准”
   ```xml
   <speak>
      <sub alias="网络协议标准">W3C</sub>
   </speak>
   ```
5. XML 特殊字符必须转义：& -> &  < -> <  > -> >  " -> "  ' -> '
6. 避免输出 emoji / 特殊符号（如 🔥），必要时改为文字表达（例如“火焰”）。
</rule>

请将以下文本翻译为 {target_language}并为其添加适合语音合成的 SSML 格式，提升语音效果：

{text}

严格确保输出格式仅为```xml包裹，如：
```xml
<speak>
...SSML内容...
</speak>
<speak>
...SSML内容...
</speak>
```
"""


class QAQAliyunttsPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.data_path: Path = Path(get_astrbot_data_path()) / "plugin_data" / "astrbot_plugin_qaqaliyuntts"
        self.data_path.mkdir(parents=True, exist_ok=True)
        if self.config is not None:
            print(f"[astrbot_plugin_qaqaliyuntts] 插件已加载 ，当前配置：{self.config}")

        self.trigger_probability = self.config.get("trigger_probability", 0.3)
        self.min_text_length = self.config.get("min_text_length", 2)
        self.enable = self.config.get("enable", False)
        if not self.enable:
            logger.info("[astrbot_plugin_qaqaliyuntts] 插件未启用")
            return
        self.max_saved_audios = self.config.get("max_saved_audios", 20)
        self.dashscope = self.config.get("dashscope", {})
        self.dashscope_api_key = self.dashscope.get("api_key", "")
        dashscope.api_key = self.dashscope_api_key
        self.dashscope_backend_type = self.dashscope.get("backend", "cosy")
        self.vioce_model = self.dashscope.get("model", "cosyvoice-v3-flash")
        self.voice_language = self.dashscope.get("voice_language", "")

        if self.dashscope_backend_type == "cosy":
            self.cosy_voice = self.dashscope.get("cosy_voice", "")
            self._cosy_pool = SpeechSynthesizerObjectPool(10)
        else:
            self.qwen_voice = self.dashscope.get("qwen_voice", "")
            self._qwen_pool = QwenTTSBackendPool(
                max_size=10,
                api_key=self.dashscope_api_key,
                model=self.vioce_model,
                voice=self.qwen_voice,
            )

        self.preprocess_config = self.config.get("preprocess", {})
        self.enable_preprocess = self.preprocess_config.get("enable", False)

        self.preprocess_provider_id = self.preprocess_config.get("provider_id", "")
        self.preprocess_system_prompt = self.preprocess_config.get("system_prompt", "")
        self.preprocess_target_language = self.preprocess_config.get("target_language", "中文")
        self.prompt_template = self.preprocess_config.get("prompt", "请将以下文本翻译为 {target_language}：\n\n{text}")
        self.enable_SSML = self.preprocess_config.get("enable_SSML", False)
        self.SSML_prompt = self.preprocess_config.get("SSML_prompt", SSML_PROMPOT_TEMPLATE)
        self.SSML_history_length = self.preprocess_config.get("SSML_history_length", 20)
        self.SSML_regex = self.preprocess_config.get("SSML_regex", r"```xml\s*([\s\S]*?)\s*```")

        self.hist = defaultdict(lambda: deque(maxlen=self.SSML_history_length))

    async def initialize(self):
        """可选择实现异步的插件初始化方法，当实例化该插件类之后会自动调用该方法。"""
        pass

    @filter.event_message_type(filter.EventMessageType.ALL)  # 收到用户消息时
    async def record_incoming(self, event: AstrMessageEvent):
        sid = event.message_obj.session_id
        user_text = event.get_message_outline()
        send = (event.message_obj.sender.user_id, event.message_obj.sender.nickname, user_text)
        if user_text:
            self.hist[sid].append(("user", send))

    @filter.on_llm_response()
    async def send_tts(self, event: AstrMessageEvent, resp: LLMResponse):
        """处理消息并进行语音合成。"""
        sid = event.message_obj.session_id
        text = resp.completion_text
        if text is None or text == "":
            return
        send = (event.message_obj.sender.user_id, event.message_obj.sender.nickname, text)
        self.hist[sid].append(("robot", send))
        if not self.enable:
            return
        if self.trigger_probability < 1.0:
            import random
            rand_val = random.random()
            if rand_val > self.trigger_probability:
                logger.info(f"[astrbot_plugin_qaqaliyuntts] 未触发语音合成，随机值：{rand_val:.4f}，触发概率：{self.trigger_probability}")
                return
        # chain = result.chain
        logger.info("[astrbot_plugin_qaqaliyuntts] 开始处理消息，进行语音合成")
        
        if not text or len(text) < self.min_text_length:
            logger.info(f"[astrbot_plugin_qaqaliyuntts] 文本长度不足，跳过语音合成，文本长度：{len(text) if text else 0}")
            return
        if self.enable_preprocess:
            text = await self.clean_text_by_ai(text, session_id=sid)
        # 如果正则提取失败（或被清洗成空），就不继续
        if not text or not text.strip():
            logger.info("[astrbot_plugin_qaqaliyuntts] 预处理结果为空/SSML提取失败，跳过语音合成")
            return
        logger.info(f"[astrbot_plugin_qaqaliyuntts] 预处理后的文本：{text}")
        wav_path = self.get_wav_by_tts(text)
        logger.info(f"[astrbot_plugin_qaqaliyuntts] 语音合成完成，音频文件路径：{wav_path}")
        if not wav_path:
            logger.error("[astrbot_plugin_qaqaliyuntts] 语音合成失败，未获取到音频文件")
            return
        if not os.path.exists(wav_path):
            logger.error(f"[astrbot_plugin_qaqaliyuntts] 语音合成失败，音频文件不存在：{wav_path}")
            return
        m = MessageChain()
        m.chain.append(Comp.Record(file=wav_path, url=wav_path))
        await event.send(m)

    @filter.after_message_sent()
    async def cleanup_audios(self, event: AstrMessageEvent):
        """清理多余的音频文件。"""
        if not self.enable:
            return
        if self.max_saved_audios < 0:
            return
        audio_files = sorted(
            [f for f in os.listdir(self.data_path) if f.endswith('.wav')],
            key=lambda x: os.path.getmtime(os.path.join(self.data_path, x))
        )
        while len(audio_files) > self.max_saved_audios:
            file_to_remove = audio_files.pop(0)
            try:
                os.remove(os.path.join(self.data_path, file_to_remove))
                logger.info(f"[astrbot_plugin_qaqaliyuntts] 已删除多余音频文件：{file_to_remove}")
            except Exception as e:
                logger.error(f"[astrbot_plugin_qaqaliyuntts] 删除音频文件失败：{file_to_remove}，错误：{e}")
        
    async def terminate(self):
        """可选择实现异步的插件销毁方法，当插件被卸载/停用时会调用。"""
        if not getattr(self, "enable", False):
            return
        if getattr(self, "_qwen_pool", None):
            try:
                self._qwen_pool.close_all()
                logger.info("[astrbot_plugin_qaqaliyuntts] Qwen 连接池已关闭")
            except Exception as e:
                logger.warning(f"[astrbot_plugin_qaqaliyuntts] 关闭 Qwen 连接池失败：{e}")
        if getattr(self, "_cosy_pool", None):
            try:
                self._cosy_pool.shutdown()
                logger.info("[astrbot_plugin_qaqaliyuntts] Cosy 连接池已关闭")
            except Exception as e:
                logger.warning(f"[astrbot_plugin_qaqaliyuntts] 关闭 Cosy 连接池失败：{e}")

    async def clean_text_by_ai(self, text: str, **kwargs) -> str:
        """使用 LLM 对文本进行预处理，返回处理后的文本。"""
        usr_prompt = self.prompt_template.format(
            text=text,
            target_language=self.preprocess_target_language,
        )
        if self.enable_SSML:
            sid = kwargs.get("session_id", "")
            items = self.hist.get(sid, [])
            items = list(items)[:-1]  # 排除最后一句

            history = []
            for role, (user_id, nickname, msg_text) in items:
                if role == "user":
                    history.append(f"{nickname}（{user_id}）：\n{msg_text}")
                else:
                    history.append(f"assistant: \n{msg_text}")

            usr_prompt = self.SSML_prompt.format(
                history="\n".join(history),
                text=text,
                target_language=self.preprocess_target_language,
            )
        # logger.info(f"[astrbot_plugin_qaqaliyuntts] 预处理提示词：\n{usr_prompt}")
        llm_resp = await self.context.llm_generate(
            chat_provider_id=self.preprocess_provider_id,
            prompt=usr_prompt,
        )
        logger.info(f"[astrbot_plugin_qaqaliyuntts] 预处理 LLM 输出：\n{llm_resp.completion_text}")
        # 开启 SSML 时：必须命中正则，否则不继续
        if self.enable_SSML:
            out = (llm_resp.completion_text or "").strip()
            try:
                m = re.search(self.SSML_regex, out)
            except re.error as e:
                logger.error(f"[astrbot_plugin_qaqaliyuntts] SSML_regex 无效：{e}")
                return ""
            
            if not m:
                logger.info("[astrbot_plugin_qaqaliyuntts] 正则未命中任何 SSML 内容，终止后续流程")
                return ""

            ssml_block = (m.group(1) if m.lastindex else m.group(0)).strip()
            if ssml_block.startswith("```"):
                ssml_block = re.sub(r"^```(?:xml)?\s*", "", ssml_block)
                ssml_block = re.sub(r"\s*```$", "", ssml_block)
            # 额外兜底：确保最终至少包含 <speak>...</speak>
            if "<speak" not in ssml_block or "</speak>" not in ssml_block:
                logger.info("[astrbot_plugin_qaqaliyuntts] 提取结果不含 <speak>...</speak>，终止后续流程")
                return ""
            return ssml_block.strip()
        return llm_resp.completion_text
    
    def get_wav_by_tts(self, text: str) -> str:
        """获取音频文件的完整路径。"""
        if self.dashscope_backend_type == "cosy":
            audio_data = None
            synthesizer = self._cosy_pool.borrow_synthesizer(
                model=self.vioce_model,
                voice=self.cosy_voice,
                format=SpeechSynthesizerAudioFormat.WAV_44100HZ_MONO_16BIT,
            )
            try:
                audio_data = synthesizer.call(text)
            except WebSocketConnectionClosedException:
                self._cosy_pool.return_synthesizer(synthesizer)
                synthesizer = self._cosy_pool.borrow_synthesizer(
                    model=self.vioce_model,
                    voice=self.cosy_voice,
                    format=SpeechSynthesizerAudioFormat.WAV_44100HZ_MONO_16BIT,
                )
                audio_data = synthesizer.call(text)
            finally:
                if synthesizer is not None:
                    self._cosy_pool.return_synthesizer(synthesizer)
        else:
            audio_data = None
            backend = None
            try:
                backend = self._qwen_pool.borrow_backend()
                audio_data = backend.call(text)
            except WebSocketConnectionClosedException:
                if backend is not None:
                    self._qwen_pool.discard_backend(backend)
                backend = self._qwen_pool.borrow_backend()
                audio_data = backend.call(text)
            finally:
                if backend is not None:
                    self._qwen_pool.return_backend(backend)
            if audio_data:
                audio_data = self._pcm_to_wav(audio_data, sample_rate=24000)
        if not audio_data:
            return ""
        file_name = f"{time.time()}_{uuid.uuid4()}.wav"
        output_path = path.join(self.data_path, file_name)
        with open(output_path, 'wb') as f:
            f.write(audio_data)
        return output_path

    @staticmethod
    def _pcm_to_wav(pcm_bytes: bytes, sample_rate: int, channels: int = 1, sampwidth: int = 2) -> bytes:
        with io.BytesIO() as buf:
            with wave.open(buf, "wb") as wf:
                wf.setnchannels(channels)
                wf.setsampwidth(sampwidth)
                wf.setframerate(sample_rate)
                wf.writeframes(pcm_bytes)
            return buf.getvalue()

class QwenTTSBackend:
    def __init__(self, api_key: str, model: str, voice: str):
        self.model = model
        self.voice = voice
        self._callback = CollectBytesCallback()
        if api_key:
            dashscope.api_key = api_key
        self.qwen_tts_realtime = QwenTtsRealtime(
            model=self.model,
            callback=self._callback,
            # 以下为北京地域url，若使用新加坡地域的模型，需将url替换为：wss://dashscope-intl.aliyuncs.com/api-ws/v1/realtime
            url='wss://dashscope.aliyuncs.com/api-ws/v1/realtime'
        )
        self.qwen_tts_realtime.connect()

    def call(self, text: str):
        if not text:
            return b""
        self._callback.reset()
        self.qwen_tts_realtime.update_session(
            voice=self.voice or "Cherry",
            response_format=AudioFormat.PCM_24000HZ_MONO_16BIT,
            mode='server_commit',
        )
        self.qwen_tts_realtime.append_text(text)
        self.qwen_tts_realtime.finish()
        self._callback.wait_for_finished(timeout=30)
        if not self._callback.complete_event.is_set():
            return b""
        return self._callback.audio_bytes

    def close(self) -> None:
        try:
            if hasattr(self.qwen_tts_realtime, "close"):
                self.qwen_tts_realtime.close()
            elif hasattr(self.qwen_tts_realtime, "disconnect"):
                self.qwen_tts_realtime.disconnect()
        except Exception as e:
            logger.warning(f"[astrbot_plugin_qaqaliyuntts] 关闭 Qwen 实例失败：{e}")


class QwenTTSBackendPool:
    def __init__(self, max_size: int, api_key: str, model: str, voice: str):
        self._queue: Queue[QwenTTSBackend] = Queue(maxsize=max_size)
        self._max_size = max_size
        self._created = 0
        self._lock = threading.Lock()
        self._api_key = api_key
        self._model = model
        self._voice = voice
        self._all: list[QwenTTSBackend] = []

    def borrow_backend(self) -> QwenTTSBackend:
        try:
            return self._queue.get_nowait()
        except Empty:
            with self._lock:
                if self._created < self._max_size:
                    self._created += 1
                    backend = QwenTTSBackend(
                        api_key=self._api_key,
                        model=self._model,
                        voice=self._voice,
                    )
                    self._all.append(backend)
                    return backend
            return self._queue.get()

    def return_backend(self, backend: QwenTTSBackend) -> None:
        self._queue.put(backend)

    def discard_backend(self, backend: QwenTTSBackend) -> None:
        del backend

    def close_all(self) -> None:
        with self._lock:
            backends = list(self._all)
            self._all.clear()
            self._created = 0
        for backend in backends:
            backend.close()


class CollectBytesCallback(QwenTtsRealtimeCallback):
    """只收集流式音频，不播放。"""
    def __init__(self):
        self.complete_event = threading.Event()
        self._chunks: list[bytes] = []
        self._last_error: Exception | None = None

    @property
    def audio_bytes(self) -> bytes:
        return b"".join(self._chunks)

    def on_open(self) -> None:
        print("[TTS] 连接已建立")

    def on_close(self, close_status_code, close_msg) -> None:
        print(f"[TTS] 连接关闭 code={close_status_code}, msg={close_msg}")

    def on_event(self, response: dict) -> None:
        try:
            event_type = response.get("type", "")
            if event_type == "response.audio.delta":
                # delta 通常是 base64 的音频分片（常见为 PCM16 LE）
                self._chunks.append(base64.b64decode(response["delta"]))
            elif event_type == "response.done":
                # 有些 SDK 会在这里表示一次 response 完结
                pass
            elif event_type == "session.finished":
                self.complete_event.set()
        except Exception as e:
            self._last_error = e
            self.complete_event.set()

    def wait_for_finished(self, timeout: float | None = None):
        self.complete_event.wait(timeout=timeout)
        if self._last_error:
            raise self._last_error

    def reset(self) -> None:
        self.complete_event.clear()
        self._chunks.clear()
        self._last_error = None
