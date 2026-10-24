"""Well Control Simulator & Teacher Station launcher view for Streamlit."""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

_APP = Path(__file__).resolve().parent.parent
if str(_APP) not in sys.path:
    sys.path.insert(0, str(_APP))

import shared
from wellcontrol.sim.scenario import SCENARIOS

shared.inject_industrial_theme()

st.title("🎮 Well Control Simulator & Cyberbase Console")

st.markdown(
    """
    The simulator forward-models a rig under the student's hands in real time.
    Bottomhole pressure, casing shoe integrity, choke manifold backpressure,
    and fluid column physics respond dynamically to live inputs.
    """
)

# Backend status check
simulator_url = "http://localhost:8000"
crew_url = "http://localhost:8000/crew"
teacher_url = "http://localhost:8000/teacher"

col1, col2, col3 = st.columns(3)

with col1:
    st.subheader("🖥️ Student Panel")
    st.markdown(
        """
        - **Instruments**: SPP, Casing, SPM, Pit Gain.
        - **Full Rig Console**: Choke, BOP, P&ID synoptics.
        - **Rig Audio**: Procedural pumps, choke hiss, sirens.
        - **Step Guide**: Real-time checklists.
        """
    )
    st.link_button("🚀 Open Choke Panel", simulator_url, type="primary")

with col2:
    st.subheader("👥 Crew CRM Mode")
    st.markdown(
        """
        - **Multiplayer Roles**: Driller, Choke, Toolpusher, Mud.
        - **Real-Time WebSockets**: Live shared well synchronization.
        - **Tactical Intercom**: Standard IADC/IWCF radio callouts.
        - **Team Discipline**: Role-authorized controls & locks.
        """
    )
    st.link_button("👥 Open Crew Console", crew_url, type="primary")

with col3:
    st.subheader("🎓 Master Station")
    st.markdown(
        """
        - **Live Grid**: Monitor all active students.
        - **Fault Injector**: Trigger surprise rig faults.
        - **Master Freeze**: Pause runs for coaching.
        - **AI Gradebook**: IADC scoring and CSV export.
        """
    )
    st.link_button("🎓 Open Teacher Station", teacher_url, type="secondary")

st.divider()

st.subheader("📋 Scenario Direct Launch Presets")
st.markdown("Launch directly into a specific well control exercise:")

scenario_cols = st.columns(3)
for idx, sc in enumerate(SCENARIOS):
    col = scenario_cols[idx % 3]
    with col:
        with st.container(border=True):
            st.markdown(f"**{sc.name}**")
            st.caption(sc.objective)
            st.link_button(
                f"Launch '{sc.key}'",
                f"{simulator_url}/?scenario={sc.key}",
                use_container_width=True,
            )

st.divider()

st.subheader("🖥️ Embedded Simulator Preview")
st.caption(
    "If the simulator backend is running locally on port 8000, it can be flown directly below. "
    "If not yet started, launch it from terminal with: `uvicorn wellcontrol.sim.api:app --reload --port 8000`"
)

with st.expander("Show Embedded Simulator in Page", expanded=False):
    components.iframe(simulator_url, height=840, scrolling=True)
