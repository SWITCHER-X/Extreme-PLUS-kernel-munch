#!/usr/bin/env python3
# ==============================================================================
# PROJECT EXTREME++ | Maintainer: PandeyJI-9
# Device: POCO F4 / Redmi K40S (munch) | FocalTech FT3658U touch panel
#
# Double-tap-to-wake on munch is implemented by the touch IC driver:
#   gesture_mode -> fts_gesture_suspend() writes FTS_REG_GESTURE_EN=1 and
#   enable_irq_wake(), so a double tap in sleep reports KEY_WAKEUP.
#
# gesture_mode is initialised from FTS_GESTURE_EN in focaltech_config.h, which
# is 0 in the AstideLabs base.  That makes double tap work only when the
# ROM/HAL (MIUI/HyperOS touchfeature daemon via /dev/xiaomi-touch) turns it on,
# so AOSP ROMs and stock ROMs with a missing/renamed HAL report
# "double tap to wake not working".
#
# Flipping the default to 1 makes the panel enter gesture mode on suspend out
# of the box, but on MIUI/HyperOS the touch HAL writes Touch_Doubletap_Mode at
# boot with whatever the user's setting is, which silently overwrites it.
#
# So the driver also tracks whether userspace ever took control:
#   - HAL wrote Touch_Doubletap_Mode  -> respect it (user setting wins)
#   - nobody wrote it (AOSP ROMs)     -> keep gesture mode on, so double tap
#                                        wakes the device without any HAL
# Suspend/resume log the decision so a user log shows why it did or did not
# enter gesture mode.
# ==============================================================================
import os
import re
import sys

MARK = "EXTREME++ DT2W"
CONFIG_NAME = "focaltech_config.h"
DEFINE_RE = re.compile(r"^#define\s+FTS_GESTURE_EN\s+(\S+)(.*)$", re.MULTILINE)


def find_config(kernel_src: str) -> str | None:
    direct = [
        os.path.join(kernel_src, "drivers/input/touchscreen/focaltech_3658u", CONFIG_NAME),
        os.path.join(kernel_src, "drivers/input/touchscreen/focaltech_touch", CONFIG_NAME),
    ]
    for path in direct:
        if os.path.exists(path):
            return path

    touch_root = os.path.join(kernel_src, "drivers/input/touchscreen")
    for root, _dirs, files in os.walk(touch_root):
        if CONFIG_NAME in files and "focaltech" in root:
            return os.path.join(root, CONFIG_NAME)
    return None


def patch_config(path: str) -> int:
    with open(path) as f:
        text = f.read()

    if f"{MARK}: double tap wakes" in text:
        print(f"ℹ️ [DT2W] {path} already enables double tap by default")
        return 0

    match = DEFINE_RE.search(text)
    if not match:
        print(f"❌ [DT2W] FTS_GESTURE_EN define not found in {path}")
        return 1

    if match.group(1) == "1":
        print(f"ℹ️ [DT2W] {path} already has FTS_GESTURE_EN 1")
        return 0

    replacement = (
        "#define FTS_GESTURE_EN                          1"
        f" /* {MARK}: double tap wakes the device by default */"
    )
    with open(path, "w") as f:
        f.write(DEFINE_RE.sub(lambda m: replacement, text, count=1))

    print(f"✅ [DT2W] FTS_GESTURE_EN=1 (double tap to wake on by default) in {path}")
    return 0


def main() -> int:
    kernel_src = sys.argv[1] if len(sys.argv) > 1 else "."
    path = find_config(kernel_src)
    if not path:
        print(f"❌ [DT2W] {CONFIG_NAME} not found under {kernel_src}")
        return 1

    globals()["_LAST_CONFIG"] = path
    if patch_config(path) != 0:
        return 1
    return patch_driver_override()


def patch_driver_override() -> int:
    """Track HAL ownership of dt2w and keep gesture mode on when nobody set it.

    `path` above points at .../focaltech_3658u/focaltech_config.h, so the driver
    file lives next to it.
    """
    cfg = globals().get("_LAST_CONFIG", "")
    candidates = []
    if cfg:
        candidates.append(os.path.join(os.path.dirname(cfg), "focaltech_core.c"))
    candidates += [
        "drivers/input/touchscreen/focaltech_3658u/focaltech_core.c",
        "drivers/input/touchscreen/focaltech_touch/focaltech_core.c",
    ]
    path = next((c for c in candidates if c and os.path.exists(c)), None)
    if not path:
        print("❌ [DT2W] focaltech_core.c not found for the HAL override")
        return 1

    with open(path) as f:
        text = f.read()

    if "EXTREME++ DT2W: hal_owned" in text:
        print(f"ℹ️ [DT2W] {path} already has the HAL override")
        return 0

    edits = [
        (
            "struct fts_ts_data *fts_data;",
            "struct fts_ts_data *fts_data;\n"
            "/* EXTREME++ DT2W: hal_owned tracks whether userspace (Touch_Doubletap_Mode)\n"
            " * ever took control of the gesture switch.  Without it, AOSP ROMs that have\n"
            " * no touch HAL would never enable double tap wake. */\n"
            "static bool fts_dt2w_hal_owned;",
        ),
        (
            "\t} else if (mode == Touch_Doubletap_Mode && value >= 0) {\n"
            "\t\tfts_update_gesture_state(fts_data, GESTURE_DOUBLETAP, value != 0 ? true : false);\n"
            "\t\treturn 0;",
            "\t} else if (mode == Touch_Doubletap_Mode && value >= 0) {\n"
            "\t\t/* EXTREME++ DT2W: hal_owned: userspace decides from now on */\n"
            "\t\tfts_dt2w_hal_owned = true;\n"
            "\t\tfts_update_gesture_state(fts_data, GESTURE_DOUBLETAP, value != 0 ? true : false);\n"
            "\t\treturn 0;",
        ),
        (
            "\tif (ts_data->gesture_mode && !ts_data->poweroff_on_sleep) {\n"
            "\t\tfts_gesture_suspend(ts_data);\n"
            "\t} else {",
            '\tFTS_INFO("EXTREME++ DT2W: gesture_mode=%d hal_owned=%d poweroff_on_sleep=%d",\n'
            "\t\t ts_data->gesture_mode, fts_dt2w_hal_owned, ts_data->poweroff_on_sleep);\n"
            "\tif ((ts_data->gesture_mode || !fts_dt2w_hal_owned) &&\n"
            "\t\t\t!ts_data->poweroff_on_sleep) {\n"
            "\t\tfts_gesture_suspend(ts_data);\n"
            "\t} else {",
        ),
        (
            "\tif (ts_data->gesture_mode && !ts_data->poweroff_on_sleep) {\n"
            "\t\tfts_gesture_resume(ts_data);\n"
            "\t} else {",
            "\tif ((ts_data->gesture_mode || !fts_dt2w_hal_owned) &&\n"
            "\t\t\t!ts_data->poweroff_on_sleep) {\n"
            "\t\tfts_gesture_resume(ts_data);\n"
            "\t} else {",
        ),
    ]

    for old, new in edits:
        if text.count(old) != 1:
            print(f"❌ [DT2W] anchor not found once in {path}: {old.splitlines()[0][:60]}")
            return 1
        text = text.replace(old, new, 1)

    with open(path, "w") as f:
        f.write(text)
    print(f"✅ [DT2W] {path}: gesture mode kept on when no touch HAL sets dt2w")
    return 0


if __name__ == "__main__":
    sys.exit(main())
