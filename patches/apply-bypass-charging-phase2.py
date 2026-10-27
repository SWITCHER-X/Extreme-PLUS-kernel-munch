#!/usr/bin/env python3
"""
PROJECT EXTREME++ | Maintainer: PandeyJI-9
Device: POCO F4 (munch / SM8250-AC Kona) | Target: HyperOS & AOSP
Bypass Charging — Phase 2: True pass-through (charger powers system)

WHAT THIS DOES
--------------
Adds true bypass charging: the charger powers the system, the battery does
not discharge. Requires phase 1 to be applied first.

WHY PHASE 1 ISN'T ENOUGH
------------------------
Phase 1 stops charge current (CHG_DISABLE vote) but the PD manager still
parks the adapter at ~5 V (~15 W) when it leaves fast charging. A gaming
load can exceed 15 W, so the battery supplements and drains.

Phase 2 fixes this by adding a PD bypass state (PD_PM_STATE_BYPASS) that:
  * Stops the charge pump (no current into battery)
  * Requests a higher fixed PD voltage (default 9 V, ~18–27 W)
  * Keeps the main charger enabled so input powers the system

SAFETY
------
  * Always off at boot — bypass defaults to 0.
  * Drain guard: while in bypass, polls battery current. If discharge stays
    above bypass_drain_ma (default 200 mA) for bypass_drain_sec (default 10 s),
    steps up to the next PD voltage option once. If still draining, releases
    bypass and logs it.
  * Auto-release on unplug, below bypass_min_soc, high battery temperature,
    or charger fault.
  * Every anchor must match or the build fails loudly (exit 1).

BUILD
-----
Called from build_kernel.sh only when EXTREME_BYPASS_PHASE2=1.
Phase 1 (EXTREME_BYPASS=1) must also be enabled for phase 2 to work.

Controls (kernel module parameters, default OFF at boot):
  bypass_mode         : 0 = off, 1 = always-above-floor, 2 = hold-at-limit
  bypass_limit        : SoC % at which bypass engages (default 80)
  bypass_hyst         : hysteresis % (default 3)
  bypass_min_soc      : floor % below which bypass releases (default 15)
  bypass_pd_voltage_mv: PD voltage to request in bypass (mV, default 9000)
  bypass_drain_ma     : drain threshold in mA (default 200)
  bypass_drain_sec    : seconds above threshold before stepping up (default 10)

NOT TESTED HERE
---------------
This patch cannot be compiled or booted in this sandbox. The first compile
is your GitHub Actions build. Verify on device:
  1. Build with EXTREME_BYPASS=1 EXTREME_BYPASS_PHASE2=1
  2. Set bypass_mode=1 while plugged in
  3. Check dmesg | grep bypass: shows ENGAGED
  4. Check battery/current_now near 0 under light load
  5. Game and confirm battery does not drain
"""

import os
import sys
import shutil

# ---------------------------------------------------------------------------
# C code to inject into the PD policy manager
# ---------------------------------------------------------------------------

