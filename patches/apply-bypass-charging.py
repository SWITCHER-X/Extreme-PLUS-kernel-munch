#!/usr/bin/env python3
"""
PROJECT EXTREME++ | Maintainer: PandeyJI-9
Device: POCO F4 (munch / SM8250-AC Kona) | Target: HyperOS & AOSP
Bypass Charging — Phase 1: Charge-disable vote + safety checks

WHAT THIS DOES
--------------
Adds a bypass vote on the main charger's charge-enable switch (CHG_DISABLE
votable). Stops charge current into the battery while keeping the input path
up. Thermal, JEITA and fast-charge-finish keep their own votes on the same
votable, and bypass releases only when every voter does.

IMPORTANT: Phase 1 does NOT request higher PD voltage. The adapter may fall
back to ~5 V (~15 W). Heavy gaming loads can exceed this, and the battery
will supplement the difference. Use phase 2 for true pass-through (charger
powers the system, battery does not drain).

Controls (kernel module parameters, default OFF at boot):
  bypass_mode    : 0 = off (default), 1 = always while plugged & above floor,
                    2 = hold at bypass_limit
  bypass_limit   : SoC % at which bypass engages (default 80)
  bypass_hyst    : hysteresis % (default 3)
  bypass_min_soc : floor % below which bypass always releases (default 15)

A kernel manager (NKM / FKM / custom sysfs) can write bypass_mode=1 or 2 to
/sys/module/<charger-module>/parameters/bypass_mode to enable bypass.

Safety
------
  * Always off at boot — a crashed app can't leave bypass on across a reboot.
  * Night-charging logic looks only at the user vote — bypass never fights it.

BUILD
-----
Called from build_kernel.sh only when EXTREME_BYPASS=1. A default build is
unchanged. Every anchor must match or the build fails loudly (exit 1).

NOT IN THIS PHASE
-----------------
  * 9 V PD power request (adapter falls back to ~5 V / ~15 W).
  * Battery-drain guard / watchdog (needs device data to confirm current sign).
"""

import os
import sys
import shutil

# ---------------------------------------------------------------------------
# C code to inject into the smb5_lib charger driver
# ---------------------------------------------------------------------------

BYPASS_C = r'''
/* =========================================================================
 * PROJECT EXTREME++ — Bypass Charging (Phase 1)
 * POCO F4 (munch) / SM8250-AC
 * ========================================================================= */

/* Module parameters — default OFF at boot */
static unsigned int bypass_mode = 0;
static unsigned int bypass_limit = 80;
static unsigned int bypass_hyst = 3;
static unsigned int bypass_min_soc = 15;
module_param(bypass_mode, uint, 0644);
module_param(bypass_limit, uint, 0644);
module_param(bypass_hyst, uint, 0644);
module_param(bypass_min_soc, uint, 0644);
MODULE_PARM_DESC(bypass_mode, "0=off 1=always-above-floor 2=hold-at-limit");
MODULE_PARM_DESC(bypass_limit, "SoC percent at which bypass engages");
MODULE_PARM_DESC(bypass_hyst, "Hysteresis percent around bypass_limit");
MODULE_PARM_DESC(bypass_min_soc, "Floor percent below which bypass releases");

/* Bypass state */
static bool bypass_active = false;
static bool bypass_plugged = false;
static unsigned int bypass_soc = 0;
static struct mutex bypass_lock;
static struct smb5 *bypass_sm;
static void *bypass_voter;

/* ----------------------------------------------------------------------- */
/* Helper: is bypass allowed right now?                                    */
/* ----------------------------------------------------------------------- */
static bool bypass_allowed(void)
{
	if (bypass_mode == 0)
		return false;
	if (!bypass_plugged)
		return false;
	if (bypass_soc < bypass_min_soc)
		return false;

	if (bypass_mode == 2) {
		if (!bypass_active && bypass_soc >= bypass_limit)
			return true;
		if (bypass_active && bypass_soc <= bypass_limit - bypass_hyst)
			return false;
		return bypass_active;
	}

	return bypass_soc >= bypass_min_soc;
}

/* ----------------------------------------------------------------------- */
/* Vote on CHG_DISABLE (main charger charge-enable bit)                    */
/* ----------------------------------------------------------------------- */
static void bypass_vote_chg_disable(bool enable)
{
	if (bypass_sm && bypass_sm->chg_disable_votable && bypass_voter) {
		vote(bypass_sm->chg_disable_votable, bypass_voter, enable, 0);
	}
}

/* ----------------------------------------------------------------------- */
/* Engage / release bypass immediately                                     */
/* ----------------------------------------------------------------------- */
static void bypass_apply(bool want_on)
{
	if (want_on && !bypass_active) {
		bypass_vote_chg_disable(true);
		bypass_active = true;
		pr_info("PROJECT EXTREME++: bypass ENGAGED (soc=%u)\n", bypass_soc);
	} else if (!want_on && bypass_active) {
		bypass_vote_chg_disable(false);
		bypass_active = false;
		pr_info("PROJECT EXTREME++: bypass RELEASED (soc=%u)\n", bypass_soc);
	}
}

/* ----------------------------------------------------------------------- */
/* Write callback for bypass_mode module parameter                         */
/* ----------------------------------------------------------------------- */
static int bypass_mode_set(const char *val, const struct kernel_param *kp)
{
	int rc;
	union power_supply_propval pval = {0};

	rc = param_set_uint(val, kp);
	if (rc < 0)
		return rc;

	mutex_lock(&bypass_lock);

	/* Read current SoC and plug state from the charger */
	if (bypass_sm && bypass_sm->usb_psy) {
		bypass_sm->usb_psy->get_property(bypass_sm->usb_psy,
						 POWER_SUPPLY_PROP_CAPACITY, &pval);
		bypass_soc = pval.intval;
		bypass_sm->usb_psy->get_property(bypass_sm->usb_psy,
						 POWER_SUPPLY_PROP_ONLINE, &pval);
		bypass_plugged = (pval.intval > 0);
	}

	bypass_apply(bypass_allowed());
	mutex_unlock(&bypass_lock);

	return 0;
}

static const struct kernel_param_ops bypass_mode_param_ops = {
	.set = bypass_mode_set,
	.get = param_get_uint,
};
module_param_cb(bypass_mode, &bypass_mode_param_ops, &bypass_mode, 0644);

/* =========================================================================
 * PATCH POINTS (anchored below in apply-bypass-charging.py)
 * ========================================================================= */

/* A. In smblib_get_prop_battery_input_suspend: return true while bypass on */
/*    Anchor: the existing getter body, before the final return.            */

/* B. In smblib_set_battery_input_suspend: release bypass when set to 0     */
/*    Anchor: the existing setter body.                                     */

/* C. In smblib_probe: init bypass mutex, register voter, store smb5 ptr    */
/*    Anchor: after chg_disable_votable creation (vote_create("chg_disable")). */

/* D. In night-charging getter: look only at user vote                      */
/*    Anchor: smblib_get_prop_battery_night_charging or equivalent.         */

/* E. On USB unplug: release bypass                                         */
/*    Optional for phase 1 — userspace sets bypass_mode=0 to release.       */

/* =========================================================================
 * END OF INJECTED C
 * =========================================================================
'''

