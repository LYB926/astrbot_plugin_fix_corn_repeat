# astrbot_plugin_fix_corn_repeat

AstrBot “未来任务”重复发送止血插件。

当一次 `active_agent` Cron 任务已经通过内建 `send_message_to_user`
工具成功发送消息后，本插件会把该工具结果转换为 AstrBot Runner 的终止信号，
并拦截同一任务执行中剩余的重复发送，避免一分钟内持续刷屏。

> 这是针对 AstrBot 4.26.6 的临时兼容性修复，不是 AstrBot 上游的正式补丁。
> 仓库名中的 `corn` 为既有仓库标识；文档中的功能名称统一使用 `Cron`。

## 工作方式

插件加载时会对 AstrBot 内建 `SendMessageToUserTool.call()` 安装一个可逆包装：

1. 仅处理 `CronMessageEvent` 且 `cron_job.type == "active_agent"` 的事件；
2. 调用原始发送工具前，在当前事件中写入 `IN_FLIGHT` 标记；
3. 参数、权限或路径校验返回 `error:` 时清除标记，允许模型纠正参数；
4. 首次发送成功后写入 `DELIVERED`，并返回 `None` 终止 Agent Runner；
5. 同一事件中的后续发送直接返回 `None`，不再触发真实发送。

普通聊天、后台任务完成通知以及非 `active_agent` Cron 任务会原样委托给
AstrBot，不改变其发送行为。

## 要求

- AstrBot `>=4.26.6,<4.27`
- Python 3.12 或 AstrBot 所支持的更高版本

插件没有第三方运行时依赖，也不会修改 AstrBot 的安装文件。

## 安装

在 AstrBot WebUI 的插件管理页面通过仓库地址安装：

```text
https://github.com/LYB926/astrbot_plugin_fix_corn_repeat
```

也可以在 AstrBot 数据目录中手动克隆：

```bash
cd data/plugins
git clone https://github.com/LYB926/astrbot_plugin_fix_corn_repeat.git
```

然后在 WebUI 中重载插件或重启 AstrBot。

日志出现以下文字表示守卫已安装：

```text
Future-task repeat-send guard installed
```

## 行为边界

- 默认语义是：每个 `active_agent` Cron 执行最多成功发送一次。一次工具调用中
  的多个消息组件不受影响，但有意进行多次、多目标发送的任务会被截断。
- 传输层抛出异常时无法判断消息是否已经送达。插件采用 at-most-once 策略，
  会阻止同一执行继续重试；这可能牺牲一次未送达消息，但能避免未知投递状态下刷屏。
- 状态只保存在当前 `CronMessageEvent`。两个独立调度触发、进程重启重放或
  多 AstrBot 实例并发不在本插件的去重范围内。
- 成功后以 `None` 结束 Runner 时，AstrBot 可能记录“没有最终 LLM 回复”一类警告；
  已发送的提醒和任务结束状态不受影响。
- 插件包装的是 AstrBot 私有核心接口。版本范围、异步签名和实际缓存工具都会在
  安装前检查；检测到不兼容时插件会停用守卫并记录错误，不会修改未知版本行为。
- 本版本不包含上下文压缩或工具轮保护逻辑。

禁用、卸载或热重载插件时，它会停用自己的包装，并在仍拥有目标方法时恢复
加载前的方法。

## 背景

本插件处理的现象与 [AstrBot #8789](https://github.com/AstrBotDevs/AstrBot/issues/8789)
相同。上游 [PR #8833](https://github.com/AstrBotDevs/AstrBot/pull/8833) 修复了
Gemini 工具消息转换，但没有建立“主动发送成功后必须终止”的通用控制流保证。

## 开发与测试

```bash
uv venv --python 3.12
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q main.py patching.py tests
```

测试使用假工具和假事件，不会调用真实模型或消息平台。

## License

[MIT](LICENSE)
