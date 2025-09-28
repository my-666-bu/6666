import streamlit as st
import time
import numpy as np

st.title("实时折线图")
chart = st.line_chart(np.random.randn(1, 1))

for i in range(1000):
    chart.add_rows(np.random.randn(1, 1))
    time.sleep(1)
