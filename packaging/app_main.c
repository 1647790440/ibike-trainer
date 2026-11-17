/*
 * iBike.app 的最小主程序（Mach-O）。
 *
 * 为什么不能直接用 shell 脚本当 CFBundleExecutable：
 *   LaunchServices 会用 `/bin/bash` 当"主进程"，而 **TCC 的权限归属算在
 *   主进程头上**——bash 没有用途说明字符串，于是访问 ~/Desktop 时系统
 * **静默拒绝**（日志里只有 "(Sandbox) System Policy: Python deny file-read-data"），
 *   用户看到的就是"双击完全没反应"，连授权弹窗都没有。
 *   换成真正编译出来的可执行文件之后，权限归属就是 iBike 自己，Info.plist 里的
 *   NSDesktopFolderUsageDescription 才会生效、才会正常弹窗。
 *
 * 它做两件事：
 *   1. 自检能不能读到项目里的 pyvenv.cfg / 启动器；读不到就弹窗说清楚（别静默死）
 *   2. 把参数里的 -psn_* 滤掉，然后 exec 项目里的 python + 启动器
 *
 * 编译时用 -DIB_PYTHON / -DIB_LAUNCHER / -DIB_PYVENV_CFG 把路径写进来
 * （见 packaging/make_app.sh）。
 */

#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>
#include <pwd.h>
#include <sys/stat.h>

#ifndef IB_PYTHON
#define IB_PYTHON "/usr/bin/python3"
#endif
#ifndef IB_LAUNCHER
#define IB_LAUNCHER ""
#endif
#ifndef IB_PYVENV_CFG
#define IB_PYVENV_CFG ""
#endif

static const char *home_dir(void) {
    const char *home = getenv("HOME");
    if (home && home[0]) {
        return home;
    }
    struct passwd *pw = getpwuid(getuid());
    return pw ? pw->pw_dir : "/tmp";
}

static void log_line(const char *msg) {
    char path[4096];
    snprintf(path, sizeof(path), "%s/Library/Logs/iBike-launcher.log", home_dir());
    FILE *fh = fopen(path, "a");
    if (!fh) {
        return;
    }
    time_t now = time(NULL);
    struct tm tmv;
    localtime_r(&now, &tmv);
    char stamp[32];
    strftime(stamp, sizeof(stamp), "%Y-%m-%d %H:%M:%S", &tmv);
    fprintf(fh, "%s  [app] %s\n", stamp, msg);
    fflush(fh);
    fclose(fh);
}

/* 双击启动时没有终端，错误只能靠系统对话框传达。 */
static void show_dialog(const char *text) {
    char script[2048];
    snprintf(script, sizeof(script),
             "display dialog \"%s\" with title \"iBike 启动失败\" "
             "buttons {\"好\"} default button 1", text);
    pid_t pid = fork();
    if (pid == 0) {
        execl("/usr/bin/osascript", "osascript", "-e", script, (char *)NULL);
        _exit(127);
    }
    if (pid > 0) {
        int status = 0;
        waitpid(pid, &status, 0);
    }
}

int main(int argc, char **argv) {
    log_line("主程序启动");

    /* 自检：macOS 不给 App 读「桌面/文稿/下载」时会**静默拒绝**，python 连
       pyvenv.cfg 都读不到就当场退出（而且不会走到 launcher.py，日志里什么都没有）。
       这里提前探一下，把原因直接告诉用户。 */
    if (IB_PYVENV_CFG[0] && access(IB_PYVENV_CFG, R_OK) != 0) {
        char msg[512];
        snprintf(msg, sizeof(msg),
                 "iBike 读不到项目目录（%s）。\\n\\n"
                 "请在 系统设置 → 隐私与安全性 → 文件与文件夹 里，"
                 "给 iBike 勾上「桌面文件夹」，然后再打开 iBike。",
                 IB_PYVENV_CFG);
        log_line("读不到 pyvenv.cfg —— 多半是没给「桌面」访问权限");
        show_dialog(msg);
        return 1;
    }
    if (IB_LAUNCHER[0] && access(IB_LAUNCHER, R_OK) != 0) {
        log_line("找不到启动器脚本");
        show_dialog("iBike 找不到启动器（packaging/launcher.py）。"
                    "项目目录可能搬家了——重新跑一次 packaging/make_app.sh 就好。");
        return 1;
    }

    /* 参数：[python, launcher, 其余...]；-psn_* 是 Finder 的老式参数，丢掉 */
    char **args = calloc((size_t)argc + 3, sizeof(char *));
    if (!args) {
        log_line("内存不足");
        return 127;
    }
    int n = 0;
    args[n++] = (char *)IB_PYTHON;
    args[n++] = (char *)IB_LAUNCHER;
    for (int i = 1; i < argc; i++) {
        if (strncmp(argv[i], "-psn_", 5) == 0) {
            continue;
        }
        args[n++] = argv[i];
    }
    args[n] = NULL;

    log_line("交给 python 启动器");
    execv(IB_PYTHON, args);

    /* 走到这里说明 exec 失败（python 不存在之类）——记下来，别静默退出 */
    char msg[512];
    snprintf(msg, sizeof(msg), "无法启动 %s：%s", IB_PYTHON, strerror(errno));
    log_line(msg);
    show_dialog(msg);
    return 127;
}
