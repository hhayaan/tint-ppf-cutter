"""Entry point for the native, read-only ACE catalog exporter.

The experimental pure-Python page reader was rejected after native ACE
comparison exposed shifted variable text fields. Production export uses ACE.
"""
from export_catalog_ace import main

if __name__ == "__main__":
    main()
