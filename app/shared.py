"""Helpers shared by the app's pages.

Kept separate so each page stays a readable script, and so the pages can be
run individually -- which is how they are tested.
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "src"
if _SRC.exists() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from wellcontrol import content as content_lib  # noqa: E402
from wellcontrol.geometry import (  # noqa: E402
    Annulus,
    DrillString,
    HoleSection,
    Pipe,
    Pump,
    Wellbore,
)
from wellcontrol.killsheet import SlowCirculatingRate  # noqa: E402
from wellcontrol.methods.base import WellState  # noqa: E402
from wellcontrol.subsea import SubseaGeometry  # noqa: E402
from wellcontrol.survey import Survey  # noqa: E402
from wellcontrol.wellconfig import Trajectory, WellConfiguration  # noqa: E402

CONTENT_DIR = _ROOT / "content"

#: Path used by the planner to jump to the Learn page, relative to the entry
#: script as Streamlit requires.
LEARN_PAGE_PATH = "views/learn.py"
PLANNER_PAGE_PATH = "views/planner.py"


# --------------------------------------------------------------------------
# Content
# --------------------------------------------------------------------------

@st.cache_data(show_spinner=False)
def chapter_files() -> list[str]:
    return content_lib.chapter_files(CONTENT_DIR)


@st.cache_data(show_spinner=False)
def chapter_title(filename: str) -> str:
    return content_lib.chapter_title(filename, CONTENT_DIR)


@st.cache_data(show_spinner=False)
def chapter_body(filename: str) -> str:
    return content_lib.chapter_body(filename, CONTENT_DIR)


def chapter_exists(filename: str) -> bool:
    return content_lib.chapter_exists(filename, CONTENT_DIR)


@st.cache_data(show_spinner=False)
def referenced_chapters(filename: str) -> list[str]:
    return content_lib.referenced_chapters(filename, CONTENT_DIR)


# --------------------------------------------------------------------------
# Display
# --------------------------------------------------------------------------

def as_text_rows(rows: list[dict]) -> list[dict]:
    """Render every cell as text before handing a table to Streamlit.

    These tables mix types within a column on purpose -- a snub force is a
    number when snubbing is needed and "-" when it is not, and the summary
    holds both "10000 / 10000" and 5392. Arrow cannot type a mixed column, so
    Streamlit falls back with a logged traceback on every rerun. Formatting to
    text up front avoids the round trip and the noise.
    """
    return [{k: ("" if v is None else str(v)) for k, v in row.items()}
            for row in rows]


def detect_theme() -> str:
    """Whichever theme Streamlit is rendering in, so the plots match it.

    A light figure on a dark page reads as a bright slab. Streamlit has moved
    this API around between versions, so try the current one, then the older
    config option, then fall back to light.
    """
    try:
        base = st.context.theme.type
        if base in ("light", "dark"):
            return base
    except Exception:
        pass
    try:
        base = st.get_option("theme.base")
        if base in ("light", "dark"):
            return base
    except Exception:
        pass
    return "light"


def inject_industrial_theme() -> None:
    """Inject the Cyberbase & Heavy Industrial rig console theme into Streamlit."""
    st.markdown(
        """
        <style>
        /* Cyberbase & Heavy Industrial Rig Console Theme (Images 1 - 4) */
        .stApp {
            background: radial-gradient(circle at 50% 10%, #151c24 0%, #090c10 100%);
            color: #f1f5f9;
        }
        /* Metal panel card styling in authentic brushed stainless steel */
        div[data-testid="stVerticalBlockBorderWrapper"] {
            background: 
                repeating-linear-gradient(90deg, rgba(255,255,255,0.08) 0px, rgba(255,255,255,0.08) 1px, transparent 1px, transparent 3px),
                repeating-linear-gradient(0deg, rgba(0,0,0,0.05) 0px, rgba(0,0,0,0.05) 1px, transparent 1px, transparent 2px),
                linear-gradient(120deg, #7c8691 0%, #a2adb9 14%, #757f8a 27%, #c3ceda 42%, #8a94a0 56%, #d0dbe6 72%, #79838e 86%, #9ba5b1 100%) !important;
            border: 1px solid #5a6675 !important;
            border-radius: 6px !important;
            box-shadow: 
                inset 1.5px 1.5px 0 rgba(255, 255, 255, 0.85),
                inset 2.5px 2.5px 5px rgba(255, 255, 255, 0.45),
                inset -1.5px -1.5px 0 rgba(0, 0, 0, 0.8),
                inset -3px -3px 6px rgba(0, 0, 0, 0.45),
                0 8px 24px rgba(0,0,0,0.65) !important;
            color: #0f172a !important;
        }
        div[data-testid="stMetric"] {
            background: #06090d !important;
            border: 2px solid #2d3748 !important;
            border-radius: 6px !important;
            padding: 8px 12px !important;
            box-shadow: inset 2.5px 2.5px 6px #000, 0 1.5px 0 rgba(255,255,255,0.85) !important;
        }
        div[data-testid="stMetricLabel"] {
            color: #94a3b8 !important;
            font-weight: 800 !important;
            text-transform: uppercase !important;
            letter-spacing: 0.06em !important;
        }
        /* Buttons styled like industrial switchgear */
        .stButton > button, div[data-testid="stLinkButton"] > a {
            background: linear-gradient(180deg, #475466 0%, #262e38 100%) !important;
            color: #f8fafc !important;
            border: 1.5px solid #64748b !important;
            border-radius: 4px !important;
            font-weight: 800 !important;
            letter-spacing: 0.04em !important;
            box-shadow: inset 0 1px 0 rgba(255,255,255,0.35), 0 2px 4px rgba(0,0,0,0.5) !important;
        }
        .stButton > button:hover, div[data-testid="stLinkButton"] > a:hover {
            filter: brightness(1.2) !important;
            border-color: #38bdf8 !important;
        }
        /* Primary buttons */
        .stButton > button[kind="primary"], div[data-testid="stLinkButton"] > a[kind="primary"] {
            background: linear-gradient(180deg, #0284c7 0%, #0369a1 100%) !important;
            border-color: #38bdf8 !important;
            box-shadow: 0 0 10px rgba(56, 189, 248, 0.4), inset 0 1px 0 rgba(255,255,255,0.3) !important;
        }
        /* Metrics / digital readouts */
        div[data-testid="stMetricValue"] {
            font-family: Consolas, "Liberation Mono", monospace !important;
            color: #38f87b !important;
            font-weight: 800 !important;
            text-shadow: 0 0 8px rgba(56, 248, 123, 0.4) !important;
        }
        /* Headers with industrial stamped look */
        h1, h2, h3 {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif !important;
            font-weight: 800 !important;
            letter-spacing: 0.05em !important;
            text-transform: uppercase !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


# --------------------------------------------------------------------------
# Well construction
# --------------------------------------------------------------------------

DEFAULTS: dict[str, dict[str, float]] = {
    "surface_vertical": dict(
        total_md=10000.0, shoe_md=7000.0, casing_id=8.681, hole_id=8.5,
        bit_md=10000.0, dp_od=5.0, dp_id=4.276, dc_od=6.5, dc_id=2.8125,
        dc_len=600.0, mw=9.6, sidpp=400.0, sicp=660.0, pit_gain=20.0,
        scr_spm=30.0, scr_psi=600.0, frac=12.85, clf=0.0,
        air_gap=0.0, water_depth=0.0, kop=0.0, build=3.0, dp_weight=19.5,
    ),
    "surface_horizontal": dict(
        total_md=12000.0, shoe_md=7200.0, casing_id=8.681, hole_id=6.0,
        bit_md=12000.0, dp_od=4.0, dp_id=3.34, dc_od=4.75, dc_id=2.25,
        dc_len=400.0, mw=10.2, sidpp=350.0, sicp=350.0, pit_gain=15.0,
        scr_spm=35.0, scr_psi=750.0, frac=13.5, clf=0.0,
        air_gap=0.0, water_depth=0.0, kop=4000.0, build=3.0, dp_weight=14.0,
    ),
    # An imported survey can classify a well as deviated, which is the most
    # common real trajectory -- a build to a tangent angle that is nowhere near
    # 90 degrees. These entries keep DEFAULTS in step with the trajectories
    # WellConfiguration.from_key already accepts.
    "surface_deviated": dict(
        total_md=12500.0, shoe_md=8200.0, casing_id=8.681, hole_id=8.5,
        bit_md=12500.0, dp_od=5.0, dp_id=4.276, dc_od=6.75, dc_id=3.0,
        dc_len=700.0, mw=12.2, sidpp=480.0, sicp=776.0, pit_gain=22.0,
        scr_spm=30.0, scr_psi=320.0, frac=15.8, clf=0.0,
        air_gap=0.0, water_depth=0.0, kop=2000.0, build=2.5, dp_weight=19.5,
    ),
    "subsea_deviated": dict(
        total_md=16000.0, shoe_md=11500.0, casing_id=12.415, hole_id=12.25,
        bit_md=16000.0, dp_od=5.5, dp_id=4.778, dc_od=8.0, dc_id=3.0,
        dc_len=800.0, mw=12.5, sidpp=500.0, sicp=700.0, pit_gain=25.0,
        scr_spm=30.0, scr_psi=900.0, frac=13.8, clf=350.0,
        air_gap=75.0, water_depth=5000.0, kop=3000.0, build=2.5, dp_weight=21.9,
    ),
    "subsea_vertical": dict(
        total_md=15000.0, shoe_md=11000.0, casing_id=12.415, hole_id=12.25,
        bit_md=15000.0, dp_od=5.5, dp_id=4.778, dc_od=8.0, dc_id=3.0,
        dc_len=800.0, mw=12.5, sidpp=500.0, sicp=664.0, pit_gain=25.0,
        scr_spm=30.0, scr_psi=900.0, frac=13.8, clf=350.0,
        air_gap=75.0, water_depth=5000.0, kop=0.0, build=3.0, dp_weight=21.9,
    ),
    "subsea_horizontal": dict(
        total_md=17000.0, shoe_md=12200.0, casing_id=8.681, hole_id=6.0,
        bit_md=17000.0, dp_od=4.0, dp_id=3.34, dc_od=4.75, dc_id=2.25,
        dc_len=400.0, mw=12.5, sidpp=450.0, sicp=450.0, pit_gain=20.0,
        scr_spm=30.0, scr_psi=950.0, frac=14.2, clf=350.0,
        air_gap=75.0, water_depth=5000.0, kop=9000.0, build=3.0, dp_weight=14.0,
    ),
}


def read_upload_as_csv(uploaded) -> str:
    """CSV text from an uploaded file, converting Excel on the way.

    Tallies in particular tend to live in spreadsheets rather than CSV, so
    accepting both saves a manual export step -- and a manual export step is
    where transcription errors get introduced.
    """
    name = getattr(uploaded, "name", "").lower()
    data = uploaded.getvalue()
    if name.endswith((".xlsx", ".xlsm", ".xls")):
        import io as _io

        import pandas as pd

        frame = pd.read_excel(_io.BytesIO(data))
        return frame.to_csv(index=False)
    return data.decode("utf-8-sig", errors="replace")


def trajectory_from_survey(survey: Survey) -> Trajectory:
    """Classify a real survey, rather than trusting a dropdown.

    Once a survey is imported the trajectory is a fact about the file, not a
    choice -- and getting it wrong changes which warnings a method raises.
    """
    peak = survey.max_inclination_deg
    if peak >= 85.0:
        return Trajectory.HORIZONTAL
    if peak >= 1.0:
        return Trajectory.DEVIATED
    return Trajectory.VERTICAL


def build_well(
    config: WellConfiguration,
    values: dict,
    survey_override: Survey | None = None,
    string_components: list[Pipe] | None = None,
) -> WellState:
    """Assemble a WellState from the form values.

    Args:
        config: BOP location and trajectory.
        values: The sidebar inputs.
        survey_override: An imported survey, used in place of the generated
            trajectory.
        string_components: An imported pipe tally, bottom-up. The last item is
            treated as the drill pipe and resized to reach the bit.

    Raises:
        ValueError: with a message suitable for showing to the user.
    """
    total_md = values["total_md"]
    shoe_md = values["shoe_md"]
    bit_md = values["bit_md"]

    if survey_override is not None:
        # A real survey stops behind the bit, so the file routinely ends a
        # stand or so above total depth. Hold the last inclination through the
        # unsurveyed hole; extended_to() rejects the case where the gap is too
        # large to be the usual sensor offset.
        survey = survey_override.extended_to(total_md)
    elif config.is_horizontal:
        survey = Survey.build_and_hold(
            kop_md=values["kop"],
            build_rate_deg_per_100ft=values["build"],
            final_inclination_deg=90.0,
            total_md=total_md,
        )
    else:
        survey = Survey.vertical(total_md)

    sections: list[HoleSection] = []
    if config.is_subsea:
        mudline = values["air_gap"] + values["water_depth"]
        if mudline >= shoe_md:
            raise ValueError(
                f"The mudline ({mudline:,.0f} ft) is at or below the casing shoe "
                f"({shoe_md:,.0f} ft). Check the air gap, water depth and shoe "
                "depth."
            )
        sections.append(
            HoleSection("Riser", id_in=values["riser_id"], top_md=0.0,
                        bottom_md=mudline)
        )
        sections.append(
            HoleSection("Casing", id_in=values["casing_id"], top_md=mudline,
                        bottom_md=shoe_md)
        )
    else:
        sections.append(
            HoleSection("Casing", id_in=values["casing_id"], top_md=0.0,
                        bottom_md=shoe_md)
        )
    sections.append(
        HoleSection("Open Hole", id_in=values["hole_id"], top_md=shoe_md,
                    bottom_md=total_md, is_open_hole=True)
    )
    wellbore = Wellbore(sections)

    if string_components:
        if len(string_components) == 1:
            string = DrillString.from_bha([], string_components[0], bit_md)
        else:
            string = DrillString.from_bha(
                list(string_components[:-1]), string_components[-1], bit_md
            )
    else:
        collars = Pipe(
            name="Drill Collars", od_in=values["dc_od"], id_in=values["dc_id"],
            length_ft=values["dc_len"],
        )
        drill_pipe = Pipe(
            name="Drill Pipe", od_in=values["dp_od"], id_in=values["dp_id"],
            length_ft=0.0,
        )
        string = DrillString.from_bha([collars], drill_pipe, bit_md)
    annulus = Annulus(wellbore, string)

    pump = Pump(
        liner_id_in=values["liner_id"],
        stroke_length_in=values["stroke_len"],
        efficiency=values["pump_eff"],
        kind=values["pump_kind"],
        rod_od_in=values.get("rod_od", 0.0),
    )

    subsea = None
    if config.is_subsea:
        subsea = SubseaGeometry(
            air_gap_ft=values["air_gap"],
            water_depth_ft=values["water_depth"],
        )

    return WellState(
        annulus=annulus,
        survey=survey,
        pump=pump,
        mud_weight_ppg=values["mw"],
        slow_circulating_rate=SlowCirculatingRate(
            spm=values["scr_spm"], pressure_psi=values["scr_psi"]
        ),
        configuration=config,
        sidpp_psi=values["sidpp"],
        sicp_psi=values["sicp"],
        pit_gain_bbl=values["pit_gain"],
        frac_gradient_ppg=values["frac"] or None,
        shoe_md_ft=shoe_md,
        subsea=subsea,
        choke_line_friction_psi=values["clf"],
        safety_factor_ppg=values["safety"],
        pipe_weight_lb_per_ft=values["dp_weight"],
        stand_length_ft=values["stand_length"],
        gas_migration_rate_ft_per_hr=values["migration"],
    )
