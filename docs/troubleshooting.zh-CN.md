# 故障排查

## 服务没有运行

```bash
systemctl --user status codex-desktop-auto-continue.service --no-pager
journalctl --user -u codex-desktop-auto-continue.service -n 100 --no-pager
```

如果移动过仓库或更新了安装脚本，重新执行 `./install.sh`。

## 提示已有监视器持有锁

每个用户只允许运行一个监视器。先检查服务：

```bash
systemctl --user status codex-desktop-auto-continue.service --no-pager
```

停止已有服务或进程，不要启动第二个副本。

## 当前聊天没有被继续

监视器从已有日志末尾开始读取。启动之前就已经记录的失败会被有意忽略。
对该聊天手动点一次“重试”或“恢复目标”，以后的失败会被监视器捕获。

不要对正常的会话根目录使用 `--scan-existing`，否则可能重新激活许多旧失败
聊天。

## Desktop 按钮没有被点击

先确认 Desktop 正在运行，并查看日志中是否有 inspector 或标题错误：

```bash
journalctl --user -u codex-desktop-auto-continue.service -n 100 --no-pager
curl -sS http://127.0.0.1:9229/json/list | head
```

服务会在第一次需要时自动为当前 Desktop 主进程开启 loopback inspector。若 Desktop
不是与 systemd 服务相同的 Linux 用户启动，或者当前不是图形 X11 会话，服务无法
点击按钮；请用同一个用户重新打开 Desktop 后再让失败发生一次。

确认安装的脚本已更新：

```bash
./install.sh
systemctl --user restart codex-desktop-auto-continue.service
```

日志中应看到 `desktop_ui=True`。如果是活动 Goal，看到按钮不可用时程序会保留
事件继续尝试，不会发送一条新的 `continue` 消息。

## `codex queue` fallback 失败

确认 Desktop 已经打开，并确认所选运行时支持该命令：

```bash
/usr/lib/chatgpt/resources/codex queue --help
```

安装后的服务对普通失败使用这个 queue 路径：它向同一个线程发送 `continue`，不打开
普通对话的 Desktop UI，因此不会切换你正在看的普通会话。聊天中出现可见的
`continue` 消息是预期行为；活动 Goal 仍使用 Desktop 官方“恢复目标”按钮。

## `正在重新连接 1/5` 后没有日志

这是官方内部正在进行的重连状态，并不是已经完成的失败。监视器等待最终的
`task_complete.error`，随后点击 Desktop 的“恢复目标”或“重试”。如果 Codex
自己重新连接成功，则不需要继续动作，日志中也不会出现点击记录。
