"""进程级异常原件采集，不将空日志解释为没有异常。"""


def capture_crash(device, pid, output):
    output.write_text(device.shell("logcat", "-b", "crash", "-d", "-v", "threadtime", "--pid=" + str(pid)))
