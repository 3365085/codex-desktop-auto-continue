# 效果与限制

## 同线程继续

Codex 会话日志的元数据包含稳定的线程 ID。某轮以受支持的暂时性错误结束后，
安装后的服务采用混合策略：活动/阻塞 Goal 读取线程标题并点击 Desktop 官方按钮；
普通失败不读取 Desktop 标题、不打开 Electron inspector，直接向原线程发送 queue
`continue`，因此不会切换普通对话：

```text
codex queue --thread <原线程ID> --message continue
```

这条消息属于原聊天线程。普通对话中会出现可见的 `continue`，但不会为了恢复它而
切换 Desktop 当前会话；活动 Goal 是唯一会使用 Desktop 官方按钮的例外。手动运行脚本
且不传 `--goal-desktop-ui` 时，普通失败也可使用 Desktop 按钮模式。

## 为什么不直接调用 app-server Goal API

独立启动一个 `codex app-server --listen stdio://` 可以读到线程和 Goal 状态，也
可以把数据库中的 Goal 改为 `active`，但它不是 Desktop 当前正在使用的 live
app-server 连接；因此不会创建 Desktop 的新 turn。包装器仍然使用 app-server
只读线程标题，真正的继续动作由 Desktop 自己的按钮触发。

## 会保留什么

- 原聊天记录仍属于同一线程；
- 原工作目录与 worktree 关联不变；
- 已经写入磁盘的文件修改保留在工作树中；
- 已经记入日志的工具调用和消息仍在原线程上下文中；
- `/goal` 的目标、预算和内部 context 不会转成一条普通用户消息。

如果传输中断时某个外部命令仍在运行，它可能已经产生副作用，但完成事件尚未
写入日志。重试后的智能体仍应先检查工作树和进程状态，再决定是否重复不可幂等
操作。

## 不会改变什么

- 不修改 Codex Desktop 或 app-server 的安装文件；安装后的服务只为活动 Goal 操作 Desktop UI；
- 不改变模型选择，也不启用模型 fallback；
- 不删除 Desktop 内部 `1/5` 重连等待；包装器只在正式的 `task_complete.error`
  之后立即点击官方按钮；
- 不启动或关闭 Desktop；Desktop 关闭期间无法执行按钮，只会等待并记录日志；
- 默认不处理启动前已经存在的历史错误；
- 不保证模型容量问题一定会成功，成功取决于服务端之后是否接受下一次尝试。

程序不会在第一次出现“正在重新连接 1/5”时杀死或重放当前 turn，从而避免
丢失工具状态和把修改重复执行。若要删除官方内部重连等待，需要修改 Codex
app-server 内部实现，而不是这个外部包装器。

## 本地 inspector 安全边界

活动 Goal 或手动启用 Desktop 按钮模式时，监视器才会向当前 Electron 主进程发送
`SIGUSR1`。普通失败的 queue 路径不需要 Node inspector。

## 并发注意事项

监视器处理失败时不要同时手动点击“继续/重试”，否则两边都可能请求两次继续。
进程锁可以防止同一个用户同时启动两个监视器，但无法阻止手动 UI 操作。
