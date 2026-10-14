# Pitstop — CNC Mill M-01 Maintenance Manual

**Document:** CNC-M01 | **Version:** 1.0 | **Purpose:** Fictional hackathon demonstration

> **Safety notice:** This is a simulated reference for an AI maintenance-planning agent, not an OEM service manual. Physical repair requires trained personnel, site-approved lockout/tagout, manufacturer procedures, and verification of a safe state. Never bypass guards or interlocks.

## Tool wear

**Failure type:** `tool_wear` (primary cutting tool)

**Symptoms:** Increasing cutting torque or load, deteriorating finish on main cutting passes, burrs, chatter, dimensional drift, and high tool-wear readings. A model alert should trigger inspection, not be treated as proof of damage.

**Likely cause:** The primary 10 mm cutting end mill is worn or chipped.

**Correct part number and name:** **EM-1004 — Carbide end mill 10 mm 4-flute**.

**Fix:** Replace the worn primary cutting tool after confirming it is the affected tool.

**Repair time:** **30 minutes** (demo estimate).

**Required skill:** **`cnc_tooling`**.

**Repair steps:**
1. Record the machine alarm, tool-wear minutes, torque, spindle speed, and active tool identifier.
2. Stop the machining cycle; a qualified engineer applies the site's isolation and safety procedure.
3. Inspect the primary 10 mm end mill, holder, and workpiece for damage.
4. The qualified engineer replaces the end mill with **EM-1004**, following the OEM tooling procedure, and checks the tool offsets.
5. After authorized restoration, perform a controlled test cut and inspect load, dimensions, and surface finish.
6. Record the repair and hand the machine back to the operator.

**Look-alike warning:** **EM-1006** is a smaller **6 mm finishing tool**, not the primary 10 mm end mill. **CP-220** is a coolant pump. Neither is a substitute for **EM-1004**.

**Planning note:** If **EM-1004** has to be ordered, the repair can start only after the part arrives.

## Finishing tool

**Failure type:** `finishing_tool`

**Symptoms:** Fine scoring, roughness, burrs, or dimensional problems concentrated in the final finishing pass while the roughing pass appears normal.

**Likely cause:** Worn or chipped 6 mm finishing end mill, or an incorrect finishing-tool offset.

**Correct part number and name:** **EM-1006 — Carbide end mill 6 mm 2-flute (finishing)**.

**Fix:** Confirm the finishing tool is responsible, then replace it if worn or damaged.

**Repair time:** **25 minutes** (demo estimate).

**Required skill:** **`cnc_tooling`**.

**Repair steps:**
1. Confirm that the problem occurs during the 6 mm finishing operation.
2. Record the finishing tool identifier and inspect the defect pattern.
3. Stop and isolate the machine according to the site's safety procedure.
4. Have a qualified tooling engineer inspect the tool, holder, and finishing offsets.
5. If replacement is justified, use **EM-1006**.
6. Replace and verify the finishing tool under the OEM procedure, then perform an authorized test finishing pass.
7. Confirm surface quality and dimensional accuracy and record the outcome.

**Look-alike warning:** **EM-1004** is the 10 mm primary cutting end mill. A general complaint about poor finish does not by itself identify which tool failed. **CP-220** should only be considered when coolant-flow evidence supports a pump fault.

**Planning note:** Do not substitute **EM-1004** for **EM-1006**.

## Coolant pump

**Failure type:** `coolant_pump`

**Symptoms:** Low or absent coolant flow, flow alarms, intermittent circulation, overheating, or reduced machining quality associated with inadequate cooling.

**Likely cause:** Pump failure, blocked coolant line or filter, or low coolant level.

**Correct part number and name:** **CP-220 — Coolant pump**.

**Fix:** Identify whether the pump itself has failed; replace **CP-220** only if a pump fault is confirmed.

**Repair time:** **90 minutes** (demo estimate).

**Required skill:** **`mechanical`**.

**Repair steps:**
1. Stop machining if cooling is inadequate and record the flow alarm and operating conditions.
2. Have a qualified mechanical engineer safely inspect coolant level, filters, hoses, and circulation.
3. Distinguish a blocked flow path from an actual failed pump.
4. If the pump has failed, the qualified engineer isolates the machine and replaces it with **CP-220** under OEM and site fluid-handling procedures.
5. Verify coolant flow, absence of leaks, and cleared alarms before authorized restart.
6. Document the work and communicate the outcome to the operator.

**Look-alike warning:** **CP-220** is not a cutting tool. Do not order **EM-1004** or **EM-1006** to repair a confirmed coolant pump fault. Poor surface finish can be caused by either tool wear or cooling problems, so check the actual symptoms.

**Planning note:** If **CP-220** has to be ordered, the repair can start only after the part arrives.

## Spindle bearing

**Failure type:** `spindle_bearing`

**Symptoms:** Abnormal spindle noise, increasing vibration, excessive heat, runout, or surface-finish problems affecting multiple tools.

**Likely cause:** Bearing wear, lubrication problems, or another spindle/holder fault.

**Part number and name:** **Not listed in this manual.** The bearing must be identified by the OEM; never guess a part number.

**Fix:** Stop use, obtain qualified spindle diagnosis, and escalate for an OEM-confirmed replacement part if necessary.

**Repair time:** **60 minutes for initial assessment only**; actual replacement time is unknown.

