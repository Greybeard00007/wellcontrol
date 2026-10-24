"""Well Control Kill Planner -- Streamlit front end.

Run with::

    streamlit run app/streamlit_app.py

or double-click ``Launch Kill Planner.bat``.

Two pages:

* **Kill Planner** (``views/planner.py``) -- pick a well configuration and a
  kill method, enter the well and kick data, and get the procedure with
  diagnostic plots. Each plan links to the chapter explaining its method.
* **Learn** (``views/learn.py``) -- the teaching content from ``content/``,
  rendered in the app.

The pages are separate scripts rather than functions so each can be run and
tested on its own. Shared helpers live in ``shared.py``.
"""

from __future__ import annotations

import streamlit as st
import shared

st.set_page_config(
    page_title="Well Control Kill Planner",
    page_icon="🛢",
    layout="wide",
)

shared.inject_industrial_theme()

st.navigation([
    st.Page("views/planner.py", title="Kill Planner", icon="🛢", default=True),
    st.Page("views/learn.py", title="Learn", icon="📖"),
    st.Page("views/simulator.py", title="Simulator & Teacher Station", icon="🎮"),
]).run()