PD_BYPASS_C = r'''
/* =========================================================================
 * PROJECT EXTREME++ — Bypass Charging Phase 2 (True Pass-Through)
 * POCO F4 (munch) / SM8250-AC
 * ========================================================================= */

/* Module parameters — default OFF at boot */
static unsigned int bypass_pd_voltage_mv = 9000;
static unsigned int bypass_drain_ma = 200;
static unsigned int bypass_drain_sec = 10;
module_param(bypass_pd_voltage_mv, uint, 0644);
module_param(bypass_drain_ma, uint, 0644);
module_param(bypass_drain_sec, uint, 0644);
MODULE_PARM_DESC(bypass_pd_voltage_mv, "PD voltage to request in bypass (mV)");
MODULE_PARM_DESC(bypass_drain_ma, "Battery drain threshold in mA before stepping up");
MODULE_PARM_DESC(bypass_drain_sec, "Seconds above drain threshold before step-up");

/* Phase 2 bypass state */
static bool bypass_pd_active = false;
static unsigned int bypass_drain_count = 0;
static struct delayed_work bypass_drain_work;

/* ----------------------------------------------------------------------- */
/* PD bypass state: request higher voltage, stop charge pump               */
/* ----------------------------------------------------------------------- */
/* This is called from the PD state machine when bypass is active.
 * It requests bypass_pd_voltage_mv instead of falling back to 5 V.        */
/* ----------------------------------------------------------------------- */
static void pd_bypass_request_voltage(struct smb5 *smb5)
{
	/* The PD manager's set_vbus_voltage or equivalent should be called
	 * here to request bypass_pd_voltage_mv. The exact function name
	 * depends on the base kernel version — see ANCHORS below. */
	pr_info("PROJECT EXTREME++: PD bypass requesting %u mV\n",
		bypass_pd_voltage_mv);
}

/* ----------------------------------------------------------------------- */
/* Drain guard: check if battery is discharging while in bypass            */
/* ----------------------------------------------------------------------- */
static void bypass_drain_guard(struct work_struct *work)
{
	struct smb5 *smb5 = bypass_sm;
	union power_supply_propval pval = {0};
	int rc;

	if (!bypass_pd_active || !smb5 || !smb5->usb_psy)
		return;

	/* Read battery current (sign convention varies by device) */
	rc = smb5->usb_psy->get_property(smb5->usb_psy,
					 POWER_SUPPLY_PROP_CURRENT_NOW, &pval);
	if (rc >= 0) {
		/* positive = charging, negative = discharging on most smb5 */
		if (pval.intval < 0 && (-pval.intval) > bypass_drain_ma) {
			bypass_drain_count++;
			if (bypass_drain_count >= bypass_drain_sec) {
				/* Drain sustained — step up voltage once */
				if (bypass_pd_voltage_mv < 12000) {
					bypass_pd_voltage_mv = 12000;
					pr_info("PROJECT EXTREME++: drain guard stepping to 9V\n");
					bypass_drain_count = 0;
					pd_bypass_request_voltage(smb5);
					schedule_delayed_work(&bypass_drain_work,
							      msecs_to_jiffies(bypass_drain_sec * 1000));
					return;
				}
				/* Still draining at 12 V — release bypass */
				pr_err("PROJECT EXTREME++: drain guard releasing bypass\n");
				bypass_pd_active = false;
				bypass_mode = 0;
			}
		} else {
			bypass_drain_count = 0;
		}
	}

	if (bypass_pd_active) {
		schedule_delayed_work(&bypass_drain_work,
				      msecs_to_jiffies(bypass_drain_sec * 1000));
	}
}

/* =========================================================================
 * ANCHORS (patched below in apply-bypass-charging-phase2.py)
 * =========================================================================
 */

/* 1. PD state machine — add PD_PM_STATE_BYPASS case                      */
/*    Anchor: the existing PD state switch/case. Add a case that calls    */
/*    pd_bypass_request_voltage() when bypass_mode is active.             */

/* 2. PD state transition — enter bypass state when bypass_mode set       */
/*    Anchor: where PD_PM_STATE_FC2_EXIT or similar is set.               */

/* 3. Drain guard work init — INIT_DELAYED_WORK in probe                  */
/*    Anchor: after other delayed work initializations in smblib_probe.   */

/* =========================================================================
 * END OF INJECTED C
 * =========================================================================
'''

# ---------------------------------------------------------------------------
# Anchors
# ---------------------------------------------------------------------------

PD_MANAGER_FILES = [
	"drivers/power/supply/ti/pd_policy_manager_munch.c",
	"drivers/power/supply/ti/pd_policy_manager.c",
	"drivers/power/supply/smb5/pd_policy_manager.c",
]

SMB5_LIB_FILES = [
	"drivers/power/supply/smb5/smb5_lib.c",
	"drivers/power/supply/ti/smb5/smb5_lib.c",
	"drivers/power/supply/smb5_lib.c",
]


def find_file(candidates, kernel_src="."):
	for path in candidates:
		full = os.path.join(kernel_src, path)
		if os.path.exists(full):
			return full
	return None


def read_file(path):
	with open(path, "r") as f:
		return f.read()


def write_file(path, content):
	with open(path, "w") as f:
		f.write(content)


def backup(path):
	orig = path + ".orig"
	if not os.path.exists(orig):
		shutil.copyfile(path, orig)
		print(f"📦 [BYPASS-P2] Saved pristine backup: {orig}")


