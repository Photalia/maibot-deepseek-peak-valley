# 梁文峰／梁文谷：DeepSeek 峰谷价格播报

一个不需要 LLM、但非常需要幽默感的 MaiBot 插件。

它会根据北京时间判断 DeepSeek API 当前处于高峰还是空闲时段，将其分别称为：

- 高峰：**梁文峰**，饮料瓶指标为「五梁液」
- 空闲：**梁文谷**，饮料瓶评估为「梁白开」

> 本项目是非官方社区插件，与 DeepSeek 官方无隶属关系。价格数据来源于 [DeepSeek API 官方价格文档](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)，最后核对日期为 2026-08-18；官方价格变化后需要更新插件。

## 功能

- `/峰谷`：实时查询当前时段、V4 Flash／Pro 价格及下一次切换时间。
- 定时播报：北京时间 `09:00`、`12:00`、`14:00`、`18:00` 自动向配置的 QQ 群推送。
- 双层防刷：同时限制同一用户和同一聊天流的查询频率。
- 持久化去重：重启不会重复发送已经成功推送的切换通知。
- 独立失败重试：某个目标群发送失败时，只重试失败群，不重复轰炸成功群。
- 零外部依赖：不调用 AI，也不请求 DeepSeek API。

## 官方峰谷时段

北京时间：

| 时段 | 名称 | V4 Flash（输入／输出／缓存命中） | V4 Pro（输入／输出／缓存命中） |
|---|---|---|---|
| 09:00–12:00、14:00–18:00 | 梁文峰 | 3／9／0.1 元 | 9／27／0.3 元 |
| 其余时间 | 梁文谷 | 1.5／4.5／0.05 元 | 4.5／13.5／0.15 元 |

价格单位均为 **每百万 tokens，人民币**。

## 输出示例

```text
当前时间是 12:00
处于「梁文谷」时段
V4 Flash:输入1.5输出4.5缓存0.05
V4 Pro:输入4.5输出13.5缓存0.15
下一次「梁文峰」时间在2小时后
饮料瓶评估:梁白开
```

## 安装

### 插件广场

在 MaiBot 插件广场搜索「DeepSeek 峰谷价格播报」并安装。

### 手动安装

在 MaiBot 的 `plugins` 目录执行：

```bash
git clone https://github.com/Photalia/maibot-deepseek-peak-valley.git local_deepseek-peak-valley
```

随后重启 MaiBot。插件要求：

- MaiBot `>= 1.1.4`
- MaiBot Plugin SDK `>= 2.5.0`

## 配置

MaiBot 可根据插件配置模型自动生成 `config.toml`。也可以手动复制示例：

```bash
cp config.example.toml config.toml
```

主要配置：

```toml
[plugin]
enabled = true
config_version = "1.0.0"

[schedule]
enabled = true
# 需要主动播报的 QQ 群号；留空时只启用 /峰谷。
target_group_ids = ["123456789", "987654321"]
# 切换后允许补发及重试的秒数。
grace_seconds = 120

[rate_limit]
# 同一用户 30 秒内只响应一次。
user_cooldown_seconds = 30
# 同一聊天流 5 秒内只响应一次。
stream_cooldown_seconds = 5
```

冷却期间的重复命令会被静默吞掉，避免「查询太频繁」提示本身成为刷屏内容。

## 定时逻辑

切换时间为：

- `09:00`：进入梁文峰
- `12:00`：进入梁文谷
- `14:00`：进入梁文峰
- `18:00`：进入梁文谷

插件使用固定 UTC+8 计算北京时间，不依赖宿主机当前时区。每个群的发送状态分别记录在插件 `data/state.json` 中。

## 与睡眠／宵禁插件共存

如果另一个插件会在睡眠期间全局拦截入站命令或出站消息，那么 `09:00` 定时播报或睡眠期间的 `/峰谷` 也可能被它拦截。这属于拦截插件的全局策略；请在对应插件中为 `/峰谷` 和本插件的系统播报配置白名单。

本仓库不会自动修改其他插件。

## 隐私与网络

- 不读取聊天历史。
- 不收集用户数据。
- 不调用任何 LLM。
- 不访问 DeepSeek 或其他外部 API。
- 仅使用 MaiBot 的聊天流查询与文本发送能力。

## 开发与测试

```bash
python -m py_compile plugin.py
python tests/test_pricing.py
```

## License

[MIT](LICENSE)