**Required skill:** **`mechanical`**, with OEM spindle expertise if required.

**Repair steps:**
1. Stop the spindle when unsafe noise, heat, or vibration is detected.
2. Record speed, alarms, temperature and vibration observations.
3. Isolate the machine and arrange a qualified mechanical inspection.
4. Check whether tool-holder, lubrication, cooling, or spindle components explain the symptoms.
5. If bearing failure is suspected, obtain the OEM's exact bearing specification and approved repair plan.
6. Escalate to the maintenance manager; do not draft a purchase order using a guessed part number.
7. Resume operation only after authorized repair, testing, and sign-off.

**Look-alike warning:** Tool wear can cause vibration, but a fault across multiple tools can point to the spindle. None of **EM-1004**, **EM-1006**, or **CP-220** is a spindle bearing.

**Planning note:** Escalate to the maintenance manager: the part is not catalogued here. The 60-minute assessment is not a complete repair.

## Drive belt or spindle transmission

**Failure type:** `drive_belt`

**Symptoms:** Spindle speed fluctuates, slipping noise, failure to reach commanded RPM, or drive alarms.

**Likely cause:** Worn transmission component, belt tension issue, or drive-control fault.

**Part number and name:** **Not listed in this manual**; OEM identification required.

**Fix:** Inspect and diagnose the transmission before deciding on replacement.

**Repair time:** **45 minutes for initial assessment only**.

**Required skill:** **`mechanical`**.

**Repair steps:**
1. Stop the operation and preserve RPM readings and alarm history.
2. Follow site isolation and safety procedures.
3. Have a qualified engineer distinguish mechanical slipping from drive-control problems.
4. Identify the exact component using the OEM reference if replacement is required.
5. Escalate for parts and approval; do not substitute inventory items unrelated to the drive.
6. After authorized repair, confirm stable spindle speed under supervised testing.

**Look-alike warning:** A fluctuating RPM alone is not evidence of end-mill wear or coolant-pump failure.

**Planning note:** Escalate to the maintenance manager; never order a part that is not listed here.

## Axis positioning or backlash

**Failure type:** `axis_positioning`

**Symptoms:** Repeated positioning errors, out-of-tolerance dimensions, backlash, or axis-position alarms.

**Likely cause:** Calibration error, mechanical wear, or a servo/feedback issue.

**Part number and name:** **Not listed in this manual**; depends on the confirmed diagnosis.

**Fix:** Qualified inspection and calibration or OEM-directed repair.

**Repair time:** **60 minutes for initial assessment only**.

**Required skill:** **`mechanical`**; a controls specialist may also be needed.

**Repair steps:**
1. Pause production and flag potentially defective parts for inspection.
2. Record the affected axis, commanded positions, actual measurements, and alarms.
3. Isolate the machine as required for inspection.
4. Have a qualified engineer assess mechanical, calibration, and control causes.
5. Escalate for the correct repair and any missing component specification.
6. Verify positional repeatability and dimensional tolerance before release.

**Look-alike warning:** Dimensional drift does not automatically mean **EM-1004** needs replacement.

**Planning note:** Escalate to the maintenance manager when the diagnosis or parts are unavailable.

## Electrical or controller fault

**Failure type:** `electrical_control`

**Symptoms:** Controller resets, power errors, servo faults, or safety-interlock alarms.

**Likely cause:** Electrical supply, drive, wiring, controller, or interlock fault.

**Part number and name:** **Not listed in this manual.**

**Fix:** Specialist electrical/controls diagnosis and OEM-approved service.

**Repair time:** **60 minutes for initial assessment only**; repair duration unknown.

**Required skill:** **`electrical_controls`**.

**Repair steps:**
1. Stop operation and preserve controller alarms and fault logs.
2. Keep the machine out of service pending qualified assessment.
3. Do not bypass interlocks or access energized components without proper authorization.
4. Escalate to a certified electrical/controls specialist.
5. Obtain the correct component specification and approved repair instructions if replacement is needed.
6. Require safety verification and authorized sign-off before restart.

**Look-alike warning:** A controller fault cannot be repaired by ordering **EM-1004**, **EM-1006**, or **CP-220** without independent evidence of those faults.

**Planning note:** Escalate to a certified electrical/controls specialist.

---

## Quick reference

| Failure type | Part number | Part name | Repair minutes | Required skill |
|---|---|---|---:|---|
| `tool_wear` | **EM-1004** | Carbide end mill 10 mm 4-flute | **30** | `cnc_tooling` |
| `finishing_tool` | **EM-1006** | Carbide end mill 6 mm 2-flute (finishing) | 25 | `cnc_tooling` |
| `coolant_pump` | **CP-220** | Coolant pump | 90 | `mechanical` |
| `spindle_bearing` | None (OEM part) | Not specified | 60 (assessment only) | `mechanical` |
| `drive_belt` | None (OEM part) | Not specified | 45 (assessment only) | `mechanical` |
| `axis_positioning` | None | Depends on diagnosis | 60 (assessment only) | `mechanical` |
| `electrical_control` | None | Depends on diagnosis | 60 (assessment only) | `electrical_controls` |

**Planning rules:** A repair that needs an ordered part is scheduled after the part arrives and before the predicted failure. Purchase orders and engineer bookings are drafts until the maintenance manager approves them. If no part number is listed or no qualified engineer is free, escalate to the manager instead of guessing.