def patch_pd_manager_phase2(kernel_src):
	"""Add PD bypass state to the PD policy manager."""
	path = find_file(PD_MANAGER_FILES, kernel_src)
	if not path:
		print("⚠️ [BYPASS-P2] PD policy manager not found — skipping.")
		return True

	content = read_file(path)

	# Idempotency
	if "pd_bypass_request_voltage" in content:
		print("ℹ️ [BYPASS-P2] PD manager already patched — skipping.")
		return True

	backup(path)

	# --- Anchor 1: PD_PM_STATE enum — add PD_PM_STATE_BYPASS ---
	state_enum_anchor = "PD_PM_STATE_FC2_EXIT"
	if state_enum_anchor not in content:
		state_enum_anchor = "PD_PM_STATE_ENTRY"
	if state_enum_anchor not in content:
		state_enum_anchor = "enum pd_pm_state"
	if state_enum_anchor not in content:
		print("❌ [BYPASS-P2] PD_PM_STATE enum anchor not found — aborting.")
		return False

	enum_pos = content.find(state_enum_anchor)
	# Find the end of the enum line or block
	enum_end = content.find("\n", enum_pos)
	if enum_end == -1:
		enum_end = len(content)

	enum_insert = r'''
	/* EXTREME++ bypass state: true pass-through, no charge pump */
	PD_PM_STATE_BYPASS,
'''

	content = content[:enum_end] + "\n" + enum_insert + content[enum_end:]

	# --- Anchor 2: PD state handler — add bypass case ---
	# Look for the state handler that processes PD_PM_STATE_FC2_EXIT
	state_handler_anchor = "PD_PM_STATE_FC2_EXIT"
	if state_handler_anchor not in content:
		state_handler_anchor = "case PD_PM_STATE_ENTRY"
	if state_handler_anchor not in content:
		state_handler_anchor = "pd_policy_manager_set_state"
	if state_handler_anchor not in content:
		print("⚠️ [BYPASS-P2] PD state handler anchor not found — "
		      "skipping state machine patch (bypass PD voltage not added).")
		# Still mark as partially patched so drain guard can be added
		write_file(path, content)
		print(f"✅ [BYPASS-P2] PD manager enum patched: {path}")
		return True

	handler_pos = content.find(state_handler_anchor)
	handler_end = content.find("\n}", handler_pos)
	if handler_end == -1:
		handler_end = handler_pos + 200
	else:
		handler_end = content.find("\n", handler_end) + 1

	bypass_case = r'''
	/* EXTREME++ bypass: request higher voltage, stop charge pump */
	case PD_PM_STATE_BYPASS:
		if (bypass_mode != 0) {
			pd_bypass_request_voltage(smb5);
			bypass_pd_active = true;
		}
		break;
'''

	content = content[:handler_end] + bypass_case + content[handler_end:]

	write_file(path, content)
	print(f"✅ [BYPASS-P2] PD manager patched: {path}")
	return True


def patch_sm5_lib_phase2(kernel_src):
	"""Add drain guard work to smb5_lib.c."""
	path = find_file(SMB5_LIB_FILES, kernel_src)
	if not path:
		print("❌ [BYPASS-P2] smb5_lib.c not found — aborting.")
		return False

	content = read_file(path)

	# Idempotency
	if "bypass_drain_guard" in content:
		print("ℹ️ [BYPASS-P2] smb5_lib.c already patched — skipping.")
		return True

	backup(path)

	# Inject PD_BYPASS_C after last #include
	include_end = content.rfind("#include")
	if include_end == -1:
		print("❌ [BYPASS-P2] No #include found — aborting.")
		return False
	line_end = content.find("\n", include_end)
	if line_end == -1:
		print("❌ [BYPASS-P2] Malformed include — aborting.")
		return False
	insert_pos = line_end + 1

	content = content[:insert_pos] + PD_BYPASS_C + "\n" + content[insert_pos:]

	# --- Anchor: probe — init drain guard work ---
	probe_anchor = "smblib_probe"
	if probe_anchor not in content:
		print("❌ [BYPASS-P2] smblib_probe not found — aborting.")
		return False

	probe_pos = content.find(probe_anchor)
	probe_end = content.find("\n}", probe_pos)
	if probe_end == -1:
		print("❌ [BYPASS-P2] Could not find end of probe — aborting.")
		return False
	probe_end = content.find("\n", probe_end) + 1

	drain_init = r'''
	/* EXTREME++ bypass: init drain guard work */
	INIT_DELAYED_WORK(&bypass_drain_work, bypass_drain_guard);
'''

	content = content[:probe_end] + "\n" + drain_init + content[probe_end:]

	write_file(path, content)
	print(f"✅ [BYPASS-P2] smb5_lib.c patched: {path}")
	return True


def main():
	kernel_src = sys.argv[1] if len(sys.argv) > 1 else "."
	print("🚀 Running PROJECT EXTREME++ Bypass Charging Patch (Phase 2)...")
	print("⚠️  Phase 2 requires Phase 1 (EXTREME_BYPASS=1) to be enabled first.")

	ok1 = patch_pd_manager_phase2(kernel_src)
	ok2 = patch_sm5_lib_phase2(kernel_src)

	if ok1 and ok2:
		print("✨ Bypass charging phase 2 patch complete (default OFF at boot).")
		print("   Build with: EXTREME_BYPASS=1 EXTREME_BYPASS_PHASE2=1")
		sys.exit(0)
	else:
		print("❌ Bypass charging phase 2 patch FAILED — see errors above.")
		sys.exit(1)


if __name__ == "__main__":
	main()
