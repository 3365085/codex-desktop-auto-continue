# Codex Desktop 自动继续

[English](README.md)

这个项目在 Linux 上为官方 **Codex Desktop** 提供同线程自动继续能力。
它监视本机新增的 Codex 会话事件；当某一轮因为模型满载、响应流断开或
内部重连耗尽而正式失败时，如果该线程启用了活动的 `/goal`，监视器会调用
Codex app-server 的 Goal 继续机制；普通线程才会向原线程排入一条 `continue`。

它不会重新发送原始问题，不会创建替代聊天，也不会把任务路由到其他模型。

> 这是独立的社区项目，并非 OpenAI 官方产品，也不代表 OpenAI。

## 效果

```text
Codex Desktop 原线程
        │
        │ task_complete + 可重试错误
        ▼
本机 JSONL 会话日志
        │
        ├─ 活动 /goal ──► app-server thread/goal/set(status=active)
        │                  （等同于 Goal 的“继续”语义，不新增用户消息）
        │
        └─ 普通线程 ────► codex queue --thread <原ID> --message continue
                           （仍在原来的 Desktop 对话中继续）
```

默认识别：

- `server_overloaded`
- `Selected model is at capacity`
- `http_connection_failed`
- `response_stream_connection_failed`
- `response_stream_disconnected`
- `response_too_many_failed_attempts`
- `正在重新连接` / `reconnecting` 等文本形式

每个线程默认最多成功自动继续 `10000` 次。程序不添加模型 fallback，也不
修改模型路由。对活动 Goal 的继续不会在聊天记录中插入一条用户可见的
`continue`；它请求的是 Codex 自己的 Goal runtime 继续路径。

## `/goal` 模式下为什么不发送 `continue`

顶部 Goal 的“继续”操作不是重新提问，而是让同一个 Goal 在当前线程空闲时
恢复下一轮。监视器通过 app-server 的 `thread/goal/set`（保持原目标，只把
状态设为 `active`）调用同一类内部继续机制，因此不会污染聊天记录，也不会
丢失 Goal 的目标、预算或线程上下文。

如果 app-server 调用失败，活动 Goal 会回退到同线程 `continue`，优先保证任务
还能向前推进；日志会明确记录这次回退。已暂停、阻塞、完成或受限的 Goal
不会被监视器擅自恢复。

## 普通线程为什么仍使用 `continue`

重复原始问题会把未完成的工作重新解释成一个新任务，可能重复执行命令、
重复修改文件或覆盖已有判断。本项目读取失败会话的准确 thread ID，并把
`continue` 排入同一个线程。原提示、聊天上下文、工作目录以及已经落盘的
修改都会继续保留。

## 系统要求

- Linux，具备 systemd 用户会话
- Python 3.10 或更高版本
- Codex Desktop / ChatGPT Desktop，并且其捆绑运行时支持 `codex queue`；
  或者 `PATH` 中存在兼容的 `codex` 程序

不依赖任何第三方 Python 包。

## 一键安装

克隆仓库后执行：

```bash
./install.sh
```

安装器会：

1. 把监视器复制到用户数据目录；
2. 创建 systemd 用户服务；
3. 立即启动，并设置为登录系统后自动运行。

它不需要 `sudo`，不会修改 Codex 安装文件，也不会自动打开 Desktop。

检查状态：

```bash
systemctl --user status codex-desktop-auto-continue.service --no-pager
```

看到下面两项表示正常：

```text
Loaded: loaded (...; enabled)
Active: active (running)
```

查看实时日志：

```bash
journalctl --user -u codex-desktop-auto-continue.service -f
```

活动 Goal 成功触发时会出现：

```text
[codex-auto-continue] requested Goal continuation on 019... after server_overloaded (1/10000)
```

普通线程成功触发时会出现：

```text
[codex-auto-continue] queued 'continue' on 019... after server_overloaded (1/10000)
```

## 不安装，临时运行

```bash
python3 codex_desktop_auto_continue.py \
  --codex-bin /usr/lib/chatgpt/resources/codex \
  --max-attempts 10000
```

使用 Codex Desktop 时保持这个进程运行。

## 与当前正在运行的会话

不需要重启 Desktop，也不需要重新创建会话。监视器从启动时的会话日志末尾
开始读取；当前会话之后写入的目标失败会被处理。

如果某个会话在监视器启动之前已经停在错误页面，默认不会回头激活它，避免
误把大量历史失败会话全部重新启动。这个会话手动点一次“继续”即可。

不要随意添加 `--scan-existing`。这个选项会读取已有历史错误，只适合在隔离
测试目录或明确限定的会话日志中使用。

## `正在重新连接 1/5` 的处理方式

为了保留同一个线程和已经执行的工具状态，程序不会在第一次显示
`正在重新连接 1/5` 时杀掉 Codex 进程。它会让官方内部重连走完；如果最后
仍然失败并产生 `task_complete.error`，活动 Goal 才会请求 Goal 继续，普通线程
才会向原线程排入 `continue`。

因此它实现的是“内部重试用尽以后仍然继续”，但不会取消原本 `1/5` 内部的
等待时间。若要同时移除内部等待并保留同一轮的精确执行状态，需要修改
app-server 内部重试实现，外部监视器无法安全做到。

## 服务何时启动

服务在用户登录系统后自动运行，不是在打开 Codex 时才启动：

```text
登录系统 → 监视器启动并等待 → 打开 Codex Desktop → 自动监视新事件
```

关闭 Desktop 后监视器仍会安静等待；它不会自动拉起 Desktop。

## 参数

```text
--session-root PATH       会话 JSONL 根目录；默认 CODEX_HOME/sessions
--codex-bin PATH          用于执行 `codex queue` 和 app-server 的程序
--message TEXT            继续消息；默认 continue
--max-attempts N          每个线程成功继续次数；默认 10000
--max-queue-attempts N    每个失败事件的本地排队尝试；默认 10000
--poll-ms N               文件轮询间隔；默认 100 毫秒
--queue-retry-ms N        本地排队失败后的间隔；默认 250 毫秒
--app-server-timeout-ms N Goal 继续请求超时；默认 15000 毫秒
--scan-existing           处理已有错误；对旧日志有误触发风险
--all-clients             同时处理非 Desktop 会话
--dry-run                 只检测和记录，不发送 continue
--once                    扫描一次后退出
--version                 显示版本
```

## 卸载

```bash
./uninstall.sh
```

卸载器只会删除本项目安装的用户服务和程序目录，不会删除 Codex 会话、项目或
聊天记录。

## 测试

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile codex_desktop_auto_continue.py
```

## 更多文档

- [效果与限制](docs/effect-and-limitations.zh-CN.md)
- [故障排查](docs/troubleshooting.zh-CN.md)
- [官方 Codex app-server 文档](https://learn.chatgpt.com/docs/app-server)
- [官方 Codex 故障排查与会话日志文档](https://learn.chatgpt.com/docs/reference/troubleshooting)

## 许可证

[MIT](LICENSE)
