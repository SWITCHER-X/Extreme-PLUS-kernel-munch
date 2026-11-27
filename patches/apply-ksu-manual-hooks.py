#!/usr/bin/env python3
# ==============================================================================
# PROJECT EXTREME++ | KernelSU manual-hook port for the susfs-free variant.
#
# The pinned AstideLabs base only wires KernelSU up through the SuSFS inline
# hook: every exec/stat/open/reboot call site lives inside
# `#ifdef CONFIG_KSU_SUSFS`. With CONFIG_KSU_SUSFS=n those calls vanish and
# nothing calls ksu_handle_*, so the manual hook (CONFIG_KSU_MANUAL_HOOK, the
# only susfs-free option on non-GKI 4.19) has nothing to run.
#
# This adds the required call sites, in place, under
#   #if defined(CONFIG_KSU_MANUAL_HOOK) && !defined(CONFIG_KSU_SUSFS)
# so a SuSFS build is byte-for-byte unchanged and a build that enables both
# still uses the SuSFS path only.
#
# Manual-hook ABI differences handled here:
#   * ksu_handle_stat / ksu_handle_faccessat take (const char __user **), not
#     the SuSFS (struct filename **) — the user pointer is hooked before lookup.
#   * post-exec is ksu_handle_post_execveat (manual), not
#     ksu_handle_post_execveat_sucompat.
#   * ksu_handle_newfstat_ret / ksu_handle_fstat64_ret only exist when
#     CONFIG_KSU_MANUAL_HOOK is on (they live behind that guard in ReSukiSU).
#
# Fails closed: a missing/duplicated anchor aborts the build instead of
# silently producing a kernel with no hooks.
#
# Usage: python3 apply-ksu-manual-hooks.py <kernel_dir>
# ==============================================================================
import os
import sys

MARK = "EXTREME++ KSU MANUAL HOOK"
GUARD = "#if defined(CONFIG_KSU_MANUAL_HOOK) && !defined(CONFIG_KSU_SUSFS)"


class PatchError(RuntimeError):
    pass


def read(path):
    with open(path) as fh:
        return fh.read()


def replace_once(text, old, new, label):
    if text.count(old) != 1:
        raise PatchError(f"{label}: anchor found {text.count(old)} times, expected 1")
    return text.replace(old, new, 1)


def insert_after(text, anchor, payload, label):
    return replace_once(text, anchor, anchor + payload, label)


def patch_exec(src):
    path = os.path.join(src, "fs/exec.c")
    text = read(path)
    if MARK in text:
        print(f"info: {path} already patched")
        return

    # 1. prototypes (the SuSFS block is compiled out in manual builds)
    text = insert_after(
        text,
        "extern int ksu_handle_post_execveat_sucompat(int *fd, struct filename **filename_ptr, void *argv,\n"
        "\t\t\tvoid *envp, int *flags, int *retval);\n#endif\n",
        f"""
/* {MARK}: manual hook has no SuSFS block to call from */
{GUARD}
extern struct static_key_true ksu_su_compat_enabled;
extern int ksu_handle_execveat(int *fd, struct filename **filename_ptr, void *argv,
\t\t\tvoid *envp, int *flags);
extern int ksu_handle_post_execveat(int *fd, struct filename **filename_ptr, void *argv,
\t\t\tvoid *envp, int *flags, int *retval);
#endif
""",
        "exec prototypes",
    )

    # 2. pre-exec hook (before the RLIMIT_NPROC recheck, where the base runs SuSFS)
    text = insert_after(
        text,
        "orig_flow:\n#endif\n",
        f"""
{GUARD}
\tif (static_branch_likely(&ksu_su_compat_enabled))
\t\tksu_handle_execveat(&fd, &filename, &argv, &envp, &flags);
#endif
""",
        "exec pre-hook",
    )

    # 3. post-exec hook next to the SuSFS one
    text = insert_after(
        text,
        "#ifdef CONFIG_KSU_SUSFS\n"
        "\tif (unlikely(is_su_session))\n"
        "\t\t(void)ksu_handle_post_execveat_sucompat(&fd, &filename, &argv, &envp, &flags, &retval);\n"
        "#endif // #ifdef CONFIG_KSU_SUSFS\n",
        f"""
{GUARD}
\t(void)ksu_handle_post_execveat(&fd, &filename, &argv, &envp, &flags, &retval);
#endif
""",
        "exec post-hook",
    )

    with open(path, "w") as fh:
        fh.write(text)
    print(f"patched {path}")


