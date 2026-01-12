import base64
from os import path
import os
from pathlib import Path
import threading
import time
import uuid
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig
import dashscope
from dashscope.audio.tts_v2 import VoiceEnrollmentService, SpeechSynthesizer
from dashscope.audio.tts_v2 import AudioFormat as SpeechSynthesizerAudioFormat
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
import astrbot.api.message_components as Comp
from dashscope.audio.qwen_tts_realtime import QwenTtsRealtime, QwenTtsRealtimeCallback, AudioFormat

class QAQAliyunttsPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.data_path: Path = Path(get_astrbot_data_path()) / "plugin_data" / "astrbot_plugin_qaqaliyuntts"
        self.data_path.mkdir(parents=True, exist_ok=True)
        if self.config is not None:
            print(f"[astrbot_plugin_qaqaliyuntts] 插件已加载 ，当前配置：{self.config}")

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
            self.voice_backend = SpeechSynthesizer(model=self.vioce_model, voice=self.dashscope.get("cosy_voice", ""), format=SpeechSynthesizerAudioFormat.WAV_44100HZ_MONO_16BIT)
        else:
            self.voice_backend = QwenTTSBackend(api_key=self.dashscope_api_key, model=self.vioce_model, voice=self.dashscope.get("qwen_voice", ""))

        self.preprocess_config = self.config.get("preprocess", {})
        self.enable_preprocess = self.preprocess_config.get("enable", False)

        self.preprocess_provider_id = self.preprocess_config.get("provider_id", "")
        self.preprocess_system_prompt = self.preprocess_config.get("system_prompt", "")
        self.preprocess_target_language = self.preprocess_config.get("target_language", "中文")
        self.prompt_template = self.preprocess_config.get("prompt", "请将以下文本翻译为 {target_language}：\n\n{text}")

    async def initialize(self):
        """可选择实现异步的插件初始化方法，当实例化该插件类之后会自动调用该方法。"""
        pass

    @filter.on_decorating_result()
    async def send_tts(self, event: AstrMessageEvent):
        if not self.enable:
            return
        result = event.get_result()
        if not result:
            return
        # chain = result.chain
        logger.info("[astrbot_plugin_qaqaliyuntts] 开始处理消息，进行语音合成")
        text = result.get_plain_text()
        
        if self.enable_preprocess:
            text = await self.clean_text_by_ai(text)
        logger.info(f"[astrbot_plugin_qaqaliyuntts] 预处理后的文本：{text}")
        wav_path = self.get_wav_by_tts(text)
        logger.info(f"[astrbot_plugin_qaqaliyuntts] 语音合成完成，音频文件路径：{wav_path}")
        if not wav_path:
            logger.error("[astrbot_plugin_qaqaliyuntts] 语音合成失败，未获取到音频文件")
            return
        if not os.path.exists(wav_path):
            logger.error(f"[astrbot_plugin_qaqaliyuntts] 语音合成失败，音频文件不存在：{wav_path}")
            return
        result.chain.append(Comp.Record(file=wav_path, url=wav_path))

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
        pass

    async def clean_text_by_ai(self, text: str) -> str:
        """使用 LLM 对文本进行预处理，返回处理后的文本。"""
        usr_prompt = self.prompt_template.format(
            text=text,
            target_language=self.preprocess_target_language,
        )
        logger.info(f"[astrbot_plugin_qaqaliyuntts] 预处理提示词：\n{usr_prompt}")
        llm_resp = await self.context.llm_generate(
            chat_provider_id=self.preprocess_provider_id,
            prompt=usr_prompt,
        )
        return llm_resp.completion_text
    
    def get_wav_by_tts(self, text: str) -> str:
        """获取音频文件的完整路径。"""
        audio_data = self.voice_backend.call(text)
        if not audio_data:
            return ""
        file_name = f"{time.time()}_{uuid.uuid4()}.wav"
        output_path = path.join(self.data_path, file_name)
        with open(output_path, 'wb') as f:
            f.write(audio_data)
        return output_path


class QwenTTSBackend:
    def __init__(self, api_key: str, model: str, voice: str):
        self.model = model
        self.voice = voice
        self.qwen_tts_realtime = QwenTtsRealtime(
            model=self.model,
            callback=CollectBytesCallback(),
            # 以下为北京地域url，若使用新加坡地域的模型，需将url替换为：wss://dashscope-intl.aliyuncs.com/api-ws/v1/realtime
            url='wss://dashscope.aliyuncs.com/api-ws/v1/realtime'
        )
        self.qwen_tts_realtime.connect()

    def call(self, text: str):
        pass


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