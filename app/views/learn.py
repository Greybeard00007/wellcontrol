"""Learn page -- the teaching content from ``content/``, rendered in the app.

Runs standalone as well as under navigation, which is how it is tested.
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

_APP = Path(__file__).resolve().parent.parent
if str(_APP) not in sys.path:
    sys.path.insert(0, str(_APP))

from shared import (  # noqa: E402
    CONTENT_DIR,
    PLANNER_PAGE_PATH,
    chapter_body,
    chapter_files,
    chapter_title,
    inject_industrial_theme,
    referenced_chapters,
)
from wellcontrol import rounding  # noqa: E402

inject_industrial_theme()

st.title("Well control — the theory")
st.caption(
    "The same material as the `content/` folder, rendered here. Read in order, "
    "or jump to the well type you are working on. Kill plans link straight to "
    "the chapter that explains their method."
)

files = chapter_files()

if not files:
    st.error(
        f"No chapters found in `{CONTENT_DIR}`. The content folder should sit "
        "alongside `app/` in the project."
    )
    st.stop()

titles = {name: chapter_title(name) for name in files}

# Honour a jump from a kill plan or in-page navigation, defaulting to the first chapter.
target = st.session_state.get("learn_chapter", files[0])
if target not in files:
    target = files[0]

with st.sidebar:
    st.header("Contents")
    choice = st.radio(
        "Chapter",
        files,
        index=files.index(target),
        format_func=lambda name: titles[name],
        label_visibility="collapsed",
    )

st.session_state["learn_chapter"] = choice
position = files.index(choice)

# Reading progress indicator
st.progress((position + 1) / len(files))

header_left, header_right = st.columns([3, 1])
with header_left:
    st.subheader(titles[choice])
    st.caption(f"Chapter {position + 1} of {len(files)}  ·  `content/{choice}`")

# Direct jump to kill planner when chapter relates to a specific method / config
CHAPTER_PRESETS: dict[str, tuple[str, str]] = {
    "05-kill-sheet.md": ("Surface BOP - Vertical", "Driller's Method"),
    "06-circulation-methods.md": ("Surface BOP - Vertical", "Wait and Weight"),
    "08-directional-wells.md": ("Surface BOP - Horizontal", "Driller's Method"),
    "09-subsea-wells.md": ("Subsea BOP - Vertical", "Driller's Method"),
    "12-choosing-a-kill-method.md": ("Surface BOP - Vertical", "Volumetric Method"),
    "13-stripping.md": ("Surface BOP - Vertical", "Stripping to Bottom"),
}

with header_right:
    if choice in CHAPTER_PRESETS:
        cfg_name, method_name = CHAPTER_PRESETS[choice]
        if st.button(
            "📐 Open in Planner",
            help=f"Practice {method_name} on {cfg_name}",
            key=f"planner_jump_{choice}",
            width="stretch",
        ):
            st.session_state["planner_config"] = cfg_name
            st.session_state["planner_method"] = method_name
            try:
                st.switch_page(PLANNER_PAGE_PATH)
            except Exception:
                pass

# Cross-chapter references
related = referenced_chapters(choice)
if related:
    st.markdown("**Related chapters in this manual:**")
    ref_cols = st.columns(min(len(related), 4))
    for i, ref_file in enumerate(related[:4]):
        col = ref_cols[i % len(ref_cols)]
        ref_title = titles.get(ref_file, ref_file)
        if col.button(
            f"📖 {ref_title}",
            key=f"rel_{choice}_{ref_file}",
            help=f"Jump to {ref_title}",
            width="stretch",
        ):
            st.session_state["learn_chapter"] = ref_file
            st.rerun()

st.markdown(chapter_body(choice))

# --------------------------------------------------------------------------
# Interactive Training & Sandboxes
# --------------------------------------------------------------------------

QUIZ_DATA: dict[str, dict[str, object]] = {
    "01-fundamentals.md": {
        "question": "What is the primary well control barrier during normal drilling operations?",
        "options": [
            "The hydrostatic pressure exerted by the drilling fluid column",
            "The blowout preventer (BOP) annular preventer",
            "The surface choke manifold and mud-gas separator",
            "The formation fracture gradient at the casing shoe",
        ],
        "correct": 0,
        "explanation": (
            "Primary well control is maintained when hydrostatic pressure of the drilling mud "
            "exceeds formation pore pressure. The BOP and surface equipment are secondary barriers."
        ),
    },
    "02-hydrostatics.md": {
        "question": "Which depth must always be used when calculating hydrostatic pressure?",
        "options": [
            "True Vertical Depth (TVD)",
            "Measured Depth (MD)",
            "Drill string total length",
            "Casing shoe measured depth",
        ],
        "correct": 0,
        "explanation": (
            "Pressure is created strictly by the vertical height of fluid column: "
            "P = 0.052 × MW × TVD. Measured depth is used only for volumetric stroke counts."
        ),
    },
    "03-volumes-and-strokes.md": {
        "question": "Why are strokes to bit and strokes to surface always rounded UP on a kill sheet?",
        "options": [
            "Stopping short of the bit or shoe leaves kill mud displaced incomplete",
            "Pump liners deliver more volume under high backpressure",
            "Directional rounding requires rounding down for safety",
            "Banker's rounding dictates rounding toward the nearest even integer",
        ],
        "correct": 0,
        "explanation": (
            "Fractional strokes cannot be pumped, and under-displacing leaves kill mud short of the bit, "
            "so strokes always round up to ensure complete fluid displacement."
        ),
    },
    "04-kick-detection.md": {
        "question": "Why is Shut-In Casing Pressure (SICP) typically higher than Shut-In Drillpipe Pressure (SIDPP)?",
        "options": [
            "The annulus contains the lower-density influx, reducing hydrostatic in that column",
            "The choke line causes friction loss while the well is shut in",
            "The drill pipe has a float valve preventing pressure transmission",
            "Casing burst rating is lower than drill pipe yield strength",
        ],
        "correct": 0,
        "explanation": (
            "Both columns balance the same formation pressure at bottomhole. Because the influx has entered "
            "the annulus, the annular hydrostatic is lower than the clean mud in the drill string. "
            "Surface casing pressure must be higher to balance formation pressure."
        ),
    },
    "05-kill-sheet.md": {
        "question": "During a constant bottomhole pressure kill, which gauge must the choke operator follow?",
        "options": [
            "Drill pipe gauge (ICP down to FCP schedule)",
            "Casing gauge",
            "Choke manifold downstream pressure gauge",
            "Accumulator bottle charge gauge",
        ],
        "correct": 0,
        "explanation": (
            "The drill string contains clean mud of known density. Following the drill pipe pressure schedule "
            "keeps bottomhole pressure constant regardless of influx expansion in the annulus."
        ),
    },
    "06-circulation-methods.md": {
        "question": "What is the key advantage of the Wait and Weight method over the Driller's Method?",
        "options": [
            "Lower casing shoe pressure during circulation because kill mud enters the annulus sooner",
            "It requires no kill sheet calculations prior to starting circulation",
            "It can be performed without weighting up mud in the active pits",
            "It circulates the kick out twice as fast",
        ],
        "correct": 0,
        "explanation": (
            "Wait and Weight pumps kill mud immediately. The kill mud column in the drill string and lower "
            "annulus adds hydrostatic backpressure, reducing peak casing shoe and surface pressures."
        ),
    },
    "07-kick-tolerance.md": {
        "question": "What happens to MAASP (Maximum Allowable Annular Surface Pressure) when mud weight increases?",
        "options": [
            "MAASP decreases",
            "MAASP increases",
            "MAASP stays constant",
            "MAASP becomes equal to LOT pressure",
        ],
        "correct": 0,
        "explanation": (
            "MAASP = (LOT EMW - Current MW) × 0.052 × Shoe TVD. Heavier mud increases hydrostatic pressure "
            "at the shoe, leaving less margin before formation breakdown."
        ),
    },
    "08-directional-wells.md": {
        "question": "While a gas kick is completely contained within a horizontal lateral (90° inc), what will SICP read?",
        "options": [
            "SICP equals SIDPP because the horizontal influx has zero vertical height (TVD)",
            "SICP is substantially higher due to hole curvature",
            "SICP is zero because horizontal influxes cannot migrate",
            "SICP is double SIDPP",
        ],
        "correct": 0,
        "explanation": (
            "In a flat lateral, the influx replaces mud over measured distance but zero vertical height. "
            "Annular hydrostatic equals drill pipe hydrostatic, so SICP equals SIDPP."
        ),
    },
    "09-subsea-wells.md": {
        "question": "Why must Choke Line Friction Loss (CLFL) be accounted for on a subsea well?",
        "options": [
            "Friction in the long, narrow choke line adds backpressure directly to bottomhole and the shoe",
            "Subsea BOPs cannot close against friction pressure",
            "It reduces the hydrostatic head of seawater in the riser",
            "It changes the pump displacement efficiency",
        ],
        "correct": 0,
        "explanation": (
            "Circulating up the subsea choke line creates significant friction loss. If not compensated for "
            "by opening the choke, bottomhole pressure rises by the CLFL amount, potentially breaking the shoe."
        ),
    },
    "10-hpht-wells.md": {
        "question": "In HPHT wells, how does high downhole temperature typically affect mud density?",
        "options": [
            "Thermal expansion reduces effective downhole mud density",
            "Compressibility increases density more than thermal expansion",
            "Temperature has zero effect on oil-based muds",
            "Density increases linearly with depth regardless of temperature",
        ],
        "correct": 0,
        "explanation": (
            "High temperature causes thermal expansion, which decreases mud density downhole. In HPHT wells, "
            "this thermal reduction can erode overbalance if not modeled accurately."
        ),
    },
    "11-mpd-and-ubd.md": {
        "question": "What role does surface backpressure (SBP) serve in Managed Pressure Drilling?",
        "options": [
            "It acts as a dynamic, fast pressure lever to maintain target bottomhole pressure during pump stops",
            "It eliminates the need for any drilling fluid in the hole",
            "It replaces the subsea BOP stack",
            "It prevents drill string rotation",
        ],
        "correct": 0,
        "explanation": (
            "When the main mud pumps stop for connections, annular friction loss vanishes. MPD immediately "
            "applies surface backpressure to replace friction loss and keep bottomhole pressure constant."
        ),
    },
    "12-choosing-a-kill-method.md": {
        "question": "Why is the Volumetric Method contraindicated if a gas influx is in a horizontal lateral?",
        "options": [
            "Gas will not migrate along a horizontal wellbore without a vertical gradient",
            "Volumetric method requires pumping at 30 SPM",
            "The drill pipe must be rotated continuously",
            "Surface casing pressure cannot be measured in horizontal wells",
        ],
        "correct": 0,
        "explanation": (
            "The volumetric method relies on gas naturally migrating upward to build surface pressure. "
            "In a horizontal lateral, there is no upward component, so the bubble remains stationary."
        ),
    },
    "13-stripping.md": {
        "question": "When stripping closed-end drill pipe into a shut-in well, what volume of mud must be bled off?",
        "options": [
            "The closed-end displacement (OD displacement) of the stripped pipe",
            "Only the pipe steel displacement (OD² - ID²)",
            "Zero volume until bit reaches bottom",
            "Double the internal capacity of the pipe",
        ],
        "correct": 0,
        "explanation": (
            "With a closed bit or float valve, the entire external volume (OD displacement) of the pipe "
            "displaces fluid into the well. Exactly this volume must be bled to keep BHP constant."
        ),
    },
}

with st.expander("📝 Knowledge Check & Training Quiz", expanded=False):
    quiz = QUIZ_DATA.get(choice)
    if quiz:
        st.markdown(f"**Question:** {quiz['question']}")
        options = quiz["options"]
        ans = st.radio(
            "Select your answer:",
            options,
            key=f"quiz_{choice}",
            index=None,
        )
        if ans is not None:
            selected_idx = options.index(ans)
            if selected_idx == quiz["correct"]:
                st.success(f"✅ **Correct!** {quiz['explanation']}")
            else:
                st.error(f"❌ **Incorrect.** {quiz['explanation']}")
    else:
        st.info("Review the chapter above to master key well control principles.")

# Interactive Sandbox for calculation chapters
if choice in ("02-hydrostatics.md", "03-volumes-and-strokes.md", "05-kill-sheet.md", "07-kick-tolerance.md", "13-stripping.md"):
    with st.expander("🧮 Interactive Formula Sandbox", expanded=False):
        if choice == "02-hydrostatics.md":
            st.markdown("##### Hydrostatic Pressure & ECD Calculator")
            c1, c2, c3 = st.columns(3)
            sb_mw = c1.number_input("Mud Weight (ppg)", value=10.0, step=0.1, min_value=1.0, max_value=25.0, key=f"sb_{choice}_mw")
            sb_tvd = c2.number_input("TVD (ft)", value=10000.0, step=500.0, min_value=100.0, key=f"sb_{choice}_tvd")
            sb_apl = c3.number_input("Annular Pressure Loss (psi)", value=250.0, step=25.0, min_value=0.0, key=f"sb_{choice}_apl")
            hp = 0.052 * sb_mw * sb_tvd
            ecd = sb_mw + (sb_apl / (0.052 * sb_tvd)) if sb_tvd > 0 else sb_mw
            r1, r2 = st.columns(2)
            r1.metric("Hydrostatic Pressure", f"{hp:,.0f} psi")
            r2.metric("Equivalent Circulating Density (ECD)", f"{ecd:.2f} ppg")

        elif choice == "03-volumes-and-strokes.md":
            st.markdown("##### Capacity & Pump Strokes Calculator")
            c1, c2, c3 = st.columns(3)
            sb_id = c1.number_input("Pipe ID (in)", value=4.276, step=0.1, min_value=1.0, max_value=12.0, key=f"sb_{choice}_id")
            sb_len = c2.number_input("Pipe Length (ft)", value=10000.0, step=500.0, min_value=10.0, key=f"sb_{choice}_len")
            sb_pump = c3.number_input("Pump Output (bbl/stk)", value=0.119, step=0.01, min_value=0.01, key=f"sb_{choice}_pump")
            cap = (sb_id ** 2) / 1029.4
            vol = cap * sb_len
            stks = rounding.strokes(vol / sb_pump) if sb_pump > 0 else 0
            r1, r2, r3 = st.columns(3)
            r1.metric("Capacity", f"{cap:.5f} bbl/ft")
            r2.metric("Total Volume", f"{rounding.volume(vol):.1f} bbl")
            r3.metric("Strokes to Displace", f"{stks:,} stks")

        elif choice == "05-kill-sheet.md":
            st.markdown("##### Kill Mud Weight, ICP & FCP Calculator")
            c1, c2 = st.columns(2)
            sb_mw = c1.number_input("Current Mud Weight (ppg)", value=10.0, step=0.1, min_value=1.0, key=f"sb_{choice}_mw")
            sb_tvd = c2.number_input("Bit TVD (ft)", value=10000.0, step=500.0, min_value=100.0, key=f"sb_{choice}_tvd")
            c3, c4 = st.columns(2)
            sb_sidpp = c3.number_input("SIDPP (psi)", value=550.0, step=25.0, min_value=0.0, key=f"sb_{choice}_sidpp")
            sb_scr = c4.number_input("SCR Pressure at kill rate (psi)", value=750.0, step=25.0, min_value=0.0, key=f"sb_{choice}_scr")
            kmw_exact = sb_mw + (sb_sidpp / (0.052 * sb_tvd)) if sb_tvd > 0 else sb_mw
            kmw = rounding.kill_mud_weight(kmw_exact)
            icp = sb_sidpp + sb_scr
            fcp = rounding.pressure_step(sb_scr * (kmw / sb_mw)) if sb_mw > 0 else sb_scr
            r1, r2, r3 = st.columns(3)
            r1.metric("Kill Mud Weight (KMW)", f"{kmw:.1f} ppg", help=f"Exact: {kmw_exact:.2f} ppg (rounded UP)")
            r2.metric("Initial Circulating Pressure (ICP)", f"{icp:,.0f} psi")
            r3.metric("Final Circulating Pressure (FCP)", f"{fcp:,.0f} psi")

        elif choice == "07-kick-tolerance.md":
            st.markdown("##### MAASP Calculator")
            c1, c2, c3 = st.columns(3)
            sb_lot = c1.number_input("Leak-Off Test EMW (ppg)", value=14.5, step=0.1, min_value=1.0, key=f"sb_{choice}_lot")
            sb_mw = c2.number_input("Current Mud Weight (ppg)", value=10.5, step=0.1, min_value=1.0, key=f"sb_{choice}_cur_mw")
            sb_shoe = c3.number_input("Casing Shoe TVD (ft)", value=6000.0, step=200.0, min_value=100.0, key=f"sb_{choice}_shoe")
            maasp_psi = rounding.pressure_limit((sb_lot - sb_mw) * 0.052 * sb_shoe) if sb_shoe > 0 and sb_lot >= sb_mw else 0
            st.metric("MAASP (Maximum Allowable Annular Surface Pressure)", f"{maasp_psi:,.0f} psi", help="Rounded DOWN for safety margin")

        elif choice == "13-stripping.md":
            st.markdown("##### Stripping Bleed Volume Calculator")
            c1, c2 = st.columns(2)
            sb_od = c1.number_input("Pipe OD (in)", value=5.0, step=0.1, min_value=1.0, key=f"sb_{choice}_od")
            sb_id = c2.number_input("Pipe ID (in)", value=4.276, step=0.1, min_value=0.5, key=f"sb_{choice}_id")
            c3, c4 = st.columns(2)
            sb_stand = c3.number_input("Stand Length (ft)", value=93.0, step=1.0, min_value=10.0, key=f"sb_{choice}_stand")
            sb_closed = c4.checkbox("Bit is closed-ended / float valve installed", value=True, key=f"sb_{choice}_closed")
            disp_ft = (sb_od ** 2 / 1029.4) if sb_closed else ((sb_od ** 2 - sb_id ** 2) / 1029.4)
            stand_bleed = rounding.volume(disp_ft * sb_stand)
            r1, r2 = st.columns(2)
            r1.metric("Displacement per foot", f"{disp_ft:.5f} bbl/ft")
            r2.metric("Bleed Volume per Stand", f"{stand_bleed:.2f} bbl")

st.divider()
previous_column, next_column = st.columns(2)

if position > 0:
    previous_column.button(
        f"← {titles[files[position - 1]]}",
        width="stretch",
        key="learn_previous",
        on_click=lambda: st.session_state.update(
            learn_chapter=files[position - 1]
        ),
    )

if position < len(files) - 1:
    next_column.button(
        f"{titles[files[position + 1]]} →",
        width="stretch",
        key="learn_next",
        on_click=lambda: st.session_state.update(
            learn_chapter=files[position + 1]
        ),
    )

st.divider()
st.caption(
    "These are engineering aids, not a substitute for certification, a company "
    "well control manual, or the judgement of a competent person on the rig."
)
