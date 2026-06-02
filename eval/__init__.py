"""VARD research eval harness.

Measures VARD as the dependent variable. The instrument is the *dark-gold* metric:
of the gold spans a real fix touched, which ones are NOT reachable by any conventional
retrieval channel (lexical / semantic / import-graph)? Those are the only spans where a
retrieval layer can beat the agent's own search. Everything here exists to quantify how
much of that dark gold a given retriever recovers.

Not shipped with the package — research only.
"""
