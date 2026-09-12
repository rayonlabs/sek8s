"""Chute log shipper: standalone VM agent that discovers chute pods via CRI,
reads their logs off disk, and streams them to the validator over mTLS.

See docs/specs/chute-log-shipper.md for the design.
"""
