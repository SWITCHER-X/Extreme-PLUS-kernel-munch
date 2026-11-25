#!/usr/bin/env python3
# ==============================================================================
# PROJECT EXTREME++ | Maintainer: PandeyJI-9
# Touch-latency / jitter mitigation for apps that drive fps hints hard.
#
# Kernel log evidence (kernel_log_1795110033195.txt, POCO F4):
#   ALERT: changing window size from 8000000 to 20000000 at ...
#   mi-dsi-panel: ... DSI_CMD_SET_TIMING_SWITCH
#   [FTS_TS/I]drm_notifier_callback:DRM event:1, blank:0
#   [FTS_TS]fts_ts_resume: Enter / Already in awake state
#
# Two distinct things get blamed on "touch drops":
#
# 1. kernel/sched/walt.c flips the WALT window between 8ms and 20ms whenever
#    userspace changes its fps hint - up to ~13 flips/s in bursts.  Each flip
#    re-bases task window accounting (sched_ravg_window) and reliably shows up
#    as scheduling jitter / frame drops while such an app is foregrounded.
#    Debounce the flips to at most one per 100ms.
#
# 2. focaltech's drm_notifier_callback re-queues resume_work on every
#    MI_DRM_BLANK_UNBLANK, and munch's panel sends UNBLANK on each timing
#    switch (4-5/s in the log, 44 in 15s).  fts_ts_resume() bails out with
#    "Already in awake state" anyway, so skipping the queue when the touch IC
#    is already awake removes pure workqueue churn on the input path.
#
# Neither change touches the panel timing itself - that one is a display/HWC
# decision and rate-limiting it would pin the refresh rate.
# ==============================================================================
import os
import sys

MARK = "EXTREME++ TOUCH JITTER"


def write(path: str, text: str) -> None:
    with open(path, "w") as f:
        f.write(text)


def patch_walt(src: str) -> int:
    path = os.path.join(src, "kernel/sched/walt.c")
    if not os.path.exists(path):
        print(f"❌ [TOUCH JITTER] missing {path}")
        return 1

    with open(path) as f:
        text = f.read()

    if MARK in text:
        print(f"ℹ️ [TOUCH JITTER] {path} already patched")
        return 0

    old_decl = "u64 sched_ravg_window_change_time;"
    new_decl = old_decl + f"""

/* {MARK}: userspace fps hints can flip the WALT window 8ms<->20ms many times
 * per second; every flip re-bases window accounting and costs scheduling
 * jitter.  Keep at most one flip per 100ms. */
#define WALT_WINDOW_CHANGE_MIN_NS (100ULL * NSEC_PER_MSEC)
static u64 walt_window_change_last_ns;"""
    if text.count(old_decl) != 1:
        print("❌ [TOUCH JITTER] walt.c: sched_ravg_window_change_time declaration not found")
        return 1
    text = text.replace(old_decl, new_decl, 1)

    old_cond = """		if ((sched_ravg_window != new_sched_ravg_window) &&
		    (wc < this_rq()->window_start + new_sched_ravg_window)) {
			sched_ravg_window_change_time = sched_ktime_clock();"""
    new_cond = f"""		if ((sched_ravg_window != new_sched_ravg_window) &&
		    (wc < this_rq()->window_start + new_sched_ravg_window) &&
		    ((sched_ktime_clock() - walt_window_change_last_ns) >=
		     WALT_WINDOW_CHANGE_MIN_NS)) {{
			sched_ravg_window_change_time = sched_ktime_clock();
			walt_window_change_last_ns = sched_ravg_window_change_time;"""
    if text.count(old_cond) != 1:
        print("❌ [TOUCH JITTER] walt.c: window change block not found")
        return 1
    text = text.replace(old_cond, new_cond, 1)

    write(path, text)
    print(f"✅ [TOUCH JITTER] {path}: WALT window flips debounced to 100ms")
    return 0


def patch_focaltech(src: str) -> int:
    candidates = [
        "drivers/input/touchscreen/focaltech_3658u/focaltech_core.c",
        "drivers/input/touchscreen/focaltech_touch/focaltech_core.c",
    ]
    path = next((os.path.join(src, c) for c in candidates if os.path.exists(os.path.join(src, c))), None)
    if not path:
        print("❌ [TOUCH JITTER] focaltech_core.c not found")
        return 1

    with open(path) as f:
        text = f.read()

    if MARK in text:
        print(f"ℹ️ [TOUCH JITTER] {path} already patched")
        return 0

    old = """	if (blank == MI_DRM_BLANK_UNBLANK) {
		queue_work(fts_data->ts_workqueue, &fts_data->resume_work);

	} else if (blank == MI_DRM_BLANK_POWERDOWN ||"""
    new = f"""	if (blank == MI_DRM_BLANK_UNBLANK) {{
		/* {MARK}: munch sends UNBLANK on every panel timing switch.
		 * resume_work would only hit "Already in awake state", so skip
		 * the workqueue round-trip while the IC is already awake. */
		if (ts_data->suspended)
			queue_work(fts_data->ts_workqueue, &fts_data->resume_work);

	}} else if (blank == MI_DRM_BLANK_POWERDOWN ||"""
    if text.count(old) != 1:
        print("❌ [TOUCH JITTER] focaltech_core.c: UNBLANK branch not found")
        return 1
    text = text.replace(old, new, 1)

    write(path, text)
    print(f"✅ [TOUCH JITTER] {path}: skipped redundant resume_work on UNBLANK")
    return 0


def main() -> int:
    src = sys.argv[1] if len(sys.argv) > 1 else "."
    return patch_walt(src) or patch_focaltech(src)


if __name__ == "__main__":
    sys.exit(main())
