"""Kill Planner page -- pick a well and a method, get the procedure and plots.

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
    DEFAULTS,
    LEARN_PAGE_PATH,
    as_text_rows,
    build_well,
    chapter_exists,
    chapter_title,
    detect_theme,
    inject_industrial_theme,
    read_upload_as_csv,
    trajectory_from_survey,
)
from wellcontrol.importers import import_survey, import_tally  # noqa: E402
from wellcontrol.methods import METHODS, get_method, strip_then_kill  # noqa: E402
from wellcontrol.plotting import plot_kill, trace_table  # noqa: E402
from wellcontrol.wellconfig import WellConfiguration  # noqa: E402

inject_industrial_theme()

st.title("Well Control Kill Planner")
st.caption(
    "Select the well type and kill method, enter the data, and the kill is "
    "calculated step by step with diagnostic plots."
)

configs = WellConfiguration.standard()

config_options = [c.label for c in configs]
target_config = st.session_state.get("planner_config")
config_idx = 0
if target_config:
    for idx, opt in enumerate(config_options):
        if target_config.lower() in opt.lower() or opt.lower() in target_config.lower():
            config_idx = idx
            break

method_options = [m.name for m in METHODS]
target_method = st.session_state.get("planner_method")
method_idx = 1
if target_method:
    for idx, opt in enumerate(method_options):
        if target_method.lower() in opt.lower() or opt.lower() in target_method.lower():
            method_idx = idx
            break

selector_left, selector_right = st.columns(2)
with selector_left:
    config_label = st.selectbox(
        "Well configuration", config_options, index=config_idx
    )
    config = next(c for c in configs if c.label == config_label)
with selector_right:
    method_label = st.selectbox("Kill method", method_options, index=method_idx)
    method = next(m for m in METHODS if m.name == method_label)

st.caption(method.summary)

defaults = DEFAULTS[config.key]

# --------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------

with st.sidebar:
    st.header("Well data")

    with st.expander("📄 Import survey and tally", expanded=False):
        st.caption(
            "Optional. Uploading the real survey and tally replaces the "
            "idealised geometry below — TVD comes from the actual trajectory "
            "and string volume from the actual components. CSV or Excel. "
            "See `samples/` for the formats."
        )
        survey_file = st.file_uploader(
            "Directional survey",
            type=["csv", "xlsx", "xlsm", "xls"],
            help="Needs measured depth and inclination. Azimuth and TVD are "
                 "used when present. TVD is computed by minimum curvature "
                 "otherwise.",
            key="survey_upload",
        )
        tally_file = st.file_uploader(
            "Pipe tally",
            type=["csv", "xlsx", "xlsm", "xls"],
            help="Needs OD, ID and length. A joints or count column multiplies "
                 "the length.",
            key="tally_upload",
        )
        tally_bottom_up = st.checkbox(
            "Tally is written bottom-up (bit first)",
            value=True,
            help="Untick for a tally written from surface down.",
        )

    with st.expander("Hole and casing", expanded=True):
        total_md = st.number_input(
            "Total depth (ft MD)", 1000.0, 40000.0, defaults["total_md"], 100.0
        )
        shoe_md = st.number_input(
            "Casing shoe (ft MD)", 100.0, 40000.0, defaults["shoe_md"], 100.0
        )
        casing_id = st.number_input(
            "Casing ID (in)", 3.0, 30.0, defaults["casing_id"], 0.001,
            format="%.3f",
        )
        hole_id = st.number_input(
            "Open hole ID (in)", 3.0, 30.0, defaults["hole_id"], 0.001,
            format="%.3f",
        )

    if config.is_subsea:
        with st.expander("Subsea", expanded=True):
            air_gap = st.number_input(
                "Air gap (ft)", 0.0, 300.0, defaults["air_gap"], 5.0
            )
            water_depth = st.number_input(
                "Water depth (ft)", 100.0, 15000.0, defaults["water_depth"], 100.0
            )
            riser_id = st.number_input("Riser ID (in)", 10.0, 24.0, 19.5, 0.5)
            clf = st.number_input(
                "Choke line friction at kill rate (psi)",
                0.0, 3000.0, defaults["clf"], 25.0,
                help="Measured: SCR through the choke line minus SCR through the "
                     "riser. Acts on the formation, so it is subtracted from the "
                     "circulating pressure targets.",
            )
    else:
        air_gap = water_depth = 0.0
        riser_id = 19.5
        clf = 0.0

    if config.is_horizontal:
        with st.expander("Trajectory", expanded=True):
            kop = st.number_input(
                "Kick-off point (ft MD)", 0.0, 30000.0, defaults["kop"], 100.0
            )
            build = st.number_input(
                "Build rate (deg/100 ft)", 0.5, 15.0, defaults["build"], 0.5
            )
    else:
        kop, build = 0.0, 3.0

    with st.expander("Drill string", expanded=False):
        bit_md = st.number_input(
            "Bit depth (ft MD)", 100.0, 40000.0, defaults["bit_md"], 100.0,
            help="Set this above total depth to plan a Stripping operation.",
        )
        dp_od = st.number_input("Drill pipe OD (in)", 2.0, 8.0,
                                defaults["dp_od"], 0.125)
        dp_id = st.number_input("Drill pipe ID (in)", 1.0, 7.0,
                                defaults["dp_id"], 0.001, format="%.3f")
        dc_od = st.number_input("Collar OD (in)", 2.0, 12.0,
                                defaults["dc_od"], 0.125)
        dc_id = st.number_input("Collar ID (in)", 1.0, 6.0,
                                defaults["dc_id"], 0.0625, format="%.4f")
        dc_len = st.number_input("Collar length (ft)", 0.0, 3000.0,
                                 defaults["dc_len"], 30.0)
        dp_weight = st.number_input(
            "Drill pipe weight (lb/ft)", 5.0, 60.0, defaults["dp_weight"], 0.5,
            help="Nominal weight in air. Sets the balance point between pipe "
                 "weight and well pressure when stripping.",
        )
        stand_length = st.number_input(
            "Stand length (ft)", 30.0, 120.0, 93.0, 1.0,
            help="93 ft for triples, 63 ft for doubles.",
        )

    with st.expander("Pump", expanded=False):
        pump_kind = st.selectbox("Type", ["triplex", "duplex"], index=0)
        liner_id = st.number_input("Liner ID (in)", 3.0, 9.0, 6.5, 0.125)
        stroke_len = st.number_input("Stroke length (in)", 6.0, 20.0, 12.0, 0.5)
        pump_eff = st.slider("Volumetric efficiency", 0.70, 1.00, 0.95, 0.01)
        rod_od = (
            st.number_input("Rod OD (in)", 0.5, 4.0, 2.0, 0.125)
            if pump_kind == "duplex" else 0.0
        )

    st.header("Kick data")
    mw = st.number_input("Mud weight (ppg)", 8.0, 20.0, defaults["mw"], 0.1)
    sidpp = st.number_input("SIDPP (psi)", 0.0, 8000.0, defaults["sidpp"], 10.0)
    sicp = st.number_input("SICP (psi)", 0.0, 8000.0, defaults["sicp"], 10.0)
    pit_gain = st.number_input("Pit gain (bbl)", 0.0, 500.0,
                               defaults["pit_gain"], 1.0)
    scr_spm = st.number_input("Kill rate (spm)", 5.0, 120.0,
                              defaults["scr_spm"], 1.0)
    scr_psi = st.number_input("SCR pressure at kill rate (psi)", 0.0, 5000.0,
                              defaults["scr_psi"], 10.0)
    frac = st.number_input(
        "Fracture gradient at shoe (ppg EMW)", 0.0, 25.0, defaults["frac"], 0.05,
        help="From the leak-off test. Set to 0 if unknown -- the shoe cannot "
             "then be checked.",
    )
    safety = st.number_input("Kill mud safety factor (ppg)", 0.0, 2.0, 0.0, 0.1)
    migration = st.number_input(
        "Gas migration rate (ft/hr)", 0.0, 5000.0, 0.0, 50.0,
        help="From the rate of shut-in pressure rise: rate = (psi/hr) / "
             "(0.052 x MW). Used by the Volumetric and Stripping methods. Leave "
             "at 0 if gas is not migrating.",
    )

    st.header("Display")
    theme = st.radio(
        "Chart theme", ["light", "dark"], horizontal=True,
        index=["light", "dark"].index(detect_theme()),
        help="Defaults to whichever theme the app is running in, so the plots "
             "do not sit as a bright slab on a dark page.",
    )

values = dict(
    total_md=total_md, shoe_md=shoe_md, casing_id=casing_id, hole_id=hole_id,
    bit_md=bit_md, dp_od=dp_od, dp_id=dp_id, dc_od=dc_od, dc_id=dc_id,
    dc_len=dc_len, mw=mw, sidpp=sidpp, sicp=sicp, pit_gain=pit_gain,
    scr_spm=scr_spm, scr_psi=scr_psi, frac=frac, clf=clf, safety=safety,
    air_gap=air_gap, water_depth=water_depth, riser_id=riser_id,
    kop=kop, build=build, liner_id=liner_id, stroke_len=stroke_len,
    pump_eff=pump_eff, pump_kind=pump_kind, rod_od=rod_od,
    dp_weight=dp_weight, stand_length=stand_length, migration=migration,
)

# --------------------------------------------------------------------------
# Imports
# --------------------------------------------------------------------------

imported_survey = None
imported_components = None
import_notes: list[str] = []

if survey_file is not None:
    try:
        outcome = import_survey(read_upload_as_csv(survey_file))
        imported_survey = outcome.survey
        import_notes.extend(outcome.warnings)
        derived = trajectory_from_survey(imported_survey)
        if derived is not config.trajectory:
            config = WellConfiguration(config.bop, derived)
            import_notes.append(
                f"Trajectory taken from the survey as **{derived.label}** "
                f"(maximum inclination {imported_survey.max_inclination_deg:.1f}°), "
                "overriding the selection above."
            )
        st.success(
            f"**Survey imported** — {outcome.rows_read} stations, "
            f"{imported_survey.total_md:,.0f} ft MD / "
            f"{imported_survey.total_tvd:,.0f} ft TVD, "
            f"max inclination {imported_survey.max_inclination_deg:.1f}°, "
            f"max dogleg {imported_survey.max_dogleg_severity:.1f}°/100 ft. "
            + ("TVD computed by minimum curvature."
               if outcome.tvd_was_computed else "TVD taken from the file.")
        )
    except ValueError as exc:
        st.error(f"**Survey import failed:** {exc}")
        st.stop()

if tally_file is not None:
    try:
        outcome = import_tally(
            read_upload_as_csv(tally_file),
            bottom_up=tally_bottom_up,
            expected_bit_depth_ft=bit_md,
        )
        imported_components = outcome.components
        import_notes.extend(outcome.warnings)
        st.success(
            f"**Tally imported** — {len(outcome.components)} components, "
            f"{outcome.total_length_ft:,.0f} ft total, "
            f"{outcome.internal_volume_bbl:,.1f} bbl internal volume."
        )
    except ValueError as exc:
        st.error(f"**Tally import failed:** {exc}")
        st.stop()

if import_notes:
    with st.expander(f"📄 {len(import_notes)} note(s) on the imported data",
                     expanded=True):
        for note in import_notes:
            st.markdown(f"- {note}")

# --------------------------------------------------------------------------
# Build and plan
# --------------------------------------------------------------------------

try:
    well = build_well(config, values, imported_survey, imported_components)
except Exception as exc:
    st.error(f"**Cannot build this well:** {exc}")
    st.stop()

try:
    plan = get_method(method.key).plan(well)
except Exception as exc:
    st.error(f"**{method.name} could not be planned:** {exc}")
    st.stop()


chapter = getattr(method, "chapter", "")
if chapter and chapter_exists(chapter):
    depth = getattr(method, "chapter_depth", "in context")
    link_column, _ = st.columns([2, 3])
    # Handled on the button's return value rather than an on_click callback:
    # st.switch_page is a rerun, and Streamlit makes reruns a no-op inside
    # callbacks, so the click would silently do nothing.
    if link_column.button(
        f"📖  Read the theory — {chapter_title(chapter)}",
        help=f"Covers {method.name} {depth}.",
        width="stretch",
    ):
        st.session_state["learn_chapter"] = chapter
        try:
            st.switch_page(LEARN_PAGE_PATH)
        except Exception:
            # This page is running standalone rather than under navigation.
            # The chapter is remembered, so Learn opens at it regardless.
            pass

# --------------------------------------------------------------------------
# Applicability
# --------------------------------------------------------------------------

applicability = plan.applicability
if applicability.blockers:
    st.error(
        f"**{method.name} is not usable on this well.**\n\n"
        + "\n\n".join(f"- {b}" for b in applicability.blockers)
    )
if applicability.warnings:
    count = len(applicability.warnings)
    with st.expander(
        f"⚠ {count} thing{'' if count == 1 else 's'} to watch — "
        f"{method.name} on a {config.label} well",
        expanded=bool(applicability.blockers),
    ):
        for warning in applicability.warnings:
            st.markdown(f"- {warning}")

# --------------------------------------------------------------------------
# Headline numbers
# --------------------------------------------------------------------------

st.subheader("Kill sheet numbers")

metrics = st.columns(4)
metrics[0].metric("Kill mud weight", f"{well.kill_mud_weight_ppg:.2f} ppg",
                  f"{well.kill_mud_weight_ppg - well.mud_weight_ppg:+.2f}")
metrics[1].metric("Formation pressure", f"{well.formation_pressure_psi:,.0f} psi")
metrics[2].metric("Bit TVD", f"{well.bit_tvd_ft:,.0f} ft",
                  f"{well.bit_tvd_ft - well.bit_md_ft:+,.0f} vs MD")

peak_shoe = plan.peak_shoe_pressure_psi
if peak_shoe is not None and plan.shoe_frac_pressure_psi:
    margin = plan.shoe_frac_pressure_psi - peak_shoe
    metrics[3].metric(
        "Margin at shoe", f"{margin:,.0f} psi",
        delta=None if margin >= 0 else "FRACTURES",
        delta_color="inverse" if margin < 0 else "normal",
    )
else:
    metrics[3].metric("Peak casing",
                      f"{plan.peak_casing_pressure_psi or 0:,.0f} psi")

with st.expander("All calculated values", expanded=False):
    st.dataframe(
        as_text_rows([{"Quantity": k, "Value": v}
                      for k, v in plan.summary.items()]),
        width="stretch", hide_index=True,
    )

# --------------------------------------------------------------------------
# Plots
# --------------------------------------------------------------------------

st.subheader("Kill diagnostics")
try:
    st.pyplot(plot_kill(plan, theme=theme))
except ValueError as exc:
    st.info(str(exc))

with st.expander("Plot data as a table", expanded=False):
    st.dataframe(trace_table(plan, every=3), width="stretch", hide_index=True)

# --------------------------------------------------------------------------
# Schedule by section
# --------------------------------------------------------------------------

if method.key in ("drillers", "wait_and_weight"):
    st.subheader("Drill pipe pressure schedule, by section")
    segments = well.kill_sheet.schedule_segments()

    if len(segments) == 1:
        st.caption(
            "One section — a uniform string in a vertical hole, so the classic "
            "single straight line from ICP to FCP is correct here."
        )
    else:
        st.caption(
            f"{len(segments)} sections. The schedule is only a straight line "
            "within a section; it changes slope wherever the TVD gained per "
            "foot changes (the build) or the volume per foot changes (a pipe "
            "size change)."
        )

    st.dataframe(
        as_text_rows([
            {
                "Section": s.name,
                "Break": s.reason,
                "From (ft MD)": round(s.top_md),
                "To (ft MD)": round(s.bottom_md),
                "Strokes": round(s.strokes),
                "DP psi": f"{s.start_pressure_psi:,.0f} → {s.end_pressure_psi:,.0f}",
                "psi / 100 stk": f"{s.slope_psi_per_100_strokes:+.1f}",
                "Direction": "TAKE UP" if s.pressure_rises else "take down",
            }
            for s in segments
        ]),
        width="stretch", hide_index=True,
    )

    rising = [s for s in segments if s.pressure_rises]
    if rising:
        names = ", ".join(sorted({s.name for s in rising}))
        st.warning(
            f"**Drill pipe pressure has to be taken UP through the {names} "
            "section, not down.** The kill mud front gains no true vertical "
            "depth there, so hydrostatic stops increasing while friction keeps "
            "climbing toward FCP. A single straight line from ICP to FCP would "
            "have the driller bleeding pressure off exactly where he should be "
            "adding it."
        )

# --------------------------------------------------------------------------
# Procedure
# --------------------------------------------------------------------------

st.subheader("Procedure")
for index, stage in enumerate(plan.stages, 1):
    header = stage.name
    if stage.strokes:
        header += f"  ·  {stage.strokes:,.0f} strokes"
    with st.expander(header, expanded=index <= 2):
        st.markdown(f"*{stage.description}*")
        for action in stage.actions:
            st.markdown(f"- {action}")
        if stage.table:
            st.dataframe(as_text_rows(stage.table), width="stretch",
                         hide_index=True)

if plan.notes:
    with st.expander("Notes on this method and well type", expanded=False):
        for note in plan.notes:
            st.markdown(f"- {note}")

# --------------------------------------------------------------------------
# Stripping is half an operation -- plan the kill that follows it
# --------------------------------------------------------------------------

if method.key == "stripping" and plan.applicability.usable:
    st.divider()
    st.subheader("Then kill the well")
    st.caption(
        "Stripping restores the circulation path; it does not kill the well. "
        "This plans the circulating kill that follows, with the bit back on "
        "bottom, and totals both phases."
    )

    follow_on = st.radio(
        "Kill method once on bottom",
        ["drillers", "wait_and_weight"],
        format_func=lambda k: get_method(k).name,
        horizontal=True,
        key="follow_on_method",
    )

    try:
        sequence = strip_then_kill(well, follow_on)
    except Exception as exc:
        st.error(f"**Could not plan the follow-on kill:** {exc}")
    else:
        totals = st.columns(3)
        totals[0].metric("Total time, both phases",
                         f"{sequence.total_hours:.1f} hr")
        peak = sequence.peak_shoe_pressure_psi
        if peak is not None:
            totals[1].metric("Peak shoe, whole operation", f"{peak:,.0f} psi")
            totals[2].metric("Worst phase", sequence.worst_phase)

        if sequence.worst_phase == "stripping":
            st.info(
                "**The stripping phase, not the kill, puts the most pressure "
                "on the shoe here.** Planning the kill in isolation would miss "
                "that."
            )

        if sequence.blockers:
            st.error("\n\n".join(f"- {b}" for b in sequence.blockers))

        st.dataframe(
            as_text_rows([{"Quantity": k, "Value": v}
                          for k, v in sequence.summary().items()]),
            width="stretch", hide_index=True,
        )

        with st.expander(
            f"Diagnostics for the {sequence.kill.method_name} phase",
            expanded=False,
        ):
            try:
                st.pyplot(plot_kill(sequence.kill, theme=theme))
            except ValueError as exc:
                st.info(str(exc))

        with st.expander(
            f"Procedure for the {sequence.kill.method_name} phase",
            expanded=False,
        ):
            for stage in sequence.kill.stages:
                st.markdown(f"**{stage.name}**")
                st.markdown(f"*{stage.description}*")
                for action in stage.actions:
                    st.markdown(f"- {action}")

st.divider()
st.caption(
    "These are engineering aids, not a substitute for certification, a company "
    "well control manual, or the judgement of a competent person on the rig."
)