def patch_stat(src):
    path = os.path.join(src, "fs/stat.c")
    text = read(path)
    if MARK in text:
        print(f"info: {path} already patched")
        return

    text = insert_after(
        text,
        "extern int ksu_handle_stat(int *dfd, struct filename **filename, int *flags);\n"
        "#endif // #ifdef CONFIG_KSU_SUSFS\n",
        f"""
/* {MARK}: manual-hook prototypes (user-pointer ABI) */
{GUARD}
extern int ksu_handle_stat(int *dfd, const char __user **filename_user, int *flags);
extern void ksu_handle_newfstat_ret(unsigned int *fd, struct stat __user **statbuf_ptr);
#if defined(__ARCH_WANT_STAT64) || defined(__ARCH_WANT_COMPAT_STAT64)
extern void ksu_handle_fstat64_ret(unsigned long *fd, struct stat64 __user **statbuf_ptr);
#endif
#endif
""",
        "stat prototypes",
    )

    # newfstatat: hook the user pointer before the lookup
    text = replace_once(
        text,
        "SYSCALL_DEFINE4(newfstatat, int, dfd, const char __user *, filename,\n"
        "\t\tstruct stat __user *, statbuf, int, flag)\n"
        "{\n"
        "\tstruct kstat stat;\n"
        "\tint error;\n"
        "\n"
        "\terror = vfs_fstatat(dfd, filename, &stat, flag);\n",
        "SYSCALL_DEFINE4(newfstatat, int, dfd, const char __user *, filename,\n"
        "\t\tstruct stat __user *, statbuf, int, flag)\n"
        "{\n"
        "\tstruct kstat stat;\n"
        "\tint error;\n"
        "\n"
        f"{GUARD}\n"
        "\tksu_handle_stat(&dfd, &filename, &flag);\n"
        "#endif\n"
        "\n"
        "\terror = vfs_fstatat(dfd, filename, &stat, flag);\n",
        "newfstatat hook",
    )

    # newfstat: return hook (init.rc size spoofing)
    text = replace_once(
        text,
        "SYSCALL_DEFINE2(newfstat, unsigned int, fd, struct stat __user *, statbuf)\n"
        "{\n"
        "\tstruct kstat stat;\n"
        "\tint error = vfs_fstat(fd, &stat);\n"
        "\n"
        "\tif (!error)\n"
        "\t\terror = cp_new_stat(&stat, statbuf);\n"
        "\n"
        "\treturn error;\n"
        "}\n",
        "SYSCALL_DEFINE2(newfstat, unsigned int, fd, struct stat __user *, statbuf)\n"
        "{\n"
        "\tstruct kstat stat;\n"
        "\tint error = vfs_fstat(fd, &stat);\n"
        "\n"
        "\tif (!error)\n"
        "\t\terror = cp_new_stat(&stat, statbuf);\n"
        "\n"
        f"{GUARD}\n"
        "\tif (!error)\n"
        "\t\tksu_handle_newfstat_ret(&fd, &statbuf);\n"
        "#endif\n"
        "\n"
        "\treturn error;\n"
        "}\n",
        "newfstat ret hook",
    )

    # fstat64: 32-bit pointer-size spoofing (only where stat64 exists)
    text = replace_once(
        text,
        "SYSCALL_DEFINE2(fstat64, unsigned long, fd, struct stat64 __user *, statbuf)\n"
        "{\n"
        "\tstruct kstat stat;\n"
        "\tint error = vfs_fstat(fd, &stat);\n"
        "\n"
        "\tif (!error)\n"
        "\t\terror = cp_new_stat64(&stat, statbuf);\n"
        "\n"
        "\treturn error;\n"
        "}\n",
        "SYSCALL_DEFINE2(fstat64, unsigned long, fd, struct stat64 __user *, statbuf)\n"
        "{\n"
        "\tstruct kstat stat;\n"
        "\tint error = vfs_fstat(fd, &stat);\n"
        "\n"
        "\tif (!error)\n"
        "\t\terror = cp_new_stat64(&stat, statbuf);\n"
        "\n"
        f"{GUARD}\n"
        "#if defined(__ARCH_WANT_STAT64) || defined(__ARCH_WANT_COMPAT_STAT64)\n"
        "\tif (!error)\n"
        "\t\tksu_handle_fstat64_ret(&fd, &statbuf);\n"
        "#endif\n"
        "#endif\n"
        "\n"
        "\treturn error;\n"
        "}\n",
        "fstat64 ret hook",
    )

    with open(path, "w") as fh:
        fh.write(text)
    print(f"patched {path}")


