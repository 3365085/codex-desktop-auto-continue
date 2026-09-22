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
对该聊天手动点一次“继续”，以后的失败会被监视器捕获。

不要对正常的会话根目录使用 `--scan-existing`，否则可能重新激活许多旧失败
聊天。

## `codex queue` 失败

确认 Desktop 已经打开，并确认所选运行时支持该命令：

```bash
/usr/lib/chatgpt/resources/codex queue --help
```

监视器会以固定的短间隔重试本地排队操作，不会切换模型。

## `正在重新连接 1/5` 后没有日志

这是官方内部正在进行的重连状态，并不是已经完成的失败。监视器等待最终的
`task_complete.error`。如果 Codex 自己重新连接成功，则不需要继续消息，
日志中也不会出现动作。
