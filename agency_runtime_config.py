"""Read server configuration without requiring Streamlit Cloud."""
import os

def setting(name, default=None):
    value = os.environ.get(name)
    if value is not None and value.strip():
        return value.strip()
    try:
        import streamlit as st
        from streamlit.errors import StreamlitSecretNotFoundError
    except ImportError:
        return default
    try:
        return st.secrets.get(name, default)
    except (StreamlitSecretNotFoundError, FileNotFoundError, OSError):
        return default
