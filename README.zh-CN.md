# Codex Desktop 自动继续

[English](README.md)

这个项目在 Linux 上为官方 **Codex Desktop** 提供同线程自动继续能力。它监视本机新增的 Codex 会话事件；当某一轮因为模型满载、响应流断开或内部重连耗尽而正式失败时，监视器取得原线程的标题，并通过本机 Desktop 窗口执行官方按钮：`/goal` 线程点击顶部“恢复目标”，普通线程点击错误卡片的“重试”。

这是真正的 Desktop 动作，不是另起一个 CLI 会话，也不是向聊天框插入一条黑色的 `continue` 消息。它不会重新发送原始问题，不会创建替代聊天，也不会把任务路由到其他模型。

> 这是独立的社区项目，并非 OpenAI 官方产品，也不代表 OpenAI。

## 效果

```text
Codex Desktop 原线程
        │
        │ task_complete + 可重试错误
        ▼
本机 JSONL 会话日志
        │
        ├─ 活动 /goal ──► Desktop 顶部“恢复目标”
        │                  （原 Goal runtime 继续，不新增用户消息）
        │
        └─ 普通线程 ────► Desktop 错误卡片“重试”
                           （重试失败轮次，保留原线程状态）
```

只有在 Desktop 按钮不可用且没有活动 Goal 时，才会回退为同线程 `codex queue --message continue`；日志会明确写出 fallback。活动 Goal 不会被这种回退消息污染，而是保留事件并继续尝试点击“恢复目标”。

默认识别：

- `server_overloaded`
- `Selected model is at capacity`
- `http_connection_failed`
- `response_stream_connection_failed`
- `response_stream_disconnected`
- `response_too_many_failed_attempts`
- `正在重新连接` / `reconnecting` 等文本形式

每个线程默认最多成功自动继续 `10000` 次。程序不添加模型 fallback，也不修改模型路由。对活动 Goal 的继续不会在聊天记录中插入一条用户可见的 `continue`；它点击的是 Desktop 自己的 Goal runtime 继续路径。

## 为什么必须点击 Desktop 按钮

仅通过一个独立的 app-server 进程执行 `thread/goal/set` 只能改变持久化状态，不能让已经打开的 Desktop 线程启动新的 turn。这正是早期版本“日志显示成功但桌面没有继续”的原因。

现在监视器通过 Electron 本地 loopback inspector 找到 Desktop 窗口，在目标线程中点击真实的“恢复目标”或“重试”按钮，因此会产生 Desktop 自己的 `thread_goal_updated`、`task_started` 和内部 Goal context。监视器会在第一次需要时向当前 Desktop 主进程发送 `SIGUSR1`，让 Node inspector 只监听 `127.0.0.1:9229`；不需要安装第三方 Python 包，也不修改 Desktop 安装文件。

如果本轮暂时性失败让 Goal 变成 Desktop 中显示的“目标已停滞”，监视器会点击顶部“恢复目标”。手动暂停、完成或受用量限制的 Goal 不会被擅自恢复；只有会话日志中已经是 active/blocked 的目标才会自动处理。

## 普通线程为什么点击“重试”

重复原始问题会把未完成的工作重新解释成一个新任务，可能重复执行命令、重复修改文件或覆盖已有判断。Desktop 错误卡片的“重试”是官方针对失败轮次的动作，监视器点击它而不是重发原始问题，所以不会把已经落盘的修改变成一次新的用户请求。

## 系统要求

- Linux，具备 systemd 用户会话和 X11/桌面会话；
- Python 3.10 或更高版本；
- Codex Desktop / ChatGPT Desktop，且与监视器使用同一个 Linux 用户；
- Desktop 捆绑的 `codex` 可执行文件（安装器会自动寻找 `/usr/lib/chatgpt/resources/codex`，也支持 `PATH` 中的 `codex`）。

不依赖任何第三方 Python 包。

## 一键安装

克隆仓库后执行：

```bash
./install.sh
```