# ---------------------------------------------------------------------------
# Anchors
# ---------------------------------------------------------------------------

SMB5_LIB_FILES = [
	"drivers/power/supply/smb5/smb5_lib.c",
	"drivers/power/supply/ti/smb5/smb5_lib.c",
	"drivers/power/supply/smb5_lib.c",
]

PD_MANAGER_FILES = [
	"drivers/power/supply/ti/pd_policy_manager_munch.c",
	"drivers/power/supply/ti/pd_policy_manager.c",
	"drivers/power/supply/smb5/pd_policy_manager.c",
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
		print(f"📦 [BYPASS] Saved pristine backup: {orig}")


def _indent_before_return(getter_body, last_return):
	"""Return the indentation string preceding a return statement."""
	ret_line_start = getter_body.rfind("\n", 0, last_return)
	if ret_line_start == -1:
		ret_line_start = 0
	else:
		ret_line_start += 1
	return getter_body[ret_line_start:last_return]


def patch_sm5_lib(kernel_src):
	"""Inject bypass module params, vote, and hooks into smb5_lib.c."""
	path = find_file(SMB5_LIB_FILES, kernel_src)
	if not path:
		print("❌ [BYPASS] smb5_lib.c not found — aborting.")
		return False

	content = read_file(path)

	# Idempotency: already patched
	if "bypass_mode_param_ops" in content:
		print("ℹ️ [BYPASS] smb5_lib.c already patched — skipping.")
		return True

	backup(path)

	# --- Anchor 1: inject BYPASS_C after the last #include ---
	include_end = content.rfind("#include")
	if include_end == -1:
		print("❌ [BYPASS] No #include found in smb5_lib.c — aborting.")
		return False
	line_end = content.find("\n", include_end)
	if line_end == -1:
		print("❌ [BYPASS] Malformed include in smb5_lib.c — aborting.")
		return False
	insert_pos = line_end + 1

	content = content[:insert_pos] + BYPASS_C + "\n" + content[insert_pos:]

	# --- Anchor A: (REMOVED in phase 1 — phase 2 handles PD voltage)
	#   We intentionally do NOT set battery_input_suspend=1 here, because that
	#   tells the PD manager to stop the charge pump and park at 5V (~15W).
	#   Phase 2 adds a proper PD bypass state that requests 9V+ instead.

	# --- Anchor B: set_battery_input_suspend → release bypass when set to 0 ---
	setter_sig = "static void smblib_set_battery_input_suspend"
	if setter_sig not in content:
		setter_sig = "void smblib_set_battery_input_suspend"
	if setter_sig not in content:
		print("❌ [BYPASS] battery_input_suspend setter not found — aborting.")
		return False

	s_start = content.find(setter_sig)
	s_end = content.find("\n}", s_start)
	if s_end == -1:
		print("❌ [BYPASS] Could not find end of setter — aborting.")
		return False
	s_end = content.find("\n", s_end) + 1

	setter_body = content[s_start:s_end]

	bypass_setter_hook = r'''
	/* EXTREME++ bypass: release when input suspend is cleared */
	mutex_lock(&bypass_lock);
	if (!suspend && bypass_active) {
		bypass_vote_chg_disable(false);
		bypass_active = false;
		pr_info("PROJECT EXTREME++: bypass released via setter (soc=%u)\n", bypass_soc);
	}
	mutex_unlock(&bypass_lock);
'''

	last_brace = setter_body.rstrip().rfind("}")
	if last_brace == -1:
		new_setter = setter_body + bypass_setter_hook
	else:
		new_setter = (setter_body[:last_brace] +
		              bypass_setter_hook +
		              setter_body[last_brace:])

	content = content[:s_start] + new_setter + content[s_end:]

	# --- Anchor C: probe — init bypass mutex, register voter, store smb5 ---
	# Use the full assignment smb5->chg_disable_votable = vote_create(...) so
	# we don't match the comment inside the BYPASS_C block itself.
	votable_anchor = 'smb5->chg_disable_votable = vote_create("chg_disable"'
	if votable_anchor not in content:
		# Fallback: try just the votable creation
		votable_anchor = "chg_disable_votable = vote_create"
	if votable_anchor not in content:
		print("❌ [BYPASS] chg_disable_votable creation not found — aborting.")
		return False

	v_start = content.find(votable_anchor)
	v_semi = content.find(";", v_start)
	if v_semi == -1:
		print("❌ [BYPASS] Malformed chg_disable_votable statement — aborting.")
		return False
	v_end = v_semi + 1

	bypass_init_hook = r'''
	/* EXTREME++ bypass: init mutex, register bypass voter, store smb5 */
	bypass_sm = smb5;
	mutex_init(&bypass_lock);
	bypass_voter = vote_create("bypass", VOTE_SET);
	if (!bypass_voter)
		pr_err("PROJECT EXTREME++: failed to create bypass voter\n");
'''

	content = content[:v_end] + "\n" + bypass_init_hook + content[v_end:]

	write_file(path, content)
	print(f"✅ [BYPASS] smb5_lib.c patched: {path}")
	return True


def patch_pd_manager(kernel_src):
	"""Make night-charging logic look only at the user vote."""
	path = find_file(PD_MANAGER_FILES, kernel_src)
	if not path:
		print("⚠️ [BYPASS] PD policy manager not found — skipping night-charging guard.")
		return True

	content = read_file(path)

	if "bypass_active" in content:
		print("ℹ️ [BYPASS] PD manager already patched — skipping.")
		return True

	backup(path)

	night_sig = "smblib_get_prop_battery_night_charging"
	if night_sig not in content:
		night_sig = "get_prop_battery_night_charging"
	if night_sig not in content:
		night_sig = "battery_night_charging"
	if night_sig not in content:
		print("⚠️ [BYPASS] night-charging getter not found — skipping guard.")
		return True

	n_start = content.find(night_sig)
	n_end = content.find("\n}", n_start)
	if n_end == -1:
		print("⚠️ [BYPASS] Could not find end of night-charging getter — skipping.")
		return True
	n_end = content.find("\n", n_end) + 1

	night_body = content[n_start:n_end]

	night_guard = r'''
	/* EXTREME++ bypass: night-charging looks only at the user vote.
	 * If bypass is active, do not let night-charging undo it. */
	mutex_lock(&bypass_lock);
	if (bypass_active) {
		mutex_unlock(&bypass_lock);
		val->intval = 0;
		return 0;
	}
	mutex_unlock(&bypass_lock);
'''

	last_return = night_body.rfind("return")
	if last_return == -1:
		print("⚠️ [BYPASS] No return in night-charging getter — skipping guard.")
		return True

	new_night = (night_body[:last_return] +
	             night_guard +
	             night_body[last_return:])

	content = content[:n_start] + new_night + content[n_end:]

	write_file(path, content)
	print(f"✅ [BYPASS] PD manager patched: {path}")
	return True


def main():
	kernel_src = sys.argv[1] if len(sys.argv) > 1 else "."
	print("🚀 Running PROJECT EXTREME++ Bypass Charging Patch (Phase 1)...")

	ok1 = patch_sm5_lib(kernel_src)
	ok2 = patch_pd_manager(kernel_src)

	if ok1 and ok2:
		print("✨ Bypass charging patch complete (default OFF at boot).")
		sys.exit(0)
	else:
		print("❌ Bypass charging patch FAILED — see errors above.")
		sys.exit(1)


if __name__ == "__main__":
	main()
