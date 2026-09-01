# 演示语音素材

英文演示用的两段提问。**它们是数据，不是替身**：音频喂进去之后，走的是
麦克风采样之后**完全相同**的那条链路 —— 同一个 faster-whisper、同一个
lens_agent、同一个 DeepSeek、同一个排版引擎、同一套帧下发。

| 文件 | 台词 | 演示的东西 |
|---|---|---|
| `en-weather.wav` | *What's the weather like today? Do I need a jacket?* | 真工具调用（S5 天气态）+ 真实时数据；答案一页放得下 |
| `en-navigation.wav` | *How do I get to Golden Gate Park from downtown?* | 一整段有用的回答，一页放得下 |
| `en-park.wav` | *What are the top five things to see in Golden Gate Park?* | 长回答分页：页脚 `1/2 ›`，可翻页 |
| `en-remind.wav` | *Remind me in twenty seconds to check the oven.* | **写能力**：真的排了一条提醒，20 秒后自己上屏（`S9 Lens ◆ Reminder`）|

`en-remind.wav` 要配 `--linger` 才看得到那一帧 —— 提醒到点是**后来**发生的事，
那一轮早就结束了：

```sh
cd gateway && .venv/bin/python ../demo/verify_audio.py ../demo/audio/en-remind.wav --linger=35
```

## 怎么生成的

macOS 内置 TTS，16kHz 单声道 PCM —— 与眼镜四麦阵列的上行格式一致
（`docs/HARDWARE-SPEC.md`：*4-mic array, single stream, 16 kHz PCM*）：

```sh
say -v Samantha -o /tmp/q.aiff "What's the weather like today? Do I need a jacket?"
ffmpeg -i /tmp/q.aiff -ar 16000 -ac 1 -c:a pcm_s16le demo/audio/en-weather.wav
```

## 怎么用

浏览器夹具里把 `navigator.mediaDevices.getUserMedia` 换成一个由本文件解码
喂出来的 `MediaStreamDestination`。**插件代码一行没改** —— `mock.ts` 里
`getUserMedia → AudioContext → 重采样 → pushHubEvent('audioEvent')` 那条链路
原样跑，只是麦克风换成了文件。

命令行复验（Python 设备模拟器，直接连 `/ws`）：

```sh
cd gateway && .venv/bin/python ../demo/verify_audio.py ../demo/audio/en-weather.wav
```