安装器会把监视器复制到用户数据目录、创建 systemd 用户服务、立即启动并设置为登录系统后自动运行。它不需要 `sudo`，不会修改 Codex 安装文件，也不会自动打开 Desktop。

检查状态：

```bash
systemctl --user status codex-desktop-auto-continue.service --no-pager
```

看到 `Loaded: loaded (...; enabled)` 和 `Active: active (running)` 表示正常。查看实时日志：

```bash
journalctl --user -u codex-desktop-auto-continue.service -f
```

活动 Goal 成功触发时会出现：

```text
[codex-auto-continue] clicked Desktop resume-goal on 019... after server_overloaded (1/10000)
```

普通线程成功触发时会出现：

```text
[codex-auto-continue] clicked Desktop retry-failed-turn on 019... after server_overloaded (1/10000)
```

如果看到 `falling back to 'continue'`，说明这次没有找到 Desktop 的“重试”按钮；如果是活动 Goal，程序不会发送这条消息，而会保留事件继续尝试。

## 不安装，临时运行

```bash
python3 codex_desktop_auto_continue.py \
  --codex-bin /usr/lib/chatgpt/resources/codex \
  --max-attempts 10000
```

使用 Codex Desktop 时保持这个进程运行。

## 与当前正在运行的会话

不需要重启 Desktop，也不需要重新创建会话。监视器从启动时的会话日志末尾开始读取；当前会话之后写入的失败会被处理。Desktop 必须保持打开，且使用与 systemd 用户服务相同的 Linux 用户运行。

如果某个会话在监视器启动之前已经停在错误页面，默认不会回头激活它，避免误把大量历史失败会话全部重新启动。这个会话手动点一次“重试”或“恢复目标”即可。不要随意添加 `--scan-existing`；它会读取已有历史错误，只适合隔离测试目录。

## `正在重新连接 1/5` 的处理方式

为了保留同一个线程和已经执行的工具状态，程序不会在第一次显示 `正在重新连接 1/5` 时杀掉 Codex 进程。它会让官方内部重连走完；如果最后仍然失败并产生 `task_complete.error`，监视器会立即点击 Desktop 的“恢复目标”或“重试”。因此它不额外增加自己的等待间隔，也不会把失败轮次改写成新的原始提问。它不改变 Desktop 内部 `1/5` 的实现；要修改那段内部退避，需要改官方 app-server，本包装器不触碰它。

## 服务何时启动

服务在用户登录系统后自动运行，不是在打开 Codex 时才启动：

```text
登录系统 → 监视器启动并等待 → 打开 Codex Desktop → 自动监视新事件
```

关闭 Desktop 后监视器仍会安静等待；它不会自动拉起 Desktop。Desktop 关闭期间发生的失败无法执行 UI 按钮，服务会重试并在日志中说明。

## 参数

```text
--session-root PATH        会话 JSONL 根目录；默认 CODEX_HOME/sessions
--codex-bin PATH           用于读取线程信息和 legacy fallback 的程序
--message TEXT             legacy fallback 消息；默认 continue
--max-attempts N           每个线程成功继续次数；默认 10000
--max-queue-attempts N     每个失败事件的本地排队尝试；默认 10000
--poll-ms N                文件轮询间隔；默认 100 毫秒
--queue-retry-ms N         Desktop/queue 操作失败后的重试间隔；默认 250 毫秒
--app-server-timeout-ms N  读取线程信息/控制 Desktop 的超时；默认 15000 毫秒
--desktop-unavailable-retry-ms N  Desktop 关闭时的挂起间隔；默认 30000 毫秒
--inspector-port N         Desktop 本地 Node inspector 端口；默认 9229
--no-desktop-ui            禁用真实 Desktop 按钮，使用旧版 queue fallback
--scan-existing            处理已有错误；对旧日志有误触发风险
--all-clients              同时处理非 Desktop 会话
--dry-run                  只检测和记录，不发送动作
--once                     扫描一次后退出
--version                  显示版本
```

## 卸载

```bash
./uninstall.sh
```

卸载器只会删除本项目安装的用户服务和程序目录，不会删除 Codex 会话、项目或聊天记录。

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
