## QAQ Aliyun TTS 插件

阿里云百炼（Dashscope）TTS 语音合成插件，支持 CosyVoice 模型，生成音频后自动以语音消息发送。

## 功能
- 文本转语音（CosyVoice）
- 可选预处理（LLM 翻译/清洗）
- 自动清理历史音频文件

## 配置
在插件配置中填写 Dashscope API Key 与模型配置。

示例（节选）：
```json
{
  "enable": true,
  "max_saved_audios": 20,
  "dashscope": {
    "api_key": "YOUR_DASHSCOPE_API_KEY",
    "model": "cosyvoice-v3-flash",
    "backend": "cosy",
    "cosy_voice": "你的音色ID"
  },
  "preprocess": {
    "enable": false,
    "target_language": "中文",
    "provider_id": "",
    "prompt": "请将以下文本翻译为 {target_language}：\n\n{text}"
  }
}
```

## 使用
启用插件后，机器人在发送文本消息时会概率触发语音合成，并在消息链中追加语音记录。

音频默认保存到：
`data/plugin_data/astrbot_plugin_qaqaliyuntts/`

## 注意事项
- 需要有效的 Dashscope API Key。
- 预处理功能依赖 AstrBot 的 LLM Provider 配置。

## TODO
- Qwen TTS 实时合成尚未完成，暂不可用。