def patch_open(src):
    path = os.path.join(src, "fs/open.c")
    text = read(path)
    if MARK in text:
        print(f"info: {path} already patched")
        return

    text = insert_after(
        text,
        "extern int ksu_handle_faccessat(int *dfd, struct filename **filename, int *mode,\n"
        "\t\t\tint *flags);\n"
        "#endif\n",
        f"""
/* {MARK}: manual-hook prototype (user-pointer ABI) */
{GUARD}
extern int ksu_handle_faccessat(int *dfd, const char __user **filename_user, int *mode,
\t\t\tint *__unused_flags);
#endif
""",
        "open prototype",
    )

    text = replace_once(
        text,
        "SYSCALL_DEFINE3(faccessat, int, dfd, const char __user *, filename, int, mode)\n"
        "{\n"
        "\treturn do_faccessat(dfd, filename, mode);\n"
        "}\n",
        "SYSCALL_DEFINE3(faccessat, int, dfd, const char __user *, filename, int, mode)\n"
        "{\n"
        f"{GUARD}\n"
        "\tksu_handle_faccessat(&dfd, &filename, &mode, NULL);\n"
        "#endif\n"
        "\treturn do_faccessat(dfd, filename, mode);\n"
        "}\n",
        "faccessat hook",
    )

    with open(path, "w") as fh:
        fh.write(text)
    print(f"patched {path}")


def patch_reboot(src):
    path = os.path.join(src, "kernel/reboot.c")
    text = read(path)
    if MARK in text:
        print(f"info: {path} already patched")
        return

    text = replace_once(
        text,
        "#ifdef CONFIG_KSU_SUSFS\n"
        "extern int ksu_handle_sys_reboot(int magic1, int magic2, unsigned int cmd, void __user **arg);\n"
        "#endif\n",
        f"/* {MARK}: reboot hook shared by both hook types */\n"
        "#if defined(CONFIG_KSU_SUSFS) || defined(CONFIG_KSU_MANUAL_HOOK)\n"
        "extern int ksu_handle_sys_reboot(int magic1, int magic2, unsigned int cmd, void __user **arg);\n"
        "#endif\n",
        "reboot prototype",
    )

    text = replace_once(
        text,
        "#ifdef CONFIG_KSU_SUSFS\n"
        "    if (system_state == SYSTEM_RUNNING) {\n"
        "        ksu_handle_sys_reboot(magic1, magic2, cmd, &arg);\n"
        "    }\n"
        "#endif\n",
        "#if defined(CONFIG_KSU_SUSFS) || defined(CONFIG_KSU_MANUAL_HOOK)\n"
        "    if (system_state == SYSTEM_RUNNING) {\n"
        "        ksu_handle_sys_reboot(magic1, magic2, cmd, &arg);\n"
        "    }\n"
        "#endif\n",
        "reboot call",
    )

    with open(path, "w") as fh:
        fh.write(text)
    print(f"patched {path}")


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else "."
    if not os.path.isfile(os.path.join(src, "fs/exec.c")):
        print(f"error: {src} does not look like a kernel tree", file=sys.stderr)
        return 1
    try:
        patch_exec(src)
        patch_stat(src)
        patch_open(src)
        patch_reboot(src)
    except PatchError as exc:
        print(f"error: [KSU MANUAL HOOK] {exc}", file=sys.stderr)
        return 1
    print("ok: manual-hook call sites in place")
    return 0


if __name__ == "__main__":
    sys.exit(main())
